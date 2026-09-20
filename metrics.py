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

import atexit
import json
import os
import signal
import threading
import time
import hashlib
from collections import Counter, deque

# ── Configuration ────────────────────────────────────────────────────────
REQUEST_LOG_CAP = int(__import__("os").environ.get("ACLAW_METRICS_LOG_CAP", "250"))
LATENCY_WINDOW = int(__import__("os").environ.get("ACLAW_METRICS_WINDOW", "60"))

# Phase-3.1 (research 8-b items 1+5): clients eviction cap + persistence.
CLIENTS_CAP = int(__import__("os").environ.get("ACLAW_METRICS_CLIENTS_CAP", "1000"))
_PERSIST_ENABLED = __import__("os").environ.get("ACLAW_METRICS_PERSIST", "1").strip().lower() not in ("0", "false", "no", "off")
_PERSIST_PATH = __import__("os").environ.get(
    "ACLAW_METRICS_PERSIST_PATH",
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "metrics_state.json"))
_FLUSH_INTERVAL_S = float(__import__("os").environ.get("ACLAW_METRICS_FLUSH_S", "30"))
_ROLLUP_RETENTION_H = int(__import__("os").environ.get("ACLAW_METRICS_ROLLUP_HOURS", "72"))
_PERSIST_SCHEMA = 1

# Backend-switch control surface (Synergy #15: OmniClaw dashboard).
#   owl-first       — OWL free-pool racing, then thermoptic, then direct
#   thermoptic-first — browser-camouflaged egress first (most robust),
#                      then OWL, then direct
#   direct-only     — plain requests, no proxy layers (debug escape hatch)
VALID_BACKEND_PREFS = ("owl-first", "thermoptic-first", "direct-only")

_lock = threading.Lock()
_started_at = time.time()            # process boot (proxy uptime)
_first_started_at = time.time()      # telemetry age (survives restarts)
_persistence_disabled = False        # set on fatal IO errors (fail-safe)
_flusher_started = False

# counters ------------------------------------------------------------------
_totals = Counter()          # "requests", "errors", "blocks", "stream", "buffered"
_status = Counter()          # HTTP status → count
_via = Counter()             # transport label → count ("owl-proxy/vendored", "direct", ...)
_model = Counter()           # client-facing model → count
_blocks = Counter()          # loop_breaker / negative_cache / auth_failed / rate_limited
_features = Counter()        # dsml_shim / ws_fallback / thermoptic / thermoptic_probe_fail
_feature_models = Counter()  # "feature|model" cross-label (per-model DSML frequency)

# hourly rollups (history panel data source; persisted, pruned to retention)
_rollups = {}                # hour_epoch -> {requests, errors, blocks, via{}, models{}, lat_sum, lat_n}
_rollup_hour = int(time.time() // 3600)

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
                if model:
                    _feature_models[f"{feature}|{str(model)[:40]}"] += 1
            # hourly rollup bucket (O(1); pruned lazily on hour boundary)
            global _rollup_hour
            hour = int(now // 3600)
            if hour != _rollup_hour:
                _rollup_hour = hour
                _prune_rollups_locked(hour)
            roll = _rollups.get(hour)
            if roll is None:
                roll = _rollups[hour] = {"requests": 0, "errors": 0,
                                         "blocks": 0, "via": {},
                                         "models": {}, "lat_sum": 0.0,
                                         "lat_n": 0}
            roll["requests"] += 1
            if isinstance(status, int) and status >= 500:
                roll["errors"] += 1
            if block:
                roll["blocks"] += 1
            if via:
                vk = str(via)[:40]
                roll["via"][vk] = roll["via"].get(vk, 0) + 1
            if model:
                mk = str(model)[:40]
                roll["models"][mk] = roll["models"].get(mk, 0) + 1
            lat = float(latency_ms or 0.0)
            roll["lat_sum"] += lat
            roll["lat_n"] += 1
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
            if len(_clients) > CLIENTS_CAP:
                # LRU eviction by last_seen (O(n) only at the cap boundary).
                oldest = min(_clients, key=lambda k: _clients[k]["last_seen"])
                if oldest != masked:
                    _clients.pop(oldest, None)
                else:
                    _clients.pop(sorted(
                        _clients, key=lambda k: _clients[k]["last_seen"])[1],
                        None)
    except Exception:
        pass  # metrics must never break the proxy


def _prune_rollups_locked(current_hour):
    """Drop rollup buckets older than the retention window. Called with
    _lock held (non-reentrant lock — never call public locked API here)."""
    cutoff = current_hour - _ROLLUP_RETENTION_H
    for h in [h for h in _rollups if h < cutoff]:
        del _rollups[h]


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
                "process_started_at": round(_started_at, 3),
                "first_started_at": round(_first_started_at, 3),
                "totals": totals,
                "status_codes": dict(_status),
                "via": dict(_via),
                "models": dict(_model),
                "blocks": blocks,
                "features": dict(_features),
                "feature_models": dict(_feature_models),
                "rollups": _rollups_compact_locked(),
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
    global _started_at, _backend_pref, _backend_pref_updated, _rollups, _rollup_hour
    with _lock:
        _totals.clear()
        _status.clear()
        _via.clear()
        _model.clear()
        _blocks.clear()
        _features.clear()
        _feature_models.clear()
        _log.clear()
        _latency_window.clear()
        _clients.clear()
        _rollups = {}
        _rollup_hour = int(time.time() // 3600)
        _started_at = time.time()
        _dirty["v"] = True
        # NOTE: deliberately does NOT touch _backend_pref — that is control
        # state, not telemetry.


def stats():
    """Compact stats block for the /health endpoint."""
    snap = snapshot()
    snap.pop("request_log", None)
    snap.pop("clients", None)
    snap.pop("latency", None)
    snap.pop("rollups", None)
    snap.pop("feature_models", None)
    return snap


# ── Persistence (Phase-3.1, research 8-b item 1) ─────────────────────────
# Telemetry survives proxy restarts: single versioned JSON file, atomic
# replace, flushed by a daemon thread + atexit. Never hold _lock across disk
# I/O (the non-reentrant-lock deadlock lesson from v2.3.0). Any fatal IO
# error disables persistence for the process lifetime — metrics collection
# itself must keep working (fail-safe contract).

_dirty = {"v": True}


def _rollups_compact_locked():
    """Rollups as a compact list (oldest→newest) for snapshot consumers."""
    out = []
    for h in sorted(_rollups):
        r = _rollups[h]
        out.append({
            "hour": h,
            "requests": r["requests"], "errors": r["errors"],
            "blocks": r["blocks"],
            "via": dict(r["via"]), "models": dict(r["models"]),
            "lat_avg_ms": round(r["lat_sum"] / r["lat_n"], 1) if r["lat_n"] else 0.0,
            "lat_n": r["lat_n"],
        })
    return out


def _serialize_state_locked():
    """Plain-JSON snapshot of everything worth persisting (called with
    _lock held; control state backend_pref deliberately NOT persisted)."""
    return {
        "schema": _PERSIST_SCHEMA,
        "saved_at": round(time.time(), 3),
        "first_started_at": round(_first_started_at, 3),
        "totals": dict(_totals),
        "status_codes": dict(_status),
        "via": dict(_via),
        "models": dict(_model),
        "blocks": dict(_blocks),
        "features": dict(_features),
        "feature_models": dict(_feature_models),
        "clients": {k: dict(v) for k, v in list(_clients.items())[:CLIENTS_CAP]},
        "request_log": list(_log),
        "latency_window": list(_latency_window),
        "rollups": {str(h): dict(v) for h, v in _rollups.items()},
        "rollup_retention_h": _ROLLUP_RETENTION_H,
    }


def _write_state():
    """Atomic write of the current state. Returns True on success."""
    global _persistence_disabled
    if not _PERSIST_ENABLED or _persistence_disabled:
        return False
    try:
        with _lock:
            payload = _serialize_state_locked()
            _dirty["v"] = False
        tmp = _PERSIST_PATH + ".tmp"
        os.makedirs(os.path.dirname(_PERSIST_PATH) or ".", exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, _PERSIST_PATH)
        return True
    except Exception as exc:
        # Disk full / permissions / whatever: disable for this process
        # lifetime, log once, keep serving from memory.
        try:
            import logging
            logging.getLogger("autoclaw.metrics").warning(
                "metrics persistence disabled after IO error: %s", exc)
        except Exception:
            pass
        _persistence_disabled = True
        return False


def _load_state():
    """Load persisted telemetry at boot. Corrupt/schema-mismatched files are
    renamed aside and ignored (fresh start). Never raises."""
    global _first_started_at, _persistence_disabled
    if not _PERSIST_ENABLED or _persistence_disabled:
        return False
    try:
        if not os.path.exists(_PERSIST_PATH):
            return False
        with open(_PERSIST_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict) or data.get("schema") != _PERSIST_SCHEMA:
            raise ValueError("schema mismatch")
        with _lock:
            _totals.update({k: v for k, v in (data.get("totals") or {}).items()
                            if isinstance(v, (int, float))})
            for key, target in (("status_codes", _status), ("via", _via),
                                ("models", _model), ("blocks", _blocks),
                                ("features", _features),
                                ("feature_models", _feature_models)):
                target.update({k: v for k, v in (data.get(key) or {}).items()
                               if isinstance(v, (int, float))})
            clients = data.get("clients") or {}
            if isinstance(clients, dict):
                for k, v in list(clients.items())[:CLIENTS_CAP]:
                    if isinstance(v, dict) and "requests" in v:
                        _clients[str(k)[:64]] = dict(v)
            log = data.get("request_log") or []
            if isinstance(log, list):
                _log.extend(log[-REQUEST_LOG_CAP:])
            latw = data.get("latency_window") or []
            if isinstance(latw, list):
                _latency_window.extend(
                    [x for x in latw if isinstance(x, (int, float))]
                    [-LATENCY_WINDOW:])
            rollups = data.get("rollups") or {}
            if isinstance(rollups, dict):
                for h, v in rollups.items():
                    try:
                        hk = int(h)
                        if isinstance(v, dict) and "requests" in v:
                            _rollups[hk] = dict(v)
                    except (ValueError, TypeError):
                        continue
                _prune_rollups_locked(int(time.time() // 3600))
            fs = data.get("first_started_at")
            if isinstance(fs, (int, float)):
                _first_started_at = float(fs)
        return True
    except Exception as exc:
        # Corrupt file: rename aside, start fresh (never crash boot).
        try:
            if os.path.exists(_PERSIST_PATH):
                os.replace(_PERSIST_PATH,
                           _PERSIST_PATH + f".corrupt-{int(time.time())}")
            import logging
            logging.getLogger("autoclaw.metrics").warning(
                "metrics state discarded (%s); starting fresh", exc)
        except Exception:
            pass
        return False


def _flush_loop():
    interval = _FLUSH_INTERVAL_S if _FLUSH_INTERVAL_S and _FLUSH_INTERVAL_S > 0 else 30.0
    while True:
        time.sleep(interval)
        if _dirty["v"]:
            _write_state()


def init():
    """Boot hook: load persisted state + start the flusher. Call once from
    the proxy before serving (both __main__ and wsgi paths). Idempotent,
    never raises. ACLAW_METRICS_FLUSH_S=0 disables the background flusher
    (atexit flush still runs)."""
    global _flusher_started
    try:
        if _flusher_started:
            return False
        _flusher_started = True
        loaded = _load_state()
        if _PERSIST_ENABLED and not _persistence_disabled:
            if _FLUSH_INTERVAL_S and _FLUSH_INTERVAL_S > 0:
                t = threading.Thread(target=_flush_loop, name="metrics-flush",
                                     daemon=True)
                t.start()
            atexit.register(_write_state)
            try:  # graceful flush on SIGTERM (systemd/docker stops)
                signal.signal(signal.SIGTERM, _sigterm_flush)
            except (ValueError, OSError):  # pragma: no cover — main-thread only
                pass
        return loaded
    except Exception:
        return False


def _sigterm_flush(signum, frame):  # pragma: no cover — signal path
    try:
        _write_state()
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTERM)


def flush():
    """Force a persistence write (tests + admin paths). Returns bool."""
    return _write_state()


def persistence_info():
    """Diagnostics for /health's metrics block."""
    return {"enabled": bool(_PERSIST_ENABLED and not _persistence_disabled),
            "path": _PERSIST_PATH if _PERSIST_ENABLED else None,
            "flush_interval_s": _FLUSH_INTERVAL_S,
            "rollup_retention_h": _ROLLUP_RETENTION_H}
