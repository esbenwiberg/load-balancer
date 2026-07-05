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

## What this is *not* (yet)

- **Not durable.** Both sinks are ephemeral (stdout ring / mockd in-memory).
  Durable, queryable, per-user/team spend is [goal 11b](../GOALS.md) (Postgres
  spend logs).
- **Not a dashboard.** The read-only "where did my prompt go?" UI over these
  records is [goal 12](../GOALS.md); this goal is the data layer it reads.
- **Streaming latency caveat.** `latency_ms` on an `llm_call` is LiteLLM's
  `response_time`; for streamed responses that reflects time-to-completion of the
  logged call, not time-to-first-token. TTFT is a later refinement.
