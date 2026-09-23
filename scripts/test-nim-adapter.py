#!/usr/bin/env python3
"""v2.7.0 — NIM channel adapter unit tests (offline, mocked upstream).

Covers, without any network access (chat_post is monkeypatched):
  1.  Model addressing: nim/ prefix parse (slash-bearing NIM ids)
  2.  OpenAI passthrough (non-stream): model rewrite + verbatim response
  3.  OpenAI passthrough (stream): SSE line forwarding incl. [DONE]
  4.  OpenAI error translation: 401 → authentication_error
  5.  Anthropic non-stream adapter: request translation (system/tools/
      max_tokens) + response → Anthropic shape (stop_reason/usage/thinking)
  6.  Anthropic stream adapter: full event sequence, text-only (backwards
      compat with the AutoClaw wire shape)
  7.  Anthropic stream adapter + reasoning_content: thinking block opens
      before text, switches advance block index, deltas typed correctly
  8.  anthropic_compat non-stream: reasoning_content → thinking block
  9.  Disabled channel → 503 with correct wire shape (OpenAI + Anthropic)
  10. Unknown NIM model → 404 not_found_error (Anthropic + OpenAI shapes)

Usage: python3 scripts/test-nim-adapter.py   (exit 0 = all pass)
"""

import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

os.environ.pop("NVIDIA_API_KEY", None)  # deterministic start

from flask import Flask, g, jsonify, request  # noqa: E402

import nim_adapter  # noqa: E402
from anthropic_compat import (  # noqa: E402
    anthropic_to_openai, openai_to_anthropic_response,
    AnthropicStreamConverter,
)

RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok)))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f": {detail}" if detail else ""))
    return ok


class FakeResp:
    """Minimal stand-in for a requests.Response from nim_adapter.chat_post."""

    def __init__(self, status=200, lines=None, payload=None, text=""):
        self.status_code = status
        self._lines = lines or []
        self._payload = payload
        self.text = text or json.dumps(payload) if payload is not None else text

    def iter_lines(self):
        return iter(self._lines)

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload


# ── Test harness app (mirrors the proxy.py wiring) ───────────────────────

app = Flask(__name__)


@app.route("/v1/chat/completions", methods=["POST"])
def cc():
    body = request.get_json(force=True)
    if nim_adapter.is_nim_model(body.get("model")):
        return nim_adapter.handle_openai_chat(body, g)
    return jsonify({"error": {"message": "not routed"}}), 400


@app.route("/v1/messages", methods=["POST"])
def msgs():
    body = request.get_json(force=True)
    model = body.get("model", "")
    if nim_adapter.is_nim_model(model):
        return nim_adapter.handle_anthropic_messages(body, model, g)
    return jsonify({"type": "error", "error": {"type": "invalid_request_error",
                                               "message": "not routed"}}), 400


client = app.test_client()

_capture = {}


def mock_chat_post(resp):
    def _post(openai_body, stream):
        _capture["body"] = openai_body
        _capture["stream"] = stream
        return resp
    return _post


def with_key(fn):
    nim_adapter.enabled = lambda: True
    nim_adapter.chat_post = fn


def without_key():
    nim_adapter.enabled = lambda: False


# ── 1. Model addressing ──────────────────────────────────────────────────
print("\n[1] model addressing")
check("nim/ prefix detected",
      nim_adapter.is_nim_model("nim/meta/llama-3.3-70b-instruct"))
check("non-nim rejected", not nim_adapter.is_nim_model("glm-5.2"))
check("non-string rejected", not nim_adapter.is_nim_model(None))
check("prefix stripped (slash-bearing id preserved)",
      nim_adapter.upstream_model("nim/meta/llama-3.3-70b-instruct")
      == "meta/llama-3.3-70b-instruct")

# ── 2. OpenAI passthrough non-stream ────────────────────────────────────
print("\n[2] openai passthrough (non-stream)")
upstream_json = {
    "id": "cmpl-x", "choices": [{"index": 0,
                                 "message": {"role": "assistant",
                                             "content": "Hello from NIM"},
                                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8},
}
with_key(mock_chat_post(FakeResp(payload=upstream_json)))
with app.test_request_context():
    r = client.post("/v1/chat/completions", json={
        "model": "nim/meta/llama-3.3-70b-instruct", "stream": False,
        "messages": [{"role": "user", "content": "hi"}]})
check("upstream JSON passed through verbatim",
      r.status_code == 200 and r.get_json() == upstream_json)
check("model rewritten (nim/ stripped)",
      _capture["body"]["model"] == "meta/llama-3.3-70b-instruct")
check("stream flag forwarded", _capture["stream"] is False)

# ── 3. OpenAI passthrough stream ────────────────────────────────────────
print("\n[3] openai passthrough (stream)")
sse_lines = [
    b'data: {"choices":[{"delta":{"content":"He"},"finish_reason":null}]}',
    b'data: {"choices":[{"delta":{"content":"y"},"finish_reason":null}]}',
    b"data: [DONE]",
]
with_key(mock_chat_post(FakeResp(lines=sse_lines)))
with app.test_request_context():
    r = client.post("/v1/chat/completions", json={
        "model": "nim/qwen/qwen2.5-coder-32b-instruct", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
body = b"".join(r.response).decode() if hasattr(r, "response") else r.data.decode()
check("stream content-type", r.mimetype == "text/event-stream")
check("SSE lines forwarded in order",
      'data: {"choices":[{"delta":{"content":"He"}' in body
      and body.rstrip().endswith("data: [DONE]"))
check("stream flag forwarded upstream", _capture["stream"] is True)
check("upstream stream flag present (streaming request)",
      _capture["body"].get("stream") is True)

# ── 4/10. Error translation ─────────────────────────────────────────────
print("\n[4] error translation")
with_key(mock_chat_post(FakeResp(
    status=401, text='{"error": {"message": "Invalid API key"}}')))
with app.test_request_context():
    r = client.post("/v1/chat/completions", json={
        "model": "nim/meta/llama-3.1-8b-instruct",
        "messages": [{"role": "user", "content": "hi"}]})
e = r.get_json()["error"]
check("openai 401 → authentication_error",
      r.status_code == 401 and e["type"] == "authentication_error"
      and "Invalid API key" in e["message"])

with_key(mock_chat_post(FakeResp(
    status=404, text='{"detail": "Model meta/xyz is not available"}')))
with app.test_request_context():
    r = client.post("/v1/messages", json={
        "model": "nim/meta/xyz", "max_tokens": 16,
        "messages": [{"role": "user", "content": "hi"}]})
a = r.get_json()["error"]
check("anthropic 404 → not_found_error",
      r.status_code == 404 and a["type"] == "not_found_error"
      and "not available" in a["message"]
      and r.get_json()["type"] == "error")

# ── 5. Anthropic non-stream adapter ─────────────────────────────────────
print("\n[5] anthropic adapter (non-stream)")
nim_resp = {
    "choices": [{
        "message": {"role": "assistant", "content": "Weather lookup done",
                    "reasoning_content": "I should call the tool."},
        "finish_reason": "tool_calls",
        "tool_calls": None,
    }],
}
nim_resp = {
    "choices": [{
        "message": {"role": "assistant",
                    "content": None,
                    "reasoning_content": "User wants weather.",
                    "tool_calls": [{
                        "id": "call_abc", "type": "function",
                        "function": {"name": "get_weather",
                                     "arguments": "{\"city\": \"Tokyo\"}"},
                    }]},
        "finish_reason": "tool_calls",
    }],
    "usage": {"prompt_tokens": 21, "completion_tokens": 14},
}
with_key(mock_chat_post(FakeResp(payload=nim_resp)))
with app.test_request_context():
    r = client.post("/v1/messages", json={
        "model": "nim/meta/llama-3.3-70b-instruct",
        "max_tokens": 512,
        "system": "You are a weather agent.",
        "tools": [{"name": "get_weather", "description": "Get weather",
                   "input_schema": {"type": "object",
                                    "properties": {"city": {"type": "string"}},
                                    "required": ["city"]}}],
        "messages": [{"role": "user",
                      "content": [{"type": "text",
                                   "text": "Weather in Tokyo?"}]}]})
out = r.get_json()
check("anthropic 200 message shape",
      r.status_code == 200 and out["type"] == "message"
      and out["role"] == "assistant"
      and out["model"] == "nim/meta/llama-3.3-70b-instruct")
blocks = out.get("content", [])
check("thinking block precedes tool_use",
      [b.get("type") for b in blocks] == ["thinking", "tool_use"])
tool_block = next((b for b in blocks if b["type"] == "tool_use"), {})
check("tool_use block carries name+parsed input",
      tool_block.get("name") == "get_weather"
      and tool_block.get("input") == {"city": "Tokyo"}
      and bool(tool_block.get("id")))
check("stop_reason tool_use + usage mapped",
      out["stop_reason"] == "tool_use"
      and out["usage"]["input_tokens"] == 21
      and out["usage"]["output_tokens"] == 14)
up = _capture["body"]
check("request translated: system message",
      up["messages"][0]["role"] == "system"
      and up["messages"][0]["content"] == "You are a weather agent.")
check("request translated: tools → OpenAI function shape",
      up["tools"][0]["type"] == "function"
      and up["tools"][0]["function"]["name"] == "get_weather"
      and up["tools"][0]["function"]["parameters"]["required"] == ["city"])
check("request translated: model + max_tokens",
      up["model"] == "meta/llama-3.3-70b-instruct" and up["max_tokens"] == 512)
check("upstream stream flag present (non-stream request)",
      up.get("stream") is False)

# ── 6. Anthropic stream adapter (text-only — AutoClaw wire compat) ──────
print("\n[6] anthropic stream adapter (text-only)")
stream_chunks = [
    b'data: {"choices":[{"delta":{"role":"assistant","content":""}}]}',
    b'data: {"choices":[{"delta":{"content":"Hel"}}]}',
    b'data: {"choices":[{"delta":{"content":"lo"}}]}',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
    b"data: [DONE]",
]
with_key(mock_chat_post(FakeResp(lines=stream_chunks)))
with app.test_request_context():
    r = client.post("/v1/messages", json={
        "model": "nim/meta/llama-3.1-8b-instruct", "max_tokens": 32,
        "stream": True,
        "messages": [{"role": "user", "content": "say hello"}]})
raw = b"".join(r.response).decode() if hasattr(r, "response") else r.data.decode()
ev_types = [ln[7:].strip() for ln in raw.splitlines()
            if ln.startswith("event: ")]
deltas = [json.loads(ln[6:])["delta"] for ln in raw.splitlines()
          if ln.startswith("data: ") and '"text_delta"' in ln]
check("full Anthropic SSE sequence",
      ev_types[0] == "message_start" and "message_stop" in ev_types
      and ev_types.count("message_delta") == 1)
check("text deltas concatenated",
      "".join(d.get("text", "") for d in deltas) == "Hello")
kinds = [json.loads(ln[6:]).get("content_block", {}).get("type")
         for ln in raw.splitlines() if ln.startswith("data: ")
         and '"content_block_start"' in ln]
check("single text block opened (no spurious thinking)",
      kinds == ["text"])

# ── 7. Anthropic stream adapter + reasoning_content ─────────────────────
print("\n[7] anthropic stream adapter (reasoning → thinking)")
r_chunks = [
    b'data: {"choices":[{"delta":{"reasoning_content":"Think "}}]}',
    b'data: {"choices":[{"delta":{"reasoning_content":"hard."}}]}',
    b'data: {"choices":[{"delta":{"content":"Answer!"}}]}',
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":7}}',
    b"data: [DONE]",
]
with_key(mock_chat_post(FakeResp(lines=r_chunks)))
with app.test_request_context():
    r = client.post("/v1/messages", json={
        "model": "nim/deepseek-ai/deepseek-r1", "max_tokens": 64,
        "stream": True,
        "messages": [{"role": "user", "content": "puzzle"}]})
raw = b"".join(r.response).decode() if hasattr(r, "response") else r.data.decode()
starts = [json.loads(ln[6:]) for ln in raw.splitlines()
          if ln.startswith("data: ") and '"content_block_start"' in ln]
kinds = [s["content_block"]["type"] for s in starts]
idxs = [s["index"] for s in starts]
th_deltas = [json.loads(ln[6:])["delta"] for ln in raw.splitlines()
             if ln.startswith("data: ") and '"thinking_delta"' in ln]
tx_deltas = [json.loads(ln[6:])["delta"] for ln in raw.splitlines()
             if ln.startswith("data: ") and '"text_delta"' in ln]
stops = [json.loads(ln[6:])["index"] for ln in raw.splitlines()
         if ln.startswith("data: ") and '"content_block_stop"' in ln]
check("thinking block opens before text",
      kinds == ["thinking", "text"] and idxs == [0, 1])
check("thinking deltas typed + concatenated",
      "".join(d.get("thinking", "") for d in th_deltas) == "Think hard.")
check("text delta intact",
      "".join(d.get("text", "") for d in tx_deltas) == "Answer!")
check("both blocks closed", sorted(stops) == [0, 1])
check("message_stop terminates", raw.rstrip().endswith('"message_stop"')
      or '"type": "message_stop"' in raw or "'message_stop'" in raw)

# ── 8. Non-stream converter reasoning → thinking ────────────────────────
print("\n[8] openai_to_anthropic_response (reasoning)")
conv = openai_to_anthropic_response({
    "choices": [{"message": {"role": "assistant",
                             "content": "final",
                             "reasoning_content": "chain of thought"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 3, "completion_tokens": 2},
}, "nim/x")
check("thinking prepended before text",
      [b["type"] for b in conv["content"]] == ["thinking", "text"]
      and conv["content"][0]["thinking"] == "chain of thought")

# converter unit: interleaved reasoning switches advance block index
c = AnthropicStreamConverter("t")
evs = []
evs += c.process_chunk({"choices": [{"delta": {"reasoning_content": "a"}}]})
evs += c.process_chunk({"choices": [{"delta": {"content": "b"}}]})
evs += c.process_chunk({"choices": [{"delta": {"reasoning_content": "c"}}]})
evs += c.process_chunk({"choices": [{"delta": {}, "finish_reason": "stop"}]})
evs += c.flush_trailers()
txt = "".join(evs)
check("converter: open/close per kind switch",
      txt.count('"content_block_start"') == 3
      and txt.count('"content_block_stop"') == 3)

# ── 9. Disabled channel ─────────────────────────────────────────────────
print("\n[9] disabled channel")
without_key()
with app.test_request_context():
    r = client.post("/v1/messages", json={
        "model": "nim/meta/llama-3.3-70b-instruct", "max_tokens": 8,
        "messages": [{"role": "user", "content": "hi"}]})
check("anthropic 503 api_error when key missing",
      r.status_code == 503
      and r.get_json()["error"]["type"] == "api_error")
with app.test_request_context():
    r = client.post("/v1/chat/completions", json={
        "model": "nim/meta/llama-3.3-70b-instruct",
        "messages": [{"role": "user", "content": "hi"}]})
check("openai 503 when key missing",
      r.status_code == 503
      and r.get_json()["error"]["type"] == "api_error")
check("static catalog fallback without key",
      nim_adapter.list_models_live() == nim_adapter.NIM_MODELS)

# ── request-conversion regression guard (existing behaviour) ────────────
print("\n[10] anthropic_to_openai regression guard")
o = anthropic_to_openai({
    "model": "x", "max_tokens": 10, "temperature": 0.2, "top_p": 0.9,
    "stop_sequences": ["STOP"],
    "system": "sys",
    "messages": [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f",
             "input": {"a": 1}}]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "res"}]},
    ],
})
check("simple params pass through",
      o["max_tokens"] == 10 and o["temperature"] == 0.2
      and o["top_p"] == 0.9 and o["stop"] == ["STOP"])
check("tool_use/tool_result round-trip",
      o["messages"][2]["tool_calls"][0]["id"] == "t1"
      and o["messages"][3]["role"] == "tool"
      and o["messages"][3]["tool_call_id"] == "t1")

# ── Verdict ──────────────────────────────────────────────────────────────
passed = sum(1 for _, ok in RESULTS if ok)
total = len(RESULTS)
print("\n" + "=" * 64)
print(f" VERDICT: {passed}/{total} checks passed")
print("=" * 64)
sys.exit(0 if passed == total else 1)
