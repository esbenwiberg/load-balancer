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

from conformance import (
    AssistantTurn,
    ToolCall,
    aggregate,
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
        AssistantTurn("", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"),
        AssistantTurn("", [_tc("edit_file", f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 8000", "new_string": "PORT = 9000"}}')], "tool_calls"),
        AssistantTurn("", [_tc("run_bash", '{"command": "pytest -q"}')], "tool_calls"),
        AssistantTurn("Done — PORT is 9000 and tests pass.", [], "stop"),
    ]


def s_recovery():
    # First edit uses a wrong old_string -> env returns "error:"; model recovers.
    return [
        AssistantTurn("", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls"),
        AssistantTurn("", [_tc("edit_file", f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 7000", "new_string": "PORT = 9000"}}')], "tool_calls"),
        AssistantTurn("", [_tc("edit_file", f'{{"path": "{CONFIG_PATH}", "old_string": "PORT = 8000", "new_string": "PORT = 9000"}}')], "tool_calls"),
        AssistantTurn("", [_tc("run_bash", '{"command": "pytest -q"}')], "tool_calls"),
        AssistantTurn("Recovered — tests pass.", [], "stop"),
    ]


def s_leak():
    return [AssistantTurn('<tool_call>\n{"name": "read_file", "arguments": {"path": "app/config.py"}}\n</tool_call>', [], "stop")]


def s_bad_json():
    return [AssistantTurn("", [_tc("read_file", '{"path": app/config.py}')], "tool_calls")]


def s_unknown_tool():
    return [AssistantTurn("", [_tc("grep_files", '{"pattern": "PORT"}')], "tool_calls")]


def s_runaway():
    return [AssistantTurn("!" * 200, [], "stop")]


def _run(script):
    m = run_once(FakeTransport(script), temperature=0.0, max_turns=12, stream=True, verbose=False)
    return m, aggregate([m], 0.02, 0.8)


def main() -> int:
    fails = []

    m, agg = _run(s_happy())
    if not (m.completed and agg["agent_capable"] and m.errored == 0):
        fails.append(f"happy: expected clean pass, got {agg} grade={m.grade}")

    m, agg = _run(s_recovery())
    if not (m.completed and m.tool_errors_fed == 1 and m.recovered and m.errored == 0):
        fails.append(f"recovery: expected completed+recovered w/ 0 conformance errors, got errored={m.errored} fed={m.tool_errors_fed} recovered={m.recovered}")

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
    good_parallel = [AssistantTurn("", [
        _tc("read_file", f'{{"path": "{CONFIG_PATH}"}}', cid="a"),
        _tc("read_file", '{"path": "app/main.py"}', cid="b"),
    ], "tool_calls")]
    r = probe_parallel(FakeTransport(good_parallel), True, 0.0, False)
    if not (r["parallelized"] and r["distinct_ids"] and not r["defect"]):
        fails.append(f"parallel(good): expected clean, got {r}")

    dup_parallel = [AssistantTurn("", [
        _tc("read_file", f'{{"path": "{CONFIG_PATH}"}}', cid="dup"),
        _tc("read_file", '{"path": "app/main.py"}', cid="dup"),  # id collision (#21331)
    ], "tool_calls")]
    r = probe_parallel(FakeTransport(dup_parallel), True, 0.0, False)
    if not r["defect"]:
        fails.append(f"parallel(dup id): expected defect, got {r}")

    r = probe_tool_choice_required(FakeTransport([AssistantTurn("", [_tc("read_file", f'{{"path": "{CONFIG_PATH}"}}')], "tool_calls")]), True, 0.0, False)
    if not (r["honored"] and not r["defect"]):
        fails.append(f"tool_choice(honored): expected clean, got {r}")

    r = probe_tool_choice_required(FakeTransport([], raise_on_turn=True), True, 0.0, False)
    if not (r["defect"] and r["http_error"]):
        fails.append(f"tool_choice(400): expected defect+http_error, got {r}")

    # A probe defect must sink agent_capable even when the runs are clean.
    m, _ = _run(s_happy())
    agg = aggregate([m], 0.02, 0.8, probes={"parallel": {"defect": True}})
    if agg["agent_capable"]:
        fails.append("probe defect should force agent_capable=false even with clean runs")

    if fails:
        print("SELF-TEST FAILED:")
        for f in fails:
            print("  -", f)
        return 1
    print("SELF-TEST PASSED: runs (happy/recovery), failure modes "
          "(leak/bad-json/unknown-tool/runaway), and probes (parallel/tool_choice) all correct.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
