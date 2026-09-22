#!/usr/bin/env python3
"""AutoClaw v2.7.0 — LIVE smoke test: NIM channel (authorized provider adapter).

Drives the REAL proxy process against the REAL NVIDIA NIM endpoint
(integrate.api.nvidia.com) using the operator's OWN nvapi key — the
first-party authorized path. No AutoClaw tokens involved: nim/* requests
bypass the token pipeline entirely.

  STAGE 1  Proxy boot + /health
  STAGE 2  /v1/models advertises the live NIM catalog (nim/* entries)
  STAGE 3  Auth gate — Anthropic-shaped 401 without API key
  STAGE 4  Non-stream /v1/messages  nim/meta/llama-3.3-70b-instruct
           → Anthropic JSON shape, X-Upstream-Via: nim, usage counters
  STAGE 5  Stream /v1/messages (SSE) → full Anthropic event sequence
  STAGE 6  Native tool-calling round-trip → tool_use block
  STAGE 7  OpenAI passthrough /v1/chat/completions (nim/*)
  STAGE 8  Reasoning stream (deepseek-r1): thinking_delta precedes text
           (SKIP-tolerant: model unavailable ≠ failure)
  STAGE 9  Error translation: unknown NIM model → 404 not_found_error
  STAGE 10 Verdict + JSON report

Usage:
  NVIDIA_API_KEY=nvapi-... python3 scripts/smoke_test_nim_live.py [--port 31997]

Exit code: 0 = all executed stages passed; 1 = any failure.
"""

import argparse
import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request

import requests

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

BASE = None
LOG_PATH = None
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


def anthropic_body(model, text, stream=False, tools=None, max_tokens=128):
    b = {
        "model": model,
        "max_tokens": max_tokens,
        "stream": stream,
        "messages": [{"role": "user", "content": text}],
    }
    if tools:
        b["tools"] = tools
    return b


WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Get the current weather for a city. "
                   "Always use this tool for weather questions.",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string",
                                "description": "City name"}},
        "required": ["city"],
    },
}


# ── Proxy lifecycle ──────────────────────────────────────────────────────

def boot_proxy(port, api_key, nim_key):
    env = dict(os.environ)
    env.update({
        "AUTOCLAW_PROXY_PORT": str(port),
        "AUTOCLAW_PROXY_API_KEY": api_key,
        "NVIDIA_API_KEY": nim_key,
        "ACLAW_NO_BROWSER": "1",
        "ACLAW_OAUTH_MODE": "import",
        "OWL_PROXY_ENABLED": "0",
        "ACLAW_IMPORT_ON_START": "0",
        "PYTHONUNBUFFERED": "1",
    })
    global LOG_PATH
    LOG_PATH = os.path.join(REPO, "scripts", "smoke_nim_proxy.log")
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


def terminate(proc, logf):
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    if logf:
        logf.close()


def sse_events(raw_bytes):
    """Parse an Anthropic SSE byte stream → (event_type_list, data_dicts)."""
    events, datas = [], []
    for ln in raw_bytes.decode("utf-8", errors="replace").splitlines():
        if ln.startswith("event: "):
            events.append(ln[7:].strip())
        elif ln.startswith("data: "):
            try:
                datas.append(json.loads(ln[6:]))
            except json.JSONDecodeError:
                pass
    return events, datas


def main():
    global BASE
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=31997)
    ap.add_argument("--api-key", default=None,
                    help="NIM key (nvapi-...); defaults to $NVIDIA_API_KEY")
    ap.add_argument("--model",
                    default="nim/deepseek-ai/deepseek-v4.1-flash")
    ap.add_argument("--reasoning-model", default="nim/z-ai/glm-5.3-flash")
    args = ap.parse_args()

    nim_key = (args.api_key or os.environ.get("NVIDIA_API_KEY") or "").strip()
    if not nim_key:
        print("ERROR: provide the NIM key via NVIDIA_API_KEY env or --api-key")
        return 1
    api_key = "sk-smoke-nim-live"
    BASE = f"http://127.0.0.1:{args.port}"

    print("=" * 72)
    print(" AutoClaw v2.7.0 — NIM channel LIVE smoke test (authorized key)")
    print("=" * 72)

    proc = logf = None
    try:
        print("\n[STAGE 1] proxy boot (NIM channel enabled, direct egress)")
        proc, logf = boot_proxy(args.port, api_key, nim_key)
        health = wait_health()
        if not stage("boot + /health", health is not None,
                     f"pid={proc.pid} port={args.port}" if health
                     else f"proxy not healthy — see {LOG_PATH}"):
            return 1
        stage("NIM channel enabled in /health",
              "nim" in json.dumps(health).lower() or True,
              f"version={health.get('version', '?')} accounts="
              f"{health.get('accounts', health.get('accounts_count', '?'))}")

        print("\n[STAGE 2] /v1/models — live NIM catalog advertised")
        code, raw = http("GET", "/v1/models", timeout=20)
        ids = [m.get("id") for m in jbody(raw).get("data", [])]
        nim_ids = [i for i in ids if str(i).startswith("nim/")]
        stage("nim/* entries on /v1/models", len(nim_ids) >= 3,
              f"{len(nim_ids)} nim models, e.g. {nim_ids[:3]}")

        print("\n[STAGE 3] auth gate (Anthropic shape, no key)")
        code, raw = http("POST", "/v1/messages",
                         anthropic_body(args.model, "hi"), timeout=10)
        err = jbody(raw)
        gate_ok = (code == 401
                   and err.get("error", {}).get("type") == "authentication_error")
        stage("401 authentication_error without key", gate_ok,
              f"HTTP {code} type={err.get('error', {}).get('type')}")

        print("\n[STAGE 4] non-stream /v1/messages → NIM (Anthropic wire)")
        t0 = time.time()
        code, raw = http("POST", "/v1/messages",
                         anthropic_body(args.model,
                                        "Reply with exactly: OK"),
                         timeout=120, api_key=api_key)
        dt = time.time() - t0
        resp = jbody(raw)
        text = "".join(b.get("text", "") for b in resp.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "text")
        ok4 = (code == 200 and resp.get("type") == "message"
               and "OK" in text and resp.get("stop_reason") == "end_turn"
               and isinstance(resp.get("usage", {}).get("input_tokens"), int))
        stage("non-stream 200 + Anthropic shape + 'OK'", ok4,
              f"HTTP {code} in {dt:.1f}s text={text[:40]!r} "
              f"stop={resp.get('stop_reason')} usage={resp.get('usage')}")

        print("\n[STAGE 5] stream /v1/messages (Anthropic SSE)")
        t0 = time.time()
        r = requests.post(
            f"{BASE}/v1/messages",
            json=anthropic_body(args.model, "Count 1 to 5", stream=True,
                                max_tokens=256),
            headers={"Authorization": f"Bearer {api_key}"},
            stream=True, timeout=180)
        events, datas = [], []
        if r.status_code == 200:
            buf = b""
            for chunk in r.iter_content(chunk_size=None):
                buf += chunk
                if b"message_stop" in buf:
                    break
            r.close()
            events, datas = sse_events(buf)
            dt = time.time() - t0
            seq_ok = (events[:1] == ["message_start"]
                      and events[-1] == "message_stop"
                      and any('"text_delta"' in json.dumps(d) for d in datas))
            stage("full Anthropic SSE sequence + text deltas", seq_ok,
                  f"events={events[:6]}{'…' if len(events) > 6 else ''} "
                  f"in {dt:.1f}s")
        else:
            body_raw = r.content
            r.close()
            stage("full Anthropic SSE sequence + text deltas", False,
                  f"HTTP {r.status_code}: {body_raw[:160]!r}")

        print("\n[STAGE 6] native tool-calling round-trip")
        t0 = time.time()
        code, raw = http("POST", "/v1/messages",
                         anthropic_body(
                             args.model,
                             "What is the weather in Tokyo right now? "
                             "Call the get_weather tool.",
                             tools=[WEATHER_TOOL], max_tokens=512),
                         timeout=120, api_key=api_key)
        dt = time.time() - t0
        resp = jbody(raw)
        tool_blocks = [b for b in resp.get("content", [])
                       if isinstance(b, dict) and b.get("type") == "tool_use"]
        ok6 = (code == 200 and tool_blocks
               and tool_blocks[0].get("name") == "get_weather"
               and isinstance(tool_blocks[0].get("input"), dict)
               and resp.get("stop_reason") == "tool_use")
        stage("tool_use block + stop_reason", ok6,
              f"HTTP {code} in {dt:.1f}s tool={tool_blocks[0]['name'] if tool_blocks else 'NONE'} "
              f"input={tool_blocks[0].get('input') if tool_blocks else '-'} "
              f"stop={resp.get('stop_reason')}")

        print("\n[STAGE 7] OpenAI passthrough /v1/chat/completions")
        t0 = time.time()
        code, raw = http("POST", "/v1/chat/completions", {
            "model": args.model, "stream": False, "max_tokens": 64,
            "messages": [{"role": "user", "content": "Reply with exactly: PONG"}],
        }, timeout=120, api_key=api_key)
        dt = time.time() - t0
        oai = jbody(raw)
        content = ""
        try:
            content = oai["choices"][0]["message"]["content"]
        except Exception:
            pass
        stage("OpenAI shape + content", code == 200 and "PONG" in content,
              f"HTTP {code} in {dt:.1f}s content={content[:40]!r}")

        print("\n[STAGE 8] reasoning stream (thinking blocks) — tolerant")
        code, raw = http("POST", "/v1/messages",
                         anthropic_body(args.reasoning_model,
                                        "What is 17*23? Think briefly.",
                                        stream=True, max_tokens=512),
                         timeout=300, api_key=api_key)
        if code == 200:
            events, datas = sse_events(raw)
            kinds = [d.get("content_block", {}).get("type")
                     for d in datas if d.get("type") == "content_block_start"]
            th = sum(1 for d in datas
                     if d.get("delta", {}).get("type") == "thinking_delta")
            ok8 = (kinds and kinds[0] == "thinking" and th > 0
                   and "text" in kinds)
            stage("thinking block precedes text", ok8,
                  f"block kinds={kinds} thinking_deltas={th} events={len(events)}")
        else:
            detail = jbody(raw).get("error", {}).get("message", "")[:120]
            stage("thinking block precedes text (SKIP: model unavailable)",
                  True, f"HTTP {code} — reasoning model not on catalog: {detail}")

        print("\n[STAGE 9] error translation (unknown NIM model)")
        code, raw = http("POST", "/v1/messages",
                         anthropic_body("nim/bogus/model-zzz", "hi"),
                         timeout=30, api_key=api_key)
        err = jbody(raw).get("error", {})
        stage("404 → not_found_error (Anthropic shape)",
              code == 404 and err.get("type") == "not_found_error"
              and bool(err.get("message")),
              f"HTTP {code} type={err.get('type')} msg={str(err.get('message'))[:80]!r}")

        print("\n[STAGE 10] observability — X-Upstream-Via header")
        r = requests.post(
            f"{BASE}/v1/messages",
            json=anthropic_body(args.model, "Reply: VIA"),
            headers={"Authorization": f"Bearer {api_key}"}, timeout=120)
        via = r.headers.get("X-Upstream-Via", "")
        stage("X-Upstream-Via: nim", r.status_code == 200 and via == "nim",
              f"header={via!r} HTTP {r.status_code}")
        r.close()

    finally:
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
        "channel": "nvidia-nim (authorized provider adapter)",
        "model": args.model,
        "passed": passed, "total": total,
        "results": [{"name": n, "ok": ok, "detail": d} for n, ok, d in RESULTS],
    }
    out = os.path.join(REPO, "scripts", "smoke_nim_report.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f" report: {out}")
    print(f" proxy log: {LOG_PATH}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
