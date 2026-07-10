#!/usr/bin/env python3
"""
control_plane — the fleet's state registry + heartbeat interface (goal 5).

This is the Phase-1 control-plane *skeleton*: the one genuinely novel component
(docs/06 decision 8). It answers exactly ONE question —

    "What is the fleet's live state?"

— per model, across every workbench that has checked in:

    {warm, in_flight, healthy, agent_capable}

Workbenches PUSH heartbeats (POST /heartbeat) declaring which models they serve
and each model's current state. The registry stores that in SQLite, derives
`healthy` from heartbeat freshness (a stale workbench is unhealthy no matter what
it last claimed), and exposes read-only views: per-instance (/registry) and
aggregated per-model (/models). A future dashboard (goal 13) renders /models
live; a future router reads it to place work.

╔═══════════════════════════════════════════════════════════════════════════╗
║ HARD SCOPE BOUNDARY (goal 5 constraint, docs/10, GOALS.md §Needs-a-human)  ║
║                                                                            ║
║ This service DOES NOT decide where a request goes. It reports state; it    ║
║ never selects a backend. The routing POLICY (how {warm,in_flight,healthy,  ║
║ agent_capable} translate into a backend choice) and the SESSION-STICKINESS ║
║ rule are deliberately NOT here — they are irreversible, design-bearing     ║
║ decisions reserved for a human (GOALS.md §Needs-a-human: "Routing          ║
║ granularity decision"). Adding a `/route` or `/pick` endpoint here would   ║
║ cross that line. Don't.                                                     ║
╚═══════════════════════════════════════════════════════════════════════════╝

DESIGN DECISIONS (reversible calls, made + documented per CLAUDE.md; see
docs/10-control-plane.md for the full rationale + the open questions):
  * SQLite, not Redis. Stdlib (no pip, no extra container), a single durable
    file, ACID upserts. The skeleton has one writer path (heartbeats) and
    read-mostly consumers — Redis's pub/sub + multi-writer story isn't needed
    yet. Swap-in point is documented in docs/10.
  * PUSH heartbeats, not pull-probes. A workbench knows its own warmth and
    in-flight count better than a prober can infer it, and push scales without
    the control plane holding a connection to every box. TTL turns "stopped
    pushing" into "unhealthy".
  * `healthy` is DERIVED: reported_healthy AND (now - last_seen) <= TTL. A
    crashed workbench can't send "unhealthy" — it just goes silent — so
    staleness MUST override the last-known flag. This freshness decay is the
    heart of the state model.
  * Injectable clock (`now_ms`). Real runtime uses the wall clock; tests inject
    a fake so TTL expiry is deterministic (no sleeps, no flakes).

Stdlib only — Python 3.9+. Runs bare (`python control_plane.py`) or in a slim
container with no pip install.

HTTP surface:
  POST /heartbeat   {workbench_id, models:[{model, warm, in_flight,
                     agent_capable, healthy, api_base?}]}
                                                 # upsert; refreshes last_seen
  GET  /registry                                 # per-(workbench,model) rows,
                                                 #   with derived healthy + age
  GET  /models                                   # aggregated per-model state —
                                                 #   the headline view
  GET  /models/<model>                           # one model's aggregate
  POST /deregister  {workbench_id}               # graceful removal of a box
  POST /__reset                                  # clear all state (test isolation)
  GET  /health                                   # 200 liveness

Like mockd/dashboard, the surface is UNAUTHENTICATED — a test/dev daemon; bind
it to localhost / an internal compose network only.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A workbench is HEALTHY only if it has heartbeat within this window. Tunable via
# CONTROL_PLANE_TTL_MS. Kept generous relative to a heartbeat cadence so one
# dropped beat doesn't flap a box to unhealthy.
DEFAULT_TTL_MS = 15_000


def _default_clock() -> int:
    """Monotonic-ish wall clock in ms. Injected as `now_ms` so tests can fake it
    (module-level scripts can't call time in a resumable workflow, but this
    daemon is a plain process — the real clock is correct here)."""
    return int(time.time() * 1000)


class Registry:
    """SQLite-backed fleet state. Thread-safe; the core logic under the HTTP skin.

    One row per (workbench_id, model): a workbench declares each model it serves
    and that model's current {warm, in_flight, agent_capable, reported_healthy}.
    `healthy` in the views is DERIVED — reported_healthy AND fresh — so a silent
    (crashed) workbench decays to unhealthy on its own.

    Tested directly (e2e/control_plane_test.py) with an injected clock; the HTTP
    layer is a thin adapter over these methods."""

    def __init__(
        self,
        db_path: str = ":memory:",
        ttl_ms: int = DEFAULT_TTL_MS,
        now_ms=_default_clock,
    ):
        self._ttl_ms = ttl_ms
        self._now_ms = now_ms
        self._lock = threading.Lock()
        # check_same_thread=False: ThreadingHTTPServer serves each request on its
        # own thread; every access is already serialized by self._lock, so the
        # single connection is safe to share.
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init_schema()

    def _init_schema(self) -> None:
        with self._lock:
            self._db.execute(
                """
                CREATE TABLE IF NOT EXISTS registrations (
                    workbench_id   TEXT    NOT NULL,
                    model          TEXT    NOT NULL,
                    warm           INTEGER NOT NULL DEFAULT 0,
                    in_flight      INTEGER NOT NULL DEFAULT 0,
                    agent_capable  INTEGER NOT NULL DEFAULT 0,
                    reported_healthy INTEGER NOT NULL DEFAULT 1,
                    last_seen_ms   INTEGER NOT NULL,
                    api_base       TEXT,
                    PRIMARY KEY (workbench_id, model)
                )
                """
            )
            # Goal 28: pre-existing db files (CONTROL_PLANE_DB is durable by
            # default) lack the api_base column — add it in place.
            cols = {r[1] for r in self._db.execute("PRAGMA table_info(registrations)")}
            if "api_base" not in cols:
                self._db.execute("ALTER TABLE registrations ADD COLUMN api_base TEXT")
            self._db.commit()

    # --- writes ------------------------------------------------------------

    def heartbeat(self, workbench_id: str, models: list) -> int:
        """Upsert one workbench's per-model state; stamp last_seen = now.

        `models` is a list of dicts: {model, warm?, in_flight?, agent_capable?,
        healthy?, api_base?}. Missing fields default conservatively (not warm,
        no load, not agent-capable, reported healthy, no api_base). Returns the
        count accepted.

        `api_base` (goal 28) is the OpenAI-compatible base URL this workbench
        serves the model on — the join key that lets the dashboard attribute
        gateway attempt traffic (which records api_base) back to a workbench.
        Optional: absent → NULL, and full-snapshot semantics apply like every
        other field — a beat that omits it clears a previously-declared one.

        A heartbeat REPLACES the prior row for each (workbench, model) — it is a
        full snapshot of that model's current state, not a delta. Re-heartbeating
        with a shrunk model list does NOT drop the omitted models here (that
        would make a partial beat look like a deregistration); use /deregister
        for removal. Stale omitted rows decay via TTL instead."""
        if not workbench_id or not isinstance(models, list):
            raise ValueError("heartbeat requires workbench_id and a models list")
        now = self._now_ms()
        accepted = 0
        with self._lock:
            for m in models:
                model = (m or {}).get("model")
                if not model:
                    continue  # skip malformed entries rather than fail the whole beat
                api_base = m.get("api_base")
                if not isinstance(api_base, str) or not api_base.strip():
                    api_base = None  # junk types / empty strings read as absent
                self._db.execute(
                    """
                    INSERT INTO registrations
                        (workbench_id, model, warm, in_flight, agent_capable,
                         reported_healthy, last_seen_ms, api_base)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(workbench_id, model) DO UPDATE SET
                        warm=excluded.warm,
                        in_flight=excluded.in_flight,
                        agent_capable=excluded.agent_capable,
                        reported_healthy=excluded.reported_healthy,
                        last_seen_ms=excluded.last_seen_ms,
                        api_base=excluded.api_base
                    """,
                    (
                        workbench_id,
                        model,
                        1 if m.get("warm") else 0,
                        int(m.get("in_flight") or 0),
                        1 if m.get("agent_capable") else 0,
                        0 if m.get("healthy") is False else 1,
                        now,
                        api_base,
                    ),
                )
                accepted += 1
            self._db.commit()
        return accepted

    def deregister(self, workbench_id: str) -> int:
        """Remove every row for a workbench (graceful shutdown). Returns rows
        deleted."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM registrations WHERE workbench_id = ?", (workbench_id,)
            )
            self._db.commit()
            return cur.rowcount

    def reset(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM registrations")
            self._db.commit()

    # --- reads -------------------------------------------------------------

    def _row_view(self, row: sqlite3.Row, now: int) -> dict:
        age = now - row["last_seen_ms"]
        stale = age > self._ttl_ms
        healthy = bool(row["reported_healthy"]) and not stale
        return {
            "workbench_id": row["workbench_id"],
            "model": row["model"],
            "api_base": row["api_base"],  # goal 28: attempts→workbench join key
            "warm": bool(row["warm"]),
            "in_flight": row["in_flight"],
            "agent_capable": bool(row["agent_capable"]),
            "reported_healthy": bool(row["reported_healthy"]),
            "healthy": healthy,  # DERIVED: reported_healthy AND fresh
            "stale": stale,
            "age_ms": age,
            "last_seen_ms": row["last_seen_ms"],
        }

    def registry(self) -> list:
        """Every (workbench, model) row with derived health + staleness, ordered
        stably (model, then workbench)."""
        now = self._now_ms()
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM registrations ORDER BY model, workbench_id"
            ).fetchall()
        return [self._row_view(r, now) for r in rows]

    def models(self) -> list:
        """Aggregate the per-instance rows into the headline per-MODEL view.

        For each model, across all workbenches serving it:
          warm          — count of instances that are warm AND healthy (a warm
                           row on a stale box can't actually serve, so it doesn't
                           count as warm capacity)
          in_flight     — sum of load across HEALTHY instances (stale in-flight
                           counts are unreliable, so they're excluded)
          healthy       — count of healthy instances
          agent_capable — True iff AT LEAST ONE healthy instance is agent_capable
                           (the model is agent-usable if any live box can do it)
          instances     — the underlying per-instance rows, for drill-down

        This is a STATE projection, not a routing decision — it says what the
        fleet can do, not what a given request should use."""
        agg: dict = {}
        for row in self.registry():
            m = agg.setdefault(
                row["model"],
                {
                    "model": row["model"],
                    "warm": 0,
                    "in_flight": 0,
                    "healthy": 0,
                    "instances_total": 0,
                    "agent_capable": False,
                    "instances": [],
                },
            )
            m["instances"].append(row)
            m["instances_total"] += 1
            if row["healthy"]:
                m["healthy"] += 1
                m["in_flight"] += row["in_flight"]
                if row["warm"]:
                    m["warm"] += 1
                if row["agent_capable"]:
                    m["agent_capable"] = True
        return [agg[k] for k in sorted(agg)]

    def model(self, name: str):
        """One model's aggregate, or None if no workbench serves it."""
        for m in self.models():
            if m["model"] == name:
                return m
        return None


# --- HTTP adapter -----------------------------------------------------------
# A thin skin over Registry. Every handler just parses/serializes; the state
# logic lives in Registry (and is unit-tested there without sockets).

REGISTRY: Registry  # set in main()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        sys.stderr.write("[control-plane] " + (fmt % args) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return None  # signal malformed -> 400

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok", "daemon": "control-plane"})
        if self.path.startswith("/registry"):
            return self._json(200, {"registrations": REGISTRY.registry()})
        if self.path.startswith("/models/"):
            name = self.path[len("/models/") :].split("?", 1)[0]
            m = REGISTRY.model(name)
            if m is None:
                return self._json(404, {"error": "no such model: " + name})
            return self._json(200, m)
        if self.path.startswith("/models"):
            return self._json(200, {"models": REGISTRY.models()})
        return self._json(404, {"error": "not found: " + self.path})

    def do_POST(self):
        body = self._read_body()
        if body is None:
            return self._json(400, {"error": "malformed JSON body"})
        if self.path.startswith("/heartbeat"):
            try:
                n = REGISTRY.heartbeat(
                    body.get("workbench_id"), body.get("models") or []
                )
            except ValueError as e:
                return self._json(400, {"error": str(e)})
            return self._json(200, {"ok": True, "accepted": n})
        if self.path.startswith("/deregister"):
            wid = body.get("workbench_id")
            if not wid:
                return self._json(400, {"error": "deregister requires workbench_id"})
            return self._json(200, {"ok": True, "removed": REGISTRY.deregister(wid)})
        if self.path.startswith("/__reset"):
            REGISTRY.reset()
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "not found: " + self.path})


def main():
    global REGISTRY
    port = int(os.environ.get("CONTROL_PLANE_PORT", "9400"))
    host = os.environ.get("CONTROL_PLANE_HOST", "0.0.0.0")
    ttl_ms = int(os.environ.get("CONTROL_PLANE_TTL_MS", str(DEFAULT_TTL_MS)))
    # Default to a file so state survives a daemon restart; ":memory:" for
    # ephemeral test runs. The dev container mounts a writable path here.
    db_path = os.environ.get("CONTROL_PLANE_DB", "/tmp/control_plane.db")
    REGISTRY = Registry(db_path=db_path, ttl_ms=ttl_ms)
    server = ThreadingHTTPServer((host, port), Handler)
    print(
        "control-plane listening on http://%s:%d "
        "(POST /heartbeat , GET /models , GET /registry) — db=%s ttl_ms=%d"
        % (host, port, db_path, ttl_ms)
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
