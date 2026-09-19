# Changelog

## v2.3.0 — Phase 3: WS Fallback + thermoptic Egress + React Dashboard (2026-09-20)

**Synergies implemented:** #13 cloud-to-local WebSocket fallback
(eequaled/GLM_proxy), #14 real-browser traffic camouflaging via
mandatoryprogrammer/thermoptic (supersedes the uTLS Chrome-120 ClientHello
item with a strictly stronger mechanism), #15 local React dashboard
(guell11/OmniClaw-GLM-Proxy) — the Priority-3 tier of the AutoClaw
Ecosystem Synergy Research Deep-Dive, per the Phase-3 roadmap.

### Added

- **`ws_fallback.py` (Synergy 13)** — cloud-to-local WebSocket transport:
  - Engages ONLY after every HTTP tier hits a network-level fault
    (connection refused / DNS / connect timeout); HTTP 4xx/5xx are
    upstream decisions and pass through verbatim
  - Wire contract `autoclaw-ws-agent-v1`: chat.request / chat.headers /
    chat.delta (raw SSE in `sse`) / chat.end / chat.error; SSE frames
    reassemble into a requests-compatible `iter_lines()` shim so the DSML
    sieve and streaming path work unchanged over WebSocket
  - Local-agent discovery: exact `ACLAW_WS_AGENT_URL` or 127.0.0.1 port
    scan (60 s positive / 10 s negative cache); network-only breaker
    (3 strikes → 60 s open); chat.error envelopes never trip the breaker
- **`thermoptic_bridge.py` (Synergy 14)** — real-browser JA4+ camouflage
  egress tier via mandatoryprogrammer/thermoptic (Docker/CDP out-of-process):
  - Health probe (gstatic 204 through the tunnel, 30 s positive cache),
    transport-only breaker, optional proxy auth + rootCA trust
  - Slots into the egress chain (owl → thermoptic → direct; order
    runtime-switchable); `X-Upstream-Via: thermoptic` observability
  - Launcher `scripts/thermoptic-up.sh` + `deploy/thermoptic-README.md`
- **`metrics.py` + React dashboard (Synergy 15)** — the OmniClaw operator
  surface, rebuilt as a Vite/React bundle served at `/dashboard`:
  - Thread-safe in-process registry: totals, per-status/per-transport/
    per-model counters, block counters (loop_breaker, negative_cache,
    auth_failed), feature activations (dsml_shim, ws_fallback,
    thermoptic), latency EMA + p95 + sparkline window, capped request
    log, SHA-256 masked-IP client list (raw IPs never stored)
  - `GET /api/dashboard/state` snapshot; long-running WebSocket push
    `/api/dashboard/stream` (flask-sock, 1 Hz) with REST fallback
  - `POST /api/dashboard/control` backend-switch control surface
    (owl-first | thermoptic-first | direct-only) + clear-negative-cache +
    reset-metrics; protected by `AUTOCLAW_PROXY_API_KEY` when set
- **Tiered egress chain in `proxy.py`** — owl → thermoptic → direct (or
  thermoptic-first), each tier falling to the next on failure, then the
  WS local-agent as last resort on network faults; metrics + masked
  client tagging via before/after-request hooks; `/health` gains
  `ws_fallback`, `thermoptic`, `metrics` blocks
- **`scripts/smoke_test_dsml_live.py`** — live smoke: boots the real
  proxy, probes real upstream egress (direct + OWL-raced), sends the
  banner+DSML envelope over the live wire, and (with tokens.json) runs
  full buffered + streaming tool-call round-trips checking
  `X-DSML-Shim: 1` and zero markup leakage

### Fixed

- Latent deadlock: `metrics.snapshot()` re-entered the non-reentrant
  registry lock via `backend_pref_info()` → `/health` hung forever
  (caught by the route test suite timeout)
- `WsStreamResponse.read_error()` now drains pending agent frames so
  late `chat.error` envelopes (arriving after `chat.headers`) surface in
  the proxy's error path instead of returning an empty body

### Tests

- 112 → **148 offline tests** (+36): metrics registry, backend-pref
  control, thermoptic probe/breaker/auth/verify, WS protocol shim +
  scripted local-agent (discovery, breaker, error envelopes), dashboard
  routes, and full egress-chain integration (tier ordering, WS-on-
  network-fault-only rule, direct-only escape hatch)
- Live smoke 12/12 stages (see `scripts/smoke_test_dsml_live.py`)

## v2.2.0 — Phase 2: DSML Tool-Calling Shim + Per-Chat Fingerprint + loop_breaker (2026-09-20)

**Synergies implemented:** #8 DSML tool-calling shim
(tt-52101/chat-z-ai-proxy-web2api-free) and #9 per-chat fingerprint
isolation + loop_breaker (eroslifestyle/ai-router-switch), per the
AutoClaw Ecosystem Synergy Research Deep-Dive roadmap (items 2.3, 2.4,
2.5).

### Added

- **`dsml_shim.py` (Synergy 8)** — synthesises OpenAI function-calling for
  the upstream, which lacks a native tool API:
  - Request side: when the client sends `tools`, a DSML (Domain-Specific
    Markup Language) protocol block is appended after the AutoClaw banner
    describing every tool (name, description, JSON-schema parameters) plus
    the exact `<dsml:tool_call>` markup the model must emit
  - Buffered response side: regex parse converts DSML blocks into real
    `message.tool_calls` (`finish_reason: "tool_calls"`, `X-DSML-Shim: 1`
    response header); prose outside blocks stays as `content`
  - Streaming response side: `DSMLStreamSieve` state machine forwards prose
    immediately, holds back possible tag starts, and converts completed
    blocks into OpenAI streaming `tool_calls` deltas (id/name delta +
    arguments delta) — DSML markup never leaks to clients
  - `tool_choice` honoured: `"none"` disables the shim, `"required"` /
    forced-function add mandate lines; native upstream tool_calls (when the
    upstream emits them) pass through untouched
  - Tolerant parsing: case-insensitive tags, optional ids (minted when
    absent), pretty-printed arguments repaired to single-line JSON,
    truncated blocks degrade to prose at end-of-stream
- **`chat_fingerprint.py` (Synergy 9a)** — per-chat fingerprint isolation:
  - SHA-256 of the FIRST user message (later turns never re-mint identity),
    or the `X-AutoClaw-Chat-Id` request header when the client wants
    explicit control
  - Pin-on-first-use to the serving account: follow-up turns of the same
    conversation reuse that account (affinity via
    `get_next_token(prefer_email=...)`); repin-on-drift when the pinned
    account turns unusable
  - TTL (24 h default) + LRU capacity (10 000 chats) + thread-safe
- **`loop_breaker.py` (Synergy 9b, depends on the fingerprint)** —
  stuck-conversation guard:
  - Counts re-emits of the SAME turn (last-user-message hash) at >=80%
    context fill; 4th re-emit returns HTTP 400 `loop_breaker_triggered`
    forcing a clean restart instead of runaway token spend
  - New turn resets the streak — legitimate long conversations are never
    killed; per-model context-window table with
    `AUTOCLAW_CONTEXT_WINDOW` global override
- **Observability:** `/health` now exposes `fingerprint`, `loop_breaker`
  and `dsml` stat blocks
- **Tests:** suite grows 61 -> 112 offline tests (fingerprint unit +
  thread-safety, loop_breaker trip/reset/window mapping, DSML protocol /
  parse / StreamSieve, Flask route integration incl. streaming DSML with
  markup-leak check, affinity, chat-id header, loop 400)

### Changed

- `get_next_token()` accepts `prefer_email` (pinned-account affinity;
  round-robin unchanged when no pin exists)
- Buffered non-stream aggregation initialises `usage` explicitly
  (replaces the `'usage' in dir()` idiom)
- Test fixture resets Phase-2 singletons and accepts kwargs in the token
  stub

### Env knobs

| Variable | Default | Purpose |
|---|---|---|
| `AUTOCLAW_DSML_ENABLED` | `1` | DSML tool-calling shim master switch |
| `AUTOCLAW_FINGERPRINT_ENABLED` | `1` | Per-chat fingerprint isolation |
| `AUTOCLAW_FINGERPRINT_TTL` | `86400` | Pin lifetime (s) |
| `AUTOCLAW_FINGERPRINT_MAX` | `10000` | LRU capacity |
| `AUTOCLAW_LOOP_BREAKER_ENABLED` | `1` | loop_breaker master switch |
| `AUTOCLAW_LOOP_REEMITS` | `4` | Re-emits before trip |
| `AUTOCLAW_LOOP_RATIO` | `0.8` | Context-fill threshold |
| `AUTOCLAW_LOOP_TTL` | `3600` | Streak memory (s) |
| `AUTOCLAW_CONTEXT_WINDOW` | *(per-model)* | Global context-window override |

## v2.1.0 — OWL-AGENT Proxy Defense Integration (2026-09-19)
 — OWL-AGENT Proxy Defense Integration (2026-09-19)

**Synergy implemented:** OWL-AGENT v5.3 proxy defense stack (user-provided
unified installer) integrated into AutoClaw as the upstream network-resilience
layer. Hybrid vendored/external backend, proxy-first routing with hedged
parallel racing, direct fallback always available.

### Added

- **`owl_proxy.py`** — vendored OWL-AGENT v5.3 core, adapted for AutoClaw:
  - `ProxyPoolManager`: GitHub/proxifly seeding → dedup → background
    validation → multi-dimensional scoring → health-monitored pool
  - Hedged parallel proxy racing (`HEDGE_FANOUT=3`, `PROXY_TIMEOUT=6s`) —
    first proxy to deliver headers carries the request (worst case +6 s vs
    90 s serial)
  - Single-strike idempotent proxy bans with backoff scaling
  - `AdaptiveRateLimiter` (token buckets per domain; 429 halves rate,
    success grows it) and per-domain `AsyncCircuitBreaker` on *network
    failures only* — HTTP 4xx/5xx never trip the breaker
  - **Streaming support** (`stream_request`): connection-establishment
    racing for SSE — the body streams through the winning proxy; upstream
    v5.3 was buffer-only
  - **Header-aware cache/dedup keys** (upstream keys omitted auth headers —
    two accounts' authenticated GETs would collide) and GET-only policy
    (POSTs like token refresh must never be cached or coalesced)
  - SOCKS4/5 via optional `aiohttp-socks`; optional `curl_cffi` TLS
    impersonation (`OWL_TLS_IMPERSONATE=chrome110`); all optional deps guarded
  - Env-tunable constants (12 `OWL_*` variables) + standalone CLI
    (`python owl_proxy.py stats|fetch|benchmark`)
- **`owl_bridge.py`** — sync adapter for the Flask/requests codebase:
  - One background asyncio loop in a daemon thread
    (`run_coroutine_threadsafe`); pool/caches persist across requests
  - **Hybrid backend loader**: external `~/.owl-agent/proxy_defense.py`
    wins over the vendored module (GLM_proxy cloud→local pattern); broken
    installs degrade gracefully to vendored
  - Fail-safe contract: init failure or per-request timeout raises
    `OwlUnavailable` → callers transparently fall back to direct `requests`
  - SSE bridge: async pump → bounded queue → requests-compatible
    `.iter_lines()` byte semantics
- **Routing**: all upstream calls proxied — chat completions SSE
  (`proxy.py`), token refresh ×2, profile, wallet, ledger (`auth.py`);
  Google-OAuth registration keeps `proxies.txt` round-robin when set
  (paid proxies outrank the free pool), OWL engages when none
- **Observability**: `X-Upstream-Via` response header; `GET /health`
  now reports `version` + `owl` stats block
- **Tests**: `test_owl_integration.py` — 61 offline tests covering the OWL
  core (ban lifecycle, header-aware cache, GET-only dedup, limiter,
  breaker, pool, hedged racing, stream pipeline), the bridge (hybrid
  loader, shims, line reassembly, fake-runtime roundtrips), Flask route
  integration (OWL passthrough, error translation/classification, direct
  fallback), and Phase-1 synergy regression (first committed tests)
- `deploy/env.template` gains the `OWL_*` block; README gains the OWL section

### Fixed

- **Duplicate SSE `[DONE]` terminator** — upstream `[DONE]` fell through the
  passthrough filter and the generator appended a second one; strict OpenAI
  clients could choke
- `_sign_headers` in `proxy.py` duplicated the auth helper from `auth.py`
  (kept for compatibility, now consistent)

### Changed

- `config.VERSION` 1.9.1 → 2.1.0 (aligned with the release tag)
- `requirements.txt`: `httpx[http2]` + `aiohttp-socks` added; `curl_cffi`
  optional

### Notes

- Default behavior is **proxy-first ON**. Set `OWL_PROXY_ENABLED=0` for the
  exact v2.0.0 direct-only behavior. With an empty/unhealthy pool the
  overhead is a single hedged race round (≤6 s) before direct fallback.
- `~/.owl-agent/` is shared with an external OWL-AGENT install (cache +
  proxy pool state).

## v2.0.0 — Synergy Enhancements from Ecosystem Research (2026-09-19)

- Synergy 1: Output-cap clamping (prevents silent DeepSeek 7× substitution)
- Synergy 2: System-banner injection (required upstream gate)
- Synergy 3: Permanent-failure negative cache (60 s TTL, thread-safe)
- Synergy 4: Chinese → English error translation (11-entry map)
- Synergy 5: In-chat `!router` command (synthetic intercept)
- Sources: eequaled/GLM_proxy, eroslifestyle/ai-router-switch
