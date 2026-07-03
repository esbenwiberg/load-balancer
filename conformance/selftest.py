#!/usr/bin/env python3
"""
Offline self-test for the conformance harness.

No network, no model. Feeds scripted `AssistantTurn`s through the real grading
path to prove the detectors and scoring behave: a well-behaved model PASSES, and
each failure mode (leaked call, malformed JSON, unknown tool, runaway) is caught
and drives agent_capable=false. Run before trusting a live result.

    python selftest.py
"""

from __future__ import annotations

import sys

import conformance as C
from conformance import AssistantTurn, ToolCall, aggregate, run_once
from scenarios import CONFIG_PATH


class FakeClient:
    """Stands in for OpenAI; replays a scripted list of AssistantTurns.

    We monkeypatch the module-level caller so run_once() drives this instead of
    a real endpoint. Each call pops the next scripted turn.
    """

    def __init__(self, script):
        self._script = list(script)
        self.seen_messages = []

    def next_turn(self, messages):
        self.seen_messages = messages
        if not self._script:
            # Nothing left to say -> clean empty final answer.
            return AssistantTurn("done.", [], "stop")
        return self._script.pop(0)


def _install(fake):
    C._call_streaming = lambda client, model, messages, temp: fake.next_turn(messages)
    C._call_blocking = C._call_streaming


def _tc(name, args_json, cid="c1"):
    return ToolCall(id=cid, name=name, arguments_raw=args_json)


def scenario_happy():
    """Read -> correct edit -> run tests -> final summary. Should PASS."""
    return [
        AssistantTurn("", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"),
        AssistantTurn(
            "",
            [_tc("edit_file", f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 8000", "new_string": "PORT = 9000"}}')],
            "tool_calls",
        ),
        AssistantTurn("", [_tc("run_bash", '{"command": "pytest -q"}')], "tool_calls"),
        AssistantTurn("Done — changed PORT to 9000 and tests pass.", [], "stop"),
    ]


def scenario_leak():
    """Tool call leaked into content instead of structured tool_calls."""
    return [
        AssistantTurn(
            '<tool_call>\n{"name": "read_file", "arguments": {"path": "app/config.py"}}\n</tool_call>',
            [],
            "stop",
        ),
    ]


def scenario_bad_json():
    return [
        AssistantTurn("", [_tc("read_file", '{"path": app/config.py}')], "tool_calls"),  # unquoted -> invalid
    ]


def scenario_unknown_tool():
    return [
        AssistantTurn("", [_tc("grep_files", '{"pattern": "PORT"}')], "tool_calls"),
    ]


def scenario_runaway():
    return [
        AssistantTurn("!" * 200, [], "stop"),
    ]


def _run(script):
    fake = FakeClient(script)
    _install(fake)
    m = run_once(client=None, model="fake", temperature=0.0, max_turns=12, stream=True, verbose=False)
    return m, aggregate([m], fail_threshold=0.02, min_completion=0.8)


def main() -> int:
    failures = []

    m, agg = _run(scenario_happy())
    if not (m.completed and agg["agent_capable"] and m.errored == 0):
        failures.append(f"happy path should pass cleanly, got {agg} / grade={m.grade}")

    m, agg = _run(scenario_leak())
    if not (m.leaked_in_content == 1 and not agg["agent_capable"]):
        failures.append(f"leak should be detected + fail, got {agg}")

    m, agg = _run(scenario_bad_json())
    if not (m.invalid_json_args == 1 and not agg["agent_capable"]):
        failures.append(f"bad json should be detected + fail, got {agg}")

    m, agg = _run(scenario_unknown_tool())
    if not (m.unknown_tool == 1 and not agg["agent_capable"]):
        failures.append(f"unknown tool should be detected + fail, got {agg}")

    m, agg = _run(scenario_runaway())
    if not (m.runaway == 1 and not agg["agent_capable"]):
        failures.append(f"runaway should be detected + fail, got {agg}")

    if failures:
        print("SELF-TEST FAILED:")
        for f in failures:
            print("  -", f)
        return 1
    print("SELF-TEST PASSED: happy path clean; leak / bad-json / unknown-tool / runaway all caught.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
