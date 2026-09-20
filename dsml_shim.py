"""DSML tool-calling shim (Synergy 8 — Phase 2).

Source: tt-52101/chat-z-ai-proxy-web2api-free. Upstream definition from the
synergy research:

    "A DSML tool-calling shim that synthesises OpenAI function-calling
    responses for upstream models that lack a native tool API. Wraps the
    model's natural-language tool-use intent into a structured tool_calls
    field that the OpenAI Python SDK can dispatch on. Adds tool-using
    capability to models that would otherwise be excluded."

DSML = Domain-Specific Markup Language. The shim works in two directions:

  Request   When the client sends OpenAI `tools`, a protocol block is
            appended to the system prompt describing every tool (name,
            description, JSON-schema parameters) and the exact DSML markup
            the model must emit to invoke one: <dsml:tool_call> blocks.

  Response  The model's textual tool-use intent is parsed back into real
            OpenAI `tool_calls` — on the buffered path via a regex parse of
            the aggregated text, and on the streaming path via a
            StreamSieve-style state machine that forwards prose immediately,
            holds back anything that looks like the start of a DSML block,
            and converts captured blocks into streaming tool_calls deltas
            (id/name chunk, then arguments chunk — maximally client-compatible).

Native upstream tool_calls (when the upstream happens to emit them) pass
through untouched: the sieve only intercepts delta.content, and the buffered
path only synthesises when no native calls were aggregated.

Env knobs:
  AUTOCLAW_DSML_ENABLED   1|0   master switch (default 1)
"""

import json
import os
import re
import threading
import time
import uuid
from collections import deque

_ENABLED = os.environ.get("AUTOCLAW_DSML_ENABLED", "1").strip().lower() not in (
    "0", "false", "no", "off")

_PROTOCOL_MARKER = "## Tool Calling Protocol (DSML)"
_OPEN_TAG = "<dsml:tool_call"
_CLOSE_TAG = "</dsml:tool_call>"
_MAX_OPEN_TAG_WAIT = 256     # chars before an ambiguous "<dsml..." is prose
_MAX_BLOCK_BUFFER = 262144   # pathological unclosed-block guard (256 KiB)

_BLOCK_RE = re.compile(
    r"(?is)<dsml:tool_call\b[^>]*>(.*?)</dsml:tool_call>")
_FUNC_RE = re.compile(r"(?is)<dsml:function[^>]*>(.*?)</dsml:function>")
_ARGS_RE = re.compile(r"(?is)<dsml:arguments[^>]*>(.*?)</dsml:arguments>")
_ID_ATTR_RE = re.compile(r"""(?is)\bid\s*=\s*["']([^"']*)["']""")

_LOCK = threading.Lock()
# Legacy counters keep their names/meaning (health consumers + smoke test).
# Phase-3.1 additions (research task 8-c): grain-correct denominators, the
# leak taxonomy, finish overrides and the tool-loop approximation.
_STATS = {"injections": 0, "buffered_parses": 0, "stream_calls": 0,
          "parse_failures": 0,
          "responses_stream": 0, "responses_stream_hits": 0,
          "responses_buffered": 0,
          "parse_attempts": 0, "calls_buffered": 0,
          "finish_overrides": 0, "tool_results_seen": 0}
_LEAKS = {"truncated_flush": 0, "oversize_degrade": 0,
          "open_tag_timeout": 0, "unparseable_buffered": 0}
_OVERHEAD_MS = deque(maxlen=60)   # shim-added latency ring (last 60 responses)


def _note(key, n=1):
    """Fail-safe counter bump. Never raises; unknown keys are ignored."""
    try:
        with _LOCK:
            if key in _STATS:
                _STATS[key] += n
    except Exception:
        pass


def note_response(path):
    """Count a shim-processed response: path 'stream' or 'buffered'.

    Call AFTER the response completed; `hits=True` variants are tracked via
    note_stream_hit() so parse-success rates stay response-grain honest.
    """
    _note("responses_stream" if path == "stream" else "responses_buffered")


def note_stream_hit():
    """A streaming shim response produced at least one tool call."""
    _note("responses_stream_hits")


def note_parse_attempt():
    """Buffered path: markup was present, a parse will be attempted."""
    _note("parse_attempts")


def note_calls_buffered(n):
    """Buffered path produced n call-grain tool_calls (grain parity with
    stream_calls)."""
    _note("calls_buffered", n)


def note_override():
    """finish_reason was overridden to 'tool_calls' (client-visible action)."""
    _note("finish_overrides")


def note_tool_results_seen():
    """Request carried role:'tool' messages (APPROXIMATE round-trip signal:
    also fires for native tool calls; fleet-level only, never per-call)."""
    _note("tool_results_seen")


def note_leak(reason):
    """A detected markup-leak incident. Reasons: truncated_flush
    (stream block never closed), oversize_degrade (block > 256 KiB),
    open_tag_timeout (open tag never completed), unparseable_buffered
    (buffered markup with zero parseable blocks). Detected leaks only —
    malformed-but-scrubbed fragments are not observable proxy-side."""
    try:
        with _LOCK:
            if reason in _LEAKS:
                _LEAKS[reason] += 1
    except Exception:
        pass


def note_overhead_ms(ms):
    """Record shim-added latency (sieve feed/flush + buffered parse wall
    time) into the 60-sample ring. Mirrors LiteLLM's overhead-latency
    histogram concept."""
    try:
        if ms is None or ms != ms or ms < 0 or ms > 60000:
            return
        with _LOCK:
            _OVERHEAD_MS.append(float(ms))
    except Exception:
        pass


def enabled():
    """True when the DSML shim is active (client tools will be shimmed)."""
    return _ENABLED


# ──────────────────────────────────────────────────────────────────────────
# Request side: protocol injection
# ──────────────────────────────────────────────────────────────────────────

def _normalize_tools(tools):
    """Flatten OpenAI tool specs to [{name, description, parameters}]."""
    norm = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        if not name:
            continue
        norm.append({
            "name": name,
            "description": fn.get("description", ""),
            "parameters": fn.get("parameters") or {
                "type": "object", "properties": {}},
        })
    return norm


def _choice_directive(tool_choice):
    """Extra mandate line derived from OpenAI tool_choice."""
    if tool_choice == "required":
        return "You MUST invoke at least one tool in this reply."
    if isinstance(tool_choice, dict):
        fn = tool_choice.get("function") or {}
        name = fn.get("name")
        if name:
            return (f'You MUST invoke the tool "{name}" in this reply; '
                    "do not answer in prose.")
    return ""  # "auto"/absent — no mandate


def build_tool_protocol(tools, tool_choice=None):
    """Render the DSML protocol block injected into the system prompt."""
    norm = _normalize_tools(tools)
    lines = [
        _PROTOCOL_MARKER,
        "To invoke a tool, output a block EXACTLY in this form "
        "(plain text, no code fence):",
        "<dsml:tool_call id=\"call_1\">",
        "<dsml:function>tool_name</dsml:function>",
        "<dsml:arguments>{\"json\": \"object\"}</dsml:arguments>",
        "</dsml:tool_call>",
        "Rules:",
        "- <dsml:arguments> MUST contain one valid JSON object on a single "
        "line, conforming to the tool's parameters schema.",
        "- Multiple tool calls: emit multiple consecutive blocks.",
        "- Prose between blocks is allowed and is your visible reply.",
        "- If no tool is needed, never emit a <dsml:tool_call> block.",
    ]
    directive = _choice_directive(tool_choice)
    if directive:
        lines.append("- " + directive)
    lines.append("### Available tools")
    lines.append("<dsml:tools>"
                 + json.dumps(norm, ensure_ascii=False)
                 + "</dsml:tools>")
    return "\n".join(lines)


def inject_tool_protocol(messages, tools, tool_choice=None):
    """Append the DSML protocol to the first system message (mutates in place).

    Runs AFTER the AutoClaw banner injection (the banner must stay first for
    upstream to keep the metered chat path). Creates a system message when
    the request has none. Idempotent on the protocol marker so retries and
    double-injection cannot stack protocol blocks.
    """
    if not isinstance(messages, list) or not messages:
        return messages
    protocol = build_tool_protocol(tools, tool_choice)
    if messages[0].get("role") == "system":
        content = messages[0].get("content", "")
        if isinstance(content, str) and _PROTOCOL_MARKER in content:
            return messages  # already injected
        new_sys = dict(messages[0])
        new_sys["content"] = (content + "\n\n" + protocol) if content else protocol
        messages[0] = new_sys
    else:
        messages.insert(0, {"role": "system", "content": protocol})
    with _LOCK:
        _STATS["injections"] += 1
    return messages


# ──────────────────────────────────────────────────────────────────────────
# Response side: block parsing (shared by buffered + streaming paths)
# ──────────────────────────────────────────────────────────────────────────

def _next_call_id(used_ids, hint=None):
    """Stable unique call id: honour the model's id attr, else mint one."""
    base = hint or f"call_{uuid.uuid4().hex[:8]}"
    candidate = base
    while candidate in used_ids:
        candidate = f"{base}_{uuid.uuid4().hex[:4]}"
    used_ids.add(candidate)
    return candidate


def _extract_call(open_tag, block_body, used_ids):
    """Parse one DSML block body into an OpenAI tool_call dict (or None)."""
    name_m = _FUNC_RE.search(block_body)
    if not name_m:
        with _LOCK:
            _STATS["parse_failures"] += 1
        return None
    name = name_m.group(1).strip()
    args_m = _ARGS_RE.search(block_body)
    args_raw = args_m.group(1).strip() if args_m else "{}"
    # Validate arguments JSON; best-effort repair for models that emit
    # pretty-printed objects despite the single-line rule.
    try:
        json.loads(args_raw)
    except (ValueError, TypeError):
        squashed = re.sub(r"\s+", " ", args_raw)
        try:
            json.loads(squashed)
            args_raw = squashed
        except (ValueError, TypeError):
            pass  # keep raw — dispatcher-side failure is the model's fault
    id_m = _ID_ATTR_RE.search(open_tag or "")
    call_id = _next_call_id(used_ids, id_m.group(1).strip() if id_m else None)
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": args_raw}}


def parse_dsml(text):
    """Buffered parse: extract all DSML blocks from a completed reply.

    Returns {"tool_calls": [...], "content": "<prose outside blocks>"} when
    at least one block parses with a function name, else None (passthrough).
    """
    if not text or _OPEN_TAG not in text.lower():
        return None
    with _LOCK:
        _STATS["parse_attempts"] += 1
    used = set()
    tool_calls = []
    for m in _BLOCK_RE.finditer(text):
        whole = m.group(0)
        gt = whole.find(">")
        open_tag = whole[:gt + 1] if gt != -1 else whole
        call = _extract_call(open_tag, m.group(1), used)
        if call:
            tool_calls.append(call)
    if not tool_calls:
        # Markup was present but zero blocks parsed — the whole text
        # (markup included) passes through to the client: a detected leak.
        with _LOCK:
            _LEAKS["unparseable_buffered"] += 1
        return None
    with _LOCK:
        _STATS["calls_buffered"] += len(tool_calls)
    content = _BLOCK_RE.sub("", text)
    content = re.sub(r"\n{3,}", "\n\n", content).strip()
    with _LOCK:
        _STATS["buffered_parses"] += 1
    return {"tool_calls": tool_calls, "content": content}


# ──────────────────────────────────────────────────────────────────────────
# Response side: StreamSieve (streaming path)
# ──────────────────────────────────────────────────────────────────────────

class DSMLStreamSieve:
    """Incremental splitter between prose and DSML blocks in a content stream.

    feed(text)  → list of OpenAI delta dicts:
                    {"content": "..."}            prose to forward now
                    {"tool_calls": [tc_delta]}    synthesised call deltas
    flush()     → drain at end-of-stream (truncated block degrades to prose).
    saw_tool_calls  → True once at least one block completed.

    Holdback rule: only the tail from the last unterminated "<" is buffered;
    anything that cannot become a DSML open tag is released one character at
    a time, so normal prose (including literal "<" in code/markdown) streams
    through with at most a few characters of latency.
    """

    def __init__(self):
        self._buf = ""
        self._capturing = False
        self._open_tag = ""
        self.saw_tool_calls = False
        self._used_ids = set()
        self._index = 0

    # -- internals ---------------------------------------------------------

    def _emit_call(self, call):
        """Convert a parsed tool_call into streaming delta pieces."""
        tc_id = call["id"]
        fn = call["function"]
        first = {"index": self._index, "id": tc_id, "type": "function",
                 "function": {"name": fn["name"], "arguments": ""}}
        second = {"index": self._index,
                  "function": {"arguments": fn["arguments"]}}
        self._index += 1
        self.saw_tool_calls = True
        with _LOCK:
            _STATS["stream_calls"] += 1
        return [{"tool_calls": [first]}, {"tool_calls": [second]}]

    # -- public API ---------------------------------------------------------

    def feed(self, text):
        """Consume a content delta; return deltas ready for the client."""
        if not text:
            return []
        self._buf += text
        out = []
        while True:
            if not self._capturing:
                i = self._buf.find("<")
                if i == -1:
                    if self._buf:
                        out.append({"content": self._buf})
                        self._buf = ""
                    break
                if i > 0:
                    out.append({"content": self._buf[:i]})
                    self._buf = self._buf[i:]
                # buf starts with "<"
                if len(self._buf) < len(_OPEN_TAG):
                    if _OPEN_TAG.startswith(self._buf.lower()):
                        break  # could still become the open tag — hold
                    out.append({"content": "<"})
                    self._buf = self._buf[1:]
                    continue
                if self._buf[:len(_OPEN_TAG)].lower() == _OPEN_TAG:
                    gt = self._buf.find(">")
                    if gt == -1:
                        if len(self._buf) > _MAX_OPEN_TAG_WAIT:
                            with _LOCK:
                                _LEAKS["open_tag_timeout"] += 1
                            out.append({"content": "<"})
                            self._buf = self._buf[1:]
                            continue
                        break  # open tag still streaming — hold
                    self._capturing = True
                    self._open_tag = self._buf[:gt + 1]
                    self._buf = self._buf[gt + 1:]
                    continue
                # definitely not DSML — release "<" and keep scanning
                out.append({"content": "<"})
                self._buf = self._buf[1:]
            else:
                j = self._buf.lower().find(_CLOSE_TAG)
                if j == -1:
                    if len(self._buf) > _MAX_BLOCK_BUFFER:
                        # pathological unclosed block — degrade to prose
                        with _LOCK:
                            _LEAKS["oversize_degrade"] += 1
                        out.append({"content": self._open_tag + self._buf})
                        self._buf = ""
                        self._capturing = False
                        self._open_tag = ""
                    break
                block_body = self._buf[:j]
                self._buf = self._buf[j + len(_CLOSE_TAG):]
                self._capturing = False
                call = _extract_call(self._open_tag, block_body, self._used_ids)
                self._open_tag = ""
                if call:
                    out.extend(self._emit_call(call))
                # a stray block without a function name is dropped silently
        return out

    def flush(self):
        """End-of-stream drain. Idempotent (buffer cleared)."""
        out = []
        if self._capturing:
            raw = self._open_tag + self._buf
            if raw:
                out.append({"content": raw})
                # A capturing block that never closed releases its markup as
                # prose: a detected leak (stream path).
                with _LOCK:
                    _LEAKS["truncated_flush"] += 1
        elif self._buf:
            out.append({"content": self._buf})
        self._buf = ""
        self._capturing = False
        self._open_tag = ""
        return out


# ──────────────────────────────────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────────────────────────────────

def _pct(num, den):
    return round(num * 100.0 / den, 1) if den else 0.0


def stats():
    """Observability block for /health + dashboard payload. Never raises.

    Superset of the legacy shape: legacy keys keep names/meaning; new keys
    are additive so consumers can index blindly.
    """
    try:
        with _LOCK:
            s = {"enabled": _ENABLED, **_STATS,
                 "markup_leaks": dict(_LEAKS)}
            ov = sorted(_OVERHEAD_MS)
            window = [round(x, 2) for x in _OVERHEAD_MS]
        n = len(ov)
        if n:
            avg = sum(ov) / n
            p95 = ov[min(n - 1, int(round(0.95 * (n - 1))))]
            s["overhead_ms"] = {"avg": round(avg, 2), "p95": round(p95, 2),
                                "window_n": n, "window": window}
        else:
            s["overhead_ms"] = {"avg": 0.0, "p95": 0.0, "window_n": 0,
                                "window": []}
        # rates (computed, never stored) — response-grain honesty:
        attempted = s["parse_attempts"] + s["responses_stream"]
        hits = s["buffered_parses"] + s["responses_stream_hits"]
        shimmed = s["responses_stream"] + s["responses_buffered"]
        leaks_total = sum(_LEAKS.values())
        with_calls = s["buffered_parses"] + s["responses_stream_hits"]
        s["rates"] = {
            "parse_success_pct": _pct(hits, attempted),
            "leak_rate_pct": _pct(leaks_total, shimmed),
            "tool_loop_pct": _pct(s["tool_results_seen"], with_calls),
            # role:"tool" also occurs for native calls — fleet-level signal
            # only, never a per-call completion rate.
            "tool_loop_approximate": True,
        }
        s["calls_stream"] = s["stream_calls"]  # grain-consistent alias
        return s
    except Exception as exc:  # pragma: no cover — defensive
        return {"enabled": _ENABLED, "error": str(exc)}


def reset():
    """Clear counters (offline tests only)."""
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0
        for k in _LEAKS:
            _LEAKS[k] = 0
        _OVERHEAD_MS.clear()
