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

> ⚠️ **LiteLLM quirk (verified against `v1.83.14-stable`).** On a
> **non-streamed** *proxy fallback*, LiteLLM does not reliably fire a success
> **`llm_call`** event for the deployment that *won* the fallback — only the
> failed primary attempt is guaranteed to log there. That is exactly why the
> `delivered` record exists: it is emitted from the proxy's
> `async_post_call_success_hook` and names the winner even when no success
> `llm_call` was logged for it. Treat `delivered` as the authoritative "who
> served it" and the `llm_call` records as the attempt trail. When the winner's
> success event *does* fire, it shares the failed primary's `trace_id`, so you
> can also correlate the pair that way. (**Streamed** fallbacks behave
> differently: a consumed stream's winner *always* fires its success event on
> this pin — the goal-29 research, § "Streamed requests are first-class".)

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
  verified quirk: a **non-streamed** fallback winner's success event may not
  fire — goal-29 research showed a *consumed stream's* winner always logs),
  the delivered tokens stand in for the winner. Never dropped, never
  double-counted (`dashboard._consumed_tokens`).

**The at-a-glance rollup.** `/api/records` carries an **`overhead`** object —
`{requests, tokens_delivered, tokens_consumed, overhead_tokens,
overhead_ratio, unattributed_attempt_tokens}` — rendered on the Requests
header. A retry-storm or flappy-primary config shows up as a **ratio**, not a
vibe. `unattributed_attempt_tokens` counts `llm_call` tokens whose
`correlation_id` matches **no** `delivered` record — since goal 29 gave
streamed responses a `delivered` record of their own, that means
aborted/mid-stream-dead streams plus requests that errored out entirely.
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
logging kwargs) — attempt-level stamping keeps every attempt auditable alone
(and was streamed traffic's only carrier until goal 29 gave streams a
`delivered` record of their own). The dashboard shows a bucket badge
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

## Shadow session classification — session-turn vs one-shot (goal 22)

**Why this exists.** The decided **hybrid routing granularity**
([docs/03](03-open-questions-and-risks.md) decision block, 2026-07-08) splits
traffic into **sticky sessions** and **freely-routed one-shots**. Its
foundational assumption — *the proxy can tell those apart* — is proven here as
shadow telemetry before any routing policy consumes it. Same discipline as the
complexity tag: deterministic, documented, computed in the logging hooks after
routing — zero influence, zero request-path latency.

**The tag.** Both record shapes carry
`session: {request_class, stickiness_key, key_source}`
(`obs_callback._session`):

- **`request_class`** is transcript shape, *per request*: `session-turn` when
  the transcript shows a conversation in progress (any assistant/tool-role
  message — a coding agent's turn 2+ always matches, since the client replays
  the growing transcript); `one-shot` otherwise. **Honest edge:** turn 1 of a
  real session also looks like a one-shot — the proxy can't see the future.
  The explicit session tag below is what disambiguates turn 1, and that
  asymmetry is precisely the telemetry this goal exposes.
- **`stickiness_key`** is what a sticky router would pin on. Precedence:
  1. **`tag`** — a `session:<id>` entry in the **`x-litellm-tags`** header.
     **Verified on the pinned `v1.83.14` (probed live, not guessed):** the raw
     inbound header map reaches *both* logging surfaces — the delivered hook at
     `data["metadata"]["headers"]` and attempt events at
     `kwargs["litellm_params"]["metadata"]["headers"]` (streamed included) —
     while LiteLLM's own `request_tags` parsing does **not** pick this header
     up on this pin (it only derives User-Agent tags), so the callback reads
     the raw header itself. Auth headers are already stripped from that map by
     LiteLLM, and the callback reads only the one tags key, never emitting the
     header map. Per goal 17: Codex can carry its native `session_id` here;
     Claude Code injects it via `ANTHROPIC_CUSTOM_HEADERS` — no client patching.
     Trusted from turn 1, one-shots included.
  2. **`transcript`** — untagged session-turns: sha256 of the *first user
     turn's content*, truncated to 16 hex chars. Agent transcripts grow
     append-only, so the first user turn is constant across a session — a
     stable key with zero client cooperation (pinned by
     `test_transcript_key_stable_as_transcript_grows`). Documented limitation:
     two sessions opening with byte-identical first prompts collide; the tag
     path is the fix.
  3. **`null`** — an untagged one-shot needs no stickiness.

**Where it lands.** Delivered records AND attempt records (streamed included
since goal 29 — before that the attempt trail was streamed traffic's only
carrier). The
dashboard shows a class badge per request (stickiness key + source on hover)
and `/api/records` carries a **`request_classes`** distribution (untagged ⇒
`unclassified`) — the sticky-vs-free load split the hybrid router's capacity
planning hangs on.

**How it feeds the router.** Goal 23's spec consumes exactly these fields:
`stickiness_key` is the pin for sticky routing, `request_class` gates the
free-routing path, and the accumulated distribution says how much traffic each
policy arm will actually carry.

## Shadow routing policy — the stateless arm (goal 24)

**Why this exists.** The engine fork is decided ([docs/03](03-open-questions-and-risks.md)
engine decision block: **LiteLLM custom policy layer**), which makes the policy
*ours to build* — as hook code. The safest first brick is
[docs/12 §4](12-hybrid-router-spec.md)'s **stateless cheapest-capable policy**,
computed at ingress but **SHADOW**: the decision rides the routing record next
to what actually happened, so its choices are auditable against reality before
anything enforces (goal 26 flips the switch only after this evidence
accumulates). Same anti-Fugu constraints as goals 21/22: deterministic,
inputs-on-record, **zero routing influence, never buffer the stream**.

**The block.** `obs_callback.async_pre_call_hook` computes, **pre-call**, what
the stateless arm would choose, and records stamp it as:

```json
"shadow_policy": {
  "arm": "stateless",
  "candidate_set": ["qwen3-coder", "claude-sonnet", "claude-opus", "gpt"],
  "chosen": "qwen3-coder",
  "reason": "governance: key unrestricted; agent_capable gate not applied (bucket=trivial); health via control-plane 4->4; chose qwen3-coder (tier=local, in_flight=0)",
  "registry": "live",
  "actual": "claude-opus",
  "agree": false
}
```

- **`candidate_set`** — the aliases that survived every filter, in final
  ranked order (`chosen` is its head). Empty ⇒ `chosen: null`, never a guess.
- **`reason`** — the citable audit trail: what each step did, by count, and
  why the winner won. "Why did the policy pick THIS backend?" is answerable
  from the record alone (the anti-Fugu constraint).
- **`actual`/`agree`** — filled post-response: `actual` = the backend that
  really served (`served_model` on `delivered` — authoritative even on the
  fallback path), `agree = chosen == actual`. `agree: null` when there is no
  verdict (no survivor, or no served backend).

**The order, docs/12 §4 verbatim** (`obs_callback._policy_stateless`, a pure
function — offline tests in `obs_callback_test.py` pin every step):

1. **governance** — the calling key's model allowlist (LiteLLM's key-scoped
   `models` off `UserAPIKeyAuth`) bounds the candidate set. Wildcards
   (`all-proxy-models`, `*`) and keyless/master-key traffic are unrestricted.
   Tag-scoped governance is future work (premium-gated on the pin — docs/12 R6).
2. **`agent_capable` gate** — `complexity.bucket ∈ {toolful, agentic}` (the
   goal-21 classifier, reused at ingress) requires an agent-capable backend:
   the **registry verdict** when the model is registered (any healthy instance
   capable), the **config declaration** (`model_info.agent_capable` — the
   conformance gate's declared-for-mocks / earned-for-real-models story)
   otherwise.
3. **health** — control-plane derived `healthy` ([docs/10](10-control-plane.md)
   D3) excludes **registered-but-unhealthy** backends. Models the registry has
   never seen pass on config: workbenches heartbeat, Foundry backends don't,
   so absence-from-registry must not exile the fallback tier.
4. **cheapest capable** — cheaper tier first (`local` < `foundry`; undeclared
   tiers last), tie-break lowest `in_flight` (control-plane; unregistered = 0),
   then name — a total, deterministic order.

**The registry consumption + degrade story (the first consumer of goal 5's
control plane).** The hook reads `CONTROL_PLANE_URL /models` with a short
timeout, TTL-cached (`POLICY_REGISTRY_CACHE_S`, default 2s — the e2e stack
sets 0 so tests see their own heartbeats immediately). When the registry is
**absent** (no URL / unreachable with nothing cached) or **stale**
(unreachable and the last snapshot outlived `POLICY_REGISTRY_STALE_S`,
default 10s), the policy **degrades to config-only candidates** — step 3
becomes a no-op — **and the record says so**: `registry: "absent"|"stale"`
(vs `"live"`), plus the degrade named in `reason`. A blip inside the stale
window rides the last good snapshot as `"live"`. Stacks without a control
plane (bare pytest, cli-auth, local) just carry `registry: "absent"` blocks —
never an error.

**How the block crosses hook boundaries.** The pre-call decision must reach
records built in *other* hooks. It travels by the **goal-16 correlation id**
in a bounded module map: the id is already proven to reach every surface
(delivered records via `data`, attempt events via `slo.trace_id`) — unlike
the request `metadata` dict, whose shape varies across the three inbound
protocols. `delivered` carries the authoritative verdict — streamed requests included
since goal 29; `llm_call` attempts carry the block best-effort (`actual` =
that attempt's alias on success only).

**Zero influence, on record.** The hook never touches `data["model"]`, never
buffers a stream, and any policy error degrades to "no block" — the request
path is untouched. The e2e proofs assert it: every policy test also asserts
the request was served by exactly the backend it asked for, and
`test_shadow_policy_disagrees_when_cheaper_capable_backend_is_healthy` pins
the exact shape enforcement will act on — a request to `claude-opus` while a
healthy `qwen3-coder` is registered yields `agree: false, chosen: qwen3-coder`
while claude-opus still serves. The other two proofs:
`test_routing_records_carry_shadow_policy_block` (the block, whole, with a
non-empty ranked `candidate_set`) and
`test_shadow_policy_candidate_set_respects_key_allowlist` (a governed key's
`candidate_set` excludes the out-of-allowlist backends per request, on record).

**Dashboard.** Each request row gets a **policy** badge — `agree` /
`chose <backend>` (disagreement names the policy's pick) / `no verdict` — with
the chosen backend, registry mode, and full reason on hover. `/api/records`
carries a **`policy_agreement`** rollup `{evaluated, agree, disagree,
unevaluated, agreement_rate}` rendered on the Requests header — the number the
goal-26 enforcement flip will be judged against. Honest-denominator convention
throughout: no-block and no-verdict records count as `unevaluated`, and an
empty stream yields `agreement_rate: null`, not a fake 100%. Session-arm
blocks (below) flow through the same badge and rollup — `agree` semantics are
identical (`chosen == actual`), and the pin story is in the hover reason.

## Shadow sticky pins + escalation — the session arm (goal 25)

**Why this exists.** The stateless arm covers one-shots; the decided HYBRID
granularity ([docs/03](03-open-questions-and-risks.md)) says *sessions route
sticky, with one upward-only escalation hop*. Goal 25 builds those mechanics
([docs/12 §2/§3/§5](12-hybrid-router-spec.md)) — still **SHADOW**, zero
routing influence — on goal 22's stickiness key. The escalation **trigger** is
now **DECIDED (2026-07-23): manual / client-signaled v1**, and goal 31 promoted
goal 25's STUB tag to a first-class contract — see "The escalation contract"
below.

### The escalation contract (goal 31)

The client fires the one upward hop with a **`router:escalate`** entry on the
goal-22 carrier `x-litellm-tags` (comma-separated, next to `session:<id>`).
This is the **public contract** — the promotion of goal 25's stub now that the
trigger is decided ([docs/03](03-open-questions-and-risks.md) escalation-trigger
block, [docs/12 §5](12-hybrid-router-spec.md)). The bare **`escalate`** tag
(goal 25's original name) is a **documented back-compat alias**: same signal,
recognised forever, never required — existing clients and the goal-25 tests do
not change. Read pre-call where the session tag is read, so **zero
request-path latency** is added. `obs_callback._escalate_requested` recognises
either entry (exact match — `router:escalate-please` does **not** count).

**Attribution as a first-class event.** The one firing turn stamps
`escalation_trigger: "manual"` next to `escalated_from` on the record. v1 is
manual-only, so the value is constant today — the **field's existence** is what
lets a future automatic trigger be told apart on-record. It rides only the
firing turn (not the pin-hit turns after), so each escalation is attributable
exactly once. The dashboard's session drill-down (goal 30) surfaces it as a
labeled, visible timeline event (`↑ escalation (manual): old → new`) and the
sessions table's escalated badge carries the trigger label — metadata only, no
prompt content (the governance line holds).

**The trigger-2 telemetry gate.** Trigger 2 (the goal-21 complexity bucket
crossing a line) is adoptable only once the manual-escalation telemetry shows
the bucket signal would have fired where humans did. `GET /api/records` now
carries `escalation_trigger2_gate` `{manual_escalations, would_fire,
agreement_rate, escalate_buckets, min_sample, verdict}`: over the labeled set
(manual escalations), how many had a complexity bucket in the escalate-set
`{heavy, agentic}` at the escalating turn — a recall-flavoured agreement
number (precision needs the non-escalating population, which needs real
traffic we do not fabricate). `verdict` reads **`"insufficient data"`** until
the label count clears `min_sample`, and `agreement_rate` is `null` on an empty
set — the pipe exists and fills as v1 runs; it never manufactures a number.
Full metric definition in [docs/12 §5.1](12-hybrid-router-spec.md).

**Arm dispatch (docs/12 §2).** `async_pre_call_hook` derives the stickiness
key **pre-call** (goal 22's derivation verbatim: `session:<id>` tag >
transcript hash > null; the header map is read from the request metadata,
whichever of its per-surface shapes is present). A key ⇒ the session arm; no
key ⇒ the stateless arm (goal 24, unchanged).

**The pin store** (`obs_callback._PinStore`) is docs/12 §3 option (a) — the
decided default for the single-gateway build phase: **gateway-local**, keyed
by stickiness key, bounded (least-recently-seen eviction past 4096), with an
**inactivity TTL** knob `POLICY_PIN_TTL_S` (default 86400 = the spec's 24h;
every hit refreshes it). First sight of a key runs the stateless arm and
**pins its choice**; subsequent same-key requests carry the pin, bypassing
re-evaluation — a pure pin hit reads no registry and stamps `registry: null`
(no health signal was consulted, and the record must not claim one). Because
this is the *shadow* router's own state, the pin records what the policy
**would have** pinned, not what actually served.

**Backing: a container-scoped SQLite file, not process memory** (knob
`POLICY_PIN_DB`, default under the container's `/tmp` — the control-plane's
own SQLite pattern). Discovered building this: **every profile runs the proxy
with `--num_workers 2`**, and pins are the first *cross-request* state in the
callback — per-process memory gave each worker its own contradictory pin
universe (requests round-robin, so a session's pin "flapped" ~50% of turns).
A multi-worker gateway is already "replicas" in docs/12 §3(a)'s sense. The
file keeps §3(a)'s intent — nothing leaves the gateway container, no shared
infra, no schema in the shared Postgres — while giving all workers one store;
guarded SQL (`INSERT OR IGNORE` for pin-once, `UPDATE … WHERE escalated=0`
for the hop) makes first-writer-wins and **exactly-once escalation atomic
across workers**, not just threads. **Restart story, by design:** a recreated
container starts with a fresh `/tmp`, so pins are lost — the next turn just
re-pins (docs/12 §3's "the cache-loss cost is the same as a restart today").
TTL, restart, worker-sharing, and cross-worker exactly-once are all proven
offline with an injected clock (`obs_callback_test.py`), no docker, no
sleeping. Postgres promotion — needed only when *replicas* (separate hosts)
arrive — stays a later, flagged decision (docs/12 §8.3).

**The block.** Same-key requests carry:

```json
"shadow_policy": {
  "arm": "session",
  "stickiness_key": "e2e-pin-esc-…",
  "pin_hit": true,
  "pinned_backend": "claude-opus",
  "escalated": true,
  "chosen": "claude-opus",
  "reason": "pin hit: qwen3-coder (tier=local, escalated=False); escalate signal: pin qwen3-coder -> claude-opus (upward, exactly once) [governance: key unrestricted; …]",
  "registry": "live",
  "actual": "claude-sonnet",
  "agree": false,
  "escalated_from": "qwen3-coder",
  "escalation_trigger": "manual"
}
```

**Escalation mechanics (docs/12 §5), exactly as spec'd.** The `router:escalate`
signal (or its `escalate` alias) replaces the pin **upward only** — the target is the stateless arm
re-run over the tiers *strictly above* the pin's, so governance, the
agent_capable gate, and health still bound it. The state machine is
`pinned(local) → escalated(foundry)` with **no reverse edge and no second
hop**: the firing request carries `escalated: true` + `escalated_from` (the
old pin — escalation is visible on the record, never silent); any further
signal is a recorded no-op ("already escalated"). A signal that finds **no
capable higher-tier candidate** (already top-tier, or filters emptied the
pool) is also a recorded no-op that does **not** burn the hop — nothing
moved, and a blip must not spend the session's one escalation (the §6
blip-must-not-burn-the-hop spirit, applied to the trigger).

**Proofs.** Offline (`obs_callback_test.py`): the full state machine —
first-sight pinning, stickiness-beats-re-evaluation, per-key independence,
TTL expiry + activity refresh (injected clock), restart-loses-pins-safely,
upward/exactly-once/no-downward, governance-bounded targets, recorded no-ops,
determinism. Live (`test_e2e.py`):
`test_shadow_session_pin_sticks_and_pins_are_independent` (same tag ⇒ same
`pinned_backend` across requests, different tags ⇒ independent pins) and
`test_shadow_escalation_flips_the_pin_upward_exactly_once` (the flip, the
no-op second signal, pin durability, bystander isolation) — every assertion
paired with the zero-influence check that each request was served by exactly
the model it addressed.

## Enforcement — the policy drives routing, behind a flag (goal 26)

**The knob.** `ROUTER_POLICY` — `shadow` (the default: everything above is
pure telemetry, byte-for-byte the pre-goal-26 behavior; the full existing e2e
suite runs against a default-mode gateway) or `enforce`: the owned pre-call
hook **rewrites `data["model"]` to the policy's chosen backend**, for both
arms — a one-shot goes to the cheapest capable candidate, a session-tagged
request goes to its pin, an escalated session to its escalated pin. All
verified mechanics (docs/12 §7 goal-26 research addendum): the hook's
returned data is what routes, on all three surfaces, with streaming
untouched.

**Records under enforce.** The policy block gains `enforced: true` and
`requested` — the client's original ask, **stashed before the rewrite**
because nothing downstream can reconstruct it afterwards (the pipeline sees
only the new model). The block thus carries the full triple: `requested`
(what the client asked), `chosen` (what the policy decided), `actual` (what
really served). Two semantics to know:

- the record's top-level `requested_model` shows the **post-policy** model
  under enforce (it reads `data["model"]`); the client-level ask lives on
  the block. The `fallback` flag keeps meaning "the availability chain
  fired" — under enforce that is `served != chosen`, surfaced as
  `agree: false` + `fallback: true` together.
- the **client's** `response.model` is restored by LiteLLM to the original
  ask on the direct path (enforcement is invisible there) and shows the
  winner on the fallback path — unchanged from today's fallback behavior.

**Failure semantics, proven live (the docs/12 §6 story).** A forced 503 on
the policy-chosen backend follows **that backend's own** fallback chain to a
clean response (R4 — the fallback lookup keys off the rewritten group), and
the pin does **not** move: the next healthy turn is served by the pinned
backend again. A blip neither exiles a session nor burns its one escalation
hop. A policy block with no survivor rewrites nothing — the request proceeds
on the client's own ask, enforcement degrades to shadow for that request,
never to a failure.

**Governance under enforce — the policy is the SOLE guard.** LiteLLM checks
the key's model allowlist only at auth time, against the requested model; a
rewrite is never re-checked (verified on the pin). The policy's governance
filter (candidates bounded by the key allowlist, docs/12 §4 step 1) is
therefore the only thing keeping enforced traffic inside the key's world —
pinned by `test_enforce_governance_is_the_sole_guard`, which proves a
restricted key is never routed to the cheaper out-of-allowlist workbench.

**E2e coverage.** The e2e stack runs a SECOND gateway container
(`litellm-e2e-enforce`, port 4001, `ROUTER_POLICY=enforce`, its own pin
store) so the existing suite keeps hitting the default-mode gateway
unchanged — the "existing tests pass under the default" condition holds by
construction. Dedicated tests: one-shot served by cheapest-capable with the
triple on record; session pin + escalation actually serving; the
503-fallback + pin-does-not-move + recovery story; streaming with proper
terminators on chat//v1/messages//v1/responses; the governance guard.

## Dashboard v3 — stats per model / user / session / backend (goal 27)

The record streams above already carried every dimension a fleet operator asks
about; goal 27 folds them into first-class rollups on `GET /api/records` and
renders each as a table on the page. All folds are pure functions over the
in-memory record store (`e2e/dashboard.py`), pinned offline in
`dashboard_test.py` and asserted live in the e2e suite:

- **`models`** — traffic per model, *demand vs supply*: how often an alias was
  **asked for** vs how often it actually **served** (plus fallback wins, its
  delivered/consumed tokens, cost). Under `ROUTER_POLICY=enforce` the record's
  requested_model is the post-rewrite ask, so demand here is the *router's*
  demand; the client's original ask lives in the policy block. Fleet
  *capacity* per model stays on `/api/fleet` — different question.
- **`users`** — per `user_id` across **all** their virtual keys (the per-key
  table shows a two-key user as two rows; this shows one, with a distinct-key
  count). Nulls collapse into one `(no user)` row.
- **`sessions`** — one row per stickiness key (goal 22's derivation): turns,
  distinct backends served, and the goal-25/26 state made visible — latest
  pinned backend, pin hits, whether the one upward escalation fired, and
  whether any turn was **enforced** vs shadow. Ordered by most recent
  activity, using a sink-side `received_at` stamp (arrival time at the
  dashboard — records carry no wall clock of their own; documented as such,
  never presented as backend time).
- **`backends`** — attempt-trail traffic per concrete deployment
  `(backend, api_base)`: attempts, failures, tokens, mean completion latency,
  tier. This is the honest per-workbench view **today**: the control-plane
  registry has no `api_base`, so a hard attempts→workbench_id join does not
  exist yet (follow-up goal — heartbeat gains `api_base`). In the dev/e2e
  stacks each workbench is a distinct `api_base`, so the table already reads
  per-box.

Two visibility fixes ride along:

- **Enforcement on the page** (the goal-26 gap): the policy badge now
  distinguishes `enforced` (the policy *drove* routing) from shadow
  agree/disagree — under enforcement, disagree means *post-rewrite fallback
  drift* (the chain fired after the rewrite, docs/12 R4), rendered as
  `enforced·drift`. Session-arm chips (`pin`, `esc`) surface goal 25's state
  per request, and `policy_agreement` gains an `enforced` sub-count so the
  strip reads "N enforced (M drift)".
- **The streamed-traffic hole, surfaced** (not fixed here — **closed by goal
  29**, § "Streamed requests are first-class"): at goal-27 time no `delivered`
  record fired for streamed responses, so streamed requests were invisible
  to every per-request fold. `overhead.unattributed_requests` counts
  distinct correlation ids that have attempts but no delivered record, and
  the page surfaces the count instead of silently undercounting — since goal
  29 that means aborted/errored requests, not streamed ones.

## Dashboard v4 — routes, pages & entity drill-downs (goal 30)

Goal 27 gave the dashboard its data; a live UX audit (2026-07-10) showed the
presentation had hit the wall: nine sections on one unbounded scroll, zero
navigation or focusable elements, all explanations hover-only, tables clipping
at laptop widths, and one long identifier able to blow a table out. V4 is the
audit's blueprint, still ONE stdlib file with zero dependencies:

- **Hash-routed views** — `#/overview` (strips + fleet), `#/traffic`
  (per-model + per-backend), `#/identity` (per-key + per-user), `#/sessions`,
  `#/requests` (requests + attempt trail) — behind a persistent header nav.
  All sections remain in the served HTML (the router only toggles
  visibility), so the page-render e2e contract is unchanged.
- **Entity drill-downs** — every user id links to `#/user/<id>` (that user's
  keys, sessions and requests) and every session links to `#/session/<key>`:
  a per-turn table where each turn shows its attempt-trail chips **and the
  policy's full reason + complexity features as visible text** — the "why"
  is no longer hover-only where it matters.
- **Metadata-only session titles** — `<key alias> · <first-seen HH:MM> ·
  <dominant complexity> · <n> turns` (e.g. `repo-a · 14:02 · agentic ·
  3 turns`). Records carry NO prompt content by design, so titles derive
  strictly from metadata; the rollup gained `key_alias`, `user_id`,
  `first_received_at` and `complexity_mix` (additive) to source them.
- **Hardening from the audit** — identifier truncation with full value +
  click-to-copy on detail views; relative `age` columns on request/attempt
  rows (sink-arrival time); `Cache-Control: no-store` on the page shell
  (a stale pre-upgrade page was observed served from browser cache); the
  `/` route tolerates query strings; inline SVG favicon (kills the 404).

`/api/records` and `/api/fleet` stayed byte-compatible (additive keys only) —
every pre-v4 assertion passes unchanged.

## Streamed requests are first-class routing records (goal 29)

**The hole this closes.** On the pinned LiteLLM, `async_post_call_success_hook`
— the `delivered` record's carrier — **structurally never runs for streams**:
every streaming route (chat SSE, Anthropic-messages SSE, the Responses bridge)
early-returns through an SSE generator *before* the proxy reaches
`post_call_success_hook` (`proxy/common_request_processing.py`). So streamed
traffic had attempts but no request row: invisible to every per-request fold,
surfacing only in goal 27's `unattributed_requests` count.

**What the pin actually offers post-stream (the research, probed live on
`v1.83.14-stable` — not guessed):**

- `async_post_call_streaming_hook` — fires **per chunk**, receives only the
  response text; no request dict, no join key. Useless as a record carrier.
- `async_post_call_streaming_iterator_hook` — wraps the final response
  iterator (fallback winner included) with `request_data` in hand. A workable
  carrier, **rejected deliberately**: it sits ON the request path — a bug in
  a pass-through generator breaks live streams, and per-surface usage parsing
  (chat usage chunk vs `message_delta` vs `response.completed`) is fragile.
  Observability must never be able to break the request path.
- **The success event (`async_log_success_event`) — the chosen carrier.**
  `CustomStreamWrapper` fires it when the client's stream is exhausted, with
  the full `StandardLoggingPayload`: `stream: true`, the **assembled token
  usage** (present even when the client never asked for `include_usage`),
  cost, `api_base`, and our pre-call `trace_id`. Crucially, **it fires for a
  streamed fallback WINNER too, carrying the same shared trace_id** —
  verified on all three inbound surfaces. The long-standing "fallback winner
  fires no success event" quirk is a **non-streamed-path** behavior; a
  consumed stream always logs its success attempt on this pin.

**The mechanism** (`obs_callback._delivered_stream_record`): a success event
with `stream: true` yields the request's `delivered` record — marked
`stream: true` so the carrier is on-record — and the existing folds pick it
up unchanged. What the event alone cannot say is what the request was routed
*for* (the winner's `model_group` IS the winner — a fallback would look
direct), *who* asked, and the cross-protocol session tag. Those ride a
**pre-call context stash** keyed by the goal-16 correlation id (the same
bounded-map pattern as the policy blocks): `requested_model` read
post-enforcement (so `fallback` keeps meaning "the availability chain fired",
exactly the non-streamed semantics), identity read off `UserAPIKeyAuth` (the
same source as the non-streamed path), and the session tag derived via the
all-protocol header reader. On a stash miss the record degrades honestly
(requested = served, null identity) rather than guessing. A bounded
**dedupe guard** claims each correlation id so a future litellm upgrade that
fires both carriers can only ever yield one `delivered` per request.

**What deliberately still has no request row:** a stream the client ABORTED
or that died mid-stream fires the *failure* event — nothing was delivered.
Such traffic stays visible via its attempt trail and the unattributed counts,
which since this goal mean "aborted/errored", not "streamed".

Pinned live by `test_streamed_request_is_first_class_in_requests_view`
(direct: row + tokens + minted-key identity + joined trail with `ttft_ms`)
and `test_streamed_fallback_is_first_class_in_requests_view` (503'd primary,
winner serves the stream: `fallback: true`, the 503 "why" and the winner's
success on the same correlation id); the builder's edge cases (dedupe,
stash miss, aborted streams) offline in `obs_callback_test.py`.

## What this is *not* (yet)

- **Not durable.** All sinks are ephemeral (stdout ring / mockd + dashboard
  in-memory). Durable, queryable, per-user/team spend is [goal 11b](../GOALS.md)
  (Postgres spend logs).
- **TTFT is per successful streamed attempt.** It rides the success `llm_call`
  record — present for direct streamed routes AND streamed fallback winners
  (whose success event always fires on this pin, per the goal-29 research);
  the `delivered` summary does not carry it. Aggregated per-model TTFT
  (p50/p95) over these records is a later refinement.
