# Proposed architecture

This is a starting proposal, not a decision. Every choice lists the trade-off.

## Guiding principles

1. **Assemble, don't build.** The gateway/fallback/translation is a solved problem. Our
   value-add is the *fleet-aware routing policy*, not another proxy.
2. **Route sessions, not requests.** Pick a backend at session start (or per client
   alias); keep it sticky. Re-routing mid-conversation is the thing that breaks
   (see risks doc).
3. **Fail open to Foundry.** Any doubt, any Spark unhealthy/busy/cold → Foundry. Foundry is
   the floor of quality and the ceiling of capacity.
4. **Ship in phases.** Value on day one with zero smart routing. Add intelligence only
   where cheaper tiers demonstrably misroute.

---

## Components

```
                          ┌──────────────────────────────────────────────┐
  Claude Code ─(Anthropic)─▶                                              │
                          │   GATEWAY (LiteLLM)                           │
  Codex ───────(OpenAI    │   • protocol translation (Anthropic ↔ OpenAI  │
                Responses)─▶     ↔ Responses)                             │
                          │   • virtual keys / auth / spend / logging     │
  Others ─────────────────▶   • health checks, cooldowns, retries        │
                          │   • FALLBACK chain → Foundry                  │
                          └───────┬───────────────────────────┬──────────┘
                                  │ asks: "which backend?"      │
                          ┌───────▼─────────┐                   │
                          │ ROUTER          │                   │
                          │ (policy engine) │                   │
                          └───────┬─────────┘                   │
                                  │ reads fleet state           │
                          ┌───────▼─────────┐                   │
                          │ CONTROL PLANE   │  ← the new bit we build
                          │ • model→Spark   │                   │
                          │   registry      │                   │
                          │ • live load /   │                   │
                          │   health / warm │                   │
                          └───────┬─────────┘                   │
                    ┌─────────────┼─────────────┐               │
              ┌─────▼────┐  ┌─────▼────┐  ┌──────▼───┐    ┌──────▼──────┐
              │ Spark A  │  │ Spark B  │  │ Spark C  │    │ Azure       │
              │ qwen3-   │  │ small    │  │ (llama-  │    │ AI Foundry  │
              │ coder-30b│  │ models   │  │  swap)   │    │ Opus / GPT  │
              └──────────┘  └──────────┘  └──────────┘    └─────────────┘
              vLLM (hot)    vLLM/Ollama   multi-model      unlimited, fallback
```

### 1. Gateway = LiteLLM
- Terminates both client protocols, translates, authenticates, does fallback/retries,
  logs spend and latency. Well-documented for Claude Code and local vLLM.
- **Trade-off:** LiteLLM's own auto-routing is basic. We either (a) use it and accept
  crude routing, or (b) put our own router in front/inside and use LiteLLM purely as the
  dumb execution layer. Recommend (b) for control — but start with (a) to get moving.
- **Alternative:** **Arch (`archgw`)** collapses gateway+router into one Envoy-based box
  with purpose-built routing models. Evaluate head-to-head if routing quality becomes the
  bottleneck. Cost: heavier, Envoy ops, less mainstream than LiteLLM.

### 2. Router = a thin policy engine
Decision inputs, cheapest signal first:
- **Static:** which client, which requested model alias, which virtual key/team.
- **Cheap heuristics (<1ms):** prompt/context token count, code presence, tool definitions
  present, reasoning markers. (LiteLLM complexity router or our own.)
- **Semantic (~30–90ms):** embedding vs utterance sets, *if* heuristics misroute.
- **LLM router (Arch-Router-1.5B on a Spark):** only if semantic isn't enough.

Decision output: a target backend + a fallback chain, subject to **fleet state**.

### 3. Control plane = the genuinely new component
A small service the router consults:
- **Registry:** which model is loaded/pinned on which Spark, and each Spark's capability
  tier (S/M/L).
- **Live state:** per-Spark in-flight request count, queue depth, health, whether the
  target model is *warm* (loaded) or *cold* (would trigger a llama-swap).
- **Policy it enforces:**
  - Prefer a Spark where the model is **already warm** and in-flight < N.
  - Never trigger an interactive-path cold swap; if only a cold Spark could serve it →
    **go to Foundry** and (optionally) warm the Spark async for next time.
  - Spark unhealthy / saturated / timing out → Foundry.
- Implementation: could be as small as a Redis/SQLite table + a heartbeat from each Spark
  (llama-swap and vLLM expose health/metrics endpoints to scrape).

### 4. Backends
- **Sparks:** one **hot pinned model per box** for interactive use (Spark A =
  `qwen3-coder-30b`). Boxes that must host several models run **llama-swap**, but treat
  those as best-effort / batch, not low-latency interactive.
- **Foundry:** LiteLLM `azure/` + Anthropic-on-Foundry entries. The tail of every fallback
  chain. Confirm the wire format for Anthropic models on Foundry (open question).

---

## Request lifecycle (happy path + fallbacks)

1. Client hits gateway with its native protocol; gateway authenticates (virtual key).
2. Gateway asks router for a backend. Router applies static → heuristic → (opt) semantic
   signals, then filters by control-plane fleet state.
3. **If** a warm, healthy, unsaturated Spark has the right model → route there.
   **Else** → Foundry.
4. Gateway translates the request to the backend's protocol, streams the response back
   translated to the client's protocol.
5. On error/timeout/cooldown → LiteLLM fallback chain advances → ultimately Foundry.
6. Session stickiness: record the chosen backend for this session so later turns reuse it
   (don't re-taste every turn — see risks).

---

## Phased delivery

| Phase | Scope | Routing intelligence | Why |
|-------|-------|----------------------|-----|
| **0 — Passthrough** | LiteLLM in front, model chosen by the alias the client asks for; Foundry fallback. | None | Proves protocol translation + fallback + auth end-to-end for both Claude Code and Codex. Immediate value: one endpoint, no env swapping. |
| **1 — Fleet-aware** | Add control plane; route a requested model to the right *warm* Spark, else Foundry. | Static + health | Real load balancing across Sparks with safe fallback. Still no "tasting." |
| **2 — Heuristic tasting** | Rule-based complexity routing picks tier (small Spark vs Foundry-Opus) at session start. | <1ms rules | The "small→Spark, big→Foundry" promise, cheaply. |
| **3 — Semantic/LLM tasting** | Embedding or Arch-Router refines model choice. | 30–90ms / LLM | Only if Phase 2 misroutes too often. Measure first. |

Each phase is independently useful and shippable. Don't skip to Phase 3.

---

## Why not just build a custom proxy from scratch?
- Protocol translation across Anthropic Messages ↔ OpenAI Chat ↔ OpenAI Responses is
  fiddly, changes often (Codex just dropped `wire_api=chat`), and is exactly what LiteLLM
  maintains for us.
- Streaming, tool-call schema mapping, prompt-cache headers, retries/cooldowns — all
  already handled and battle-tested.
- Our scarce engineering should go into the **control plane + routing policy**, which is
  where our specific fleet knowledge lives and where no off-the-shelf tool knows our
  Sparks.
