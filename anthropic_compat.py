"""Anthropic Messages API compatibility layer (Phase-2 Synergy 6).

Translates Anthropic /v1/messages wire format to/from the OpenAI chat
completions format used internally, so Claude Code, OpenCode, and any
Anthropic-SDK client can talk to AutoClaw natively.

Synergy: guell11/OmniClaw-GLM-Proxy anthropic.js (anthropicToOpenAI,
openAIChunkToAnthropicEvents) — reimplemented in Python for autoclaw-autologin.

Endpoints exposed by proxy.py:
  POST /v1/messages              — Anthropic Messages (stream + non-stream)
  POST /v1/messages/count_tokens — stub returning {input_tokens: 0}
"""

import json
import time
import uuid
import logging

logger = logging.getLogger("autoclaw.anthropic_compat")


# ─── Request conversion: Anthropic → OpenAI ─────────────────────────

def anthropic_to_openai(body: dict) -> dict:
    """Convert an Anthropic Messages request body to OpenAI chat format.

    Handles:
      - system (string or content-block list)  -> system message
      - messages[] with text blocks            -> content string
      - tool_use / tool_result blocks          -> assistant tool_calls / tool role
      - image blocks (base64 source)           -> image_url data-URL parts
      - thinking blocks                        -> skipped (no OpenAI equivalent)
      - max_tokens, temperature, top_p, stop_sequences, tools, tool_choice

    Returns an OpenAI-style body dict (model filled by caller).
    """
    messages = []

    # System prompt: string or list of content blocks
    system = body.get("system")
    if system:
        if isinstance(system, str):
            messages.append({"role": "system", "content": system})
        elif isinstance(system, list):
            text = " ".join(
                b.get("text", "") for b in system
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            if text:
                messages.append({"role": "system", "content": text})

    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Plain string content — fast path
        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        # Complex content blocks
        text_parts = []
        tool_calls = []
        tool_results = []
        image_parts = []

        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type", "")

            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "image":
                src = block.get("source", {})
                if src.get("type") == "base64":
                    media = src.get("media_type", "image/png")
                    data = src.get("data", "")
                    image_parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{media};base64,{data}"},
                    })
            elif btype == "tool_use":
                tool_calls.append({
                    "id": block.get("id", f"call_{uuid.uuid4().hex[:8]}"),
                    "type": "function",
                    "function": {
                        "name": block.get("name", ""),
                        "arguments": json.dumps(block.get("input", {})),
                    },
                })
            elif btype == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, list):
                    result_content = " ".join(
                        b.get("text", "") for b in result_content
                        if isinstance(b, dict) and b.get("type") == "text"
                    )
                tool_results.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id", ""),
                    "content": str(result_content),
                })
            elif btype == "thinking":
                # No OpenAI equivalent — skip (matches OmniClaw behavior)
                continue

        # tool_result blocks become standalone 'tool' role messages
        if tool_results:
            messages.extend(tool_results)
            continue

        # Build the message for this turn
        if image_parts:
            # Multimodal: content as parts array
            parts = [{"type": "text", "text": t} for t in text_parts if t]
            parts.extend(image_parts)
            message = {"role": role, "content": parts}
        elif tool_calls:
            message = {
                "role": role,
                "content": " ".join(text_parts) if text_parts else None,
                "tool_calls": tool_calls,
            }
        else:
            message = {"role": role, "content": " ".join(text_parts)}
        messages.append(message)

    openai_body = {"messages": messages}

    # Simple parameter passthrough
    if body.get("max_tokens") is not None:
        openai_body["max_tokens"] = body["max_tokens"]
    if body.get("temperature") is not None:
        openai_body["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        openai_body["top_p"] = body["top_p"]
    if body.get("stop_sequences"):
        openai_body["stop"] = body["stop_sequences"]

    # Tools: Anthropic shape -> OpenAI shape
    if body.get("tools"):
        openai_tools = []
        for tool in body["tools"]:
            if not isinstance(tool, dict):
                continue
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name", ""),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {}),
                },
            })
        openai_body["tools"] = openai_tools

    if body.get("tool_choice"):
        tc = body["tool_choice"]
        if isinstance(tc, dict):
            ttype = tc.get("type", "auto")
            if ttype == "auto":
                openai_body["tool_choice"] = "auto"
            elif ttype == "any":
                openai_body["tool_choice"] = "required"
            elif ttype == "tool" and tc.get("name"):
                openai_body["tool_choice"] = {
                    "type": "function",
                    "function": {"name": tc["name"]},
                }

    return openai_body


# ─── Response conversion: OpenAI → Anthropic (non-stream) ────────────

def openai_to_anthropic_response(openai_resp: dict, client_model: str) -> dict:
    """Convert an OpenAI chat.completion response to Anthropic Messages shape."""
    choices = openai_resp.get("choices", [])
    choice = choices[0] if choices else {}
    message = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    content_blocks = []
    if message.get("content"):
        content_blocks.append({"type": "text", "text": message["content"]})

    for tc in message.get("tool_calls", []) or []:
        try:
            tool_input = json.loads(tc.get("function", {}).get("arguments", "{}"))
        except json.JSONDecodeError:
            tool_input = {}
        content_blocks.append({
            "type": "tool_use",
            "id": tc.get("id", f"toolu_{uuid.uuid4().hex[:12]}"),
            "name": tc.get("function", {}).get("name", ""),
            "input": tool_input,
        })

    # Map OpenAI finish_reason -> Anthropic stop_reason
    stop_reason_map = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
        "function_call": "tool_use",
        "content_filter": "end_turn",
    }
    stop_reason = stop_reason_map.get(finish, "end_turn")

    usage = openai_resp.get("usage", {}) or {}
    return {
        "id": f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": client_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


# ─── Streaming conversion: OpenAI chunks → Anthropic SSE events ──────

class AnthropicStreamConverter:
    """Stateful converter: OpenAI chat.completion.chunk stream → Anthropic SSE.

    Emits the full Anthropic event sequence:
      message_start, content_block_start, content_block_delta*,
      content_block_stop, message_delta, message_stop

    Synergy: OmniClaw openAIChunkToAnthropicEvents state machine.
    """

    def __init__(self, client_model: str):
        self.client_model = client_model
        self.message_id = f"msg_{uuid.uuid4().hex[:24]}"
        self.input_tokens = 0
        self.output_tokens = 0
        self.block_index = 0
        # Track open blocks: index -> type ("text" | "tool_use")
        self.open_blocks = {}
        self.tool_call_names = {}  # tool index -> name
        self.message_started = False
        self.finished = False

    def _ensure_message_start(self):
        if not self.message_started:
            self.message_started = True
            return _sse_event({
                "type": "message_start",
                "message": {
                    "id": self.message_id,
                    "type": "message",
                    "role": "assistant",
                    "model": self.client_model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            })
        return None

    def _open_block(self, index: int, block_type: str, name: str = None):
        """Open a content block (only if not already open)."""
        if index in self.open_blocks:
            return None
        self.open_blocks[index] = block_type
        if block_type == "text":
            block = {"type": "text", "text": ""}
        else:  # tool_use
            block = {
                "type": "tool_use",
                "id": f"toolu_{uuid.uuid4().hex[:12]}",
                "name": name or "",
                "input": {},
            }
        return _sse_event({
            "type": "content_block_start",
            "index": index,
            "content_block": block,
        })

    def _close_block(self, index: int):
        if index not in self.open_blocks:
            return None
        del self.open_blocks[index]
        return _sse_event({"type": "content_block_stop", "index": index})

    def process_chunk(self, chunk: dict):
        """Process one OpenAI chunk; yield Anthropic SSE event strings."""
        events = []

        msg_start = self._ensure_message_start()
        if msg_start:
            events.append(msg_start)

        choices = chunk.get("choices", [])
        if not choices:
            # Usage-only chunk
            if chunk.get("usage"):
                self.output_tokens = chunk["usage"].get("completion_tokens", 0)
                self.input_tokens = chunk["usage"].get("prompt_tokens", 0)
            return events

        choice = choices[0]
        delta = choice.get("delta", {})

        # Text content
        text = delta.get("content")
        if text:
            blk = self.block_index
            open_ev = self._open_block(blk, "text")
            if open_ev:
                events.append(open_ev)
            events.append(_sse_event({
                "type": "content_block_delta",
                "index": blk,
                "delta": {"type": "text_delta", "text": text},
            }))

        # Tool calls (streamed by index)
        for tc in delta.get("tool_calls", []) or []:
            tc_idx = tc.get("index", 0)
            # Map OpenAI tool index to a distinct Anthropic block index
            block_idx = self._block_for_tool(tc_idx)
            fn = tc.get("function", {})
            name = fn.get("name", "")
            args = fn.get("arguments", "")

            open_ev = self._open_block(block_idx, "tool_use", name or None)
            if open_ev:
                events.append(open_ev)
            if name and not self.tool_call_names.get(tc_idx):
                self.tool_call_names[tc_idx] = name
            if args:
                events.append(_sse_event({
                    "type": "content_block_delta",
                    "index": block_idx,
                    "delta": {"type": "input_json_delta", "partial_json": args},
                }))

        # If text block is complete (finish_reason present) close it later
        finish = choice.get("finish_reason")
        if finish:
            # Close text block if open
            if self.block_index in self.open_blocks:
                stop_ev = self._close_block(self.block_index)
                if stop_ev:
                    events.append(stop_ev)
                self.block_index += 1
            # Close any open tool blocks
            for idx in sorted(list(self.open_blocks.keys()), reverse=True):
                stop_ev = self._close_block(idx)
                if stop_ev:
                    events.append(stop_ev)

            stop_reason_map = {
                "stop": "end_turn",
                "length": "max_tokens",
                "tool_calls": "tool_use",
                "function_call": "tool_use",
            }
            events.append(_sse_event({
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason_map.get(finish, "end_turn"),
                          "stop_sequence": None},
                "usage": {"output_tokens": self.output_tokens},
            }))
            events.append(_sse_event({"type": "message_stop"}))
            self.finished = True

        return events

    def _block_for_tool(self, tc_idx: int) -> int:
        """Assign each tool call index a stable Anthropic block index."""
        if not hasattr(self, "_tool_block_map"):
            self._tool_block_map = {}
        if tc_idx not in self._tool_block_map:
            # Reserve: text block uses current block_index; tools get subsequent
            self._tool_block_map[tc_idx] = self.block_index + 1 + len(self._tool_block_map)
        return self._tool_block_map[tc_idx]

    def flush_trailers(self):
        """Emit any missing close events (e.g. upstream ended without finish_reason)."""
        events = []
        if self.message_started and not self.finished:
            for idx in sorted(list(self.open_blocks.keys()), reverse=True):
                stop_ev = self._close_block(idx)
                if stop_ev:
                    events.append(stop_ev)
            events.append(_sse_event({
                "type": "message_delta",
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": self.output_tokens},
            }))
            events.append(_sse_event({"type": "message_stop"}))
            self.finished = True
        return events


def _sse_event(payload: dict) -> str:
    """Format one Anthropic SSE event (event: type + data: json)."""
    etype = payload.get("type", "message")
    return f"event: {etype}\ndata: {json.dumps(payload)}\n\n"


def count_tokens_stub(body: dict) -> dict:
    """Stub count_tokens (matches OmniClaw behavior — upstream has no equivalent)."""
    # Cheap heuristic estimate: ~4 chars per token
    total_chars = 0
    for msg in body.get("messages", []):
        content = msg.get("content", "")
        if isinstance(content, str):
            total_chars += len(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    total_chars += len(block.get("text", ""))
    return {"input_tokens": max(1, total_chars // 4)}
