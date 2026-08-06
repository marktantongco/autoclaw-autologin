# AutoClaw Auto-Login

OpenAI-compatible reverse proxy + Google OAuth auto-login automation for [AutoGLM/Z.ai](https://z.ai) (AutoClaw backend).

AutoClaw gives free access to GLM-5.2, GLM-5 Turbo, and DeepSeek models via Google SSO. This tool automates the login flow and exposes a local OpenAI-compatible API — so any OpenAI client (Cursor, Continue, OpenWebUI, etc.) can use GLM models for free.

Uses [CloakBrowser](https://cloakbrowser.dev) — C++ source-level stealth Chromium (58 patches) instead of raw Playwright. Passes Cloudflare, reCAPTCHA v3, FingerprintJS, BrowserScan without JS injection.

## Features

- **Auto-Login**: Automated Google OAuth login for AutoClaw accounts (batch mode, concurrent)
- **Rotating Proxy**: Bypass IP rate limit (630014) — each account uses a different proxy IP
- **OpenAI-compatible Proxy**: Drop-in `/v1/chat/completions` endpoint — works with any OpenAI client
- **Token Management**: Auto-refresh (24h TTL), round-robin rotation, wallet balance monitoring
- **Dashboard**: Web UI at `http://localhost:31000` for monitoring accounts, credits, token expiry
- **Stealth**: CloakBrowser handles all fingerprinting at C++ binary level — no JS injection needed

## Prerequisites

- **Python 3.10+** — [Download here](https://www.python.org/downloads/) (check "Add Python to PATH" during install)
- **Google accounts** — email:password for each AutoClaw account you want to login

## Quick Start (Windows — One-Click)

```
1. Double-click setup.bat      → installs deps + CloakBrowser binary (~535MB)
2. Edit accounts.txt           → add email:password per line
3. Edit proxies.txt            → add proxy list (host:port:user:pass per line)
4. Double-click start-proxy.bat → starts proxy on http://localhost:31000
5. Double-click run-batch.bat  → auto-login all accounts
```

Done. Open `http://localhost:31000` in your browser to see the dashboard.

## Quick Start (Manual / Any OS)

```bash
# 1. Install dependencies + CloakBrowser binary
pip install -r requirements.txt
python -m cloakbrowser install
python -m playwright install-deps chromium

# 2. Copy account template
cp accounts.txt.example accounts.txt
# Edit accounts.txt — add email:password per line

# 3. Copy proxy template
cp proxies.txt.example proxies.txt
# Edit proxies.txt — add host:port:user:pass per line

# 4. Start proxy (also starts OAuth callback server on port 18432)
python proxy.py

# 5. Auto-login accounts (Google OAuth automation)
python autoclaw_autologin.py --batch accounts.txt --interactive
```

## Usage

### Auto-Login (Batch)

```bash
# Interactive — asks headless/concurrent, shows summary
python autoclaw_autologin.py --batch accounts.txt --interactive

# Headless batch with 3 concurrent
python autoclaw_autologin.py --batch accounts.txt --headless --concurrent 3

# Test single account (no save)
python autoclaw_autologin.py --test email@gmail.com:password

# Force re-login
python autoclaw_autologin.py --batch accounts.txt --force
```

Account format in accounts.txt: `email:password` (one per line, # for comments)

### Rotating Proxy (Required for Batch)

Z.ai enforces IP rate limit (630014) — ~2 account registrations per IP before cooldown. Without rotating proxy, batch register will fail after 2 accounts.

**Setup:**

1. Get proxies from [webshare.io](https://webshare.io) or your provider
2. Add to `proxies.txt` — one proxy per line:

```
host:port:username:password
45.39.75.38:5952:user123:pass456
82.21.231.11:7325:user123:pass456
...
```

3. Run batch — each account automatically uses the next proxy (round-robin)

**Rules of thumb:**
- 1 proxy per account = best result (zero rate limit)
- Fewer proxies than accounts = some IPs repeat (may hit rate limit on 3rd+ use)
- No proxies.txt = batch blocked (use `--interactive` to override)
- `proxies.txt` is gitignored — safe to add real credentials

### Interactive Login

```bash
# Opens callback server on port 18432
python login.py

# Manual — paste callback URL
python login.py --manual

# List accounts
python login.py --list

# Force refresh all tokens
python login.py --refresh

# Check profile + wallet
python login.py --check
```

### Proxy API

Proxy runs on `http://localhost:31000`. OpenAI-compatible:

```bash
curl http://localhost:31000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "glm-5.2",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Models

| Model alias | Upstream | Note |
|-------------|----------|------|
| `glm-5.2` | `openrouter_glm-5.2` | **Best** — real GLM-5.2 |
| `glm-5.2-true` | `openrouter_glm-5.2` | Same as above |
| `glm-5-turbo` | `zai_glm-5-turbo` | **Cheapest** (-1pt/call) |
| `cheap` | `zai_glm-5-turbo` | Same as above |
| `auto` | `zai_auto` | **Avoid** — secretly DeepSeek ~7x cost |
| `deepseek` | `zai_auto` | Same as above |

### Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/v1/chat/completions` | POST | OpenAI chat completions |
| `/v1/models` | GET | List models |
| `/health` | GET | Health check |
| `/accounts` | GET | List stored accounts |
| `/refresh-all` | POST | Force refresh all tokens |
| `/wallet` | GET | Check wallet balance |
| `/ledger` | GET | Check billing ledger |
| `/api/login-url` | POST | Get OAuth URL for browser automation |
| `/auth/callback-google` | GET | OAuth callback handler (auto-captures code) |
| `/api/login-status` | GET | Check if OAuth login completed |
| `/api/accounts-detail` | GET | Accounts with wallet + token expiry |
| `/api/wallet/<email>` | GET | Wallet balance for single account |
| `/api/refresh/<email>` | POST | Refresh token for single account |
| `/api/delete/<email>` | DELETE | Remove account |

## Token Management

- Access token TTL: 24h (auto-refresh 5min before expiry)
- Refresh token TTL: ~30 days
- Auto round-robin across multiple accounts
- Tokens stored in `tokens.json` (gitignored)

## CloakBrowser Notes

- Binary auto-downloads on first run (~535MB, cached at `~/.cloakbrowser/`)
- Free tier: Chromium 146 (58 patches, unlimited sessions)
- Pro tier: Chromium 148 (59 patches, latest anti-bot patches)
- No `playwright install chromium` needed — CloakBrowser has its own binary
- System deps still needed: `python -m playwright install-deps chromium`
- Stealth is automatic — no JS injection, no config, no flags needed

## Files

```
autoclaw-autologin/
├── config.py              # Constants, endpoints, model map, loads proxies.txt
├── auth.py                # Token management, refresh, OAuth with rotating proxy
├── proxy.py               # Flask proxy server + OAuth callback + Dashboard API
├── login.py               # Interactive OAuth login helper
├── autoclaw_autologin.py  # Batch auto-login (CloakBrowser + proxy rotation)
├── tokens.json            # Token storage (auto-generated, gitignored)
├── accounts.txt           # email:password list (gitignored)
├── accounts.txt.example   # Template for accounts.txt
├── proxies.txt            # Proxy list: host:port:user:pass (gitignored)
├── proxies.txt.example    # Template for proxies.txt
├── requirements.txt       # Python deps (cloakbrowser, flask, requests, aiohttp)
├── setup.bat              # One-click setup (checks Python, installs deps + binary)
├── start-proxy.bat        # Start proxy server
├── run-batch.bat          # Batch login (interactive)
├── run-test.bat           # Test single account
├── run.bat                # Quick proxy launcher
├── autoclaw-login.bat     # Login shortcut
├── ui/                    # Dashboard UI
│   └── index.html
└── README.md
```

## License

MIT
