# thermoptic egress tier — deployment guide (Synergy 14)

[thermoptic](https://github.com/mandatoryprogrammer/thermoptic) (ISC) is a
local HTTP proxy that replays your requests through a real Chrome/Chromium
instance over CDP, making TLS + HTTP fingerprints (JA3/JA4/JA4H and the
rest of the JA4+ family) indistinguishable from a genuine browser. For the
AutoClaw upstream it is the strongest anti-bot egress in the stack — a
real browser fingerprint rather than a forged ClientHello.

## Why a separate process?

thermoptic is a Node.js + Docker stack (mockttp MITM proxy + containerized
Chrome). The AutoClaw proxy deliberately does NOT vendor or embed it:
- it is heavy (a full Chrome runtime, ~2 GB RAM limit recommended)
- it must run out-of-process to present a REAL browser fingerprint —
  Python cannot forge Chrome's TLS/HTTP2 behavior
- absence degrades gracefully: `thermoptic_bridge.healthy()` fails fast
  and the egress chain falls to OWL free-pool racing, then direct.

## Quick start

```bash
bash scripts/thermoptic-up.sh          # clone + docker compose up + wait
```

Then enable the tier in the AutoClaw environment (or flip
`thermoptic-first` in the dashboard control surface):

```bash
export OWL_THERMOPTIC_ENABLED=1
export OWL_THERMOPTIC_URL=http://127.0.0.1:1234
export OWL_THERMOPTIC_CA=./thermoptic/ssl/rootCA.crt   # recommended
```

Verify camouflage independently:

```bash
curl --proxy http://127.0.0.1:1234 --insecure https://ja4db.com/id/ja4h/
```

The JA4H shown must match Chrome's, not curl's.

## Dockerless operation (your own Chrome)

thermoptic can attach to any Chrome launched with `--remote-debugging-port`
(9222/9223 style) instead of the containerized one — see the upstream
README ("You can connect thermoptic to any Chrome/Chromium instance…").
This is the recommended posture for maximum stealth (GPU-rendered, real
profile, non-datacenter IP).

## Auth

If you expose the proxy beyond localhost, set `PROXY_USERNAME` /
`PROXY_PASSWORD` in thermoptic's compose env and mirror them:
`OWL_THERMOPTIC_USERNAME` / `OWL_THERMOPTIC_PASSWORD`.

## TLS posture

thermoptic re-terminates TLS with its own CA (`ssl/rootCA.crt`, generated
on first run). Point `OWL_THERMOPTIC_CA` at that file so the bridge pins
exactly that CA; when unset, verification is disabled for thermoptic-routed
traffic (consistent with this project's default `verify=False`).

## Health & breaker

- Probe: GET `https://www.gstatic.com/generate_204` through the tunnel,
  positive result cached 30 s (`OWL_THERMOPTIC_PROBE_TTL`), negative 10 s.
- Breaker: 3 consecutive transport failures (`connect refused`, probe
  timeouts) degrade the tier for 60 s; upstream HTTP error statuses are
  never transport failures.
- `/health` shows the `thermoptic` block (enabled, healthy, breaker state,
  requests served, last error).
