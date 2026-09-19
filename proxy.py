"""AutoClaw Proxy — OpenAI-compatible reverse proxy for AutoGLM/Z.ai

Endpoints:
  POST /v1/chat/completions   — OpenAI chat completions (stream + non-stream)
  GET  /v1/models             — list available models
  GET  /health                — health check
  GET  /accounts              — list stored accounts
  POST /refresh-all           — force refresh all tokens
  GET  /wallet                — check wallet balance
  GET  /ledger                — check billing ledger
  GET  /auth/callback-google  — OAuth callback handler (auto-captures code)
"""

import os
import json
import time
import uuid
import threading
import logging
from flask import Flask, request, Response, jsonify, g
import requests as req_lib
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

from config import (
    CHAT_COMPLETIONS, MODEL_MAP, DEFAULT_MODEL,
    PROXY_HOST, PROXY_PORT, TOKENS_FILE, VERSION,
    STRICT_MODEL_VALIDATION, PROXY_API_KEY, TLS_VERIFY,
    # Synergy 1: Output-cap clamping (clampMaxOutput / OUTPUT_CAPS)
    # Synergy 2: System-banner injection (AUTOCLAW_SYSTEM_BANNER)
    # Source: eequaled/GLM_proxy lib/core.js
    OUTPUT_CAPS, clamp_max_output, AUTOCLAW_SYSTEM_BANNER,
)
from auth import (
    get_valid_token, refresh_all, list_accounts,
    check_wallet, check_ledger, load_tokens, save_tokens,
)
# Synergy 3: Permanent-failure negative cache (createPermanentFailureCache)
# Source: eequaled/GLM_proxy lib/core.js
from cache import (
    is_permanent_failure_cached, mark_permanent_failure,
    clear_permanent_failures,
)
# Synergy 4: Chinese → English error translation (ZH_ERROR_MAP)
# Source: eequaled/GLM_proxy lib/core.js
from i18n_errors import translate_error
# Synergy 6: OWL-AGENT proxy defense layer (proxy-first routing, hedged
# racing, single-strike ban, direct fallback). Hybrid backend: external
# ~/.owl-agent install wins over the vendored owl_proxy.py.
import owl_bridge

# Synergy 9 (Phase 2): per-chat fingerprint isolation + loop_breaker.
# Source: eroslifestyle/ai-router-switch — SHA-256 of the first user message
# pinned on first use; loop_breaker 400s re-emit storms at ≥80% context fill.
import chat_fingerprint
import loop_breaker
# Synergy 8 (Phase 2): DSML tool-calling shim.
# Source: tt-52101/chat-z-ai-proxy-web2api-free — synthesises OpenAI
# function-calling (buffered + streaming) for tools-less upstreams.
import dsml_shim

# ─── Structured Logging ──────────────────────────────────────────────
logger = logging.getLogger("autoclaw.proxy")

TOKENS_FILE_FULL = TOKENS_FILE  # full path for backup operations

app = Flask(__name__)

@app.after_request
def _owl_via_header(resp):
    """Observability: expose which network path served the request."""
    try:
        via = g.get("_upstream_via")
        if via:
            resp.headers["X-Upstream-Via"] = via
    except Exception:
        pass
    return resp

# ── Pending OAuth login state (supports concurrent logins) ──
_pending_logins = {}  # keyed by state: {state: {"device_id":..., "result":..., "error":...}}

# ── Round-robin token rotation ──
_token_idx = 0
_token_lock = threading.Lock()
_token_exchange_lock = threading.Lock()  # Serialize AutoClaw token exchanges (avoid 630014)

# ── Request counter per account ──
_request_counts = {}  # email → int


def get_next_token(prefer_email=None):
    """Round-robin token selection across all accounts.
    Auto-refreshes expired tokens. Skips known-exhausted accounts (from cache only).

    Synergy 9 (Phase 2): when a chat fingerprint is pinned to an account,
    the caller passes it as prefer_email — that account is tried FIRST so a
    conversation keeps account affinity; if it is unusable the normal
    round-robin runs and the caller repins to whoever serves the turn.
    """
    global _token_idx
    data = load_tokens()
    if not data["accounts"]:
        return None, None

    n = len(data["accounts"])
    with _token_lock:
        idx = _token_idx % n
        _token_idx += 1

    from config import ACCESS_TOKEN_TTL, REFRESH_MARGIN

    # ── Synergy 9: pinned-account affinity (tried before round-robin) ──
    if prefer_email:
        for acc in data["accounts"]:
            if acc.get("email") != prefer_email:
                continue
            age = time.time() - acc.get("last_refreshed", 0)
            if age < ACCESS_TOKEN_TTL - REFRESH_MARGIN:
                if not _is_cached_exhausted(acc):
                    return acc["access_token"], acc
            else:
                from auth import refresh_token
                new_token = refresh_token(acc)
                if new_token and not _is_cached_exhausted(acc):
                    return new_token, acc
            break  # pinned account found but unusable → round-robin below

    # Try each account starting from idx
    for i in range(n):
        acc = data["accounts"][(idx + i) % n]
        # Check token validity
        age = time.time() - acc.get("last_refreshed", 0)
        if age < ACCESS_TOKEN_TTL - REFRESH_MARGIN:
            # Token still valid — check exhausted cache (non-blocking, cached only)
            if not _is_cached_exhausted(acc):
                return acc["access_token"], acc
            else:
                continue
        # Try refresh
        from auth import refresh_token
        new_token = refresh_token(acc)
        if new_token:
            if not _is_cached_exhausted(acc):
                return new_token, acc
            else:
                continue

    # Fallback: return first valid token even if exhausted
    for acc in data["accounts"]:
        return acc["access_token"], acc
    return None, None


# ── Exhausted accounts cache (wallet balance=0) ──
# Only uses cache — does NOT make API calls during chat requests
_exhausted_cache = {}  # email → {"balance": int, "checked": timestamp}
_EXHAUSTED_CACHE_TTL = 300  # cache valid for 5 min


def _is_cached_exhausted(acc):
    """Check if account is known to be exhausted (cache only, no API call)."""
    email = acc.get("email", "")
    now = time.time()
    cached = _exhausted_cache.get(email)
    if cached and (now - cached["checked"]) < _EXHAUSTED_CACHE_TTL:
        return cached["balance"] <= 0
    # No cache or expired — assume NOT exhausted (don't block chat)
    return False


def _refresh_exhausted_cache():
    """Background: check all accounts' wallet balances and update cache.
    Called periodically, not during chat requests."""
    data = load_tokens()
    for acc in data["accounts"]:
        email = acc.get("email", "")
        try:
            result = check_wallet(acc["access_token"])
            if result.get("code") == 0:
                balance = result["data"]["total_balance"]
                _exhausted_cache[email] = {"balance": balance, "checked": time.time()}
                if balance <= 0:
                    logger.info(f"Exhausted: {email} (0 pts)")
        except:
            pass


def _bg_wallet_checker():
    """Background thread: refresh exhausted cache every 5 minutes."""
    while True:
        try:
            _refresh_exhausted_cache()
        except Exception as e:
            logger.warning(f"Wallet checker error: {e}")
        time.sleep(_EXHAUSTED_CACHE_TTL)


def _sign_headers():
    import hashlib
    from config import APP_ID, APP_KEY, PRODUCT, VERSION, PLATFORM
    ts = str(int(time.time()))
    sign = hashlib.md5(f"{APP_ID}&{ts}&{APP_KEY}".encode()).hexdigest()
    return {
        "X-Auth-Appid": APP_ID,
        "X-Auth-TimeStamp": ts,
        "X-Auth-Sign": sign,
        "X-Product": PRODUCT,
        "X-Version": VERSION,
        "X-Tm": PLATFORM,
        "X-Trace-Id": str(uuid.uuid4()),
        "Content-Type": "application/json",
    }


# ──────────────────────────────────────────────────────────────────────────
# Synergy 2: System-Banner Injection (helper)
# Source: eequaled/GLM_proxy lib/core.js (injectSystemBanner)
# ──────────────────────────────────────────────────────────────────────────
# AutoClaw upstream requires a specific system banner as the first system
# message — without it, upstream returns HTTP 400 and traffic falls into
# the unmetered WS agent path. We prepend to an existing system message,
# or insert a new system message at index 0 if none exists. The helper is
# a pure function on a messages list (caller must hand us a copy if it
# wants the original preserved).
def _inject_system_banner(messages):
    """Inject required AutoClaw system banner before the user's first message.

    Synergy: eequaled/GLM_proxy lib/core.js (injectSystemBanner)

    Mutates `messages` in-place. If messages[0] is already a system
    message, the banner is prepended to its content (with a blank-line
    separator). Otherwise a new system message is inserted at index 0.
    Re-injection is idempotent — if the banner is already present in
    messages[0]['content'], no change is made.

    Args:
        messages: list of OpenAI message dicts. Caller should pass a
            shallow-copied list (e.g. list(original)) to avoid mutating
            the caller's request body. Message dicts that are system
            messages are replaced with new dict instances so we don't
            mutate shared references.

    Returns:
        The same `messages` list (mutated in-place) for chaining.
    """
    if not messages:
        return messages
    if messages[0].get("role") == "system":
        existing = messages[0].get("content", "")
        if isinstance(existing, str) and AUTOCLAW_SYSTEM_BANNER not in existing:
            # Replace dict (not mutate) so we don't break shared refs
            new_sys = dict(messages[0])
            new_sys["content"] = AUTOCLAW_SYSTEM_BANNER + "\n\n" + existing
            messages[0] = new_sys
        return messages
    # No system message at index 0 — insert one
    messages.insert(0, {"role": "system", "content": AUTOCLAW_SYSTEM_BANNER})
    return messages


# ──────────────────────────────────────────────────────────────────────────
# Synergy 5: In-chat !router Command (helper)
# Source: eroslifestyle/ai-router-switch src/router_commands.py
# ──────────────────────────────────────────────────────────────────────────
# Switching backends normally requires changing env vars and restarting
# the proxy. The !router command lets the operator do common operations
# (status, reset, refresh-all, help) in-band via the chat-completions
# endpoint — the proxy intercepts the command and returns a synthetic
# OpenAI-shaped response without ever forwarding upstream.
def _check_router_command(messages):
    """Check if the last user message is a !router command.

    Synergy: eroslifestyle/ai-router-switch src/router_commands.py

    Returns a synthetic OpenAI-shaped response and never forwards
    upstream. Supported sub-commands:
      !router status       — show current model + account count
      !router reset        — reset token rotation index to 0
      !router refresh-all  — trigger async token refresh
      !router help         — show this help text

    Args:
        messages: list of OpenAI message dicts from the request body.

    Returns:
        Tuple (synthetic_response_dict_or_None, is_command_bool). When
        is_command is True, synthetic_response_dict is an OpenAI-shaped
        dict ready to be returned by jsonify(). When False, the proxy
        should proceed with normal chat completion handling.
    """
    if not messages:
        return None, False
    last_msg = messages[-1]
    if last_msg.get("role") != "user":
        return None, False
    content = last_msg.get("content", "")
    if isinstance(content, list):
        # Multimodal content (vision requests) — concat text parts
        content = " ".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not content.startswith("!router"):
        return None, False

    parts = content.split()
    if len(parts) < 2:
        return {"choices": [{"message": {"role": "assistant", "content":
            "!router commands:\n"
            "  !router status - Show current model + account\n"
            "  !router reset - Reset token rotation index\n"
            "  !router refresh-all - Force refresh all tokens\n"
            "  !router help - Show this help"}}], "usage": {}}, True

    cmd = parts[1].lower()
    if cmd == "status":
        data = load_tokens()
        n_accounts = len(data.get("accounts", []))
        return {"choices": [{"message": {"role": "assistant", "content":
            f"Router Status:\n"
            f"  Active accounts: {n_accounts}\n"
            f"  Default model: {DEFAULT_MODEL}\n"
            f"  Token cache TTL: 5s\n"
            f"  Encryption: {'enabled' if os.environ.get('AUTOCLAW_TOKEN_KEY') else 'disabled'}"}}],
            "usage": {}}, True
    elif cmd == "reset":
        global _token_idx
        _token_idx = 0
        return {"choices": [{"message": {"role": "assistant",
            "content": "Token rotation index reset to 0."}}], "usage": {}}, True
    elif cmd == "refresh-all":
        # Trigger async refresh (non-blocking) so the chat client gets an
        # immediate response — operator can poll /api/refresh-progress.
        threading.Thread(target=refresh_all, daemon=True).start()
        # Synergy 3: clear the permanent-failure cache so the proxy
        # immediately re-attempts the upstream after refresh completes,
        # rather than waiting 60s for TTL.
        clear_permanent_failures()
        return {"choices": [{"message": {"role": "assistant",
            "content": "Token refresh triggered. "
                       "Check /api/refresh-progress for status."}}],
            "usage": {}}, True
    elif cmd == "help":
        return {"choices": [{"message": {"role": "assistant", "content":
            "!router commands:\n"
            "  !router status\n"
            "  !router reset\n"
            "  !router refresh-all\n"
            "  !router help"}}], "usage": {}}, True
    return None, False


@app.route("/v1/chat/completions", methods=["POST"])
def chat_completions():
    """OpenAI-compatible chat completions proxy."""
    # API key auth (Audit Fix: proxy was unauthenticated)
    if PROXY_API_KEY:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {PROXY_API_KEY}":
            return jsonify({"error": {"message": "Invalid API key", "type": "auth_error"}}), 401

    body = request.get_json(force=True)
    if not body:
        return jsonify({"error": {"message": "Invalid JSON body"}}), 400

    # ── Synergy 5: In-chat !router Command (early intercept) ──
    # Source: eroslifestyle/ai-router-switch src/router_commands.py
    # Intercept !router commands BEFORE model validation — they never
    # forward upstream and never consume a token.
    router_resp, is_router_cmd = _check_router_command(body.get("messages", []))
    if is_router_cmd:
        logger.info("!router command intercepted (not forwarded upstream)")
        return jsonify(router_resp)

    # Model mapping with strict validation (Audit Fix: silent fallback to expensive model)
    client_model = body.get("model", DEFAULT_MODEL)
    upstream_model = MODEL_MAP.get(client_model)
    if upstream_model is None:
        if STRICT_MODEL_VALIDATION:
            return jsonify({"error": {
                "message": f"Unknown model '{client_model}'. Available: {list(MODEL_MAP.keys())}",
                "type": "invalid_request_error",
            }}), 400
        else:
            upstream_model = DEFAULT_MODEL

    # ── Synergy 1: Output-Cap Clamping ──
    # Source: eequaled/GLM_proxy lib/core.js (clampMaxOutput)
    # Prevent silent DeepSeek substitution when requesting >131072 output
    # tokens — AutoClaw cloud silently switches to DeepSeek-V4-Pro (7x
    # cost). Only clamp if the client explicitly set max_tokens; we never
    # inflate (only ever reduce to the cap).
    if "max_tokens" in body and body["max_tokens"] is not None:
        original_max = body["max_tokens"]
        body["max_tokens"] = clamp_max_output(client_model, original_max)
        if body["max_tokens"] < original_max:
            logger.info(
                f"Output-cap clamp: {client_model} requested={original_max} "
                f"-> clamped={body['max_tokens']} (cap={OUTPUT_CAPS.get(upstream_model, OUTPUT_CAPS['default'])})"
            )

    # ── Synergy 9 (Phase 2): per-chat fingerprint + loop_breaker ──
    # Source: eroslifestyle/ai-router-switch. The fingerprint (SHA-256 of
    # the first user message, or the X-AutoClaw-Chat-Id header) is the unit
    # of isolation; loop_breaker trips HTTP 400 when the SAME turn is
    # re-emitted ≥N times at ≥80% context fill (runaway-retry guard).
    fp = chat_fingerprint.compute_fingerprint(
        body.get("messages"), request.headers.get("X-AutoClaw-Chat-Id"))
    if fp:
        verdict = loop_breaker.check(fp, body.get("messages"), client_model)
        if verdict:
            logger.warning(
                f"loop_breaker trip: fp={fp[:12]}... reemits={verdict['reemits']} "
                f"fill={verdict['ratio']:.0%} est={verdict['estimated_tokens']}"
                f"/{verdict['context_window']} — HTTP 400 forced restart"
            )
            return jsonify({"error": {
                "message": (
                    "Conversation loop detected: this turn was re-emitted "
                    f"{verdict['reemits']} times at {verdict['ratio']:.0%} of "
                    "the context window. Start a fresh conversation instead "
                    "of retrying — further retries will keep failing."),
                "type": "loop_breaker_triggered",
                "code": 400,
                "details": verdict,
            }}), 400

    # Force stream=True for upstream (DeepSeek models 500 on non-stream)
    upstream_body = dict(body)
    upstream_body["stream"] = True
    upstream_body["model"] = "x"  # ignored by upstream, but fill it

    # ── Synergy 2: System-Banner Injection ──
    # Source: eequaled/GLM_proxy lib/core.js (injectSystemBanner)
    # AutoClaw upstream returns HTTP 400 without this banner; with it,
    # traffic stays on the metered chat-completions path (reliable).
    # Copy the messages list so we don't mutate the original request body
    # (callers may inspect body['messages'] after the response).
    if isinstance(upstream_body.get("messages"), list):
        upstream_body["messages"] = list(upstream_body["messages"])
        _inject_system_banner(upstream_body["messages"])

    # ── Synergy 8 (Phase 2): DSML tool-calling shim (request side) ──
    # Source: tt-52101/chat-z-ai-proxy-web2api-free. When the client sends
    # OpenAI `tools`, describe them in a DSML protocol block appended after
    # the AutoClaw banner; the response side parses <dsml:tool_call> blocks
    # back into real tool_calls. tool_choice="none" disables the shim.
    tools = body.get("tools")
    dsml_active = bool(
        dsml_shim.enabled()
        and isinstance(tools, list) and tools
        and body.get("tool_choice") != "none"
    )
    if dsml_active and isinstance(upstream_body.get("messages"), list):
        dsml_shim.inject_tool_protocol(
            upstream_body["messages"], tools, body.get("tool_choice"))
        logger.info(
            f"DSML shim active: {len(tools)} tool(s), "
            f"tool_choice={body.get('tool_choice', 'auto')}")

    # Get token (round-robin, with Synergy 9 pinned-account affinity)
    pinned_email = chat_fingerprint.pinned_email(fp) if fp else None
    if pinned_email:
        access_token, acc = get_next_token(prefer_email=pinned_email)
    else:
        access_token, acc = get_next_token()
    if not access_token:
        return jsonify({
            "error": {
                "message": "No valid tokens. Add accounts first via /login or manually edit tokens.json",
                "type": "auth_error",
            }
        }), 401

    # ── Synergy 9: pin-on-first-use / repin-on-drift ──
    if fp and acc:
        served_email = acc.get("email", "unknown")
        chat_fingerprint.bind(fp, served_email)
        if pinned_email and served_email != pinned_email:
            logger.info(
                f"Chat fingerprint repinned: fp={fp[:12]}... "
                f"{pinned_email} → {served_email} (pinned account unusable)")

    # ── Synergy 3: Permanent-Failure Negative Cache ──
    # Source: eequaled/GLM_proxy lib/core.js (createPermanentFailureCache)
    # Avoid replaying doomed 30s+ cloud sequences when (model, account) is
    # known to be permanently failed (quota exhausted, banned, etc.).
    used_email = acc.get("email", "unknown")
    if is_permanent_failure_cached(upstream_model, used_email):
        logger.warning(
            f"Permanent-failure cache hit: model={upstream_model} "
            f"account={used_email} — returning 429 without forwarding upstream"
        )
        return jsonify({
            "error": {
                "message": "Account temporarily unavailable (cached permanent "
                           "failure — wait 60s or run !router refresh-all).",
                "type": "upstream_error",
                "code": 429,
            }
        }), 429

    # Increment request counter for this account
    _request_counts[used_email] = _request_counts.get(used_email, 0) + 1

    headers = _sign_headers()
    # Token already has "Bearer " prefix — don't double it
    raw_token = access_token.replace("Bearer ", "")
    headers["X-Authorization"] = f"Bearer {raw_token}"
    headers["X-Request-Id"] = str(uuid.uuid4())
    headers["X-Request-Model"] = upstream_model

    client_wants_stream = body.get("stream", False)

    try:
        # ── Synergy 6: OWL proxy-first streaming (owl-agent v5.3) ──
        # Race HEDGE_FANOUT free proxies for connection establishment;
        # first to deliver headers carries the SSE stream. Direct fallback
        # fires when OWL is disabled, unavailable, or the race is lost.
        upstream_resp = None
        via = "direct"
        if owl_bridge.owl_enabled():
            try:
                upstream_resp = owl_bridge.owl_stream_request(
                    "POST", CHAT_COMPLETIONS,
                    headers=headers, json_body=upstream_body, timeout=600,
                )
                via = f"owl-proxy/{owl_bridge.owl_backend()}"
                logger.info(f"Chat upstream via {via}")
            except owl_bridge.OwlUnavailable as e:
                logger.warning(f"OWL unavailable → direct fallback: {e}")
                upstream_resp = None
        g._upstream_via = via
        if upstream_resp is None:
            # Always request stream from upstream (direct path)
            upstream_resp = req_lib.post(
                CHAT_COMPLETIONS,
                json=upstream_body,
                headers=headers,
                stream=True,
                timeout=600,
                verify=False,
            )

        if upstream_resp.status_code != 200:
            if isinstance(upstream_resp, owl_bridge.OwlStreamResponse):
                raw_error = upstream_resp.read_error(1000)
            else:
                raw_error = upstream_resp.text[:1000]
            # ── Synergy 4: Chinese → English Error Translation ──
            # Source: eequaled/GLM_proxy lib/core.js (translateError)
            # AutoClaw upstream returns Chinese error phrases (积分不足,
            # 账号封禁, etc.). Translate them to stable English so
            # international clients can match/switch on them.
            translated = translate_error(raw_error)
            if translated != raw_error:
                logger.info(f"Translated upstream error: '{raw_error[:120]}' -> '{translated[:120]}'")
            # ── Synergy 3: Mark permanent failures ──
            # Classify by status code and translated text so the next
            # request returns 429 immediately (within 60s TTL) without
            # replaying the doomed cloud sequence.
            failure_class = None
            lower_translated = translated.lower()
            if upstream_resp.status_code in (401, 403) or "auth" in lower_translated:
                failure_class = "auth_failed"
            elif upstream_resp.status_code == 404 or "model not found" in lower_translated:
                failure_class = "model_not_found"
            elif upstream_resp.status_code == 402 or "insufficient credits" in lower_translated or "quota exhausted" in lower_translated:
                failure_class = "quota_exhausted"
            elif "banned" in lower_translated:
                failure_class = "account_banned"
            if failure_class:
                mark_permanent_failure(upstream_model, failure_class, used_email)
                logger.warning(
                    f"Marked permanent failure: model={upstream_model} "
                    f"account={used_email} class={failure_class}"
                )
            return jsonify({
                "error": {
                    "message": f"Upstream error {upstream_resp.status_code}: {translated}",
                    "type": "upstream_error",
                    "code": upstream_resp.status_code,
                    "failure_class": failure_class,  # None for transient errors
                }
            }), upstream_resp.status_code

        if client_wants_stream:
            # Pass through SSE stream — filter reasoning, only forward content chunks
            # Synergy 8 (Phase 2): when the DSML shim is active, content deltas
            # flow through a StreamSieve that forwards prose immediately, holds
            # back possible <dsml:tool_call> starts, and converts completed
            # blocks into OpenAI streaming tool_calls deltas.
            sieve = dsml_shim.DSMLStreamSieve() if dsml_active else None

            def _sse(piece):
                return b"data: " + json.dumps(
                    {"choices": [{"index": 0, "delta": piece,
                                  "finish_reason": None}]}).encode() + b"\n\n"

            def generate():
                for line in upstream_resp.iter_lines():
                    if line:
                        if line.startswith(b"data:"):
                            raw = line[5:].strip()
                            if raw == b"[DONE]":
                                # v2.1.0 fix: upstream [DONE] previously fell
                                # through and the generator appended a SECOND
                                # [DONE] — duplicate terminator confused some
                                # strict OpenAI clients.
                                # v2.2.0: drain any held-back DSML tail first.
                                if sieve is not None:
                                    for piece in sieve.flush():
                                        yield _sse(piece)
                                yield line + b"\n\n"
                                return
                            try:
                                chunk = json.loads(raw)
                                choices = chunk.get("choices", [])
                                pieces = []
                                carry = False
                                if choices:
                                    delta = choices[0].get("delta", {})
                                    # Strip reasoning fields — Cline/OpenAI clients don't understand them
                                    if "reasoning" in delta:
                                        del delta["reasoning"]
                                    if "reasoning_details" in delta:
                                        del delta["reasoning_details"]
                                    if "reasoning_content" in delta:
                                        del delta["reasoning_content"]
                                    # ── Synergy 8: DSML sieve (content only) ──
                                    # Native tool_calls deltas pass through
                                    # untouched; only textual content is sieved.
                                    if sieve is not None:
                                        if delta.get("content"):
                                            pieces = sieve.feed(
                                                delta.pop("content"))
                                        if choices[0].get("finish_reason"):
                                            pieces += sieve.flush()
                                            if sieve.saw_tool_calls:
                                                # synthesised calls arrived —
                                                # fix the terminal reason
                                                choices[0]["finish_reason"] = "tool_calls"
                                    # Skip chunks with empty content and no tool_calls (pure thinking)
                                    has_content = bool(delta.get("content"))
                                    has_tool_calls = bool(delta.get("tool_calls"))
                                    has_finish = bool(choices[0].get("finish_reason"))
                                    # Only forward if there's actual content/tool_calls/finish
                                    carry = has_content or has_tool_calls or has_finish
                                    if carry or has_finish:
                                        # Clean native_finish_reason
                                        if "native_finish_reason" in choices[0]:
                                            del choices[0]["native_finish_reason"]
                                # Emit sieve pieces first — they carry text that
                                # streamed in BEFORE this chunk arrived.
                                for piece in pieces:
                                    yield _sse(piece)
                                if carry:
                                    yield b"data: " + json.dumps(chunk).encode() + b"\n\n"
                            except (json.JSONDecodeError, KeyError):
                                yield line + b"\n\n"
                yield b"data: [DONE]\n\n"
            return Response(
                generate(),
                mimetype="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "X-Accel-Buffering": "no",
                },
            )
        else:
            # Aggregate stream → single JSON response (OpenAI non-stream format)
            full_content = ""
            full_tool_calls = []
            finish_reason = None
            model_name = upstream_model
            usage = None

            for line in upstream_resp.iter_lines():
                if not line:
                    continue
                line_str = line.decode("utf-8", errors="replace")
                if line_str.startswith("data: "):
                    data_str = line_str[6:]
                    if data_str.strip() == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data_str)
                        choices = chunk.get("choices", [])
                        if choices:
                            delta = choices[0].get("delta", {})
                            if "content" in delta and delta["content"]:
                                full_content += delta["content"]
                            if "tool_calls" in delta and delta["tool_calls"]:
                                for tc in delta["tool_calls"]:
                                    idx = tc.get("index", 0)
                                    while len(full_tool_calls) <= idx:
                                        full_tool_calls.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                                    if "id" in tc and tc["id"]:
                                        full_tool_calls[idx]["id"] = tc["id"]
                                    if "type" in tc and tc["type"]:
                                        full_tool_calls[idx]["type"] = tc["type"]
                                    if "function" in tc:
                                        fn = tc["function"]
                                        if "name" in fn and fn["name"]:
                                            full_tool_calls[idx]["function"]["name"] += fn["name"]
                                        if "arguments" in fn and fn["arguments"]:
                                            full_tool_calls[idx]["function"]["arguments"] += fn["arguments"]
                            if choices[0].get("finish_reason"):
                                finish_reason = choices[0]["finish_reason"]
                        if chunk.get("model"):
                            model_name = chunk["model"]
                        if chunk.get("usage"):
                            usage = chunk["usage"]
                    except json.JSONDecodeError:
                        pass

            # ── Synergy 8 (Phase 2): DSML buffered parse ──
            # When the client sent tools and the upstream produced no native
            # tool_calls, convert any <dsml:tool_call> blocks in the reply
            # into a real OpenAI tool_calls message.
            dsml_result = None
            if dsml_active and not full_tool_calls:
                dsml_result = dsml_shim.parse_dsml(full_content)

            message = {"role": "assistant", "content": None}
            if dsml_result:
                message["content"] = dsml_result["content"] or None
                message["tool_calls"] = dsml_result["tool_calls"]
                # upstream said "stop" because its TEXT finished; the client
                # must see "tool_calls" to dispatch the synthesised calls
                finish_reason = "tool_calls"
            else:
                message["content"] = full_content if full_content else None
                if full_tool_calls:
                    message["tool_calls"] = full_tool_calls
                    if not finish_reason:
                        finish_reason = "tool_calls"

            response = {
                "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": client_model,
                "choices": [{
                    "index": 0,
                    "message": message,
                    "finish_reason": finish_reason or "stop",
                }],
                "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
            resp = jsonify(response)
            if dsml_result:
                resp.headers["X-DSML-Shim"] = "1"
            return resp

    except req_lib.exceptions.Timeout:
        return jsonify({"error": {"message": "Upstream timeout"}}), 504
    except Exception as e:
        return jsonify({"error": {"message": f"Proxy error: {str(e)}"}}), 500


@app.route("/v1/models", methods=["GET"])
def list_models():
    """List available models (OpenAI-compatible)."""
    models = []
    for alias, upstream in MODEL_MAP.items():
        models.append({
            "id": alias,
            "object": "model",
            "created": 1700000000,
            "owned_by": "autoclaw",
            "upstream": upstream,
        })
    return jsonify({"object": "list", "data": models})


@app.route("/health", methods=["GET"])
def health():
    data = load_tokens()
    return jsonify({
        "status": "ok",
        "accounts": len(data["accounts"]),
        "port": PROXY_PORT,
        "version": VERSION,
        # Synergy 6: OWL-AGENT proxy defense layer stats (never raises)
        "owl": owl_bridge.owl_stats(),
        # Synergy 9 (Phase 2): per-chat fingerprint + loop_breaker (ai-router-switch)
        "fingerprint": chat_fingerprint.stats(),
        "loop_breaker": loop_breaker.stats(),
        # Synergy 8 (Phase 2): DSML tool-calling shim (chat-z-ai-proxy)
        "dsml": dsml_shim.stats(),
    })


@app.route("/accounts", methods=["GET"])
def accounts():
    """List all stored accounts."""
    list_accounts()
    data = load_tokens()
    safe = []
    for acc in data["accounts"]:
        safe.append({
            "email": acc["email"],
            "user_id": acc.get("user_id"),
            "added_at": acc.get("added_at"),
            "last_refreshed": acc.get("last_refreshed"),
            "token_preview": acc["access_token"][:30] + "..." if acc.get("access_token") else None,
        })
    return jsonify({"accounts": safe})


@app.route("/api/accounts-detail", methods=["GET"])
def accounts_detail():
    """List all accounts with wallet balance + token expiry (bulk)."""
    import base64 as _b64
    data = load_tokens()
    result = []
    for acc in data["accounts"]:
        # Decode JWT for expiry
        exp = 0
        iat = 0
        try:
            token = acc["access_token"].replace("Bearer ", "")
            parts = token.split(".")
            payload = parts[1] + "=" * (4 - len(parts[1]) % 4)
            decoded = json.loads(_b64.urlsafe_b64decode(payload))
            exp = decoded.get("exp", 0)
            iat = decoded.get("iat", 0)
        except:
            pass

        now = time.time()
        remaining_h = max(0, (exp - now) / 3600) if exp else 0
        expired = remaining_h <= 0

        result.append({
            "email": acc["email"],
            "user_id": acc.get("user_id", ""),
            "device_id": acc.get("device_id", ""),
            "added_at": acc.get("added_at", 0),
            "last_refreshed": acc.get("last_refreshed", 0),
            "expires_at": exp,
            "remaining_hours": round(remaining_h, 1),
            "expired": expired,
            "has_refresh_token": bool(acc.get("refresh_token")),
            "request_count": _request_counts.get(acc.get("email", ""), 0),
        })
    return jsonify({"accounts": result, "total": len(result)})


@app.route("/api/refresh/<path:email>", methods=["POST"])
def refresh_single(email):
    """Refresh access token for a single account."""
    import traceback
    from auth import refresh_token as do_refresh
    try:
        data = load_tokens()
        acc = None
        for a in data["accounts"]:
            if a.get("email") == email:
                acc = a
                break
        if not acc:
            return jsonify({"error": "Account not found"}), 404

        new_token = do_refresh(acc)
        if new_token:
            return jsonify({"success": True, "email": email, "message": "Token refreshed"})
        return jsonify({"success": False, "email": email, "error": "Refresh failed"}), 500
    except Exception as e:
        tb = traceback.format_exc()
        logger.error(f"refresh_single error: {tb}")
        return jsonify({"success": False, "email": email, "error": str(e), "traceback": tb}), 500


@app.route("/api/wallet-bulk", methods=["GET"])
def wallet_bulk():
    """Get wallet balance for all accounts (bulk)."""
    data = load_tokens()
    result = []
    for acc in data["accounts"]:
        balance = None
        status = "active"
        try:
            wallet_data = check_wallet(acc["access_token"])
            if wallet_data.get("code") == 0:
                balance = wallet_data["data"]["total_balance"]
                if balance <= 0:
                    status = "exhausted"
        except:
            status = "error"
        result.append({
            "email": acc["email"],
            "balance": balance,
            "status": status,
        })
    return jsonify({"accounts": result})


@app.route("/api/delete/<path:email>", methods=["DELETE"])
def delete_account(email):
    """Remove an account from tokens.json."""
    data = load_tokens()
    before = len(data["accounts"])
    data["accounts"] = [a for a in data["accounts"] if a.get("email") != email]
    after = len(data["accounts"])
    if after < before:
        save_tokens(data)
        return jsonify({"success": True, "email": email})
    return jsonify({"error": "Account not found"}), 404


# ── Refresh-all progress tracking (for UI counter) ──
_refresh_progress = {"total": 0, "done": 0, "success": 0, "fail": 0, "running": False}


@app.route("/refresh-all", methods=["POST"])
def do_refresh_all():
    # If already running, return current progress
    if _refresh_progress["running"]:
        return jsonify({"status": "running", **_refresh_progress})

    # Backup tokens.json before refresh (preventive)
    import shutil
    try:
        shutil.copy2(TOKENS_FILE_FULL, TOKENS_FILE_FULL + ".bak")
    except Exception as e:
        logger.warning(f"Backup before refresh-all failed: {e}")

    # Start refresh in background thread
    data = load_tokens()
    _refresh_progress["total"] = len(data["accounts"])
    _refresh_progress["done"] = 0
    _refresh_progress["success"] = 0
    _refresh_progress["fail"] = 0
    _refresh_progress["running"] = True

    def _bg_refresh():
        import auth as _auth
        from config import REFRESH_URL
        import requests as _req
        d = _auth.load_tokens()
        now = int(time.time())
        for acc in d["accounts"]:
            try:
                headers = _auth._sign_headers()
                body = {
                    "source_id": acc.get("source_id", "autoclaw"),
                    "device_id": acc["device_id"],
                    "refresh_token": acc["refresh_token"],
                }
                resp = _req.post(REFRESH_URL, json=body, headers=headers, timeout=15, verify=False)
                resp_data = resp.json()
                if resp_data.get("code") == 0 and "data" in resp_data:
                    new_access = resp_data["data"].get("access_token")
                    new_refresh = resp_data["data"].get("refresh_token", acc["refresh_token"])
                    if new_access:
                        acc["access_token"] = new_access
                        if new_refresh:
                            acc["refresh_token"] = new_refresh
                        acc["last_refreshed"] = now
                        _refresh_progress["success"] += 1
                    else:
                        _refresh_progress["fail"] += 1
                else:
                    _refresh_progress["fail"] += 1
            except Exception:
                _refresh_progress["fail"] += 1
            _refresh_progress["done"] += 1
        # Save once at end
        _auth.save_tokens(d)
        _refresh_progress["running"] = False
        logger.info(f"Refresh all done: {_refresh_progress['success']} ok, {_refresh_progress['fail']} fail")

    t = threading.Thread(target=_bg_refresh, daemon=True)
    t.start()

    return jsonify({"status": "started", **_refresh_progress})


@app.route("/api/refresh-progress", methods=["GET"])
def refresh_progress():
    return jsonify(_refresh_progress)


@app.route("/wallet", methods=["GET"])
def wallet():
    """Check wallet balance for first account."""
    data = load_tokens()
    if not data["accounts"]:
        return jsonify({"error": "No accounts"}), 404
    acc = data["accounts"][0]
    result = check_wallet(acc["access_token"])
    return jsonify(result)


@app.route("/ledger", methods=["GET"])
def ledger():
    """Check billing ledger for first account."""
    data = load_tokens()
    if not data["accounts"]:
        return jsonify({"error": "No accounts"}), 404
    acc = data["accounts"][0]
    result = check_ledger(acc["access_token"])
    return jsonify(result)


# ── Web UI ──

# `os` already imported at top of file (Synergy 5: _check_router_command
# uses os.environ.get for AUTOCLAW_TOKEN_KEY).

from flask import send_from_directory

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")


@app.route("/")
def ui_dashboard():
    return send_from_directory(UI_DIR, "index.html")


@app.route("/ui/<path:path>")
def ui_static(path):
    return send_from_directory(UI_DIR, path)


@app.route("/api/login-url", methods=["POST"])
def api_login_url():
    """Generate Google OAuth URL for browser-based login.
    Stores pending state → auto-captured when /auth/callback-google is hit.
    """
    from auth import google_oauth_url, _next_proxy
    # Get proxy BEFORE calling google_oauth_url so we can reuse it for token exchange
    proxy_used = _next_proxy()
    oauth_url, state, device_id, err_info = google_oauth_url(proxy=proxy_used)
    if oauth_url:
        _pending_logins[state] = {"device_id": device_id, "result": None, "error": None, "proxy": proxy_used}
        return jsonify({"oauth_url": oauth_url, "state": state, "device_id": device_id})
    # err_info = {"code": 400005, "msg": "Limit error", "retried": 5}
    return jsonify({
        "error": "Failed to get OAuth URL",
        "detail": err_info,
    }), 500


@app.route("/auth/callback-google")
def auth_callback_google():
    """Google OAuth redirect target — auto-captures code+state, exchanges tokens."""
    from auth import google_oauth_login, add_token

    code = request.args.get("code")
    state = request.args.get("state")
    error = request.args.get("error")

    # Find pending login by state — MUST match, no fallback in concurrent mode
    pending = _pending_logins.get(state)
    if not pending:
        # No state match — do NOT fallback to "first pending" (causes cross-account
        # contamination in concurrent mode). Just fail this callback.
        logger.warning(f"Unknown state={state[:20]}... (not in {len(_pending_logins)} pending)")
        return "<html><body><h1>Login Failed</h1><p>Unknown state</p></body></html>", 400

    if error:
        pending["error"] = error
        return f"<html><body><h1>Login Failed</h1><p>{error}</p></body></html>", 400

    if not code:
        pending["error"] = "No code in callback"
        return "<html><body><h1>Login Failed</h1><p>No code</p></body></html>", 400

    device_id = pending.get("device_id")
    proxy_used = pending.get("proxy")  # Reuse same proxy from URL generation

    # Serialize token exchanges — AutoClaw API rate-limits per IP (630014).
    # Without this, 3 concurrent callbacks all hit the API simultaneously → all fail.
    # Lock ensures 1 exchange at a time, with small gap between.
    with _token_exchange_lock:
        logger.info(f"Exchanging token for state={state[:12]}... device_id={device_id[:8]}... proxy={'yes' if proxy_used else 'none'}")
        result = google_oauth_login(code, state, device_id, proxy=proxy_used)
        time.sleep(2)  # Gap so next concurrent callback doesn't hit 630014
    if not result:
        logger.error(f"Token exchange FAILED for state={state[:12]}... (google_oauth_login returned None)")
        pending["error"] = "Token exchange failed"
        return "<html><body><h1>Login Failed</h1><p>Token exchange failed</p></body></html>", 500
    logger.info(f"Token exchange OK for state={state[:12]}... user_id={result.get('user_id')}")

    # Extract email from JWT jti field
    import base64 as _b64
    email = result.get("user_name") or f"user_{result['user_id']}"
    try:
        token_part = result["access_token"].replace("Bearer ", "").split(".")[1]
        token_part += "=" * (4 - len(token_part) % 4)
        payload = json.loads(_b64.urlsafe_b64decode(token_part))
        if payload.get("jti"):
            email = payload["jti"]
    except:
        pass

    add_token(
        email=email,
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user_id=result["user_id"],
        device_id=device_id,
    )
    pending["result"] = {
        "email": email,
        "user_id": result.get("user_id"),
        "first_login": result.get("first_login"),
    }
    return f"""<html><body>
<h1>Login Success!</h1>
<p>Email: {email}</p>
<p>User ID: {result.get('user_id')}</p>
<p>You can close this tab.</p>
</body></html>"""


@app.route("/api/login-status", methods=["GET"])
def api_login_status():
    """Check if pending OAuth login completed.
    Query param: state=<state> for concurrent login tracking.
    """
    state = request.args.get("state")

    if state:
        # Specific state lookup (concurrent mode)
        pending = _pending_logins.get(state)
        if not pending:
            return jsonify({"status": "pending"})
        if pending.get("result"):
            return jsonify({"status": "ok", "account": pending["result"]})
        if pending.get("error"):
            return jsonify({"status": "error", "error": pending["error"]})
        return jsonify({"status": "pending"})
    else:
        # Legacy: return first available result
        for s, p in _pending_logins.items():
            if p.get("result"):
                return jsonify({"status": "ok", "account": p["result"]})
            if p.get("error"):
                return jsonify({"status": "error", "error": p["error"]})
        return jsonify({"status": "pending"})


@app.route("/api/login-callback", methods=["POST"])
def api_login_callback():
    """Exchange OAuth code for tokens (manual paste mode)."""
    from auth import google_oauth_login, add_token
    body = request.get_json(force=True)
    code = body.get("code")
    state = body.get("state")
    device_id = body.get("device_id")

    if not code or not state or not device_id:
        return jsonify({"error": "Missing code, state, or device_id"}), 400

    result = google_oauth_login(code, state, device_id)
    if not result:
        return jsonify({"error": "Token exchange failed"}), 500

    email = result.get("user_name") or f"user_{result['user_id']}"
    add_token(
        email=email,
        access_token=result["access_token"],
        refresh_token=result["refresh_token"],
        user_id=result["user_id"],
        device_id=device_id,
    )
    return jsonify({
        "success": True,
        "email": email,
        "user_id": result.get("user_id"),
        "user_name": result.get("user_name"),
        "first_login": result.get("first_login"),
    })


@app.route("/api/test-chat", methods=["POST"])
def api_test_chat():
    """Test chat from UI — returns response text."""
    body = request.get_json(force=True)
    model = body.get("model", "glm-5.2")
    message = body.get("message", "Hello!")
    stream = body.get("stream", False)

    # Forward to our own /v1/chat/completions
    import requests as r
    try:
        resp = r.post(
            f"http://127.0.0.1:{PROXY_PORT}/v1/chat/completions",
            json={
                "model": model,
                "messages": [{"role": "user", "content": message}],
                "stream": False,
            },
            timeout=600,
        )
        if resp.status_code == 200:
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            usage = data.get("usage", {})
            return jsonify({
                "success": True,
                "content": content,
                "model": data.get("model"),
                "usage": usage,
            })
        else:
            return jsonify({"error": resp.json()}), resp.status_code
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/wallet/<email>", methods=["GET"])
def api_wallet_email(email=None):
    """Check wallet for specific account by email."""
    from auth import get_valid_token_for_email
    token, acc = get_valid_token_for_email(email)
    if not token:
        return jsonify({"error": "No valid token for " + email}), 404
    result = check_wallet(token)
    return jsonify(result)


if __name__ == "__main__":
    logger.info(f"AutoClaw Proxy starting on {PROXY_HOST}:{PROXY_PORT}")
    logger.info(f"Dashboard: http://localhost:{PROXY_PORT}")
    logger.info(f"API: http://localhost:{PROXY_PORT}/v1/chat/completions")
    logger.info(f"Models: {', '.join(MODEL_MAP.keys())}")
    list_accounts()

    # Start background wallet checker (every 5 min, non-blocking)
    wallet_thread = threading.Thread(target=_bg_wallet_checker, daemon=True)
    wallet_thread.start()
    logger.info(f"Background wallet checker started (interval={_EXHAUSTED_CACHE_TTL}s)")

    # Start OAuth callback server on port 18432 in background thread
    # (Google registered redirect_uri = localhost:18432)
    from werkzeug.serving import make_server
    callback_server = make_server(PROXY_HOST, 18432, app, threaded=True)
    callback_thread = threading.Thread(target=callback_server.serve_forever, daemon=True)
    callback_thread.start()
    logger.info(f"OAuth callback server on port 18432")

    # Main proxy server (blocking)
    app.run(host=PROXY_HOST, port=PROXY_PORT, threaded=True)
