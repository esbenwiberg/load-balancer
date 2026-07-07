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


if __name__ == "__main__":
    unittest.main()
