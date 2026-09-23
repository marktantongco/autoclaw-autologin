"""AutoClaw thermoptic Bridge — real-browser traffic camouflage transport.

Integrates mandatoryprogrammer/thermoptic (ISC license) as an optional
egress tier: thermoptic is a local HTTP proxy that replays requests through
a real Chrome/Chromium instance (via CDP), making TLS+HTTP fingerprints
(JA3/JA4/JA4H and friends) indistinguishable from a genuine browser. This
supersedes the Phase-3 "uTLS Chrome 120 ClientHello" synergy (#14) with a
strictly stronger mechanism — real browser fingerprints instead of forged
ClientHellos — without a Go/Rust TLS bridge.

Where it sits (Synergy #14 × Synergy 6):
    OWL free-pool racing (existing) → thermoptic → direct
  Order is switchable at runtime via the dashboard control surface
  (metrics.set_backend_pref): "owl-first" (default) or "thermoptic-first"
  (browser camouflage wins — most robust anti-bot egress), or
  "direct-only".

Operation model:
  thermoptic runs OUT-OF-PROCESS (typically `docker compose up` from the
  upstream repo — see deploy/thermoptic-README.md). This bridge only
  speaks HTTP-proxy protocol to it, probes its health, and degrades
  gracefully when it is down. It never blocks startup.

Contract (mirrors owl_bridge fail-safety):
  - only transport-level faults (proxy connect refused, probe timeouts)
    trip the breaker; upstream HTTP error statuses pass through verbatim
  - every helper is fail-safe; routing helpers raise ThermopticUnavailable
    and callers fall to the next tier

Configuration (all env):
  OWL_THERMOPTIC_ENABLED        1|0  master switch (default OFF)
  OWL_THERMOPTIC_URL            default http://127.0.0.1:1234
                                (upstream default HTTP_PROXY_PORT)
  OWL_THERMOPTIC_USERNAME       proxy auth (optional; thermoptic
  OWL_THERMOPTIC_PASSWORD       enforces when PROXY_USERNAME/PASSWORD set)
  OWL_THERMOPTIC_CA             path to thermoptic rootCA.crt (recommended;
                                default verify=False like the rest of the
                                proxy — thermoptic MITMs TLS by design)
  OWL_THERMOPTIC_TIMEOUT        float, probe + connect cap s (default 6)
  OWL_THERMOPTIC_PROBE_TTL      float, positive probe cache s (default 30)
  OWL_THERMOPTIC_BREAKER_THRESHOLD  int consecutive transport failures
                                before degraded (default 3)
  OWL_THERMOPTIC_BREAKER_COOLDOWN   float degraded seconds (default 60)
"""

import os
import time
import logging
import threading

logger = logging.getLogger("autoclaw.thermoptic")

# ── Configuration ────────────────────────────────────────────────────────
ENV_ENABLED = os.environ.get("OWL_THERMOPTIC_ENABLED", "0").strip().lower() not in (
    "0", "false", "no", "off")
BASE_URL = os.environ.get("OWL_THERMOPTIC_URL", "http://127.0.0.1:1234").rstrip("/")
USERNAME = os.environ.get("OWL_THERMOPTIC_USERNAME", "").strip()
PASSWORD = os.environ.get("OWL_THERMOPTIC_PASSWORD", "").strip()
CA_FILE = os.environ.get("OWL_THERMOPTIC_CA", "").strip()
TIMEOUT = float(os.environ.get("OWL_THERMOPTIC_TIMEOUT", "6"))
PROBE_TTL = float(os.environ.get("OWL_THERMOPTIC_PROBE_TTL", "30"))
BREAKER_THRESHOLD = int(os.environ.get("OWL_THERMOPTIC_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN = float(os.environ.get("OWL_THERMOPTIC_BREAKER_COOLDOWN", "60"))

PROBE_URL = os.environ.get("OWL_VALIDATE_URL", "https://www.gstatic.com/generate_204")

# thermoptic banner claim (kept in sync with upstream repo README)
UPSTREAM_REPO = "https://github.com/mandatoryprogrammer/thermoptic"


class ThermopticUnavailable(RuntimeError):
    """Raised when the thermoptic tier cannot serve a request (caller
    should fall through to the next transport tier)."""


def enabled() -> bool:
    return ENV_ENABLED


def proxy_url() -> str:
    """HTTP(S) proxy URL for requests/httpx, including basic auth if set."""
    if USERNAME and PASSWORD:
        import urllib.parse
        auth = urllib.parse.quote(USERNAME, safe="") + ":" + urllib.parse.quote(PASSWORD, safe="")
        scheme, rest = BASE_URL.split("://", 1)
        return f"{scheme}://{auth}@{rest}"
    return BASE_URL


def requests_proxies() -> dict:
    return {"http": proxy_url(), "https": proxy_url()}


def verify():
    """TLS verification policy for traffic routed through thermoptic.
    thermoptic re-terminates TLS with its own CA; trusting that CA is the
    correct posture. Falls back to False (project-wide default) when no
    CA file is configured."""
    if CA_FILE and os.path.exists(CA_FILE):
        return CA_FILE
    return False


# ── Breaker (transport-level failures only) ──────────────────────────────
_lock = threading.Lock()
_consecutive_failures = 0
_degraded_until = 0.0
_requests_served = 0
_probe_failures = 0
_last_error = None


def _record_success():
    global _consecutive_failures, _requests_served
    with _lock:
        _consecutive_failures = 0
        _requests_served += 1


def _record_failure(reason):
    global _consecutive_failures, _degraded_until, _last_error
    with _lock:
        _consecutive_failures += 1
        _last_error = str(reason)[:200]
        if _consecutive_failures >= BREAKER_THRESHOLD:
            _degraded_until = time.time() + BREAKER_COOLDOWN
            logger.warning(
                f"thermoptic breaker OPEN after {_consecutive_failures} "
                f"transport failures (cooldown {BREAKER_COOLDOWN}s)")


def breaker_open() -> bool:
    with _lock:
        return time.time() < _degraded_until


# ── Health probe (cached) ────────────────────────────────────────────────
_probe_cache = {"ok_until": 0.0, "bad_until": 0.0}
_probe_lock = threading.Lock()


def _probe_once(timeout=None):
    """One live probe: tiny GET through the thermoptic proxy."""
    import requests as req_lib
    try:
        r = req_lib.get(PROBE_URL, proxies=requests_proxies(),
                        timeout=timeout or TIMEOUT, verify=verify(),
                        allow_redirects=False)
        _ = r.status_code  # any HTTP response proves the tunnel works
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def probe(force=False):
    """Cached health probe. True when thermoptic answered over the tunnel."""
    now = time.time()
    with _probe_lock:
        if not force:
            if now < _probe_cache["ok_until"]:
                return True
            if now < _probe_cache["bad_until"]:
                return False
    ok, err = _probe_once()
    with _probe_lock:
        if ok:
            _probe_cache["ok_until"] = time.time() + PROBE_TTL
            _probe_cache["bad_until"] = 0.0
        else:
            global _probe_failures
            with _lock:
                _probe_failures += 1
            _probe_cache["bad_until"] = time.time() + min(10.0, PROBE_TTL)
            logger.info(f"thermoptic probe failed: {err}")
    return ok


def healthy() -> bool:
    """Cheap gate used by the routing chain: enabled + not degraded +
    probe cached-positive (a failed probe is retried after the short
    negative TTL, so recovery is quick)."""
    if not enabled() or breaker_open():
        return False
    return probe()


# ── Transport entry point ────────────────────────────────────────────────
def http_request(method, url, headers=None, json_body=None, stream=False,
                 timeout=600):
    """requests-compatible call routed through thermoptic.

    Returns the raw requests.Response. Raises ThermopticUnavailable on
    transport-level faults (never on upstream HTTP error statuses — those
    are valid responses and flow back like any other).
    """
    if not enabled():
        raise ThermopticUnavailable("thermoptic disabled")
    if breaker_open():
        raise ThermopticUnavailable("thermoptic breaker open")
    import requests as req_lib
    try:
        resp = req_lib.request(
            method.upper(), url, headers=headers, json=json_body,
            proxies=requests_proxies(), verify=verify(),
            stream=stream, timeout=timeout)
        _record_success()
        return resp
    except req_lib.exceptions.RequestException as e:
        # Transport-level fault (connect/timeout/proxy handshake).
        _record_failure(f"{type(e).__name__}: {e}")
        raise ThermopticUnavailable(f"thermoptic transport failed: {e}")


def stats():
    """/health block (never raises)."""
    with _lock:
        base = {
            "enabled": enabled(),
            "url": BASE_URL,
            "source": UPSTREAM_REPO,
            "auth_configured": bool(USERNAME and PASSWORD),
            "ca_configured": bool(CA_FILE),
            "breaker_open": time.time() < _degraded_until,
            "consecutive_failures": _consecutive_failures,
            "requests_served": _requests_served,
            "probe_failures": _probe_failures,
            "last_error": _last_error,
        }
    if enabled():
        try:
            base["healthy"] = probe()
        except Exception:
            base["healthy"] = False
    else:
        base["healthy"] = False
    return base


def _reset_for_tests():
    """Zero all runtime state (tests must not leak breaker/probe caches)."""
    global _consecutive_failures, _degraded_until, _requests_served
    global _probe_failures, _last_error
    with _lock:
        _consecutive_failures = 0
        _degraded_until = 0.0
        _requests_served = 0
        _probe_failures = 0
        _last_error = None
    with _probe_lock:
        _probe_cache.update({"ok_until": 0.0, "bad_until": 0.0})
