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

### 5. Anthropic models on Azure AI Foundry — wire format
We assume Foundry serves Anthropic models. Need to confirm the exact API surface and that
LiteLLM has a first-class provider entry for it (vs Azure OpenAI, which it clearly does).
**Open:** verify and pin the LiteLLM provider config.

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
      headroom per box.
- [ ] Verify Codex Responses↔ChatCompletions bridging in LiteLLM (blocking for Codex→Spark).
- [ ] Verify Anthropic-on-Foundry API surface + LiteLLM provider support.
- [ ] Decide routing granularity: session-only (safe) vs allow-one-escalation (riskier).
- [ ] Decide what "belongs" on a Spark vs always-Foundry (latency-driven, not just size).
- [ ] Evaluate LiteLLM-only vs Arch(`archgw`) for the routing layer.
- [ ] Data-governance sign-off with DISCO on Foundry usage + transparent routing.
