# End-to-end test harness — the balancer, without Foundry or Sparks

Test the balancer (the LiteLLM gateway now; router + control plane later)
without real Foundry or Sparks. Four profiles behind the same gateway config:

| Profile | Backends | Proves | Cost / deps |
|---|---|---|---|
| **mock** (default, CI) | ONE `mockd` — a controllable fake speaking OpenAI Chat + Responses, serving every alias | protocol translation, the Responses bridge, fallback, cooldown, virtual-key scoping, streaming | free, offline, deterministic |
| **dev** (standing fixture) | THREE `mockd` containers — two distinct workbench slots + a mock-Foundry, each stamping its own instance identity | the full local topology as a leave-it-running dev target you point a real client at; per-instance load/faults | free, offline; stays up until torn down |
| **local** (opt-in, manual) | REAL small coding model (`qwen3:8b`) served by **Ollama** as the workbench | runs the `agent_capable` gate against a **real** model (default passes: `agent_capable=true`), **no keys, no ToS, offline** | free, offline; heavy (multi-GB model + CPU inference). **Never in CI** |
| **cli-auth** (opt-in, manual) | REAL hosted models — Haiku as the "workbench", bigger models as "Foundry" | the real Claude Code / Codex client path end-to-end | needs API keys; hits the internet |

All leave the **system under test** — the gateway + its config — identical to
what ships in [`../deploy/`](../deploy/). Only the backends swap.

> **Why four, not one?** Two different things hide under "test it e2e":
> the balancer's *logic* (routing/fallback/translation/auth — needs
> *controllable* backends, not smart ones) and a model's *tool-calling quality*
> (the `agent_capable` gate — needs a *real* model, and
> [`../conformance/`](../conformance/) already does that). The **mock** + **dev**
> profiles own the first; **local** + **cli-auth** + conformance own the second —
> `local` runs the `agent_capable` gate against a real model offline and keyless,
> `cli-auth` gets you the real hosted client path. See
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
   scoping, missing-auth rejection, streaming integrity, **negative paths**
   (malformed JSON body + unknown model alias → a clean `4xx`, never a `5xx` or a
   hang, on all three client surfaces), and **concurrency smoke** (goal 9): a
   fleet of parallel streams across four aliases — with a 503 on one forcing a
   fallback hop mid-fleet — asserts every response carries its *own* backend's
   `served_model` stamp (no cross-request bleed) and terminates its stream
   cleanly, including a mixed-surface variant that runs chat + responses +
   messages streams at once to catch interleaved SSE across protocols. Also the
   **wallet guardrails** (goal 11): every issued key inherits a default budget +
   rate limit from config, and an over-budget key and an over-rate-limit key are
   each refused with a clean `4xx` (never a `5xx` or a hang). See
   [Budgets & rate limits](#budgets--rate-limits-goal-11) below.
2. **conformance DIRECT against mockd** — `conformance.py --api chat` pointed at
   mockd's OpenAI chat endpoint with **no gateway hop**. This isolates a mockd
   regression from a gateway regression: the other conformance steps run
   *through* the gateway, so without this a broken mockd and a broken gateway are
   the same red. Runs first, so an isolated backend fault is attributed before
   the gateway steps can muddy the signal.
3. **conformance through the gateway** — `conformance.py --api responses` (and
   `--api anthropic`) pointed at LiteLLM. mockd plays the Read→Edit→Bash scenario
   by the rules, so this is **deterministically green** and gates the
   **Responses→ChatCompletions bridge mechanics** (Blocker A,
   [docs/03 risk 4](../docs/03-open-questions-and-risks.md)) — the plumbing, not a
   real model's quality.

The LiteLLM image pin (never the `1.82.7`/`1.82.8` malware tags — [docs/03 risk 8](../docs/03-open-questions-and-risks.md))
is machine-enforced by `scripts/check.sh` (fast tier, so it runs in the
pre-commit hook, the Stop hook, and CI): every active `litellm` image reference
across compose files must equal the vetted pin, or the gate fails.

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

### Budgets & rate limits (goal 11)

Unattended goal runs (and, later, a hosted endpoint) mean runaway-spend risk —
the point is to burn *subscription*, not rack up an invoice. So **every virtual
key the gateway mints inherits a default budget and rate limit from config**,
and a key that blows past either is refused with a clean `4xx`.

**Where the defaults live** — `litellm-config.e2e.yaml` →
`litellm_settings.default_key_generate_params`:

```yaml
litellm_settings:
  default_key_generate_params:
    max_budget: 100          # USD of spend, lifetime (no reset window)
    rpm_limit: 60            # requests per minute, per key
    tpm_limit: 200000        # tokens per minute, per key
```

LiteLLM applies each field only when the `/key/generate` call left it unset, so
these are *defaults*, not caps — an explicit value on the request always wins.
The defaults are deliberately generous: they're a backstop against a runaway
loop, not a throttle on normal traffic.

**How to raise (or lower) a limit**

- *Per key, at issue time* — pass the field explicitly; it overrides the default:
  ```bash
  curl -X POST localhost:4000/key/generate -H "Authorization: Bearer $MASTER" \
    -d '{"models":["qwen3-coder"],"max_budget":1000,"rpm_limit":600}'
  ```
  Set `budget_duration` (e.g. `"30d"`) for a budget that resets on a window
  instead of a lifetime cap.
- *For an existing key* — `POST /key/update {"key": "...", "rpm_limit": ...}`.
- *For every future key* — edit `default_key_generate_params` above.

**What refusal looks like** (both are 4xx, never 5xx, never a hang):

| Over… | HTTP | `error.type` |
|-------|------|--------------|
| budget (`spend >= max_budget`) | `400` | `budget_exceeded` |
| rate limit (`rpm`/`tpm`)       | `429` | rate-limit message |

**Units caveat (the goal-11 / goal-11b boundary).** `max_budget` is USD of
*spend*, and spend = tokens × per-model cost. The goal-11 **gate** tests stay
cost-independent on purpose: the over-budget test uses an explicit `max_budget:
0` key so the `spend >= max_budget` gate trips on the first request, without
depending on async spend-flush or auth-cache TTL. Goal **11b** adds the other
half — a per-token cost on the `qwen3-coder` alias, so mockd traffic now accrues
**nonzero, attributable, durable** spend. See [Spend audit](#spend-audit--who-spent-what-goal-11b).

### Spend audit — who spent what (goal 11b)

Budgets (goal 11) cap the damage; this makes spend **attributable and durable**.
Every key belongs to a **user**, users group into **teams**, and every request's
dollar spend is queryable per key / user / team after the fact — and survives a
gateway restart because it lives in Postgres, not gateway memory.

**How spend comes to exist.** The `qwen3-coder` alias carries a per-token cost
(`litellm-config.e2e.yaml` → `litellm_params.input_cost_per_token` /
`output_cost_per_token`). mockd returns a usage block on every reply, so
`spend = tokens × cost > 0`. LiteLLM buffers spend in memory and flushes it to
Postgres on an interval (`general_settings.proxy_batch_write_at`, set low here so
tests are fast); the audit endpoints below read the DB, so they report spend only
*after* a flush.

**The hierarchy** — team → user → key:

```bash
MASTER="$LITELLM_MASTER_KEY"; H="Authorization: Bearer $MASTER"
# 1. a team
curl -sX POST localhost:4000/team/new    -H "$H" -d '{"team_id":"acme","team_alias":"acme","max_budget":1000}'
# 2. a user, grouped INTO that team
curl -sX POST localhost:4000/team/member_add -H "$H" -d '{"team_id":"acme","member":{"user_id":"alice","role":"user"}}'
# 3. a key bound to that user + team
curl -sX POST localhost:4000/key/generate -H "$H" -d '{"models":["qwen3-coder"],"user_id":"alice","team_id":"acme"}'
```

> **Data governance:** `user_id` / `team_id` are synthetic handles — never put a
> real name or email here (no PII), per the CLAUDE.md guardrail.

**The audit queries** (all require the master key):

| Question | Query |
|----------|-------|
| What did this **key** spend? | `GET /key/info?key=sk-…` → `info.spend` |
| What did this **user** spend? | `GET /user/info?user_id=alice` → `spend` |
| What did this **team** spend? | `GET /team/info?team_id=acme` → `spend` |
| Per-**request** ledger (who/what/how much) | `GET /spend/logs?user_id=alice` → rows of `{request_id, api_key (sha256), user, team_id, model, spend, total_tokens}` |

`/spend/logs` is the per-request truth table straight out of `LiteLLM_SpendLogs`;
the `*/info` endpoints are the running aggregates on `LiteLLM_VerificationToken`
(key), `LiteLLM_UserTable`, and `LiteLLM_TeamTable`. Direct SQL against Postgres
is an equivalent audit path (`docker compose -f docker-compose.e2e.yaml exec db
psql -U litellm -c 'select api_key,user,team_id,model,spend from "LiteLLM_SpendLogs";'`).

**What the suite proves.** `test_spend_attributed_to_key_user_team` provisions a
fresh team→user→key (all zero-spend), sends costed traffic, and asserts a
`SpendLogs` row carries the right user + team + model + nonzero spend hashed to
that key, and that all three `*/info` aggregates read nonzero.
`test_spend_survives_gateway_restart` then confirms the ledger in Postgres,
**restarts the gateway container** (run.sh sets `E2E_ALLOW_RESTART=1`), and
re-reads it: spend and the issued key are still there. That's the open
persistence question answered — **keys and their spend survive a restart.**
(`prod`/`deploy` today runs stateless with no DB; the same endpoints and audit
work identically once a Postgres is wired behind it.)

### Observability — "where did my prompt go?" (goal 3)

Every request leaves a per-request routing trail: **{chosen backend, why,
latency, tokens, fallback-hit}**. It's captured by a LiteLLM callback
(`obs_callback.py`, wired via `litellm_settings.callbacks`) with no external
observability stack.

The callback publishes two record shapes — `llm_call` (one per backend
**attempt**: backend, tier, latency, tokens, and on failure the error that
triggered a fallback) and `delivered` (one per **request**: requested vs served
backend ⇒ `fallback` flag, plus tokens). Records go to **stdout**
(`ROUTING_RECORD <json>`) always, and `OBS_WEBHOOK_URL` fans each record to a
**comma-separated list** of webhook sinks. The e2e compose points it at *both*
`http://mockd:9100/__observe` (so the suite can read records back) *and*
`http://dashboard:9300/records` (the goal-12 dashboard, below):

```bash
./run.sh --keep
curl -s localhost:9100/__observe | jq '.records'      # raw record stream
```

`test_fallback_is_observable_in_routing_record` forces a fallback and asserts it
shows up in the records. Full design (incl. the LiteLLM "fallback winner logs no
success event" quirk, and the prod stdout path): **[docs/09](../docs/09-observability.md)**.

### The dashboard — a read-only view of the above (goal 12)

`dashboard.py` (`:9300`, stdlib-only, same shape as mockd) is the **visible face**
of that data: a routing-record **sink** (`POST /records`) plus a read-only **page**
(`GET /`) and its **data endpoint** (`GET /api/records`). Open it while any stack
is up and watch every prompt's route land live:

```bash
./run.sh --keep                                       # or the dev stack (below)
open http://localhost:9300                            # the page
curl -s localhost:9300/api/records | jq '.requests'   # per-request routing rows
```

It shows **Requests** (requested alias → served backend, `direct`/`fallback`
badge, tokens, cost) and the **Attempt trail** (every backend tried + the error
that forced a fallback). We *built* this thin page rather than reuse LiteLLM's
admin UI — the routing-record shape is ours, an owned JSON endpoint is
assertable, and it keeps the zero-dependency floor; full reasoning in
[docs/09](../docs/09-observability.md) and `dashboard.py`'s header.

**Dashboard v2 — the Fleet view (goal 13).** The same page also shows *what the
fleet is doing right now*: which workbenches are subscribed, with which models,
warm/healthy, and their live in-flight load. That data is the control-plane
registry ([goal 5](../docs/10-control-plane.md)) — the dashboard reads it
server-side and re-serves it at its own `GET /api/fleet`:

```bash
curl -s localhost:9300/api/fleet | jq '.models[] | {model, healthy, warm, in_flight}'
```

In the **dev** stack each mockd workbench pushes real heartbeats
(`HEARTBEAT_URL`/`HEARTBEAT_MODELS`), so the Fleet view is live — drive traffic
through `:4000` and watch `in_flight` move. In the **e2e** stack no mockd beats
(kept deterministic); the fleet assertion pushes its own heartbeats.
`test_dashboard_fleet_reflects_control_plane_registry` and
`test_dashboard_fleet_surfaces_derived_health` cover the registry→dashboard path;
`dashboard_test.py` covers the offline shaping + graceful-degrade branches.

`test_dashboard_data_endpoint_shows_direct_request`,
`test_dashboard_data_endpoint_shows_fallback_route`, and
`test_dashboard_page_renders` cover the data endpoint + page.

---

## Profile: dev — the standing self-validation fleet (goal 10)

The mock profile above is a CI gate: one `mockd`, up → test → **down**. The **dev
profile** is a *leave-it-running* fixture — the gateway in front of **three
distinct mock containers** — so an agent building features (or a real Claude
Code / Codex) can point at it, iterate, and inject per-instance faults. It's the
local miniature of the endgame topology.

```bash
cd e2e
docker compose -f docker-compose.dev.yaml up -d      # bring up, stays up
./dev_smoke.sh                                        # prove all 3 surfaces route
docker compose -f docker-compose.dev.yaml down -v     # explicit teardown
```

Topology (`docker-compose.dev.yaml`):

| Service | Host port | Instance identity | Role |
|---|---|---|---|
| `litellm` | `:4000` | — | the gateway (SUT, same config shape as `deploy/`) |
| `workbench-a` | `:9101` | `workbench-a` | a Spark workbench slot (`qwen3-coder-a`) |
| `workbench-b` | `:9102` | `workbench-b` | a second, distinct workbench slot (`qwen3-coder-b`) |
| `foundry` | `:9103` | `mock-foundry` | the always-up fallback tier (`claude-sonnet` / `claude-opus` / `gpt`) |
| `dashboard` | `:9300` | — | goals 12+13 viewer — routes (`/api/records`) + fleet (`/api/fleet`); open `http://localhost:9300` |
| `control-plane` | `:9400` | — | goal-5 fleet registry — each workbench heartbeats it; the dashboard renders it |
| `db` | (internal) | — | Postgres — the virtual-key store |

The dev gateway wires the same `obs_callback` and fans records to the dashboard
(`OBS_WEBHOOK_URL=http://dashboard:9300/records`) — so with the stack up, every
prompt you send through `:4000` shows up live at `http://localhost:9300`.

Each `mockd` workbench also PUSHES heartbeats to the `control-plane`
(`HEARTBEAT_URL`/`HEARTBEAT_MODELS`), so the dashboard's **Fleet** section shows
the live fleet — per-workbench models, health, and in-flight load that moves as
you drive traffic (goal 13). `dev_smoke.sh` asserts the fleet populates.

Each `mockd` sets a distinct `MOCKD_INSTANCE`, so its reply stamps
`served_model=<model>@<instance>` — that's how you tell two otherwise-identical
containers apart. The single-mockd mock profile leaves `MOCKD_INSTANCE` unset, so
its stamp stays the bare `served_model=<model>` the CI suite asserts.

**`dev_smoke.sh`** mints a scoped virtual key, then drives **all three client
surfaces** through the gateway, each to a *different* container, and asserts the
instance stamp:

- Anthropic `/v1/messages` (streaming, Claude Code's path) → `qwen3-coder-a` → `workbench-a`
- OpenAI `/v1/chat/completions` → `qwen3-coder-b` → `workbench-b`
- OpenAI `/v1/responses` (Codex's path, via the bridge) → `claude-sonnet` → `mock-foundry`

Each host-published `mockd` port (`:9101/2/3`) takes `/__control` independently,
so you can fault **one** instance and watch the gateway fall back while the
other stays up:

```bash
curl localhost:9101/__control -d '{"model":"*","status":503}'  # fault workbench-a only
curl localhost:9101/v1/chat/completions -d '{"model":"qwen3-coder-a","messages":[{"role":"user","content":"hi"}]}'  # -> 503
curl localhost:9102/v1/chat/completions -d '{"model":"qwen3-coder-b","messages":[{"role":"user","content":"hi"}]}'  # -> 200 (unaffected)
curl -X POST localhost:9101/__reset                             # clear it
```

### Point a real client at the dev stack

The gateway is OpenAI- **and** Anthropic-compatible on `:4000`. First mint a key
(or use the master key `sk-dev-master-test-key` directly — test-only, not a
secret):

```bash
MASTER=sk-dev-master-test-key    # test-only default, not a secret
KEY=$(curl -s -X POST localhost:4000/key/generate \
  -H "Authorization: Bearer $MASTER" -H "Content-Type: application/json" \
  -d '{"models":["qwen3-coder-a","qwen3-coder-b","claude-sonnet","claude-opus","gpt"],"user_id":"me"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['key'])")
```

**Claude Code** → the Anthropic surface:

```bash
ANTHROPIC_BASE_URL="http://localhost:4000" \
ANTHROPIC_AUTH_TOKEN="$KEY" \
ANTHROPIC_MODEL="qwen3-coder-a" \
  claude -p "Reply with exactly: PONG"
```

**Codex** → the OpenAI Responses surface (Codex speaks `/v1/responses`, bridged
to the mock chat backend via `use_chat_completions_api`):

```bash
OPENAI_BASE_URL="http://localhost:4000/v1" \
OPENAI_API_KEY="$KEY" \
  codex exec --model claude-sonnet "Reply with exactly: PONG"
```

Against the default (keyless, offline) dev stack a real client gets mockd's
canned stamp back, not a model's reasoning — enough to confirm the *plumbing*
(auth, routing, translation, streaming) works. For real model output, use the
haiku variant below.

### Variant: one workbench slot backed by real Haiku (NOT the default)

The default dev stack is **keyless and offline** — every backend is a `mockd`.
If you want one slot to return *real* model output (a closer stand-in for a Spark
until real ones arrive), back it with Haiku via the existing cli-auth borrow —
this is a **documented option, deliberately not the default**, because it needs a
real key and hits the internet (org data-governance applies — synthetic prompts
only, Context& / Delegate / Projectum / Consit work, in doubt → DISCO):

```bash
./borrow_creds.sh     # discover a clean ANTHROPIC_API_KEY -> .env.cliauth (gitignored)
```

Then run the **cli-auth** profile below (its `haiku` alias is real Haiku). The
dev stack stays the keyless default; the cli-auth stack is the real-model path.
Keeping the two separate is deliberate: the default must never need a key or the
network, so an unattended run can always bring the fleet up.

### Load-balanced workbench alias — a future knob

The two workbenches are given **distinct** aliases (`qwen3-coder-a` /
`qwen3-coder-b`) pinned one-to-one to a container, so each is individually
addressable — the point of goal 10 is to *tell instances apart* and fault them
independently. LiteLLM also supports one `model_name` with *two* deployments for
real load-balancing; that hides which instance served (bad for the smoke) but is
the right shape once the control plane (goal 5) drives placement. Add it as a
third deployment in `litellm-config.dev.yaml` when that lands.

---

## Profile: local — the `agent_capable` gate against a real model, offline & keyless (goal 4)

The one thing the mock profile **can't** give: a verdict on a *real* model.
`mockd` replays a scripted Read→Edit→Bash sequence — perfect for testing the
balancer's logic, useless for measuring a model's `agent_capable` quality (it
would always "pass"). `cli-auth` gives a real model but needs API keys and hits
the internet. The **local** profile is the third leg: it points
[`conformance.py`](../conformance/conformance.py) — the harness that *earns*
`agent_capable` — at a real small coding model (**`qwen3:8b`** on
[Ollama](https://ollama.com)) behind the gateway, with **no keys, no ToS, and no
network** once the model is pulled. It's the closest analog to a Spark workbench
until real Sparks arrive — and the stand-in for one meanwhile.

> **The default model clears the gate for real: `agent_capable=true`.** Pointed at
> `qwen3:8b`, conformance passes end-to-end — structured tool calls (no leak into
> content), the full Read→Edit→Bash task completed, and both probes (parallel +
> `tool_choice:required`) honored, all over streaming. Not every model does — see
> [The model ladder](#the-model-ladder-what-passes-and-what-doesnt) below for what
> fails and why. That's the profile working *correctly*: it runs the real gate
> against a real backend and the gate *discriminates* — exactly what mockd can't.

```bash
cd e2e
./run.local.sh                 # up -> conformance (anthropic, 1 run) -> down
./run.local.sh --keep          # leave the stack up to poke :4000 / :11434
./run.local.sh --api chat      # wire protocol: chat | responses | anthropic
./run.local.sh --runs 3        # more runs for a stabler error rate
```

> **Heads-up: the green default is slow on CPU.** `qwen3:8b` uses reasoning
> ("thinking") mode, which is what lets it drive the multi-step loop — but on a Mac
> (CPU-only, see below) a full run can take **10–20+ minutes**. That's the price of
> a real green here; lighter (red) models are one env var away — see the ladder.

`run.local.sh` brings up `docker-compose.local.yaml` (just **Ollama + the
gateway** — no Postgres, no mockd), waits until the model is pulled *and loaded*,
then runs [`../conformance/conformance.py`](../conformance/conformance.py)
**through the gateway** against the real model and prints the JSON verdict. The
alias is the same `qwen3-coder` every profile uses — only the backend swaps — so
you point conformance at it exactly as against the mock profile:

```bash
../.venv-e2e/bin/python ../conformance/conformance.py \
  --base-url http://localhost:4000/v1 --api anthropic \
  --model qwen3-coder --api-key "$LITELLM_MASTER_KEY" --runs 3
```

### Self-contained bring-up (pull is entrypoint-gated)

The Ollama container is self-contained: [`ollama-entrypoint.sh`](ollama-entrypoint.sh)
starts the daemon, **pulls the model**, warms it into memory, then serves. The
compose **healthcheck** gates the gateway's `depends_on` until both the daemon
answers *and* the pull+warm finished (a readiness marker), so the gateway never
boots against an empty Ollama. Pulled models persist on a named volume, so only
the **first** run downloads (`docker compose -f docker-compose.local.yaml down -v`
wipes them).

### The Mac CPU-only-in-Docker caveat

On a Mac, Docker Desktop runs Linux containers in a **lightweight VM with no GPU
passthrough** — so Ollama here runs **CPU-only**. Consequences:

- **It's slow.** A cold multi-turn Read→Edit→Bash run can take minutes to tens of
  minutes (the config sets a 600s gateway timeout for exactly this) — and the
  green default `qwen3:8b`, with reasoning mode, is at the slow end. This is a
  core reason the profile is **manual and never in CI** — see below.
- **The backend model is one env var.** `OLLAMA_MODEL` drives **both** what the
  Ollama entrypoint pulls **and** what the gateway requests (the config reads it
  via `model: os.environ/OLLAMA_MODEL`), so swapping the model is a single knob
  with no other change:
  ```bash
  OLLAMA_MODEL=qwen2.5-coder:3b ./run.local.sh   # faster, but red (see the ladder)
  ```

### Fast path: native Ollama on the host (GPU)

The CPU-only slowness is a **Docker-on-Mac** limit, not a model limit: Apple's GPU
is reachable only through **Metal**, which needs a native macOS process — Docker
Desktop runs Linux containers in a VM with no Metal passthrough, so *any* Mac
container is CPU-only (same for Colima/Podman/Apple's `container` — all Linux VMs).
GPU-in-Docker only works on a **Linux host with an NVIDIA GPU**.

So on a Mac, run the **model** natively (Metal, fast) but keep the **gateway**
containerized (prod parity + the vetted pinned image — never a host `pip install
litellm`, the supply-chain vector [docs/03 risk 8](../docs/03-open-questions-and-risks.md)
guards against). One flag does it:

```bash
brew install ollama          # once
./run.local.sh --native-ollama
```

`--native-ollama` uses [`docker-compose.local-native.yaml`](docker-compose.local-native.yaml)
(gateway only) and preflights the host: installs-check, starts the daemon with
`OLLAMA_HOST=0.0.0.0` (so the container can reach it over `host.docker.internal`),
and pulls `$OLLAMA_MODEL`. The gateway then talks to the host Ollama via
`OLLAMA_API_BASE=http://host.docker.internal:11434/v1` (the same
`api_base: os.environ/OLLAMA_API_BASE` knob the container path uses). Everything
else — the `qwen3-coder` alias, conformance, the JSON verdict — is identical; only
where the model runs changes. **Measured:** qwen3:8b ran on Metal at **~28 tok/s**
(vs single-digit tok/s CPU-in-Docker) — same green `agent_capable=true`, inference
in minutes instead of tens of minutes.

The default (no flag) stays fully containerized, so the profile is still portable
and CI-shaped (and on a Linux/NVIDIA box the container path *is* the GPU path).

### The model ladder (what passes, and what doesn't)

Getting a real model green on this stack is a *model-quality* question — the
gateway wiring is identical for all of them. What we measured (CPU-only, Mac):

| Model | Structured `tool_calls`? | Probes (parallel + `tool_choice`) | Drives the multi-turn task? | `agent_capable` |
|---|---|---|---|---|
| **`qwen3:8b`** (default) | ✅ clean (thinking kept out of `content`) | ✅ both honored | ✅ read→edit→test→pass in ~4 turns | **✅ true** |
| `qwen3:4b` | ✅ clean | ✅ both honored | ❌ answers in prose, makes 0 tool calls | ❌ false |
| `qwen2.5-coder:7b` | ❌ leaks (`{"city":"Paris"}` valid args, but unwrapped) | — | — | ❌ false |
| `qwen2.5-coder:3b` | ❌ leaks (`{"city":{"type":"string",…}}` malformed) | — | — | ❌ false |

Two distinct failure modes, both real and both caught by the gate:

1. **qwen2.5-coder leaks (never structures a call).** Its Ollama template requires
   the model to wrap calls in `<tool_call>…</tool_call>` for Ollama's parser to
   populate structured `tool_calls`; the model omits the wrapper, so Ollama returns
   the call unparsed in `content`. Reproduces **direct against Ollama** (native
   `/api/chat` *and* OpenAI-compat `/v1`) and through **both** LiteLLM providers
   (`openai/`, `ollama_chat/`) — so it's the model+engine, not the gateway.
2. **qwen3:4b structures calls but won't *drive* the loop.** It nails direct
   single-turn imperatives (both probes pass) but, on the open-ended task, thinks
   and then answers in *prose* instead of calling tools — despite the system prompt
   forbidding exactly that. A capability gap the 8B doesn't have.

This is the whole point of separating *plumbing* from *model quality* (see
[docs/08](../docs/08-e2e-testing.md)): the profile runs the real gate against a
real backend and returns a trustworthy verdict. `qwen3:8b` earns the green;
swapping `OLLAMA_MODEL` (the reversible knob above) points it at any other model
without touching the profile, config, or conformance wiring.

### Hard constraint: NEVER in CI

Ollama + a multi-GB model + CPU inference is far too heavy for the CI gate.
Nothing wires it there: `e2e/run.sh` (the merge gate) and the CI workflow use the
**mock** profile only, and `docker-compose.local.yaml` is referenced by nothing
but `run.local.sh`. `scripts/check.sh`'s fast tier *does* run `docker compose
config` on this file — but that's pure schema validation, **not** `up`; it starts
no containers. The deliverable is the **profile + docs**, machine-verified by the
mock profile staying green.

### Org data-governance guardrail

The local profile is **offline and keyless** — the model runs entirely on your
machine, nothing leaves it — so it's the *safest* place to poke at real
tool-calling. Keep prompts synthetic anyway (the conformance scenarios already
are). No Foundry, no hosted model, no data egress.

---

## Profile: manual — the live end-to-end demo (real model + fallback + audit) (goal 19)

The single stack that tells the **whole story** at once, driven by hand and
watched: a real client (Claude Code / Codex / curl) → the gateway (SUT) → a
**REAL** small coding model on your **host Ollama** as the primary workbench,
falling back to a **mock Foundry** tier, with the **audit dashboard** rendering
every hop live. It combines what `local` proves (real tool-calling, offline,
keyless) with what `dev` proves (tiered fallback + the routing dashboard) into
one demoable path.

Two files define it — both **committed, both carry no secrets** (master key from
env, Ollama is auth-less, mockd ignores auth):
[`docker-compose.manual.yaml`](docker-compose.manual.yaml) and
[`litellm-config.manual.yaml`](litellm-config.manual.yaml).

> **HARD CONSTRAINT — NEVER in CI, NEVER in `run.sh`.** It needs a host Ollama
> and is meant to be watched, not asserted on. Nothing wires it into the merge
> gate: `e2e/run.sh` and CI use the **mock** profile only. `scripts/check.sh`'s
> fast tier *does* run `docker compose config` on this file (pure schema
> validation — **not** `up`, starts no containers), exactly like the `local`
> profile. That's the only automation it ever touches.

Topology (two containers + your host Ollama):

```
host    Ollama (NATIVE, you run it)   qwen3:8b on the GPU — the primary workbench
:4000   litellm gateway               clients + conformance.py hit this (SUT)
:9103   foundry  (mockd)              the mock fallback tier (claude-*/gpt)
:9300   dashboard                      open it — watch the primary→fallback hop live
```

### Prereq: a host Ollama (native)

The gateway talks to Ollama on the **host** over `host.docker.internal:11434`, so
Ollama must listen on an interface the container VM can reach:

```bash
brew install ollama                    # once (or the Ollama.app / Linux install)
OLLAMA_HOST=0.0.0.0 ollama serve       # daemon on an iface the VM sees
ollama pull qwen3:8b                    # the model the gateway will request
```

Running the model natively (Metal on a Mac, or an NVIDIA host) is why this is the
*fast* real-model path — see "Fast path: native Ollama on the host" under the
`local` profile for the why.

### Bring-up

```bash
cd e2e
docker compose -f docker-compose.manual.yaml up -d
# then open the audit dashboard and drive traffic through :4000:
open http://localhost:9300
../.venv-e2e/bin/python ../conformance/conformance.py \
  --base-url http://localhost:4000/v1 --api anthropic \
  --model qwen3-coder --api-key "${LITELLM_MASTER_KEY:-sk-manual-master-test-key}" --runs 1
```

Every prompt lands on the dashboard as a routing record — normally served by
`qwen3-coder` on your host Ollama (`backend_tier=local`).

### The echo-mode step — make the fallback tier unmistakable

Put the mock Foundry into **echo mode** so anything it serves comes back as a
literal `"mockd echo."` — a dead-giveaway that the *fallback* answered rather
than the real model:

```bash
curl -sX POST http://localhost:9103/__control \
  -d '{"model": "*", "mode": "echo"}'
```

`:9103` is the host-published port of the `foundry` mockd, so you drive its
`/__control` without touching the gateway.

### The kill-Ollama fallback demo

Now stop the primary and watch the gateway fail over to the mock Foundry:

```bash
# 1. kill the host Ollama (Ctrl-C the `ollama serve`, or:)
pkill -f 'ollama serve'

# 2. send a request — qwen3-coder is down, so the gateway falls through the
#    chain (claude-sonnet → claude-opus → gpt), all on the mock Foundry:
curl -s http://localhost:4000/v1/chat/completions \
  -H "Authorization: Bearer ${LITELLM_MASTER_KEY:-sk-manual-master-test-key}" \
  -H 'Content-Type: application/json' \
  -d '{"model": "qwen3-coder", "messages": [{"role": "user", "content": "ping"}]}'
# -> content is "mockd echo." — the fallback tier served it.
```

On the dashboard (`http://localhost:9300`) you'll see two records for that
prompt: a failed `qwen3-coder` attempt (`backend_tier=local`) then a delivered
`claude-sonnet` (`backend_tier=foundry`) — the primary→fallback hop, live. Bring
Ollama back (`OLLAMA_HOST=0.0.0.0 ollama serve`) and, after the cooldown expires,
traffic returns to the real model.

### Teardown

```bash
docker compose -f docker-compose.manual.yaml down -v
```

(and stop/restart your host `ollama serve` as you like — it's not part of the
compose stack).

### Org data-governance guardrail

The model runs on your machine (offline, keyless) and the fallback tier is a
mock — so no data egress. Keep prompts synthetic anyway. No Foundry, no hosted
model. In doubt → **DISCO**.

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
mockd.py                     controllable mock backend (stdlib, no deps; MOCKD_INSTANCE stamps identity) + /__observe sink
obs_callback.py              observability callback: per-request routing records -> stdout + webhook(s) (docs/09)
dashboard.py                 goals 12+13 read-only viewer: routes (/api/records) + fleet (/api/fleet) + record sink (:9300)
dashboard_test.py            stdlib unit tests for the fleet view shaping + graceful-degrade (goal 13, fast tier)
control_plane.py             goal-5 fleet state registry + heartbeat interface (:9400)
control_plane_test.py        stdlib unit tests for the registry state model + wire (goal 5, fast tier)
litellm-config.e2e.yaml      mock-profile gateway config (all aliases -> one mockd)
docker-compose.e2e.yaml      mock stack: litellm + mockd + dashboard + control-plane + postgres
test_e2e.py                  raw-HTTP pytest suite (the CI driver)
run.sh                       up -> test -> conformance gate -> teardown
requirements.txt             test-driver deps (httpx, pytest, openai)
.env.e2e.example             mock-profile env (test-only, safe to commit)

litellm-config.dev.yaml      dev-profile config (workbench-a/-b + foundry, distinct instances; obs -> dashboard)
docker-compose.dev.yaml      dev stack: litellm + 2 workbenches + foundry + dashboard + control-plane + postgres (stays up)
dev_smoke.sh                 dev-profile smoke: all 3 surfaces -> 3 distinct containers + fleet view live

litellm-config.local.yaml    local-profile config (workbench alias -> real qwen3:8b on Ollama; committed, keyless)
docker-compose.local.yaml    local stack: litellm + ollama only (no postgres/mockd); NEVER run in CI
docker-compose.local-native.yaml  local NATIVE variant: gateway only -> host Ollama (Mac GPU fast path)
ollama-entrypoint.sh         ollama container entrypoint: pull + warm the model, healthcheck-gated (self-contained)
run.local.sh                 up -> conformance THROUGH the gateway vs the real model -> teardown (manual, heavy)

litellm-config.cliauth.yaml  cli-auth gateway config (real providers, env-keyed)
docker-compose.cliauth.yaml  cli-auth stack: litellm + postgres (no mockd)
borrow_creds.sh              discover clean API keys -> .env.cliauth (gitignored)
smoke_cli.sh                 opt-in: drive real Claude Code / Codex (fidelity check)
```
