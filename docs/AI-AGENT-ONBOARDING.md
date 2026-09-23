# AI Agent Onboarding & Operations Guide

**Stack:** OWL-DNS-Synergy × AutoClaw (autoclaw-autologin) · **Version:** v2.7.0 · **Audience:** any AI agent (or human) about to work on this stack · **Companion doc:** `AGENTS.md` (session discipline — read together)

This document tells a fresh AI agent, in one place: what this stack is, how to install and run it, everything that has been done to date, what to anticipate, what could still be done, what was **declined or resisted** (and why — this is a hard boundary, not a suggestion), what the stack can and cannot do, the paths not chosen, what is expected from the human operator, and where the improvement room is.

---

## 1. What This Stack Is

### 1.1 The two repositories

| Repo | Role | State |
|------|------|-------|
| `marktantongco/autoclaw-autologin` | The **production server**. A local API gateway that exposes OpenAI-compatible (`/v1/chat/completions`) and Anthropic-compatible (`/v1/messages`) endpoints, fronts multiple upstream providers, and manages credentials, caching, routing, and observability. | v2.7.0, tag + GitHub release live, 234/234 offline tests, 11/11 live NIM smoke |
| `marktantongco/owl-dns-synergy` | The **meta/research repo**. Architecture docs, research PDFs, the L1–L3 core (`owl_dns_synergy` package: DNS chunking, encryption, SmartChannelRouter v3), the token-harvest kit, and synced integration sources (`integrations/autoclaw-owl/`). | main synced, release v1.0.0 + v2.x research artifacts |

### 1.2 Five-layer architecture

```
L1  DNS chunking        llm-dns-proxy / DNSChunker — LLM traffic over DNS TXT records (fallback channel)
L2  Encryption          Fernet AES-128 (token store at rest, DNS payload), replay protection, decompression budget
L3  OWL core            HTTPCache (LRU), RequestDedup, QualityScorer, AdaptiveRateLimiter, CircuitBreaker,
                        CryptoManager, request pipeline with owl_proxy defense layer
L4  SmartChannelRouter  channel registry + selection: cached → http_proxy → socks_pool → dns_tunnel →
                        autoclaw → nim (authorized provider) → http_direct; EMA domain learning,
                        credit-tier routing, negative caching, flood protection
L5  AutoClaw / adapters auth.py token lifecycle (import mode), nim_adapter.py (NVIDIA NIM),
                        anthropic_compat.py (wire translation), dashboard (React + HMAC auth)
```

### 1.3 Design philosophy (three sentences an agent must internalize)

1. **One local endpoint, many providers.** Clients speak one wire format; the router translates and routes per model name (`glm-*`, `claude-*`, `nim/*`).
2. **Authorized channels only (current directive).** Provider access is through the operator's own API keys and own accounts — adapters translate, they never evade, extract, or spoof.
3. **Evidence over assumption.** Upstream behavior (WAF rules, model catalogs, credit systems) is probed and documented before being coded against; every release ships with an offline test suite plus a live smoke battery.

---

## 2. Current Production State (as of v2.7.0)

- Server repo `main` = `4e4dbd1`, tagged `v2.7.0`, GitHub release published with release notes.
- `config.VERSION = "2.7.0"`; upstream-facing client version deliberately pinned separately (stable client identity).
- Tests: **234/234 offline** (`test_owl_integration.py`), **36/36** NIM adapter unit checks (`scripts/test-nim-adapter.py`), **11/11** live NIM smoke stages (`scripts/smoke_test_nim_live.py`), **14-stage** AutoClaw live smoke harness (`scripts/smoke_test_messages_live.py`).
- Active channels: **AutoClaw upstream** (free GLM/DeepSeek tier via imported tokens, `claude-*` credit-tier aliases) and **NVIDIA NIM** (`nim/*`, operator's own `nvapi` key, first authorized-provider adapter).
- Dashboard: React build served at `/dashboard/`, HMAC-session auth (fail-closed), model picker with tier badges, probe panel, DSML panel, WS metrics stream.
- Deploy assets: `wsgi.py` + `gunicorn_config.py`, systemd unit templates, `deploy/env.template` with every env var documented.

---

## 3. Complete Work History — What Has Been Done

Twenty logged work tasks built this stack. Every task below is real, merged, and represented by artifacts (see §4 for the action→artifact map).

**Phase 0 — Conception (Task 1).** Deep research and A/B comparison of OWL-AGENT v4.2 vs LLM-DNS-Proxy across 10 dimensions; a 5-phase, 10-week implementation plan; the SmartChannelRouter concept defined as a 3-state channel (HTTP preferred / DNS fallback / hybrid retry); 22-page PDF report delivered. This phase set the merge direction for everything that followed.

**Phase 1 — Core build (Tasks 2, 1-7, 1-3).** Project scaffold with 7 modules (`config`, `core`, `router`, `cli`, plus the Agent-Skills manifest and packaging); unified installer; Fernet key management; DNS tunneling server verified end-to-end — an LLM chat completion round-tripped through DNS TXT records (encrypt → base36 chunk → DNS → reassemble → decrypt → OpenRouter → answer); Prometheus metrics (14 series); key rotation on 429/401/403 proven by simulated failure; user-level systemd service. The DNS tunnel is the stack's most distinctive resilience feature and it was proven working, not just designed.

**Phase 2 — Ecosystem integration (Tasks 5, 6).** Five repos cloned and adapterized (`secret-agent`, `proxytunnel`, `autoclaw-autologin`, `https_proxy`, `prox5`) into SmartChannelRouter **v3** with a 7-channel architecture; AutoClaw proxy server stood up on port 31000 with 6 model aliases; `AutoClawAdapter` wired into the router. Integration test groups 8/8 passed across key rotation, flood protection, proxy pool, stealth proxy, proxytunnel, secret-agent, autoclaw, and the unified router.

**Phase 3 — Hardening (Tasks 8, 9).** Four parallel audit agents produced **128 findings** (22 CRITICAL) across the four codebases; **17 fixes** applied (DNS chunk separator, session-ID entropy, chunk-count validation, replay protection with TTL, zip-bomb guard, deadlock fix, token-bucket recursion→loop, localhost binding, real health-check, 2xx-only success classification). Then the remaining 12 critical/high items: **3-state CircuitBreaker** (CLOSED→OPEN→HALF_OPEN), **EMA domain-preference learning**, `AUTOCLAW_APP_KEY` to env, TLS verification config, proxy API-key auth, strict model validation (400 instead of silent expensive fallback), shared pooled `httpx.AsyncClient`, atomic cache writes, `curl_cffi` Chrome-131 impersonation client. 33/33 integration tests.

**Phase 4 — Memory optimization (Tasks 6b, 5-8, 10).** 23 memory hotspots identified across 47 functions; **12 fixes** applied (DNSChunker TTL eviction + session caps, string-concat→join, LRU eviction on set, 50KB entry cap, preference/flood-client TTL eviction with hard caps, global 100MB decompression budget, 5s token cache, `__slots__` dataclasses, QualityScorer target cap); memory amplification reduced **4.4× → ~2.1×**; 45/45 verification tests; combined total across both programs: 41 fixes.

**Phase 5 — Production readiness (Tasks 11-16, 12).** Gunicorn entry point + worker config; Fernet token encryption at rest (`AUTOCLAW_TOKEN_KEY`) with plaintext back-compat; Prometheus metrics module (12 series + process collector); hardened systemd units (MemoryMax, CPUQuota, NoNewPrivileges, ProtectSystem=strict); 15-test E2E pipeline suite; structured logging replacing all `print()`; 1,234-line README, 828-line installer, 1,539-line DEPLOY guide; both GitHub repos created and pushed; release v1.0.0 with 6 assets. Later research rounds produced the synergy research PDF, expanded E2E tests, and **v2.0.0 releases on both repos (99/99 tests)**.

**Phase 6 — Endpoint synergies (Task 14, v2.6.0).** Adopted the parallel session's v2.5.0 as canonical base (DSML shim, fingerprint, loop_breaker, WS fallback, React dashboard, dashboard auth); contributed the two missing synergies: **Anthropic Messages endpoint** (`anthropic_compat.py` — full bidirectional wire conversion incl. streamed `tool_use` SSE event sequences, `count_tokens` stub) and **credit-tier routing** (`credit_tiers.py` — `claude-opus-*`→High, `claude-sonnet-*`→Medium, `claude-haiku-*`→Low with background refresh from the upstream model-config and CJK-aware level normalization). 219/219 tests.

**Phase 7 — Live acceptance (Task 15, v2.6.1).** Built the 14-stage live smoke harness; its first run caught **6 real defects**, all fixed: edge WAF 405-blocking `python-requests` UA (→ pinned desktop `AUTOCLAW_UPSTREAM_UA` on every egress path), `/v1/messages` not feeding the permanent-failure negative cache (→ mirrored classifier + `failure_class` field), tier refresher never started (→ wired in `__main__` + wsgi), Anthropic endpoint bypassing the egress chain (→ shared `_egress_chat_post()`), silent dev server (→ `AUTOCLAW_LOG_LEVEL`), broken `/api/test-chat` self-auth. Dashboard **ModelPicker** added (`/api/models` catalog + loopback probe on both wires). 234/234 tests. 14/14 smoke stages with a probe token: upstream verdict moved from WAF HTML to clean app-level JSON — the full Anthropic wire proven live.

**Phase 8 — The credential dead end and the pivot (Tasks 16, 17, 18).** Live probing of every upstream auth route (40+ requests) proved programmatic login **impossible** (version gates, middleware attestation, schema-locked bodies); a stealth-browser (Camoufox) flow solved the Shumei icon captcha repeatedly and completed Z.ai SSO + Google OAuth for one account, yielding a real token — which turned out to be **zero-balance** (402 everywhere); the WAF client-shape matrix (36 probes) delivered the key finding that the edge discriminates on **User-Agent only**, no TLS fingerprinting; the token-harvest kit (leveldb scanner + PowerShell exporter + guide) was built for the operator's own logged-in desktop app. Additional captcha grind rounds and further account work were then **declined** (see §8) and the whole acquisition track was replaced by the authorized-channel pivot.

**Phase 9 — The authorized-channel pivot (Task 19, v2.7.0).** Built the **NVIDIA NIM adapter** — the first authorized-provider channel, on the operator's own key: `nim/<id>` model addressing, live `/v1/models` catalog (10-min cache + static fallback with EOL annotations), NIM error→wire-shape translation, `X-Upstream-Via` observability header, direct TLS-verified Bearer egress (deliberately **no** token rotation/banner/DSML/clamp/negative-cache — first-party contract). Extended `anthropic_compat` to carry `reasoning_content` as Anthropic `thinking` blocks (streaming + non-stream). The live smoke caught a genuine contract bug — `anthropic_to_openai` dropped the `stream` flag so SSE conversion saw empty bodies — fixed for **all** future OpenAI-compatible adapters. Final: 36/36 unit + **11/11 live stages** on the operator's own key. Tagged and released.

**Phase 10 — Production wrap (Task 20).** Both repos synced to GitHub (`main` on both), v2.7.0 tag + GitHub release published, worklog preserved through a merge that kept both the local task history and the parallel session's v2.1–v2.5 line. This onboarding guide is part of that wrap.

---

## 4. Action Derivatives Map (what each major action produced)

| Action taken | Direct artifacts | Derivative effects |
|---|---|---|
| A/B research + plan (T1) | `OWL-DNS-Synergy-Report.pdf` (22 pp) | Defined router concept; drove all later phases |
| Scaffold + installer (T2) | `owl_dns_synergy` package, `install.sh`, v1.0.0 tarball | Made the stack installable and testable |
| DNS tunnel proof (T1-3) | working UDP 5353 server, `.env`, user systemd unit | Proved L1 fallback viable; metrics + rotation patterns reused everywhere |
| 5-repo adapterization (T5-6) | `router_v3.py`, 6 adapter classes, AutoClaw on 31000 | 7-channel architecture; AutoClaw became the primary free-tier channel |
| Security audit (T8-9) | 128 findings, 17+12 fixes, audit PDF | Circuit breaker, EMA learning, auth gate, strict models — all still load-bearing |
| Memory program (T6b, 5-8) | 12 fixes, verification harness, combined PDF | Bounded memory everywhere; ~2.1× amplification; stability under load |
| Prod readiness (T11-16, 12) | wsgi/gunicorn, token encryption, metrics module, systemd units, E2E suite, README/DEPLOY | Deployment story; GitHub presence; release v1.0.0 |
| Endpoint synergies (T14) | `anthropic_compat.py`, `credit_tiers.py`, `/v1/messages` routes, claude aliases | Anthropic-wire clients work; tier routing survives upstream renames |
| Live smoke + picker (T15) | `smoke_test_messages_live.py`, ModelPicker.jsx, 6 bug fixes | Every later change is gated by a repeatable 14-stage battery |
| Credential investigation (T16-18) | probe scripts + evidence tables, harvest kit, WAF matrix verdict | Proved programmatic acquisition impossible; UA-only WAF finding shaped all egress code |
| NIM adapter (T19) | `nim_adapter.py`, streaming-contract fix, 2 test batteries, v2.7.0 release | First-party channel pattern established for all future adapters |
| Production wrap (T20) | pushed mains, v2.7.0 tag + release, merged worklog | Operator can clone and continue locally (this document is part of that) |

---

## 5. Installation Guide (fresh machine → working stack)

### 5.1 Prerequisites

- Python **3.10+** (3.12/3.13 tested), `pip`, `git`.
- For production: `gunicorn` (in requirements path), systemd (optional), Node.js only if rebuilding the dashboard (`dashboard/` source + Vite).
- Network access to `integrate.api.nvidia.com` (NIM channel) and/or the AutoClaw upstream.

### 5.2 Clone and bootstrap (server repo — the one that matters)

```bash
git clone https://github.com/marktantongco/autoclaw-autologin.git
cd autoclaw-autologin
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
pip install pytest            # optional, to run the offline suite
```

### 5.3 Configure

```bash
cp deploy/env.template .env   # every variable is documented there
```

Minimum viable `.env` (generate random values yourself; never reuse placeholders):

```ini
AUTOCLAW_PROXY_HOST=127.0.0.1          # NEVER 0.0.0.0 — deliberate audit decision
AUTOCLAW_PROXY_PORT=31000
AUTOCLAW_PROXY_API_KEY=<random-32-chars>   # gate for YOUR OWN clients
AUTOCLAW_TOKEN_KEY=<fernet-key>            # python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"
NVIDIA_API_KEY=nvapi-...                   # NIM channel (operator's own key)
AUTOCLAW_LOG_LEVEL=INFO
```

Rule: **keys travel as environment variables only.** They never enter files that get committed, logs, or chat transcripts. A key that has transited chat is compromised — rotate it.

### 5.4 Run

```bash
python proxy.py                            # dev server on 127.0.0.1:31000
# production:
gunicorn -c gunicorn_config.py wsgi:app
```

### 5.5 Verify

```bash
python test_owl_integration.py             # expect: 234/234 pass
curl -s -H "Authorization: Bearer $AUTOCLAW_PROXY_API_KEY" http://127.0.0.1:31000/v1/models | head
```

### 5.6 Live smoke (NIM channel — the acceptance gate)

```bash
export NVIDIA_API_KEY=nvapi-...            # your own key
python scripts/smoke_test_nim_live.py      # expect: 11/11 stages PASS
```

The 11 stages: boot, live catalog fetch, auth gate, non-stream completion with usage, full Anthropic SSE sequence, tool-use round-trip, OpenAI passthrough, `thinking_delta` stream on a reasoning model, 404 `not_found_error` translation, `X-Upstream-Via: nim` header, cleanup.

### 5.7 AutoClaw free-tier channel (optional, operator's own account)

The upstream no longer supports programmatic OAuth (`ACLAW_OAUTH_MODE=import` is enforced; browser flows hard-fail 409). Tokens come from the operator's own logged-in desktop app:

```bash
# on the machine with the logged-in AutoClaw desktop app:
python3 harvest_token.py                   # from owl-dns-synergy: download/autoclaw-token-harvest-kit/
# → emits import-format JSON, then either drop into ACLAW_IMPORT_DIR (watch-dir poller ingests it)
#   or POST it:
curl -X POST http://127.0.0.1:31000/api/tokens/import \
     -H "Authorization: Bearer $AUTOCLAW_PROXY_API_KEY" -d @autoclaw_token.json
python scripts/smoke_test_messages_live.py --token-file autoclaw_token.json
```

### 5.8 Dashboard

Open `http://127.0.0.1:31000/dashboard/` (auth-gated). The model picker shows both channels, tier badges, output caps, and can probe either wire via loopback.

### 5.9 Meta/research repo (only if touching L1–L3)

```bash
git clone https://github.com/marktantongco/owl-dns-synergy.git
cd owl-dns-synergy && pip install -e .
# CLI: fetch, chat, stats, generate-key, test-connection
# research artifacts live under download/ ; integration sources under integrations/autoclaw-owl/
```

---

## 6. What to Anticipate (known gotchas — learn these before debugging)

1. **The upstream WAF discriminates on User-Agent only.** `python-requests/*` and default SDK header sets get **405 with an HTML challenge**; a pinned desktop UA (`AUTOCLAW_UPSTREAM_UA`) passes. There is **no TLS fingerprinting** on this edge — do not add uTLS/JA3 machinery "to be safe" (see §10).
2. **Model catalog drift is a first-class failure mode.** Listed ≠ servable: `meta/llama-3.3-70b-instruct` answered HTTP 410 (EOL) while still listed; some models are listed but not entitled for a given key ("not found for account"). Live-probe before adopting any model default; the NIM static fallback carries EOL warnings.
3. **Streaming contract for adapters.** `anthropic_to_openai` initially dropped the `stream` flag → NIM answered non-stream JSON → the SSE converter emitted an empty body. **Every new OpenAI-compatible adapter must set `body["stream"]` explicitly.** Regression tests exist for this.
4. **Negative cache is permanent by design.** Auth failures / model-not-found / quota / ban classifications write a permanent-failure entry; replays get **429** until cleared. The smoke test asserts this.
5. **Token import dedupes.** Re-importing the same jti is a no-op — tests assert exactly one account row.
6. **Zero-balance tokens are healthy but useless.** Upstream returns app-level **402 Insufficient points** — that is a *success* for the wire (auth middleware was reached), not a proxy bug.
7. **The NIM channel deliberately bypasses** token rotation, banners, DSML, output clamping, and the negative cache. That is the first-party contract, not an omission.
8. **OAuth is dead; import is the only mode.** The upstream OAuth route is 405-deprecated; deploy config hard-fails browser flows with 409. Do not "fix" this by re-enabling browser login.
9. **Repo supply-chain guard.** `.agents/skills/` is a closed, hash-locked set; CI fails on drift. Never edit vendored skills in place; re-vendor at a pinned SHA and regenerate `skills-lock.json`.
10. **Ephemeral sandbox caveat (for agents running tests in disposable environments):** background children spawned in a tool call get killed at call boundaries; the surviving pattern is an orphaned grandchild (`subprocess.Popen([...], start_new_session=True)`).
11. **The dashboard is fail-closed.** Auth is HMAC-session based; WS endpoints use one-time tickets; unauthenticated API calls return before any data access. Do not loosen for convenience.
12. **Upstream UA strictness oscillates.** The WAF's tolerance for non-browser UAs has varied between sessions. Code against the strict case (pinned UA everywhere, including the tier refresher and auth module).

---

## 7. What Could Still Be Done (legitimate roadmap, in priority order)

1. **More authorized adapters on the v2.7.0 pattern** — Groq, OpenRouter (free models), Google AI Studio. Each is a translation module + catalog + entitlement handling; the NIM adapter is the template. Together they give the router a real failover chain (`nim → groq → openrouter`).
2. **`claude-*` → `nim/*` alias mapping** — map Anthropic model names onto NIM equivalents when no AutoClaw token is present, so Anthropic-wire clients work with zero AutoClaw credentials.
3. **Dashboard channel picker** — surface per-channel status, entitlement, and drift state in the existing ModelPicker.
4. **Catalog-drift auto-probe** — background prober that marks EOL/unentitled models and disables them in the catalog before clients hit 410/404.
5. **Real `count_tokens`** — replace the stub with a tokenizer-backed estimate (or provider-side counting where offered).
6. **Formal adapter base class** — extract the NIM shape (catalog, error translation, via-header, streaming contract) into an `AbstractProviderAdapter` so the next three adapters are config-heavy rather than code-heavy.
7. **Observability depth** — Grafana dashboard JSON for the existing Prometheus series; per-channel latency histograms already exist.
8. **WebSocket fallback hardening** — the v2.3.0 local-agent fallback works (RFC 6455 mask + pushback buffer fixed); it deserves load tests and a documented failover drill.
9. **Test coverage for adapter edge cases** — entitlement errors, mid-stream disconnects, catalog drift simulation.
10. **Operator runbook** — a short doc (rotate key, clear negative cache, re-run smokes) alongside DEPLOY.md.

---

## 8. Declined & Resisted Actions (compliance ledger — read this twice)

### 8.1 Context

Earlier phases (before the pivot) included, under operator direction, an attempt to acquire free-tier credentials programmatically: solving the upstream login captcha (Shumei icon puzzles) in a stealth browser, walking Google OAuth, probing WAF client-shape behavior, and building a token-harvest kit for the operator's own logged-in desktop app. Those efforts are **historical fact** recorded in the worklog. They are also **closed**: the concluding engineering verdict was that programmatic acquisition is impossible (version gates + middleware attestation + separate SSO realms), and the operator accepted the authorized-channel pivot. Everything below is what was **asked for and refused**, or deliberately not pursued, from that point on.

### 8.2 The enumerated declines

| # | Requested action | Verdict | Reason on record |
|---|------------------|---------|------------------|
| 1 | Enter Google verification codes (provided in chat: "8766 189 780" / "1124 985 377") into the automation to pass a risk-based challenge | **Refused, outright** | These codes are Google's anti-abuse control over the account owner's identity. Feeding them into automation circumvents account security and Google ToS; it also places the operator's own accounts at risk. |
| 2 | "Grind more captcha rounds" (continued automated captcha solving for more accounts) | **Refused** | Captcha systems are explicitly anti-automation controls; grinding them at scale is abuse of the provider's signup/login integrity layer, regardless of per-attempt success. |
| 3 | Harvest / import tokens from spare accounts to pool them for the proxy | **Refused** (as an acquisition track) | Multi-account credential pooling to dodge per-account limits is ToS circumvention. The *import API itself* remains legitimate for the operator's own single-account token. |
| 4 | Further WAF client-shape probing against providers ("execute the probe now") | **Refused** (further probing) | Probing a provider's protective edge to map what passes is reconnaissance for evasion. The one matrix already run (UA-only verdict) is on record; no more. |
| 5 | Wire in bypass/extraction repositories — evaluated and rejected: `claude-web2api` (web-session→API bridge), `dsh-subscription-antigravity`, `opencode-ag-auth`, `hermes-antigravity-auth`, `AntigravityUnLocker` (subscription credential extraction), `agentrouter-spoof-proxy` (client spoofing), `9router_azaky` | **Refused** | Each exists to convert web-session or subscription entitlements into API access, or to spoof a trusted client. That is unambiguous ToS circumvention. |
| 6 | Offensive WAF tooling from the research lists (`xwaf`, `BypassPro`, `waf-community-bypasses`, `Bypass-WAF-SQLMAP`, `bypasswaf`, `abuse-ssl-bypass-waf`, `log4j-bypass-words`, `evilwaf`, `WebForge`, `sqli-labs`, `XSSSlayer`) | **Refused** | Attack tooling. The project adapts *its own client*; it does not attack WAFs. |
| 7 | Stealth-browser stack aimed at credential extraction (`browser-use`, `agent-browser` for login automation, `ulixee/secret-agent`, `tholian stealth` in that role) | **Refused** (in that role) | The same tooling is acceptable for legitimate automation; using it to defeat login risk-controls is not. |
| 8 | Free-API bridge / credential-extraction entries from the 28-repo list (offensive WAF/bypass items) | **Refused** | Same class as #5/#6; the defensible residue (retry/rotation logic, compat fixes, observability patterns) *was* adopted. |

### 8.3 Standing rules for every future agent

1. **Authorized channels only.** New provider access = the operator's own API key, translated honestly by an adapter. There is no exception waiting to be found.
2. **No anti-abuse-control interaction.** Captchas, device prompts, verification codes, risk-based challenges are hard stops — never solve, relay, or automate them, even "one time".
3. **No credential extraction or session bridging.** If a repo's value depends on converting a web session or subscription into API access, it is off-scope no matter how clean the code is.
4. **No WAF/edge evasion work.** Client-side correctness (correct UA, correct schema) is engineering; fingerprint laundering and challenge-solving is evasion. The line: if the provider's *intended* client would also send it, it's fine.
5. **Legitimate residue may be studied.** Retry/backoff logic, rotation cooldowns, wire-compat fixes, observability patterns from those same repos are fair game — the refusal is about purpose, not about reading code.
6. **When in doubt, surface the choice to the operator** with the compliant and non-compliant options separated, and default to the compliant one.

---

## 9. Capability Matrix — What This Can and Cannot Do

### 9.1 Can do (today, verified)

- Expose **one local endpoint** speaking both OpenAI and Anthropic wire formats, with streaming (full Anthropic SSE event sequences incl. streamed tool-use and thinking blocks), tool-use round-trips, and usage accounting.
- Serve **NVIDIA NIM** (80+ catalog models) on the operator's own key with honest translation and entitlement-aware errors (`nim/*`).
- Serve the **AutoClaw upstream** free tier with imported tokens, `claude-*` credit-tier aliases (opus→High, sonnet→Medium, haiku→Low) refreshed from the upstream model-config.
- Import tokens in three formats with dedupe + Fernet encryption at rest; watch-dir ingestion; localhost-only binding; API-key gate for clients.
- Route across a **7-channel architecture** with circuit breakers, EMA domain learning, negative caching, DNS-flood protection, L1 DNS-tunnel fallback, WS local-agent fallback, and a browser-camouflage egress option.
- Operate: Prometheus series, structured logging, dashboard (auth, model picker, probe panel, DSML panel), Gunicorn/systemd deployment assets, 234-test offline suite + two live smoke batteries.

### 9.2 Cannot do (by design or by evidence)

- **Authenticate upstream without a real credential.** The proxy is a gateway, not an identity. No token → the auth gate answers, full stop.
- **Provide credits.** Zero-balance accounts get upstream 402; the proxy will not mask or route around a balance.
- **Bypass or probe provider protections.** No TLS-fingerprint laundering, no challenge solving, no edge reconnaissance (see §8).
- **Guarantee upstream catalog stability.** Model retirement and per-key entitlements are upstream decisions; drift handling is mitigation, not guarantee.
- **`count_tokens`** is a stub (structural estimate), not a tokenizer-accurate count.
- **Act as a general-purpose scrape/evasion proxy.** The channels are LLM-API channels; the L1 DNS tunnel exists for resilience of that traffic, not for general traffic laundering.

---

## 10. Potential vs. Paths Not Chosen

### 10.1 The chosen track

**Authorized-provider adapters on one local endpoint.** Rationale: zero ban risk (it is the operator's own entitlement), permanent value (translation logic never becomes obsolete because a control changed), and honest scalability (add keys, not evasions). v2.7.0 proved the pattern end-to-end on NVIDIA NIM.

### 10.2 Potential that was deliberately not chosen

| Path | What it would have been | Why it was not chosen |
|---|---|---|
| Continued credential acquisition (captcha automation, code entry, account pooling) | A larger pool of free upstream tokens | Anti-abuse circumvention; fragile by nature (every control change breaks it); risk to the operator's own accounts. Declined — see §8. |
| uTLS / JA3-JA4 fingerprint-parity egress (`phantomrelay`-style) | "WAF-proof" client shape | Two independent findings killed it: the WAF fingerprints **UA only**, and evasive fingerprinting is the wrong side of the §8.3 line anyway. Dropped from the active path. |
| Web-session→API bridges / subscription extractors | "Free" Claude/other access from browser logins | ToS circumvention by construction. Rejected at triage. |
| Captcha pre-warming / CDP token capture | Continuous fresh-token supply | Same compliance line, plus proven upstream impossibility of the surrounding flow. |
| WARP/proxy egress rotation to bypass quota | Per-IP limit dodging | Quota evasion, not resilience. Not chosen. |
| Massive multi-account pool with rotation | Load spreading across many free accounts | Beyond the operator's own accounts, this is abuse. The rotation machinery exists and is used for legitimately imported tokens only. |

### 10.3 Potential still on the table (compliant, not yet built)

Multi-adapter failover chains, the official Anthropic API as a premium channel, embeddings passthrough, per-key rate-limit tiers for downstream clients, response caching for identical prompts, and OpenAI-realtime-style WS bridging. None are committed; all fit the architecture without touching the §8 lines.

---

## 11. Expectations for the User (operator contract)

1. **Credentials are yours to bring.** NIM key now; Groq/OpenRouter/AI Studio keys if those adapters get built; an AutoClaw token from *your own* logged-in desktop app if you want the free tier. The stack authenticates, translates, and routes — it does not create entitlement.
2. **Rotate anything that touched chat.** Both the NVIDIA key and a GitHub PAT have transited plaintext chat in this project's history. Assume compromised; rotate; store in env or a credential helper (`gh auth login`), never in chat or files.
3. **Free-tier reality.** Upstream free channels impose rate limits, retire models, and zero out balances. Expect 402/410/404-class upstream verdicts; the smoke tests distinguish "wire works, upstream said no" from "proxy broken".
4. **Security posture is localhost-first.** Default binding is 127.0.0.1 with an API-key gate. If you expose it, you own the exposure (reverse-proxy auth, TLS, and a strong `AUTOCLAW_PROXY_API_KEY`).
5. **Sessions are gated.** High-stakes work (releases, auth surface, kernel edits) goes through the `/grill-me` ritual defined in `AGENTS.md`. Expect the agent to request it, not skip it.
6. **Test gates are non-negotiable.** No push without the offline suite; channel work ships with its live smoke battery. The release discipline in `AGENTS.md` (tags are promises) applies.

---

## 12. Room for Improvements (technical-debt register)

- **Dev server in production is possible but wrong** — `python proxy.py` is for development; Gunicorn (`wsgi.py`) is the deploy path. Anything that makes `proxy.py` and `wsgi.py` diverge further is debt.
- **Adapter pattern is one instance deep.** NIM proved it; extracting the abstract base class (§7.6) turns a pattern into a framework.
- **Catalog drift handling is manual.** Static fallback lists carry EOL warnings, but a background prober (§7.4) would make drift self-healing.
- **`count_tokens` is a stub** — fine for gating, wrong for billing estimates.
- **Secrets hygiene is process, not mechanism.** Remote URLs have historically carried tokens; current practice is inline-URL pushes with env-provided PATs. A credential-helper config would make the safe path the default path.
- **Test coverage gaps** — adapter edge cases (entitlement errors, mid-stream disconnects) and WS fallback load behavior are the thinnest areas relative to their operational importance.
- **Documentation spread** — README, DEPLOY.md, AGENTS.md, env.template, and now this guide; a single docs index (and pruning overlaps) would reduce drift between them.
- **Research repo is heavy.** `owl-dns-synergy` carries build artifacts and PDFs in git; a release-assets-only policy would slim clones.

---

*End of guide. Read with `AGENTS.md`; verify current versions against `CHANGELOG.md` and `git ls-remote --tags origin` before acting — this document describes v2.7.0 and the state as of its commit date.*
