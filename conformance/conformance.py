#!/usr/bin/env python3
"""
Tool-calling conformance harness — the gate that earns `agent_capable`.

Drives a model through a real multi-tool coding task (Read -> Edit -> Bash)
UNDER STREAMING against any OpenAI-compatible base_url (a vLLM Spark today, or
LiteLLM in front of it), and measures how reliably it emits *clean, structured*
tool calls. Emits pass/fail + a tool-call-error-rate.

Why this exists: see docs/04-tool-calling.md. A model can chat fine and still be
useless as a coding-agent backend because its tool calls leak into plain text,
carry malformed JSON, name tools that don't exist, or run away (`!!!!`). Those
are the failure modes this harness hunts for. The output sets `agent_capable`
in the control-plane registry; the running error-rate is the OpenRouter-style
"tool call error rate" you deprioritize a backend on when it drifts.

Usage:
    python conformance.py \
        --base-url http://spark-a.internal:8000/v1 \
        --model Qwen/Qwen3-Coder-30B-A3B-Instruct \
        --runs 5

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
    SYSTEM_PROMPT,
    TASK_PROMPT,
    TOOL_NAMES,
    TOOLS,
    Grade,
    VirtualEnv,
)

# `openai` is imported lazily inside main() so the offline self-test (which
# monkeypatches the transport) can run without the dependency installed.


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
    # A single character repeated many times in a row.
    m = re.search(r"(.)\1{40,}", content)
    if m:
        ch = m.group(1)
        display = repr(ch)
        return f"runaway repetition of {display}"
    # Pathologically long output with almost no character diversity.
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


def _call_streaming(client, model, messages, temperature) -> AssistantTurn:
    """Reassemble a streamed chat completion into one AssistantTurn.

    Streaming is the point of the test: several open vLLM bugs (hermes parser,
    qwen3_coder) only manifest under streaming, which is how coding agents run.
    """
    stream = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        temperature=temperature,
        stream=True,
    )
    content_parts: list[str] = []
    # Reassemble tool-call deltas by index.
    acc: dict[int, dict] = {}
    finish_reason = None
    for chunk in stream:
        if not chunk.choices:
            continue
        choice = chunk.choices[0]
        delta = choice.delta
        if choice.finish_reason:
            finish_reason = choice.finish_reason
        if getattr(delta, "content", None):
            content_parts.append(delta.content)
        for tc in getattr(delta, "tool_calls", None) or []:
            slot = acc.setdefault(tc.index, {"id": None, "name": "", "arguments": ""})
            if tc.id:
                slot["id"] = tc.id
            fn = getattr(tc, "function", None)
            if fn is not None:
                if getattr(fn, "name", None):
                    slot["name"] = fn.name
                if getattr(fn, "arguments", None):
                    slot["arguments"] += fn.arguments
    tool_calls = [
        ToolCall(
            id=slot["id"] or f"call_{i}",
            name=slot["name"],
            arguments_raw=slot["arguments"],
        )
        for i, slot in sorted(acc.items())
    ]
    return AssistantTurn("".join(content_parts), tool_calls, finish_reason)


def _call_blocking(client, model, messages, temperature) -> AssistantTurn:
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=TOOLS,
        tool_choice="auto",
        temperature=temperature,
        stream=False,
    )
    choice = resp.choices[0]
    msg = choice.message
    tool_calls = [
        ToolCall(
            id=tc.id or f"call_{i}",
            name=tc.function.name,
            arguments_raw=tc.function.arguments or "",
        )
        for i, tc in enumerate(msg.tool_calls or [])
    ]
    return AssistantTurn(msg.content or "", tool_calls, choice.finish_reason)


# --- Per-run result ---------------------------------------------------------


@dataclass
class RunMetrics:
    turns: int = 0
    structured_calls: int = 0        # tool calls emitted as structured tool_calls
    valid_calls: int = 0             # structured + parseable + known + schema-ok
    leaked_in_content: int = 0       # tool call leaked into plain text
    invalid_json_args: int = 0
    unknown_tool: int = 0
    missing_required_arg: int = 0
    runaway: int = 0
    api_error: int = 0
    error_detail: list = field(default_factory=list)
    grade: dict = field(default_factory=dict)
    completed: bool = False

    @property
    def attempts(self) -> int:
        # Every intent to call a tool, whether structured or leaked.
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


def run_once(client, model, temperature, max_turns, stream, verbose) -> RunMetrics:
    env = VirtualEnv()
    grade = Grade()
    m = RunMetrics()
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": TASK_PROMPT},
    ]
    caller = _call_streaming if stream else _call_blocking

    for _ in range(max_turns):
        m.turns += 1
        try:
            turn = caller(client, model, messages, temperature)
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
            # Record the assistant turn (with its tool_calls) into history.
            messages.append(
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
                    ],
                }
            )
            for tc in turn.tool_calls:
                m.structured_calls += 1
                if tc.name not in TOOL_NAMES:
                    m.unknown_tool += 1
                    m.error_detail.append(f"unknown_tool: {tc.name!r}")
                    messages.append(_tool_result(tc.id, f"error: unknown tool {tc.name}"))
                    if verbose:
                        print(f"    [unknown_tool] {tc.name!r}")
                    continue
                args, err = _validate_args(tc.name, tc.arguments_raw)
                if err == "invalid_json_args":
                    m.invalid_json_args += 1
                    m.error_detail.append(f"invalid_json_args for {tc.name}: {tc.arguments_raw[:120]!r}")
                    messages.append(_tool_result(tc.id, "error: arguments were not valid JSON"))
                    if verbose:
                        print(f"    [invalid_json] {tc.name}: {tc.arguments_raw[:80]!r}")
                    continue
                if err == "missing_required_arg":
                    m.missing_required_arg += 1
                    m.error_detail.append(f"missing_required_arg for {tc.name}: {args}")
                    messages.append(_tool_result(tc.id, f"error: missing required argument(s) for {tc.name}"))
                    if verbose:
                        print(f"    [missing_arg] {tc.name}: {args}")
                    continue
                # Clean, structured, valid call. Grade + execute against the env.
                m.valid_calls += 1
                grade.observe_call(tc.name, args, env)
                result = env.dispatch(tc.name, args)
                messages.append(_tool_result(tc.id, result))
                if verbose:
                    print(f"    [ok] {tc.name}({_short(args)}) -> {result[:60]!r}")
            continue

        # No structured tool calls in this turn.
        leak = detect_content_toolcall_leak(turn.content)
        if leak:
            m.leaked_in_content += 1
            m.error_detail.append(f"leaked_in_content: {leak}")
            if verbose:
                print(f"    [leak] {leak}")
            break  # a leaking model will keep leaking; the run is a fail.

        # Clean plain-text turn == the model's final answer.
        grade.produced_final_text = True
        if verbose:
            print(f"    [final] {turn.content[:100]!r}")
        break

    m.grade = grade.summary()
    m.completed = grade.task_completed
    return m


def _tool_result(tool_call_id: str, content: str) -> dict:
    return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


def _short(args: dict) -> str:
    return ", ".join(f"{k}={str(v)[:20]!r}" for k, v in args.items())


# --- Aggregation across runs ------------------------------------------------


def aggregate(runs: list, fail_threshold: float, min_completion: float) -> dict:
    total_attempts = sum(r.attempts for r in runs)
    total_errored = sum(r.errored for r in runs)
    total_api_errors = sum(r.api_error for r in runs)
    overall_rate = total_errored / total_attempts if total_attempts else 0.0
    completions = sum(1 for r in runs if r.completed)
    completion_rate = completions / len(runs) if runs else 0.0

    # agent_capable: clean tool mechanics AND the model can actually finish the
    # task, with no hard API errors (a broken parser config throws 400s).
    agent_capable = (
        overall_rate <= fail_threshold
        and completion_rate >= min_completion
        and total_api_errors == 0
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
        "error_breakdown": {
            "leaked_in_content": sum(r.leaked_in_content for r in runs),
            "invalid_json_args": sum(r.invalid_json_args for r in runs),
            "unknown_tool": sum(r.unknown_tool for r in runs),
            "missing_required_arg": sum(r.missing_required_arg for r in runs),
            "runaway": sum(r.runaway for r in runs),
            "api_error": total_api_errors,
        },
        "agent_capable": agent_capable,
    }


def main() -> int:
    p = argparse.ArgumentParser(
        description="Tool-calling conformance harness (sets agent_capable).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--base-url", default=os.environ.get("CONFORMANCE_BASE_URL", "http://localhost:8000/v1"),
                   help="OpenAI-compatible base URL (vLLM Spark, or LiteLLM in front of it).")
    p.add_argument("--api-key", default=os.environ.get("CONFORMANCE_API_KEY", "dummy"),
                   help="API key/token. vLLM often ignores it; LiteLLM wants a virtual key.")
    p.add_argument("--model", default=os.environ.get("CONFORMANCE_MODEL"),
                   help="Model name/alias to target (required).")
    p.add_argument("--runs", type=int, default=int(os.environ.get("CONFORMANCE_RUNS", "5")),
                   help="Repeat the scenario N times for a stable error rate.")
    p.add_argument("--max-turns", type=int, default=12,
                   help="Max assistant turns before giving up on a run.")
    p.add_argument("--temperature", type=float, default=0.0)
    p.add_argument("--fail-threshold", type=float, default=0.02,
                   help="Max tool-call-error-rate to still count as agent_capable.")
    p.add_argument("--min-completion", type=float, default=0.8,
                   help="Min fraction of runs that must complete the task.")
    no_stream_default = os.environ.get("CONFORMANCE_NO_STREAM") == "1"
    p.add_argument("--no-stream", action="store_true", default=no_stream_default,
                   help="Disable streaming (streaming is the realistic path — keep it on).")
    p.add_argument("--json-out", help="Write the full JSON report to this path.")
    p.add_argument("-v", "--verbose", action="store_true", help="Print each tool call.")
    args = p.parse_args()

    if not args.model:
        p.error("--model (or CONFORMANCE_MODEL) is required.")

    try:
        from openai import OpenAI
    except ImportError:
        sys.exit("The 'openai' package is required. Install it:\n    pip install -r requirements.txt")

    client = OpenAI(base_url=args.base_url, api_key=args.api_key)
    stream = not args.no_stream

    print(f"Conformance: model={args.model!r} base_url={args.base_url!r} "
          f"stream={stream} runs={args.runs}")
    print("-" * 72)

    runs: list[RunMetrics] = []
    for i in range(args.runs):
        print(f"Run {i + 1}/{args.runs}:")
        m = run_once(client, args.model, args.temperature, args.max_turns, stream, args.verbose)
        runs.append(m)
        print(f"  turns={m.turns} valid_calls={m.valid_calls} errored={m.errored} "
              f"completed={m.completed} run_error_rate={m.error_rate:.3f}")

    summary = aggregate(runs, args.fail_threshold, args.min_completion)
    report = {
        "model": args.model,
        "base_url": args.base_url,
        "streaming": stream,
        "summary": summary,
        "runs": [asdict(r) for r in runs],
    }

    print("=" * 72)
    print(json.dumps(summary, indent=2))
    verdict = "PASS  agent_capable=true" if summary["agent_capable"] else "FAIL  agent_capable=false"
    print("=" * 72)
    print(verdict)

    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(report, f, indent=2)
        print(f"Full report -> {args.json_out}")

    return 0 if summary["agent_capable"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
