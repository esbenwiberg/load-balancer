# Open questions & risks

The stuff that can sink this. Ranked roughly by how much it threatens the premise.

## 🔴 Premise-level risks

### 1. Mid-session re-routing breaks agents
Claude Code and Codex maintain a **stateful conversation** with one model. If turn 3 goes
to Qwen and turn 4 to Opus:
- **Tool-call formats differ** (Anthropic `tool_use`/`tool_result` blocks vs OpenAI
  `tool_calls`). A transcript written by one may not parse for the other.
- **Prompt caching breaks** — the new backend has none of the cached prefix, so cost/
  latency spike exactly when you were trying to save.
- **Reasoning/thinking blocks differ** and may be rejected or mishandled.
- **Style/quality discontinuity** mid-task confuses the agent.

**Implication:** route at **session granularity**, not per request. The original framing
("taste *every* request and route it") is probably wrong for coding agents.
**Open:** can we even reliably detect "session start" from a stateless proxy? (Heuristics:
new/empty transcript, first message, a client-provided session header?)
**→ DECIDED (2026-07-08): hybrid — see the decision block after risk 2.** On session
detection, goal 17's spike ([docs/09](09-observability.md)) turned the open question into
facts: Codex already emits a per-invocation `session_id` (+ `prompt_cache_key`); Claude
Code sends no conversation id by default but supports header injection
(`ANTHROPIC_CUSTOM_HEADERS`) and LiteLLM carries `x-litellm-tags` end-to-end; transcript
shape (empty vs prior-turns) is the no-cooperation fallback heuristic.

### 2. Can you actually "taste" a coding-agent request well?
A coding-agent turn is not a clean single-intent query. It's a huge blob: system prompt,
tool definitions, whole file contents, prior turns. Signal for "is this hard?" is buried.
And difficulty **escalates within a session** — turn 1 ("rename this var") is trivial,
turn 8 ("now refactor the auth layer") is not. If we routed the whole session on turn 1,
we mis-provisioned.
**Open:** is session-start tasting good enough, or do we need to allow *one* escalation
hop (e.g. bump to Foundry when a turn exceeds some complexity, accepting the cache/format
cost once)?

> **DECISION (2026-07-08) — routing granularity: HYBRID.** Made at the keyboard after
> the Fugu research session (GOALS.md 2026-07-08; Sakana's Fugu does per-request routing
> by *owning the conversation surface* — an orchestration model that rewrites prompts
> per worker and normalizes every response, paying ~10x token overhead, 8–160s latency,
> and total routing opacity. A pass-through gateway in front of *client-side* stateful
> agent loops has none of those affordances, so naive per-request routing stays wrong
> here — risks 1–2 stand). The decided shape:
>
> 1. **Sessions route sticky, at session granularity.** A request that belongs to a
>    stateful conversation (session id when the client provides one, transcript-shape
>    heuristic when it doesn't) pins to the backend the session started on. No
>    mid-session backend swaps — tool-call format continuity and prompt-cache reuse win.
> 2. **Stateless one-shots route per-request, freely.** Traffic with no conversational
>    state (empty/single-turn transcript, no tool history — e.g. Claude Code's
>    background/haiku-class calls) has no format-continuity problem; route it to the
>    cheapest capable backend every time. Goal 21's shadow classifier separates these
>    shapes already.
> 3. **One escalation hop per session, upward only.** A session may escalate
>    local → Foundry once (accepting the one-time cache/format cost); it never
>    de-escalates — big-tier models tolerate a foreign-format transcript far better
>    than small local ones, so downward moves are where risk-1 breakage actually bites.
>    The escalation *trigger* (complexity threshold vs verify-then-escalate vs manual)
>    was deliberately NOT decided here. **→ trigger DECIDED 2026-07-23: manual /
>    client-signaled v1 — see the escalation-trigger decision block below.**
>
> Hard constraints carried over from the Fugu research: routing stays deterministic +
> auditable (per-request records must prove which backend saw every prompt — the
> data-governance moat), and never buffer the stream behind a routing decision.
> Implementation is unblocked but engine-shaped: the hybrid's requirements are the
> input to the LiteLLM-vs-`archgw` evaluation (checklist below).
> **→ engine DECIDED 2026-07-09 — next decision block.**

> **DECISION (2026-07-09) — routing engine: LiteLLM custom policy layer** (over
> archgw/Plano). Made at the keyboard against the R1–R9 requirements table
> ([docs/12 §7](12-hybrid-router-spec.md)). Basis:
>
> 1. **LiteLLM (pinned 1.83.x) covers the table today**: R1/R2/R4/R5/R9 are
>    *verified* in our own harness (pre-call hook rewrites `data["model"]`,
>    headers reach hooks, fallbacks proven, capability flags queryable, all
>    three surfaces exercised daily by e2e). R3 (sticky pin store) is the one
>    structural gap and it is a small owned component already spec'd
>    (docs/12 §3: gateway-memory now → Postgres at replica time).
> 2. **archgw no longer exists under that name**: Katanemo renamed and
>    re-architected it into **Plano** (2026-01-10, early-stage). Session
>    affinity — the single best reason to switch — is **undocumented** (R3
>    unverified), and Responses-bridge parity (R9, the Codex commitment) is
>    unverified. Migrating would mean re-proving the entire e2e surface
>    (three protocols, fallback semantics, obs callbacks) on a new data plane
>    for no verified gain.
> 3. **Blast radius**: the policy layer is hook code we own — deterministic,
>    offline-testable, reversible; a data-plane swap is the whole gateway.
>
> **Re-look condition (a gate, not a date preference): no earlier than 2027-01
> AND Plano documents session affinity.** Separable and still open: Katanemo's
> open-weights *router model* as a learned taster inside our deterministic
> policy (docs/12 §4 note + open decision 5) — telemetry-gated, adoptable
> without Plano the proxy. This decision unblocks the policy-layer build goals
> (GOALS.md 24–26).

> **DECISION (2026-07-23) — escalation trigger: manual / client-signaled v1**,
> with the automatic triggers as telemetry-gated follow-ups. Made at the
> keyboard. The hybrid router (decision block above) reserves one upward-only
> hop per session; goal 25 built the *mechanics* (pin replaced upward, exactly
> once, no downward edge) behind a STUB `escalate` tag. This decides what fires
> that hop for real. The four candidates, scored against the non-negotiable
> constraints (deterministic + auditable; never buffer the stream behind a
> verdict; governance candidate-set filter — [docs/12 §1](12-hybrid-router-spec.md)):
>
> 1. **Manual / client-signaled → CHOSEN as v1.** A namespaced tag
>    `router:escalate` on the goal-22-verified carrier (`x-litellm-tags`, which
>    reaches both logging surfaces on the pin — no new header plumbing to
>    re-prove). Fits every constraint by construction (the client asked; it's
>    on the record), needs no new infra (goal 25's mechanics stand), and is the
>    only option decidable **today** — the automatic triggers below all need
>    real traffic distributions to set a threshold or validate a verifier, and
>    the harness has none (every byte is synthetic e2e traffic). Decisive
>    second-order effect: **manual escalations ARE the eval set** the automatic
>    triggers need — each one is a human-labeled "local wasn't enough here",
>    the same measured-not-argued discipline already committed for the learned
>    taster ([docs/12 §4](12-hybrid-router-spec.md), open decision 5). A coding
>    agent stuck in a loop also knows it's stuck better than a request-shape
>    heuristic guesses.
> 2. **Complexity threshold (goal 21 bucket crosses a line) → SPECCED FOLLOW-UP,
>    telemetry-gated.** Deterministic + auditable and zero-latency (classified
>    pre-call from the request alone), but it's a *predictor*: it escalates on a
>    guess about request shape *before* local gets a chance, and because the hop
>    is upward-only + permanent, one heavy-looking turn exiles the whole session
>    to Foundry forever — a large cost/governance consequence from a cheap
>    heuristic with an arbitrary threshold no data backs yet. **Adoption gate:**
>    v1's manual-escalation telemetry shows the bucket signal would have fired
>    where humans did (precision/recall against the labeled set), not before.
> 3. **Verify-then-escalate (Fugu/TRINITY Verifier) → REJECTED as primary.**
>    Breaks two hard constraints at once: a verifier *model* judging local's
>    answer is "the model said so" (fails auditability), and to verify local's
>    output you must have it — i.e. buffer the stream (fails the cardinal rule).
>    Fugu Ultra's 8–160s latency floor is the tombstone. The one survivable
>    variant — a **structural, rule-based** post-hoc check applied on the *next*
>    turn (tool call didn't parse, code didn't compile, test failed) — is kept
>    as a possible later signal, not v1.
> 4. **N-consecutive fallback-served turns (rotten pin — risk 7 + docs/12
>    §6) → SEPARATE later signal.** This is a *health* signal, not a *difficulty*
>    one: §6 already holds the pin through transient blips, so N-consecutive is
>    just the documented "the pin is genuinely rotten" exception. Small blast
>    radius, decided later on its own.
>
> **Folds in open decision 4 (streaming-latency override, risk 3):** it
> evaporates for v1 — a manual trigger never auto-routes `heavy` traffic, so
> there's no threshold-vs-TTFT tension to resolve until trigger #2 is on the
> table.
>
> **Reversible + autonomy-friendly to build.** Nothing deploys from `main`; the
> tag contract and telemetry gate are e2e-verifiable. This turns the former
> § Needs-a-human blocker into a vetted build goal (GOALS.md): promote the
> `escalate` stub to the first-class `router:escalate` contract + write the
> telemetry gate that governs adopting trigger #2.

### 3. Spark interactive latency for big models
A 30B-class model on a Spark is **single-digit tok/s for one user.** A coding agent
streaming a large diff at 6 tok/s is a bad experience. Aggregate throughput at high
concurrency looks nice on paper but interactive single-user latency is what devs feel.
**Open:** which tasks are genuinely acceptable on a Spark? Maybe only short completions /
small models, and anything that needs to *stream a lot* goes to Foundry regardless of
"complexity."

## 🟠 Sharp technical edges

### 4. Codex Responses API vs local Chat-Completions servers
Codex now requires `wire_api = "responses"`. vLLM/Ollama typically speak Chat Completions.
Routing Codex → a Spark needs a **Responses ↔ Chat-Completions bridge**.
**Open / must-verify:** does LiteLLM's Responses endpoint fully bridge to a Chat-Completions
backend (incl. streaming + tool calls)? If not, Codex→Spark may be off the table initially
(Codex→Foundry-OpenAI still fine).

> **✅ RESOLVED (Phase-0 research) — YES, on paper; validate by smoke test.**
> LiteLLM has an **opt-in** `/v1/responses → /chat/completions` bridge: set
> `use_chat_completions_api: true` on the `model_list` entry (or encode `openai/chat_completions/<model>`).
> Source (`litellm/responses/litellm_completion_transformation/`) shows it translates
> **streaming** (SSE deltas → Responses events) **and tool calls both directions**
> (`transform_responses_api_tools_to_chat_completion_tools`, `_queue_tool_call_delta_events`).
> Requested specifically for the Codex use case (issue #23716; PRs #24783, #25346).
> **Caveats:** (a) the flag ships only in the **1.83.x-stable** line (≈1.83.14+), which
> **post-dates** the 1.82.7/1.82.8 malware — you can't pin pre-incident 1.82.6 *and* get
> this bridge; run a vetted 1.83.x-stable and verify the digest. (b) Bridge is young with a
> bug history (parallel-tool index collision #21331, mixed text+tool drop #17246 — both
> fixed; `developer`-role #24664; a `file_search` flag-drop P1 on #24783). **All evidence is
> source/doc reading, not an observed Codex↔vLLM round-trip** → smoke-test streaming +
> parallel tools + mixed text/tool output before committing Codex→Spark. Codex→Foundry-OpenAI
> is unaffected. The conformance harness (`conformance/`) can now run that smoke test directly:
> `conformance.py --api responses --base-url <litellm>/v1` drives LiteLLM's `/v1/responses`
> endpoint through the bridge, with parallel-tool and `tool_choice:required` probes for the
> exact bug classes above (#21331, the Qwen3 400). **Decision: Codex IS in scope for Phase 0**
> (2026-07-03), so this smoke test is a release gate, not optional.

### 5. Anthropic models on Azure AI Foundry — wire format
We assume Foundry serves Anthropic models. Need to confirm the exact API surface and that
LiteLLM has a first-class provider entry for it (vs Azure OpenAI, which it clearly does).
**Open:** verify and pin the LiteLLM provider config.

> **✅ RESOLVED (Phase-0 research).** Claude on Azure AI Foundry is **GA** (~June 2026),
> served as the **native Anthropic Messages API** at
> `https://<resource>.services.ai.azure.com/anthropic`. The correct LiteLLM provider is
> **`azure_ai/`** — *not* `azure/` (that's Azure OpenAI / GPT) and *not* `anthropic/`
> (that's Anthropic-direct). LiteLLM has a purpose-built page
> (`/providers/azure/azure_anthropic`, PR #17104). Config:
> `model: azure_ai/<deployment-name>` + `api_base: .../anthropic` + `api_key: os.environ/AZURE_API_KEY`.
> **No `api_version`** needed (versioning is via the `anthropic-version` header LiteLLM
> injects). `max_tokens` is required by the Azure Anthropic API (LiteLLM defaults 4096).
> Only auth differs from `anthropic/`-direct; **tool calls, streaming, thinking are
> identical**. Route on the **deployment name**, not the catalog ID (fast-moving). Some
> newest models are **Entra-ID-only** (use `AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET`).
> Now wired into `deploy/litellm-config.yaml`. Prompt-caching on the Azure route is
> inferred (LiteLLM claims feature parity) but **unconfirmed** — verify before relying on it.

### 6. Cold-start / model-swap on the hot path
llama-swap cold start = seconds to tens of seconds. If routing sends an interactive request
to a Spark whose target model is cold, the user eats the swap latency.
**Decision (proposed):** never cold-swap on the interactive path. Route to Foundry, warm
the Spark asynchronously for next time. Control plane must know warm vs cold.

### 7. Streaming, timeouts, retries across a translating proxy
Every hop (client → gateway → Spark) must stream correctly and handle mid-stream failures.
A retry that re-sends a partially-streamed request is a correctness bug.
**Open:** define timeout/retry semantics per hop; test mid-stream Spark death → clean
fallback.

> **✅ OBSERVED (mid-stream death, e2e — 2026-07).** mockd's `hangup` mode
> (partial SSE chunk, then the connection is slammed shut with no terminator)
> exercises this on both surfaces through LiteLLM `v1.83.14-stable`. Pinned by
> `test_chat_stream_backend_hangup_midstream` and
> `test_responses_stream_backend_hangup_midstream` in [e2e/test_e2e.py](../e2e/test_e2e.py).
> Retry/fallback config in play: `num_retries: 1`, fallback chain
> `qwen3-coder → claude-sonnet → claude-opus → gpt` ([e2e/litellm-config.e2e.yaml](../e2e/litellm-config.e2e.yaml)).
>
> **Semantics per hop — the fallback boundary is the first byte to the client:**
> - **Backend → gateway (retry/fallback CAN act):** happens only *before* the
>   client response line is committed. Connection-establishment errors and
>   non-2xx statuses (e.g. a 503) are retried/failed-over cleanly — that's what
>   `test_fallback_to_foundry_on_5xx` proves. A 5xx/connection error while the
>   upstream stream has NOT yet started still falls back.
> - **Gateway → client (retry/fallback CANNOT act):** once the gateway forwards
>   the first SSE byte, HTTP `200` + headers are on the wire, so it can no longer
>   change status or re-route. When the backend dies *mid-stream* the client
>   receives the partial pre-hangup content (`chat`: a `delta.content` chunk;
>   `responses`: a `response.output_text.delta`) and then a **truncated stream
>   that never emits `[DONE]` / `response.completed`** — it goes silent until the
>   client's own read timeout fires. **No clean fallback is possible here** — this
>   is inherent to HTTP streaming, not a LiteLLM bug.
> - **No duplicate upstream request / no spliced reply:** measured backend hit
>   count for one mid-stream-death request is **1** — LiteLLM does *not* re-send
>   the partially-streamed request to another backend, and no Foundry-tier
>   `served_model` stamp leaks into the truncated stream. So the specific
>   correctness bug this risk warns about ("a retry that re-sends a
>   partially-streamed request") does **not** occur on the streaming path; the
>   failure mode is instead a clean truncation the **client must detect** via the
>   missing terminator.
>
> **Implication for the design:** mid-stream backend death is a client-visible
> truncation, not a gateway-recoverable event. Clients (Claude Code / Codex)
> already treat a stream that ends without its terminator as a failed turn and
> retry the *whole* turn — which is the correct recovery (a fresh request CAN
> fall back at the backend→gateway boundary). Two follow-ups worth tracking:
> (a) the gateway holds the client connection open (silent) rather than closing
> it on upstream death, so clients rely on their own read timeout — consider a
> gateway-side idle-stream timeout so truncations surface fast; (b) to *avoid*
> mid-stream death costing a full turn, routing could prefer the always-up
> Foundry tier for requests expected to stream a lot (ties into risk 3).

> **✅ OBSERVED (retry-vs-fallback ORDER, e2e — 2026-07).** With
> `num_retries: 1` and the fallback chain
> `qwen3-coder → claude-sonnet → claude-opus → gpt`, the pinned order is:
> **LiteLLM spends its `num_retries` retrying the SAME backend BEFORE the
> fallback chain is consulted.** Proven by observation with mockd's
> count-limited fault (`test_transient_5xx_retries_same_backend_before_fallback`
> in [e2e/test_e2e.py](../e2e/test_e2e.py)):
> - A transient 5xx **shorter than the retry budget** (one 503, then the fault
>   clears) is **absorbed on the original backend** — the single retry lands on
>   qwen3-coder again and succeeds; `served_model` stays `qwen3-coder` and the
>   fallback chain is never touched.
> - A fault that **outlasts the retry budget** (first attempt *and* its retry
>   both 503) exhausts retries first, and **only then** advances the chain, so
>   `claude-sonnet` answers.
>
> **Why this matters (the config tripwire this test guards):** the retry is a
> re-send to the *same* deployment, so a single backend fault can become up to
> `num_retries + 1` upstream requests to that backend before any failover. That
> is safe for *idempotent, non-streamed* attempts (the retry only fires before
> the first byte reaches the client — see the mid-stream block above), but it
> means raising `num_retries` multiplies backend load under fault, and any config
> change that reordered this to *fallback-before-retry* would silently change
> which tier serves a flapping workbench.
>
> **Cooldown, and why the e2e harness disables it:** in production a deployment
> that trips `allowed_fails` is put in *cooldown* — the router pre-emptively skips
> it for `cooldown_time` seconds, so a flapping workbench stops eating the
> retry-then-fallback tax on every request. This is a latency optimization layered
> ON TOP of fallback, not a distinct client-visible contract. It is **deliberately
> disabled in the e2e config (`disable_cooldowns: true`)**: cooldown is in-memory,
> time-based gateway state that mockd's `/__reset` cannot clear, so when the fault
> tests flap qwen3-coder that state **bled into the next serially-run test** and
> silently rerouted a request the next test expected qwen3-coder to serve — an
> order/timing-dependent flake. Since every assertion in the suite is about
> fallback (which is cooldown-independent), disabling cooldown removes the flake
> without losing coverage. Prod (`deploy/litellm-config.yaml`) keeps cooldown on.
>
> **429 (rate-limit) behaves as a fallback-triggering fault**, identical to 5xx —
> a persistent 429 on the workbench advances to the Foundry tier rather than
> surfacing to the client (`test_fallback_on_429`). And a **malformed tool call**
> (truncated JSON arguments) is passed through the Responses bridge **verbatim**,
> not repaired or rejected — the bridge is a transport, JSON validation is the
> client's job (`test_malformed_tool_call_through_responses_bridge`).

## 🟡 Operational / security

### 8. LiteLLM supply-chain
PyPI `1.82.7`/`1.82.8` shipped credential-stealing malware. **Pin a vetted version, verify
hashes, watch advisories.** This box will hold API keys for Foundry — it's a juicy target.

### 9. Secrets & auth
Gateway holds Foundry credentials + issues virtual keys to users. Keys in env/secret store,
never in config committed to git. Per-user virtual keys for attribution and revocation.

### 10. Data governance (org constraint)
Org policy: **no personal/customer data** to these models, and only for Context& /
Delegate / Projectum / Consit work. A transparent router means users may not realize *which*
model/where their prompt lands. Sparks are local/private (good); Foundry is Azure (verify
data-residency/retention terms). **If in doubt, reach out to DISCO.** Consider logging/
guardrails so we can prove what went where.

### 11. Observability & cost attribution
We need per-request: chosen backend, why (routing decision), latency, tokens, fallback-hit.
Without this we can't tune routing or prove the cost savings that justify the whole thing.
LiteLLM logging + control-plane decision logs.

### 12. Single point of failure
Everyone now depends on this one endpoint. If the gateway is down, *all* coding agents are
down. Needs HA (at least 2 gateway instances), and the fallback-to-Foundry must not itself
depend on a flaky component.

## Questions for the team / next research

- [ ] Confirm the exact Spark inventory: how many boxes, which models pinned where, memory
      headroom per box. **(still needs a human — see deploy/RUNBOOK.md step 0)**
- [x] Verify Codex Responses↔ChatCompletions bridging in LiteLLM (blocking for Codex→Spark).
      **→ risk 4: YES on paper via `use_chat_completions_api` (1.83.x-stable); smoke-test pending.**
- [x] Verify Anthropic-on-Foundry API surface + LiteLLM provider support.
      **→ risk 5: `azure_ai/<deployment>` + `/anthropic` base URL; wired into the scaffold.**
- [x] Decide routing granularity: session-only (safe) vs allow-one-escalation (riskier).
      **→ DECIDED 2026-07-08: HYBRID — sticky sessions + free per-request stateless +
      one upward-only escalation hop; see the decision block after risk 2. Escalation
      trigger DECIDED 2026-07-23: manual / client-signaled v1 (automatic triggers
      telemetry-gated) — see the escalation-trigger decision block after the engine block.**
- [ ] Decide what "belongs" on a Spark vs always-Foundry (latency-driven, not just size).
- [x] Evaluate LiteLLM-only vs Arch(`archgw`) for the routing layer.
      **→ DECIDED 2026-07-09: LiteLLM custom policy layer — see the engine decision
      block after risk 2. archgw was renamed/re-architected into Plano (2026-01-10);
      re-look gate: ≥ 2027-01 AND documented session affinity. The learned-taster
      sub-option (docs/12 §8 decision 5) stays open, telemetry-gated.**
- [ ] Data-governance sign-off with DISCO on Foundry usage + transparent routing.
