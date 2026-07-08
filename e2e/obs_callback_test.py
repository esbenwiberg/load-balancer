#!/usr/bin/env python3
"""
Unit tests for obs_callback's SHADOW complexity classifier (goal 21).

Stdlib `unittest` only — no pytest, no docker, no network, and NO litellm: the
callback's only litellm dependency is the CustomLogger base class, so a stub
module satisfies the import and the classifier under test (`_complexity`, a
pure function over request features) runs offline. The e2e suite proves the
live path — the gateway stamps the tag onto real routing records; these tests
pin the decision tree itself: every bucket, every precedence rule, and the
never-crash degradations.

Run:  python3 obs_callback_test.py        (also pytest-discoverable)
"""

from __future__ import annotations

import sys
import types
import unittest

# obs_callback imports litellm's CustomLogger at module load. The classifier
# needs none of it — stub the import chain so this test stays offline (the
# fast tier has no litellm installed, deliberately: docs/03 risk 8 pins the
# vetted image; nothing on the host should pip-install litellm).
_stub = types.ModuleType("litellm.integrations.custom_logger")
_stub.CustomLogger = object
sys.modules.setdefault("litellm", types.ModuleType("litellm"))
sys.modules.setdefault("litellm.integrations", types.ModuleType("litellm.integrations"))
sys.modules.setdefault("litellm.integrations.custom_logger", _stub)

from obs_callback import _complexity  # noqa: E402  (needs the stub above)


def _user(text):
    return {"role": "user", "content": text}


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
            },
        },
    }
]


class TestBuckets(unittest.TestCase):
    def test_short_toolless_ask_is_trivial(self):
        cx = _complexity([_user("say hi")], None)
        self.assertEqual(cx["bucket"], "trivial")
        self.assertEqual(cx["tools"], 0)
        self.assertEqual(cx["turns"], 1)

    def test_tools_offered_single_shot_is_toolful(self):
        cx = _complexity([_user("read the config")], _TOOLS)
        self.assertEqual(cx["bucket"], "toolful")
        self.assertEqual(cx["tools"], 1)

    def test_tool_role_message_means_agentic(self):
        # An agent loop in motion: a tool result is in the transcript.
        msgs = [
            _user("fix the port in the config"),
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "port=8080"},
        ]
        cx = _complexity(msgs, _TOOLS)
        self.assertEqual(cx["bucket"], "agentic")

    def test_many_turns_with_tools_is_agentic_without_tool_role(self):
        msgs = [_user("a"), {"role": "assistant", "content": "b"}, _user("c")]
        cx = _complexity(msgs, _TOOLS)
        self.assertEqual(cx["bucket"], "agentic")

    def test_big_toolless_prompt_is_heavy(self):
        cx = _complexity([_user("x" * 10_000)], None)  # ~2500 approx tokens
        self.assertEqual(cx["bucket"], "heavy")
        self.assertGreater(cx["approx_prompt_tokens"], 2000)

    def test_long_toolless_transcript_is_heavy(self):
        msgs = [_user("short %d" % i) for i in range(5)]
        cx = _complexity(msgs, None)
        self.assertEqual(cx["bucket"], "heavy")
        self.assertEqual(cx["turns"], 5)

    def test_agentic_outranks_heavy(self):
        # Precedence: a huge prompt WITH an active tool loop is agentic — the
        # loop is the stronger routing signal.
        msgs = [
            _user("x" * 10_000),
            {"role": "tool", "tool_call_id": "c", "content": "r"},
        ]
        cx = _complexity(msgs, _TOOLS)
        self.assertEqual(cx["bucket"], "agentic")


class TestFeatures(unittest.TestCase):
    def test_tool_schemas_count_toward_prompt_weight(self):
        # Tools are serialized into the real prompt — the approx must grow.
        bare = _complexity([_user("hi")], None)["approx_prompt_tokens"]
        with_tools = _complexity([_user("hi")], _TOOLS)["approx_prompt_tokens"]
        self.assertGreater(with_tools, bare)

    def test_list_content_parts_are_counted(self):
        msgs = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "y" * 400}],
            }
        ]
        cx = _complexity(msgs, None)
        self.assertGreaterEqual(cx["approx_prompt_tokens"], 100)

    def test_feature_vector_is_complete(self):
        # The anti-Fugu constraint: the WHY rides the record — all four fields,
        # always, so any classification is auditable after the fact.
        cx = _complexity([_user("hi")], _TOOLS)
        self.assertEqual(set(cx), {"bucket", "approx_prompt_tokens", "turns", "tools"})


class TestDegradations(unittest.TestCase):
    def test_no_messages_returns_none_not_a_guess(self):
        self.assertIsNone(_complexity(None, _TOOLS))
        self.assertIsNone(_complexity([], _TOOLS))
        self.assertIsNone(_complexity("not a list", _TOOLS))

    def test_garbage_entries_never_crash(self):
        msgs = [42, None, {"role": "user", "content": {"weird": True}}, _user("ok")]
        cx = _complexity(msgs, "not a list")
        self.assertEqual(cx["bucket"], "trivial")
        self.assertEqual(cx["tools"], 0)

    def test_deterministic(self):
        # Same input, same answer — the auditable-routing constraint in test form.
        msgs = [_user("classify me")]
        self.assertEqual(_complexity(msgs, _TOOLS), _complexity(msgs, _TOOLS))


if __name__ == "__main__":
    unittest.main()
