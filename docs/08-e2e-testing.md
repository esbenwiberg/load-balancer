# E2E testing — the balancer without Foundry or Sparks

How we test the system without the real backends, and the trade-offs behind the
shape. Runnable harness lives in [`../e2e/`](../e2e/); this is the *why*.

## The premise, restated

We can't lean on the real backends to develop the balancer: Foundry costs money
and touches data-governance, and the Sparks aren't provisioned yet (RUNBOOK step
0 still needs a human). So we stand in for them. The insight that shapes
everything:

> **Two different things hide under "test it e2e."** The balancer's *logic*
> (protocol translation, fallback, cooldown, auth, streaming) needs
> **controllable** backends — not smart ones. A model's *tool-calling quality*
> (the `agent_capable` gate) needs a **real** model — and
> [`../conformance/`](../conformance/) already does that. Conflating them is the
> trap: you don't need a real model to test that a 503 triggers fallback, and
> you can't test tool-call quality against a fake one.

And the corollary that makes the mock profile worth building at all:

> **You cannot test fallback, cooldown, or mid-stream death against a real
> Spark.** You can't make a real box return 503, or die mid-stream, on cue. The
> highest-value thing to build is therefore a backend you can *order to
> misbehave*.

## The design: one gateway, swappable backend profiles

The **system under test** — the LiteLLM gateway and its config — stays identical
to [`../deploy/`](../deploy/). Only the backends swap, by profile:

```
 Claude Code ─(Anthropic /v1/messages)─┐
 Codex ───────(OpenAI /v1/responses)───┤
 pytest e2e ───────────────────────────┤──▶  LiteLLM :4000  ──▶  workbench alias ─┐  foundry aliases ─┐
 conformance ──────────────────────────┘         (the SUT)                        │                   │
                                                                      ┌────────────┴───────┐  ┌────────┴─────────┐
                                                          mock profile │ mockd (controllable)│  │ mockd (fallback) │
                                                       cli-auth profile │ Haiku (real)        │  │ Sonnet / GPT     │
                                                                        └─────────────────────┘  └──────────────────┘
```

### Decisions & trade-offs

| # | Decision | Why | Trade-off accepted |
|---|----------|-----|--------------------|
| 1 | **Started with `mock` + `cli-auth`; added `local` (Ollama) in goal 4.** | mock covers all *logic* deterministically in CI; cli-auth covers the *real client path*. The gap they leave — real tool-calling with **no keys/ToS, offline** — is now filled by the `local` profile (§ *The local profile* below). | `local` is heavy (multi-GB model + CPU inference) so it's **manual, never in CI**; the merge gate stays the mock profile. The config was already parameterised for it, as predicted. |
| 2 | **mockd doubles as a scripted-compliant agent.** | Turns `conformance.py` through LiteLLM into a *deterministic* CI gate for the **Responses→ChatCompletions bridge** (Blocker A) — the plumbing that was "confirmed on paper, smoke test pending". | It proves bridge **mechanics**, not model quality. A real model still has to earn `agent_capable` separately (cli-auth + conformance). |
| 3 | **Every backend is `openai/` in the mock profile — including `claude-*`.** | The balancer's interesting translation is *client-side* (Anthropic-in, Responses-in), which is provider-agnostic. Mocking the `azure_ai/`+`/anthropic` **backend** wire format faithfully would mean reverse-engineering Azure's surface. | The `azure_ai/` backend serialization is **not** covered here — it's validated only against real Foundry (RUNBOOK). Documented, not papered over. |
| 4 | **Fault injection via an out-of-band control endpoint** (`/__control`), not just prompt markers. | The test sits *upstream* of LiteLLM; it can't easily set backend headers. A side channel lets a test arm a fault, then drive through the gateway normally. | mockd holds mutable global state → tests must reset between cases (an autouse fixture does). |
| 5 | **Real Postgres in the stack.** | `/key/generate` (virtual keys — the attribution/revocation story) needs a DB; without one it 500s. Testing key scoping deterministically is worth the container. | Heavier stack, slower first boot (migrations). Acceptable for a test rig. |
| 6 | **Two client drivers: raw-HTTP (CI) + real-CLI (opt-in).** | Raw HTTP is deterministic, fast, and needs nothing installed — the CI gate. A real-CLI smoke (`smoke_cli.sh`) is the high-fidelity "does an actual agent work through it" check. | The real-CLI path is manual and flaky by nature; it's not in CI. |
| 7 | **cli-auth wires clean API keys only; subscription OAuth is refused.** | ToS + it doesn't work through a generic proxy without spoofing (see below). | The frictionless "just borrow the CLI's login" dream doesn't hold for subscription auth — you provision a key. Honest > convenient. |

## The cli-auth question, answered straight

The ask was "use the codex and/or claude CLI auth if possible." Investigated —
here's the real answer:

- **If the CLI is authed with an API key** → reuse it. It's just a key, zero
  issues. `borrow_creds.sh` wires it.
- **If it's a *subscription* OAuth token** (Claude Pro/Max, ChatGPT) → **don't.**
  1. Using a personal subscription token to back a proxy violates Anthropic/
     OpenAI consumer **ToS** and can flag the account.
  2. Those tokens are validated to come from the real client (right
     `anthropic-beta` header, the client's own system prompt), so a generic
     proxy request generally **401s** anyway — making it work means spoofing the
     client, which is more effort *and* a clearer violation. It defeats the
     "frictionless" point.
  → Provision an API key. The org has an **Anthropic Enterprise license** — a
  workspace key is the clean, supported path. On the current dev box both CLIs
  are OAuth-only, so cli-auth needs a provisioned key to run at all.

## What this harness has already earned

Building it flushed out two real bugs before they hit anyone:

1. **`model_info.tier` silently drops every deployment.** `tier` is a reserved
   LiteLLM `ModelInfo` literal (`'free'|'paid'`); the Phase-0 scaffold's
   `tier: local`/`tier: foundry` failed validation → "no healthy deployments" on
   boot. Fixed to `backend_tier` in **both** the e2e config *and*
   [`../deploy/litellm-config.yaml`](../deploy/litellm-config.yaml). This would
   have bricked the production Phase-0 stand-up.
2. **Non-streaming `/v1/messages` → openai backend drops the text** in LiteLLM
   1.83.14 (usage still maps; streaming is fine). Coding agents stream, so
   impact is low — but it's guarded by a test that flips to failing when a
   LiteLLM bump fixes it, so we'll know to drop the caveat.

Both are logged as risks worth carrying into deploy validation.

## The local profile — the `agent_capable` gate against a real model, offline & keyless (goal 4)

The mock profile can prove every *logic* path but, by construction, can never
give a verdict on a *real* model — `mockd` replays a script, so it would always
"pass" the gate. `cli-auth` gives a real model but needs API keys and hits the
internet. The **`local`** profile closes that gap: it points `conformance.py`
(the harness that *earns* `agent_capable`) at a real small coding model
(`qwen2.5-coder:3b`) served by **Ollama** behind the same gateway, with **no
keys, no ToS, and no network** once the model is pulled. Runnable bits:
[`../e2e/docker-compose.local.yaml`](../e2e/docker-compose.local.yaml),
[`../e2e/litellm-config.local.yaml`](../e2e/litellm-config.local.yaml),
[`../e2e/run.local.sh`](../e2e/run.local.sh).

**Why keyless local real tool-calling matters.** `agent_capable` is the gate
that decides whether a backend is fit to serve a coding agent at all, and it can
*only* be earned by a real model — a fake one that replays clean tool calls
proves nothing. Every other way to get a real model costs something that blocks
an unattended/offline run: Foundry costs money and trips data-governance;
cli-auth needs a provisioned key and the public internet. Ollama serving a small
coding model is the only path that is simultaneously **real, free, offline, and
ToS-clean** — which also makes it the closest analog we have to a Spark
workbench (a box running a small local model) until real Sparks are provisioned.
It's the stand-in for one meanwhile.

**The Mac CPU-only-in-Docker caveat.** On a Mac, Docker Desktop runs Linux
containers in a lightweight VM with **no GPU passthrough**, so Ollama here runs
**CPU-only**. `qwen2.5-coder:3b` is chosen to fit that envelope. Two
consequences: (1) it's *slow* — a cold multi-turn Read→Edit→Bash run can take
minutes (the config carries a 600s gateway timeout for exactly this), which is a
core reason the profile is manual and never in CI; and (2) the backend model is a
single knob — `OLLAMA_MODEL` drives both what the entrypoint pulls and what the
gateway requests (`model: os.environ/OLLAMA_MODEL`), so swapping it (e.g.
`OLLAMA_MODEL=qwen2.5-coder:7b ./run.local.sh`) needs no other change.

**What the first bring-up found (the gate discriminates).** Pointed at
`qwen2.5-coder`, conformance returned **`agent_capable=false`** — a *real*
result, not a rubber stamp. Both `:3b` and `:7b` emit a tool call but **leak it
into `content`** instead of structured `tool_calls` (3b's args are malformed —
it echoes the JSON schema; 7b's args are valid — `{"city":"Paris"}` — but still
unwrapped). Root cause, diagnosed not guessed: qwen2.5-coder's Ollama template
requires the model to wrap calls in `<tool_call>…</tool_call>` for Ollama's
parser to structure them, and the model omits the wrapper. It reproduces
**direct against Ollama** (native `/api/chat` *and* OpenAI-compat `/v1`) and
through **both** LiteLLM providers (`openai/`, `ollama_chat/`) — so it's the
model+engine, not the gateway, which faithfully passes Ollama's output through.
This is precisely the *plumbing-vs-quality* split this doc is built on: the
profile's job is to run the real gate against a real backend and report a
trustworthy verdict, which it does. A **green** is a model-choice question — swap
`OLLAMA_MODEL` to a tag whose Ollama build emits structured tool calls (a
`llama3.1`/`qwen3`-family template) — and nothing else changes. We kept
`qwen2.5-coder` (the goal's named coding model) and documented the finding rather
than chase a green with another family: the deliverable is the profile + a
trustworthy gate, not a particular model passing.

**How to point conformance at it.** The workbench alias is the same `qwen3-coder`
every profile uses (only the backend swaps), so conformance targets it exactly as
against the mock profile — `run.local.sh` does this for you and prints the JSON
verdict:

```bash
../.venv-e2e/bin/python ../conformance/conformance.py \
  --base-url http://localhost:4000/v1 --api anthropic \
  --model qwen3-coder --api-key "$LITELLM_MASTER_KEY" --runs 3
```

**Hard constraint — never in CI.** Ollama + a multi-GB model + CPU inference is
far too heavy for the CI gate, and nothing wires it there: `run.sh` (the merge
gate) and the CI workflow use the mock profile only, and `docker-compose.local.yaml`
is referenced by nothing but `run.local.sh`. `scripts/check.sh` *does* `docker
compose config`-validate the file in its fast tier, but that is schema
validation, not `up` — it starts no containers. The deliverable is the profile +
docs, **machine-verified by the mock profile staying green**.

## Not covered here (know the edges)

- The `azure_ai/`+`/anthropic` **backend** wire format (decision 3) — real
  Foundry only.
- HA / gateway-as-SPOF (docs/03 risk 12) — single instance by design.
- Real Spark latency (docs/03 risk 3) — mockd answers instantly; it tests
  *correctness of routing*, never *performance*.
- Prompt-caching behaviour on the Azure route (docs/03 risk 5, unconfirmed).

## Next

- ~~Wire `e2e/run.sh` (mock profile) into CI~~ — done (goal 1).
- ~~Add an Ollama profile for offline real-tool-calling without keys~~ — done
  (goal 4, § *The local profile* above). Landed ahead of Sparks: with Spark
  intake parked, it's the stand-in for a workbench, not just a future addition.
- ~~Add a mid-stream-`hangup` fallback assertion~~ — done (goal 2).
