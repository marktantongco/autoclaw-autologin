# Changelog

## v2.6.0 — Anthropic Messages Endpoint + Claude Credit-Tier Routing (2026-09-21)

**Scope:** the two remaining research-synergy gaps from the 9-repo
ecosystem deep-dive (`AutoClaw-Synergy-Research-Deep-Dive.pdf`, synergies
6 + 7 of the Phase-2 roadmap) — the pieces v2.2-v2.5 did not cover. Both
are additive; no existing surface changed.

### Added

- **Anthropic Messages endpoint (Synergy 6)** — new `anthropic_compat.py`
  translates Anthropic `/v1/messages` wire format to the internal OpenAI
  chat pipeline and back, so Claude Code, OpenCode, and any
  Anthropic-SDK client can use AutoClaw natively (no adapter):
  - `POST /v1/messages` — stream + non-stream. Streaming emits the full
    Anthropic SSE sequence (`message_start`, `content_block_start`,
    `content_block_delta` text/input_json_delta, `content_block_stop`,
    `message_delta` with stop_reason, `message_stop`), including
    streamed `tool_use` blocks from OpenAI `tool_calls` deltas.
  - Request conversion covers system (string + block list), text
    blocks, base64 image blocks -> `image_url` data-URLs,
    `tool_use` -> `tool_calls`, `tool_result` -> standalone `tool`
    role messages, `thinking` blocks skipped (OmniClaw parity).
  - Response conversion maps finish_reason -> stop_reason
    (stop/length/tool_calls -> end_turn/max_tokens/tool_use) and
    usage -> input/output tokens.
  - `POST /v1/messages/count_tokens` — stub with a ~4 chars/token
    heuristic (both upstream proxies stub this endpoint too).
  - Reuses the full security pipeline: API-key gate (Bearer OR
    `x-api-key`), system-banner injection, output-cap clamping,
    permanent-failure negative cache (`negative_cache` metrics block),
    structured logging; metrics recorded by the existing after_request
    hook (g._model / g._wants_stream set for correct tags).
- **Claude credit-tier routing (Synergy 7)** — new `credit_tiers.py`:
  `claude-opus-*` -> High tier (`openrouter_glm-5.2`),
  `claude-sonnet-*` -> Medium (`zai_auto`), `claude-haiku-*` -> Low
  (`zai_glm-5-turbo`).
  - Tier targets refresh in the background from the remote
    model-config endpoint (5 min interval, heuristic degradation on
    failure — the service never blocks on the refresh).
  - Wired into both `/v1/chat/completions` and `/v1/messages` model
    resolution (before MODEL_MAP lookup; strict validation still
    rejects truly unknown non-Claude models).
  - `/v1/models` now advertises `claude-opus-latest`,
    `claude-sonnet-latest`, `claude-haiku-latest`.
  - `!router tiers` forces a synchronous refresh; `!router status`
    shows the tier table + source.
- **21 new tests** (converter coverage, stream event sequence, tool_use
  streaming, count_tokens, endpoint error shapes, alias routing,
  tier extraction, `!router tiers`) — suite now **219/219**.

### Notes

- The credit-tier refresh thread starts at proxy import (daemon,
  idempotent — safe under Gunicorn preload). In offline/test
  environments it degrades to the heuristic table silently.
- Env knobs: `AUTOCLAW_MODEL_CONFIG_URL`, `AUTOCLAW_TIER_REFRESH_S`
  (see `deploy/env.template`).

## v2.5.0 — Dashboard Auth Surface (SPEC-8b7) (2026-09-20)

**Scope:** the top remaining 8-b backlog item — the dashboard/telemetry
authentication surface, implemented per the research-graded SPEC-8b7
(`release-assets/spec-8b7-dashboard-auth-v250.md`). Also carries the deploy
hardening commit `4d7de65` (production `ACLAW_OAUTH_MODE=import`).

### Added

- **Fail-closed posture matrix (D2)** — with `AUTOCLAW_PROXY_API_KEY`
  configured, `/api/dashboard/state`, `/api/dashboard/control` and the WS
  stream require Bearer parity or a session cookie; WITHOUT a key, control
  answers **503 `auth_unconfigured` (never open again)** and reads are
  loopback-only — fixing 8-b finding #2 (control used to be open when no
  key was set) and #1/#3 (unauthenticated state/WS read APIs).
- **Stateless session bootstrap (D1)** — `POST /api/dashboard/auth`
  exchanges `Bearer $AUTOCLAW_PROXY_API_KEY` once for an
  `HttpOnly; SameSite=Strict; Path=/` cookie (`aclaw_dash_session`,
  8h TTL). Value = `exp.nonce.HMAC-SHA256(key)` with the signing key
  derived from the API key — **stateless, multi-worker-safe** (no shared
  session store; constant-time verification; key rotation invalidates all
  sessions). `DELETE /api/dashboard/auth` signs out.
- **CSRF defense-in-depth (D1a)** — cookie-authenticated mutating requests
  are Origin/Host-checked; cross-origin POSTs rejected 403.
- **WS one-time tickets (D3)** — `GET /api/dashboard/ticket` mints a
  single-use 60s ticket consumed via `?ticket=` on the WS handshake
  (browsers cannot set WS headers); unauthenticated handshakes get an
  error frame + close code **4001**.
- **Brute-force backoff + audit (D5)** — ≥5 failed auth attempts / 60s /
  source → 429; failed dashboard auth is recorded into the metrics
  registry as `blocks.auth_failed` (audit signal, not noise).
- **Log redaction (D5)** — werkzeug access-log filter rewrites
  `ticket=…` to `ticket=[redacted]`; one-time tickets never persist.
- **UI login card + auth state machine (D4)** — 401 flips the React
  dashboard to a login card (key typed once, never stored — no
  localStorage); all fetches send `credentials: same-origin`; 401s are
  handled (never swallowed again — fixes 8-b finding #4); sign-out
  button; fail-closed banner explains the disabled control surface when
  the proxy runs keyless.
- **`ACLAW_DASHBOARD_PUBLIC=1` escape hatch (D2)** — public
  *unauthenticated READS* for exotic setups; surfaced as a warning flag in
  `/health` (`dashboard_auth.public_read`) and a UI banner; control stays
  503 regardless.
- `/health` gains the `dashboard_auth` posture block; `health_mini` gains
  `dashboard_auth_configured` / `dashboard_public` for the UI.

### Changed

- Dashboard bundle rebuilt (vite, 51 KB gz) with the auth state machine.
- 4 existing keyless control tests re-anchored to the authenticated
  posture; **17 new auth tests** (posture matrix, session crypto incl.
  expiry/forge/key-rotation binding, CSRF, ticket single-use/expiry, WS
  handshake matrix, redaction, backoff) — suite now **198/198**.

## v2.4.0 — Phase 3.1: Synergy #7 Import Mode + DSML Real-World Metrics + Dashboard Tuning (2026-09-20)

**Scope:** the deferred Synergy #7 (no-CloakBrowser mode, promoted after a
live probe confirmed the upstream OAuth route is 405-deprecated for ALL
forks), the DSML shim's real-world observability surface, and the
highest-value dashboard-tuning backlog items from the parallel deep-research
(tasks 8-a/8-b/8-c).

### Added

- **`token_import.py` (Synergy 7 — server-first no-CloakBrowser mode)** —
  the token-supply lifeline: import desktop-extracted credentials via
  `POST /api/tokens/import` (single record / batch list / tokens.json
  fragment), a watch dir (`ACLAW_IMPORT_DIR`, ingested at boot + on
  demand), auto-detected formats (`autoclaw2api`, `desktop-ls`,
  `tokens-json`), JWT jti-email extraction + expiry checks, desktop
  `device_id` preservation (mint-with-warning when absent), dedupe by
  email or refresh_token, encrypted-store merge via `auth.add_token`
  (Fernet-at-rest inherited). Fail-safe: per-record results, never raises
  into the request path.
  - `/api/login-url` gated by `ACLAW_OAUTH_MODE`
    (`auto|browser|import|none`) + `ACLAW_NO_BROWSER`; upstream 405 now
    returns `upstream_oauth_deprecated` with an actionable import hint
  - `GET /api/tokens/import/status` + optional `ACLAW_APPLOGIN_PROBE`
    (report-only reachability check for the new upstream app-login routes)
  - `/health` gains the `token_import` block
- **DSML real-world metrics (8-c schema)** — grain-correct counters
  (`responses_stream/buffered`, `parse_attempts`, `calls_buffered`,
  `finish_overrides`, `tool_results_seen`), the **markup-leak taxonomy**
  (`truncated_flush`, `oversize_degrade`, `open_tag_timeout`,
  `unparseable_buffered` — the headline safety metric), shim overhead
  latency ring (avg/p95/window), computed rates with honest denominators
  (`parse_success_pct`, `leak_rate_pct`, `tool_loop_pct` +
  `tool_loop_approximate: true`); full superset in `/health` + dashboard
- **DSML dashboard panel** — parse success / calls / leaks / tool-loop
  stat cards, leak breakdown, overhead sparkline, per-model activations
  (`feature_models` cross-label in the metrics registry)
- **Metrics persistence** — telemetry survives restarts: versioned JSON
  state (`data/metrics_state.json`), atomic replace, dirty-flag flusher
  (30 s default) + atexit + SIGTERM hooks, corrupt-file quarantine,
  hourly rollups (72 h retention) feeding the new History panel
- **History panels** — hourly requests + block rate (zero chart deps)

### Fixed

- **Streaming latency was TTFB-only** — after_request recorded when the
  SSE Response object was created; the record now defers to stream close
  so avg/p95 reflect true chat duration (8-b W4)
- **`features.dsml_shim` was dead documentation** — the counter existed in
  the registry but no route ever set it; now wired at injection (8-b W2)
- `/health` no longer pollutes telemetry (external uptime monitors were
  recorded into the request log / latency window, 8-b W13)
- WS dashboard stream: keepalive ping per push cycle + concurrent-client
  cap (`ACLAW_DASH_WS_CAP`, default 20) — dead peers no longer hold worker
  threads (8-b W10); clients registry got an LRU cap too (W5)
- `/dashboard` (slash-less) served the shell with relative asset URLs →
  browsers fetched `/assets/*` → 404 → **blank white page**; now a 308
  canonicalizes to `/dashboard/` (found by live browser verification)
- Dashboard WS reconnect: exponential backoff (1 s→15 s) with reset on
  success + periodic WS retry while polling — a proxy restart no longer
  strands the page in REST-polling mode (8-b W8)

### Tests

- **149 → 180 offline tests** (+31: DSML metrics schema/leaks/rates,
  persistence roundtrip/corrupt/cap/rollups, telemetry hygiene, token
  import formats/dedupe/JWT/API/gating, version-bump guard)

### Notes

- Google SSO cannot be HTTP-ized (reCAPTCHA/consent/device checks) —
  harvesting stays browser-based, just elsewhere; #7 imports the result
- `tool_loop_pct` is a fleet-level approximation (role:"tool" messages
  also occur for native tool calls) — labelled `approximate` in payloads
- gunicorn: run `GUNICORN_WORKERS=1` (per-worker metrics aggregation is
  deferred; 8-b item 17)

## v2.3.0 — Phase 3: WS Fallback + thermoptic Egress + React Dashboard (2026-09-20)
