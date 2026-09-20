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

## 🧩 Phase 3.1 — Synergy #7 Import Mode, DSML Real-World Metrics, Dashboard Tuning (v2.4.0)

The upstream OAuth URL route is **405-deprecated** (live-probed; all forks
affected). Synergy #7 — the deferred no-CloakBrowser mode — is therefore the
token-supply lifeline: harvest tokens with the AutoClaw desktop app
(DevTools → Application → Local Storage → `access_token` / `refresh_token`),
then import them:

```bash
# paste directly
curl -X POST localhost:31000/api/tokens/import \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@x","access_token":"...","refresh_token":"...","device_id":"<keep-desktop-device-id>"}'
# or drop *.json files into .autoclaw_imports/ (ingested at boot)
```

Formats auto-detected (`autoclaw2api` credential files, raw desktop
localStorage dumps, `tokens.json` fragments); JWT jti-email extraction,
expiry checks, desktop `device_id` preservation, dedupe by email or
refresh_token, encrypted store merge. `/api/login-url` is gated by
`ACLAW_OAUTH_MODE` (`auto|browser|import|none`).

DSML real-world metrics now surface in `/health` **and** the dashboard:
parse success rate, call grain by path, the markup-leak taxonomy
(`truncated_flush` / `oversize_degrade` / `open_tag_timeout` /
`unparseable_buffered`), shim overhead latency, per-model activations and
the labelled-approximate tool-loop signal. Metrics **persist across
restarts** (`data/metrics_state.json`, hourly rollups, 72 h retention) and
the dashboard gained a DSML panel + hourly history strips.

**Fixed:** streaming latency was TTFB-only (now true stream duration);
`features.dsml_shim` was never wired; `/health` polluted telemetry; WS
stream dead peers held threads (keepalive + client cap now); slash-less
`/dashboard` rendered a blank white page (308 canonicalization).

**Tests: 149 → 180.**

## 🔐 Phase 3.2 — Dashboard Auth Surface (v2.5.0)

The operator dashboard and telemetry APIs are now **fail-closed** (SPEC-8b7).
Posture depends on whether `AUTOCLAW_PROXY_API_KEY` is configured:

| Posture | state/WS reads | control |
|---------|----------------|---------|
| key set | Bearer **or** session cookie | Bearer / session (Origin-checked) |
| no key, loopback peer | allowed | **503 `auth_unconfigured`** (never open) |
| no key, remote peer | 403 | 503 |
| `ACLAW_DASHBOARD_PUBLIC=1` | public (warned in `/health` + UI banner) | 503 |

- **Login:** open the dashboard with a key configured → login card → the key
  is exchanged ONCE (`POST /api/dashboard/auth`) for an HttpOnly
  `SameSite=Strict` cookie (8h). The key is never stored by the UI.
- **WebSocket:** browsers cannot set WS headers, so the UI mints a one-time
  60s ticket (`GET /api/dashboard/ticket`) and appends it to the handshake;
  unauthenticated handshakes are closed with code 4001. Tickets are
  redacted from access logs.
- **Hardening:** ≥5 failed auths / 60s / source → 429; failed dashboard
  auth lands in the metrics registry as `blocks.auth_failed` (visible on
  the Blocks panel); sessions are HMAC-signed with a key derived from
  `AUTOCLAW_PROXY_API_KEY` — stateless, multi-worker-safe, and rotation
  invalidates every session instantly.
- **Knobs:** `ACLAW_DASH_SESSION_TTL` (default 28800s),
  `ACLAW_DASHBOARD_PUBLIC` (default 0 — leave off unless you understand the
  exposure), `ACLAW_DASH_WS_CAP` (client cap, default 20).

## 🧩 Phase 3 — WS Fallback, thermoptic Egress, React Dashboard (v2.3.0)

Completes the Priority-3 tier of the AutoClaw Ecosystem Synergy Research
Deep-Dive: #13 cloud-to-local WebSocket fallback (eequaled/GLM_proxy),
#14 browser-grade traffic camouflaging via
[mandatoryprogrammer/thermoptic](https://github.com/mandatoryprogrammer/thermoptic)
(ISC) — a strictly stronger superset of the uTLS Chrome-120 ClientHello
item — and #15 the local React dashboard (guell11/OmniClaw-GLM-Proxy).

### Tiered egress chain

The chat route now walks transport tiers, order controlled at runtime from
the dashboard (no restart):

    owl-first (default)    owl free-pool racing → thermoptic → direct
    thermoptic-first       thermoptic → owl → direct
    direct-only            direct

After every tier hits a *network-level* fault (connection refused / DNS /
connect timeout — never HTTP 4xx/5xx), the cloud-to-local **WebSocket
fallback** transparently re-routes the request through a locally running
AutoClaw desktop agent (wire contract `autoclaw-ws-agent-v1`, see
`ws_fallback.py`), streaming SSE frames back through the same DSML sieve
path. A network-only breaker (3 strikes → 60 s cooldown) keeps a dead
agent from adding latency.

### thermoptic egress (browser camouflage)

[thermoptic](https://github.com/mandatoryprogrammer/thermoptic) is a local
HTTP proxy that replays requests through a real Chrome instance, making
JA3/JA4/JA4H fingerprints indistinguishable from a genuine browser. Run it
with `scripts/thermoptic-up.sh` (Docker), set `OWL_THERMOPTIC_ENABLED=1`,
and the proxy probes it, routes through it, and degrades gracefully when
it is down. See `deploy/thermoptic-README.md`.

### React dashboard (`/dashboard`)

A built React (Vite) bundle served from `ui/dashboard/` — live request
counters, latency sparkline, per-transport egress distribution, status
codes, block + feature counters, masked-IP client list, a capped request
log, and the backend-switch control surface. Data flows over a
long-running WebSocket (`/api/dashboard/stream`, flask-sock) with
automatic REST polling fallback (`/api/dashboard/state`). The classic UI
remains at `/`.

### Live smoke test

`scripts/smoke_test_dsml_live.py` boots the real proxy and drives the DSML
tool-calling shim against the real upstream edge (banner + DSML injection
over the live wire, OWL-raced attempt, i18n-translated upstream decision).
With `tokens.json` present it additionally runs full buffered + streaming
tool-call round-trips and asserts no DSML markup leakage.

### Phase-3 environment variables

| Variable | Default | Purpose |
|---|---|---|
| `ACLAW_WS_FALLBACK` | `0` | WS local-agent fallback master switch |
| `ACLAW_WS_AGENT_URL` | *(discovery)* | Exact agent ws:// URL (skips port scan) |
| `ACLAW_WS_DISCOVERY_PORTS` | `18789,18790,18791` | Local discovery ports |
| `ACLAW_WS_TIMEOUT` | `600` | WS stream cap (s) |
| `ACLAW_WS_BREAKER_THRESHOLD` | `3` | Network failures before breaker opens |
| `ACLAW_WS_BREAKER_COOLDOWN` | `60` | Breaker cooldown (s) |
| `OWL_THERMOPTIC_ENABLED` | `0` | thermoptic egress tier switch |
| `OWL_THERMOPTIC_URL` | `http://127.0.0.1:1234` | thermoptic proxy URL |
| `OWL_THERMOPTIC_CA` | *(verify off)* | thermoptic rootCA.crt (recommended) |
| `OWL_THERMOPTIC_USERNAME/PASSWORD` | *(none)* | thermoptic proxy auth |
| `OWL_THERMOPTIC_TIMEOUT` | `6` | Probe/connect cap (s) |
| `OWL_THERMOPTIC_PROBE_TTL` | `30` | Positive probe cache (s) |
| `ACLAW_METRICS_LOG_CAP` | `250` | Dashboard request-log size |

## License


MIT
