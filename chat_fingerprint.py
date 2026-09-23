"""Per-chat fingerprint isolation (Synergy 9 — Phase 2).

Source: eroslifestyle/ai-router-switch (the most mature repo in the surveyed
set: 930 tests, 679 commits). Upstream definition from the synergy research:

    "Compute SHA-256 of the first user message, pin-on-first-use to a single
    upstream chat_id. Defeats conversation-merge attacks where two unrelated
    client sessions are concatenated server-side and leak context across
    users. The fingerprint is the unit of isolation, not the bearer token."

Adaptation to this proxy
------------------------
AutoClaw cloud chat-completions is, from our side, a stateless POST: the full
message history rides in every request, so there is no upstream chat_id to
pin. The unit of upstream state we DO rotate is the account (a tokens.json
entry picked round-robin per request). Conversation-merge in this topology
means two concurrent conversations sharing one proxy interleave accounts
turn-by-turn — splitting one conversation's billing across accounts and
making per-account upstream session affinity impossible.

We therefore pin each chat fingerprint to the account that served its first
request. Follow-up turns of the SAME conversation reuse that account
(affinity), while other fingerprints keep rotating normally. If the pinned
account becomes unusable (expired, exhausted, cached dead) the pin re-binds
to the account that actually served the turn (repin-on-drift).

The fingerprint itself is SHA-256 over the first user message content.
Clients that want explicit control may send an ``X-AutoClaw-Chat-Id`` header
(hashed the same way) to force identity without relying on message content.
Loop_breaker (Synergy 9 companion) keys its re-emit tracking off this
fingerprint, which is why the research gates 2.4 on 2.3.

Env knobs:
  AUTOCLAW_FINGERPRINT_ENABLED   1|0     master switch (default 1)
  AUTOCLAW_FINGERPRINT_TTL       secs    pin lifetime (default 86400)
  AUTOCLAW_FINGERPRINT_MAX       int     LRU capacity (default 10000)
"""

import hashlib
import json
import os
import threading
import time
from collections import OrderedDict

_ENABLED = os.environ.get("AUTOCLAW_FINGERPRINT_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
_TTL = int(os.environ.get("AUTOCLAW_FINGERPRINT_TTL", "86400"))
_MAX = int(os.environ.get("AUTOCLAW_FINGERPRINT_MAX", "10000"))

_LOCK = threading.Lock()
# fingerprint → {"email", "pinned_at", "last_seen", "turns", "repins"}
_PINS = OrderedDict()


def enabled():
    """True when per-chat fingerprint isolation is active."""
    return _ENABLED


def compute_fingerprint(messages, client_chat_id=None):
    """Derive the per-chat fingerprint for a request.

    Priority: an explicit ``X-AutoClaw-Chat-Id`` header (client-controlled,
    hashed with a domain tag) beats content hashing — operators can then
    restart a conversation client-side without changing the first message.

    Content hashing follows the upstream spec exactly: SHA-256 of the FIRST
    user message. Later messages are deliberately ignored — the first user
    message is the conversation's identity anchor; hashing the whole history
    would mint a new identity every turn and defeat pinning.

    Args:
        messages: OpenAI message list from the request body.
        client_chat_id: value of X-AutoClaw-Chat-Id (or None).

    Returns:
        64-char hex fingerprint, or None when there is no identifiable chat
        (empty messages, no user message, or feature disabled).
    """
    if not _ENABLED:
        return None
    if client_chat_id:
        return hashlib.sha256(("chatid:" + str(client_chat_id)).encode(
            "utf-8", errors="replace")).hexdigest()
    if not isinstance(messages, list):
        return None
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            # Multimodal content — hash only the text parts, in order;
            # image/audio parts carry no text and must not perturb the hash.
            content = "\n".join(
                p.get("text", "") for p in content
                if isinstance(p, dict) and p.get("text"))
        if not isinstance(content, str) or not content:
            return None
        return hashlib.sha256(("v1:" + content).encode(
            "utf-8", errors="replace")).hexdigest()
    return None


def pinned_email(fp):
    """Return the account email this fingerprint is pinned to (or None).

    Expired pins are evicted lazily here so the caller can treat a None
    result as "no valid pin" without a separate expiry check.
    """
    if not fp:
        return None
    now = time.time()
    with _LOCK:
        entry = _PINS.get(fp)
        if entry is None:
            return None
        if now - entry["pinned_at"] > _TTL:
            _PINS.pop(fp, None)
            return None
        entry["last_seen"] = now
        _PINS.move_to_end(fp)
        return entry["email"]


def bind(fp, email, reason="turn"):
    """Pin (or re-pin) a fingerprint to the account that served its turn.

    Pin-on-first-use: the first bind for a fingerprint wins. Subsequent
    binds with the SAME email just refresh bookkeeping (turns/last_seen).
    A bind with a DIFFERENT email means the previously pinned account could
    not serve this turn (expired/exhausted/refresh-failed) — the pin
    re-binds to the account that actually served it (repin-on-drift) so the
    conversation keeps affinity going forward instead of pinning to a dead
    account forever.

    Args:
        fp: fingerprint from compute_fingerprint (None → no-op).
        email: account email that actually served this request.
        reason: log label ("turn" / "repin") for observability.

    Returns:
        The effective pinned email after this call (echoes `email`).
    """
    if not fp or not email:
        return email
    now = time.time()
    with _LOCK:
        entry = _PINS.get(fp)
        if entry is None:
            _PINS[fp] = {
                "email": email, "pinned_at": now, "last_seen": now,
                "turns": 1, "repins": 0,
            }
            _PINS.move_to_end(fp)
        elif entry["email"] != email:
            entry.update({
                "email": email, "pinned_at": now, "last_seen": now,
                "turns": entry["turns"] + 1, "repins": entry["repins"] + 1,
            })
            _PINS.move_to_end(fp)
        else:
            entry["last_seen"] = now
            entry["turns"] += 1
            _PINS.move_to_end(fp)
        while len(_PINS) > _MAX:
            _PINS.popitem(last=False)
    return email


def unpin(fp):
    """Forget a pin (operator reset; loop_breaker fresh-restart bookkeeping)."""
    with _LOCK:
        _PINS.pop(fp, None)


def stats():
    """Observability block for /health. Never raises."""
    try:
        now = time.time()
        with _LOCK:
            live = sum(1 for e in _PINS.values()
                       if now - e["pinned_at"] <= _TTL)
            repins = sum(e["repins"] for e in _PINS.values())
            turns = sum(e["turns"] for e in _PINS.values())
        return {
            "enabled": _ENABLED,
            "pinned_chats": live,
            "capacity": _MAX,
            "ttl": _TTL,
            "turns_served": turns,
            "repins": repins,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {"enabled": _ENABLED, "error": str(exc)}


def reset():
    """Clear all pins (offline tests only)."""
    with _LOCK:
        _PINS.clear()
