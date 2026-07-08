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
| `llm_call`  | backend **attempt** (success *or* failure) | `requested_group`, `backend`, `backend_model_id`, `api_base`, `tier`, `latency_ms` (time-to-completion), `ttft_ms` (time-to-first-token, **streamed attempts only** — goal 18), `tokens`, and on failure `error_code`/`error_class`, plus `litellm_call_id`/`trace_id`, and the join key `correlation_id` (goal 16) |
| `delivered` | client **request** (the final response) | `requested_model`, `served_model`, `served_model_id`, `api_base`, `provider`, `response_cost`, `tokens`, `fallback`, the caller's identity `key_alias`/`user_id`/`team_id` (goal 15), and the join key `correlation_id` + winner `litellm_call_id` (goal 16) |

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

### Trace correlation — joining a request to its attempt trail (goal 16)

Both record shapes carry a **`correlation_id`** so the dashboard can nest each
`delivered` request *under* its `llm_call` attempts instead of showing them side
by side. Debugging "why did **this** request fall back?" is then a lookup, not
timestamp-eyeballing.

The id is LiteLLM's request-scoped **`litellm_trace_id`**, which the router
**shares across a whole fallback group**: `Router._update_kwargs_before_fallbacks`
sets it **once, before the fallback loop**, via `setdefault` — so the failed
primary attempt *and* the winner carry the **same** `trace_id`. That already
reaches the `llm_call` records (it's their `standard_logging_object.trace_id`).

The gap this closes is on the **`delivered`** side. It is built in
`async_post_call_success_hook`, and there the shared id is *not* reachable: the
winner's response `_hidden_params` exposes only the winner's *own* `litellm_call_id`
(which differs per attempt), **not** the shared `trace_id`; and on the fallback
path the winner's success `llm_call` — the one record that *would* bridge the
winner's call id to the shared trace_id — is not reliably fired (the quirk above).

**The fix (no gateway fork — it lives entirely in our own callback):**
`obs_callback`'s `async_pre_call_hook` **stamps `data["litellm_trace_id"]`** at
ingress (keeping any client-supplied one). Because the router uses `setdefault`,
our id becomes *the* shared trace_id for every attempt; and because the proxy
threads the **same `data` dict** through `pre_call_hook → the LLM call →
async_post_call_success_hook`, the `delivered` record reads it straight back off
`data`. Result: a **guaranteed shared `correlation_id`** linking a request to
**all** its attempts — the failed primary of a fallback included — on both the
direct and fallback paths, without depending on the unreliable winner success
event. This was verified against `litellm==1.83.14`: on a forced fallback the
failed primary and the winner both log `trace_id == our stamped id`, and the same
id is present on `data` in the success hook.

The winner's own `litellm_call_id` is *also* recorded on `delivered` (a bonus
exact link to the winning attempt *when* its success event fires); `correlation_id`
is the reliable join. The dashboard's `_requests_view` indexes `llm_call` records
by `correlation_id` and nests each request's trail under it (`e2e/dashboard.py`);
`e2e/test_e2e.py::test_dashboard_request_row_joined_to_failure_attempt_by_correlation_id`
proves a forced fallback's request row is joined to its 503 failure attempt, and
the record-level share is checked in `test_fallback_is_observable_in_routing_record`.

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
| `GET /api/records` | the **data endpoint** the page fetches: `{count, requests[], attempts[], keys[], records[]}` |
| `GET /` | the read-only HTML page (auto-refreshes off `/api/records` every 2s) |
| `POST /__reset` | clear the sink (test isolation, like mockd's `/__reset`) |

The page shows three tables: a **Per key** rollup (goal 15 — see below),
**Requests** (requested alias → served backend, a `direct`/`fallback` badge, the
caller's `key`/`user`, provider, tokens, cost — folded from the `delivered`
records, with each row now **nesting its own attempt trail** joined by
`correlation_id`, goal 16) and the **Attempt trail** (every `llm_call`: backend,
tier, status, latency, and on failure the error that triggered a fallback — the
"why"; kept as the flat, cross-request view).

### Identity — *who* asked? (goal 15)

Goals 3 + 12 answered *where* a prompt went but never *whose* it was:
`async_post_call_success_hook` received LiteLLM's `UserAPIKeyAuth`
(`user_api_key_dict`) and discarded it. Goal 15 reads the caller's identity off
it — `key_alias`, `user_id`, `team_id` (goal 11b's key→user→team binding) — and
stamps it onto every `delivered` record (`obs_callback._identity`).

- **Null-safe by design.** All three are `null` when the **master key** or **no
  key store** authenticates the request — the master key carries no
  alias/user/team, and the bare-pytest + cli-auth profiles use it. So those
  profiles keep working and simply carry a null identity; no crash, no phantom id.
- **Surfaced two ways.** The **Requests** table shows each row's `key`/`user`,
  and a **Per key** rollup (`/api/records → keys[]`) aggregates per distinct
  `key_alias`: requests, fallbacks, tokens, cost. Null-identity (master-key)
  traffic collapses into a single `(master key / no key)` row rather than
  scattering or vanishing.
- **Synthetic only.** Test identities are aliases like `repo-a` and ids like
  `e2e-user-…` — never real names or emails (CLAUDE.md guardrail).

The assertion is `e2e/test_e2e.py::test_dashboard_shows_minted_key_identity`: it
mints a key bound to a synthetic alias+user+team, drives a request **with that
key**, and asserts the identity round-trips to the dashboard's `/api/records` on
both the request row and the per-key rollup. Offline shaping (identity
pass-through, rollup aggregation, null collapse) is covered in
`e2e/dashboard_test.py` in the fast tier.

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

## The fleet view — "what's the fleet doing right now?" (goal 13)

Dashboard **v2** adds the other half of the vision: the same page shows which
workbenches are subscribed, with which models, warm/healthy, and how loaded. The
data comes from the control-plane registry ([goal 5](10-control-plane.md)), not
the routing records — the dashboard reads the control-plane's `/models`
server-side and re-serves it at its own **`/api/fleet`** endpoint, which the page
renders under a Fleet section (per-model aggregate + a per-workbench instance
table). See [docs/10 "Why the dashboard PROXIES the registry"](10-control-plane.md)
for the reversible design call (owned/assertable endpoint, no CORS, graceful
degrade to `available:false`).

In the **dev** stack each mockd workbench pushes real heartbeats, so the fleet
view is live and `in_flight` moves as you drive traffic. In the **e2e** stack the
test is the sole (deterministic) heartbeat producer. Assertions in
`e2e/test_e2e.py`: `test_dashboard_fleet_reflects_control_plane_registry` (two
workbenches heartbeat one model → the endpoint aggregates warm/healthy/summed
in-flight and lists both instances) and
`test_dashboard_fleet_surfaces_derived_health` (a workbench reporting
`healthy:false` shows unhealthy *and* is excluded from the aggregate — the derived
health signal survives the whole registry→dashboard path). Offline shaping +
graceful-degrade branches are covered by `e2e/dashboard_test.py` (fast tier).

## Repo + session attribution — the spike (goal 17)

Two questions from the status audit: can spend/routing be sliced **by repo** and
**by session**? Repo is a *solved pattern*; session is a *findings-first spike*
(capture what agents actually send — don't guess).

### Repo granularity — the key-per-repo pattern (no new machinery)

Repo attribution needs **zero** new code and **zero** client hacking: it's goal
11b's key store used as a pattern — **mint one virtual key per repo, with the
repo name as the key's `key_alias`.** Every request on that key is then
attributed to the repo for free:

- `GET /key/info?key=…` → the repo's **aggregate** spend + its `key_alias`;
- each `LiteLLM_SpendLogs` row is hashed to the repo's key (`api_key =
  sha256(key)`), so `/spend/logs` slices the **per-request** ledger by repo;
- goal-15 identity already stamps `key_alias` onto every `delivered` routing
  record, so the dashboard's **Per key** rollup *is* a per-repo rollup.

The proof is `e2e/test_e2e.py::test_spend_attributed_per_repo_key`: it mints two
keys aliased `repo-a`/`repo-b` (synthetic — never a real repo name), drives them
at **different** volumes (1 vs 3 requests), and asserts each key's SpendLogs rows
carry only its own hash **and** that repo-b strictly outspends repo-a — the
falsifiable proof the two repos accrue *separately*, not from a shared pool. See
also e2e/README "Repo-granularity attribution".

### Session granularity — what coding agents actually send (captured, not guessed)

Session attribution needs a fact we didn't have: what identity/session metadata
survives from a real coding agent, through the gateway, to the backend? mockd now
captures every inbound `/v1/*` request — **`GET /__requests`** returns the method,
path, **headers (credential values redacted)**, and body of each. Point a real
client at the **dev** stack and dump it:

```bash
docker compose -f e2e/docker-compose.dev.yaml up -d
# drive Claude Code / Codex at :4000 (see e2e/README "Point a real client…"),
# with SYNTHETIC prompts only — no PII, per CLAUDE.md. Then, on any mockd backend:
curl -s localhost:9101/__requests | python3 -m json.tool   # wb-a mockd
```

`e2e/test_e2e.py::test_session_metadata_capture_through_gateway` exercises the
capture across **both** agent surfaces (Anthropic `/v1/messages`, Codex
`/v1/responses`) with synthetic prompts and asserts the plumbing records the
forwarded request **and never leaks a secret** — so the dump above is reproducible
in CI and safe to paste.

**Finding 1 — what the clients emit at the edge.** Claude Code speaks the
Anthropic Messages API; Codex speaks the OpenAI Responses API. Their SDKs stamp a
recognizable header set (from static analysis of the clients/SDKs; run the dump
against a live client to pin exact values for the pinned versions):

| Surface | Identity/session-bearing fields the client emits |
|---------|--------------------------------------------------|
| **Claude Code** → `POST /v1/messages` | **Headers:** `anthropic-version`, `anthropic-beta`, `x-api-key` **or** `authorization: Bearer …`, `user-agent: claude-cli/<ver>`, `x-app: cli`, and Anthropic-SDK `x-stainless-*` (lang/os/arch/runtime/package-version/retry-count). **Body:** `model`, `system`, `messages`, `tools`, `max_tokens`, `stream`, and **`metadata.user_id`** — a stable per-account hash, the one identity field in the body. No per-conversation session id is sent by default. |
| **Codex** → `POST /v1/responses` | **Headers:** `authorization: Bearer …`, `user-agent`/`originator` naming the Codex CLI, and OpenAI-SDK `x-stainless-*`; Codex additionally emits a **`session_id`** header (per-invocation) — the highest-value session carrier, to confirm against the dump. **Body:** `model`, `instructions`, `input`, `tools`, `stream`, `store`, and **`prompt_cache_key`** (Codex sets this to the session/conversation id). |

**Finding 2 — the gateway hop is a hard boundary.** LiteLLM *terminates* the
client request, authenticates the virtual key, then **re-issues a fresh request
to the backend** with the backend's own credentials. The `/__requests` dump
confirms the consequence: client **transport headers do not survive** —
`user-agent`, `x-stainless-*`, `anthropic-version`, and any custom header (we
send an `x-session-id` in the test to demonstrate this) die at the gateway and
never reach the backend. What crosses is the **translated body**, plus whatever
LiteLLM itself attaches. So a session id set as a *client header* is invisible
downstream, and a client's `metadata.user_id` is consumed by LiteLLM's own user
tracking rather than forwarded verbatim. **A session id therefore needs a
LiteLLM-native carrier, not a raw client header.**

**Finding 3 — the LiteLLM mechanism that could carry a session id end-to-end.**
LiteLLM has a first-class **request-tags / metadata** channel that lands in the
audit surface we already query:

- **`x-litellm-tags: session:<uuid>,repo:<name>`** header, *or* body
  **`metadata.tags: [...]`** — LiteLLM records tags into `LiteLLM_SpendLogs`
  (`metadata`) and exposes per-tag spend at **`GET /spend/tags`**, plus tag-based
  routing/budgets. This is the cleanest "carry a session id and slice spend by
  it" path: it's queryable and durable (Postgres), exactly like goal 11b.
- **Request `metadata`** more broadly is surfaced to the logging callback via
  `standard_logging_object.metadata.requester_metadata` — so obs_callback could
  stamp a session id onto the `delivered` routing record the same way goal 15
  stamps identity, giving the dashboard a per-session view with no new sink.

**No client hacking required:** both agents allow injecting custom request headers
(e.g. Claude Code's `ANTHROPIC_CUSTOM_HEADERS`, Codex's config `http_headers`), so
`x-litellm-tags` can be set without patching either client. (These are LiteLLM
~1.83.x features — verify against the pinned build before building on them.)

**Recommendation (findings only — not built here).** A future goal should
standardize `x-litellm-tags: session:<uuid>` (repo already covered by the
key-per-repo pattern), record it via SpendLogs + a `session_id` on the routing
record, and expose per-session spend at `/spend/tags`. This spike deliberately
makes **no client-side change** — it captures facts and the mechanism; wiring it
end-to-end is a separate, vetted step.

## TTFT for streamed responses — the felt latency (goal 18)

`latency_ms` is **time-to-completion**. For an agent, the *felt* latency is
**time-to-first-token (TTFT)** — how long the client waits before output starts
streaming. Without it, workbench-vs-Foundry comparisons mislead: a local model
with a slow first token can "win" on completion latency while feeling dead. So
every **streamed** `llm_call` record now also carries **`ttft_ms`**.

**Where it comes from — LiteLLM's own timestamp, verified against the pinned
`v1.83.14-stable` (not guessed).** LiteLLM's `StandardLoggingPayload` (the
`standard_logging_object` our callback already reads) carries
`startTime`, `completionStartTime`, `endTime`, and a `stream` flag
(`litellm/types/utils.py`). It stamps `completionStartTime` at the moment the
first token arrives — `Logging._update_completion_start_time`, called from the
streaming wrapper — and marks the payload `stream: true` once the streamed
response is complete. So **`ttft_ms = (completionStartTime − startTime) · 1000`**
on a streamed attempt. For a non-streamed call `completionStartTime` *defaults to*
`endTime`, so TTFT would just equal latency and carries no signal — therefore
`obs_callback._ttft_ms` returns it **only** when the payload is `stream: true`,
and **non-streamed records omit `ttft_ms` entirely**.

**A subtlety this also fixed — what `latency_ms` used to mean for streams.** On
`v1.83.14`, LiteLLM's `response_time` (the old source of `latency_ms`) is **not**
time-to-completion for a streamed call:
`StandardLoggingPayloadSetup.get_response_time` returns
`completionStartTime − startTime` (i.e. TTFT itself) when `stream=True`, and only
`endTime − startTime` otherwise. Sourcing `latency_ms` from `response_time` would
have made it equal TTFT for streams — so `latency_ms` now comes straight from the
raw `endTime − startTime` timestamps (`obs_callback._latency_ms`), keeping it
**time-to-completion for streamed and non-streamed attempts alike**. By
construction `startTime ≤ completionStartTime ≤ endTime`, so **`ttft_ms ≤
latency_ms`** always holds. (Non-streamed `latency_ms` is byte-identical to
before: `endTime − startTime == response_time` when not streaming.)

**Surfaced** on the dashboard's **Attempt trail** (a `ttft` column, and inline in
each request's nested trail chip as `12ms ttft / 40ms`) and on the `/api/records`
attempts. The e2e assertion is
`e2e/test_e2e.py::test_streamed_llm_call_carries_ttft`: it drives a **streamed**
`claude-sonnet` request (direct, so the winner's success event — where TTFT
lives — fires reliably; a fallback winner's success `llm_call` is not guaranteed,
per the quirk above) and asserts the record's `ttft_ms` is present, non-negative,
and `<= latency_ms`. `test_direct_request_routing_record_no_fallback` guards the
complement: a non-streamed attempt **omits** `ttft_ms`.

## Overhead attribution — delivered vs consumed tokens (goal 20)

**Why this exists — the Fugu lesson.** Sakana AI's Fugu (an orchestration model
fronting a pool of frontier LLMs behind one OpenAI-compatible endpoint — the
maximalist cousin of this gateway) was reverse-engineered in 2026-06 delivering
**~2,223 visible tokens while consuming ~22,710** — a **10× overhead invisible
to the client**, buried in orchestration prompts and multi-model calls. A
routing gateway has the same failure mode in miniature: retries and failed
fallback attempts burn backend tokens the `delivered` record never showed. This
instrument exists so that shape **cannot hide here**: what clients got and what
backends burned sit side by side, per request and in aggregate.

**The per-request fields.** Each row of the dashboard's per-request view now
carries **`tokens_delivered`** (the `delivered` record's total — what the
client got) and **`tokens_consumed`** (the sum of `tokens.total` over the
request's goal-16 **joined attempt trail** — failed primary, every retry, and
the winner). Two conventions, both deliberate:

- **No usage reported ⇒ counts 0.** Attempts that report no usage contribute
  nothing — the sum never guesses.
- **The winner is counted exactly once.** When the trail contains a success
  attempt, its tokens are already in the sum. When it does **not** (the
  verified quirk: a fallback winner's success event may not fire — streamed
  winners in particular), the delivered tokens stand in for the winner. Never
  dropped, never double-counted (`dashboard._consumed_tokens`).

**The at-a-glance rollup.** `/api/records` carries an **`overhead`** object —
`{requests, tokens_delivered, tokens_consumed, overhead_tokens,
overhead_ratio, unattributed_attempt_tokens}` — rendered on the Requests
header. A retry-storm or flappy-primary config shows up as a **ratio**, not a
vibe. `unattributed_attempt_tokens` counts `llm_call` tokens whose
`correlation_id` matches **no** `delivered` record — chiefly **streamed
traffic** (no `delivered` record fires for streamed responses on the pinned
LiteLLM — the standing caveat above) plus requests that errored out entirely.
It is surfaced **separately** rather than folded into the ratio, so the
per-request math stays exact and the gap stays visible instead of silently
skewing the number.

**The verified finding (probed live against `v1.83.14-stable`, not guessed):
failed attempts report zero usage.** A 503'd backend never processed the
prompt; LiteLLM's failure event carries `tokens 0/0/0` (confirmed for plain
503, retry-then-fallback, and mid-stream hangup). Two consequences:

- On the mock stack a forced 503-fallback **honestly** shows
  `consumed == delivered` (ratio 1.0) — the failed hop wasted *latency*, not
  gateway-visible tokens. The e2e assertion
  (`test_dashboard_overhead_attribution_direct_and_fallback`) pins this
  premise via the nested trail, so a LiteLLM upgrade that starts billing
  failed attempts breaks the test loudly instead of silently changing the
  metric's meaning. The **summation logic itself** — a token-carrying failed
  attempt ⇒ `consumed > delivered` — is proven offline in
  `dashboard_test.py::TestOverheadAttribution` with synthetic records.
- **Gateway-visible consumed is a LOWER BOUND on true backend burn.** A real
  provider may bill for work a failure event doesn't report (a mid-stream
  death consumed real GPU time and streamed real tokens; the failure record
  still says 0). When real Foundry/Spark backends land, expect the true ratio
  to be ≥ what this shows — never ≤.

## Shadow complexity — request-shape telemetry for the future router (goal 21)

**Why this exists — the other Fugu lesson.** Fugu/TRINITY's (Sakana AI, ICLR
2026) core routing lever is a **per-request complexity gate**: trivial queries
go to one cheap worker, hard ones escalate — TRINITY does it with a ~0.6B
coordinator. Our task-aware router is parked behind the **routing-granularity
decision** (Needs-a-human, GOALS.md) — but the *telemetry* was never blocked.
Every routing record now carries a **`complexity`** tag, so by the time that
decision is made, the router gets designed against **real request
distributions** instead of guesses.

**Two deliberate anti-Fugu constraints** (Fugu's routing is proprietary and
opaque — the reverse of what a governed gateway needs):

- **Deterministic + transparent.** A documented decision tree over request
  features only — no model call, no scoring net — and the **full feature
  vector rides on the record** (`{bucket, approx_prompt_tokens, turns,
  tools}`), so every classification is auditable after the fact
  (`obs_callback_test.py::TestDegradations::test_deterministic` pins
  determinism as a test).
- **Shadow only.** Computed inside the *logging* hooks, after routing is
  decided. It influences nothing: no routing, no latency on the request path.
  The e2e test asserts both requests were served by exactly the backend they
  asked for.

**The decision tree** (`obs_callback._complexity`, precedence order):

| bucket | rule |
|---|---|
| `agentic` | tools offered AND a loop in motion (tool/function-role message, assistant `tool_calls`, or >2 turns with tools) |
| `toolful` | tools offered, single-shot |
| `heavy` | no tools, but approx_prompt_tokens > 2000 or > 4 turns |
| `trivial` | everything else |

`approx_prompt_tokens` is chars/4 over message content **plus the serialized
tool schemas** (tools are injected into the real prompt, so they count). A
crude proxy on purpose — stable, dependency-free, good enough to bucket by;
exact token counts already ride the records (goals 3/20). Unreadable messages ⇒
the tag is **omitted**, never guessed.

**Where it lands.** Both record shapes: `delivered` (classified from the
original request `data`) and best-effort on `llm_call` attempts (from the
logging kwargs) — attempt-level stamping matters because **streamed requests
fire no `delivered` record** on the pinned LiteLLM (the standing caveat), so
the attempt trail is their only carrier. The dashboard shows a bucket badge
per request (feature vector on hover) and `/api/records` carries a
**`complexity_buckets`** distribution — untagged records count as
`unclassified` so the denominator stays honest.

**How it feeds the router later.** Once the routing-granularity decision is
made, the accumulated distribution answers the sizing questions a
complexity-gated router needs: what share of real traffic is `trivial` (a
local-model candidate), how much is `agentic` (needs an `agent_capable=true`
backend — the conformance gate), and whether `heavy` traffic is common enough
to justify a context-length routing rule. The bucket thresholds are the
tunable starting point; the recorded feature vectors are the data to tune them
against.

## What this is *not* (yet)

- **Not durable.** All sinks are ephemeral (stdout ring / mockd + dashboard
  in-memory). Durable, queryable, per-user/team spend is [goal 11b](../GOALS.md)
  (Postgres spend logs).
- **TTFT is per successful streamed attempt.** It rides the success `llm_call`
  record, so it is present for direct streamed routes and for a streamed
  fallback *winner* only when that winner's success event fires (docs/09 quirk);
  the `delivered` summary does not carry it. Aggregated per-model TTFT
  (p50/p95) over these records is a later refinement.
