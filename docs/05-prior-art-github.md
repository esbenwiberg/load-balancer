# Prior art on GitHub — how others actually do this

Searched GitHub for real implementations, weighted by stars/relevance. The punchline
reshapes the plan.

## ⭐ The reference implementation: `musistudio/claude-code-router` (~35.5k ★)

This is the battle-tested front half of our idea. Read it before building anything.

- **Topology:** a **per-user local proxy** (listens on `localhost:8080`), driven by
  Claude Code's `ANTHROPIC_BASE_URL`. **Not** a shared, fleet-aware load balancer — this is
  the main gap vs what we want (central endpoint, many users, Spark-load awareness).
- **Codex + others:** supports Claude Code, Codex, ZCode. Providers: OpenAI-compatible,
  Anthropic Messages, Gemini, OpenRouter, DeepSeek, SiliconFlow, Moonshot/Kimi, Mistral,
  Z.AI, etc.

### How it routes — structural categories, NOT semantic tasting
The `Router` config maps a small set of **request categories** to a `"provider,model"`:

| Category | Fires when… | Typical target |
|----------|-------------|----------------|
| `default` | normal turns | your main model |
| `background` | Claude Code's own cheap subtasks (summaries, titles) — it *tags* these | small/local model |
| `think` | Plan Mode / reasoning-heavy | a reasoning model |
| `longContext` | token count > `longContextThreshold` (default ~60K) | big-context model |
| `webSearch` / `image` | those capabilities | capable model |

**This is the key lesson.** It does not "understand" the prompt. It routes on **signals
already present in the request**: which model the client asked for (haiku/sonnet/opus),
whether `thinking` is on, the token count, whether tools/web/image are involved, and
Claude Code's *own* background-task tagging. That's the pragmatic version of "tasting" —
and it's what a 35k-star tool settled on.

### Pluggable custom router (reusable design)
`CUSTOM_ROUTER_PATH` → a JS function roughly `async (req, config) => "provider,model"`.
It receives the **full Anthropic request** (messages, system, tools, model, thinking), so
you can compute anything (token count, tool presence, last-message heuristics) and return a
target — or `null` to fall back to the category rules. This is exactly the "taster hook"
shape we'd want; we can port the interface.

### Transformers = their protocol-translation + tool-coaxing layer (our Layers 1 & 2)
Per-provider request/response adapters. Built-ins include `anthropic`, `openai`, `gemini`,
`deepseek`, `openrouter`, plus behavior shims: `maxtoken`, `reasoning`, and crucially
**`tooluse`** — a transformer that *coaxes tool calling* on models that don't do it cleanly
(via `tool_choice` nudging / prompt shaping). That's a direct acknowledgement of our
**Layer-2 tool-calling minefield** ([04-tool-calling.md](04-tool-calling.md)) — even the
leading tool needs per-model hacks to make tool calls work.

## Other relevant repos

| Repo | ★ | What it teaches |
|------|----|-----------------|
| `0xrdan/claude-router` | ~42 | Routes Claude Code to Haiku/Sonnet/Opus **by complexity** — but stays inside the Claude family (no format-switching pain). Complexity routing in practice. |
| `jhammant/AIonDemandCluster` | ~38 | Spins up an open LLM on **rented GPUs (vast.ai/RunPod)**, serves via vLLM/llama.cpp, drives it from Claude Code via CCR. The on-demand-GPU-serving pattern — analogous to our Sparks + `llama-swap`. |
| `IntelliRoute-AI` | ~1 | Routes **local Ollama ↔ OpenAI GPT by prompt complexity** (FastAPI + Kafka + Redis), claims ~85% cost cut. Tiny, but it's *exactly* our local-vs-cloud-by-complexity shape as a reference arch. |
| `glidea/claude-worker-proxy` | ~272 | Cloudflare Worker proxy for Claude Code — thin translation layer, deploy pattern. |

## The generic "LLM router gateway" space is a graveyard of clones
The `llm router gateway` search returned **dozens of 0–3 star repos** all promising
"save 40–78% on costs," most abandoned toys. Signal: this is **easy to prototype, hard to
productionize**. The serious, maintained players are few:
- **LiteLLM** (gateway/fallback/translation) — our base.
- **Portkey gateway**, **archgw (Katanemo)** — heavier alternatives.
- **claude-code-router** — the coding-agent-specific niche leader.

Don't add to the clone pile. Stand on LiteLLM + steal CCR's routing taxonomy.

---

## What this changes about our plan

1. **Drop (or defer hard) semantic/LLM "tasting."** The dominant tool routes on **structural
   signals already in the request** — requested model, `thinking`, token count, tool/web/
   image flags, and Claude Code's own `background` tagging. That *is* Phase-2 heuristic
   routing, and it's apparently enough. Our Phase-3 embedding/Arch-Router tier looks
   speculative — nobody popular bothers. Demote it to "only if measured misrouting demands
   it."
2. **Steal the category taxonomy directly:** `default / background / think / longContext /
   webSearch / image` is a proven, cheap, explainable routing schema. Start here.
3. **The novel bit is confirmed to be the topology, not the routing.** CCR is per-user and
   fleet-blind. Our differentiator = **central shared endpoint + Spark-load/warmth-aware
   control plane** ([02-architecture.md](02-architecture.md)). That's what no off-the-shelf
   tool gives us.
4. **`tooluse`/`reasoning` transformers = independent confirmation** that per-model tool/
   reasoning shims are mandatory, not optional (Layer 2). Budget for them.
5. **Build target:** LiteLLM (central gateway, multi-user, fallback) + a CCR-style category
   router + our control plane. Consider running CCR's engine centrally, but LiteLLM is the
   better fit for a shared/team deployment.

## Sources
- [musistudio/claude-code-router](https://github.com/musistudio/claude-code-router) · [routing docs](https://musistudio.github.io/claude-code-router/docs/server/config/routing/)
- [0xrdan/claude-router](https://github.com/0xrdan/claude-router)
- [jhammant/AIonDemandCluster](https://github.com/jhammant/AIonDemandCluster)
- [glidea/claude-worker-proxy](https://github.com/glidea/claude-worker-proxy)
- GitHub repo search: `claude code router`, `llm router gateway`
