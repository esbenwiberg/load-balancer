# Recommendation — what we build, in what order

Consolidates docs 01–05 into a decision. TL;DR: **assemble, don't invent.** Stand on
LiteLLM, steal `claude-code-router`'s structural routing taxonomy, and build the one thing
nobody sells us — a **fleet-aware control plane**.

## Decision log

| # | Decision | Why | Docs |
|---|----------|-----|------|
| 1 | **Gateway = LiteLLM** (central, multi-user), not a from-scratch proxy | Protocol translation + fallback + auth + logging already solved & maintained; Codex/Claude Code both documented | [01](01-landscape.md), [05](05-prior-art-github.md) |
| 2 | **Route sessions, not requests** | Mid-session backend swap breaks tool-call state, prompt cache, reasoning blocks | [03](03-open-questions-and-risks.md), [04](04-tool-calling.md) |
| 3 | **Routing = structural signals, not semantic tasting** | The 35k★ tool routes on requested-model / thinking / token-count / tool flags. No ML needed for the core promise | [05](05-prior-art-github.md) |
| 4 | **Defer embedding/LLM routing (Phase 3)** to "only if measured misrouting demands it" | Nobody popular does it; adds latency + ops for unproven gain | [01](01-landscape.md), [05](05-prior-art-github.md) |
| 5 | **`agent_capable` is a test-earned, continuously-measured flag** | Tool-call reliability is per-model and fragile; OpenRouter gates on a live tool-call error rate — copy that | [04](04-tool-calling.md) |
| 6 | **Sparks pin one hot model each; no interactive cold-swaps** | 30B ≈ single-digit tok/s single-user; llama-swap cold start = seconds. Cold target → go Foundry, warm async | [01](01-landscape.md) |
| 7 | **Foundry is the tail of every fallback chain** | Effectively unlimited, known-good quality floor | all |
| 8 | **The novel component = fleet-aware control plane** | Off-the-shelf routers are per-user & fleet-blind; this is our only real build | [02](02-architecture.md), [05](05-prior-art-github.md) |

## Build target (one line)

`LiteLLM` (central gateway: translate + fallback + keys + logs) **+** a CCR-style category
router (`default / background / think / longContext`) **+** our control plane (model→Spark
registry, warmth/load/health, `agent_capable`).

## Phased plan

- **Phase 0 — Passthrough (ship this first).** LiteLLM in front; client asks for a model
  alias; alias resolves to a Spark or Foundry; Foundry is the fallback. Proves translation +
  fallback + auth for **both** Claude Code and Codex. Immediate win: one endpoint, no more
  env-var juggling. *Config below.*
- **Phase 1 — Fleet-aware.** Add control plane; route an alias to the right **warm** Spark,
  else Foundry. Real load balancing + safe fallback. Still no tasting.
- **Phase 2 — Structural routing.** Route by category (`background`→small Spark,
  `longContext`/`think`→Foundry) at **session start**. The "small→Spark, big→Foundry"
  promise, cheaply.
- **Phase 3 — (maybe) semantic/LLM routing.** Only if Phase 2 misroutes measurably.

## Phase-0 concrete config (illustrative — verify the ⚠️ items)

`litellm-config.yaml`:

```yaml
model_list:
  # ---- Spark workbench: qwen3-coder (OpenAI-compatible via vLLM) ----
  - model_name: qwen3-coder                      # alias clients request
    litellm_params:
      model: openai/Qwen/Qwen3-Coder-30B-A3B-Instruct
      api_base: http://spark-a.internal:8000/v1
      api_key: os.environ/SPARK_A_KEY            # "dummy" if vLLM has no auth
    model_info:
      tier: local
      agent_capable: true                        # earned via conformance test (doc 04)

  # ---- Foundry: OpenAI family (Azure OpenAI) ----
  - model_name: gpt                              # generic big-OpenAI alias
    litellm_params:
      model: azure/<your-gpt-deployment>         # ⚠️ confirm deployment name
      api_base: os.environ/AZURE_API_BASE
      api_key: os.environ/AZURE_API_KEY
      api_version: "2024-10-21"                  # ⚠️ confirm

  # ---- Foundry: Anthropic family ----
  - model_name: claude-opus                      # ⚠️ confirm Anthropic-on-Foundry wire fmt
    litellm_params:
      model: anthropic/claude-opus-4-8           # or azure_ai/… if served via Azure
      api_key: os.environ/ANTHROPIC_API_KEY

router_settings:
  # Spark unhealthy/busy/timeout → fall through to Foundry
  fallbacks:
    - qwen3-coder: ["gpt", "claude-opus"]
  num_retries: 2
  timeout: 600            # long: local big-model streams are slow
  allowed_fails: 2
  cooldown_time: 30       # match OpenRouter's ~30s outage window instinct

litellm_settings:
  drop_params: true       # tolerate params a backend doesn't support
  # ⚠️ PIN a vetted version; 1.82.7/1.82.8 shipped malware (doc 01 / risk 8)

general_settings:
  master_key: os.environ/LITELLM_MASTER_KEY   # issue per-user virtual keys off this
```

**Claude Code → proxy** (per user, or baked into a wrapper):
```bash
export ANTHROPIC_BASE_URL="http://litellm.internal:4000"
export ANTHROPIC_AUTH_TOKEN="$LITELLM_VIRTUAL_KEY"
# ANTHROPIC_MODEL should map to a model_name alias above
```
LiteLLM exposes a native Anthropic `/v1/messages` surface, so Claude Code talks its own
protocol and LiteLLM translates to the Spark/Foundry backend.

**Codex → proxy** (`~/.codex/config.toml`):
```toml
[model_providers.balancer]
base_url  = "http://litellm.internal:4000/v1"
wire_api  = "responses"            # ⚠️ Codex dropped "chat"; MUST be responses
# OPENAI_API_KEY / OPENAI_BASE_URL via env
```
⚠️ **Blocker to verify first:** does LiteLLM's Responses endpoint fully bridge to a
Chat-Completions Spark backend (streaming + tool calls)? If not, Codex→Spark is out for
Phase 0; Codex→Foundry-OpenAI still works. (doc 03 risk 4, doc 04.)

## Control-plane sketch (Phase 1+)

A tiny service (SQLite/Redis + heartbeats scraped from each Spark's vLLM/llama-swap health
& metrics endpoints) exposing, per model:
`{ spark, loaded/warm, in_flight, queue_depth, healthy, agent_capable, tool_call_error_rate }`.
Router consults it and applies: *prefer warm + unsaturated + agent_capable Spark; else
Foundry; never cold-swap on the interactive path.*

## Immediate next actions
- [ ] Verify Codex Responses↔ChatCompletions bridge in LiteLLM (blocker).
- [ ] Confirm Anthropic-on-Foundry wire format + LiteLLM provider entry.
- [ ] Confirm real Spark inventory (boxes, pinned models, memory headroom).
- [ ] Build the multi-tool **streaming** conformance test → sets `agent_capable` (doc 04).
- [ ] Stand up Phase-0 LiteLLM with 1 Spark + Foundry fallback; drive Claude Code through it.
