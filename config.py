"""AutoClaw Proxy — Constants & Config"""

import os

# ── App Signing ──
APP_ID = "100003"
# APP_KEY moved to env var (Audit Fix CVE-1: hardcoded signing key in source)
APP_KEY = os.environ.get("AUTOCLAW_APP_KEY", "38d2391985e2369a5fb8227d8e6cd5e5")
if APP_KEY == "38d2391985e2369a5fb8227d8e6cd5e5":
    import warnings
    warnings.warn("AUTOCLAW_APP_KEY not set — using default (insecure, forgeable). Set env var in production.")
PRODUCT = "autoclaw"
VERSION = "2.5.0"
PLATFORM = "win"

# ── Endpoints ──
USER_API_BASE = "https://autoglm-api.autoglm.ai"
LLM_PROXY_BASE = "https://autoglm-api.autoglm.ai/autoclaw-proxy/proxy/autoclaw"
CHAT_COMPLETIONS = f"{LLM_PROXY_BASE}/chat/completions"

# ── Auth Endpoints ──
GOOGLE_OAUTH_URL = f"{USER_API_BASE}/userapi/overseasv1/google-oauth-url"
GOOGLE_OAUTH_LOGIN = f"{USER_API_BASE}/userapi/overseasv1/google-oauth-login"
REFRESH_URL = f"{USER_API_BASE}/userapi/v1/refresh"
PROFILE_URL = f"{USER_API_BASE}/userapi/v1/user-profile"
WALLET_URL = f"{USER_API_BASE}/agent-assetmgr/api/v2/wallets?biz_app_id=autoclaw"
LEDGER_URL = f"{USER_API_BASE}/agent-assetmgr/api/v1/ledgers_std?asset_type=point&wallet_type=all"

# ── Token Storage ──
TOKENS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tokens.json")

# ── TLS Verification ──
# Set AUTOCLAW_TLS_VERIFY=true to enable (recommended for production)
TLS_VERIFY = os.environ.get("AUTOCLAW_TLS_VERIFY", "false").lower() == "true"

# ── Proxy Server ──
PROXY_HOST = os.environ.get("AUTOCLAW_PROXY_HOST", "127.0.0.1")  # Default localhost (security: was 0.0.0.0)
PROXY_PORT = int(os.environ.get("AUTOCLAW_PROXY_PORT", "31000"))

# ── Proxy API Key (optional auth for the proxy server itself) ──
PROXY_API_KEY = os.environ.get("AUTOCLAW_PROXY_API_KEY", None)

# ── Model Map (X-Request-Model → alias) ──
# Key = what client sends as "model" in OpenAI body
# Value = X-Request-Model header value sent to AutoClaw upstream
MODEL_MAP = {
    # Best — real GLM-5.2 (may be unavailable)
    "glm-5.2": "openrouter_glm-5.2",
    "glm-5.2-true": "openrouter_glm-5.2",
    # Cheapest — glm-5-turbo (always available)
    "glm-5-turbo": "zai_glm-5-turbo",
    "cheap": "zai_glm-5-turbo",
    # Avoid — secretly DeepSeek-V4-Pro ~7x cost
    "auto": "zai_auto",
    "deepseek": "zai_auto",
}

DEFAULT_MODEL = "zai_glm-5-turbo"  # Changed from openrouter_glm-5.2 (cheaper, always available)

# ── Strict Model Validation ──
# When true, unknown model names return 400 instead of silent fallback to DEFAULT_MODEL
STRICT_MODEL_VALIDATION = os.environ.get("AUTOCLAW_STRICT_MODELS", "true").lower() == "true"

# ── Access Token TTL (24h, refresh 5min before expiry) ──
ACCESS_TOKEN_TTL = 86400  # 24h
REFRESH_MARGIN = 300      # 5min before expiry

# ── Rotating Proxy for Registration ──
# Load proxies from proxies.txt (format: host:port:user:pass per line)
# Used by auth.py to bypass 630014 rate limit. Each account gets next proxy round-robin.
import os as _os
_PROXY_FILE = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "proxies.txt")
PROXY_LIST = []
if _os.path.exists(_PROXY_FILE):
    with open(_PROXY_FILE) as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or ":" not in _line:
                continue
            _parts = _line.split(":")
            if len(_parts) == 4:
                _host, _port, _user, _pwd = _parts
                PROXY_LIST.append({
                    "server": f"http://{_host}:{_port}",
                    "username": _user,
                    "password": _pwd,
                })

# ──────────────────────────────────────────────────────────────────────────
# Synergy 6: OWL-AGENT Proxy Defense Layer (v5.3-autoclaw, vendored)
# Source: OWL-AGENT v5.3 proxy_defense.py (hybrid: external install wins)
# ──────────────────────────────────────────────────────────────────────────
# Proxy-first upstream routing: all upstream HTTP (chat SSE, token refresh,
# profile, wallet, ledger) races HEDGE_FANOUT free proxies in parallel for
# connection establishment; first to deliver headers wins; losers banned
# (single-strike, idempotent, backoff-scaled). Direct fallback always on.
#   OWL_PROXY_ENABLED     1|0     master switch (default ON)
#   OWL_BASE_DIR                  state dir (default ~/.owl-agent; shared
#                                 with an external OWL-AGENT install)
#   OWL_EXTERNAL_MODULE           path to external proxy_defense.py
#   OWL_HEDGE_FANOUT      int     proxies raced per request (default 3)
#   OWL_PROXY_TIMEOUT     float   per-proxy connect cap s (default 6)
#   OWL_DIRECT_TIMEOUT    float   direct fallback connect cap s (default 30)
#   OWL_TLS_IMPERSONATE   str     curl_cffi target, e.g. chrome110
#                                 (requires `pip install curl_cffi`)
#   OWL_CACHE_TTL         int     GET cache seconds; 0 disables (default 0:
#                                 auth responses are account-specific)
#   OWL_SEED_URL          str     proxy source list (proxifly CDN)
#   OWL_SEED_COUNT        int     max proxies seeded per cycle (default 100)
#   OWL_VALIDATE_URL      str     connectivity probe (default gstatic 204)
#   OWL_STARTUP_TIMEOUT   float   seeding wait before first request (default 15)
# Google-OAuth registration calls keep proxies.txt round-robin when set —
# dedicated paid proxies outrank the free pool; OWL engages only when none.
OWL_ENABLED_DEFAULT = os.environ.get("OWL_PROXY_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")


# ── Billing Header Quirks ──
# LLM proxy: X-Authorization (capital X)
# Assetmgr: authorization (lowercase)


# ──────────────────────────────────────────────────────────────────────────
# Synergy 1: Output-Cap Clamping
# Source: eequaled/GLM_proxy lib/core.js (OUTPUT_CAPS, clampMaxOutput)
# ──────────────────────────────────────────────────────────────────────────
# Probe-verified: requesting >131072 output tokens triggers silent DeepSeek
# substitution in AutoClaw cloud — the response is delivered but billed as
# DeepSeek-V4-Pro credits (~7x more expensive than GLM-5.2). The client
# never sees a warning, only the operator gets the bill at month's end.
# These caps prevent that by clamping max_tokens per-model before the
# request is forwarded upstream.
OUTPUT_CAPS = {
    "openrouter_glm-5.2": 131072,   # GLM-5.2 — exact probe-verified threshold
    "zai_glm-5-turbo": 65536,        # GLM-5-Turbo — conservative (always available)
    "zai_auto": 32768,               # DeepSeek-V4-Pro — cap aggressively to
                                      # prevent billing surprise when "auto" alias
                                      # is used (it always routes to DeepSeek)
    "zai_glm-5": 131072,             # GLM-5 — same family as 5.2
    "default": 65536,                 # Unknown model — be safe
}


def clamp_max_output(model_alias: str, requested: int = None) -> int:
    """Clamp max_tokens to safe model-specific limit.

    Prevents silent DeepSeek substitution when requesting >131072 output
    tokens — AutoClaw cloud silently switches to DeepSeek-V4-Pro (7x cost)
    when the requested output exceeds the GLM cap, and the operator only
    finds out at month-end billing.

    Synergy: eequaled/GLM_proxy lib/core.js (clampMaxOutput)

    Args:
        model_alias: client-facing model alias (e.g. "glm-5.2", "cheap",
            "auto"). Will be looked up via MODEL_MAP to find the upstream
            model name. Falls back to DEFAULT_MODEL if not in MODEL_MAP.
        requested: client-requested max_tokens value. If None or larger
            than the model's cap, the cap is returned instead.

    Returns:
        int: safe max_tokens value, never exceeding the model's cap. If
        `requested` is smaller than the cap, `requested` is returned
        unchanged (clamp, never inflate).
    """
    upstream = MODEL_MAP.get(model_alias, DEFAULT_MODEL) if isinstance(model_alias, str) else None
    cap = OUTPUT_CAPS.get(upstream, OUTPUT_CAPS["default"]) if upstream else OUTPUT_CAPS["default"]
    if requested is None or requested > cap:
        return cap
    return requested


# ──────────────────────────────────────────────────────────────────────────
# Synergy 2: System-Banner Injection
# Source: eequaled/GLM_proxy lib/core.js (injectSystemBanner,
#         AUTOCLAW_SYSTEM_BANNER)
# ──────────────────────────────────────────────────────────────────────────
# Probe-verified: AutoClaw upstream requires a specific system banner as
# the first system message in the chat. Without it, the upstream returns
# HTTP 400 and traffic silently falls into the unmetered WS agent path
# (which is unreliable and bypasses billing telemetry). This banner is
# prepended to the user's existing system message, or inserted as a new
# system message at index 0 if the request has no system message.
AUTOCLAW_SYSTEM_BANNER = os.environ.get(
    "AUTOCLAW_SYSTEM_BANNER",
    "You are a personal assistant running inside OpenClaw.\n## Tooling"
)
