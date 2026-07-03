"""
Tool-calling conformance scenarios.

A scenario is a scripted, multi-tool coding task that a *real* coding agent
(Claude Code, Codex) would drive through structured tool calls. We hand the
model the same shape of tools those agents use — Read, Edit, Bash — and a small
virtual workspace, then grade whether the model drives the task through *clean,
structured* tool calls (not tool calls leaked into plain text).

The point (see docs/04-tool-calling.md): "does a reply come back" is not the
bar. The bar is: does THIS model + engine + parser + chat-template, UNDER
STREAMING, emit clean structured tool calls for a MULTI-tool session.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


# --- Tool schema (OpenAI Chat Completions "tools" shape) --------------------
# Mirrors the core coding-agent tools. Kept deliberately small but with a
# required multi-arg tool (edit_file) because weak models most often mangle
# multi-argument / string-heavy tool calls.

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full contents of a file at the given path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Repo-relative path to the file, e.g. app/config.py",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Replace an exact substring in a file with new text. "
                "old_string must match the file contents exactly and uniquely."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Repo-relative path to the file."},
                    "old_string": {"type": "string", "description": "Exact text to replace."},
                    "new_string": {"type": "string", "description": "Replacement text."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the repo root and return its combined stdout/stderr.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."}
                },
                "required": ["command"],
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}

# Same tools in the OpenAI *Responses* API shape (name/parameters flattened to
# the top level, not nested under "function"). Used when driving /v1/responses
# — the endpoint Codex speaks and the one that exercises LiteLLM's
# Responses->ChatCompletions bridge (Blocker A).
RESPONSES_TOOLS = [
    {
        "type": "function",
        "name": t["function"]["name"],
        "description": t["function"]["description"],
        "parameters": t["function"]["parameters"],
    }
    for t in TOOLS
]


# --- Virtual workspace ------------------------------------------------------
# Stateful so tool results depend on what the model actually did — a model that
# runs the tests before editing gets a failing result, exactly like real life.

CONFIG_PATH = "app/config.py"
MAIN_PATH = "app/main.py"
INITIAL_CONFIG = "PORT = 8000\nDEBUG = True\nWORKERS = 4\n"
INITIAL_MAIN = "from app.config import PORT\n\ndef main():\n    serve(port=PORT)\n"


@dataclass
class VirtualEnv:
    """A tiny mutable filesystem + fake test runner the tools act on."""

    files: dict = field(
        default_factory=lambda: {CONFIG_PATH: INITIAL_CONFIG, MAIN_PATH: INITIAL_MAIN}
    )

    def read_file(self, path: str) -> str:
        if path not in self.files:
            return f"error: no such file: {path}"
        return self.files[path]

    def edit_file(self, path: str, old_string: str, new_string: str) -> str:
        if path not in self.files:
            return f"error: no such file: {path}"
        content = self.files[path]
        count = content.count(old_string)
        if count == 0:
            return f"error: old_string not found in {path}"
        if count > 1:
            return f"error: old_string is not unique in {path} ({count} matches)"
        self.files[path] = content.replace(old_string, new_string)
        return f"ok: edited {path}"

    def run_bash(self, command: str) -> str:
        # Only the test runner matters for grading. Tests pass iff the port was
        # actually changed to 9000 — so a correct edit is observable downstream.
        if "pytest" in command or "test" in command:
            if "PORT = 9000" in self.files.get(CONFIG_PATH, ""):
                return "1 passed in 0.03s"
            return "1 failed in 0.03s\nE  assert app started on port 9000"
        return f"$ {command}\n(exit 0)"

    def dispatch(self, name: str, args: dict) -> str:
        if name == "read_file":
            return self.read_file(args.get("path", ""))
        if name == "edit_file":
            return self.edit_file(
                args.get("path", ""), args.get("old_string", ""), args.get("new_string", "")
            )
        if name == "run_bash":
            return self.run_bash(args.get("command", ""))
        return f"error: unknown tool {name}"


# --- The task ---------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a coding agent operating in a real repository. You accomplish tasks "
    "ONLY by calling the provided tools — never guess or invent file contents, and "
    "never describe an edit in prose instead of calling edit_file. Read before you "
    "edit. When the task is fully done, reply with a short plain-text summary and no "
    "further tool calls."
)

TASK_PROMPT = (
    "The service is booting on the wrong port. In app/config.py, change the PORT "
    "value from 8000 to 9000. Then run the test suite with `pytest -q` and tell me "
    "whether it passes. Do not modify anything else."
)

# Single-turn probe: invites the model to emit MULTIPLE tool calls in ONE turn.
# Parallel tool calls are a known translation hazard (doc 04) and the LiteLLM
# Responses bridge specifically had a parallel-call index-collision bug (#21331).
PARALLEL_PROBE_PROMPT = (
    "Before doing anything else, read BOTH app/config.py and app/main.py so you "
    "have full context. Issue both reads now, together, in a single step."
)

# Single-turn probe: forces a tool call. Qwen3 + reasoning + tool_choice:required
# is a known HTTP-400 in vLLM (doc 04) — this smokes it out.
TOOL_CHOICE_PROBE_PROMPT = "Read app/config.py."


@dataclass
class Grade:
    """Task-progress grading — separate from tool-call *mechanics* scoring."""

    did_read_config: bool = False
    did_edit_correct: bool = False   # changed 8000 -> 9000 in config
    did_run_tests: bool = False
    saw_tests_pass: bool = False     # ran tests AFTER a correct edit
    produced_final_text: bool = False

    def observe_call(self, name: str, args: dict, env_before: "VirtualEnv") -> None:
        if name == "read_file" and args.get("path") == CONFIG_PATH:
            self.did_read_config = True
        if name == "edit_file" and args.get("path") == CONFIG_PATH:
            # Correct iff this edit takes the file to PORT = 9000.
            if "9000" in str(args.get("new_string", "")) and "8000" in str(
                args.get("old_string", "")
            ):
                self.did_edit_correct = True
        if name == "run_bash" and ("pytest" in str(args.get("command", "")) or "test" in str(
            args.get("command", "")
        )):
            self.did_run_tests = True
            if "PORT = 9000" in env_before.files.get(CONFIG_PATH, ""):
                self.saw_tests_pass = True

    @property
    def task_completed(self) -> bool:
        return (
            self.did_read_config
            and self.did_edit_correct
            and self.did_run_tests
            and self.saw_tests_pass
            and self.produced_final_text
        )

    def summary(self) -> dict:
        return {
            "did_read_config": self.did_read_config,
            "did_edit_correct": self.did_edit_correct,
            "did_run_tests": self.did_run_tests,
            "saw_tests_pass": self.saw_tests_pass,
            "produced_final_text": self.produced_final_text,
            "task_completed": self.task_completed,
        }
