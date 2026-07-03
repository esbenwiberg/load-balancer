# LLM Load Balancer / Task-Aware Router — Research

**Status:** research & investigation (no code yet)
**Owner:** ewi@projectum.com
**Started:** 2026-07-03

## The idea (one paragraph)

Users of coding agents (Claude Code, Codex, etc.) point their client at **one endpoint**
— this system — instead of manually swapping login/API keys/env vars when they want a
different model. The system inspects each incoming request, decides what it needs, and
routes it to the best-fit backend:

- **Workbenches** = our NVIDIA DGX Spark boxes, each serving one or more local models
  (e.g. `qwen3-coder-30b`, plus smaller models). Cheap, private, low-concurrency.
- **Foundry** = Azure AI Foundry (Anthropic + OpenAI models). The **always-available
  fallback** with effectively unlimited capacity.

Small/simple work → a small model on a Spark. Large/hard work → Opus or GPT on Foundry.
The user never knows (or cares) what happened underneath.

## What this repo is

A research workspace. We persist findings and design discussion as documents so nothing
is lost between sessions.

| Doc | What's in it |
|-----|--------------|
| [docs/01-landscape.md](docs/01-landscape.md) | What already exists — gateways, routers, Spark serving stacks. Build-vs-assemble. |
| [docs/02-architecture.md](docs/02-architecture.md) | Proposed architecture, components, and the trade-offs behind each choice. |
| [docs/03-open-questions-and-risks.md](docs/03-open-questions-and-risks.md) | Unresolved decisions, sharp edges, and things that can go wrong. |
| [docs/04-tool-calling.md](docs/04-tool-calling.md) | Why tool calls are make-or-break, and the 3 layers where they break. |
| [docs/05-prior-art-github.md](docs/05-prior-art-github.md) | How others do it — `claude-code-router` (35k★) and friends. |
| [docs/06-recommendation.md](docs/06-recommendation.md) | **The decision:** what to build, phased, with a Phase-0 config. |

## The one-line takeaway so far

**Don't build a load balancer from scratch.** Assemble one: `LiteLLM` (gateway +
protocol translation + fallback) + a routing layer + `llama-swap` on each Spark for model
lifecycle. The genuinely new thing we'd build is a thin **fleet-aware control plane** that
knows which Spark has which model hot and how loaded it is — and a routing policy that
respects **session stickiness** (see risks doc — you probably can't safely re-route
mid-conversation).

## Reframing the premise (read this before anything else)

Two things the naive framing gets wrong:

1. **Route sessions, not requests.** Coding agents hold a stateful conversation. Swapping
   the backend model between turn 3 and turn 4 breaks tool-call formats, prompt caching,
   and reasoning/thinking blocks. The safe unit of routing is a *session* (or a client's
   chosen alias), not every individual request.
2. **Gateway ≠ router.** Unifying protocols/fallback is a solved, boring problem (LiteLLM).
   "Tasting" a request to pick a model is the hard, interesting part — and it's optional.
   You can ship value with zero smart routing on day one.
