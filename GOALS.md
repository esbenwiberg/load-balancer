# Goals backlog — the holistic plan

## The endgame (north star)

One endpoint any coding agent can `/connect` to — Claude Code, Codex, generic
OpenAI clients — hosted in **Azure**, fronting **Foundry** plus a fleet of
workbenches (mock ones first: mockd, or haiku-backed via cli-auth; real Sparks
later). A **dashboard** on the balancer shows where every prompt was routed and
why, which workbenches are subscribed with which models, and their live load.
The same stack runs **locally in one command** — gateway in dev mode + mock
workbenches + mock Foundry — so the agents building it can self-validate every
change without real infra. That local stack *is* the harness: goals 10, 3, 12.

## How to start a goal

Every completion condition below is a **self-contained `/goal` payload**:
constraints, proof requirements, and a stop bound live inside the block, so a
run needs nothing else. Two ways to kick one off:

**Morning one-liner** (recommended — only the number changes):

```
/goal read GOALS.md, quote goal <N>'s completion condition verbatim into the conversation, then work until that quoted condition literally holds — it carries its own constraints, proof requirements, and stop bound
```

**Or paste the block** — copy the goal's completion condition into `/goal`
as-is.

`/goal` re-checks the condition after every turn using a small fast model that
sees only the conversation — it runs no commands and reads no files. That is
why every condition demands proof be *surfaced*: exit codes, gate output, and
merge confirmations must appear in the transcript. How to behave while working
(branch → PR → auto-merge-if-green, document reversible calls, when to stop)
is defined once in [`CLAUDE.md`](CLAUDE.md) — read it before your first
unattended run.

## How to pick

- **On vacation / unattended → only pick from § Autonomy-friendly.** These are
  self-contained and machine-verifiable (`e2e/run.sh` is the arbiter), so a run
  can complete and merge without you.
- **At the keyboard → anything.** The § Needs-a-human goals require real infra,
  external sign-off, or an irreversible design decision that is *yours* to make.
- Respect prerequisites (stated inside the conditions). Lower number ≈ higher
  priority / fewer prerequisites — but see **Current focus** below, which
  overrides raw numbering.
- Blast radius today is "the repo" — nothing deploys from `main` yet. That's the
  standing assumption behind auto-merge (CLAUDE.md). Revisit when it changes.

**Current focus (2026-07):** we are **not** taking in Spark workbenches yet.
The "complete the idea" arc (harness core, dev stack, observability, wallet
guards, both dashboard halves, control plane, Ollama profile, Azure IaC) is
**done** — see § Done. The 2026-07-07 status audit opened a new arc:
**attribution + observability refinement** — its first batch (identity 15,
repo/session 17, manual profile 19, trace join 16, TTFT 18) is **done** —
see § Done, as is the Fugu-inspired pair — overhead attribution (20) and the
shadow complexity signal (21). **The keystone routing-granularity decision is
MADE (2026-07-08): HYBRID** — sticky sessions, free per-request stateless
routing, one upward-only escalation hop ([docs/03](docs/03-open-questions-and-risks.md)
decision block). The router arc's build-up is COMPLETE: shadow session
classification (22) and the hybrid-router spec (23, [docs/12](docs/12-hybrid-router-spec.md))
are done — the router is specified and its telemetry is accumulating. **The
engine fork is DECIDED (2026-07-09): LiteLLM custom policy layer** — archgw
was renamed into Plano (early-stage, session affinity undocumented); re-look
gate ≥ 2027-01 + documented affinity ([docs/03](docs/03-open-questions-and-risks.md)
engine decision block, [docs/12 §7](docs/12-hybrid-router-spec.md)). That
unblocked the **policy-layer build arc: goals 24 → 25 → 26** (shadow stateless
policy → shadow pins + escalation mechanics → enforcement flip behind a flag).
**The whole arc is DONE** — the stateless arm (docs/09
"Shadow routing policy", first live consumer of the control-plane registry),
the session arm (docs/09 "Shadow sticky pins": container-scoped SQLite pins
on goal 22's stickiness key + the upward-only exactly-once escalation state
machine, STUB client-signaled trigger), and the **enforcement flip** (docs/09
"Enforcement"): behind `ROUTER_POLICY=enforce` (default `shadow` — every
existing profile and test unchanged) the policy layer now actually DRIVES
routing, with the requested/chosen/served triple on-record and dedicated
enforce-mode e2e coverage against a second gateway container. **The
task-aware router exists.** The **escalation trigger is now DECIDED
(2026-07-23): manual / client-signaled v1**, automatic triggers telemetry-gated
([docs/03](docs/03-open-questions-and-risks.md) escalation-trigger decision
block). **Goal 31 built that contract** (2026-07): `router:escalate` is
first-class (bare `escalate` kept as a back-compat alias), the firing turn is
attributable (`escalation_trigger: "manual"` + a labeled dashboard event), and
the trigger-2 telemetry gate is materialised on `/api/records` (reads
"insufficient data" until real v1 traffic fills it) — see § Done. What remains
open: flipping a real deployment to enforce (an ops call, not a build),
Spark-infra-shaped work (still parked), and the discovered client-side emit
half (goal 32).

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

_The policy-layer build arc (24 → 25 → 26, engine decided 2026-07-09:
LiteLLM custom policy layer) is COMPLETE, the goal-27 dashboard-rollup
follow-ups (28 + 29) are done, and **goal 31 is done** (`router:escalate`
first-class contract + trigger-2 telemetry gate — see § Done). The only vetted
autonomy-friendly item left is the discovered follow-up below._

- **32. `agent_capable`-style conformance probe for the escalation contract on
  a real client.** Completion condition: on `main`, there is a documented,
  test-covered way for a coding agent (Claude Code / Codex) to EMIT the
  goal-31 `router:escalate` signal through the gateway — the client-side other
  half of the contract goal 31 deliberately did not touch. Autonomy-friendly
  IF it stays synthetic: extend `e2e/README` (or a helper) showing exactly how
  the `x-litellm-tags: session:<id>,router:escalate` header is set on each of
  the three inbound surfaces, plus a smoke that a header set the way a real
  client would set it reaches the pre-call hook (reuse the goal-31 e2e path).
  **Stop / draft-only if** proving it needs a REAL Claude Code / Codex process
  wired to a live gateway (that's a client-config + UX decision — when/how a
  human triggers the hop — and belongs with a human). Ship to PR → auto-merge
  if both gates green.

---

## § Needs-a-human (do NOT run unattended)

These block on real infra, external sign-off, or an irreversible call. Bring
them up when you're present; several become autonomy-friendly *after* the
decision is made.

- **Real Spark inventory** ([RUNBOOK step 0](deploy/RUNBOOK.md)) — ⏸ **parked**:
  we're not taking in Spark workbenches yet (see Current focus). Needs actual
  boxes, pinned models, memory headroom, vLLM tool-call parser. Infra.
- **Data-governance sign-off with DISCO** ([docs/03 risk 10](docs/03-open-questions-and-risks.md))
  — is Foundry OK for the intended work; residency/retention. External.
- ~~**Routing granularity decision**~~ — **✅ DECIDED 2026-07-08: HYBRID**
  (sticky sessions + free per-request stateless + one upward-only escalation
  hop) — recorded in [docs/03 risks 1–2](docs/03-open-questions-and-risks.md)
  (decision block after risk 2). Unblocked goals 22–23 above. The remaining
  sub-decision is the **escalation trigger** (next bullet).
- ~~**Escalation trigger for the hybrid router**~~ — **✅ DECIDED 2026-07-23:
  manual / client-signaled v1** (namespaced `router:escalate` tag), with the
  automatic triggers as telemetry-gated follow-ups — recorded in
  [docs/03](docs/03-open-questions-and-risks.md) (escalation-trigger decision
  block) + [docs/12 §5/§8.1](docs/12-hybrid-router-spec.md). Short version:
  it's the only option decidable without real traffic (the harness has none),
  fits every hard constraint by construction, and its manual escalations
  become the eval set that gates adopting the complexity-threshold trigger
  next. Verify-then-escalate rejected (buffers the stream + "model said so");
  a rule-based structural check and N-fallback rotten-pin kept as later
  signals. **This unblocked goal 31 below** (promote the stub → first-class
  contract + write the trigger-2 telemetry gate) — now § Autonomy-friendly.
- ~~**LiteLLM-only vs `archgw`/Plano evaluation**~~ — **✅ DECIDED 2026-07-09:
  LiteLLM custom policy layer** — recorded in [docs/03](docs/03-open-questions-and-risks.md)
  (engine decision block after risk 2) against [docs/12 §7](docs/12-hybrid-router-spec.md)'s
  R1–R9 table. Short version: LiteLLM has R1/R2/R4/R5/R9 verified on our pin
  and R3 is a small owned component; archgw was renamed into **Plano**
  (2026-01-10, early-stage) with session affinity — the one switch-worthy
  feature — undocumented. **Re-look gate: ≥ 2027-01 AND documented session
  affinity.** Still open + separable: Katanemo's open-weights *router model*
  as a learned taster inside our deterministic policy (docs/12 §8 decision 5,
  gated on shadow-telemetry evidence) — adoptable without Plano the proxy.
  Unblocked goals 24–26 above.
- **First Azure deploy + exposure model** (after goal 14) — subscription/resource
  choices, private endpoint vs public + IP allowlist, TLS, dashboard auth, who
  gets keys and how they rotate. A hosted OpenAI-compatible proxy with Foundry
  creds behind it is a *target*; a leaked master key is someone else's free LLM.
  Creds + security + outward-facing. *(Noted for later — build phase is
  local/test only, nothing hosted yet. Decide the release model first ⤵)*
- **Release model BEFORE anything deploys from `main`** — the moment the
  balancer deploys from `main`, CLAUDE.md's tripwire kills auto-merge and
  vacation autonomy with it. Decide *in advance*: manual promotion, a release
  branch, or tagged deploys. *(Decision 2026-07: fine as-is for now — nothing
  deploys, it's all testing. Revisit at the first real deploy, not before.)*
- **Real-Foundry traffic through the hosted balancer** — *(build phase: nothing
  is live and nothing routes to Foundry during unattended runs — mock/synthetic
  only, per the CLAUDE.md guardrail.)* When the balancer goes live for real
  prompts, the DISCO sign-off above gates it.
- **Verify prompt-caching on the Azure/Anthropic route** ([docs/03 risk 5](docs/03-open-questions-and-risks.md))
  — needs real Foundry creds. Infra.

---

## Done

**How to mark a goal complete** (do this in the same PR that finishes it):
1. *Delete* the goal's entry from its section above — don't leave a tombstone.
2. Add one line here: `- ✅ <goal number + title> — PR #<n> (<yyyy-mm>)`, plus
   any follow-up goals discovered, added above as new numbered entries.
3. The completion condition itself is the tag: a goal is "complete" only when
   its condition literally holds on `main` — if in doubt, re-check it, don't
   trust the checkmark.

- ✅ 31. Promote the escalation stub to the first-class `router:escalate`
  contract + write the trigger-2 telemetry gate — the manual/client-signaled
  trigger (decided 2026-07-23) is now a supported contract: `obs_callback`
  recognises the namespaced **`router:escalate`** entry on `x-litellm-tags`
  (via `_escalate_requested`, exact-match), with the bare **`escalate`** tag
  kept as a documented back-compat alias (goal 25's tests/e2e untouched). The
  firing turn stamps `escalation_trigger: "manual"` alongside `escalated_from`
  (rides the firing turn only, so each escalation counts once), and the
  dashboard session drill-down (goal 30) surfaces it as a labeled, visible
  timeline event + a trigger-labeled escalated badge (metadata only). The
  **trigger-2 telemetry gate** is materialised as `/api/records →
  escalation_trigger2_gate` `{manual_escalations, would_fire, agreement_rate,
  escalate_buckets, min_sample, verdict}` — recall of the complexity signal
  (`{heavy, agentic}`) against the human labels, reading honestly as
  "insufficient data" until the label count clears the floor (never a
  fabricated rate). Shadow default byte-for-byte unchanged; zero request-path
  latency (tag read where goal 25 reads it). New e2e proves `router:escalate`
  AND the `escalate` alias each fire the one upward hop under
  `ROUTER_POLICY=enforce` (traffic follows the flipped pin, exactly once, no-op
  on a second signal) + the gate pipe present on `/api/records`; 5 offline gate
  tests (agreement math + the low-count "insufficient data" path) + escalation
  attribution + tag-recognition tests. Docs: docs/09 "The escalation contract",
  docs/12 §5/§5.1, docs/03 decision block. Discovered follow-up: goal 32
  (client-side emit half). — PR #<pending> (2026-07)
- ✅ 28. Workbench traffic join — heartbeat carries `api_base` — the
  control-plane heartbeat accepts an optional `api_base` per (workbench,
  model) row (full-snapshot semantics like every other field: an omitting
  beat clears it; junk/empty values read as absent; in-place ALTER TABLE
  migration for pre-goal-28 db files), persisted and exposed on /registry +
  /models instance drill-downs. The dashboard's per-backend rollup joins
  attempt `api_base` → `workbench_id` server-side in /api/records via the
  same fleet read /api/fleet uses (exact match after trailing-slash strip —
  no URL canonicalization guessing; an api_base claimed by two workbenches
  is ambiguous → dropped, never guessed; control-plane down/unconfigured →
  null workbench, table reads exactly as before), and the backend-traffic
  table gained the workbench column. mockd workbenches declare it via
  HEARTBEAT_API_BASE (wired in the dev stack to each box's litellm-config
  api_base). 5 new control_plane tests + 9 dashboard tests + 1 e2e proving
  degrade-then-join live. — PR #52 (2026-07)
- ✅ 29. Streamed requests become first-class routing records — the demanded
  research first (probed live on the pin, documented in docs/09 "Streamed
  requests are first-class"): `async_post_call_success_hook` structurally
  never runs for streams (every streaming route early-returns through an SSE
  generator); the per-chunk streaming hook carries no request dict/join key;
  the iterator hook works but sits ON the request path — rejected. The
  chosen carrier: the post-stream SUCCESS EVENT, which fires with
  stream:true, assembled usage and our pre-call trace_id on all three
  surfaces — **including the streamed fallback WINNER** (the "winner fires
  no success event" quirk is non-streamed-only — key research finding).
  `obs_callback._delivered_stream_record` turns it into the request's
  `delivered` record (marked stream:true, folds pick it up unchanged);
  requested-vs-served/identity/session ride a pre-call context stash keyed
  by the goal-16 correlation id (requested read post-enforcement so
  `fallback` keeps its meaning; identity from UserAPIKeyAuth, same source as
  non-streamed; session via the all-protocol header reader). Bounded dedupe
  guard = exactly-one-delivered per request even if a future pin fires both
  carriers. Aborted/mid-stream-dead streams deliberately stay unattributed
  (nothing was delivered) — the dashboard's unattributed labels now say so.
  11 new offline builder tests + 2 e2e (direct streamed row incl. minted-key
  identity + joined trail with ttft; streamed 503-fallback row with the
  winner serving and the "why" on the trail). — PR #51 (2026-07)
- ✅ 30. Dashboard v4 — routes/pages + entity drill-downs (the UX-audit
  blueprint) — the single scrolling page became hash-routed views
  (`#/overview` strips+fleet, `#/traffic`, `#/identity`, `#/sessions`,
  `#/requests`) behind a persistent header nav, still ONE stdlib file, zero
  deps; every user id links to `#/user/<id>` (their keys, sessions,
  requests) and every session to `#/session/<key>` — a per-turn table where
  each turn shows its attempt-trail chips AND the policy reason + complexity
  features as VISIBLE text (the "why" is no longer hover-only). Sessions get
  METADATA-ONLY titles (`key alias · first-seen · dominant complexity ·
  turns` — no prompt content, governance line holds) sourced from additive
  rollup fields {key_alias, user_id, first_received_at, complexity_mix}.
  Audit hardening: identifier truncation + click-to-copy, relative `age`
  columns (sink-arrival), `Cache-Control: no-store` on the page shell
  (stale-page bug observed live), query-string-tolerant `/` route, inline
  SVG favicon. API shapes stayed byte-compatible; page-render e2e contract
  unchanged by construction (all sections remain in the served HTML). Also
  repaired goal 29's entry (its header was accidentally swallowed by PR
  #49's insertion). docs/09 "Dashboard v4". — PR #50 (2026-07)
- ✅ 27. Dashboard v3 — stats per model / user / session / backend +
  enforcement visibility — four new pure folds on `GET /api/records`, each a
  table on the page: `models` (traffic demand vs supply per alias), `users`
  (per user_id across ALL their keys, distinct-key count), `sessions` (one
  row per stickiness key: turns, latest pin state, pin hits, the one
  escalation, enforced vs shadow, recency-ordered via a sink-side
  `received_at` stamp), `backends` (per (backend, api_base) deployment —
  the honest per-workbench view until the registry carries api_base, goal
  28). The goal-26 gap closed on the page: the policy badge distinguishes
  ENFORCED routing from shadow opinion (`enforced·drift` = post-rewrite
  fallback), with pin/esc chips (goal 25) and an `enforced` sub-count on
  `policy_agreement`. The streamed-traffic hole SURFACED (not fixed):
  `overhead.unattributed_requests` counts requests the per-request folds
  can't see (→ goal 29). 19 new offline shaping tests; e2e asserts the
  rollups live incl. the full sessions row on the enforce pin+escalation
  path. docs/09 "Dashboard v3". — PR #48 (2026-07)
- ✅ 26. Enforcement flip — policy drives routing behind a flag —
  `ROUTER_POLICY` knob (default `shadow`: everything byte-for-byte unchanged,
  proven by the untouched existing suite running against the default
  gateway; `enforce`: the owned pre-call hook rewrites the requested model
  to the policy's choice for BOTH arms incl. the stub escalation).
  `_apply_enforcement` stashes the client's original ask on the block
  pre-rewrite (nothing downstream can reconstruct it — PR #46 research) so
  records carry `enforced:true` + requested/chosen/served; top-level
  requested_model shows the post-policy model, `fallback` keeps meaning
  "the chain fired" (served != chosen). E2e: second gateway container
  (`litellm-e2e-enforce`, :4001, own pin store) + 5 dedicated tests —
  cheapest-capable actually serves (client-visible stamp + triple on
  record + response.model restored to the ask on the direct path), pin +
  stub escalation serve (traffic follows the pin flip, exactly once), R4
  live (503 on the chosen backend → clean 200 via ITS chain, pin does NOT
  move, next healthy turn re-served by the pin — §6 proven), streaming
  untouched on all three surfaces, governance sole-guard (LiteLLM never
  re-checks the allowlist post-rewrite — the policy filter is the only
  wall). 5 new offline tests (71 total). Healthcheck windows widened after
  `depends_on` made a latent prisma-boot tightness binding. Docs: docs/09
  "Enforcement", docs/12 banners+§6, e2e/README. Flipping a REAL deployment
  to enforce is an ops call, deliberately not made here. — PR #47 (2026-07)
- ✅ 25. Shadow sticky pins + escalation mechanics (stub trigger) — the
  session arm (docs/12 §2/§3/§5) in shadow: `_PinStore` + `_policy_session`
  in obs_callback, dispatched pre-call by goal-22's stickiness key (tag >
  transcript hash; keyless stays goal-24 stateless). First sight pins the
  stateless arm's choice; hits carry `{arm: "session", stickiness_key,
  pin_hit, pinned_backend, escalated}` with `registry: null` honesty (no
  evaluation ran); an `escalate` entry on x-litellm-tags (STUB — the real
  trigger stays § Needs-a-human) replaces the pin upward exactly once, no
  downward edge, second signal a recorded no-op, impossible moves don't burn
  the hop. Inactivity TTL knob POLICY_PIN_TTL_S (default 24h). **Build
  discovery:** every profile runs `--num_workers 2`, so process-memory pins
  flapped per worker — the store is a container-scoped SQLite file
  (POLICY_PIN_DB, control-plane's own pattern; guarded SQL makes pin-once +
  escalate-once atomic across workers; recreated container ⇒ fresh /tmp ⇒
  safe re-pin). Postgres promotion still the replica-time §8.3 decision.
  Tests: 20 new offline (state machine, TTL/restart via injected clock,
  worker-sharing, cross-worker exactly-once) + 2 e2e (stickiness +
  independence, escalation exactly-once + bystander isolation — every step
  asserting zero routing influence). Docs: docs/09 "Shadow sticky pins",
  docs/12 §3/§5 status + discovery. — PR #45 (2026-07)
- ✅ 24. Shadow routing policy — the stateless arm, zero influence — routing
  records carry `shadow_policy: {arm: "stateless", candidate_set, chosen,
  reason, registry, actual, agree}` computed PRE-CALL in
  `obs_callback.async_pre_call_hook`, applying docs/12 §4 verbatim (key
  governance allowlist → agent_capable gate for toolful/agentic buckets →
  control-plane derived health → cheaper tier first, tie-break lowest
  in_flight, then name — total order). First live consumer of the goal-5
  registry (TTL-cached read, e2e cache 0 for determinism); absent/stale
  registry degrades to config-only candidates with `registry:
  "absent"|"stale"` on the record. Block crosses hooks via the goal-16
  correlation id (bounded map) — delivered carries the authoritative
  actual/agree, attempts carry it best-effort (the streamed-traffic carrier).
  Dashboard: per-request policy badge (chosen + reason + registry on hover) +
  `policy_agreement` rollup {evaluated, agree, disagree, unevaluated,
  agreement_rate}. Tests: 19 offline policy cases (order, filters, degrade,
  determinism) + dashboard shaping + e2e (a) block with non-empty ranked
  candidate_set, (b) claude-opus request while healthy qwen3-coder registered
  ⇒ agree:false chosen:qwen3-coder AND served untouched (zero influence), (c)
  governed key's candidate_set excludes out-of-allowlist backends. Docs:
  docs/09 "Shadow routing policy". Tag-scoped governance deferred
  (premium-gated on the pin, docs/12 R6). — PR #44 (2026-07)
- ✅ 23. Hybrid-router design spec — [docs/12](docs/12-hybrid-router-spec.md):
  request classification (consumes goals 21+22 verbatim — promotion to routing
  input is semantic, not a rewrite), the decision table, sticky-pin semantics
  (derivation per goal 22; pin-store call spec'd as gateway-memory now →
  Postgres at replica time, control-plane rejected to keep docs/10's
  registry-not-router-state boundary), upward-only single-escalation state
  machine (pin replacement, transcript re-sent as-is, re-ingestion visible via
  goal 20's overhead instrument, `escalated` flag required on records),
  stateless cheapest-capable policy (governance filter → agent_capable gate →
  health → cost/in_flight order), failure semantics (pinned-backend-down
  follows the fallback chain WITHOUT moving the pin — a blip must not burn the
  session's one hop), and the R1–R9 requirements table feeding the
  LiteLLM-vs-archgw evaluation. All open calls flagged ⛔ Needs-a-human
  (trigger, engine, pin store at replica time, streaming-latency override).
  No code/config/routing changes. — PR #41 (2026-07)
- ✅ 22. Shadow session classification — session-turn vs one-shot — routing
  records (delivered + llm_call, streamed covered) carry
  `session: {request_class, stickiness_key, key_source}` from
  `obs_callback._session`: class = transcript shape (assistant/tool turns ⇒
  session-turn; honest edge documented — turn 1 of a real session looks
  one-shot, the explicit tag disambiguates); key precedence tag >
  transcript-hash > null (tag = `session:<id>` in `x-litellm-tags`, VERIFIED
  by live probe on v1.83.14 to reach both logging surfaces at
  metadata.headers — litellm's own request_tags does NOT parse it on this pin,
  so the callback reads the raw header; transcript key = sha256 of the first
  user turn, stable across append-only growth, collision limitation
  documented). Dashboard: class badge (key+source on hover) +
  `request_classes` distribution. Tests: 14 offline classifier cases +
  shaping tests + e2e `test_routing_records_carry_shadow_session_classification`
  (one-shot / session-turn / same-tag-same-key / different-tag-different-key).
  Docs: docs/09 "Shadow session classification". — PR #40 (2026-07)
- ✅ 21. Complexity-signal spike — shadow-mode request classifier — every
  routing record (delivered + best-effort llm_call, so streamed traffic is
  covered via the attempt trail) carries a `complexity` tag from a
  deterministic, fully-auditable decision tree over request features only
  (`obs_callback._complexity`: buckets trivial/toolful/heavy/agentic; the
  whole feature vector {bucket, approx_prompt_tokens, turns, tools} rides the
  record), computed in the logging hooks AFTER routing — zero influence, zero
  request-path latency. Dashboard: bucket badge per request (features on
  hover) + `complexity_buckets` distribution in /api/records (untagged ⇒
  `unclassified`, honest denominator). Tests: 13-case offline suite
  (`obs_callback_test.py`, new fast-tier step in check.sh — litellm stubbed) +
  dashboard shaping tests + e2e
  (`test_routing_records_carry_shadow_complexity`: trivial one-liner vs
  tool-heavy multi-turn land in different buckets, both served by the backend
  they asked for). Docs: docs/09 "Shadow complexity" (incl. how the
  distribution feeds the routing-granularity decision). — PR #38 (2026-07)
- ✅ 20. Router-overhead attribution — delivered vs consumed tokens (the Fugu
  10x lesson) — per-request `{tokens_delivered, tokens_consumed}` on the
  dashboard view (consumed = Σ over the goal-16 joined attempt trail; no-usage
  attempts count 0; the winner counted exactly once — from its success attempt
  when logged, inferred from the delivered record when not, per the verified
  quirk) + an `/api/records` `overhead` rollup {delivered, consumed,
  overhead_tokens, ratio, unattributed_attempt_tokens (streamed/aborted
  traffic, surfaced separately so it can't skew the ratio)} rendered on the
  page. **Condition amended during the run (decide-and-document):** the
  original test premise "forced fallback ⇒ consumed > delivered" was probed
  live and found impossible on the pinned litellm v1.83.14 — FAILED attempts
  report zero usage (verified for 503, retry-then-fallback, and mid-stream
  hangup; the failed hop burns latency, not gateway-visible tokens). So:
  summation proven offline with synthetic token-carrying failures
  (`dashboard_test.py::TestOverheadAttribution`, consumed > delivered), and the
  e2e test (`test_dashboard_overhead_attribution_direct_and_fallback`) pins the
  real behaviour — direct AND 503-fallback show consumed == delivered, with
  the zero-usage premise asserted via the nested trail so a litellm upgrade
  that starts billing failures fails loudly. Gateway-visible consumed is
  documented as a LOWER BOUND. Docs: docs/09 "Overhead attribution". — PR #37 (2026-07)
- ✅ 18. TTFT for streamed responses — PR #34 (2026-07)
- ✅ Phase-0 groundwork (blockers A & B, conformance harness, deploy scaffold) — PR #1
- ✅ E2E test harness (mock + cli-auth profiles) — PR #2
- ✅ Goal-driven workflow (GOALS.md backlog + unattended contract) — PR #3
- ✅ 0. One check script + githooks + agent self-validation — PR #7 (2026-07)
- ✅ 1. Wire the e2e harness into CI — PR #9 (2026-07)
- ✅ 2. Mid-stream-death fallback test + pinned retry/stream semantics (risk 7) — PR #11 (2026-07)
- ✅ 6. mockd fault modes (429, transient 5xx, malformed tool-call) + pinned retry-before-fallback order + fixed cross-test cooldown flake — PR #13 (2026-07)
- ✅ 7. Tool-calling coverage on the Anthropic surface (`conformance.py --api anthropic`: /v1/messages tools+streaming, full read→edit→bash round-trip + probes, wired into run.sh) — PR #14 (2026-07)
- ✅ 8. Harness self-checks + guardrail automation (mockd-direct conformance step in run.sh; negative-path e2e tests — malformed JSON + unknown alias → clean 4xx on all three surfaces; LiteLLM image-pin guard in check.sh enforced by pre-commit/Stop/CI) — PR #15 (2026-07)
- ✅ 9. Concurrency smoke — parallel streams across 4 aliases with a 503-fault forcing a fallback mid-fleet; asserts each response keeps its own served_model stamp (no cross-request bleed) + clean stream termination, plus a mixed chat/responses/messages variant for interleaved-SSE-across-protocols — PR #16 (2026-07)
- ✅ 10. Dev-mode stack — standing dev profile (docker-compose.dev.yaml): gateway + two distinct mock workbench containers (workbench-a/-b) + mock-foundry, each stamping served_model=<model>@<instance>; dev_smoke.sh proves all 3 surfaces (messages/chat/responses) route to distinct containers; per-instance /__control faults; README wires Claude Code + Codex; real-haiku variant documented but keyless-offline stays default — PR #17 (2026-07)
- ✅ 3. Observability & cost attribution — per-request routing records via a LiteLLM callback (`e2e/obs_callback.py`): `llm_call` per backend attempt (backend, tier, latency, tokens, and on failure the 503/429 that triggered fallback) + `delivered` per request (requested vs served ⇒ fallback flag, tokens). Sinks: stdout `ROUTING_RECORD <json>` (prod) + webhook to mockd `/__observe` (e2e). `test_fallback_is_observable_in_routing_record` proves a fallback is captured; deploy/ wired for parity; doc [docs/09](docs/09-observability.md). Verified quirk: LiteLLM fallback winner fires no success event — captured via `async_post_call_success_hook`. — PR #19 (2026-07)
- ✅ 11b. Users, teams, and spend audit — per-model costs on `qwen3-coder` so mockd traffic accrues nonzero spend; keys minted bound to a user grouped into a team (`/team/new` → `/team/member_add` → `/key/generate`); `test_spend_attributed_to_key_user_team` proves a `SpendLogs` row + `/key|user|team/info` aggregates attribute spend to the right key+user+team, `test_spend_survives_gateway_restart` restarts the gateway container and proves the Postgres-backed ledger + issued key persist (the open persistence question answered); audit queries + SQL documented in e2e/README "Spend audit" — PR #20 (2026-07)
- ✅ 12. Routing dashboard v1 — "where did my prompt go?" — read-only stdlib dashboard (`e2e/dashboard.py`, `:9300`) over goal-3 routing records: a record **sink** (`POST /records`) + **data endpoint** (`GET /api/records`: per-request view {requested→served, fallback, tier, latency, tokens} + attempt trail) + read-only **page** (`GET /`). Build-vs-reuse fork decided **BUILD** (not LiteLLM's admin UI): the record shape is ours, an owned JSON endpoint is assertable, keeps the zero-dep floor, read-only, reversible — documented in `dashboard.py` header + [docs/09](docs/09-observability.md). `obs_callback` gained comma-separated multi-sink fan-out so e2e feeds both `mockd/__observe` (goal-3 suite unchanged) and the dashboard. Wired into e2e + dev stacks (dev had **no** obs wiring before this). `test_dashboard_data_endpoint_shows_direct_request` / `_shows_fallback_route` / `_page_renders`. — PR #21 (2026-07)
- ✅ 5. Phase-1 control-plane skeleton — stdlib SQLite-backed fleet registry + heartbeat interface (`e2e/control_plane.py`, `:9400`) exposing per-model `{warm, in_flight, healthy, agent_capable}`; `healthy` is DERIVED (reported-healthy AND heartbeat-fresh, TTL decay) so a silent workbench self-decays; push heartbeats + full-snapshot upsert per (workbench,model); aggregates across workbenches (summed in_flight, any-healthy agent_capable). Registry+state+tests ONLY — routing policy + session-stickiness deliberately NOT built (Needs-a-human). 19 stdlib `unittest` tests (`e2e/control_plane_test.py`: Registry state model w/ injected clock + HTTP wire) wired into check.sh fast tier; standing `control-plane` service added to dev compose (empty registry; goal 13 wires producers). Decisions + open questions in [docs/10](docs/10-control-plane.md) — PR #22 (2026-07)
- ✅ 13. Fleet dashboard v2 — who's subscribed, with what, under what load — the
  dashboard reads the goal-5 control-plane registry via its own owned `/api/fleet`
  endpoint (server-side proxy of `/models`; reversible call — owned+assertable,
  no CORS, degrades to `available:false`) and renders a Fleet section: per-model
  {warm, healthy/total, in-flight, agent} + a per-workbench instance table with
  derived health. `control-plane` added to the e2e stack; mockd gained an
  optional heartbeat producer (gated on `HEARTBEAT_URL`) + a live in-flight
  counter, wired in the **dev** stack so each workbench beats real state (e2e
  keeps the test as the sole deterministic producer). Assertions:
  `test_dashboard_fleet_reflects_control_plane_registry` +
  `test_dashboard_fleet_surfaces_derived_health` (registry→dashboard path),
  `dashboard_test.py` (offline shaping + degrade, fast tier), `dev_smoke.sh` step
  4 (live fleet). Docs: [docs/09](docs/09-observability.md), [docs/10](docs/10-control-plane.md). — PR #23 (2026-07)
- ✅ 11. Budgets + rate limits per virtual key — `default_key_generate_params` (max_budget/rpm/tpm) in litellm-config.e2e.yaml so every issued key inherits a config default; e2e proves a bare key inherits the defaults, an over-budget key (max_budget:0) → clean 400 budget_exceeded, an over-rate-limit key (rpm:1) → clean 429 (never 5xx/hang); README documents the knobs + how to raise them + the goal-11/11b units boundary (dollar-spend accrual is 11b) — PR #18 (2026-07)
- ✅ 4. Local-model (Ollama) e2e profile — a dedicated `local` profile
  (`docker-compose.local.yaml` + committed keyless `litellm-config.local.yaml`)
  running Ollama serving a real small coding model as the workbench behind the
  same gateway; self-contained bring-up (`ollama-entrypoint.sh` pulls+warms the
  model, healthcheck-gated so the gateway never boots against an empty Ollama);
  `run.local.sh` runs `conformance.py` THROUGH the gateway against the real model
  and surfaces the JSON verdict (alias stays `qwen3-coder` — only the backend
  swaps; `OLLAMA_MODEL` drives both the pull and what the gateway requests).
  Hard constraint honoured: NEVER in CI — `run.sh`/CI use the mock profile only,
  `docker compose config` validates the file in the fast tier but starts no
  containers. Docs: e2e/README "Profile: local" + docs/08. Merge gate = mock
  `e2e/run.sh` green. — PR #25 (2026-07)
- ✅ 4 (follow-up 2). Local profile GPU fast path — `run.local.sh --native-ollama`
  keeps the gateway containerized (prod parity + pinned image) but points it at a
  **host-run Ollama** on the Mac's Metal GPU via `OLLAMA_API_BASE` (new env knob:
  `api_base: os.environ/OLLAMA_API_BASE`; `docker-compose.local-native.yaml` =
  gateway-only, no Ollama container). Runner preflights the host (install-check,
  starts the daemon on `0.0.0.0`, pulls the model). Verified end-to-end: qwen3:8b
  on Metal (`size_vram=5.6GB`), `agent_capable=true`, ~28 tok/s vs single-digit
  CPU-in-Docker. Fully-containerized default unchanged (portable + CI; the GPU
  path on a Linux/NVIDIA host). Docs: e2e/README "Fast path" + docs/08. — PR #27 (2026-07)
- ✅ 4 (follow-up). Local profile made GREEN — default model → `qwen3:8b`, which
  clears the conformance gate for real (`agent_capable=true`): structured tool
  calls, the full multi-turn Read→Edit→Bash task, and both probes, over streaming.
  Established the model ladder (documented in e2e/README + docs/08): `qwen3:8b`
  passes; `qwen3:4b` structures calls + passes probes but won't drive the loop
  (answers in prose); `qwen2.5-coder:3b/:7b` leak tool calls (no `<tool_call>`
  wrapper). Slow CPU-only (reasoning mode) but never a CI/merge gate. — PR #26 (2026-07)
- ✅ 15. Identity in routing records — *who* asked? — `delivered` records now
  carry the caller's synthetic identity `{key_alias, user_id, team_id}` sourced
  from LiteLLM's `UserAPIKeyAuth` (`obs_callback._identity`; null under the master
  key / no key store, so bare-pytest + cli-auth are unaffected). The dashboard's
  per-request table shows key/user and gains a **Per key** rollup (`/api/records →
  keys[]`: requests, fallbacks, tokens, cost; master-key traffic collapses into
  one null-alias row). `test_dashboard_shows_minted_key_identity` mints a
  synthetic alias+user+team key, drives a request with it, and proves the identity
  round-trips to `/api/records` (row + rollup); offline shaping covered in
  `dashboard_test.py`. Synthetic ids only, no PII. Docs: [docs/09](docs/09-observability.md). — PR #31 (2026-07)
- ✅ 14. Azure IaC skeleton — code only, offline-validated — Bicep (decision recorded over Terraform: `bicep build` validates fully offline; Azure-native; stateless) under `deploy/azure/`: `main.bicep` + modules for the gateway Container App (managed identity, Key Vault–referenced secrets, parameterised ingress), PostgreSQL Flexible Server (private persistent store), Key Vault (secrets + MI RBAC), and VNet (delegated subnets + NSG). Secrets are required `@secure()` params with no defaults; `main.example.bicepparam` carries commit-safe placeholders. `scripts/check.sh` fast tier gained an offline `bicep build`/`build-params` step (fails on ANY diagnostic, no cloud calls/creds), CI installs bicep via `az bicep install`, and the litellm image-pin guard now also covers `.bicep`. Parity doc [docs/11](docs/11-azure-iac.md) maps every dev-stack component to its Azure counterpart (with the deliberate gaps named). — PR #24 (2026-07)
- ✅ 19. Promote the manual try-out stack to a committed profile — PR #30 (2026-07)
- ✅ 17. Repo-granularity attribution + session-metadata spike — PR #32 (2026-07)
- ✅ 16. Join the attempt trail to its request — trace correlation — PR #33 (2026-07)
