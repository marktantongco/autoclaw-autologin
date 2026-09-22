"""NVIDIA NIM channel adapter (v2.7.0 — Synergy 10: authorized provider adapters).

Routes `nim/*` models to NVIDIA NIM (build.nvidia.com / integrate.api.nvidia.com)
— a FIRST-PARTY, authorized, OpenAI-compatible endpoint used with the
operator's own API key (nvapi-...).

Design contract (deliberately different from the AutoClaw channel):
  - Bearer auth with the operator's OWN key; direct TLS-verified egress
    (verify=True — this is a first-party endpoint, no TLS games).
  - No token rotation, no system-banner injection, no DSML tool shim,
    no output-cap clamping, no permanent-failure negative cache, no
    proxy-racing egress — none of the AutoClaw upstream quirks apply to
    an authorized API.
  - Native OpenAI function-calling passes through untouched.
  - `reasoning_content` deltas (deepseek-r1 / nemotron style) surface as
    Anthropic `thinking` blocks on the /v1/messages adapter path.

Model addressing: the client-facing model id is "nim/<nim-model-id>".
The nim/ prefix is stripped and the remainder is sent verbatim — NIM ids
themselves contain slashes (e.g. "meta/llama-3.3-70b-instruct").

Env:
  NVIDIA_API_KEY       nvapi-... (required to enable the channel)
  NVIDIA_NIM_BASE_URL  default https://integrate.api.nvidia.com/v1
  NVIDIA_NIM_TIMEOUT   upstream read timeout seconds (default 300)

Wire surface (wired in proxy.py):
  POST /v1/chat/completions  model nim/*  → transparent OpenAI in/out
  POST /v1/messages          model nim/*  → Anthropic in/out (adapter)
  GET  /v1/models                         → NIM catalog appended (when enabled)
"""

import json
import logging
import os
import threading
import time

import requests
from flask import Response, jsonify

logger = logging.getLogger("autoclaw.nim")

MODEL_PREFIX = "nim/"

# Static fallback catalog (ids live-verified 2026-09-22; the live catalog
# is fetched opportunistically — see list_models_live(). NOTE: meta/
# llama-3.3-70b-instruct and deepseek-r1 reached end-of-life upstream —
# do not re-add without a live probe).
NIM_MODELS = [
    "deepseek-ai/deepseek-v4.1-flash",
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3-ultra-550b-a55b",
    "z-ai/glm-5.3",
    "z-ai/glm-5.3-flash",
    "openai/gpt-oss-20b",
]

_CATALOG_TTL = 600.0
_catalog_cache = {"models": None, "ts": 0.0}
_catalog_lock = threading.Lock()

# HTTP status → Anthropic error type (OpenAI passthrough keeps the raw status
# and carries the same type in the error payload).
_STATUS_ERROR_TYPE = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    429: "rate_limit_error",
}


# ── Configuration ────────────────────────────────────────────────────────

def api_key():
    return (os.environ.get("NVIDIA_API_KEY") or "").strip()


def base_url():
    return (os.environ.get("NVIDIA_NIM_BASE_URL")
            or "https://integrate.api.nvidia.com/v1").rstrip("/")


def read_timeout():
    try:
        return float(os.environ.get("NVIDIA_NIM_TIMEOUT", "300"))
    except ValueError:
        return 300.0


def enabled():
    return bool(api_key())


# ── Model addressing ─────────────────────────────────────────────────────

def is_nim_model(model):
    """True when the client model id belongs to the NIM channel."""
    return isinstance(model, str) and model.startswith(MODEL_PREFIX)


def upstream_model(client_model):
    """'nim/meta/llama-3.3-70b-instruct' → 'meta/llama-3.3-70b-instruct'."""
    return client_model[len(MODEL_PREFIX):]


def list_models_live():
    """Live NIM catalog (GET /models), cached 10 min; static fallback."""
    with _catalog_lock:
        if (_catalog_cache["models"] is not None
                and time.time() - _catalog_cache["ts"] < _CATALOG_TTL):
            return list(_catalog_cache["models"])
    if not enabled():
        return list(NIM_MODELS)
    try:
        r = requests.get(
            f"{base_url()}/models",
            headers={"Authorization": f"Bearer {api_key()}"},
            timeout=(3, 8), verify=True,
        )
        if r.status_code == 200:
            ids = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
            if ids:
                with _catalog_lock:
                    _catalog_cache.update(models=list(ids), ts=time.time())
                return ids
        logger.info(f"NIM catalog fetch returned HTTP {r.status_code}; using static list")
    except Exception as exc:
        logger.info(f"NIM catalog fetch failed ({type(exc).__name__}); using static list")
    return list(NIM_MODELS)


# ── Upstream call ────────────────────────────────────────────────────────

def chat_post(openai_body, stream):
    """POST /chat/completions on NIM. Direct egress, TLS-verified, Bearer auth."""
    headers = {
        "Authorization": f"Bearer {api_key()}",
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream else "application/json",
    }
    return requests.post(
        f"{base_url()}/chat/completions",
        json=openai_body, headers=headers, stream=stream,
        timeout=(15, read_timeout()), verify=True,
    )


def upstream_message(resp):
    """Extract an English message from a NIM error response body."""
    raw = ""
    try:
        raw = (resp.text or "")[:1000]
    except Exception:
        pass
    try:
        payload = json.loads(raw)
    except Exception:
        payload = None
    msg = None
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("code")
        elif isinstance(err, str):
            msg = err
        if not msg and payload.get("detail") is not None:
            detail = payload["detail"]
            msg = detail if isinstance(detail, str) else json.dumps(detail)[:300]
    if not msg:
        msg = raw.strip() or f"HTTP {resp.status_code}"
    return str(msg)[:500]


def _openai_error(status, message, etype):
    return jsonify({"error": {"message": message, "type": etype,
                              "code": status}}), status


# ── Channel handlers (wired in proxy.py) ─────────────────────────────────

def handle_openai_chat(body, g):
    """nim/* branch of POST /v1/chat/completions — transparent OpenAI in/out."""
    if not enabled():
        return _openai_error(503, "NIM channel not enabled — set NVIDIA_API_KEY",
                             "api_error")
    model_id = upstream_model(body["model"])
    upstream_body = dict(body)
    upstream_body["model"] = model_id
    wants_stream = bool(upstream_body.get("stream", False))
    g._wants_stream = wants_stream
    g._upstream_via = "nim"
    logger.info(f"NIM openai passthrough: model={model_id} stream={wants_stream}")

    try:
        resp = chat_post(upstream_body, stream=wants_stream)
    except requests.exceptions.Timeout as exc:
        return _openai_error(504, f"NIM timeout: {exc}", "timeout_error")
    except Exception as exc:
        return _openai_error(502, f"NIM unreachable: "
                                  f"{type(exc).__name__}: {exc}", "api_error")

    if resp.status_code != 200:
        return _openai_error(
            resp.status_code, upstream_message(resp),
            _STATUS_ERROR_TYPE.get(resp.status_code, "api_error"))

    if wants_stream:
        def generate():
            for line in resp.iter_lines():
                if not line:
                    continue
                if isinstance(line, bytes):
                    yield line + b"\n\n"
                else:
                    yield line.encode() + b"\n\n"
        return Response(
            generate(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        return jsonify(resp.json())
    except Exception as exc:
        return _openai_error(502, f"NIM returned invalid JSON: {exc}", "api_error")


def handle_anthropic_messages(body, client_model, g):
    """nim/* branch of POST /v1/messages — Anthropic in/out via the adapter.

    Reuses anthropic_compat (request conversion + response/stream converters);
    the NIM channel contributes auth, egress, model rewrite and error
    translation only.
    """
    from anthropic_compat import (
        anthropic_to_openai, openai_to_anthropic_response,
        AnthropicStreamConverter,
    )

    if not enabled():
        return jsonify({"type": "error", "error": {
            "type": "api_error",
            "message": "NIM channel not enabled — set NVIDIA_API_KEY",
        }}), 503

    openai_body = anthropic_to_openai(body)
    openai_body["model"] = upstream_model(client_model)
    if openai_body.get("max_tokens") is None:
        openai_body["max_tokens"] = 1024
    wants_stream = bool(body.get("stream", False))
    # CRITICAL: anthropic_to_openai does NOT carry the stream flag — without
    # an explicit upstream stream=True, NIM answers with a single non-stream
    # JSON document and the SSE converter below yields an empty 200.
    openai_body["stream"] = wants_stream
    g._wants_stream = wants_stream
    g._upstream_via = "nim"
    logger.info(f"NIM anthropic adapter: model={openai_body['model']} "
                f"stream={wants_stream} tools={len(openai_body.get('tools') or [])}")

    try:
        resp = chat_post(openai_body, stream=wants_stream)
    except requests.exceptions.Timeout as exc:
        return jsonify({"type": "error", "error": {
            "type": "timeout_error", "message": f"NIM timeout: {exc}"}}), 504
    except Exception as exc:
        return jsonify({"type": "error", "error": {
            "type": "api_error",
            "message": f"NIM unreachable: {type(exc).__name__}: {exc}"}}), 502

    if resp.status_code != 200:
        return jsonify({"type": "error", "error": {
            "type": _STATUS_ERROR_TYPE.get(resp.status_code, "api_error"),
            "message": upstream_message(resp),
        }}), resp.status_code

    if wants_stream:
        converter = AnthropicStreamConverter(client_model)

        def generate():
            for line in resp.iter_lines():
                if not line:
                    continue
                s = (line.decode("utf-8", errors="replace")
                     if isinstance(line, bytes) else line)
                if not s.startswith("data: "):
                    continue
                data_str = s[6:]
                if data_str.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data_str)
                except json.JSONDecodeError:
                    continue
                for event in converter.process_chunk(chunk):
                    yield event
            for event in converter.flush_trailers():
                yield event

        return Response(
            generate(), mimetype="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        upstream_json = resp.json()
    except Exception as exc:
        return jsonify({"type": "error", "error": {
            "type": "api_error",
            "message": f"NIM returned invalid JSON: {exc}"}}), 502
    return jsonify(openai_to_anthropic_response(upstream_json, client_model))
