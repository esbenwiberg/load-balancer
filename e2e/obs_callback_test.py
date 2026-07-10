#!/usr/bin/env python3
"""
Unit tests for obs_callback's SHADOW classifiers: complexity (goal 21), session
(goal 22), and the stateless routing policy (goal 24).

Stdlib `unittest` only — no pytest, no docker, no network, and NO litellm: the
callback's only litellm dependency is the CustomLogger base class, so a stub
module satisfies the import and the functions under test (`_complexity`,
`_session`, `_policy_stateless` — pure functions over request/config/registry
features) run offline. The e2e suite proves the live paths — the gateway stamps
the tags onto real routing records; these tests pin the decision logic itself:
every bucket, every filter, every precedence rule, and the never-crash
degradations.

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

from obs_callback import (  # noqa: E402  (needs the stub above)
    _complexity,
    _policy_stateless,
    _policy_with_outcome,
    _session,
)


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


# --- shadow session classification (goal 22) --------------------------------
# The e2e suite proves the live path (headers reach both logging surfaces on
# the pinned litellm — verified by probe); these pin the classifier itself:
# the class rule, the stickiness-key precedence (tag > transcript > null), the
# append-only stability that makes the transcript hash a usable key, and the
# never-crash degradations.

_SESSION_HDRS = {"x-litellm-tags": "session:sess-42,repo:demo"}


class TestRequestClass(unittest.TestCase):
    def test_bare_single_turn_is_one_shot(self):
        s = _session({}, [_user("say hi")])
        self.assertEqual(s["request_class"], "one-shot")
        self.assertIsNone(s["stickiness_key"])
        self.assertIsNone(s["key_source"])

    def test_assistant_turn_means_session(self):
        msgs = [_user("hi"), {"role": "assistant", "content": "hello"}, _user("more")]
        self.assertEqual(_session({}, msgs)["request_class"], "session-turn")

    def test_tool_history_means_session(self):
        msgs = [
            _user("fix it"),
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
        self.assertEqual(_session({}, msgs)["request_class"], "session-turn")

    def test_system_plus_user_is_still_one_shot(self):
        msgs = [{"role": "system", "content": "be brief"}, _user("hi")]
        self.assertEqual(_session({}, msgs)["request_class"], "one-shot")


class TestStickinessKey(unittest.TestCase):
    def test_session_tag_wins_even_on_one_shot(self):
        # Turn 1 of a real session LOOKS like a one-shot — the explicit tag is
        # what makes it sticky from the first request.
        s = _session(_SESSION_HDRS, [_user("first turn")])
        self.assertEqual(s["request_class"], "one-shot")
        self.assertEqual(s["stickiness_key"], "sess-42")
        self.assertEqual(s["key_source"], "tag")

    def test_tag_parsed_from_comma_separated_list(self):
        s = _session({"x-litellm-tags": "repo:demo , session:abc-1"}, [_user("x")])
        self.assertEqual(s["stickiness_key"], "abc-1")

    def test_untagged_session_turn_falls_back_to_transcript_hash(self):
        msgs = [_user("build me a router"), {"role": "assistant", "content": "ok"}]
        s = _session({}, msgs)
        self.assertEqual(s["key_source"], "transcript")
        self.assertTrue(s["stickiness_key"])

    def test_transcript_key_stable_as_transcript_grows(self):
        # Agent transcripts grow append-only: turn N and turn N+2 share the
        # first user message, so they derive the SAME key — that stability is
        # what makes the heuristic usable for stickiness at all.
        turn2 = [_user("build me a router"), {"role": "assistant", "content": "ok"}]
        turn4 = turn2 + [
            _user("now add tests"),
            {"role": "assistant", "content": "done"},
        ]
        self.assertEqual(
            _session({}, turn2)["stickiness_key"],
            _session({}, turn4)["stickiness_key"],
        )

    def test_different_sessions_get_different_transcript_keys(self):
        a = [_user("prompt A"), {"role": "assistant", "content": "x"}]
        b = [_user("prompt B"), {"role": "assistant", "content": "x"}]
        self.assertNotEqual(
            _session({}, a)["stickiness_key"], _session({}, b)["stickiness_key"]
        )

    def test_empty_tag_value_is_ignored(self):
        # "session:" with no id is not a key — fall through, never a "" key.
        s = _session({"x-litellm-tags": "session:"}, [_user("hi")])
        self.assertIsNone(s["stickiness_key"])


class TestSessionDegradations(unittest.TestCase):
    def test_no_messages_returns_none(self):
        self.assertIsNone(_session(_SESSION_HDRS, None))
        self.assertIsNone(_session(_SESSION_HDRS, []))

    def test_garbage_headers_never_crash(self):
        for hdrs in (None, "not a dict", {"x-litellm-tags": 42}):
            s = _session(hdrs, [_user("hi")])
            self.assertEqual(s["request_class"], "one-shot")
            self.assertIsNone(s["stickiness_key"])

    def test_list_content_first_user_turn_hashes(self):
        msgs = [
            {"role": "user", "content": [{"type": "text", "text": "multimodal"}]},
            {"role": "assistant", "content": "ok"},
        ]
        s = _session({}, msgs)
        self.assertEqual(s["key_source"], "transcript")
        self.assertTrue(s["stickiness_key"])

    def test_deterministic(self):
        msgs = [_user("same"), {"role": "assistant", "content": "same"}]
        self.assertEqual(_session(_SESSION_HDRS, msgs), _session(_SESSION_HDRS, msgs))


# --- shadow routing policy — the stateless arm (goal 24) ---------------------
# The e2e suite proves the live path (the block rides real routing records,
# chosen-vs-actual against a real registry); these pin the POLICY FUNCTION
# itself: docs/12 §4's order verbatim, every filter, the deterministic
# tie-breaks, and the config-only degrade that must say so on the record.

# The e2e config's shape: one cheap local workbench, three foundry backends.
_CANDIDATES = [
    {"model": "qwen3-coder", "tier": "local", "agent_capable": True},
    {"model": "claude-sonnet", "tier": "foundry", "agent_capable": True},
    {"model": "claude-opus", "tier": "foundry", "agent_capable": True},
    {"model": "gpt", "tier": "foundry", "agent_capable": True},
]


def _reg(**models):
    """Registry aggregates keyed by model, e.g. _reg(qwen3_coder={...}) — dashes
    aren't identifier-safe, so keys use underscores and map back here."""
    return {k.replace("_", "-"): v for k, v in models.items()}


class TestPolicyOrder(unittest.TestCase):
    def test_cheapest_capable_wins_local_before_foundry(self):
        b = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        self.assertEqual(b["chosen"], "qwen3-coder")
        self.assertEqual(b["arm"], "stateless")
        self.assertEqual(b["candidate_set"][0], "qwen3-coder")

    def test_governance_allowlist_excludes_restricted_backends(self):
        b = _policy_stateless(
            _CANDIDATES, ["claude-sonnet", "gpt"], "trivial", None, "absent"
        )
        self.assertEqual(sorted(b["candidate_set"]), ["claude-sonnet", "gpt"])
        self.assertNotIn("qwen3-coder", b["candidate_set"])
        self.assertIn("governance key-allowlist 4->2", b["reason"])

    def test_wildcard_allowlist_is_unrestricted(self):
        for wl in ([], None, ["all-proxy-models"], ["*", "gpt"]):
            b = _policy_stateless(_CANDIDATES, wl, "trivial", None, "absent")
            self.assertEqual(len(b["candidate_set"]), 4, wl)

    def test_agent_gate_applies_only_to_toolful_and_agentic(self):
        cands = _CANDIDATES + [
            {"model": "tiny", "tier": "local", "agent_capable": False}
        ]
        # trivial: the incapable-but-cheap backend may win (gate not applied)...
        trivial = _policy_stateless(cands, [], "trivial", None, "absent")
        self.assertIn("tiny", trivial["candidate_set"])
        # ...toolful/agentic: it is gated out.
        for bucket in ("toolful", "agentic"):
            b = _policy_stateless(cands, [], bucket, None, "absent")
            self.assertNotIn("tiny", b["candidate_set"], bucket)
            self.assertEqual(b["chosen"], "qwen3-coder")

    def test_registry_agent_verdict_overrides_config_declaration(self):
        # The registry saw the model live (any healthy instance capable) — that
        # verdict beats the config's static declaration, both directions.
        reg = _reg(qwen3_coder={"healthy": 1, "agent_capable": False})
        b = _policy_stateless(_CANDIDATES, [], "agentic", reg, "live")
        self.assertNotIn("qwen3-coder", b["candidate_set"])

    def test_unhealthy_registered_backend_is_excluded(self):
        reg = _reg(qwen3_coder={"healthy": 0, "in_flight": 0})
        b = _policy_stateless(_CANDIDATES, [], "trivial", reg, "live")
        self.assertNotIn("qwen3-coder", b["candidate_set"])
        self.assertEqual(b["chosen"], "claude-opus")  # cheapest surviving tier, by name
        self.assertIn("health via control-plane 4->3", b["reason"])

    def test_unregistered_backend_passes_health_on_config(self):
        # Foundry backends never heartbeat — absence from the registry must not
        # exile them (or the policy could only ever choose workbenches).
        reg = _reg(qwen3_coder={"healthy": 1})
        b = _policy_stateless(_CANDIDATES, [], "trivial", reg, "live")
        self.assertEqual(len(b["candidate_set"]), 4)

    def test_in_flight_tiebreak_within_tier(self):
        cands = [
            {"model": "wb-a", "tier": "local", "agent_capable": True},
            {"model": "wb-b", "tier": "local", "agent_capable": True},
        ]
        reg = {
            "wb-a": {"healthy": 1, "in_flight": 5},
            "wb-b": {"healthy": 1, "in_flight": 2},
        }
        b = _policy_stateless(cands, [], "trivial", reg, "live")
        self.assertEqual(b["chosen"], "wb-b")

    def test_name_tiebreak_makes_order_total(self):
        b = _policy_stateless(
            _CANDIDATES, ["claude-sonnet", "claude-opus"], "trivial", None, "absent"
        )
        # Same tier, same (absent) load — alphabetical, deterministic.
        self.assertEqual(b["candidate_set"], ["claude-opus", "claude-sonnet"])

    def test_empty_survivor_set_yields_null_chosen_with_reason(self):
        b = _policy_stateless(_CANDIDATES, ["no-such-model"], "trivial", None, "absent")
        self.assertEqual(b["candidate_set"], [])
        self.assertIsNone(b["chosen"])
        self.assertIn("no capable candidate survived", b["reason"])

    def test_deterministic(self):
        args = (
            _CANDIDATES,
            ["gpt", "qwen3-coder"],
            "toolful",
            _reg(qwen3_coder={"healthy": 1, "in_flight": 3}),
            "live",
        )
        self.assertEqual(_policy_stateless(*args), _policy_stateless(*args))


class TestPolicyDegrade(unittest.TestCase):
    def test_absent_registry_degrades_to_config_only_and_says_so(self):
        b = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        self.assertEqual(b["registry"], "absent")
        self.assertEqual(len(b["candidate_set"]), 4)  # nothing health-filtered
        self.assertIn("health degraded to config-only (registry absent)", b["reason"])

    def test_stale_registry_degrades_to_config_only_and_says_so(self):
        b = _policy_stateless(_CANDIDATES, [], "trivial", None, "stale")
        self.assertEqual(b["registry"], "stale")
        self.assertIn("health degraded to config-only (registry stale)", b["reason"])

    def test_live_registry_is_stamped_live(self):
        b = _policy_stateless(_CANDIDATES, [], "trivial", {}, "live")
        self.assertEqual(b["registry"], "live")

    def test_garbage_candidates_never_crash(self):
        b = _policy_stateless(
            [42, None, {"no_model": True}] + _CANDIDATES,
            "not-a-list",
            None,
            None,
            "absent",
        )
        self.assertEqual(b["chosen"], "qwen3-coder")


class TestPolicyOutcome(unittest.TestCase):
    def test_agreement_when_reality_matches(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        b = _policy_with_outcome(block, "qwen3-coder")
        self.assertEqual(b["actual"], "qwen3-coder")
        self.assertIs(b["agree"], True)

    def test_disagreement_names_both_sides(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        b = _policy_with_outcome(block, "claude-opus")
        self.assertIs(b["agree"], False)
        self.assertEqual(b["chosen"], "qwen3-coder")
        self.assertEqual(b["actual"], "claude-opus")

    def test_no_verdict_without_reality_or_chosen(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        self.assertIsNone(_policy_with_outcome(block, None)["agree"])
        empty = _policy_stateless(_CANDIDATES, ["no-such"], "trivial", None, "absent")
        self.assertIsNone(_policy_with_outcome(empty, "gpt")["agree"])

    def test_outcome_does_not_mutate_the_remembered_block(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        _policy_with_outcome(block, "gpt")
        self.assertIsNone(block["actual"])  # delivered + attempts each stamp fresh


if __name__ == "__main__":
    unittest.main()
