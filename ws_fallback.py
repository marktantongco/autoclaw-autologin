"""AutoClaw WS Fallback — cloud-to-local WebSocket transport (Synergy #13,
source: eequaled/GLM_proxy "Cloud-to-Local WebSocket Fallback").

When the upstream cloud API is UNREACHABLE at the network level (connection
refused / DNS failure / connect timeout — never HTTP 4xx/5xx), the chat route
transparently re-routes the request through a locally running AutoClaw
desktop agent over WebSocket. This gives best-in-class resilience for
operators who run a desktop agent alongside the proxy.

Wire contract — "AutoClaw local-agent WS protocol v1" (text frames, JSON):
  → {"type": "chat.request", "id": "<uuid>", "payload": <OpenAI body>}
  ← {"type": "chat.headers", "id": ..., "status": 200}          (once)
  ← {"type": "chat.delta",   "id": ..., "sse": "<raw SSE bytes/str>"}  (0..n)
  ← {"type": "chat.end",     "id": ...}                          (once, last)
  ← {"type": "chat.error",   "id": ..., "status": 503, "message": "..."}
  → {"type": "ping"}  /  ← {"type": "pong"}                     (discovery)

The "sse" field carries raw upstream-shaped SSE bytes ("data: {...}\n\n"
lines), so the response shim exposes requests-style iter_lines() and the
existing DSML sieve / banner path works unchanged over this transport.

Failure-safety contract (mirrors owl_bridge):
  - only network-level faults trip the breaker; chat.error / HTTP error
    statuses are upstream decisions and are passed through verbatim
  - every helper is fail-safe: callers catch WsUnavailable and fall back to
    returning the original network error to the client.

Configuration (all env):
  ACLAW_WS_FALLBACK          1|0  master switch (default OFF — most
                                  operators have no desktop agent)
  ACLAW_WS_AGENT_URL         ws://host:port/path  exact agent endpoint
                             (skips discovery when set)
  ACLAW_WS_DISCOVERY_PORTS   comma list, default "18789,18790,18791"
                             (probed on 127.0.0.1 when no URL configured)
  ACLAW_WS_TIMEOUT           float, per-request stream cap s (default 600)
  ACLAW_WS_BREAKER_THRESHOLD int, consecutive network failures before the
                             breaker opens (default 3)
  ACLAW_WS_BREAKER_COOLDOWN  float, seconds the breaker stays open
                             (default 60)
"""

import os
import json
import time
import uuid
import socket
import logging
import threading

logger = logging.getLogger("autoclaw.ws_fallback")

# ── Configuration ────────────────────────────────────────────────────────
ENV_ENABLED = os.environ.get("ACLAW_WS_FALLBACK", "0").strip().lower() not in (
    "0", "false", "no", "off")
AGENT_URL = os.environ.get("ACLAW_WS_AGENT_URL", "").strip()
DISCOVERY_PORTS = [p.strip() for p in os.environ.get(
    "ACLAW_WS_DISCOVERY_PORTS", "18789,18790,18791").split(",") if p.strip()]
DISCOVERY_HOST = os.environ.get("ACLAW_WS_DISCOVERY_HOST", "127.0.0.1")
STREAM_TIMEOUT = float(os.environ.get("ACLAW_WS_TIMEOUT", "600"))
BREAKER_THRESHOLD = int(os.environ.get("ACLAW_WS_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN = float(os.environ.get("ACLAW_WS_BREAKER_COOLDOWN", "60"))
_DISCOVERY_POSITIVE_TTL = 60.0
_DISCOVERY_NEGATIVE_TTL = 10.0

PROTOCOL_VERSION = "autoclaw-ws-agent-v1"


class WsUnavailable(RuntimeError):
    """Raised when the WS local-agent path cannot serve a request (caller
    should surface the original network error instead)."""


def enabled() -> bool:
    return ENV_ENABLED


# ── Breaker (network-level failures only) ────────────────────────────────
_lock = threading.Lock()
_consecutive_failures = 0
_open_until = 0.0
_requests_served = 0
_fallback_activations = 0
_last_error = None


def _breaker_record_success():
    global _consecutive_failures, _requests_served
    with _lock:
        _consecutive_failures = 0
        _requests_served += 1


def _breaker_record_failure(reason):
    global _consecutive_failures, _open_until, _last_error, _fallback_activations
    with _lock:
        _consecutive_failures += 1
        _fallback_activations += 1
        _last_error = str(reason)[:200]
        if _consecutive_failures >= BREAKER_THRESHOLD:
            _open_until = time.time() + BREAKER_COOLDOWN
            logger.warning(
                f"WS fallback breaker OPEN after {_consecutive_failures} "
                f"consecutive network failures (cooldown {BREAKER_COOLDOWN}s)")


def breaker_open() -> bool:
    with _lock:
        return time.time() < _open_until


# ── Low-level connect (swappable for tests) ──────────────────────────────
def _connect(url, timeout):
    """Open a WebSocket connection. Returns a websocket-client connection.
    Isolated at module level so tests can monkeypatch it."""
    import websocket  # websocket-client (sync)
    return websocket.create_connection(url, timeout=timeout)


# ── Local-agent discovery ────────────────────────────────────────────────
_agent_cache = {"url": None, "ok_until": 0.0, "bad_until": 0.0}
_agent_lock = threading.Lock()


def _ping_probe(url, timeout=2.0):
    """Send protocol ping, require pong. True when a v1 agent answers."""
    try:
        ws = _connect(url, timeout)
        try:
            ws.send(json.dumps({"type": "ping"}))
            ws.settimeout(timeout)
            deadline = time.time() + timeout
            while time.time() < deadline:
                frame = ws.recv()
                if not frame:
                    continue
                msg = json.loads(frame) if isinstance(frame, str) else {}
                if msg.get("type") == "pong":
                    return True
                if msg.get("type") not in ("ping",):  # tolerate chatty agents
                    return False
            return False
        finally:
            try:
                ws.close()
            except Exception:
                pass
    except Exception:
        return False


def discover_agent(force=False):
    """Find a live local agent. Returns its ws:// URL or None.

    Resolution order: exact ACLAW_WS_AGENT_URL → probe discovery ports on
    127.0.0.1. Positive result cached 60s, negative 10s (so a dead agent
    doesn't cost every chat request a 3×2s probe).
    """
    now = time.time()
    with _agent_lock:
        if not force:
            if _agent_cache["url"] and now < _agent_cache["ok_until"]:
                return _agent_cache["url"]
            if now < _agent_cache["bad_until"]:
                return None
    candidates = []
    if AGENT_URL:
        candidates.append(AGENT_URL)
    else:
        candidates.extend(f"ws://{DISCOVERY_HOST}:{p}/agent" for p in DISCOVERY_PORTS)
    for url in candidates:
        if _ping_probe(url):
            with _agent_lock:
                _agent_cache["url"] = url
                _agent_cache["ok_until"] = time.time() + _DISCOVERY_POSITIVE_TTL
                _agent_cache["bad_until"] = 0.0
            return url
    with _agent_lock:
        _agent_cache["bad_until"] = time.time() + _DISCOVERY_NEGATIVE_TTL
    return None


def available() -> bool:
    """Cheap readiness check used by /health and the routing chain."""
    return enabled() and not breaker_open() and discover_agent() is not None


# ── requests-compatible response shim ────────────────────────────────────
class WsStreamResponse:
    """requests.Response-like shim over the WS local-agent stream.

    Mirrors owl_bridge.OwlStreamResponse so the chat route's SSE generator
    (including the DSML sieve) consumes both transports identically:
      .status_code, .headers, .iter_lines() → bytes lines, .read_error(n)
    """

    def __init__(self, status_code, headers, frame_iter=None, error_text=None):
        self.status_code = int(status_code)
        self.headers = headers or {}
        self._frames = frame_iter
        self._error_text = error_text
        self._ws = None
        self._done = False

    def iter_lines(self):
        if self._frames is None:
            return iter(())
        return self._iter_lines_gen()

    def _iter_lines_gen(self):
        """Assemble raw SSE chunks from chat.delta frames into requests-style
        lines (no trailing newline), exactly like OwlStreamResponse._gen."""
        try:
            buf = b""
            for chunk in self._frames:
                if chunk is None:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    yield line.rstrip(b"\r")
            if buf:
                yield buf.rstrip(b"\r")
        finally:
            self.close()

    def read_error(self, limit=4096):
        if self._error_text is None and self._frames is not None:
            # Drain pending frames — a chat.error envelope commonly arrives
            # AFTER chat.headers (the handshake loop stops at headers). The
            # lazy generator captures it into _error_text. The proxy's
            # error path calls read_error() WITHOUT draining iter_lines,
            # so this must pull the frames itself.
            try:
                for _ in self._frames:
                    pass
            except Exception:
                pass
        if self._error_text is not None:
            return self._error_text[:limit]
        return b""

    def close(self):
        self._done = True
        ws = getattr(self, "_ws", None)
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass
            self._ws = None


# ── Main entry: stream a chat completion through the local agent ─────────
def chat_stream(upstream_body, timeout=None):
    """POST-equivalent of the chat completions call via the local agent.

    Returns WsStreamResponse. Raises WsUnavailable on any transport-level
    failure (caller keeps the original direct-path error in that case).
    """
    if not enabled():
        raise WsUnavailable("ws fallback disabled")
    if breaker_open():
        raise WsUnavailable("ws fallback breaker open (recent network failures)")

    url = discover_agent()
    if not url:
        raise WsUnavailable("no local agent discovered")

    timeout = timeout or STREAM_TIMEOUT
    req_id = uuid.uuid4().hex[:16]

    try:
        ws = _connect(url, timeout)
    except Exception as e:
        _breaker_record_failure(f"connect: {type(e).__name__}: {e}")
        raise WsUnavailable(f"agent connect failed: {e}")

    # WsStreamResponse needs status/headers up-front; the WS conversation
    # delivers them as the first frames. Bridge: run the generator inside a
    # tiny pump that captures headers before returning the response, and
    # hand the response a byte queue via generator chaining.
    # We must know status BEFORE returning the response. Frame-order rule
    # (protocol v1): chat.headers arrives before any chat.delta, so prime
    # the conversation by pulling frames until we see headers — but frames
    # arrive lazily. Resolve: read frames eagerly until chat.headers, then
    # stream the rest lazily through WsStreamResponse.
    ws.settimeout(timeout)
    status = None
    error_text = None
    eager = []
    try:
        ws.send(json.dumps({
            "type": "chat.request", "id": req_id,
            "protocol": PROTOCOL_VERSION, "payload": upstream_body,
        }))
        deadline = time.time() + min(timeout, 30)
        while time.time() < deadline:
            try:
                frame = ws.recv()
            except socket.timeout:
                break
            if frame is None:
                break
            if isinstance(frame, bytes):
                frame = frame.decode("utf-8", "replace")
            try:
                msg = json.loads(frame)
            except json.JSONDecodeError:
                continue
            mtype = msg.get("type")
            if msg.get("id") not in (None, req_id):
                continue
            if mtype == "chat.headers":
                status = int(msg.get("status", 200))
                break
            if mtype == "chat.error":
                status = int(msg.get("status", 502))
                error_text = str(msg.get("message", "local agent error"))
                break
            if mtype in ("chat.delta",):
                eager.append(str(msg.get("sse", "")).encode("utf-8", "replace"))
            elif mtype == "chat.end":
                break
    except Exception as e:
        _breaker_record_failure(f"handshake: {type(e).__name__}: {e}")
        try:
            ws.close()
        except Exception:
            pass
        raise WsUnavailable(f"agent handshake failed: {e}")

    if status is None:
        _breaker_record_failure("agent sent no chat.headers")
        try:
            ws.close()
        except Exception:
            pass
        raise WsUnavailable("agent sent no chat.headers (handshake timeout)")

    resp = WsStreamResponse(status, {"X-AutoClaw-Via": "ws-local-agent"},
                            frame_iter=None, error_text=error_text)
    resp._ws = ws
    resp._eager = eager

    def _lazy_frames():
        for chunk in eager:
            yield chunk
        if error_text is not None:
            # Agent delivered a structured error envelope — the TRANSPORT
            # worked, so this is an upstream decision, not a network fault.
            # Surface verbatim; never trips the breaker (OWL contract parity).
            _breaker_record_success()
            return
        # Remaining frames after headers (chat.delta / chat.end)
        try:
            while True:
                try:
                    frame = ws.recv()
                except socket.timeout:
                    logger.warning("ws fallback: recv timeout mid-stream")
                    _breaker_record_failure("mid-stream recv timeout")
                    return
                except Exception as e:
                    _breaker_record_failure(f"mid-stream: {type(e).__name__}: {e}")
                    return
                if frame is None:
                    _breaker_record_success()
                    return
                if isinstance(frame, bytes):
                    frame = frame.decode("utf-8", "replace")
                try:
                    msg = json.loads(frame)
                except json.JSONDecodeError:
                    continue
                if msg.get("id") not in (None, req_id):
                    continue
                mtype = msg.get("type")
                if mtype == "chat.delta":
                    sse = msg.get("sse", "")
                    if isinstance(sse, str):
                        yield sse.encode("utf-8", "replace")
                    elif isinstance(sse, list):
                        for piece in sse:
                            if isinstance(piece, str):
                                yield piece.encode("utf-8", "replace")
                elif mtype == "chat.error":
                    # Late error envelopes arrive AFTER chat.headers —
                    # capture the message so read_error() surfaces it
                    # (the proxy's error path depends on it).
                    resp._error_text = str(msg.get("message", "local agent error"))
                    logger.warning(f"ws fallback: agent error mid-stream: "
                                   f"{msg.get('message', '')[:120]}")
                    # Well-formed envelope = the TRANSPORT worked; an
                    # upstream decision must not count as a network fault.
                    _breaker_record_success()
                    return
                elif mtype == "chat.end":
                    _breaker_record_success()
                    return
        finally:
            try:
                ws.close()
            except Exception:
                pass
            resp._ws = None

    resp._frames = _lazy_frames()
    return resp


def stats():
    """/health block (never raises)."""
    with _lock:
        base = {
            "enabled": enabled(),
            "protocol": PROTOCOL_VERSION,
            "agent_url_configured": bool(AGENT_URL),
            "discovery_ports": list(DISCOVERY_PORTS),
            "breaker_open": time.time() < _open_until,
            "consecutive_failures": _consecutive_failures,
            "requests_served": _requests_served,
            "fallback_activations": _fallback_activations,
            "last_error": _last_error,
        }
    if enabled():
        try:
            agent = discover_agent()
            base["agent"] = agent or None
            base["agent_reachable"] = bool(agent)
        except Exception:
            base["agent"] = None
            base["agent_reachable"] = False
    return base


def _reset_for_tests():
    """Zero all runtime state (tests must not leak breaker/caches)."""
    global _consecutive_failures, _open_until, _requests_served
    global _fallback_activations, _last_error
    with _lock:
        _consecutive_failures = 0
        _open_until = 0.0
        _requests_served = 0
        _fallback_activations = 0
        _last_error = None
    with _agent_lock:
        _agent_cache.update({"url": None, "ok_until": 0.0, "bad_until": 0.0})
