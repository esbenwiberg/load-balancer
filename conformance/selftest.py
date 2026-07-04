#!/usr/bin/env python3
"""
Offline self-test for the conformance harness.

No network, no model, no `openai` dep. Feeds scripted `AssistantTurn`s through
the real grading / detection / probe path via a fake transport, to prove:
  - a well-behaved model PASSES and finishes the task;
  - each failure mode (leak, bad JSON, unknown tool, runaway) is caught;
  - recovery after a tool-level error is tracked;
  - the parallel and tool_choice:required probes flag real defects and drive
    agent_capable=false.

    python selftest.py
"""

from __future__ import annotations

import sys

import json

from conformance import (
    AnthropicTransport,
    AssistantTurn,
    ToolCall,
    _iter_sse_data,
    aggregate,
    detect_content_toolcall_leak,
    probe_parallel,
    probe_tool_choice_required,
    run_once,
)
from scenarios import CONFIG_PATH


class FakeTransport:
    """Replays a scripted list of AssistantTurns; ignores wire serialization."""

    name = "fake"

    def __init__(self, script, raise_on_turn=False):
        self._script = list(script)
        self._raise = raise_on_turn

    def reset(self, system, user):
        pass

    def get_turn(self, stream, temperature, tool_choice="auto"):
        if self._raise:
            raise RuntimeError("simulated HTTP 400 from broken parser config")
        if not self._script:
            return AssistantTurn("done.", [], "stop")
        return self._script.pop(0)

    def record_assistant(self, turn):
        pass

    def record_tool_result(self, call_id, name, content):
        pass


def _tc(name, args_json, cid="c1"):
    return ToolCall(id=cid, name=name, arguments_raw=args_json)


# --- Scenario scripts -------------------------------------------------------


def s_happy():
    return [
        AssistantTurn(
            "", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"
        ),
        AssistantTurn(
            "",
            [
                _tc(
                    "edit_file",
                    f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 8000", "new_string": "PORT = 9000"}}',
                )
            ],
            "tool_calls",
        ),
        AssistantTurn("", [_tc("run_bash", '{"command": "pytest -q"}')], "tool_calls"),
        AssistantTurn("Done — PORT is 9000 and tests pass.", [], "stop"),
    ]


def s_recovery():
    # First edit uses a wrong old_string -> env returns "error:"; model recovers.
    return [
        AssistantTurn(
            "", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"
        ),
        AssistantTurn(
            "",
            [
                _tc(
                    "edit_file",
                    f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 7000", "new_string": "PORT = 9000"}}',
                )
            ],
            "tool_calls",
        ),
        AssistantTurn(
            "",
            [
                _tc(
                    "edit_file",
                    f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 8000", "new_string": "PORT = 9000"}}',
                )
            ],
            "tool_calls",
        ),
        AssistantTurn("", [_tc("run_bash", '{"command": "pytest -q"}')], "tool_calls"),
        AssistantTurn("Recovered — tests pass.", [], "stop"),
    ]


def s_leak():
    return [
        AssistantTurn(
            '<tool_call>\n{"name": "read_file", "arguments": {"path": "app/config.py"}}\n</tool_call>',
            [],
            "stop",
        )
    ]


def s_bad_json():
    return [
        AssistantTurn("", [_tc("read_file", '{"path": app/config.py}')], "tool_calls")
    ]


def s_unknown_tool():
    return [AssistantTurn("", [_tc("grep_files", '{"pattern": "PORT"}')], "tool_calls")]


def s_runaway():
    return [AssistantTurn("!" * 200, [], "stop")]


def _run(script):
    m = run_once(
        FakeTransport(script), temperature=0.0, max_turns=12, stream=True, verbose=False
    )
    return m, aggregate([m], 0.02, 0.8)


# --- Anthropic transport wire parsing (offline, no network) -----------------
# The `anthropic` transport is the only transport with bespoke wire handling not
# exercised by the fake-transport run loop. These prove its Anthropic-Messages
# serialization (tool_use / tool_result blocks) and streaming/block parsing
# without a gateway, so a translation regression is caught in the fast tier.


def _check_anthropic(fails):
    at = AnthropicTransport("http://gw/v1", "sk-test", "qwen3-coder")

    # SSE data extraction: skip event:/blank/[DONE], parse data: JSON.
    lines = [
        "event: content_block_start",
        'data: {"type": "content_block_start", "index": 0}',
        "",
        "data: [DONE]",
    ]
    got = list(_iter_sse_data(lines))
    if got != [{"type": "content_block_start", "index": 0}]:
        fails.append(f"anthropic _iter_sse_data: got {got}")

    # Streaming: a tool_use block whose args arrive across two input_json_delta
    # chunks -> one clean ToolCall with reassembled JSON.
    stream_toolcall = [
        {"type": "message_start", "message": {}},
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "tool_use", "id": "toolu_1", "name": "read_file"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '{"path": '},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": '"app/config.py"}'},
        },
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "tool_use"}},
        {"type": "message_stop"},
    ]
    turn = AnthropicTransport._parse_stream_events(stream_toolcall)
    if not (
        len(turn.tool_calls) == 1
        and turn.tool_calls[0].id == "toolu_1"
        and turn.tool_calls[0].name == "read_file"
        and json.loads(turn.tool_calls[0].arguments_raw) == {"path": "app/config.py"}
        and turn.content == ""
        and turn.finish_reason == "tool_use"
    ):
        fails.append(f"anthropic stream tool_use parse: got {turn}")

    # Streaming: a plain-text final turn -> content, no calls.
    stream_text = [
        {
            "type": "content_block_start",
            "index": 0,
            "content_block": {"type": "text", "text": ""},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "Done — PORT is 9000"},
        },
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " and tests pass."},
        },
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
    ]
    turn = AnthropicTransport._parse_stream_events(stream_text)
    if not (
        turn.content == "Done — PORT is 9000 and tests pass." and not turn.tool_calls
    ):
        fails.append(f"anthropic stream text parse: got {turn}")

    # A leaked tool call arriving as TEXT (wrong parser upstream) must survive
    # into content so detect_content_toolcall_leak can flag it downstream.
    stream_leak = [
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {
                "type": "text_delta",
                "text": '<tool_call>{"name": "read_file"}</tool_call>',
            },
        }
    ]
    turn = AnthropicTransport._parse_stream_events(stream_leak)
    if not (not turn.tool_calls and detect_content_toolcall_leak(turn.content)):
        fails.append(f"anthropic stream leak-in-text parse: got {turn}")

    # Non-streaming block response: text + tool_use in the content array.
    body = {
        "content": [
            {"type": "text", "text": "reading"},
            {
                "type": "tool_use",
                "id": "toolu_2",
                "name": "edit_file",
                "input": {"path": "app/config.py", "old_string": "8000"},
            },
        ],
        "stop_reason": "tool_use",
    }
    turn = AnthropicTransport._parse_block_response(body)
    if not (
        turn.content == "reading"
        and len(turn.tool_calls) == 1
        and turn.tool_calls[0].name == "edit_file"
        and json.loads(turn.tool_calls[0].arguments_raw)["path"] == "app/config.py"
    ):
        fails.append(f"anthropic block parse: got {turn}")

    # record_assistant serializes tool_use blocks with a real dict `input`;
    # record_tool_result serializes a tool_result user message.
    at.reset("sys", "do the thing")
    at.record_assistant(
        AssistantTurn(
            "",
            [ToolCall("toolu_3", "read_file", '{"path": "app/config.py"}')],
            "tool_use",
        )
    )
    at.record_tool_result("toolu_3", "read_file", "PORT = 8000")
    asst, tool_msg = at.messages[-2], at.messages[-1]
    tu = asst["content"][0]
    tr = tool_msg["content"][0]
    if not (
        asst["role"] == "assistant"
        and tu["type"] == "tool_use"
        and tu["input"] == {"path": "app/config.py"}
        and tool_msg["role"] == "user"
        and tr["type"] == "tool_result"
        and tr["tool_use_id"] == "toolu_3"
        and tr["content"] == "PORT = 8000"
    ):
        fails.append(f"anthropic serialization: asst={asst} tool={tool_msg}")

    # Malformed args must NOT crash serialization — they fall back to {} so the
    # request stays well-formed (the error is recorded against the call anyway).
    at.reset("sys", "x")
    at.record_assistant(
        AssistantTurn("", [ToolCall("toolu_4", "read_file", '{"path": ')], "tool_use")
    )
    if at.messages[-1]["content"][0]["input"] != {}:
        fails.append("anthropic serialization: malformed args should fall back to {}")

    # tool_choice mapping: OpenAI 'required' -> Anthropic {"type": "any"}.
    at.reset("sys", "x")
    if at._payload(0.0, "required", True).get("tool_choice") != {"type": "any"}:
        fails.append("anthropic tool_choice: 'required' must map to {'type':'any'}")
    if at._payload(0.0, "auto", True).get("tool_choice") != {"type": "auto"}:
        fails.append("anthropic tool_choice: 'auto' must map to {'type':'auto'}")


def main() -> int:
    fails = []

    m, agg = _run(s_happy())
    if not (m.completed and agg["agent_capable"] and m.errored == 0):
        fails.append(f"happy: expected clean pass, got {agg} grade={m.grade}")

    m, agg = _run(s_recovery())
    if not (m.completed and m.tool_errors_fed == 1 and m.recovered and m.errored == 0):
        fails.append(
            f"recovery: expected completed+recovered w/ 0 conformance errors, got errored={m.errored} fed={m.tool_errors_fed} recovered={m.recovered}"
        )

    for label, script, attr in [
        ("leak", s_leak(), "leaked_in_content"),
        ("bad_json", s_bad_json(), "invalid_json_args"),
        ("unknown_tool", s_unknown_tool(), "unknown_tool"),
        ("runaway", s_runaway(), "runaway"),
    ]:
        m, agg = _run(script)
        if not (getattr(m, attr) == 1 and not agg["agent_capable"]):
            fails.append(f"{label}: expected {attr}==1 and fail, got {agg}")

    # --- Probes ---
    good_parallel = [
        AssistantTurn(
            "",
            [
                _tc("read_file", f'{{"path": "{CONFIG_PATH}"}}', cid="a"),
                _tc("read_file", '{"path": "app/main.py"}', cid="b"),
            ],
            "tool_calls",
        )
    ]
    r = probe_parallel(FakeTransport(good_parallel), True, 0.0, False)
    if not (r["parallelized"] and r["distinct_ids"] and not r["defect"]):
        fails.append(f"parallel(good): expected clean, got {r}")

    dup_parallel = [
        AssistantTurn(
            "",
            [
                _tc("read_file", f'{{"path": "{CONFIG_PATH}"}}', cid="dup"),
                _tc(
                    "read_file", '{"path": "app/main.py"}', cid="dup"
                ),  # id collision (#21331)
            ],
            "tool_calls",
        )
    ]
    r = probe_parallel(FakeTransport(dup_parallel), True, 0.0, False)
    if not r["defect"]:
        fails.append(f"parallel(dup id): expected defect, got {r}")

    r = probe_tool_choice_required(
        FakeTransport(
            [
                AssistantTurn(
                    "", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"
                )
            ]
        ),
        True,
        0.0,
        False,
    )
    if not (r["honored"] and not r["defect"]):
        fails.append(f"tool_choice(honored): expected clean, got {r}")

    r = probe_tool_choice_required(
        FakeTransport([], raise_on_turn=True), True, 0.0, False
    )
    if not (r["defect"] and r["http_error"]):
        fails.append(f"tool_choice(400): expected defect+http_error, got {r}")

    # A probe defect must sink agent_capable even when the runs are clean.
    m, _ = _run(s_happy())
    agg = aggregate([m], 0.02, 0.8, probes={"parallel": {"defect": True}})
    if agg["agent_capable"]:
        fails.append(
            "probe defect should force agent_capable=false even with clean runs"
        )

    # --- Anthropic transport wire parsing / serialization ---
    _check_anthropic(fails)

    if fails:
        print("SELF-TEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print(
        "SELF-TEST PASSED: runs (happy/recovery), failure modes "
        "(leak/bad-json/unknown-tool/runaway), probes (parallel/tool_choice), "
        "and anthropic-transport wire parsing all correct."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
