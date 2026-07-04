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

### 2. Can you actually "taste" a coding-agent request well?
A coding-agent turn is not a clean single-intent query. It's a huge blob: system prompt,
tool definitions, whole file contents, prior turns. Signal for "is this hard?" is buried.
And difficulty **escalates within a session** — turn 1 ("rename this var") is trivial,
turn 8 ("now refactor the auth layer") is not. If we routed the whole session on turn 1,
we mis-provisioned.
**Open:** is session-start tasting good enough, or do we need to allow *one* escalation
hop (e.g. bump to Foundry when a turn exceeds some complexity, accepting the cache/format
cost once)?

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
- [ ] Decide routing granularity: session-only (safe) vs allow-one-escalation (riskier).
- [ ] Decide what "belongs" on a Spark vs always-Foundry (latency-driven, not just size).
- [ ] Evaluate LiteLLM-only vs Arch(`archgw`) for the routing layer.
- [ ] Data-governance sign-off with DISCO on Foundry usage + transparent routing.
