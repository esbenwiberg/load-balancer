#!/usr/bin/env python3
"""
Tool-calling conformance harness — the gate that earns `agent_capable`.

Drives a model through a real multi-tool coding task (Read -> Edit -> Bash)
UNDER STREAMING and measures how reliably it emits *clean, structured* tool
calls. Emits pass/fail + a tool-call-error-rate.

Three transports:
  --api chat       OpenAI Chat Completions (vLLM Spark direct, or LiteLLM).
  --api responses  OpenAI Responses API — the endpoint Codex speaks. Point this
                   at LiteLLM to exercise the Responses->ChatCompletions bridge
                   end-to-end (Blocker A / docs/03 risk 4).
  --api anthropic  Anthropic Messages API — Claude Code's REAL path (/v1/messages
                   with tools, streaming). Point this at LiteLLM to exercise the
                   anthropic->chat translation both ways (tools -> tool_use ->
                   tool_result). This is our single biggest client surface.

Plus two single-turn probes for known failure modes:
  * parallel tool calls (LiteLLM bridge index-collision bug #21331, doc 04)
  * tool_choice:"required" (Qwen3 + reasoning HTTP-400, doc 04)

Why this exists: see docs/04-tool-calling.md. A model can chat fine and still be
useless as a coding-agent backend because its tool calls leak into plain text,
carry malformed JSON, name tools that don't exist, or run away (`!!!!`).

Nothing here writes to disk or shells out — the "filesystem" and "bash" are a
virtual workspace (scenarios.py). Only the model endpoint is live.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field

from scenarios import (
    ANTHROPIC_TOOLS,
    PARALLEL_PROBE_PROMPT,
    RESPONSES_TOOLS,
    SYSTEM_PROMPT,
    TASK_PROMPT,
    TOOL_CHOICE_PROBE_PROMPT,
    TOOL_NAMES,
    TOOLS,
    Grade,
    VirtualEnv,
)

# `openai` (chat/responses) and `httpx` (anthropic) are imported lazily inside
# the transports / main() so the offline self-test — which drives the parse
# helpers and a fake transport directly — can run without either installed.


# --- Leaked / degenerate tool-call detection --------------------------------
# When the serving engine has the wrong tool-call parser or chat template, the
# tool call comes back as raw text in `content` instead of structured
# `tool_calls`. These patterns catch the common leak shapes plus runaway output.

_LEAK_PATTERNS = [
    (r"<tool_call>", "hermes/qwen <tool_call> tag in content"),
    (r"</tool_call>", "hermes/qwen </tool_call> tag in content"),
    (r"<\|tool_call\|>", "special <|tool_call|> token leaked as text"),
    (r"<function[ =][^>]*>", "<function=...> call leaked as text"),
    (r"<function_call>", "<function_call> tag in content"),
    (r"\bfunctools\[", "llama functools[ ...] call leaked as text"),
    (r"<\|python_tag\|>", "llama <|python_tag|> leaked as text"),
]

# A JSON-ish blob in content that names one of our tools == a leaked call.
_TOOLNAME_ALTS = "|".join(re.escape(n) for n in TOOL_NAMES)


def detect_content_toolcall_leak(content: str) -> str | None:
    """Return a reason string if `content` looks like it contains a tool call
    that should have been structured, else None."""
    if not content:
        return None
    for pattern, reason in _LEAK_PATTERNS:
        if re.search(pattern, content, re.IGNORECASE):
            return reason
    # JSON object that both names a known tool and looks like a call payload.
    if re.search(r"[{\[]", content) and re.search(
        r'"(?:name|function|tool)"\s*:', content
    ):
        if re.search(rf'"({_TOOLNAME_ALTS})"', content) and re.search(
            r'"(?:arguments|parameters|input)"\s*:', content
        ):
            return "JSON tool-call payload leaked into content"
    return None


def detect_runaway(content: str, cap: int = 20000) -> str | None:
    """Detect degenerate generation (e.g. the Qwen `!!!!!!` infinite stream)."""
    if not content:
        return None
    m = re.search(r"(.)\1{40,}", content)
    if m:
        return f"runaway repetition of {m.group(1)!r}"
    if len(content) > cap and len(set(content)) < 12:
        return "runaway low-entropy generation"
    return None


# --- Normalized model turn --------------------------------------------------


@dataclass
class ToolCall:
    id: str
    name: str
    arguments_raw: str  # as returned by the model (may be malformed)


@dataclass
class AssistantTurn:
    content: str
    tool_calls: list  # list[ToolCall]
    finish_reason: str | None


# --- Transports -------------------------------------------------------------
# A transport owns the conversation state in its native wire format and knows
# how to (a) fetch the next assistant turn (normalized to AssistantTurn) and
# (b) append tool results. The run loop is protocol-agnostic.


class ChatTransport:
    """OpenAI Chat Completions. What vLLM speaks; LiteLLM exposes it too."""

    name = "chat"

    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.messages: list = []

    def reset(self, system: str, user: str) -> None:
        self.messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]

    def get_turn(self, stream, temperature, tool_choice="auto") -> AssistantTurn:
        if stream:
            turn = self._stream(temperature, tool_choice)
        else:
            turn = self._block(temperature, tool_choice)
        return turn

    def record_assistant(self, turn: AssistantTurn) -> None:
        self.messages.append(
            {
                "role": "assistant",
                "content": turn.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments_raw},
                    }
                    for tc in turn.tool_calls
                ]
                or None,
            }
        )

    def record_tool_result(self, call_id, name, content) -> None:
        self.messages.append(
            {"role": "tool", "tool_call_id": call_id, "content": content}
        )

    def _stream(self, temperature, tool_choice) -> AssistantTurn:
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=True,
        )
        parts: list[str] = []
        acc: dict[int, dict] = {}
        finish = None
        for chunk in stream:
            if not chunk.choices:
                continue
            ch = chunk.choices[0]
            if ch.finish_reason:
                finish = ch.finish_reason
            d = ch.delta
            if getattr(d, "content", None):
                parts.append(d.content)
            for tc in getattr(d, "tool_calls", None) or []:
                slot = acc.setdefault(
                    tc.index, {"id": None, "name": "", "arguments": ""}
                )
                if tc.id:
                    slot["id"] = tc.id
                fn = getattr(tc, "function", None)
                if fn is not None:
                    if getattr(fn, "name", None):
                        slot["name"] = fn.name
                    if getattr(fn, "arguments", None):
                        slot["arguments"] += fn.arguments
        calls = [
            ToolCall(slot["id"] or f"call_{i}", slot["name"], slot["arguments"])
            for i, slot in sorted(acc.items())
        ]
        return AssistantTurn("".join(parts), calls, finish)

    def _block(self, temperature, tool_choice) -> AssistantTurn:
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=TOOLS,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=False,
        )
        ch = resp.choices[0]
        calls = [
            ToolCall(
                tc.id or f"call_{i}", tc.function.name, tc.function.arguments or ""
            )
            for i, tc in enumerate(ch.message.tool_calls or [])
        ]
        return AssistantTurn(ch.message.content or "", calls, ch.finish_reason)


class ResponsesTransport:
    """OpenAI Responses API — the endpoint Codex requires (wire_api=responses).

    Kept stateless (resends the full `input` list each turn instead of using
    previous_response_id) so it behaves like a translating proxy would and stays
    deterministic. Assistant tool calls are `function_call` items; results go
    back as `function_call_output` items.
    """

    name = "responses"

    def __init__(self, client, model):
        self.client = client
        self.model = model
        self.instructions = ""
        self.input: list = []

    def reset(self, system: str, user: str) -> None:
        self.instructions = system
        self.input = [{"role": "user", "content": user}]

    def get_turn(self, stream, temperature, tool_choice="auto") -> AssistantTurn:
        if stream:
            return self._stream(temperature, tool_choice)
        return self._block(temperature, tool_choice)

    def record_assistant(self, turn: AssistantTurn) -> None:
        if turn.content:
            self.input.append({"role": "assistant", "content": turn.content})
        for tc in turn.tool_calls:
            self.input.append(
                {
                    "type": "function_call",
                    "call_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments_raw,
                }
            )

    def record_tool_result(self, call_id, name, content) -> None:
        self.input.append(
            {"type": "function_call_output", "call_id": call_id, "output": content}
        )

    def _create(self, temperature, tool_choice, stream):
        return self.client.responses.create(
            model=self.model,
            input=self.input,
            instructions=self.instructions,
            tools=RESPONSES_TOOLS,
            tool_choice=tool_choice,
            temperature=temperature,
            stream=stream,
        )

    def _stream(self, temperature, tool_choice) -> AssistantTurn:
        events = self._create(temperature, tool_choice, stream=True)
        text_parts: list[str] = []
        acc: dict[str, dict] = {}  # keyed by output index
        order: list[str] = []
        finish = None
        for ev in events:
            etype = getattr(ev, "type", "")
            if etype == "response.output_text.delta":
                text_parts.append(getattr(ev, "delta", "") or "")
            elif etype == "response.output_item.added":
                item = getattr(ev, "item", None)
                if item is not None and getattr(item, "type", "") == "function_call":
                    key = str(getattr(ev, "output_index", len(order)))
                    acc[key] = {
                        "id": getattr(item, "call_id", None)
                        or getattr(item, "id", None),
                        "name": getattr(item, "name", "") or "",
                        "arguments": getattr(item, "arguments", "") or "",
                    }
                    order.append(key)
            elif etype == "response.function_call_arguments.delta":
                key = str(getattr(ev, "output_index", order[-1] if order else "0"))
                slot = acc.setdefault(key, {"id": None, "name": "", "arguments": ""})
                slot["arguments"] += getattr(ev, "delta", "") or ""
                if key not in order:
                    order.append(key)
            elif etype in (
                "response.completed",
                "response.incomplete",
                "response.failed",
            ):
                finish = etype.split(".")[-1]
        calls = [
            ToolCall(acc[k]["id"] or f"call_{i}", acc[k]["name"], acc[k]["arguments"])
            for i, k in enumerate(order)
        ]
        return AssistantTurn("".join(text_parts), calls, finish)

    def _block(self, temperature, tool_choice) -> AssistantTurn:
        resp = self._create(temperature, tool_choice, stream=False)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, item in enumerate(getattr(resp, "output", []) or []):
            itype = getattr(item, "type", "")
            if itype == "function_call":
                calls.append(
                    ToolCall(
                        getattr(item, "call_id", None) or f"call_{i}",
                        getattr(item, "name", "") or "",
                        getattr(item, "arguments", "") or "",
                    )
                )
            elif itype == "message":
                for c in getattr(item, "content", []) or []:
                    if getattr(c, "type", "") in ("output_text", "text"):
                        text_parts.append(getattr(c, "text", "") or "")
        return AssistantTurn("".join(text_parts), calls, getattr(resp, "status", None))


class AnthropicTransport:
    """Anthropic Messages API (/v1/messages) — Claude Code's REAL path, with
    tools and streaming.

    Targets the GATEWAY's /v1/messages (not a backend directly): LiteLLM
    translates anthropic->chat toward the backend and back, so this exercises
    the full Anthropic tool-call round-trip — request `tools` (input_schema
    shape) -> assistant `tool_use` blocks -> `tool_result` blocks fed back — in
    BOTH directions. Constraint (goal 7): mockd is unchanged; it just sees a
    normal chat request with OpenAI tools after the gateway translates.

    Deliberately raw httpx with Bearer auth + anthropic-version, matching the
    proven e2e client path (test_e2e.py) rather than pulling in the anthropic
    SDK: no new dependency, and identical auth to the rest of the suite. The
    wire parsing is factored into pure helpers (_parse_stream_events /
    _parse_block_response) so selftest.py can cover it offline.
    """

    name = "anthropic"

    # Anthropic requires max_tokens. mockd's scripted turns are tiny; keep it
    # generous so a real backend behind the gateway wouldn't be truncated either.
    def __init__(self, base_url, api_key, model, max_tokens=1024):
        # base_url is the OpenAI-style base (…/v1). The Messages endpoint is
        # <base>/messages — e.g. http://host:4000/v1/messages, the exact URL the
        # e2e suite drives.
        self.url = base_url.rstrip("/") + "/messages"
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.system = ""
        self.messages: list = []
        self._client = None  # lazy httpx.Client

    def reset(self, system: str, user: str) -> None:
        self.system = system
        self.messages = [{"role": "user", "content": user}]

    def _http(self):
        if self._client is None:
            import httpx  # lazy: only when actually hitting the wire

            self._client = httpx.Client(timeout=60.0)
        return self._client

    def _headers(self):
        return {
            "Authorization": "Bearer " + self.api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }

    def _payload(self, temperature, tool_choice, stream):
        payload = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "messages": self.messages,
            "tools": ANTHROPIC_TOOLS,
            "temperature": temperature,
            "stream": stream,
        }
        # OpenAI tool_choice -> Anthropic tool_choice. "required" == "any".
        if tool_choice == "required":
            payload["tool_choice"] = {"type": "any"}
        elif tool_choice == "auto":
            payload["tool_choice"] = {"type": "auto"}
        return payload

    def get_turn(self, stream, temperature, tool_choice="auto") -> AssistantTurn:
        payload = self._payload(temperature, tool_choice, stream)
        if stream:
            return self._stream(payload)
        return self._block(payload)

    def record_assistant(self, turn: AssistantTurn) -> None:
        blocks: list = []
        if turn.content:
            blocks.append({"type": "text", "text": turn.content})
        for tc in turn.tool_calls:
            # Anthropic `input` must be a JSON object. If the model emitted
            # malformed args (only under injected faults — never in the clean
            # scenario), fall back to {} so the request stays well-formed; the
            # error is already recorded against the tool call by the run loop.
            try:
                inp = json.loads(tc.arguments_raw) if tc.arguments_raw.strip() else {}
                if not isinstance(inp, dict):
                    inp = {}
            except (json.JSONDecodeError, ValueError):
                inp = {}
            blocks.append(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": inp}
            )
        self.messages.append({"role": "assistant", "content": blocks})

    def record_tool_result(self, call_id, name, content) -> None:
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": call_id,
                        "content": content,
                    }
                ],
            }
        )

    def _block(self, payload) -> AssistantTurn:
        resp = self._http().post(self.url, headers=self._headers(), json=payload)
        resp.raise_for_status()
        return self._parse_block_response(resp.json())

    def _stream(self, payload) -> AssistantTurn:
        with self._http().stream(
            "POST", self.url, headers=self._headers(), json=payload
        ) as resp:
            resp.raise_for_status()
            return self._parse_stream_events(_iter_sse_data(resp.iter_lines()))

    # --- pure parse helpers (offline-testable) -----------------------------
    @staticmethod
    def _parse_block_response(body: dict) -> AssistantTurn:
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, block in enumerate(body.get("content", []) or []):
            btype = block.get("type")
            if btype == "text":
                text_parts.append(block.get("text", "") or "")
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        block.get("id") or f"call_{i}",
                        block.get("name", "") or "",
                        json.dumps(block.get("input", {}) or {}),
                    )
                )
        return AssistantTurn("".join(text_parts), calls, body.get("stop_reason"))

    @staticmethod
    def _parse_stream_events(events) -> AssistantTurn:
        """Fold Anthropic streaming events (parsed `data:` dicts) into a turn.

        Accumulates per content-block index: text_delta -> content, tool_use +
        input_json_delta -> a ToolCall whose arguments are the concatenated
        partial_json. stop_reason (message_delta) becomes finish_reason.
        """
        blocks: dict = {}  # index -> {"kind","text","id","name","args"}
        order: list = []
        finish = None
        for ev in events:
            etype = ev.get("type")
            if etype == "content_block_start":
                idx = ev.get("index", len(order))
                cb = ev.get("content_block", {}) or {}
                if cb.get("type") == "tool_use":
                    blocks[idx] = {
                        "kind": "tool_use",
                        "id": cb.get("id"),
                        "name": cb.get("name", "") or "",
                        "args": "",
                    }
                else:
                    blocks[idx] = {"kind": "text", "text": cb.get("text", "") or ""}
                if idx not in order:
                    order.append(idx)
            elif etype == "content_block_delta":
                idx = ev.get("index")
                delta = ev.get("delta", {}) or {}
                slot = blocks.setdefault(idx, {"kind": "text", "text": ""})
                if idx not in order:
                    order.append(idx)
                dtype = delta.get("type")
                if dtype == "text_delta":
                    slot.setdefault("text", "")
                    slot["text"] += delta.get("text", "") or ""
                elif dtype == "input_json_delta":
                    slot.setdefault("args", "")
                    slot["args"] += delta.get("partial_json", "") or ""
            elif etype == "message_delta":
                sr = (ev.get("delta", {}) or {}).get("stop_reason")
                if sr:
                    finish = sr
            elif etype in ("message_stop", "error"):
                if etype == "error":
                    finish = "error"
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for i, idx in enumerate(order):
            slot = blocks[idx]
            if slot.get("kind") == "tool_use":
                calls.append(
                    ToolCall(
                        slot.get("id") or f"call_{i}",
                        slot.get("name", "") or "",
                        slot.get("args", ""),
                    )
                )
            else:
                text_parts.append(slot.get("text", "") or "")
        return AssistantTurn("".join(text_parts), calls, finish)


def _iter_sse_data(lines):
    """Yield parsed JSON objects from the `data:` lines of an SSE stream,
    skipping event: lines, blanks, and the [DONE] sentinel."""
    for line in lines:
        if not line or not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            yield json.loads(payload)
        except (json.JSONDecodeError, ValueError):
            continue


def make_transport(api: str, client, model, base_url=None, api_key=None):
    if api == "anthropic":
        return AnthropicTransport(base_url, api_key, model)
    if api == "responses":
        return ResponsesTransport(client, model)
    return ChatTransport(client, model)


# --- Argument validation ----------------------------------------------------


def _validate_args(name: str, raw: str) -> tuple[dict | None, str | None]:
    """Parse + schema-check tool-call arguments. Returns (args, error_reason)."""
    try:
        args = json.loads(raw) if raw.strip() else {}
    except (json.JSONDecodeError, ValueError):
        return None, "invalid_json_args"
    if not isinstance(args, dict):
        return None, "invalid_json_args"
    schema = next(t["function"] for t in TOOLS if t["function"]["name"] == name)
    required = schema["parameters"].get("required", [])
    missing = [k for k in required if k not in args]
    if missing:
        return args, "missing_required_arg"
    return args, None


# --- Per-run result ---------------------------------------------------------


@dataclass
class RunMetrics:
    turns: int = 0
    structured_calls: int = 0
    valid_calls: int = 0
    leaked_in_content: int = 0
    invalid_json_args: int = 0
    unknown_tool: int = 0
    missing_required_arg: int = 0
    runaway: int = 0
    api_error: int = 0
    tool_errors_fed: int = 0  # tool results we returned as "error: ..."
    recovered: bool = False  # model finished the task despite a tool error
    error_detail: list = field(default_factory=list)
    grade: dict = field(default_factory=dict)
    completed: bool = False

    @property
    def attempts(self) -> int:
        return self.structured_calls + self.leaked_in_content + self.runaway

    @property
    def errored(self) -> int:
        return (
            self.leaked_in_content
            + self.invalid_json_args
            + self.unknown_tool
            + self.missing_required_arg
            + self.runaway
        )

    @property
    def error_rate(self) -> float:
        return self.errored / self.attempts if self.attempts else 0.0


def run_once(transport, temperature, max_turns, stream, verbose) -> RunMetrics:
    env = VirtualEnv()
    grade = Grade()
    m = RunMetrics()
    transport.reset(SYSTEM_PROMPT, TASK_PROMPT)
    saw_tool_error = False

    for _ in range(max_turns):
        m.turns += 1
        try:
            turn = transport.get_turn(stream, temperature)
        except Exception as exc:  # network / 400 from a broken parser config, etc.
            m.api_error += 1
            m.error_detail.append(f"api_error: {type(exc).__name__}: {exc}")
            break

        runaway_reason = detect_runaway(turn.content)
        if runaway_reason:
            m.runaway += 1
            m.error_detail.append(runaway_reason)
            if verbose:
                print(f"    [runaway] {runaway_reason}")
            break

        if turn.tool_calls:
            transport.record_assistant(turn)
            for tc in turn.tool_calls:
                m.structured_calls += 1
                if tc.name not in TOOL_NAMES:
                    m.unknown_tool += 1
                    m.error_detail.append(f"unknown_tool: {tc.name!r}")
                    transport.record_tool_result(
                        tc.id, tc.name, f"error: unknown tool {tc.name}"
                    )
                    if verbose:
                        print(f"    [unknown_tool] {tc.name!r}")
                    continue
                args, err = _validate_args(tc.name, tc.arguments_raw)
                if err == "invalid_json_args":
                    m.invalid_json_args += 1
                    m.error_detail.append(
                        f"invalid_json_args for {tc.name}: {tc.arguments_raw[:120]!r}"
                    )
                    transport.record_tool_result(
                        tc.id, tc.name, "error: arguments were not valid JSON"
                    )
                    if verbose:
                        print(
                            f"    [invalid_json] {tc.name}: {tc.arguments_raw[:80]!r}"
                        )
                    continue
                if err == "missing_required_arg":
                    m.missing_required_arg += 1
                    m.error_detail.append(f"missing_required_arg for {tc.name}: {args}")
                    transport.record_tool_result(
                        tc.id,
                        tc.name,
                        f"error: missing required argument(s) for {tc.name}",
                    )
                    if verbose:
                        print(f"    [missing_arg] {tc.name}: {args}")
                    continue
                # Clean, structured, valid call. Grade + execute against the env.
                m.valid_calls += 1
                grade.observe_call(tc.name, args, env)
                result = env.dispatch(tc.name, args)
                if result.startswith("error:"):
                    m.tool_errors_fed += 1
                    saw_tool_error = True
                transport.record_tool_result(tc.id, tc.name, result)
                if verbose:
                    print(f"    [ok] {tc.name}({_short(args)}) -> {result[:60]!r}")
            continue

        # No structured tool calls this turn.
        leak = detect_content_toolcall_leak(turn.content)
        if leak:
            m.leaked_in_content += 1
            m.error_detail.append(f"leaked_in_content: {leak}")
            if verbose:
                print(f"    [leak] {leak}")
            break  # a leaking model will keep leaking; the run is a fail.

        grade.produced_final_text = True
        if verbose:
            print(f"    [final] {turn.content[:100]!r}")
        break

    m.grade = grade.summary()
    m.completed = grade.task_completed
    # Recovery: task completed even though a tool call errored along the way.
    m.recovered = m.completed and saw_tool_error
    return m


def _short(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:20]!r}" for k, v in args.items())


# --- Single-turn probes -----------------------------------------------------


def probe_parallel(transport, stream, temperature, verbose) -> dict:
    """One turn inviting two tool calls. Not a hard gate (a model that reads
    serially is fine), BUT if it *does* parallelize, the calls must survive with
    distinct ids and valid, known names — the bridge #21331 bug collapses them.
    """
    transport.reset(SYSTEM_PROMPT, PARALLEL_PROBE_PROMPT)
    try:
        turn = transport.get_turn(stream, temperature)
    except Exception as exc:
        return {"ran": False, "error": f"{type(exc).__name__}: {exc}"}
    ids = [tc.id for tc in turn.tool_calls]
    names_ok = all(tc.name in TOOL_NAMES for tc in turn.tool_calls)
    args_ok = all(
        _validate_args(tc.name, tc.arguments_raw)[1] is None
        for tc in turn.tool_calls
        if tc.name in TOOL_NAMES
    )
    distinct_ids = len(set(ids)) == len(ids)
    parallelized = len(turn.tool_calls) >= 2
    # A defect only if it parallelized AND something is wrong with the calls.
    defect = parallelized and not (distinct_ids and names_ok and args_ok)
    if verbose:
        print(
            f"  [parallel probe] calls={len(turn.tool_calls)} distinct_ids={distinct_ids} "
            f"names_ok={names_ok} args_ok={args_ok}"
        )
    return {
        "ran": True,
        "num_calls": len(turn.tool_calls),
        "parallelized": parallelized,
        "distinct_ids": distinct_ids,
        "names_ok": names_ok,
        "args_ok": args_ok,
        "defect": defect,
    }


def probe_tool_choice_required(transport, stream, temperature, verbose) -> dict:
    """Force a tool call. Catches the Qwen3 + reasoning + tool_choice:required
    HTTP-400 (doc 04), and whether the model honors 'required' at all."""
    transport.reset(SYSTEM_PROMPT, TOOL_CHOICE_PROBE_PROMPT)
    try:
        turn = transport.get_turn(stream, temperature, tool_choice="required")
    except Exception as exc:
        if verbose:
            print(f"  [tool_choice=required] HTTP/API error: {exc}")
        return {
            "ran": True,
            "http_error": f"{type(exc).__name__}: {exc}",
            "honored": False,
            "defect": True,
        }
    honored = len(turn.tool_calls) >= 1 and all(
        tc.name in TOOL_NAMES for tc in turn.tool_calls
    )
    if verbose:
        print(
            f"  [tool_choice=required] honored={honored} calls={len(turn.tool_calls)}"
        )
    return {"ran": True, "http_error": None, "honored": honored, "defect": not honored}


# --- Aggregation ------------------------------------------------------------


def aggregate(
    runs: list, fail_threshold: float, min_completion: float, probes: dict | None = None
) -> dict:
    total_attempts = sum(r.attempts for r in runs)
    total_errored = sum(r.errored for r in runs)
    total_api_errors = sum(r.api_error for r in runs)
    overall_rate = total_errored / total_attempts if total_attempts else 0.0
    completions = sum(1 for r in runs if r.completed)
    completion_rate = completions / len(runs) if runs else 0.0

    probes = probes or {}
    probe_defect = bool(probes.get("parallel", {}).get("defect")) or bool(
        probes.get("tool_choice_required", {}).get("defect")
    )

    agent_capable = (
        overall_rate <= fail_threshold
        and completion_rate >= min_completion
        and total_api_errors == 0
        and not probe_defect
    )
    return {
        "runs": len(runs),
        "tool_call_error_rate": round(overall_rate, 4),
        "fail_threshold": fail_threshold,
        "task_completion_rate": round(completion_rate, 4),
        "min_completion": min_completion,
        "total_tool_call_attempts": total_attempts,
        "total_errored": total_errored,
        "total_api_errors": total_api_errors,
        "recoveries": sum(1 for r in runs if r.recovered),
        "error_breakdown": {
            "leaked_in_content": sum(r.leaked_in_content for r in runs),
            "invalid_json_args": sum(r.invalid_json_args for r in runs),
            "unknown_tool": sum(r.unknown_tool for r in runs),
            "missing_required_arg": sum(r.missing_required_arg for r in runs),
            "runaway": sum(r.runaway for r in runs),
            "api_error": total_api_errors,
        },
        "probes": probes,
        "agent_capable": agent_capable,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Tool-calling conformance harness (sets agent_capable).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--base-url",
        default=os.environ.get("CONFORMANCE_BASE_URL", "http://localhost:8000/v1"),
        help="OpenAI-compatible base URL (vLLM Spark, or LiteLLM in front of it).",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("CONFORMANCE_API_KEY", "dummy"),
        help="API key/token. vLLM often ignores it; LiteLLM wants a virtual key.",
    )
    p.add_argument(
        "--model",
        default=os.environ.get("CONFORMANCE_MODEL"),
        help="Model name/alias to target (required).",
    )
    p.add_argument(
        "--api",
        choices=["chat", "responses", "anthropic"],
        default=os.environ.get("CONFORMANCE_API", "chat"),
        help=(
            "Wire protocol. 'responses' targets /v1/responses (the Codex path); "
            "'anthropic' targets /v1/messages (Claude Code's path, tools+stream)."
        ),
    )
    p.add_argument(
        "--runs",
        type=int,
        default=int(os.environ.get("CONFORMANCE_RUNS", "5")),
        help="Repeat the scenario N times for a stable error rate.",
    )
    p.add_argument(
        "--max-turns", type=int, default=12, help="Max assistant turns per run."
    )
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument(
        "--fail-threshold",
        type=float,
        default=0.02,
        help="Max tool-call-error-rate to still count as agent_capable.",
    )
    p.add_argument(
        "--min-completion",
        type=float,
        default=0.8,
        help="Min fraction of runs that must complete the task.",
    )
    no_stream_default = os.environ.get("CONFORMANCE_NO_STREAM") == "1"
    p.add_argument(
        "--no-stream",
        action="store_true",
        default=no_stream_default,
        help="Disable streaming (streaming is the realistic path — keep it on).",
    )
    p.add_argument(
        "--no-probes",
        action="store_true",
        help="Skip the parallel + tool_choice:required probes.",
    )
    p.add_argument("--json-out", help="Write the full JSON report to this path.")
    p.add_argument("-v", "--verbose", action="store_true", help="Print each tool call.")
    args = p.parse_args()

    if not args.model:
        p.error("--model (or CONFORMANCE_MODEL) is required.")

    # chat/responses need the OpenAI SDK; anthropic uses httpx (built lazily in
    # the transport). Only require what this run actually uses.
    client = None
    if args.api in ("chat", "responses"):
        try:
            from openai import OpenAI
        except ImportError:
            sys.exit(
                "The 'openai' package is required. Install it:\n    pip install -r requirements.txt"
            )
        client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    stream = not args.no_stream

    print(
        f"Conformance: model={args.model!r} base_url={args.base_url!r} "
        f"api={args.api} stream={stream} runs={args.runs}"
    )
    print("-" * 72)

    runs: list[RunMetrics] = []
    for i in range(args.runs):
        print(f"Run {i + 1}/{args.runs}:")
        transport = make_transport(
            args.api, client, args.model, args.base_url, args.api_key
        )
        m = run_once(transport, args.temperature, args.max_turns, stream, args.verbose)
        runs.append(m)
        print(
            f"  turns={m.turns} valid_calls={m.valid_calls} errored={m.errored} "
            f"completed={m.completed} run_error_rate={m.error_rate:.3f}"
        )

    probes = {}
    if not args.no_probes:
        print("Probes:")
        probes["parallel"] = probe_parallel(
            make_transport(args.api, client, args.model, args.base_url, args.api_key),
            stream,
            args.temperature,
            True,
        )
        probes["tool_choice_required"] = probe_tool_choice_required(
            make_transport(args.api, client, args.model, args.base_url, args.api_key),
            stream,
            args.temperature,
            True,
        )

    summary = aggregate(runs, args.fail_threshold, args.min_completion, probes)
    report = {
        "model": args.model,
        "base_url": args.base_url,
        "api": args.api,
        "streaming": stream,
        "summary": summary,
        "runs": [asdict(r) for r in runs],
    }

    print("=" * 72)
    print(json.dumps(summary, indent=2))
    verdict = (
        "PASS  agent_capable=true"
        if summary["agent_capable"]
        else "FAIL  agent_capable=false"
    )
    print("=" * 72)
    print(verdict)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Full report -> {args.json_out}")

    return 0 if summary["agent_capable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
