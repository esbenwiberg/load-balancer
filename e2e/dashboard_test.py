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


if __name__ == "__main__":
    unittest.main()
