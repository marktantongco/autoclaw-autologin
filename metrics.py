"""AutoClaw Proxy — Phase-3 runtime metrics registry (Synergy #15 source:
guell11/OmniClaw-GLM-Proxy "Local React Dashboard").

A thread-safe, in-process metrics store that the React dashboard reads via
/api/dashboard/state (REST snapshot) and /api/dashboard/stream (long-running
WebSocket push). Zero external dependencies; the registry lives inside the
proxy worker so metrics are always consistent with what actually served
traffic.

Collected surface (per the OmniClaw dashboard spec):
  - live request counters (total, per-status, per-transport "via", per-model)
  - request log ring buffer (last N requests with latency, route, model)
  - masked-IP client list (SHA-256 prefix — raw IPs are NEVER stored)
  - block counters: loop_breaker trips, negative-cache 429s, auth failures,
    rate-limit hits
  - feature counters: DSML shim activations, WS local-agent fallbacks,
    thermoptic-routed requests
  - backend-switch control surface state (owl-first | thermoptic-first |
    direct-only) — the dashboard writes it, the chat route reads it.

All functions are fail-safe: metrics collection must never break a proxied
request, so every public entry point swallows non-fatal errors.
"""

import time
import hashlib
import threading
from collections import Counter

# ── Configuration ────────────────────────────────────────────────────────
REQUEST_LOG_CAP = int(__import__("os").environ.get("ACLAW_METRICS_LOG_CAP", "250"))
LATENCY_WINDOW = int(__import__("os").environ.get("ACLAW_METRICS_WINDOW", "60"))

# Backend-switch control surface (Synergy #15: OmniClaw dashboard).
#   owl-first       — OWL free-pool racing, then thermoptic, then direct
#   thermoptic-first — browser-camouflaged egress first (most robust),
#                      then OWL, then direct
#   direct-only     — plain requests, no proxy layers (debug escape hatch)
VALID_BACKEND_PREFS = ("owl-first", "thermoptic-first", "direct-only")

_lock = threading.Lock()
_started_at = time.time()

# counters ------------------------------------------------------------------
_totals = Counter()          # "requests", "errors", "blocks", "stream", "buffered"
_status = Counter()          # HTTP status → count
_via = Counter()             # transport label → count ("owl-proxy/vendored", "direct", ...)
_model = Counter()           # client-facing model → count
_blocks = Counter()          # loop_breaker / negative_cache / auth_failed / rate_limited
_features = Counter()        # dsml_shim / ws_fallback / thermoptic / thermoptic_probe_fail

# request log ring buffer ----------------------------------------------------
_log = []                    # newest last, capped at REQUEST_LOG_CAP
_latency_window = []         # last LATENCY_WINDOW latencies (ms) for sparkline

# masked-IP client list -------------------------------------------------------
_clients = {}                # masked_ip → {"requests": n, "first_seen": ts, "last_seen": ts}

# runtime control state (dashboard writes, chat route reads) ------------------
_backend_pref = "owl-first"
_backend_pref_updated = time.time()


def mask_ip(ip: str) -> str:
    """Stable masked client identifier — SHA-256 prefix, never the raw IP.

    OmniClaw's dashboard shows "which clients are hammering the proxy"
    without leaking the operator's LAN addresses into screenshots/bug
    reports. IPv6 and IPv4 both hash the same way.
    """
    if not ip:
        return "unknown"
    return hashlib.sha256(ip.encode("utf-8", "replace")).hexdigest()[:12]


def record(route, status, via="direct", latency_ms=0.0, client_ip=None,
           model=None, stream=False, block=None, feature=None):
    """Record one proxied request (or block event) into the registry.

    Called from proxy.py after_request / inline error paths. Never raises.
      route      — e.g. "/v1/chat/completions"
      status     — int HTTP status (client-facing)
      via        — transport that served it: "direct", "owl-proxy/<backend>",
                   "thermoptic", "ws-local-agent"
      latency_ms — wall time of the whole client-facing request
      client_ip  — raw peer IP (immediately masked, never stored raw)
      model      — client-facing model alias
      stream     — True when the client asked for SSE
      block      — None or a block class ("loop_breaker", "negative_cache",
                   "auth_failed", "rate_limited")
      feature    — None or feature tag ("dsml_shim", "ws_fallback", ...)
    """
    try:
        masked = mask_ip(client_ip)
        now = time.time()
        with _lock:
            _totals["requests"] += 1
            if isinstance(status, int):
                if status >= 500:
                    _totals["errors"] += 1
                _status[str(status)] += 1
            if via:
                _via[str(via)] += 1
            if model:
                _model[str(model)] += 1
            if stream:
                _totals["stream"] += 1
            else:
                _totals["buffered"] += 1
            if block:
                _totals["blocks"] += 1
                _blocks[str(block)] += 1
            if feature:
                _features[str(feature)] += 1
            entry = {
                "ts": round(now, 3),
                "route": str(route)[:64],
                "status": int(status) if isinstance(status, int) else 0,
                "via": str(via)[:40],
                "latency_ms": round(float(latency_ms or 0.0), 1),
                "client": masked,
                "model": str(model)[:40] if model else None,
                "stream": bool(stream),
                "block": str(block)[:32] if block else None,
                "feature": str(feature)[:32] if feature else None,
            }
            _log.append(entry)
            if len(_log) > REQUEST_LOG_CAP:
                del _log[: len(_log) - REQUEST_LOG_CAP]
            _latency_window.append(entry["latency_ms"])
            if len(_latency_window) > LATENCY_WINDOW:
                del _latency_window[: len(_latency_window) - LATENCY_WINDOW]
            cli = _clients.setdefault(
                masked, {"requests": 0, "first_seen": round(now, 3)})
            cli["requests"] += 1
            cli["last_seen"] = round(now, 3)
    except Exception:
        pass  # metrics must never break the proxy


# ── Backend-switch control surface ───────────────────────────────────────
def backend_pref():
    """Current routing preference ("owl-first" | "thermoptic-first" |
    "direct-only") — read by proxy.py's transport chain."""
    with _lock:
        return _backend_pref


def set_backend_pref(pref):
    """Set routing preference. Returns (ok, message)."""
    global _backend_pref, _backend_pref_updated
    if pref not in VALID_BACKEND_PREFS:
        return False, f"invalid backend pref '{pref}' (valid: {', '.join(VALID_BACKEND_PREFS)})"
    with _lock:
        _backend_pref = pref
        _backend_pref_updated = time.time()
    return True, f"backend preference set to {pref}"


def backend_pref_info():
    with _lock:
        return {"backend_pref": _backend_pref,
                "updated_at": round(_backend_pref_updated, 3),
                "valid": list(VALID_BACKEND_PREFS)}


# ── Snapshot for the dashboard ───────────────────────────────────────────
def _pct(n, d):
    return round(100.0 * n / d, 1) if d else 0.0


def snapshot():
    """Full dashboard state (JSON-serializable dict). Never raises."""
    try:
        with _lock:
            total = _totals["requests"]
            # Zero-fill canonical keys so consumers can index without
            # .get() (an empty Counter omits them after reset()).
            totals = dict(_totals)
            for k in ("requests", "errors", "blocks", "stream", "buffered"):
                totals.setdefault(k, 0)
            blocks = dict(_blocks)
            for k in ("loop_breaker", "negative_cache", "auth_failed",
                      "rate_limited"):
                blocks.setdefault(k, 0)
            lats = list(_latency_window)
            log = list(_log)
            clients = {k: dict(v) for k, v in _clients.items()}
            uptime = time.time() - _started_at
            avg = round(sum(lats) / len(lats), 1) if lats else 0.0
            lats_sorted = sorted(lats)
            p95 = (lats_sorted[int(0.95 * (len(lats_sorted) - 1))]
                   if lats_sorted else 0.0)
            return {
                "uptime_s": round(uptime, 1),
                "totals": totals,
                "status_codes": dict(_status),
                "via": dict(_via),
                "models": dict(_model),
                "blocks": blocks,
                "features": dict(_features),
                "error_rate_pct": _pct(_totals["errors"], total),
                "latency": {"avg_ms": avg, "p95_ms": round(p95, 1),
                            "window": lats[-LATENCY_WINDOW:]},
                "clients": clients,
                "client_count": len(clients),
                "request_log": log,
                "request_log_cap": REQUEST_LOG_CAP,
                # NOTE: control info is inlined here — we already hold _lock
                # and threading.Lock is non-reentrant, so calling
                # backend_pref_info() from inside the locked region would
                # deadlock (found by the /health route test timeout).
                "control": {"backend_pref": _backend_pref,
                            "updated_at": round(_backend_pref_updated, 3),
                            "valid": list(VALID_BACKEND_PREFS)},
            }
    except Exception as e:
        return {"error": f"metrics snapshot failed: {e}"}


def reset():
    """Wipe all counters/log (used by tests and the dashboard reset action)."""
    global _started_at, _backend_pref, _backend_pref_updated
    with _lock:
        _totals.clear()
        _status.clear()
        _via.clear()
        _model.clear()
        _blocks.clear()
        _features.clear()
        _log.clear()
        _latency_window.clear()
        _clients.clear()
        _started_at = time.time()
        # NOTE: deliberately does NOT touch _backend_pref — that is control
        # state, not telemetry.


def stats():
    """Compact stats block for the /health endpoint."""
    snap = snapshot()
    snap.pop("request_log", None)
    snap.pop("clients", None)
    snap.pop("latency", None)
    return snap
