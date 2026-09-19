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


## 🦉 OWL-AGENT Proxy Defense Layer (v2.1.0)

All upstream HTTP — chat SSE streams, token refresh, profile, wallet, ledger — is routed through an integrated **proxy-first defense stack** (OWL-AGENT v5.3, vendored as `owl_proxy.py` + `owl_bridge.py`):

1. **Seed** — 100 free proxies pulled from the proxifly CDN at boot, validated in the background against a 204 endpoint
2. **Race** — every upstream request races `OWL_HEDGE_FANOUT` (default 3) proxies in parallel for *connection establishment*; first to deliver headers carries the stream (worst case +6 s, usually sub-second)
3. **Ban** — single-strike idempotent bans with backoff scaling (60 s × fail_count, cap 10×); quality scores (latency/success/throughput EWMA) rank the pool
4. **Fallback** — direct connection always available: fires when the pool is empty, the race is lost, or OWL is disabled
5. **Guardrails** — per-domain adaptive rate limiting (429 → halve rate, success → grow), per-domain circuit breakers on *network* failures only (HTTP 4xx/5xx never trip them), GET-only header-aware HTTP cache + request deduplication

**Hybrid backend** (GLM_proxy cloud→local pattern): if an external OWL-AGENT install exists at `~/.owl-agent/proxy_defense.py`, it wins over the vendored module — vendored copy is the always-works fallback.

### OWL environment variables

| Variable | Default | Purpose |
|---|---|---|
| `OWL_PROXY_ENABLED` | `1` | Master switch (`0` = exact v2.0.0 behavior) |
| `OWL_BASE_DIR` | `~/.owl-agent` | State dir (proxy cache, HTTP cache) |
| `OWL_EXTERNAL_MODULE` | `~/.owl-agent/proxy_defense.py` | External backend path |
| `OWL_HEDGE_FANOUT` | `3` | Proxies raced per request |
| `OWL_PROXY_TIMEOUT` | `6` | Per-proxy connect cap (s) |
| `OWL_DIRECT_TIMEOUT` | `30` | Direct fallback connect cap (s) |
| `OWL_TLS_IMPERSONATE` | *(off)* | curl_cffi TLS fingerprint, e.g. `chrome110` (`pip install curl_cffi`) |
| `OWL_CACHE_TTL` | `0` | GET cache seconds (off by default — auth responses are account-specific) |
| `OWL_SEED_URL` / `OWL_SEED_COUNT` | proxifly / `100` | Proxy source list / seed size |
| `OWL_VALIDATE_URL` | gstatic 204 | Connectivity probe for validation |
| `OWL_STARTUP_TIMEOUT` | `15` | Seeding wait before first request |

### Observability

- Every chat response carries `X-Upstream-Via: direct` or `X-Upstream-Via: owl-proxy/<backend>`
- `GET /health` now includes an `owl` block: `{enabled, backend, proxies_total, proxies_healthy, ...}`
- Standalone CLI: `python owl_proxy.py stats | fetch <url> | benchmark`

> Google-OAuth **registration** calls keep their `proxies.txt` round-robin when configured — dedicated paid proxies outrank the free pool; OWL engages only when no explicit proxy is set.

## 🧩 Phase 2 — DSML Tool-Calling, Chat Fingerprint, loop_breaker (v2.2.0)

Three ecosystem synergies landed on top of the OWL layer (sources:
tt-52101/chat-z-ai-proxy-web2api-free and eroslifestyle/ai-router-switch,
per the AutoClaw Synergy Research Deep-Dive, items 8 & 9):

### DSML tool-calling shim (`dsml_shim.py`)

The upstream has no native OpenAI function-calling. When the client sends
`tools`, the shim describes them in a **DSML protocol block** appended to
the system prompt; the model's textual tool-use intent comes back as
`<dsml:tool_call>` blocks, which the shim parses into **real OpenAI
`tool_calls`** — buffered (`finish_reason: "tool_calls"`,
`X-DSML-Shim: 1` header) and streaming (`DSMLStreamSieve` emits standard
tool_calls deltas, markup never leaks to the client). `tool_choice` is
honoured (`"none"` bypasses the shim; `"required"`/forced-function add
mandate lines); native upstream tool calls pass through untouched.

### Per-chat fingerprint isolation (`chat_fingerprint.py`)

The fingerprint — SHA-256 of the **first user message**, or the
`X-AutoClaw-Chat-Id` header — is the unit of isolation, not the bearer
token. Each chat pins to the account that served its first request
(account affinity); other conversations keep rotating. The pin re-binds if
the pinned account turns unusable (repin-on-drift). Defeats
conversation-merge attacks and keeps billing attributable per conversation.

### loop_breaker (`loop_breaker.py`)

Detects stuck conversations: the SAME turn (last-user-message hash)
re-emitted **4+ times at ≥80% context fill** returns HTTP 400
`loop_breaker_triggered`, forcing the client to start a fresh conversation
instead of burning tokens on a context-rotted retry loop. A genuinely new
turn resets the streak — legitimate long conversations are never killed.

### Phase-2 environment variables

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCLAW_DSML_ENABLED` | `1` | DSML shim master switch |
| `AUTOCLAW_FINGERPRINT_ENABLED` | `1` | Fingerprint isolation |
| `AUTOCLAW_FINGERPRINT_TTL` | `86400` | Pin lifetime (s) |
| `AUTOCLAW_FINGERPRINT_MAX` | `10000` | LRU capacity |
| `AUTOCLAW_LOOP_BREAKER_ENABLED` | `1` | loop_breaker master switch |
| `AUTOCLAW_LOOP_REEMITS` | `4` | Re-emits before trip |
| `AUTOCLAW_LOOP_RATIO` | `0.8` | Context-fill threshold |
| `AUTOCLAW_LOOP_TTL` | `3600` | Streak memory (s) |
| `AUTOCLAW_CONTEXT_WINDOW` | *(per-model)* | Global context-window override |

## License


MIT
