# Handoff prompt — for a clean session

Copy everything in the block below into a fresh session / remote agent.

---

```
You are continuing work on an LLM load-balancer / task-aware router. All prior research is
already in the repo — clone it and read before doing anything.

REPO: https://github.com/esbenwiberg/load-balancer
Read in order: README.md, then docs/01 → docs/06. docs/06-recommendation.md is THE decision.

MISSION
A single endpoint that Claude Code / Codex (and similar) point at (via ANTHROPIC_BASE_URL /
Codex config.toml) instead of swapping API keys/env vars by hand. It routes each *session*
to the best backend: local models on NVIDIA DGX Spark workbenches for small/cheap work;
Azure AI Foundry (Anthropic + OpenAI) as the always-available fallback. Transparent to the
user.

DECISIONS ALREADY MADE — do not relitigate (see docs/06):
- Assemble on LiteLLM (central gateway: protocol translation + fallback + virtual keys +
  logging). Do NOT build a proxy from scratch.
- Route SESSIONS, not requests — mid-session backend swaps break tool-call state, prompt
  cache, and reasoning blocks.
- Routing = cheap STRUCTURAL signals (requested model, thinking flag, token count, tool/
  web/image flags), the way claude-code-router does it. NOT semantic prompt "tasting".
  Defer embedding/LLM routing until measured misrouting demands it.
- Sparks pin ONE hot model each; never trigger a cold model-swap on the interactive path —
  route to Foundry instead and (optionally) warm the Spark async.
- `agent_capable` is a TEST-EARNED, continuously-measured per-model flag. Never route a
  tool-using session to a model that fails tool-calling conformance.

YOUR TASK (Phase-0 groundwork — most needs no live infra):
1. BLOCKER A: Does LiteLLM's Responses API endpoint fully bridge to a Chat-Completions
   backend, including streaming + tool calls? Verify from LiteLLM docs AND source. If it
   doesn't, document that Codex→Spark is out for Phase 0 (Codex→Foundry-OpenAI still works).
   Record findings in docs/03 and docs/06.
2. BLOCKER B: Confirm how Anthropic models are served on Azure AI Foundry and which LiteLLM
   provider entry/config is correct (anthropic/ vs azure_ai/…). Update the Phase-0 config.
3. Build the tool-calling CONFORMANCE HARNESS (see docs/04): a script that drives a model
   through a real multi-tool task (Read → Edit → Bash) UNDER STREAMING and counts malformed/
   unparsed tool calls, emitting pass/fail + a tool-call-error-rate that sets `agent_capable`.
   Point it at a configurable OpenAI-compatible base_url so it can target a Spark later.
4. Produce a runnable PHASE-0 SCAFFOLD: litellm-config.yaml (from docs/06) parameterised via
   env, a docker-compose or run script, and a short RUNBOOK.md for standing it up with 1
   Spark + Foundry fallback and pointing Claude Code at it.

NEEDS HUMAN INPUT — ASK, do not invent:
- Real Spark inventory: hostnames/IPs, model pinned per box, memory headroom.
- Azure Foundry endpoint, deployment names, api-version, and how creds are provided.
- Is Codex in scope for Phase 0, or Claude Code only?

CONSTRAINTS:
- Never commit secrets — env vars / secret store only (.gitignore already covers .env*).
- Pin a VETTED LiteLLM version; 1.82.7 and 1.82.8 shipped credential-stealing malware.
- Org policy: no personal/customer data through these models; only Context& / Delegate /
  Projectum / Consit work. If unsure about data governance on Foundry, flag for DISCO.
- Persist findings into the docs/ files as you go, and commit + push.

DEFINITION OF DONE:
Blockers A & B answered in the docs; a runnable conformance harness; a Phase-0 scaffold +
RUNBOOK; docs/03 open-questions updated. Commit and push to the repo.
```
