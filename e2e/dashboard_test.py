#!/usr/bin/env python3
"""
Unit tests for the dashboard's fleet view (goal 13).

Stdlib `unittest` only — no pytest, no docker, no network. The e2e suite
(test_e2e.py) proves the REGISTRY -> DASHBOARD path end to end against a live
control-plane container; these tests cover the shaping + graceful-degrade
branches of `_fetch_fleet` OFFLINE, including the failure paths the always-up
e2e stack never exercises (control-plane unconfigured / unreachable / gibberish).

Run:  python3 dashboard_test.py        (also pytest-discoverable)
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

import dashboard


class _FakeResp:
    """Minimal stand-in for urlopen's context-manager response. Pass a dict to
    serialize as JSON, or raw bytes to feed the body verbatim (malformed case)."""

    def __init__(self, payload):
        self._body = (
            payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        )

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self._body


# A control-plane /models payload: one model, two instances (one healthy+warm
# with load, one that reported unhealthy). Mirrors control_plane.Registry.models().
_MODELS_PAYLOAD = {
    "models": [
        {
            "model": "qwen3-coder",
            "warm": 1,
            "in_flight": 4,
            "healthy": 1,
            "instances_total": 2,
            "agent_capable": True,
            "instances": [
                {
                    "workbench_id": "wb-b",
                    "model": "qwen3-coder",
                    "warm": False,
                    "in_flight": 0,
                    "healthy": False,
                    "stale": False,
                    "age_ms": 10,
                },
                {
                    "workbench_id": "wb-a",
                    "model": "qwen3-coder",
                    "warm": True,
                    "in_flight": 4,
                    "healthy": True,
                    "stale": False,
                    "age_ms": 5,
                },
            ],
        }
    ]
}


class TestFetchFleet(unittest.TestCase):
    def test_unconfigured_is_unavailable(self):
        with mock.patch.object(dashboard, "CONTROL_PLANE_URL", ""):
            out = dashboard._fetch_fleet()
        self.assertFalse(out["available"])
        self.assertIn("not configured", out["error"])

    def test_unreachable_degrades_not_raises(self):
        with mock.patch.object(dashboard, "CONTROL_PLANE_URL", "http://cp:9400"):
            with mock.patch(
                "dashboard.urllib.request.urlopen", side_effect=OSError("refused")
            ):
                out = dashboard._fetch_fleet()
        self.assertFalse(out["available"])
        self.assertIn("unreachable", out["error"])
        self.assertEqual(out["control_plane_url"], "http://cp:9400")

    def test_bad_shape_is_unavailable(self):
        with mock.patch.object(dashboard, "CONTROL_PLANE_URL", "http://cp:9400"):
            with mock.patch(
                "dashboard.urllib.request.urlopen",
                return_value=_FakeResp({"unexpected": True}),
            ):
                out = dashboard._fetch_fleet()
        self.assertFalse(out["available"])
        self.assertIn("unexpected shape", out["error"])

    def test_malformed_json_is_unavailable(self):
        with mock.patch.object(dashboard, "CONTROL_PLANE_URL", "http://cp:9400"):
            with mock.patch(
                "dashboard.urllib.request.urlopen",
                return_value=_FakeResp(b"not json{"),
            ):
                out = dashboard._fetch_fleet()
        self.assertFalse(out["available"])

    def test_happy_path_passes_models_and_flattens_instances(self):
        with mock.patch.object(dashboard, "CONTROL_PLANE_URL", "http://cp:9400"):
            with mock.patch(
                "dashboard.urllib.request.urlopen",
                return_value=_FakeResp(_MODELS_PAYLOAD),
            ):
                out = dashboard._fetch_fleet()
        self.assertTrue(out["available"])
        # models passed through untouched (the aggregate is the control-plane's).
        self.assertEqual(len(out["models"]), 1)
        self.assertEqual(out["models"][0]["model"], "qwen3-coder")
        self.assertEqual(out["models"][0]["in_flight"], 4)
        # instances flattened out of the drill-down AND sorted (workbench, model),
        # so the per-workbench table + assertion are deterministic.
        wbs = [i["workbench_id"] for i in out["instances"]]
        self.assertEqual(wbs, ["wb-a", "wb-b"])
        # the derived-health signal on each instance is preserved as-is.
        by_wb = {i["workbench_id"]: i for i in out["instances"]}
        self.assertTrue(by_wb["wb-a"]["healthy"])
        self.assertFalse(by_wb["wb-b"]["healthy"])


# --- identity in routing records (goal 15) ----------------------------------
# The e2e suite (test_e2e.py) proves the minted-key -> dashboard path end to end
# against the live gateway; these cover the record-shaping OFFLINE — that the
# per-request view carries the identity fields and the per-key rollup aggregates
# correctly, including the null-identity (master key / no key store) collapse.


def _delivered(**kw):
    """A minimal `delivered` record like obs_callback emits (goal 3 + 15)."""
    rec = {
        "event": "delivered",
        "requested_model": "qwen3-coder",
        "served_model": "qwen3-coder",
        "fallback": False,
        "response_cost": 0.01,
        "tokens": {"total": 16},
    }
    rec.update(kw)
    return rec


class TestRequestsViewIdentity(unittest.TestCase):
    def test_request_row_carries_identity(self):
        recs = [_delivered(key_alias="repo-a", user_id="test-user", team_id="team-x")]
        row = dashboard._requests_view(recs)[0]
        self.assertEqual(row["key_alias"], "repo-a")
        self.assertEqual(row["user_id"], "test-user")
        self.assertEqual(row["team_id"], "team-x")

    def test_null_identity_passes_through(self):
        # Master key / no key store: obs_callback stamps nulls; the view keeps them.
        row = dashboard._requests_view([_delivered()])[0]
        self.assertIsNone(row["key_alias"])
        self.assertIsNone(row["user_id"])
        self.assertIsNone(row["team_id"])


class TestKeyRollup(unittest.TestCase):
    def test_rollup_aggregates_per_key(self):
        recs = [
            _delivered(
                key_alias="repo-a",
                user_id="u1",
                team_id="t1",
                tokens={"total": 10},
                response_cost=0.02,
            ),
            _delivered(
                key_alias="repo-a",
                user_id="u1",
                team_id="t1",
                tokens={"total": 6},
                response_cost=0.01,
                fallback=True,
            ),
            _delivered(
                key_alias="repo-b",
                user_id="u2",
                team_id="t1",
                tokens={"total": 8},
                response_cost=0.03,
            ),
        ]
        rows = dashboard._key_rollup(recs)
        by_alias = {r["key_alias"]: r for r in rows}
        a = by_alias["repo-a"]
        self.assertEqual(a["requests"], 2)
        self.assertEqual(a["fallbacks"], 1)
        self.assertEqual(a["tokens"], 16)
        self.assertAlmostEqual(a["cost"], 0.03)
        self.assertEqual(a["user_id"], "u1")
        self.assertEqual(a["team_id"], "t1")
        b = by_alias["repo-b"]
        self.assertEqual(b["requests"], 1)
        self.assertEqual(b["fallbacks"], 0)
        # Busiest-first ordering: repo-a (2 requests) precedes repo-b (1).
        self.assertEqual(rows[0]["key_alias"], "repo-a")

    def test_null_identity_collapses_into_one_row(self):
        # Master-key traffic (no alias) all folds into a single null-alias row,
        # never scattered or dropped — so the rollup stays honest without a key
        # store in play.
        rows = dashboard._key_rollup([_delivered(), _delivered()])
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["key_alias"])
        self.assertEqual(rows[0]["requests"], 2)

    def test_ignores_non_delivered_events(self):
        rows = dashboard._key_rollup([{"event": "llm_call", "key_alias": "x"}])
        self.assertEqual(rows, [])


# --- trace correlation: join a request to its attempt trail (goal 16) --------
# The e2e suite proves the gateway actually stamps a shared correlation_id end to
# end; these cover the dashboard FOLD offline — that _requests_view nests each
# request's llm_call attempts under it by that id (the failed primary of a
# fallback included), and degrades cleanly when the id is absent.


class TestTraceCorrelation(unittest.TestCase):
    def test_fallback_request_joins_its_503_attempt(self):
        # A forced fallback: qwen3-coder 503s, claude-sonnet answers. On the proxy
        # fallback path the winner's success llm_call may not fire — so the only
        # attempt sharing the delivered record's correlation_id is the FAILED
        # primary. The join must still surface it under the request.
        cid = "obs-deadbeef"
        records = [
            {
                "event": "llm_call",
                "status": "failure",
                "requested_group": "qwen3-coder",
                "backend": "qwen",
                "tier": "local",
                "error_code": 503,
                "latency_ms": 12.3,
                "correlation_id": cid,
            },
            {
                "event": "delivered",
                "requested_model": "qwen3-coder",
                "served_model": "claude-sonnet",
                "fallback": True,
                "tokens": {"total": 7},
                "correlation_id": cid,
            },
        ]
        reqs = dashboard._requests_view(records)
        self.assertEqual(len(reqs), 1)
        row = reqs[0]
        self.assertEqual(row["correlation_id"], cid)
        self.assertEqual(len(row["attempts"]), 1)
        att = row["attempts"][0]
        self.assertEqual(att["status"], "failure")
        self.assertEqual(att["error_code"], 503)
        self.assertEqual(att["requested_group"], "qwen3-coder")
        self.assertEqual(att["correlation_id"], cid)

    def test_direct_request_joins_its_success_attempt(self):
        cid = "obs-cafe"
        records = [
            {
                "event": "llm_call",
                "status": "success",
                "requested_group": "claude-sonnet",
                "backend": "sonnet",
                "correlation_id": cid,
            },
            {
                "event": "delivered",
                "requested_model": "claude-sonnet",
                "served_model": "claude-sonnet",
                "fallback": False,
                "correlation_id": cid,
            },
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["correlation_id"], cid)
        self.assertEqual(len(row["attempts"]), 1)
        self.assertEqual(row["attempts"][0]["status"], "success")

    def test_two_requests_do_not_cross_join(self):
        records = [
            {
                "event": "llm_call",
                "status": "success",
                "backend": "a",
                "correlation_id": "id-A",
            },
            {
                "event": "llm_call",
                "status": "success",
                "backend": "b",
                "correlation_id": "id-B",
            },
            {
                "event": "delivered",
                "requested_model": "a",
                "served_model": "a",
                "fallback": False,
                "correlation_id": "id-A",
            },
            {
                "event": "delivered",
                "requested_model": "b",
                "served_model": "b",
                "fallback": False,
                "correlation_id": "id-B",
            },
        ]
        reqs = {r["correlation_id"]: r for r in dashboard._requests_view(records)}
        self.assertEqual([a["backend"] for a in reqs["id-A"]["attempts"]], ["a"])
        self.assertEqual([a["backend"] for a in reqs["id-B"]["attempts"]], ["b"])

    def test_request_without_correlation_id_degrades_to_empty_trail(self):
        # A pre-goal-16 record (no correlation_id) still yields a row, just with no
        # nested attempts — never a crash, never a wrong join. The uncorrelated
        # attempt is not lost either: it still shows in the flat trail.
        records = [
            {"event": "llm_call", "status": "failure", "backend": "x"},
            {
                "event": "delivered",
                "requested_model": "x",
                "served_model": "y",
                "fallback": True,
            },
        ]
        row = dashboard._requests_view(records)[0]
        self.assertIsNone(row["correlation_id"])
        self.assertEqual(row["attempts"], [])
        self.assertEqual(len(dashboard._attempts_view(records)), 1)


# --- overhead attribution: delivered vs consumed tokens (goal 20) ------------
# The Fugu lesson (docs/09): visible tokens are not consumed tokens once retries
# and fallbacks pile up. The e2e suite proves the live path end to end — where,
# on the pinned litellm v1.83.14, FAILED attempts report zero usage (verified),
# so a real 503-fallback shows consumed == delivered. These offline tests are
# therefore the place that PROVES the summation itself, with synthetic failed
# attempts that DO carry tokens — the instrument is ready for real backends that
# bill partial usage, even though the mock stack can't produce it.


class TestOverheadAttribution(unittest.TestCase):
    def test_direct_request_consumed_equals_delivered(self):
        cid = "obs-direct"
        records = [
            {
                "event": "llm_call",
                "status": "success",
                "backend": "qwen",
                "tokens": {"total": 16},
                "correlation_id": cid,
            },
            _delivered(tokens={"total": 16}, correlation_id=cid),
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["tokens_delivered"], 16)
        self.assertEqual(row["tokens_consumed"], 16)

    def test_fallback_with_token_carrying_failure_shows_overhead(self):
        # THE goal-20 proof: a failed attempt that burned tokens makes
        # consumed > delivered — backend burn the client never saw.
        cid = "obs-over"
        records = [
            {
                "event": "llm_call",
                "status": "failure",
                "backend": "qwen",
                "error_code": 500,
                "tokens": {"total": 7},
                "correlation_id": cid,
            },
            {
                "event": "llm_call",
                "status": "success",
                "backend": "sonnet",
                "tokens": {"total": 16},
                "correlation_id": cid,
            },
            _delivered(
                served_model="claude-sonnet",
                fallback=True,
                tokens={"total": 16},
                correlation_id=cid,
            ),
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["tokens_delivered"], 16)
        self.assertEqual(row["tokens_consumed"], 23)  # 7 wasted + 16 delivered
        self.assertGreater(row["tokens_consumed"], row["tokens_delivered"])

    def test_winner_without_success_attempt_is_inferred_not_dropped(self):
        # The verified fallback-winner quirk: no success llm_call fires. The
        # delivered tokens must stand in for the winner — consumed is 16 (0 from
        # the failed attempt + the inferred winner), never 0.
        cid = "obs-quirk"
        records = [
            {
                "event": "llm_call",
                "status": "failure",
                "backend": "qwen",
                "error_code": 503,
                "tokens": {"total": 0},
                "correlation_id": cid,
            },
            _delivered(
                served_model="claude-sonnet",
                fallback=True,
                tokens={"total": 16},
                correlation_id=cid,
            ),
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["tokens_consumed"], 16)

    def test_success_attempt_present_means_no_double_count(self):
        # When the winner's success event DID fire (verified: it does on the
        # non-streamed fallback path), its tokens are in the attempt sum and the
        # delivered tokens must NOT be added on top.
        cid = "obs-nodouble"
        records = [
            {
                "event": "llm_call",
                "status": "success",
                "backend": "sonnet",
                "tokens": {"total": 16},
                "correlation_id": cid,
            },
            _delivered(tokens={"total": 16}, correlation_id=cid),
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["tokens_consumed"], 16)  # not 32

    def test_attempt_without_usage_counts_zero(self):
        # The documented convention: no usage reported => 0, never a crash.
        cid = "obs-nousage"
        records = [
            {"event": "llm_call", "status": "failure", "correlation_id": cid},
            _delivered(tokens={"total": 5}, correlation_id=cid),
        ]
        row = dashboard._requests_view(records)[0]
        self.assertEqual(row["tokens_consumed"], 5)

    def test_overhead_rollup_sums_and_ratio(self):
        cid1, cid2 = "obs-r1", "obs-r2"
        records = [
            # request 1: clean direct, 10 delivered / 10 consumed
            {
                "event": "llm_call",
                "status": "success",
                "tokens": {"total": 10},
                "correlation_id": cid1,
            },
            _delivered(tokens={"total": 10}, correlation_id=cid1),
            # request 2: token-burning failure + winner, 16 delivered / 23 consumed
            {
                "event": "llm_call",
                "status": "failure",
                "tokens": {"total": 7},
                "correlation_id": cid2,
            },
            {
                "event": "llm_call",
                "status": "success",
                "tokens": {"total": 16},
                "correlation_id": cid2,
            },
            _delivered(fallback=True, tokens={"total": 16}, correlation_id=cid2),
            # a streamed request's winner: attempt with NO delivered record —
            # must land in unattributed, not skew the per-request ratio.
            {
                "event": "llm_call",
                "status": "success",
                "tokens": {"total": 19},
                "correlation_id": "obs-streamed",
            },
        ]
        requests = dashboard._requests_view(records)
        ov = dashboard._overhead_rollup(records, requests)
        self.assertEqual(ov["requests"], 2)
        self.assertEqual(ov["tokens_delivered"], 26)
        self.assertEqual(ov["tokens_consumed"], 33)
        self.assertEqual(ov["overhead_tokens"], 7)
        self.assertAlmostEqual(ov["overhead_ratio"], round(33 / 26, 3))
        self.assertEqual(ov["unattributed_attempt_tokens"], 19)

    def test_overhead_rollup_empty_is_calm(self):
        # No traffic: zeros and a null ratio — never a ZeroDivisionError.
        ov = dashboard._overhead_rollup([], [])
        self.assertEqual(ov["requests"], 0)
        self.assertEqual(ov["tokens_delivered"], 0)
        self.assertEqual(ov["tokens_consumed"], 0)
        self.assertIsNone(ov["overhead_ratio"])


# --- shadow complexity: the traffic-mix telemetry (goal 21) ------------------
# The classifier itself (decision tree, precedence, degradations) pins in
# obs_callback_test.py; these cover the dashboard FOLD — the per-request
# passthrough and the distribution rollup, including the "unclassified" bucket
# that keeps the denominator honest.


class TestComplexityShaping(unittest.TestCase):
    def test_request_row_carries_complexity(self):
        cx = {"bucket": "agentic", "approx_prompt_tokens": 900, "turns": 3, "tools": 2}
        row = dashboard._requests_view([_delivered(complexity=cx)])[0]
        self.assertEqual(row["complexity"], cx)

    def test_untagged_record_degrades_to_none(self):
        row = dashboard._requests_view([_delivered()])[0]
        self.assertIsNone(row["complexity"])

    def test_buckets_count_delivered_by_bucket(self):
        records = [
            _delivered(complexity={"bucket": "trivial"}),
            _delivered(complexity={"bucket": "trivial"}),
            _delivered(complexity={"bucket": "agentic"}),
            _delivered(),  # no tag -> unclassified, never dropped
            {"event": "llm_call", "complexity": {"bucket": "heavy"}},  # not delivered
        ]
        buckets = dashboard._complexity_buckets(records)
        self.assertEqual(buckets, {"trivial": 2, "agentic": 1, "unclassified": 1})

    def test_buckets_empty_stream_is_empty(self):
        self.assertEqual(dashboard._complexity_buckets([]), {})


# --- shadow session classification: the traffic-mix fold (goal 22) -----------


class TestSessionShaping(unittest.TestCase):
    def test_request_row_carries_session(self):
        sess = {
            "request_class": "session-turn",
            "stickiness_key": "sess-1",
            "key_source": "tag",
        }
        row = dashboard._requests_view([_delivered(session=sess)])[0]
        self.assertEqual(row["session"], sess)

    def test_untagged_record_degrades_to_none(self):
        self.assertIsNone(dashboard._requests_view([_delivered()])[0]["session"])

    def test_class_distribution_counts_delivered(self):
        records = [
            _delivered(session={"request_class": "session-turn"}),
            _delivered(session={"request_class": "one-shot"}),
            _delivered(session={"request_class": "one-shot"}),
            _delivered(),  # untagged -> unclassified, never dropped
            {"event": "llm_call", "session": {"request_class": "one-shot"}},
        ]
        self.assertEqual(
            dashboard._request_class_distribution(records),
            {"session-turn": 1, "one-shot": 2, "unclassified": 1},
        )


# --- shadow routing policy: the agreement fold (goal 24) ---------------------
# The policy function itself (docs/12 §4 order, filters, degrade) pins in
# obs_callback_test.py; these cover the dashboard FOLD — the per-request
# passthrough and the chosen-vs-actual agreement rollup, including the
# honest-denominator conventions (no-block and no-verdict records counted,
# never dropped; empty stream yields a null rate, not a fake 100%).


def _policy_block(**kw):
    block = {
        "arm": "stateless",
        "candidate_set": ["qwen3-coder", "claude-sonnet"],
        "chosen": "qwen3-coder",
        "reason": "governance: key unrestricted; chose qwen3-coder",
        "registry": "live",
        "actual": "qwen3-coder",
        "agree": True,
    }
    block.update(kw)
    return block


class TestPolicyShaping(unittest.TestCase):
    def test_request_row_carries_policy_block(self):
        block = _policy_block(actual="claude-opus", agree=False)
        row = dashboard._requests_view([_delivered(shadow_policy=block)])[0]
        self.assertEqual(row["policy"], block)

    def test_untagged_record_degrades_to_none(self):
        self.assertIsNone(dashboard._requests_view([_delivered()])[0]["policy"])

    def test_agreement_rollup_counts_verdicts(self):
        records = [
            _delivered(shadow_policy=_policy_block()),
            _delivered(shadow_policy=_policy_block()),
            _delivered(shadow_policy=_policy_block(actual="claude-opus", agree=False)),
            _delivered(
                shadow_policy=_policy_block(chosen=None, agree=None)
            ),  # no verdict
            _delivered(),  # no block (older stack) -> unevaluated, never dropped
            {"event": "llm_call", "shadow_policy": _policy_block()},  # not delivered
        ]
        pa = dashboard._policy_agreement(records)
        self.assertEqual(pa["evaluated"], 3)
        self.assertEqual(pa["agree"], 2)
        self.assertEqual(pa["disagree"], 1)
        self.assertEqual(pa["unevaluated"], 2)
        self.assertEqual(pa["agreement_rate"], round(2 / 3, 3))

    def test_agreement_rollup_empty_is_calm(self):
        pa = dashboard._policy_agreement([])
        self.assertEqual(pa["evaluated"], 0)
        self.assertIsNone(pa["agreement_rate"])
        self.assertEqual(pa["enforced"], {"count": 0, "agree": 0, "disagree": 0})

    def test_enforced_counted_apart_from_shadow(self):
        # goal 27: enforcement visibility. Two enforced records (one clean, one
        # post-rewrite drift) + one shadow record. The enforced split counts
        # ONLY the enforced ones; the overall verdict counts keep counting all.
        records = [
            _delivered(shadow_policy=_policy_block(enforced=True)),
            _delivered(
                shadow_policy=_policy_block(
                    enforced=True, actual="claude-sonnet", agree=False
                )
            ),
            _delivered(shadow_policy=_policy_block()),  # shadow, agree
        ]
        pa = dashboard._policy_agreement(records)
        self.assertEqual(pa["enforced"], {"count": 2, "agree": 1, "disagree": 1})
        self.assertEqual(pa["agree"], 2)
        self.assertEqual(pa["disagree"], 1)


# --- goal 27: the per-dimension rollups ---------------------------------------
# Per-model (demand vs supply), per-user (across keys), per-session (turns +
# pin state), per-backend (deployment traffic) — the folds that turn the
# request/attempt streams into "stats per model/user/session/workbench".


class TestModelRollup(unittest.TestCase):
    def _requests(self, records):
        return dashboard._requests_view(records)

    def test_demand_vs_supply_split_on_fallback(self):
        # qwen3-coder was ASKED for twice but served once; claude-sonnet was
        # never asked for but WON one via fallback — both sides visible.
        records = [
            _delivered(tokens={"total": 10}, response_cost=0.01),
            _delivered(
                served_model="claude-sonnet",
                fallback=True,
                tokens={"total": 6},
                response_cost=0.02,
            ),
        ]
        rows = dashboard._model_rollup(self._requests(records))
        by = {r["model"]: r for r in rows}
        q = by["qwen3-coder"]
        self.assertEqual(q["requested"], 2)
        self.assertEqual(q["served"], 1)
        self.assertEqual(q["fallbacks_in"], 0)
        self.assertEqual(q["tokens_delivered"], 10)
        s = by["claude-sonnet"]
        self.assertEqual(s["requested"], 0)
        self.assertEqual(s["served"], 1)
        self.assertEqual(s["fallbacks_in"], 1)
        self.assertEqual(s["tokens_delivered"], 6)
        self.assertAlmostEqual(s["cost"], 0.02)

    def test_consumed_attributed_to_serving_model(self):
        # The goal-20 join rides into the rollup: a token-burning failed
        # attempt lands in the WINNER's consumed column.
        cid = "obs-m1"
        records = [
            {
                "event": "llm_call",
                "status": "failure",
                "tokens": {"total": 7},
                "correlation_id": cid,
            },
            {
                "event": "llm_call",
                "status": "success",
                "tokens": {"total": 16},
                "correlation_id": cid,
            },
            _delivered(
                served_model="claude-sonnet",
                fallback=True,
                tokens={"total": 16},
                correlation_id=cid,
            ),
        ]
        rows = dashboard._model_rollup(self._requests(records))
        by = {r["model"]: r for r in rows}
        self.assertEqual(by["claude-sonnet"]["tokens_consumed"], 23)

    def test_sorted_busiest_served_first(self):
        records = [
            _delivered(requested_model="a", served_model="a"),
            _delivered(requested_model="b", served_model="b"),
            _delivered(requested_model="b", served_model="b"),
        ]
        rows = dashboard._model_rollup(self._requests(records))
        self.assertEqual([r["model"] for r in rows], ["b", "a"])

    def test_empty_is_empty(self):
        self.assertEqual(dashboard._model_rollup([]), [])


class TestUserRollup(unittest.TestCase):
    def test_aggregates_across_keys(self):
        # THE reason this rollup exists: one user, two virtual keys — the
        # per-key table shows two rows, this shows ONE with keys=2.
        records = [
            _delivered(
                user_id="u1",
                key_alias="repo-a",
                tokens={"total": 5},
                response_cost=0.01,
            ),
            _delivered(
                user_id="u1",
                key_alias="repo-b",
                tokens={"total": 3},
                response_cost=0.02,
                fallback=True,
            ),
            _delivered(user_id="u2", key_alias="repo-a", tokens={"total": 2}),
        ]
        rows = dashboard._user_rollup(dashboard._requests_view(records))
        by = {r["user_id"]: r for r in rows}
        u1 = by["u1"]
        self.assertEqual(u1["requests"], 2)
        self.assertEqual(u1["keys"], 2)
        self.assertEqual(u1["fallbacks"], 1)
        self.assertEqual(u1["tokens"], 8)
        self.assertAlmostEqual(u1["cost"], 0.03)
        self.assertEqual(by["u2"]["keys"], 1)
        # Busiest first.
        self.assertEqual(rows[0]["user_id"], "u1")

    def test_null_user_collapses_into_one_row(self):
        rows = dashboard._user_rollup(
            dashboard._requests_view([_delivered(), _delivered()])
        )
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["user_id"])
        self.assertEqual(rows[0]["requests"], 2)
        self.assertEqual(rows[0]["keys"], 0)


def _session_delivered(key, **kw):
    """A delivered record for one turn of a sticky session."""
    sess = {
        "request_class": "session-turn",
        "stickiness_key": key,
        "key_source": kw.pop("key_source", "tag"),
    }
    return _delivered(session=sess, **kw)


class TestSessionRollup(unittest.TestCase):
    def test_groups_turns_by_stickiness_key(self):
        records = [
            _session_delivered("sess-1", tokens={"total": 4}, response_cost=0.01),
            _session_delivered("sess-1", tokens={"total": 6}, response_cost=0.01),
            _session_delivered("sess-2", tokens={"total": 2}),
            _delivered(),  # one-shot: no session to roll up, never a row
        ]
        rows = dashboard._session_rollup(dashboard._requests_view(records))
        by = {r["stickiness_key"]: r for r in rows}
        self.assertEqual(set(by), {"sess-1", "sess-2"})
        s1 = by["sess-1"]
        self.assertEqual(s1["turns"], 2)
        self.assertEqual(s1["tokens"], 10)
        self.assertAlmostEqual(s1["cost"], 0.02)
        self.assertEqual(s1["key_source"], "tag")

    def test_pin_state_reflects_latest_session_arm_block(self):
        # Stream order (oldest -> newest): pin miss on qwen, then an escalated
        # ENFORCED pin on sonnet. The rollup must show the NEWEST pin state
        # (sonnet, escalated, enforced), with both pin hits counted.
        records = [
            _session_delivered(
                "sess-1",
                shadow_policy={
                    "arm": "session",
                    "stickiness_key": "sess-1",
                    "pin_hit": False,
                    "pinned_backend": "qwen3-coder",
                    "escalated": False,
                    "chosen": "qwen3-coder",
                },
            ),
            _session_delivered(
                "sess-1",
                shadow_policy={
                    "arm": "session",
                    "stickiness_key": "sess-1",
                    "pin_hit": True,
                    "pinned_backend": "claude-sonnet",
                    "escalated": True,
                    "enforced": True,
                    "chosen": "claude-sonnet",
                },
            ),
        ]
        row = dashboard._session_rollup(dashboard._requests_view(records))[0]
        self.assertEqual(row["pinned_backend"], "claude-sonnet")
        self.assertTrue(row["escalated"])
        self.assertTrue(row["enforced"])
        self.assertEqual(row["pin_hits"], 1)

    def test_distinct_backends_served_are_listed(self):
        records = [
            _session_delivered("sess-1", served_model="qwen3-coder"),
            _session_delivered("sess-1", served_model="claude-sonnet"),
            _session_delivered("sess-1", served_model="claude-sonnet"),
        ]
        row = dashboard._session_rollup(dashboard._requests_view(records))[0]
        self.assertEqual(sorted(row["backends"]), ["claude-sonnet", "qwen3-coder"])

    def test_most_recently_active_first(self):
        records = [
            _session_delivered("sess-old", received_at=100.0),
            _session_delivered("sess-new", received_at=200.0),
        ]
        rows = dashboard._session_rollup(dashboard._requests_view(records))
        self.assertEqual([r["stickiness_key"] for r in rows], ["sess-new", "sess-old"])
        self.assertEqual(rows[0]["last_received_at"], 200.0)


class TestBackendRollup(unittest.TestCase):
    def test_folds_attempts_per_deployment(self):
        records = [
            {
                "event": "llm_call",
                "status": "success",
                "backend": "qwen",
                "api_base": "http://wb-a:8000",
                "tier": "local",
                "tokens": {"total": 10},
                "latency_ms": 10.0,
            },
            {
                "event": "llm_call",
                "status": "failure",
                "backend": "qwen",
                "api_base": "http://wb-a:8000",
                "tier": "local",
                "latency_ms": 30.0,
            },
            {
                "event": "llm_call",
                "status": "success",
                "backend": "sonnet",
                "api_base": "http://foundry:9000",
                "tier": "foundry",
                "tokens": {"total": 5},
            },
            _delivered(),  # delivered records never count as attempts
        ]
        rows = dashboard._backend_rollup(records)
        self.assertEqual(len(rows), 2)
        # Busiest first: qwen@wb-a has 2 attempts.
        q = rows[0]
        self.assertEqual(q["backend"], "qwen")
        self.assertEqual(q["api_base"], "http://wb-a:8000")
        self.assertEqual(q["tier"], "local")
        self.assertEqual(q["attempts"], 2)
        self.assertEqual(q["failures"], 1)
        self.assertEqual(q["tokens"], 10)
        self.assertEqual(q["latency_ms_avg"], 20.0)

    def test_same_backend_on_two_bases_is_two_rows(self):
        # Two workbenches serving the same model must NOT collapse — the
        # per-box view is the point.
        records = [
            {"event": "llm_call", "backend": "qwen", "api_base": "http://wb-a:8000"},
            {"event": "llm_call", "backend": "qwen", "api_base": "http://wb-b:8000"},
        ]
        rows = dashboard._backend_rollup(records)
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(r["api_base"] for r in rows),
            ["http://wb-a:8000", "http://wb-b:8000"],
        )

    def test_no_latency_yields_null_average(self):
        rows = dashboard._backend_rollup([{"event": "llm_call", "backend": "x"}])
        self.assertIsNone(rows[0]["latency_ms_avg"])


class TestUnattributedRequests(unittest.TestCase):
    def test_streamed_attempts_counted_as_unattributed_requests(self):
        # Two attempts sharing one correlation_id with no delivered record are
        # ONE unattributed request (a streamed fallback, say); a third with its
        # own id is another. Attempts with no id at all can't be grouped and
        # must not inflate the count.
        records = [
            {
                "event": "llm_call",
                "status": "failure",
                "correlation_id": "s1",
                "tokens": {"total": 2},
            },
            {
                "event": "llm_call",
                "status": "success",
                "correlation_id": "s1",
                "tokens": {"total": 8},
            },
            {"event": "llm_call", "status": "success", "correlation_id": "s2"},
            {"event": "llm_call", "status": "success"},  # no id: tokens-only
        ]
        ov = dashboard._overhead_rollup(records, dashboard._requests_view(records))
        self.assertEqual(ov["unattributed_requests"], 2)
        self.assertEqual(ov["unattributed_attempt_tokens"], 10)

    def test_delivered_requests_are_not_unattributed(self):
        cid = "obs-ok"
        records = [
            {"event": "llm_call", "status": "success", "correlation_id": cid},
            _delivered(correlation_id=cid),
        ]
        ov = dashboard._overhead_rollup(records, dashboard._requests_view(records))
        self.assertEqual(ov["unattributed_requests"], 0)


class TestReceivedAtStamp(unittest.TestCase):
    def test_sink_stamps_arrival_time(self):
        store = dashboard.Records()
        store.add({"event": "delivered"})
        rec = store.all()[0]
        self.assertIn("received_at", rec)
        self.assertIsInstance(rec["received_at"], float)

    def test_existing_stamp_is_never_overwritten(self):
        store = dashboard.Records()
        store.add({"event": "delivered", "received_at": 123.0})
        self.assertEqual(store.all()[0]["received_at"], 123.0)

    def test_request_row_carries_received_at(self):
        row = dashboard._requests_view([_delivered(received_at=42.0)])[0]
        self.assertEqual(row["received_at"], 42.0)


if __name__ == "__main__":
    unittest.main()
