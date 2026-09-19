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
import uuid

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
_STATS = {"injections": 0, "buffered_parses": 0, "stream_calls": 0,
          "parse_failures": 0}


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
        return None
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
        elif self._buf:
            out.append({"content": self._buf})
        self._buf = ""
        self._capturing = False
        self._open_tag = ""
        return out


# ──────────────────────────────────────────────────────────────────────────
# Observability
# ──────────────────────────────────────────────────────────────────────────

def stats():
    """Observability block for /health. Never raises."""
    try:
        with _LOCK:
            return {"enabled": _ENABLED, **_STATS}
    except Exception as exc:  # pragma: no cover — defensive
        return {"enabled": _ENABLED, "error": str(exc)}


def reset():
    """Clear counters (offline tests only)."""
    with _LOCK:
        for k in _STATS:
            _STATS[k] = 0
