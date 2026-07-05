# 09 — Observability: "where did my prompt go?"

Goal 3. Every request through the gateway must leave a per-request trail of
**{chosen backend, why, latency, tokens, fallback-hit}** — otherwise we can't
tune routing or prove the workbench-vs-Foundry savings
([docs/03 risk 11](03-open-questions-and-risks.md)). This is captured by a
LiteLLM **callback**, with **zero external observability stack** (no Langfuse, no
OTEL collector, no Postgres read).

## The mechanism

`e2e/obs_callback.py` is a LiteLLM `CustomLogger` wired in via
`litellm_settings.callbacks: obs_callback.routing_recorder`. It emits two record
shapes and ships them to one or two sinks.

### The two record shapes

Keyed by `event`:

| `event`     | fired per…       | carries |
|-------------|------------------|---------|
| `llm_call`  | backend **attempt** (success *or* failure) | `requested_group`, `backend`, `backend_model_id`, `api_base`, `tier`, `latency_ms`, `tokens`, and on failure `error_code`/`error_class`, plus `litellm_call_id`/`trace_id` |
| `delivered` | client **request** (the final response) | `requested_model`, `served_model`, `served_model_id`, `api_base`, `provider`, `response_cost`, `tokens`, `fallback` |

**Why two?** A fallback has two halves — *why the primary was abandoned* and
*who ultimately answered*:

- The `llm_call` **failure** record is the **why**: e.g. `requested_group:
  qwen3-coder`, `status: failure`, `error_code: 503`, `tier: local`,
  `latency_ms: 14.5`.
- The `delivered` record is the **outcome**: `requested_model: qwen3-coder`,
  `served_model: claude-sonnet`, so **`fallback = requested_model !=
  served_model`** — the chosen backend and the fallback-hit flag, with the
  delivered token usage.

> ⚠️ **LiteLLM quirk (verified against `v1.83.14-stable`).** On a *proxy
> fallback*, LiteLLM does not reliably fire a success **`llm_call`** event for
> the deployment that *won* the fallback — only the failed primary attempt is
> guaranteed to log there. That is exactly why the `delivered` record exists: it
> is emitted from the proxy's `async_post_call_success_hook` and names the winner
> even when no success `llm_call` was logged for it. Treat `delivered` as the
> authoritative "who served it" and the `llm_call` records as the attempt trail.
> When the winner's success event *does* fire, it shares the failed primary's
> `trace_id`, so you can also correlate the pair that way.

### The two sinks

Both are independent and safe — publishing runs *after* the response is returned,
and any sink error is swallowed, so observability can never break a request.

1. **stdout — always on.** One JSON object per line, prefixed `ROUTING_RECORD `.
   This is the production-friendly, dependency-free path.

   ```bash
   docker logs litellm-gateway 2>&1 | grep '^ROUTING_RECORD ' | sed 's/^ROUTING_RECORD //' | jq .
   ```

2. **webhook — opt-in** via `OBS_WEBHOOK_URL`. If set, each record is POSTed
   there. Used by the e2e stack (below); unset in `deploy/`, so prod relies on
   stdout.

## Reading it in e2e (the machine-verified path)

The e2e compose sets `OBS_WEBHOOK_URL=http://mockd:9100/__observe`, so the
records land in **mockd**'s in-memory sink. mockd is a single process, so it
centralizes records across *both* gateway workers (`--num_workers 2`), which an
in-callback list could not. `/__reset` (called by the test suite's autouse
fixture before and after every test) clears the records too, so each test sees
only its own.

```bash
cd e2e && ./run.sh --keep                       # bring the stack up, leave it
curl -s localhost:9100/__observe | jq '.records'   # all records so far
```

The assertion that proves a fallback is observable lives in
`e2e/test_e2e.py::test_fallback_is_observable_in_routing_record`: it forces
`qwen3-coder → claude-sonnet`, then reads `/__observe` and asserts both the
`delivered` record (`fallback: true`, `served_model: claude-sonnet`, tokens) and
the `llm_call` failure record (`error_code: 503`, `tier: local`, latency).
`test_direct_request_routing_record_no_fallback` is the baseline
(`fallback: false`), guarding against an always-true flag.

## Reading it in deploy (the real gateway)

`deploy/litellm-config.yaml` wires the **same** callback (single source of truth:
`deploy/docker-compose.yaml` mounts `../e2e/obs_callback.py`). No webhook is set,
so records go to **stdout only** — scrape `docker logs` as shown above, or point
a log shipper at the container. When a real observability backend is chosen
(Langfuse / OTEL / Postgres spend logs — the goal-11b direction for durable,
queryable spend), it slots in as an additional callback; these stdout/webhook
records stay as the dependency-free floor.

## The dashboard — "where did my prompt go?" (goal 12)

Goal 3 is the data layer; **goal 12** is the read-only view over it. `e2e/dashboard.py`
is a stdlib-only daemon (same shape as mockd) that is *both* a routing-record
**sink** and a tiny **read-only web page**:

| route | purpose |
|-------|---------|
| `POST /records` | obs_callback webhook target — append one record |
| `GET /api/records` | the **data endpoint** the page fetches: `{count, requests[], attempts[], records[]}` |
| `GET /` | the read-only HTML page (auto-refreshes off `/api/records` every 2s) |
| `POST /__reset` | clear the sink (test isolation, like mockd's `/__reset`) |

The page shows two tables: **Requests** (requested alias → served backend, a
`direct`/`fallback` badge, provider, tokens, cost — folded from the `delivered`
records) and the **Attempt trail** (every `llm_call`: backend, tier, status,
latency, and on failure the error that triggered a fallback — the "why").

### Build-vs-reuse (a reversible call, decided per CLAUDE.md)

We **build** this thin page rather than **reuse** LiteLLM's bundled admin UI:

- **The data is ours.** The `{requested alias, backend served, fallback?, tier,
  latency, tokens}` shape is produced by obs_callback (goal 3). LiteLLM's UI
  renders its own SpendLogs/keys/teams and has no notion of our fallback *why*
  (the 503 that triggered it, the backend tier).
- **Machine-verifiable.** Goal 12 requires an e2e assertion on the data endpoint;
  an owned JSON endpoint is deterministically assertable, a React SPA behind
  master-key auth is not.
- **Dependency-free floor.** Keeps the "zero external observability stack"
  invariant this doc opens with — no Langfuse/OTEL, stdlib only.
- **Read-only, minimal auth surface.** LiteLLM's UI is read-write (mint keys) and
  needs the master key in-browser; this view only reads.
- **Reversible.** Records still flow to stdout + Postgres, so adopting LiteLLM's
  UI or Grafana later forecloses nothing.

### Where records flow now

obs_callback's `OBS_WEBHOOK_URL` accepts a **comma-separated list** and fans each
record to every sink. The **e2e** stack sets it to *both*
`http://mockd:9100/__observe` (the goal-3 suite reads records back there) *and*
`http://dashboard:9300/records` (the goal-12 page + its data-endpoint test). The
**dev** stack (which had no obs wiring before goal 12) sets it to the dashboard
only — its mockd containers are the *backends*, so there's no central mockd sink;
`docker compose -f docker-compose.dev.yaml up -d` then open `http://localhost:9300`
to watch routes land live. `deploy/` stays stdout-only.

The assertions live in `e2e/test_e2e.py`:
`test_dashboard_data_endpoint_shows_direct_request` (a direct route appears,
`fallback:false`), `test_dashboard_data_endpoint_shows_fallback_route` (a forced
`qwen3-coder → claude-sonnet` shows the request row *and* the 503 attempt row),
and `test_dashboard_page_renders` (the page serves and is wired to `/api/records`).

## What this is *not* (yet)

- **Not durable.** All sinks are ephemeral (stdout ring / mockd + dashboard
  in-memory). Durable, queryable, per-user/team spend is [goal 11b](../GOALS.md)
  (Postgres spend logs).
- **Not per-request hard-correlated.** The dashboard's per-request rows come from
  `delivered` records, which carry no `trace_id` (see the quirk box above), so
  the attempt trail is shown *alongside* requests, not joined to them by id.
  Fleet-level correlation is a later refinement.
- **Streaming latency caveat.** `latency_ms` on an `llm_call` is LiteLLM's
  `response_time`; for streamed responses that reflects time-to-completion of the
  logged call, not time-to-first-token. TTFT is a later refinement.
