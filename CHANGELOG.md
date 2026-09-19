# Changelog

## v2.1.0 — OWL-AGENT Proxy Defense Integration (2026-09-19)

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
