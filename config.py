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
VERSION = "1.9.1"
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

# ── Billing Header Quirks ──
# LLM proxy: X-Authorization (capital X)
# Assetmgr: authorization (lowercase)
