# Changelog

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
