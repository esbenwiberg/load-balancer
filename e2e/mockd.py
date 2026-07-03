#!/usr/bin/env python3
"""
mockd — a controllable mock LLM backend for end-to-end testing the balancer
WITHOUT any real Spark or Foundry.

It stands in for a backend model. Two jobs:

  1. Act as a *scripted-compliant coding agent*. When handed the conformance
     tools (read_file / edit_file / run_bash), it drives the exact Read -> Edit
     -> Bash scenario from ../conformance/scenarios.py, deterministically, so
     conformance.py PASSES through it. Pointed at LiteLLM's /v1/responses, that
     turns the Codex->Spark Responses->ChatCompletions bridge smoke test
     (docs/03 risk 4, "Blocker A") into an automatable CI gate for the PLUMBING
     — it proves the bridge mechanics (streaming + tool-call translation both
     ways), not a real model's quality.

  2. Misbehave ON COMMAND. A control endpoint (/__control) injects faults —
     HTTP 5xx/429, latency, mid-stream hangup, leaked/ malformed/runaway tool
     calls — so fallback chains, cooldowns, and the conformance detectors are
     testable DETERMINISTICALLY. You can't make a real Spark die mid-stream on
     cue; you can make mockd do it.

Speaks two OpenAI-compatible surfaces:
  POST /v1/chat/completions   (what vLLM speaks; the LiteLLM backend path)
  POST /v1/responses          (lets conformance --api responses hit mockd direct)

Stdlib only — no pip install, runs bare or in a slim container. Python 3.9+.

Control API (unauthenticated — this is a TEST daemon, bind it to localhost /
an internal compose network only):
  POST /__control  {"model": "<alias|*>", "status": 503, "latency_ms": 200,
                    "mode": "agent|leak|runaway|malformed|hangup|echo",
                    "count": 2}      # apply to next N requests, then auto-clear
  GET  /__control                    # dump current directives
  POST /__reset                      # clear all directives
  GET  /health                       # 200 (liveness)

A directive can also be injected inline in the prompt for one-offs, e.g. a user
message containing [[mockd:status=500]] or [[mockd:mode=runaway]].
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# --- Scenario contract ------------------------------------------------------
# These MUST match ../conformance/scenarios.py exactly, or the scripted agent
# won't satisfy the grader. Kept as literals here so mockd has zero imports and
# can run from any working directory / inside a container.
CONFIG_PATH = "app/config.py"
MAIN_PATH = "app/main.py"
TOOL_NAMES = {"read_file", "edit_file", "run_bash"}

# The four steps of the compliant run, keyed by how many tool RESULTS the
# backend has already been handed in the incoming history.
_AGENT_FINAL_TEXT = "Done — PORT is 9000 and tests pass."


def _new_id(prefix: str) -> str:
    return prefix + "_" + os.urandom(8).hex()


# --- Control state ----------------------------------------------------------


class Control:
    """Thread-safe directive store. Directives are keyed by model alias, with
    '*' as a catch-all. Each directive optionally expires after `count` uses."""

    def __init__(self):
        self._lock = threading.Lock()
        self._by_model = {}  # model -> directive dict

    def set(self, directive: dict) -> None:
        model = directive.get("model", "*")
        with self._lock:
            self._by_model[model] = {k: v for k, v in directive.items() if k != "model"}

    def reset(self) -> None:
        with self._lock:
            self._by_model = {}

    def dump(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._by_model))

    def take(self, model: str) -> dict:
        """Return the active directive for `model` (specific wins over '*'),
        decrementing its remaining `count` and clearing it when exhausted."""
        with self._lock:
            for key in (model, "*"):
                d = self._by_model.get(key)
                if not d:
                    continue
                out = {k: v for k, v in d.items() if k != "count"}
                if "count" in d:
                    d["count"] -= 1
                    if d["count"] <= 0:
                        del self._by_model[key]
                return out
            return {}


CONTROL = Control()

# --- Inline directive parsing ----------------------------------------------

_INLINE_RE = re.compile(r"\[\[mockd:([^\]]+)\]\]")


def _parse_inline(text: str) -> dict:
    """Pull a [[mockd:key=val,key=val]] directive out of prompt text."""
    if not text:
        return {}
    m = _INLINE_RE.search(text)
    if not m:
        return {}
    out = {}
    for pair in m.group(1).split(","):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k in ("status", "latency_ms", "count"):
            try:
                out[k] = int(v)
            except ValueError:
                pass
        else:
            out[k] = v
    return out


# --- Request introspection --------------------------------------------------


def _chat_last_user_text(messages) -> str:
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            c = msg.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):  # content parts
                return " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
    return ""


def _chat_tool_result_count(messages) -> int:
    return sum(1 for m in (messages or []) if m.get("role") == "tool")


def _responses_last_user_text(input_items) -> str:
    for item in reversed(input_items or []):
        if item.get("role") == "user":
            c = item.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(
                    p.get("text", "") for p in c if isinstance(p, dict)
                )
    return ""


def _responses_tool_result_count(input_items) -> int:
    return sum(
        1 for i in (input_items or []) if i.get("type") == "function_call_output"
    )


# --- The scripted-agent brain ----------------------------------------------
# Protocol-agnostic. Given (has_tools, tool_choice, last_user_text,
# tool_result_count) it returns a normalized turn: (text, tool_calls) where each
# tool_call is (name, args_dict).


def decide_turn(has_tools, tool_choice, user_text, results_so_far, served_model=""):
    # Parallel probe: "read BOTH app/config.py and app/main.py ... in a single
    # step" -> emit two distinct read calls.
    low = (user_text or "").lower()
    if has_tools and "both" in low and MAIN_PATH in low:
        return "", [
            ("read_file", {"path": CONFIG_PATH}),
            ("read_file", {"path": MAIN_PATH}),
        ]

    # tool_choice:required -> must emit a tool call regardless of step.
    if has_tools and tool_choice == "required":
        return "", [("read_file", {"path": CONFIG_PATH})]

    if not has_tools:
        # Stamp the SERVED backend model into the reply text. It survives
        # protocol translation, so a fallback test can assert which backend
        # actually answered (client alias != served model after a fallback hop).
        return "mockd served_model=%s" % (served_model or "?"), []

    # Main scripted task, driven by how many results we've been fed.
    if results_so_far <= 0:
        return "", [("read_file", {"path": CONFIG_PATH})]
    if results_so_far == 1:
        return "", [
            (
                "edit_file",
                {
                    "path": CONFIG_PATH,
                    "old_string": "PORT = 8000",
                    "new_string": "PORT = 9000",
                },
            )
        ]
    if results_so_far == 2:
        return "", [("run_bash", {"command": "pytest -q"})]
    return _AGENT_FINAL_TEXT, []


# --- Misbehaviour payloads --------------------------------------------------
# Content strings the conformance detectors are meant to catch.
_LEAK_CONTENT = (
    "Sure, I'll read it.\n<tool_call>\n"
    '{"name": "read_file", "arguments": {"path": "app/config.py"}}\n</tool_call>'
)
_RUNAWAY_CONTENT = "!" * 200


def _apply_mode(mode, text, tool_calls):
    """Rewrite a clean turn into a misbehaving one for the given mode."""
    if mode == "leak":
        return _LEAK_CONTENT, []
    if mode == "runaway":
        return _RUNAWAY_CONTENT, []
    if mode == "malformed":
        # Keep the tool call but corrupt its JSON args downstream (signalled by
        # a sentinel the serializers honor).
        return text, [(name, args, "__malformed__") for (name, args) in tool_calls]
    if mode == "echo":
        return "mockd echo.", []
    return text, tool_calls


def _norm_calls(tool_calls):
    """Normalize (name, args[, marker]) tuples to dicts with a raw-args string."""
    out = []
    for tc in tool_calls:
        name, args = tc[0], tc[1]
        malformed = len(tc) > 2 and tc[2] == "__malformed__"
        raw = json.dumps(args)
        if malformed:
            raw = raw[:-1]  # drop the closing brace -> invalid JSON
        out.append({"id": _new_id("call"), "name": name, "arguments": raw})
    return out


# =============================================================================
# HTTP handler
# =============================================================================


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    # Quieter logs; still show method+path+code.
    def log_message(self, fmt, *args):
        sys.stderr.write("[mockd] " + (fmt % args) + "\n")

    # --- small response helpers --------------------------------------------
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _sse_begin(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        # No Content-Length: chunked/streamed.
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

    def _sse_chunk(self, data_obj_or_str):
        if isinstance(data_obj_or_str, str):
            payload = data_obj_or_str
        else:
            payload = json.dumps(data_obj_or_str)
        chunk = ("data: " + payload + "\n\n").encode()
        # Manual chunked-encoding framing.
        self.wfile.write(("%X\r\n" % len(chunk)).encode() + chunk + b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        self.wfile.write(b"0\r\n\r\n")
        self.wfile.flush()

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        if os.environ.get("MOCKD_DEBUG") and raw and self.path.startswith("/v1/"):
            sys.stderr.write("[mockd DEBUG] %s <- %s\n" % (self.path, raw.decode("utf-8", "replace")))
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return {}

    # --- routing -----------------------------------------------------------
    def do_GET(self):
        if self.path.startswith("/health") or self.path == "/":
            return self._json(200, {"status": "ok", "daemon": "mockd"})
        if self.path.startswith("/__control"):
            return self._json(200, CONTROL.dump())
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.startswith("/__control"):
            body = self._read_body()
            CONTROL.set(body)
            return self._json(200, {"ok": True, "state": CONTROL.dump()})
        if self.path.startswith("/__reset"):
            CONTROL.reset()
            return self._json(200, {"ok": True})
        if self.path.startswith("/v1/chat/completions"):
            return self._handle_chat()
        if self.path.startswith("/v1/responses"):
            return self._handle_responses()
        return self._json(404, {"error": "not found: " + self.path})

    # --- directive resolution ----------------------------------------------
    def _resolve(self, model, user_text):
        """Merge control-store directive with any inline prompt directive."""
        directive = dict(CONTROL.take(model or "*"))
        inline = _parse_inline(user_text)
        directive.update(inline)  # inline wins for one-offs
        return directive

    def _maybe_fault(self, directive):
        """Handle latency + hard HTTP faults. Returns True if it fully handled
        the response (caller must stop)."""
        latency = directive.get("latency_ms")
        if latency:
            time.sleep(latency / 1000.0)
        status = directive.get("status")
        if status:
            self._json(
                int(status),
                {"error": {"message": "mockd injected status %s" % status,
                           "type": "mockd_fault", "code": int(status)}},
            )
            return True
        return False

    # --- chat completions ---------------------------------------------------
    def _handle_chat(self):
        body = self._read_body()
        model = body.get("model", "")
        messages = body.get("messages", [])
        tools = body.get("tools")
        tool_choice = body.get("tool_choice", "auto")
        stream = bool(body.get("stream"))
        user_text = _chat_last_user_text(messages)

        directive = self._resolve(model, user_text)
        if self._maybe_fault(directive):
            return

        text, calls = decide_turn(
            bool(tools), tool_choice, user_text, _chat_tool_result_count(messages), model
        )
        mode = directive.get("mode")
        if mode:
            text, calls = _apply_mode(mode, text, calls)
        norm = _norm_calls(calls)
        finish = "tool_calls" if norm else "stop"
        created = int(time.time())
        cid = _new_id("chatcmpl")

        if not stream:
            return self._json(200, {
                "id": cid, "object": "chat.completion", "created": created,
                "model": model,
                "choices": [{
                    "index": 0, "finish_reason": finish,
                    "message": {
                        "role": "assistant",
                        "content": text or None,
                        "tool_calls": [{
                            "id": c["id"], "type": "function",
                            "function": {"name": c["name"], "arguments": c["arguments"]},
                        } for c in norm] or None,
                    },
                }],
                "usage": {"prompt_tokens": 8, "completion_tokens": 8, "total_tokens": 16},
            })

        # Streaming
        self._sse_begin()
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model}

        def delta(d, finish_reason=None):
            self._sse_chunk({**base, "choices": [{"index": 0, "delta": d, "finish_reason": finish_reason}]})

        delta({"role": "assistant"})
        if directive.get("mode") == "hangup":
            # Emit a partial content chunk, then slam the connection shut with
            # no finish + no [DONE] -> mid-stream death.
            delta({"content": "partial ..."})
            try:
                self.wfile.flush()
                self.connection.close()
            except Exception:
                pass
            return
        if text:
            delta({"content": text})
        for i, c in enumerate(norm):
            delta({"tool_calls": [{
                "index": i, "id": c["id"], "type": "function",
                "function": {"name": c["name"], "arguments": c["arguments"]},
            }]})
        delta({}, finish_reason=finish)
        self._sse_chunk("[DONE]")
        self._sse_end()

    # --- responses api ------------------------------------------------------
    def _handle_responses(self):
        body = self._read_body()
        model = body.get("model", "")
        input_items = body.get("input", [])
        if isinstance(input_items, str):  # Responses allows a bare string
            input_items = [{"role": "user", "content": input_items}]
        tools = body.get("tools")
        tool_choice = body.get("tool_choice", "auto")
        stream = bool(body.get("stream"))
        user_text = _responses_last_user_text(input_items)

        directive = self._resolve(model, user_text)
        if self._maybe_fault(directive):
            return

        text, calls = decide_turn(
            bool(tools), tool_choice, user_text, _responses_tool_result_count(input_items), model
        )
        mode = directive.get("mode")
        if mode:
            text, calls = _apply_mode(mode, text, calls)
        norm = _norm_calls(calls)
        rid = _new_id("resp")

        output = []
        if text:
            output.append({
                "type": "message", "id": _new_id("msg"), "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": text}],
            })
        for c in norm:
            output.append({
                "type": "function_call", "id": _new_id("fc"),
                "call_id": c["id"], "name": c["name"], "arguments": c["arguments"],
            })

        if not stream:
            return self._json(200, {
                "id": rid, "object": "response", "status": "completed",
                "model": model, "output": output,
            })

        # Streaming Responses events.
        self._sse_begin()
        self._sse_chunk({"type": "response.created", "response": {"id": rid, "status": "in_progress"}})
        if directive.get("mode") == "hangup":
            self._sse_chunk({"type": "response.output_text.delta", "output_index": 0, "delta": "partial ..."})
            try:
                self.wfile.flush()
                self.connection.close()
            except Exception:
                pass
            return
        idx = 0
        if text:
            self._sse_chunk({"type": "response.output_item.added", "output_index": idx,
                             "item": {"type": "message", "role": "assistant"}})
            self._sse_chunk({"type": "response.output_text.delta", "output_index": idx, "delta": text})
            idx += 1
        for c in norm:
            self._sse_chunk({"type": "response.output_item.added", "output_index": idx,
                             "item": {"type": "function_call", "id": c["id"],
                                      "call_id": c["id"], "name": c["name"], "arguments": ""}})
            self._sse_chunk({"type": "response.function_call_arguments.delta",
                             "output_index": idx, "delta": c["arguments"]})
            self._sse_chunk({"type": "response.output_item.done", "output_index": idx,
                             "item": {"type": "function_call", "id": c["id"],
                                      "call_id": c["id"], "name": c["name"], "arguments": c["arguments"]}})
            idx += 1
        self._sse_chunk({"type": "response.completed", "response": {"id": rid, "status": "completed", "output": output}})
        self._sse_chunk("[DONE]")
        self._sse_end()


def main():
    port = int(os.environ.get("MOCKD_PORT", "9100"))
    host = os.environ.get("MOCKD_HOST", "0.0.0.0")
    server = ThreadingHTTPServer((host, port), Handler)
    print("mockd listening on http://%s:%d (chat + responses + /__control)" % (host, port))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
