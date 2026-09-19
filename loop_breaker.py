"""loop_breaker — stuck-conversation guard (Synergy 9 — Phase 2).

Source: eroslifestyle/ai-router-switch. Upstream definition from the synergy
research:

    "Detects stuck conversations (defined as four or more re-emits at eighty
    percent or more of context window). Returns HTTP 400 to force the client
    to start a fresh conversation rather than spinning on a context-rotted
    one. Prevents runaway token spend."

Re-emit semantics (faithful to the source)
------------------------------------------
A "re-emit" is the client re-sending the SAME conversation turn while the
request already fills ≥80% of the model's context window. Practical trigger:
an agent client (Claude Code, Cline, ...) retries a huge request in a loop
after timeouts / mid-stream failures — each retry burns the full input
pricing again and the model cannot possibly answer a context that is already
rotted.

Turn identity = hash of the LAST user message. A genuinely new turn (client
actually advanced the conversation) resets the streak, so legitimate long
conversations are never killed — only identical-payload retries of a
near-window request count.

The tracker is keyed by the per-chat fingerprint (chat_fingerprint), which
is exactly why the research dependency graph orders 2.3 (fingerprint) before
2.4 (loop_breaker): the fingerprint is the unit of tracking.

Response contract: HTTP 400, OpenAI error shape, type
"loop_breaker_triggered" — 400 (not 429) so strict OpenAI clients surface it
as a hard request error and abandon the loop instead of backing off and
retrying the same payload.

Env knobs:
  AUTOCLAW_LOOP_BREAKER_ENABLED  1|0     master switch (default 1)
  AUTOCLAW_LOOP_REEMITS          int     re-emits before trip (default 4)
  AUTOCLAW_LOOP_RATIO            float   context-fill threshold (default 0.8)
  AUTOCLAW_LOOP_TTL              secs    streak memory (default 3600)
  AUTOCLAW_CONTEXT_WINDOW        int     global override (tokens)
"""

import hashlib
import os
import threading
import time

from config import MODEL_MAP, DEFAULT_MODEL

_ENABLED = os.environ.get("AUTOCLAW_LOOP_BREAKER_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")
_REEMIT_LIMIT = int(os.environ.get("AUTOCLAW_LOOP_REEMITS", "4"))
_RATIO = float(os.environ.get("AUTOCLAW_LOOP_RATIO", "0.8"))
_TTL = int(os.environ.get("AUTOCLAW_LOOP_TTL", "3600"))
_WINDOW_OVERRIDE = os.environ.get("AUTOCLAW_CONTEXT_WINDOW", "")

# Conservative per-UPSTREAM-model context windows (prompt + completion
# budget, tokens). Same philosophy as OUTPUT_CAPS in config.py: cap
# aggressively — we protect spend, not benchmark scores. Override globally
# with AUTOCLAW_CONTEXT_WINDOW when upstream quotas change.
CONTEXT_WINDOWS = {
    "openrouter_glm-5.2": 131072,  # GLM-5.2 — matches probe-verified cap
    "zai_glm-5-turbo": 65536,      # GLM-5-Turbo — conservative
    "zai_auto": 32768,             # DeepSeek-V4-Pro route — cap hardest
    "zai_glm-5": 131072,
    "default": 65536,
}

_LOCK = threading.Lock()
# fingerprint → {"sig", "reemits", "first_seen", "last_seen", "tripped"}
_STREAKS = {}


def _context_window(upstream_model):
    if _WINDOW_OVERRIDE:
        try:
            return int(_WINDOW_OVERRIDE)
        except ValueError:
            pass
    return CONTEXT_WINDOWS.get(upstream_model, CONTEXT_WINDOWS["default"])


def estimate_tokens(messages):
    """Cheap token estimate for a message list (chars/4 + per-msg overhead).

    Deliberately a heuristic: the goal is catching ≥80%-full retries, not
    billing. Multimodal parts contribute their text fields only.
    """
    total = 0
    if not isinstance(messages, list):
        return 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                p.get("text", "") for p in content if isinstance(p, dict))
        if isinstance(content, str):
            total += len(content)
        total += 4  # role/framing overhead
    return total // 4


def _turn_signature(messages):
    """Hash of the last user message — the turn's identity."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    p.get("text", "") for p in content if isinstance(p, dict))
            return hashlib.sha256(str(content).encode(
                "utf-8", errors="replace")).hexdigest()[:16]
    return ""


def check(fp, messages, model_alias=None):
    """Observe a request and return a block verdict when the loop trips.

    Args:
        fp: per-chat fingerprint (chat_fingerprint.compute_fingerprint).
            None/empty → returns None (nothing to track against; the
            research dependency 2.3 → 2.4 in action).
        messages: client message list (BEFORE banner/DSML injection — the
            shim's protocol block would otherwise inflate the estimate by a
            constant on every request, including legit first turns).
        model_alias: client-facing model alias; mapped through MODEL_MAP to
            the upstream context window table.

    Returns:
        None when the request may proceed, otherwise a dict describing the
        trip: {"reemits", "ratio", "estimated_tokens", "context_window",
        "action": "block"} — caller renders the HTTP 400.
    """
    if not _ENABLED or not fp:
        return None
    now = time.time()
    upstream = MODEL_MAP.get(model_alias, DEFAULT_MODEL) if isinstance(
        model_alias, str) else DEFAULT_MODEL
    window = _context_window(upstream)
    est = estimate_tokens(messages)
    sig = _turn_signature(messages)
    ratio = (est / window) if window else 0.0

    with _LOCK:
        entry = _STREAKS.get(fp)
        if entry is None or now - entry["last_seen"] > _TTL:
            entry = {"sig": sig, "reemits": 0, "first_seen": now,
                     "last_seen": now, "tripped": 0}
            _STREAKS[fp] = entry
        if entry["sig"] != sig:
            # New turn → fresh streak (this is what keeps legit
            # conversations safe: only identical-turn retries accumulate).
            entry["sig"] = sig
            entry["reemits"] = 0
        if ratio >= _RATIO:
            entry["reemits"] += 1
        entry["last_seen"] = now
        if entry["reemits"] >= _REEMIT_LIMIT:
            entry["tripped"] += 1
            verdict = {
                "action": "block",
                "reemits": entry["reemits"],
                "ratio": round(ratio, 3),
                "estimated_tokens": est,
                "context_window": window,
            }
            return verdict
        # LRU-ish hygiene: drop stale streaks when the table runs hot.
        if len(_STREAKS) > 10000:
            cutoff = now - _TTL
            for k in [k for k, v in _STREAKS.items() if v["last_seen"] < cutoff]:
                _STREAKS.pop(k, None)
    return None


def release(fp):
    """Clear a fingerprint's streak (client acknowledged the 400 and
    restarted, or operator reset via !router)."""
    with _LOCK:
        _STREAKS.pop(fp, None)


def stats():
    """Observability block for /health. Never raises."""
    try:
        now = time.time()
        with _LOCK:
            live = sum(1 for e in _STREAKS.values()
                       if now - e["last_seen"] <= _TTL)
            tripped = sum(e["tripped"] for e in _STREAKS.values())
        return {
            "enabled": _ENABLED,
            "tracked_chats": live,
            "reemit_limit": _REEMIT_LIMIT,
            "ratio_threshold": _RATIO,
            "blocks_served": tripped,
        }
    except Exception as exc:  # pragma: no cover — defensive
        return {"enabled": _ENABLED, "error": str(exc)}


def reset():
    """Clear all streaks (offline tests only)."""
    with _LOCK:
        _STREAKS.clear()
