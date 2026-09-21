#!/usr/bin/env python3
"""AutoClaw v2.6.1 — LIVE smoke test: Anthropic /v1/messages × real upstream.

Sibling of smoke_test_dsml_live.py: drives the REAL proxy process against
the REAL upstream edge (autoglm-api.autoglm.ai) over the live network.

  STAGE 1  Real proxy boot (import mode) + /health + /v1/models surface
  STAGE 2  Token import over the live import API:
             --token-file F  import a REAL credential (autoclaw2api /
                             desktop-ls / tokens-json fragment — auto-detected)
             (no flag)       synthesize a structurally-valid probe JWT
                             (jti=email, exp=+24h) so the full pipeline is
                             exercised; upstream will 401 it, which is itself
                             the live error-path verdict
  STAGE 3  /v1/messages battery (live):
             auth gate · count_tokens · non-stream GLM alias · non-stream
             claude-* alias (credit-tier routing) · negative-cache replay ·
             cache clear via dashboard control · Anthropic SSE stream ·
             in-chat !router status · Prometheus counters
  STAGE 4  Verdict summary + JSON report.

The upstream verdict is reported verbatim. With a valid imported token a
200/SSE success path is asserted; with the probe token the 401 auth_failed
classification + negative-cache + error translation paths are asserted.
Either way the smoke test proves the wire format reaches the real edge and
every compat layer behaves on the live response.

Usage:
  python3 scripts/smoke_test_messages_live.py [--port 31998]
          [--token-file PATH] [--email EMAIL] [--skip-boot]

Exit code: 0 = all executed stages passed; 1 = any failure.
"""

import argparse
import base64
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request

import requests

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BASE = None      # set in main()
LOG_PATH = None  # proxy log file, grepped for routing evidence
RESULTS = []


def stage(name, ok, detail):
    RESULTS.append((name, bool(ok), detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {detail}", flush=True)
    return ok


def http(method, path, body=None, timeout=60, headers=None, api_key=None):
    h = dict(headers or {})
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, data=data, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return None, str(e).encode()


def jbody(raw):
    try:
        return json.loads(raw.decode("utf-8", errors="replace"))
    except Exception:
        return {}


def log_contains(substr, since_offset=0):
    try:
        with open(LOG_PATH, encoding="utf-8", errors="replace") as f:
            f.seek(since_offset)
            return substr in f.read()
    except Exception:
        return False


# ── STAGE 2 helpers ──────────────────────────────────────────────────────

def synth_jwt(email, ttl_s=86400):
    """Structurally-valid probe JWT (HS256-shaped, unsigned signature).

    The import pipeline validates STRUCTURE only (exp in the future, jti
    carries the email); signature verification is upstream's job — a probe
    token therefore tests the whole pipeline and yields the real upstream
    auth verdict instead of a local rejection.
    """
    now = int(time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    payload = {"jti": email, "sub": email.split("@")[0], "iat": now,
               "exp": now + ttl_s, "iss": "autoclaw-smoke"}
    b64 = lambda o: base64.urlsafe_b64encode(
        json.dumps(o).encode()).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(b"smoke-probe-signature").decode().rstrip("=")
    return f"{b64(header)}.{b64(payload)}.{sig}"


def load_token_records(args):
    """Return (records, provenance). Real credential file wins."""
    if args.token_file:
        with open(args.token_file, encoding="utf-8") as f:
            return json.load(f), f"real credential file: {args.token_file}"
    email = args.email
    rec = {
        "email": email,
        "access_token": synth_jwt(email),
        "refresh_token": "",            # absent on purpose — import must
                                        # warn but still accept
        "user_id": "smoke-probe",
        "device_id": "smoke-probe-device-0000-0000",
        "source_id": "autoclaw2api",
    }
    return rec, "synthesized probe JWT (structural; upstream will 401 it)"


# ── STAGE 3 helpers ──────────────────────────────────────────────────────

def anthropic_body(model, text, stream=False):
    return {
        "model": model,
        "max_tokens": 128,
        "stream": stream,
        "messages": [{"role": "user", "content": text}],
    }


def classify_verdict(status, body):
    """Human-readable upstream verdict for the report."""
    if status == 200:
        return "success (upstream accepted the request)"
    err = jbody(body)
    msg = ""
    if isinstance(err.get("error"), dict):
        msg = str(err["error"].get("message", ""))
    return f"upstream status {status}: {msg[:160]}"


def upstream_reached(status, body, is_probe_token):
    """Stage pass criterion for live upstream verdicts.

    probe token  — pass = 200 OR a clean edge/auth verdict (401/402/403
                   JSON). The WAF 405 HTML block page is a FAIL: it means
                   the wire never reached the auth middleware.
    real token   — pass = 200 only (the credential is expected to work).
    """
    if is_probe_token:
        if status in (401, 402, 403):
            raw = body.decode("utf-8", errors="replace")
            # our own no-token 401 is NOT an upstream verdict
            if "No valid tokens" in raw:
                return False
            return "text/html" not in raw[:200]
        return status == 200
    return status == 200


def clear_negative_cache(api_key):
    code, raw = http("POST", "/api/dashboard/control",
                     {"action": "clear_negative_cache"},
                     timeout=15, api_key=api_key)
    return code == 200 and jbody(raw).get("ok") is True


# ── Proxy lifecycle ──────────────────────────────────────────────────────

def boot_proxy(port, api_key):
    env = dict(os.environ)
    env.update({
        "AUTOCLAW_PROXY_PORT": str(port),
        "AUTOCLAW_PROXY_API_KEY": api_key,
        "ACLAW_NO_BROWSER": "1",
        "ACLAW_OAUTH_MODE": "import",
        "OWL_PROXY_ENABLED": "0",          # direct egress — clean verdicts
        "AUTOCLAW_TLS_VERIFY": "false",
        "PYTHONUNBUFFERED": "1",
    })
    global LOG_PATH
    LOG_PATH = os.path.join(REPO, "scripts", "smoke_messages_proxy.log")
    logf = open(LOG_PATH, "w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(REPO, "proxy.py")],
        cwd=REPO, env=env, stdout=logf, stderr=subprocess.STDOUT,
    )
    return proc, logf


def wait_health(timeout=90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        code, raw = http("GET", "/health", timeout=5)
        if code == 200:
            return jbody(raw)
        time.sleep(1.0)
    return None


# ── Main ─────────────────────────────────────────────────────────────────

def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=31998)
    ap.add_argument("--token-file", default=None,
                    help="real credential file (autoclaw2api / desktop-ls / "
                         "tokens-json fragment); auto-format-detected")
    ap.add_argument("--email", default="smoke-probe@autoclaw.local")
    ap.add_argument("--skip-boot", action="store_true",
                    help="drive an already-running proxy instead of booting")
    args = ap.parse_args()

    api_key = os.environ.get("SMOKE_API_KEY", "sk-smoke-messages-live")
    BASE = f"http://127.0.0.1:{args.port}"

    print("=" * 72)
    print(" AutoClaw v2.6.1 — /v1/messages LIVE smoke test (real upstream)")
    print("=" * 72)

    proc = logf = None
    if not args.skip_boot:
        print("\n[STAGE 1] proxy boot (import mode, direct egress)")
        proc, logf = boot_proxy(args.port, api_key)
        health = wait_health()
        if not stage("boot + /health", health is not None,
                     f"pid={proc.pid} port={args.port}" if health
                     else f"proxy did not become healthy — see {LOG_PATH}"):
            terminate(proc, logf)
            return 1
        import_status = jbody(http("GET", "/api/tokens/import/status",
                                   timeout=10)[1])
        stage("import subsystem live",
              import_status.get("oauth_upstream") in ("unknown", "live", "405"),
              f"counters={ {k: import_status.get(k) for k in ('files_seen','records_seen','imported','rejected')} }")

    try:
        print("\n[STAGE 2] token import via live /api/tokens/import")
        records, provenance = load_token_records(args)
        print(f"  provenance: {provenance}")
        is_probe = "synthesized" in provenance
        code, raw = http("POST", "/api/tokens/import", records,
                         timeout=20, api_key=api_key)
        summary = jbody(raw)
        # re-runs dedupe to "updated" — either counter advancing means the
        # record landed in the encrypted store
        accepted = (summary.get("imported", 0) + summary.get("updated", 0)) >= 1
        ok = code in (200, 207) and accepted
        stage("import accepted", ok,
              f"HTTP {code}: imported={summary.get('imported')} "
              f"updated={summary.get('updated')} rejected={summary.get('rejected')} "
              f"warnings={len(summary.get('results', [{}])[0].get('warnings', [])) if summary.get('results') else 0}")

        # the stored account must be visible and pickable
        code, raw = http("GET", "/accounts", timeout=10, api_key=api_key)
        accounts = jbody(raw)
        n_acc = len(accounts.get("accounts", accounts)) if isinstance(accounts, dict) else 0
        stage("account listed", n_acc >= 1, f"{n_acc} account(s) in rotation")

        print("\n[STAGE 3] /v1/messages battery — real upstream verdicts")

        # 3.0 model surface: claude-* aliases exposed
        code, raw = http("GET", "/v1/models", timeout=10)
        ids = [m.get("id") for m in jbody(raw).get("data", [])]
        claude_ids = [i for i in ids if str(i).startswith("claude-")]
        stage("v1/models exposes claude-* aliases",
              len(claude_ids) == 3, f"claude aliases: {claude_ids}")

        # 3.1 auth gate — Anthropic shape, no key
        code, raw = http("POST", "/v1/messages", anthropic_body("glm-5-turbo", "hi"),
                         timeout=10)
        err = jbody(raw)
        gate_ok = (code == 401 and err.get("error", {}).get("type") == "authentication_error")
        stage("auth gate (x-api-key/Authorization required)", gate_ok,
              f"HTTP {code} type={err.get('error', {}).get('type')}")

        # 3.2 count_tokens — pure local path
        code, raw = http("POST", "/v1/messages/count_tokens",
                         {"model": "claude-haiku-latest",
                          "messages": [{"role": "user", "content": "count me"}]},
                         timeout=10, api_key=api_key)
        ct = jbody(raw)
        stage("count_tokens", code == 200 and isinstance(ct.get("input_tokens"), int),
              f"HTTP {code} input_tokens={ct.get('input_tokens')}")

        # 3.3 non-stream GLM alias — the REAL upstream verdict
        clear_negative_cache(api_key)
        t0 = time.time()
        code, raw = http("POST", "/v1/messages",
                         anthropic_body("glm-5-turbo", "Reply with exactly: OK"),
                         timeout=90, api_key=api_key)
        dt = time.time() - t0
        glm_ok = upstream_reached(code, raw, is_probe)
        resp = jbody(raw)
        if code == 200:
            detail = (f"HTTP 200 in {dt:.1f}s — stop_reason={resp.get('stop_reason')} "
                      f"blocks={len(resp.get('content', []))}")
        else:
            detail = classify_verdict(code, raw) + f" ({dt:.1f}s)"
        stage("non-stream glm-5-turbo (Anthropic wire in/out)", glm_ok, detail)

        # 3.4 claude-* alias — credit-tier routing on the live wire
        clear_negative_cache(api_key)
        log_mark = _log_offset()
        t0 = time.time()
        code, raw = http("POST", "/v1/messages",
                         anthropic_body("claude-haiku-latest", "Reply with exactly: OK"),
                         timeout=90, api_key=api_key)
        dt = time.time() - t0
        routed = log_contains("Claude alias routed", log_mark)
        alias_ok = upstream_reached(code, raw, is_probe)
        if code == 200:
            detail = (f"HTTP 200 in {dt:.1f}s — tier routing logged: {routed}")
        else:
            detail = classify_verdict(code, raw) + f" ({dt:.1f}s) · tier routing logged: {routed}"
        stage("non-stream claude-haiku-latest (credit-tier route)", alias_ok, detail)

        # 3.5 negative cache replay — behaviour depends on the first verdict:
        #   200                      → replay must also be 200 (nothing cached)
        #   clean upstream failure   → auth_failed/quota class → replay = 429
        #   unclassified (405/5xx)   → NOT cached → replay forwarded upstream
        first_was_clean_failure = (code != 200 and code in (401, 402, 403, 404)
                                   and upstream_reached(code, raw, is_probe))
        code2, raw2 = http("POST", "/v1/messages",
                           anthropic_body("glm-5-turbo", "again"),
                           timeout=30, api_key=api_key)
        if code == 200:
            neg_ok = code2 == 200
            detail = f"upstream healthy — replay → HTTP {code2} (no failure to cache)"
        elif first_was_clean_failure:
            neg_ok = code2 == 429
            detail = (f"upstream {code} classified cacheable → replay HTTP {code2} "
                      f"(429 = cached, no upstream replay)")
        else:
            neg_ok = code2 == code
            detail = f"upstream {code} unclassified (not cached) → replay HTTP {code2} (forwarded again)"
        stage("permanent-failure negative cache", neg_ok, detail)

        # 3.6 Anthropic SSE stream
        clear_negative_cache(api_key)
        t0 = time.time()
        r = requests.post(
            f"{BASE}/v1/messages",
            json=anthropic_body("claude-sonnet-latest", "Count 1 to 5", stream=True),
            headers={"Authorization": f"Bearer {api_key}"},
            stream=True, timeout=120, verify=False)
        events = []
        if r.status_code == 200:
            for line in r.iter_lines(decode_unicode=True):
                if line and line.startswith("event: "):
                    events.append(line[7:])
                if "message_stop" in events:
                    break
            seq_ok = (events[:1] == ["message_start"]
                      and "message_stop" in events)
            r.close()
            dt = time.time() - t0
            stage("stream claude-sonnet-latest (Anthropic SSE)", seq_ok,
                  f"events={events[:8]}{'…' if len(events) > 8 else ''} in {dt:.1f}s")
        else:
            body_raw = r.content
            r.close()
            dt = time.time() - t0
            stage("stream claude-sonnet-latest (Anthropic SSE)",
                  upstream_reached(r.status_code, body_raw, is_probe),
                  classify_verdict(r.status_code, body_raw) + f" ({dt:.1f}s)")

        # 3.7 in-chat !router status — tier table live
        code, raw = http("POST", "/v1/chat/completions",
                         {"model": "glm-5-turbo", "stream": False,
                          "messages": [{"role": "user", "content": "!router status"}]},
                         timeout=15, api_key=api_key)
        content = ""
        try:
            content = jbody(raw)["choices"][0]["message"]["content"]
        except Exception:
            pass
        router_ok = code == 200 and "tier" in content.lower()
        stage("in-chat !router status (tier table)",
              router_ok, f"HTTP {code}, mentions tier table: {'tier' in content.lower()}")

        # 3.8 telemetry: the custom metrics registry records /v1/messages
        # traffic (this repo's dashboard metrics — the Prometheus /metrics
        # endpoint lives in the owl-dns-synergy repo, not here).
        code, raw = http("GET", "/api/dashboard/state", timeout=10, api_key=api_key)
        snap = jbody(raw)
        models_seen = list((snap.get("models") or {}).keys())
        telem_ok = code == 200 and "claude-sonnet-latest" in models_seen \
            and "glm-5-turbo" in models_seen
        stage("telemetry records /v1/messages traffic", telem_ok,
              f"models counter: {models_seen}")

        # 3.9 dashboard state: tier status embedded for the picker.
        # 4s grace — the background refresher's first fetch fires at boot+3s
        # with the LIVE model-config URL, so this stage doubles as end-to-end
        # proof of remote tier extraction (source should flip to "remote").
        time.sleep(4)
        code, raw = http("GET", "/api/dashboard/state", timeout=10, api_key=api_key)
        snap = jbody(raw)
        tiers = (snap.get("credit_tiers") or {}).get("tiers") or {}
        tier_src = (snap.get("credit_tiers") or {}).get("source")
        stage("dashboard state exposes credit_tiers", code == 200 and bool(tiers),
              f"source={tier_src} tiers={tiers or 'MISSING'}")

    finally:
        if proc:
            terminate(proc, logf)

    print("\n" + "=" * 72)
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print(f" VERDICT: {passed}/{total} stages passed")
    for name, ok, detail in RESULTS:
        print(f"   [{'PASS' if ok else 'FAIL'}] {name}")
    print("=" * 72)

    report = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "base_url": BASE,
        "passed": passed, "total": total,
        "token_provenance": RESULTS and "see stage 2 detail",
        "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    out = os.path.join(REPO, "scripts", "smoke_messages_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f" report: {out}")
    print(f" proxy log: {LOG_PATH}")
    return 0 if passed == total else 1


_log_fp = None
def _log_offset():
    try:
        return os.path.getsize(LOG_PATH)
    except Exception:
        return 0


def terminate(proc, logf):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if logf:
        logf.close()


if __name__ == "__main__":
    sys.exit(main())
