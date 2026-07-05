# 10 — Control-plane skeleton (Phase-1)

The control plane is the one genuinely novel component in the design
([docs/06 decision 8](06-recommendation.md)). This document covers the
**skeleton** shipped as goal 5: what it is, the decisions already made (with
reasons), and — deliberately louder — the decisions **left open**, several of
which are `Needs-a-human` and are *out of scope on purpose*.

Code: [`e2e/control_plane.py`](../e2e/control_plane.py) ·
tests: [`e2e/control_plane_test.py`](../e2e/control_plane_test.py)

## What it is

A tiny stdlib service that answers exactly one question:

> **What is the fleet's live state, per model?** — `{warm, in_flight, healthy, agent_capable}`

Workbenches **push heartbeats** declaring which models they serve and each
model's current state. The registry stores that in SQLite, derives `healthy`
from heartbeat freshness, and exposes read-only views. A future dashboard
(goal 13) renders it; a future router *reads* it to place work.

```
workbench-a ─┐  POST /heartbeat {workbench_id, models:[{model, warm,
workbench-b ─┤                    in_flight, agent_capable, healthy}]}
foundry ─────┘            │
                          ▼
                   ┌──────────────┐   GET /models     → per-model aggregate
                   │ control-plane │  GET /registry    → per-instance rows
                   │  (SQLite)     │  GET /models/<m>  → one model
                   └──────────────┘   GET /health
```

### The per-model aggregate (the headline `/models` view)

For each model, across every workbench serving it:

| field           | meaning                                                              |
|-----------------|---------------------------------------------------------------------|
| `warm`          | count of instances that are warm **and** healthy (a warm row on a stale box is not real capacity) |
| `in_flight`     | summed load across **healthy** instances only                       |
| `healthy`       | count of healthy instances                                          |
| `agent_capable` | `true` iff **at least one healthy instance** is agent-capable       |
| `instances`     | the per-instance rows, for drill-down (goal 13)                     |

`healthy` is **derived**, not stored: `reported_healthy AND (now - last_seen) <= TTL`.

## ⛔ Scope boundary — what this is NOT

This is a hard line, not a soft preference. The skeleton is **registry + state +
tests only**. It **reports** fleet state; it **never selects a backend**.

Explicitly **not built here**, because they are irreversible, design-bearing
`Needs-a-human` calls ([GOALS.md §Needs-a-human](../GOALS.md), the "Routing
granularity decision"):

- **Routing policy** — how `{warm, in_flight, healthy, agent_capable}` translate
  into "this request goes to backend X". Least-loaded? Warm-first?
  agent_capable-gated? That's a call with product consequences.
- **Session-stickiness** — whether a conversation pins to the instance that
  started it, and for how long / with what escape hatch (see
  [docs/03 risks 1–2](03-open-questions-and-risks.md)).

Adding a `/route` or `/pick` endpoint here would cross that line. The service
gives a *router* everything it needs to decide; it does not decide.

## Decisions made (reversible — decided + recorded per CLAUDE.md)

### D1 — SQLite, not Redis
Stdlib (no pip, no extra container), one durable file, ACID upserts. The
skeleton has a single writer path (heartbeats) and read-mostly consumers, so
Redis's multi-writer + pub/sub story buys nothing yet. **Revisit when:** the
control plane needs to run as more than one replica (shared state), or consumers
want *push* notification of state changes instead of polling `/models` — both
are Redis's sweet spot. The `Registry` class is the swap seam: same methods, a
different backing store.

### D2 — Push heartbeats, not pull-probes
A workbench knows its own warmth and in-flight count better than a prober can
infer it, and push scales without the control plane holding a live connection to
every box. **Consequence:** a crashed workbench simply stops pushing — so
liveness *must* come from staleness (D3), not from the last flag it sent.

### D3 — `healthy` is derived from freshness (TTL)
`healthy = reported_healthy AND (now - last_seen_ms) <= TTL`. Staleness
**overrides** the last-known `reported_healthy`: a box that said "healthy" then
went silent decays to unhealthy on its own once `TTL` (default 15 s, env
`CONTROL_PLANE_TTL_MS`) lapses. This is the heart of the state model and the
most-tested behaviour. A stale instance also drops out of `warm`, `in_flight`,
and `agent_capable` aggregates — stale capacity is not real capacity.

### D4 — Heartbeat is a full snapshot upsert, per `(workbench, model)`
Each beat *replaces* the row for a `(workbench_id, model)` pair — it's the
current truth, not a delta. A beat that *omits* a previously-seen model does
**not** delete it (a partial beat would otherwise masquerade as a
deregistration); omitted rows decay via TTL, or a workbench calls `/deregister`
for a clean shutdown.

### D5 — Injectable clock
The `Registry` takes a `now_ms` callable. Runtime uses the wall clock; tests
inject a fake and `advance()` it, so TTL expiry is deterministic — no `sleep`,
no flakes.

### D6 — Unauthenticated dev/test daemon
Like `mockd` and the dashboard, no auth. Bind to localhost / the internal
compose network only. Auth + who-may-heartbeat is folded into the Azure exposure
decision ([GOALS.md §Needs-a-human](../GOALS.md), goal 14 / first deploy).

## Open questions (for later goals / a human)

1. **Who produces heartbeats?** The skeleton ships the *registry*; nothing in
   the dev stack heartbeats it yet (the `control-plane` service comes up with an
   empty registry). Wiring the mock workbenches to heartbeat — and the dashboard
   to render `/models` live — is **goal 13** (fleet dashboard v2), whose only
   prerequisite is this goal merged.
2. **Is `in_flight` self-reported or gateway-observed?** Today the workbench
   self-reports it in the heartbeat. An alternative is the *gateway*
   incrementing/decrementing a counter as it dispatches — more accurate, but
   couples the control plane to the request path. Deferred.
3. **Is `agent_capable` self-attested or gate-verified?** Today the workbench
   asserts it. The real signal is the `conformance.py` `agent_capable` gate
   ([conformance/](../conformance/)). Whether the control plane *runs* that gate
   (active probe) or merely records a workbench's last gate result is open.
4. **Durability guarantees.** SQLite file survives a daemon restart, but the
   registry is a *cache of live state* — after a restart every row is instantly
   stale (old `last_seen`) until the next beat, which is the correct behaviour.
   No migration story is needed for a cache; revisit if the schema grows.
5. **Multi-replica control plane.** Out of scope (D1). Needs shared state
   (Redis/Postgres) + a leader or shared-store model.

## Local ↔ dev-stack wiring

The dev stack ([`e2e/docker-compose.dev.yaml`](../e2e/docker-compose.dev.yaml))
runs `control-plane` as a standing service on `:9400`, next to the gateway, the
mock workbenches, and the dashboard. **Goal 13 wired it live:** each mockd
workbench now PUSHES heartbeats here (gated on `HEARTBEAT_URL` +
`HEARTBEAT_MODELS`, set per instance in the dev compose), reporting the models it
carries, `warm`/`healthy`, and its **live `in_flight`** (a counter around every
chat/responses request). The goal-12 dashboard reads the registry back through
its own `/api/fleet` endpoint and renders it under a Fleet section — so opening
`http://localhost:9300` while the dev stack is up shows real per-workbench state,
and the in-flight count moves as you drive traffic through `:4000`.

The e2e stack ([`docker-compose.e2e.yaml`](../e2e/docker-compose.e2e.yaml)) also
runs `control-plane` (goal 13 added it) — but **no mockd beats it there.** The
up-test-down merge gate needs the registry present so the fleet assertion can
push its own deterministic heartbeats and read them back through the dashboard;
letting the mock backends beat asynchronously would make that assertion racy.
The heartbeat *producer* wiring therefore lives only in the dev compose.

### Why the dashboard PROXIES the registry (goal 13, reversible call)

The dashboard reads `/models` from the control-plane **server-side** and re-serves
it at its own `/api/fleet`, rather than having the browser fetch the control-plane
directly. This keeps the registry→dashboard data path terminating in an endpoint
*we* own (so the goal-13 assertion is deterministic, same reasoning as
`/api/records`), avoids CORS, keeps the control-plane an internal-network daemon
(only the dashboard is opened in a browser), and degrades gracefully — if the
control-plane is unset or unreachable, `/api/fleet` returns `available:false` at
HTTP 200 and the page shows "fleet unavailable" instead of erroring. Nothing here
decides routing; it only DISPLAYS state (routing policy stays Needs-a-human).

## Tests

`e2e/control_plane_test.py` (stdlib `unittest`, no docker, no pip) — run via
`python3 e2e/control_plane_test.py`, wired into `scripts/check.sh` (fast tier),
so pre-commit, the Stop hook, and CI all cover it:

- **`TestRegistry`** — the state model with an injected clock: registration,
  cross-workbench aggregation (summed `in_flight`, any-healthy `agent_capable`),
  conservative defaults, `reported_healthy=false` ⇒ unhealthy while fresh,
  **staleness overriding reported-healthy** (the D3 decay), re-heartbeat revival,
  full-snapshot upsert (D4), deregister, reset, malformed-entry tolerance.
- **`TestHttp`** — the wire adapter on an ephemeral port: `heartbeat → models`,
  `/registry`, `/models/<m>`, `/deregister`, `/health`, plus `400` on malformed
  JSON / missing `workbench_id` and `404` on unknown model.
