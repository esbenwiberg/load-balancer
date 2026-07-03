# End-to-end test harness — the balancer, without Foundry or Sparks

Test the balancer (the LiteLLM gateway now; router + control plane later) with
**zero real backends**. Two profiles behind the same gateway config:

| Profile | Backends | Proves | Cost / deps |
|---|---|---|---|
| **mock** (default, CI) | `mockd` — a controllable fake speaking OpenAI Chat + Responses | protocol translation, the Responses bridge, fallback, cooldown, virtual-key scoping, streaming | free, offline, deterministic |
| **cli-auth** (opt-in, manual) | REAL hosted models — Haiku as the "workbench", bigger models as "Foundry" | the real Claude Code / Codex client path end-to-end | needs API keys; hits the internet |

Both leave the **system under test** — the gateway + its config — identical to
what ships in [`../deploy/`](../deploy/). Only the backends swap.

> **Why two, not one?** Two different things hide under "test it e2e":
> the balancer's *logic* (routing/fallback/translation/auth — needs
> *controllable* backends, not smart ones) and a model's *tool-calling quality*
> (the `agent_capable` gate — needs a *real* model, and
> [`../conformance/`](../conformance/) already does that). The mock profile owns
> the first; cli-auth + conformance own the second. See
> [`../docs/08-e2e-testing.md`](../docs/08-e2e-testing.md) for the full design.

---

## Profile: mock (start here)

```bash
cd e2e
./run.sh              # up -> pytest -> conformance-through-gateway -> teardown
./run.sh --keep       # leave the stack running to poke at :4000 / :9100
```

`run.sh` creates a venv, brings up the stack (`docker-compose.e2e.yaml`: LiteLLM
+ mockd + Postgres), waits for health, then runs:

1. **`test_e2e.py`** — raw-HTTP suite emulating the real clients: Anthropic
   `/v1/messages` (streaming, the Claude Code path), OpenAI `/v1/responses` (the
   Codex path), fallback on injected 5xx, cascading fallback, virtual-key model
   scoping, missing-auth rejection, streaming integrity.
2. **conformance through the gateway** — `conformance.py --api responses`
   pointed at LiteLLM. mockd plays the Read→Edit→Bash scenario by the rules, so
   this is **deterministically green** and gates the **Responses→ChatCompletions
   bridge mechanics** (Blocker A, [docs/03 risk 4](../docs/03-open-questions-and-risks.md)) —
   the plumbing, not a real model's quality.

### mockd — the controllable backend

Stdlib-only ([`mockd.py`](mockd.py)), speaks `/v1/chat/completions` and
`/v1/responses` with streaming + structured tool calls. Two jobs:

- **Scripted-compliant agent.** Given the conformance tools it drives the exact
  scenario in [`../conformance/scenarios.py`](../conformance/scenarios.py),
  inferring the next step from how many tool-results it's been handed.
- **Misbehaves on command** — the only way to test fallback/cooldown/detectors
  deterministically (you can't make a real Spark 500 or die mid-stream on cue):

```bash
# next 2 requests to qwen3-coder return HTTP 503, then auto-clear:
curl localhost:9100/__control -d '{"model":"qwen3-coder","status":503,"count":2}'
curl localhost:9100/__control -d '{"model":"qwen3-coder","mode":"runaway"}'   # !!!! stream
curl localhost:9100/__control -d '{"model":"qwen3-coder","mode":"leak"}'      # <tool_call> in content
curl localhost:9100/__control -d '{"model":"qwen3-coder","mode":"hangup"}'    # mid-stream death
curl localhost:9100/__control -d '{"model":"*","latency_ms":800}'             # slow / cold-ish
curl -X POST localhost:9100/__reset                                          # clear all
```

Modes: `agent` (default) · `leak` · `runaway` · `malformed` · `hangup` · `echo`.
Faults also inject inline via a prompt marker: `[[mockd:status=500]]`.
`MOCKD_DEBUG=1` logs the exact body LiteLLM forwards (how the finding below was
found).

---

## Profile: cli-auth (real models, opt-in)

Drive **real** Claude Code / Codex through the balancer, with a small model
(Haiku) standing in for a Spark workbench and bigger models as the Foundry
fallback tier.

```bash
cd e2e
./borrow_creds.sh     # discover API keys -> .env.cliauth (gitignored)
docker compose --env-file .env.cliauth -f docker-compose.cliauth.yaml up -d
./smoke_cli.sh        # mint a virtual key, drive the installed CLIs (raw-HTTP for CI; this is the fidelity check)
```

### Credentials — the clean vs ToS-gray split (read this)

`borrow_creds.sh` is deliberately conservative:

- **Clean path — a real API key** (an `ANTHROPIC_API_KEY`/`OPENAI_API_KEY` in
  your env, or an `api_key` in `~/.codex/auth.json`). It's just a key — reused
  as-is. **This is what gets wired.**
- **Subscription OAuth** (Claude Pro/Max token in the macOS Keychain; ChatGPT
  tokens in `~/.codex/auth.json`) is **detected and refused, not wired**,
  because:
  1. using a *personal subscription* token to back a proxy is against
     Anthropic/OpenAI consumer **ToS** and can flag the account; and
  2. those tokens are validated to originate from the real client, so a generic
     proxy request generally **401s** without heavy spoofing that defeats the
     purpose.

  → Provision an API key instead. **The org has an Anthropic Enterprise
  license** — a workspace key is the clean, supported way to get a real Claude
  backend for testing. (On the current dev box, both CLIs are OAuth-only, so
  cli-auth needs a provisioned key to run at all — `borrow_creds.sh` says so.)

### Org data-governance guardrail

The cli-auth profile hits the public internet and real models. Per org policy:
**only Context& / Delegate / Projectum / Consit work, and no personal/customer
data** through it. Keep smoke prompts synthetic. If in doubt → **DISCO**.

---

## Findings this harness has already caught

- **`model_info.tier` drops the deployment.** `tier` is a reserved LiteLLM
  `ModelInfo` field (`'free'|'paid'`); the scaffold's `tier: local`/`tier:
  foundry` failed validation and LiteLLM **silently dropped every backend** →
  "no healthy deployments". Renamed to `backend_tier` here **and in
  [`../deploy/litellm-config.yaml`](../deploy/litellm-config.yaml)** — this was a
  latent boot-time bug in the shipped Phase-0 config.
- **Non-streaming `/v1/messages` over an openai backend drops text.** LiteLLM
  1.83.14 returns an empty content block for a *non-streaming* Anthropic request
  translated to an OpenAI chat backend (usage still maps). The **streaming path
  — what Claude Code actually uses — is fine.** Guarded by
  `test_anthropic_messages_nonstream_content_quirk`, which flips to failing if a
  LiteLLM bump fixes it. (docs/03.)

## Files

```
mockd.py                     controllable mock backend (stdlib, no deps)
litellm-config.e2e.yaml      mock-profile gateway config (all aliases -> mockd)
docker-compose.e2e.yaml      mock stack: litellm + mockd + postgres
test_e2e.py                  raw-HTTP pytest suite (the CI driver)
run.sh                       up -> test -> conformance gate -> teardown
requirements.txt             test-driver deps (httpx, pytest, openai)
.env.e2e.example             mock-profile env (test-only, safe to commit)

litellm-config.cliauth.yaml  cli-auth gateway config (real providers, env-keyed)
docker-compose.cliauth.yaml  cli-auth stack: litellm + postgres (no mockd)
borrow_creds.sh              discover clean API keys -> .env.cliauth (gitignored)
smoke_cli.sh                 opt-in: drive real Claude Code / Codex (fidelity check)
```
