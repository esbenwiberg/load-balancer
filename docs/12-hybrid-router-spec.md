# 12 — Hybrid-router design spec (goal 23)

**Status: SPEC, not implementation.** This document turns the decided routing
granularity ([docs/03](03-open-questions-and-risks.md) decision block,
2026-07-08 — **HYBRID**: sticky sessions, freely-routed one-shots, one
upward-only escalation hop) into buildable mechanics, and produces the
requirements table the **LiteLLM-vs-`archgw` engine fork** runs against.
Nothing here changes gateway behavior. Decisions still open are flagged
**⛔ Needs-a-human** inline and collected at the end.

The telemetry this spec consumes already ships: complexity buckets (goal 21),
session classification + stickiness keys (goal 22), overhead attribution
(goal 20), the attempt↔request join (goal 16), and the fleet registry
(goal 5 / [docs/10](10-control-plane.md)). The router described here is the
first *consumer* of the control-plane registry.

---

## 1. Request classification (the policy's input)

Every inbound request is classified by the same deterministic, auditable
functions that today run in shadow ([docs/09](09-observability.md)):

| Signal | Producer | Values | Router use |
|---|---|---|---|
| `request_class` | `obs_callback._session` shape rule | `session-turn` \| `one-shot` | picks the policy arm (§3 vs §4) |
| `stickiness_key` | tag (`x-litellm-tags: session:<id>`) > transcript-hash > null | opaque string \| null | the pin for sticky routing |
| `complexity.bucket` | `obs_callback._complexity` tree | `trivial`/`toolful`/`heavy`/`agentic` | backend *candidate set* filter |

**Moving from shadow to routing input is a semantic promotion, not a rewrite**:
the functions are pure and already tested; the router calls them at ingress
(pre-call) instead of reading them post-hoc. The shadow records stay — they are
the audit trail that the router did what the classifier said.

Hard constraints (carried from the Fugu research, non-negotiable):
- **Deterministic + auditable**: same request ⇒ same decision; every decision
  writes a routing record naming its inputs. The constraint binds the
  **policy**, not the toolbox: an opaque end-to-end learned router (the Fugu
  shape) is out, but a learned *matcher* proposing a route from a
  human-written candidate set — with rules and governance filters disposing —
  is compatible; see the §4 learned-taster note and open decision 5. What must
  always hold: the citable reason ("why did THIS prompt go to Foundry?") is a
  rule or a logged preference-match, never "the model said so".
- **Never buffer the stream** behind a routing decision: classify from the
  request alone before the first backend byte. Today that means no model
  calls; decision 5 would relax it to at most ONE bounded local matcher
  inference pre-routing (a TTFT tax to be measured, never mid-stream work).
- **Data governance**: the classifier must be able to enforce "this key/tag
  never routes to Foundry" (DISCO constraint) — a *candidate-set* filter, not
  an afterthought.

## 2. The decision table

| request_class | stickiness state | route |
|---|---|---|
| `one-shot` | (no key) | §4 free routing: cheapest capable backend |
| `one-shot` | tagged key, no pin yet | §4, then RECORD the pin (turn 1 of a declared session) |
| `session-turn` / any | key has a pin | §3 sticky: the pinned backend, always |
| `session-turn` | key unpinned (gateway restarted / heuristic key) | §3: route as if new, record the pin |
| any | pin exists AND escalation fired | §5: the escalated backend (new pin, permanent) |

## 3. Sticky sessions

- **Pin at first sight of a stickiness key**: the backend chosen for that
  request becomes the session's backend. All subsequent requests with the same
  key route there, bypassing load-based choice (but not health — §6).
- **Key derivation** is goal 22's, verbatim: explicit tag wins (trusted from
  turn 1), transcript-hash for untagged session-turns (documented collision
  caveat), null otherwise.
- **Where the pin lives** — ⛔ **Needs-a-human** at implementation time, spec'd
  as requirement R6 below. Candidates:
  - *(a) gateway-local memory* — trivial, lost on restart (acceptable: an
    unpinned session-turn just re-pins; the cache-loss cost is the same as a
    restart today), broken with >1 gateway replica;
  - *(b) the control-plane registry* — already SQLite-backed and shared, but
    docs/10's scope boundary says the registry deliberately holds *fleet*
    state, not per-session state; extending it is a real scope change;
  - *(c) the existing Postgres* (LiteLLM's store) — durable + shared, heavier.
  The spec's default recommendation: **(a) for the single-gateway build phase,
  (c) when replicas arrive** — (b) is rejected to keep the control-plane's
  "registry, not router state" boundary intact.
- **TTL**: pins expire after inactivity (default suggestion: 24h, config knob).
  An expired pin is not an error — the next turn re-pins.

## 4. Free routing (one-shots)

Cheapest **capable** backend, where "capable" is a candidate-set filter, in
order:
1. **governance filter** — key/tag-scoped backend allowlist (the "never leaves
   the building" rule);
2. **`agent_capable` gate** — `complexity.bucket ∈ {toolful, agentic}` requires
   a backend whose conformance verdict is `agent_capable=true` (the
   [conformance/](../conformance/) gate — declared-by-config for mocks, earned
   for real models);
3. **health** — control-plane derived `healthy` (heartbeat-fresh AND
   reported-healthy, docs/10 D3);
4. **cost/latency order** — within the surviving candidates, prefer the
   cheaper tier (local before Foundry), tie-break on lowest `in_flight`
   (control-plane) — the "cheapest capable" rule. `heavy` buckets may override
   toward Foundry per docs/03 risk 3 (streaming latency beats complexity) —
   the exact rule is part of the escalation-trigger decision.

**Note — the learned-taster slot (future option, ⛔ decision 5).** Step 4 is
where a *learned matcher* could later replace the goal-21 decision tree as the
taster: a small open-weights router model (e.g. Katanemo's preference-aligned
router line — pinned weights like the LiteLLM image pin, run locally so no
prompt leaves the building) proposes a route **from the candidate set that
survived steps 1–3**; the deterministic filters still dispose, and the routing
record logs the matched preference. "Model proposes, config disposes" — the
Fugu lesson applied selectively rather than as a blanket ML ban. Costs to
weigh when the time comes: a ~1.5–4B inference on the request path before the
first byte (TTFT tax + a hosted, warm meta-model on hardware meant for real
models), and reproducibility-without-explainability (pinned weights give
same-input-same-output, not enumerable failure modes). **Adoption gate:**
goals 21+22's shadow records ARE the eval set — adopt a learned taster only
when accumulated telemetry shows the deterministic tree's misclassification
cost exceeds the taster's TTFT+infra tax. Measured, not argued.

## 5. Escalation — one hop, upward only

- **Trigger**: ⛔ **Needs-a-human** (GOALS.md § Needs-a-human bullet). The spec
  reserves the *mechanics* regardless of trigger choice:
- **Mechanics**: escalation REPLACES the session's pin with a higher-tier
  backend (local → Foundry). One transition per session, ever; the state
  machine is `pinned(local) → escalated(foundry)` with no reverse edge and no
  second hop. The transcript is re-sent as-is to the new backend (both target
  tiers tolerate foreign transcripts; the reverse direction is why downward
  moves are forbidden).
- **Cost accounting**: the escalating turn re-ingests the whole transcript
  uncached. Goal 20's overhead instrument must show this: the escalated turn's
  `tokens_consumed` includes the re-ingestion; the per-session view (future)
  should mark the escalation turn. **Requirement: escalation is visible in
  routing records** (`escalated: true` on the delivered record + the old/new
  pin), never silent.

## 6. Failure semantics — stickiness vs availability-fallback

Today's availability-fallback (LiteLLM `fallbacks`, goals 2/6) stays the
innermost safety net. Interaction rules:

- **A pinned backend that is DOWN beats stickiness**: serving the user wins;
  the request follows the fallback chain. This is *not* an escalation — the
  pin does NOT move (next turn retries the pinned backend once it's healthy;
  cooldown per LiteLLM's router).
  - Rationale: a transient local blip should not permanently exile a session
    to Foundry — that would make every hiccup an unintended escalation and
    burn the session's one hop implicitly.
  - ⛔ flagged sub-question for the trigger decision: N consecutive
    fallback-served turns COULD auto-fire the escalation (it's evidence the
    pin is rotten). Deliberately not decided here.
- **Mid-stream death**: unchanged (goal 2 pinned semantics — no mid-stream
  fallback, truncated stream surfaces to the client).
- **Records**: a fallback-served sticky turn shows `fallback: true` with the
  pin intact — the dashboard's existing fallback badge + attempt trail
  already express this.

## 7. Requirements table — the engine-fork input

R-numbers are what the LiteLLM-vs-archgw evaluation scores. "LiteLLM 1.83.x"
notes what is verified vs suspected on the pinned build; archgw column is
research homework for the evaluation (⛔ engine choice is Needs-a-human).

**⚠️ Evaluation input (researched 2026-07-09): archgw no longer exists under
that name.** Katanemo renamed and re-architected it into **Plano**
(2026-01-10), scope-expanded from LLM routing to "delivery infrastructure for
agentic apps". Facts gathered so far: Envoy-based out-of-process data plane
(Envoy-contributor pedigree, streaming-first); model/alias routing plus
preference routing via a learned open-weights router (`plano_orchestrator_v1`,
~4B — usable compatibly only in the §4 learned-taster shape); session affinity
**not documented** (R3 unverified); Responses-bridge parity (R9) unverified;
early-stage post-rename. Two consequences for the evaluation: (1) the earliest
sensible re-look is roughly 12 months post-rename *and* documented session
affinity; (2) adopting Katanemo's *router model* as a §4 taster is separable
from adopting Plano the *proxy* — score them independently.

| # | Requirement | LiteLLM 1.83.x (pinned) | archgw (to evaluate) |
|---|---|---|---|
| R1 | Per-request candidate-set routing hook (pre-call, no stream buffering) | `async_pre_call_hook` can rewrite `data["model"]` — verified the hook exists + mutates data (goal 16 uses it); routing via it is a custom policy layer we own | native "routing policy" story? |
| R2 | Read request headers at the routing hook | verified: header map reaches hooks (goal 22 probe) | ? |
| R3 | Sticky pin store (per-key, TTL, survives restart when replicated) | not native — custom (memory/Postgres); LiteLLM has no session-affinity primitive | claimed session affinity? verify |
| R4 | Availability-fallback chain preserved under custom routing | native `fallbacks` — verified goals 2/6; must confirm it composes with R1 model rewriting | ? |
| R5 | agent_capable / capability filter | `model_info` flags queryable — verified (conformance gate) | ? |
| R6 | Governance allowlist per key/tag | key-scoped `models` allowlist exists natively (11b machinery); per-tag routing is premium-gated — verify on pin | ? |
| R7 | Control-plane consumption (health/in_flight) in routing | not native — custom lookup in our hook (registry is HTTP, docs/10) | ? |
| R8 | Deterministic + fully-logged decisions | ours by construction (obs_callback) either way | must not be a black box |
| R9 | Streaming untouched, OpenAI+Anthropic+Responses surfaces preserved | the whole point of the current gateway — verified daily by e2e | Responses bridge parity? |

**Reading the table honestly**: LiteLLM needs a custom policy layer (we own the
hook code — deterministic, testable, but ours to maintain) and R3 is the only
structural gap. The archgw column is empty on purpose — filling it IS the
evaluation, and goal 23 forbids pre-deciding it.

## 8. Open decisions (all ⛔ Needs-a-human, collected)

1. **Escalation trigger** — complexity threshold / verify-then-escalate /
   manual / N-fallback-turns. Decide against goal 21+22 telemetry.
2. **Engine** — LiteLLM custom policy layer vs archgw/Plano. Input: §7 table
   including the rename note.
3. **Pin store at replica time** — §3's (a)→(c) promotion point.
4. **Streaming-latency override** (docs/03 risk 3) — whether `heavy`/long-
   stream traffic skips local regardless of complexity; fold into decision 1.
5. **Learned taster inside the deterministic policy** (§4 note) — whether to
   replace the goal-21 tree with a pinned open-weights matcher proposing from
   the filtered candidate set. Adoption gate: shadow-telemetry evidence that
   the tree's misclassification cost exceeds the taster's TTFT+infra tax.
   Separable from decision 2 — the router *model* is adoptable without Plano
   the *proxy*.

## 9. What this spec deliberately does NOT do

- No code, no config, no routing behavior change (goal 23's hard constraint).
- No new telemetry — it consumes goals 16/20/21/22 as-is.
- No Spark sizing (parked with the Spark-infra arc).
- No engine choice, no trigger choice — flagged above.
