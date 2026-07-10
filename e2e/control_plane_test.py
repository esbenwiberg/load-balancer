#!/usr/bin/env python3
"""
Unit tests for the control-plane skeleton (goal 5).

Stdlib `unittest` only — no pytest, no docker, no network beyond a loopback
socket for the HTTP smoke. Two layers:

  * TestRegistry — drives the Registry class directly with an INJECTED clock, so
    TTL/freshness decay is deterministic (no sleeps, no flakes). This is where
    the state model is actually proven: aggregation, warm/in_flight/healthy/
    agent_capable, staleness overriding reported-healthy, deregister, reset.
  * TestHttp — starts the real ThreadingHTTPServer on an ephemeral port and does
    a urllib round-trip, proving the wire adapter (heartbeat -> models, 400s,
    404s) matches the core.

Run:  python3 control_plane_test.py        (also pytest-discoverable)
"""

from __future__ import annotations

import json
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer

import control_plane
from control_plane import Handler, Registry


class FakeClock:
    """A mutable clock in ms. `advance()` moves time forward so TTL expiry is
    exercised without wall-clock sleeps."""

    def __init__(self, start=1_000_000):
        self.t = start

    def __call__(self) -> int:
        return self.t

    def advance(self, ms: int) -> None:
        self.t += ms


class TestRegistry(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        # Short TTL so a single advance() crosses it; in-memory db per test.
        self.reg = Registry(db_path=":memory:", ttl_ms=10_000, now_ms=self.clock)

    def _model(self, name):
        return self.reg.model(name)

    def test_heartbeat_registers_and_aggregates(self):
        n = self.reg.heartbeat(
            "wb-a",
            [
                {
                    "model": "qwen3-coder",
                    "warm": True,
                    "in_flight": 2,
                    "agent_capable": True,
                }
            ],
        )
        self.assertEqual(n, 1)
        m = self._model("qwen3-coder")
        self.assertIsNotNone(m)
        self.assertEqual(m["warm"], 1)
        self.assertEqual(m["in_flight"], 2)
        self.assertEqual(m["healthy"], 1)
        self.assertTrue(m["agent_capable"])
        self.assertEqual(m["instances_total"], 1)

    def test_defaults_are_conservative(self):
        # A bare heartbeat entry: not warm, no load, not agent-capable, but
        # reported healthy (present-and-responding is the sane default).
        self.reg.heartbeat("wb-a", [{"model": "m1"}])
        m = self._model("m1")
        self.assertEqual(m["warm"], 0)
        self.assertEqual(m["in_flight"], 0)
        self.assertFalse(m["agent_capable"])
        self.assertEqual(m["healthy"], 1)

    def test_aggregation_across_workbenches(self):
        # Two boxes serve the same model; a third serves a different one.
        self.reg.heartbeat(
            "wb-a",
            [{"model": "m1", "warm": True, "in_flight": 3, "agent_capable": True}],
        )
        self.reg.heartbeat(
            "wb-b",
            [{"model": "m1", "warm": True, "in_flight": 4, "agent_capable": False}],
        )
        self.reg.heartbeat("wb-c", [{"model": "m2", "warm": False, "in_flight": 0}])
        m1 = self._model("m1")
        self.assertEqual(m1["warm"], 2)
        self.assertEqual(m1["in_flight"], 7)  # summed load
        self.assertEqual(m1["healthy"], 2)
        self.assertTrue(m1["agent_capable"])  # ANY healthy instance suffices
        self.assertEqual(len(m1["instances"]), 2)
        self.assertEqual(len(self.reg.models()), 2)

    def test_multiple_models_per_workbench(self):
        self.reg.heartbeat(
            "wb-a",
            [
                {"model": "m1", "warm": True, "in_flight": 1},
                {"model": "m2", "warm": False, "in_flight": 0, "agent_capable": True},
            ],
        )
        self.assertEqual(len(self.reg.registry()), 2)
        self.assertEqual(self._model("m1")["warm"], 1)
        self.assertTrue(self._model("m2")["agent_capable"])

    def test_reported_unhealthy_is_unhealthy_even_when_fresh(self):
        self.reg.heartbeat(
            "wb-a", [{"model": "m1", "warm": True, "in_flight": 5, "healthy": False}]
        )
        m = self._model("m1")
        self.assertEqual(m["healthy"], 0)  # not counted as healthy
        self.assertEqual(m["warm"], 0)  # warm capacity requires healthy
        self.assertEqual(m["in_flight"], 0)  # load from unhealthy box excluded
        # ...but the underlying row still records what it reported.
        row = self.reg.registry()[0]
        self.assertFalse(row["reported_healthy"])
        self.assertFalse(row["healthy"])
        self.assertFalse(row["stale"])

    def test_staleness_overrides_reported_healthy(self):
        # THE core decay: a box that reported healthy then went silent must flip
        # to unhealthy on its own once TTL lapses.
        self.reg.heartbeat(
            "wb-a",
            [{"model": "m1", "warm": True, "in_flight": 2, "agent_capable": True}],
        )
        self.assertEqual(self._model("m1")["healthy"], 1)

        self.clock.advance(10_001)  # just past ttl_ms=10_000
        m = self._model("m1")
        self.assertEqual(m["healthy"], 0)
        self.assertEqual(m["warm"], 0)
        self.assertEqual(m["in_flight"], 0)
        self.assertFalse(m["agent_capable"])  # no LIVE agent-capable instance
        row = self.reg.registry()[0]
        self.assertTrue(row["stale"])
        self.assertTrue(row["reported_healthy"])  # last claim preserved
        self.assertFalse(row["healthy"])  # but derived-unhealthy

    def test_reheartbeat_refreshes_and_revives(self):
        self.reg.heartbeat("wb-a", [{"model": "m1", "warm": True}])
        self.clock.advance(10_001)
        self.assertEqual(self._model("m1")["healthy"], 0)  # decayed
        # A fresh beat re-stamps last_seen -> healthy again.
        self.reg.heartbeat("wb-a", [{"model": "m1", "warm": True}])
        self.assertEqual(self._model("m1")["healthy"], 1)
        self.assertFalse(self.reg.registry()[0]["stale"])

    def test_heartbeat_is_full_snapshot_upsert(self):
        self.reg.heartbeat("wb-a", [{"model": "m1", "in_flight": 9, "warm": True}])
        # Same (workbench, model) with new numbers REPLACES, not accumulates.
        self.reg.heartbeat("wb-a", [{"model": "m1", "in_flight": 1, "warm": False}])
        self.assertEqual(len(self.reg.registry()), 1)
        m = self._model("m1")
        self.assertEqual(m["in_flight"], 1)
        self.assertEqual(m["warm"], 0)

    def test_deregister_removes_all_rows_for_workbench(self):
        self.reg.heartbeat("wb-a", [{"model": "m1"}, {"model": "m2"}])
        self.reg.heartbeat("wb-b", [{"model": "m1"}])
        removed = self.reg.deregister("wb-a")
        self.assertEqual(removed, 2)
        self.assertIsNone(self._model("m2"))
        self.assertEqual(self._model("m1")["instances_total"], 1)  # wb-b remains

    def test_reset_clears_everything(self):
        self.reg.heartbeat("wb-a", [{"model": "m1"}])
        self.reg.reset()
        self.assertEqual(self.reg.registry(), [])
        self.assertEqual(self.reg.models(), [])

    def test_malformed_entries_are_skipped_not_fatal(self):
        # A models list with a junk entry accepts the good ones, skips the bad.
        n = self.reg.heartbeat("wb-a", [{"model": "m1"}, {"no_model": True}, {}])
        self.assertEqual(n, 1)
        self.assertEqual(len(self.reg.registry()), 1)

    def test_heartbeat_requires_workbench_and_list(self):
        with self.assertRaises(ValueError):
            self.reg.heartbeat("", [{"model": "m1"}])
        with self.assertRaises(ValueError):
            self.reg.heartbeat("wb-a", "not-a-list")

    def test_unknown_model_lookup_is_none(self):
        self.assertIsNone(self._model("nope"))

    # --- api_base (goal 28): the attempts→workbench join key ----------------

    def test_api_base_persists_and_surfaces_on_views(self):
        self.reg.heartbeat("wb-a", [{"model": "m1", "api_base": "http://wb-a:8000/v1"}])
        row = self.reg.registry()[0]
        self.assertEqual(row["api_base"], "http://wb-a:8000/v1")
        # ...and rides the /models per-instance drill-down unchanged.
        inst = self._model("m1")["instances"][0]
        self.assertEqual(inst["api_base"], "http://wb-a:8000/v1")

    def test_api_base_absent_is_null(self):
        self.reg.heartbeat("wb-a", [{"model": "m1"}])
        self.assertIsNone(self.reg.registry()[0]["api_base"])

    def test_api_base_junk_reads_as_absent(self):
        # Non-string / empty values must not be stored as gibberish join keys.
        self.reg.heartbeat("wb-a", [{"model": "m1", "api_base": 42}])
        self.assertIsNone(self.reg.registry()[0]["api_base"])
        self.reg.heartbeat("wb-a", [{"model": "m1", "api_base": "  "}])
        self.assertIsNone(self.reg.registry()[0]["api_base"])

    def test_api_base_is_full_snapshot_like_everything_else(self):
        # A beat that omits api_base CLEARS a previously-declared one — same
        # REPLACE semantics as warm/in_flight, no special-case stickiness.
        self.reg.heartbeat("wb-a", [{"model": "m1", "api_base": "http://wb-a:8000/v1"}])
        self.reg.heartbeat("wb-a", [{"model": "m1"}])
        self.assertIsNone(self.reg.registry()[0]["api_base"])

    def test_schema_migration_adds_api_base_to_old_db(self):
        # A registry created before goal 28 (no api_base column) must open
        # cleanly and gain the column in place — CONTROL_PLANE_DB is a durable
        # file by default, so old schemas exist in the wild.
        import sqlite3
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".db") as f:
            db = sqlite3.connect(f.name)
            db.execute(
                """
                CREATE TABLE registrations (
                    workbench_id   TEXT    NOT NULL,
                    model          TEXT    NOT NULL,
                    warm           INTEGER NOT NULL DEFAULT 0,
                    in_flight      INTEGER NOT NULL DEFAULT 0,
                    agent_capable  INTEGER NOT NULL DEFAULT 0,
                    reported_healthy INTEGER NOT NULL DEFAULT 1,
                    last_seen_ms   INTEGER NOT NULL,
                    PRIMARY KEY (workbench_id, model)
                )
                """
            )
            db.execute(
                "INSERT INTO registrations VALUES ('wb-old', 'm1', 1, 0, 0, 1, 1000)"
            )
            db.commit()
            db.close()

            reg = Registry(db_path=f.name, ttl_ms=10_000, now_ms=FakeClock(2_000))
            row = reg.registry()[0]
            self.assertEqual(row["workbench_id"], "wb-old")  # old row survives
            self.assertIsNone(row["api_base"])  # new column, null for old rows
            reg.heartbeat(
                "wb-old", [{"model": "m1", "api_base": "http://wb-old:8000/v1"}]
            )
            self.assertEqual(reg.registry()[0]["api_base"], "http://wb-old:8000/v1")


class TestHttp(unittest.TestCase):
    """The wire adapter over a real server on an ephemeral port."""

    @classmethod
    def setUpClass(cls):
        # The HTTP layer uses the module-global REGISTRY; wire it to a test one.
        control_plane.REGISTRY = Registry(db_path=":memory:", ttl_ms=10_000)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        self._post("/__reset", {})

    def _url(self, path):
        return "http://127.0.0.1:%d%s" % (self.port, path)

    def _post(self, path, obj):
        data = json.dumps(obj).encode()
        req = urllib.request.Request(
            self._url(path),
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def _get(self, path):
        try:
            with urllib.request.urlopen(self._url(path)) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_health(self):
        code, body = self._get("/health")
        self.assertEqual(code, 200)
        self.assertEqual(body["daemon"], "control-plane")

    def test_heartbeat_then_models(self):
        code, body = self._post(
            "/heartbeat",
            {
                "workbench_id": "wb-a",
                "models": [
                    {
                        "model": "qwen3-coder",
                        "warm": True,
                        "in_flight": 2,
                        "agent_capable": True,
                    }
                ],
            },
        )
        self.assertEqual(code, 200)
        self.assertEqual(body["accepted"], 1)

        code, body = self._get("/models")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["models"]), 1)
        m = body["models"][0]
        self.assertEqual(m["model"], "qwen3-coder")
        self.assertEqual(m["warm"], 1)
        self.assertEqual(m["in_flight"], 2)
        self.assertEqual(m["healthy"], 1)
        self.assertTrue(m["agent_capable"])

        code, m = self._get("/models/qwen3-coder")
        self.assertEqual(code, 200)
        self.assertEqual(m["model"], "qwen3-coder")

        code, body = self._get("/registry")
        self.assertEqual(code, 200)
        self.assertEqual(len(body["registrations"]), 1)

    def test_deregister(self):
        self._post("/heartbeat", {"workbench_id": "wb-a", "models": [{"model": "m1"}]})
        code, body = self._post("/deregister", {"workbench_id": "wb-a"})
        self.assertEqual(code, 200)
        self.assertEqual(body["removed"], 1)
        _, body = self._get("/models")
        self.assertEqual(body["models"], [])

    def test_unknown_model_404(self):
        code, _ = self._get("/models/does-not-exist")
        self.assertEqual(code, 404)

    def test_malformed_json_400(self):
        req = urllib.request.Request(
            self._url("/heartbeat"), data=b"{not json", method="POST"
        )
        try:
            urllib.request.urlopen(req)
            self.fail("expected 400")
        except urllib.error.HTTPError as e:
            self.assertEqual(e.code, 400)

    def test_heartbeat_missing_workbench_400(self):
        code, _ = self._post("/heartbeat", {"models": [{"model": "m1"}]})
        self.assertEqual(code, 400)

    def test_api_base_rides_the_models_drilldown(self):
        # Goal 28 on the wire: a heartbeat declaring api_base must surface on
        # the /models per-instance drill-down (what the dashboard reads).
        self._post(
            "/heartbeat",
            {
                "workbench_id": "wb-a",
                "models": [{"model": "m1", "api_base": "http://wb-a:8000/v1"}],
            },
        )
        code, body = self._get("/models/m1")
        self.assertEqual(code, 200)
        self.assertEqual(body["instances"][0]["api_base"], "http://wb-a:8000/v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
