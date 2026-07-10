#!/usr/bin/env python3
"""
Unit tests for obs_callback's SHADOW classifiers: complexity (goal 21), session
(goal 22), the stateless routing policy (goal 24), and the session arm's pin
store + escalation state machine (goal 25 — TTL and restart proven here with
an injected clock; no docker, no sleeping).

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

import os
import sys
import tempfile
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
    _ESCALATE_TAG,
    _apply_enforcement,
    _complexity,
    _PinStore,
    _policy_session,
    _policy_stateless,
    _policy_with_outcome,
    _session,
    _tags,
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


# --- shadow sticky pins + escalation mechanics — the session arm (goal 25) ---
# The e2e suite proves the live path (same-tag stickiness, the escalate tag,
# zero influence); these pin the STATE MACHINE itself: pin-at-first-sight,
# stickiness-beats-re-evaluation, the inactivity TTL (injected clock — no
# sleeping), the restart-loses-pins-safely story (a fresh store IS the
# restart), and docs/12 §5 verbatim — upward only, exactly once, recorded
# no-ops, no downward edge.


def _store(ttl_s=100, cap=4096, path=None):
    """A pin store on its own fresh SQLite file — each call simulates a fresh
    gateway CONTAINER (new /tmp). Pass an explicit `path` to simulate a second
    WORKER sharing the same container's store."""
    if path is None:
        fd, path = tempfile.mkstemp(prefix="pins-test-", suffix=".db")
        os.close(fd)
    return _PinStore(ttl_s=ttl_s, cap=cap, path=path)


def _sess(pins, key, now, escalate=False, cands=_CANDIDATES, key_models=None):
    """One session-arm evaluation with quiet defaults (config-only registry)."""
    return _policy_session(
        pins, key, escalate, cands, key_models or [], "trivial", None, "absent", now
    )


class TestPinStore(unittest.TestCase):
    def test_first_sight_pins_the_stateless_choice(self):
        pins = _store(ttl_s=100)
        b = _sess(pins, "sess-1", now=0.0)
        self.assertEqual(b["arm"], "session")
        self.assertIs(b["pin_hit"], False)
        self.assertEqual(b["pinned_backend"], "qwen3-coder")  # cheapest capable
        self.assertEqual(b["chosen"], "qwen3-coder")
        self.assertIs(b["escalated"], False)
        self.assertIn("pin miss: pinned qwen3-coder (tier=local)", b["reason"])

    def test_pin_hit_beats_re_evaluation(self):
        # Stickiness is the point: once pinned, the pin wins even if a fresh
        # evaluation would now choose differently (candidate pool changed).
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)
        foundry_only = [c for c in _CANDIDATES if c["tier"] == "foundry"]
        b = _sess(pins, "sess-1", now=1.0, cands=foundry_only)
        self.assertIs(b["pin_hit"], True)
        self.assertEqual(b["pinned_backend"], "qwen3-coder")
        # A pure pin hit consulted no health signal — the block must say so.
        self.assertIsNone(b["registry"])
        self.assertIn("pin hit: qwen3-coder", b["reason"])

    def test_different_keys_get_independent_pins(self):
        pins = _store(ttl_s=100)
        _sess(pins, "sess-a", now=0.0)
        b = _sess(pins, "sess-b", now=1.0, key_models=["claude-sonnet"])
        self.assertIs(b["pin_hit"], False)
        self.assertEqual(b["pinned_backend"], "claude-sonnet")
        # sess-a is untouched by sess-b's arrival.
        a = _sess(pins, "sess-a", now=2.0)
        self.assertIs(a["pin_hit"], True)
        self.assertEqual(a["pinned_backend"], "qwen3-coder")

    def test_ttl_expires_on_inactivity_and_the_next_turn_repins(self):
        pins = _store(ttl_s=10)
        _sess(pins, "sess-1", now=0.0)
        # Past the TTL: the pin is gone — not an error, just a re-pin, which
        # re-evaluates against the CURRENT pool (here: local disappeared).
        foundry_only = [c for c in _CANDIDATES if c["tier"] == "foundry"]
        b = _sess(pins, "sess-1", now=11.0, cands=foundry_only)
        self.assertIs(b["pin_hit"], False)
        self.assertEqual(b["pinned_backend"], "claude-opus")  # name tie-break

    def test_activity_refreshes_the_ttl(self):
        # TTL is inactivity-based (docs/12 §3): a session that keeps talking
        # keeps its pin, even long past ttl_s from the FIRST sight.
        pins = _store(ttl_s=10)
        _sess(pins, "sess-1", now=0.0)
        _sess(pins, "sess-1", now=8.0)  # touch
        b = _sess(pins, "sess-1", now=16.0)  # 8s since last touch < 10s TTL
        self.assertIs(b["pin_hit"], True)
        self.assertEqual(b["pinned_backend"], "qwen3-coder")

    def test_restart_loses_pins_safely(self):
        # A store on a fresh path IS the recreated container (new /tmp — pins
        # are container-scoped by design). The escalated session re-pins
        # cleanly — and gets its one hop back, the honest reading of having
        # lost the state.
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)
        esc = _sess(pins, "sess-1", now=1.0, escalate=True)
        self.assertIs(esc["escalated"], True)
        restarted = _store(ttl_s=100)
        b = _sess(restarted, "sess-1", now=2.0)
        self.assertIs(b["pin_hit"], False)
        self.assertEqual(b["pinned_backend"], "qwen3-coder")
        self.assertIs(b["escalated"], False)

    def test_store_is_bounded(self):
        pins = _store(ttl_s=100, cap=2)
        for i, key in enumerate(("sess-a", "sess-b", "sess-c")):
            _sess(pins, key, now=float(i))
        # Oldest evicted; the two youngest survive.
        self.assertIsNone(pins.get("sess-a", 3.0))
        self.assertIsNotNone(pins.get("sess-b", 3.0))
        self.assertIsNotNone(pins.get("sess-c", 3.0))

    def test_two_workers_share_pins(self):
        # THE reason the store is a file: every profile runs the proxy with
        # --num_workers 2, so two processes must see ONE pin universe. Two
        # store instances on the same path are those two workers.
        w1 = _store(ttl_s=100)
        w2 = _store(ttl_s=100, path=w1.path)
        _sess(w1, "sess-1", now=0.0)  # worker 1 pins...
        b = _sess(w2, "sess-1", now=1.0)  # ...worker 2 must hit it
        self.assertIs(b["pin_hit"], True)
        self.assertEqual(b["pinned_backend"], "qwen3-coder")

    def test_concurrent_first_sight_is_first_writer_wins(self):
        # Same-key pin race across workers: the store's INSERT OR IGNORE makes
        # the first writer win; the loser reports the winner's pin as a hit.
        w1 = _store(ttl_s=100)
        w2 = _store(ttl_s=100, path=w1.path)
        w1.pin("sess-1", "qwen3-coder", "local", 0.0)
        pin, created = w2.pin("sess-1", "claude-opus", "foundry", 0.0)
        self.assertIs(created, False)
        self.assertEqual(pin["backend"], "qwen3-coder")


class TestEscalation(unittest.TestCase):
    def test_escalation_replaces_the_pin_upward(self):
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)  # pinned qwen3-coder (local)
        b = _sess(pins, "sess-1", now=1.0, escalate=True)
        self.assertIs(b["pin_hit"], True)
        self.assertIs(b["escalated"], True)
        self.assertEqual(b["pinned_backend"], "claude-opus")  # foundry, by name
        self.assertEqual(b["escalated_from"], "qwen3-coder")
        self.assertIn("upward, exactly once", b["reason"])

    def test_escalation_is_exactly_once_across_workers(self):
        # Two workers firing the signal for the same key: the guarded UPDATE
        # lets exactly ONE flip through; the other worker records the no-op.
        w1 = _store(ttl_s=100)
        w2 = _store(ttl_s=100, path=w1.path)
        _sess(w1, "sess-1", now=0.0)
        first = _sess(w2, "sess-1", now=1.0, escalate=True)
        second = _sess(w1, "sess-1", now=2.0, escalate=True)
        self.assertEqual(first["escalated_from"], "qwen3-coder")
        self.assertIs(second["escalated"], True)
        self.assertNotIn("escalated_from", second)
        self.assertEqual(second["pinned_backend"], "claude-opus")

    def test_second_signal_is_a_recorded_noop(self):
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)
        _sess(pins, "sess-1", now=1.0, escalate=True)
        b = _sess(pins, "sess-1", now=2.0, escalate=True)
        self.assertEqual(b["pinned_backend"], "claude-opus")  # did not move
        self.assertIs(b["escalated"], True)
        self.assertNotIn("escalated_from", b)  # nothing flipped THIS request
        self.assertIn("no-op (already escalated", b["reason"])

    def test_no_downward_edge_ever(self):
        # After escalation the local backend is still the cheapest capable —
        # and must never win the session back.
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)
        _sess(pins, "sess-1", now=1.0, escalate=True)
        b = _sess(pins, "sess-1", now=2.0)
        self.assertEqual(b["pinned_backend"], "claude-opus")
        self.assertIs(b["escalated"], True)

    def test_escalation_target_respects_the_stateless_filters(self):
        # The upward re-run is the FULL stateless arm over the higher tiers:
        # governance still bounds it (claude-opus excluded ⇒ sonnet wins).
        pins = _store(ttl_s=100)
        allow = ["qwen3-coder", "claude-sonnet"]
        _sess(pins, "sess-1", now=0.0, key_models=allow)
        b = _sess(pins, "sess-1", now=1.0, escalate=True, key_models=allow)
        self.assertIs(b["escalated"], True)
        self.assertEqual(b["pinned_backend"], "claude-sonnet")

    def test_top_tier_pin_cannot_escalate_and_the_hop_is_not_burned(self):
        # Pinned on foundry already: no higher tier exists. The signal is a
        # recorded no-op AND escalated stays False — nothing moved, so the
        # session's one hop is not spent on an impossible move.
        foundry_only = [c for c in _CANDIDATES if c["tier"] == "foundry"]
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0, cands=foundry_only)
        b = _sess(pins, "sess-1", now=1.0, escalate=True, cands=foundry_only)
        self.assertEqual(b["pinned_backend"], "claude-opus")
        self.assertIs(b["escalated"], False)
        self.assertIn("no-op, hop NOT burned", b["reason"])

    def test_escalate_with_nothing_pinnable_is_a_noop(self):
        # No capable candidate at all: no pin exists, so the signal has
        # nothing to act on — recorded, never a crash.
        pins = _store(ttl_s=100)
        b = _sess(pins, "sess-1", now=0.0, escalate=True, key_models=["no-such"])
        self.assertIsNone(b["pinned_backend"])
        self.assertIsNone(b["chosen"])
        self.assertIs(b["escalated"], False)
        self.assertIn("nothing pinned to escalate", b["reason"])

    def test_first_sight_plus_escalate_pins_then_escalates(self):
        # The stub trigger arriving on turn 1: pin first (docs/12 §2 row 2),
        # then the state machine fires — deterministic, single request.
        pins = _store(ttl_s=100)
        b = _sess(pins, "sess-1", now=0.0, escalate=True)
        self.assertIs(b["pin_hit"], False)
        self.assertIs(b["escalated"], True)
        self.assertEqual(b["pinned_backend"], "claude-opus")
        self.assertEqual(b["escalated_from"], "qwen3-coder")

    def test_deterministic(self):
        def run():
            pins = _store(ttl_s=100)
            return [
                _sess(pins, "sess-1", now=0.0),
                _sess(pins, "sess-1", now=1.0, escalate=True),
                _sess(pins, "sess-1", now=2.0, escalate=True),
            ]

        self.assertEqual(run(), run())


# --- enforcement — the policy drives routing, behind a flag (goal 26) --------
# The knob's plumbing (env read + hook branch) is two lines in the async hook;
# what needs pinning offline is the REWRITE helper's contract: original ask
# stashed before the mutation, both-arms coverage, and the no-survivor degrade.


class TestEnforcement(unittest.TestCase):
    def test_rewrite_points_data_at_the_chosen_backend(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        data = {"model": "claude-opus"}
        b = _apply_enforcement(block, data)
        self.assertEqual(data["model"], "qwen3-coder")  # the decision is real
        self.assertIs(b["enforced"], True)
        self.assertEqual(b["requested"], "claude-opus")  # stashed PRE-rewrite
        self.assertEqual(b["chosen"], "qwen3-coder")

    def test_agreeing_request_is_not_touched(self):
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        data = {"model": "qwen3-coder"}
        _apply_enforcement(block, data)
        self.assertEqual(data["model"], "qwen3-coder")

    def test_no_survivor_degrades_to_the_clients_ask(self):
        block = _policy_stateless(
            _CANDIDATES, ["no-such-model"], "trivial", None, "absent"
        )
        data = {"model": "claude-opus"}
        b = _apply_enforcement(block, data)
        self.assertEqual(data["model"], "claude-opus")  # untouched
        self.assertIs(b["enforced"], True)  # mode still on-record
        self.assertIsNone(b["chosen"])

    def test_session_arm_block_enforces_the_pin(self):
        pins = _store(ttl_s=100)
        _sess(pins, "sess-1", now=0.0)
        _sess(pins, "sess-1", now=1.0, escalate=True)  # pin now claude-opus
        block = _sess(pins, "sess-1", now=2.0)
        data = {"model": "qwen3-coder"}
        _apply_enforcement(block, data)
        self.assertEqual(data["model"], "claude-opus")  # the escalated pin
        self.assertEqual(block["requested"], "qwen3-coder")

    def test_outcome_triple_survives_enforcement(self):
        # requested vs chosen vs served, all on one block, post-response.
        block = _policy_stateless(_CANDIDATES, [], "trivial", None, "absent")
        _apply_enforcement(block, {"model": "claude-opus"})
        out = _policy_with_outcome(block, "claude-sonnet")  # fallback served
        self.assertEqual(out["requested"], "claude-opus")
        self.assertEqual(out["chosen"], "qwen3-coder")
        self.assertEqual(out["actual"], "claude-sonnet")
        self.assertIs(out["agree"], False)  # the chain fired — visible


class TestEscalateTag(unittest.TestCase):
    def test_bare_escalate_tag_parses_alongside_session_tag(self):
        headers = {"x-litellm-tags": "session:abc, escalate"}
        self.assertIn(_ESCALATE_TAG, _tags(headers))
        s = _session(headers, [_user("hi")])
        self.assertEqual(s["stickiness_key"], "abc")

    def test_garbage_headers_yield_no_tags(self):
        for h in (None, {}, {"x-litellm-tags": 42}, {"x-litellm-tags": " ,, "}):
            self.assertEqual(_tags(h), [], h)


if __name__ == "__main__":
    unittest.main()
