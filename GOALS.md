# Goals backlog — the holistic plan

## The endgame (north star)

One endpoint any coding agent can `/connect` to — Claude Code, Codex, generic
OpenAI clients — hosted in **Azure**, fronting **Foundry** plus a fleet of
workbenches (mock ones first: mockd, or haiku-backed via cli-auth; real Sparks
later). A **dashboard** on the balancer shows where every prompt was routed and
why, which workbenches are subscribed with which models, and their live load.
The same stack runs **locally in one command** — gateway in dev mode + mock
workbenches + mock Foundry — so the agents building it can self-validate every
change without real infra. That local stack *is* the harness: goals 10, 3, 12.

## How to start a goal

Every completion condition below is a **self-contained `/goal` payload**:
constraints, proof requirements, and a stop bound live inside the block, so a
run needs nothing else. Two ways to kick one off:

**Morning one-liner** (recommended — only the number changes):

```
/goal read GOALS.md, quote goal <N>'s completion condition verbatim into the conversation, then work until that quoted condition literally holds — it carries its own constraints, proof requirements, and stop bound
```

**Or paste the block** — copy the goal's completion condition into `/goal`
as-is.

`/goal` re-checks the condition after every turn using a small fast model that
sees only the conversation — it runs no commands and reads no files. That is
why every condition demands proof be *surfaced*: exit codes, gate output, and
merge confirmations must appear in the transcript. How to behave while working
(branch → PR → auto-merge-if-green, document reversible calls, when to stop)
is defined once in [`CLAUDE.md`](CLAUDE.md) — read it before your first
unattended run.

## How to pick

- **On vacation / unattended → only pick from § Autonomy-friendly.** These are
  self-contained and machine-verifiable (`e2e/run.sh` is the arbiter), so a run
  can complete and merge without you.
- **At the keyboard → anything.** The § Needs-a-human goals require real infra,
  external sign-off, or an irreversible design decision that is *yours* to make.
- Respect prerequisites (stated inside the conditions). Lower number ≈ higher
  priority / fewer prerequisites — but see **Current focus** below, which
  overrides raw numbering.
- Blast radius today is "the repo" — nothing deploys from `main` yet. That's the
  standing assumption behind auto-merge (CLAUDE.md). Revisit when it changes.

**Current focus (2026-07):** we are **not** taking in Spark workbenches yet.
Priority is (a) completing the idea — control plane, observability, dashboard —
and (b) hardening the harness + test setup. Recommended order:
harness core (0 → 1 → 2 → 6 → 7 → 9) → dev stack (10) → observability +
wallet guards (3 ✅ → 11b ✅) → dashboard v1 (12 ✅) → Ollama (4 ✅) → control plane
(5 ✅) → dashboard v2 (13 ✅) → Azure IaC (14 ✅). Spark-infra-shaped work is parked.
**All autonomy-friendly goals are now done.** The control-plane, both dashboard
halves (12, 13), the Azure IaC skeleton (14), and the Ollama local profile (4)
are complete — the "complete the idea" arc is closed, the local↔cloud parity
story is pinned, and the harness now has real keyless tool-calling. What's left
is § Needs-a-human (real infra / an irreversible call): pick those at the
keyboard.

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

_All current autonomy-friendly goals are done. The next vetted units live in
§ Needs-a-human (they require a human decision or real infra); pick one at the
keyboard, or add a new autonomy-friendly goal here when you spot one._

---

## § Needs-a-human (do NOT run unattended)

These block on real infra, external sign-off, or an irreversible call. Bring
them up when you're present; several become autonomy-friendly *after* the
decision is made.

- **Real Spark inventory** ([RUNBOOK step 0](deploy/RUNBOOK.md)) — ⏸ **parked**:
  we're not taking in Spark workbenches yet (see Current focus). Needs actual
  boxes, pinned models, memory headroom, vLLM tool-call parser. Infra.
- **Data-governance sign-off with DISCO** ([docs/03 risk 10](docs/03-open-questions-and-risks.md))
  — is Foundry OK for the intended work; residency/retention. External.
- **Routing granularity decision** ([docs/03 risks 1–2](docs/03-open-questions-and-risks.md))
  — session-only vs allow-one-escalation. Irreversible-ish design call; drives
  the whole router. Decide with a human, *then* the implementation becomes an
  autonomy-friendly goal.
- **LiteLLM-only vs `archgw` evaluation** — architecture fork; research + a call.
- **First Azure deploy + exposure model** (after goal 14) — subscription/resource
  choices, private endpoint vs public + IP allowlist, TLS, dashboard auth, who
  gets keys and how they rotate. A hosted OpenAI-compatible proxy with Foundry
  creds behind it is a *target*; a leaked master key is someone else's free LLM.
  Creds + security + outward-facing. *(Noted for later — build phase is
  local/test only, nothing hosted yet. Decide the release model first ⤵)*
- **Release model BEFORE anything deploys from `main`** — the moment the
  balancer deploys from `main`, CLAUDE.md's tripwire kills auto-merge and
  vacation autonomy with it. Decide *in advance*: manual promotion, a release
  branch, or tagged deploys. *(Decision 2026-07: fine as-is for now — nothing
  deploys, it's all testing. Revisit at the first real deploy, not before.)*
- **Real-Foundry traffic through the hosted balancer** — *(build phase: nothing
  is live and nothing routes to Foundry during unattended runs — mock/synthetic
  only, per the CLAUDE.md guardrail.)* When the balancer goes live for real
  prompts, the DISCO sign-off above gates it.
- **Verify prompt-caching on the Azure/Anthropic route** ([docs/03 risk 5](docs/03-open-questions-and-risks.md))
  — needs real Foundry creds. Infra.

---

## Done

**How to mark a goal complete** (do this in the same PR that finishes it):
1. *Delete* the goal's entry from its section above — don't leave a tombstone.
2. Add one line here: `- ✅ <goal number + title> — PR #<n> (<yyyy-mm>)`, plus
   any follow-up goals discovered, added above as new numbered entries.
3. The completion condition itself is the tag: a goal is "complete" only when
   its condition literally holds on `main` — if in doubt, re-check it, don't
   trust the checkmark.

- ✅ Phase-0 groundwork (blockers A & B, conformance harness, deploy scaffold) — PR #1
- ✅ E2E test harness (mock + cli-auth profiles) — PR #2
- ✅ Goal-driven workflow (GOALS.md backlog + unattended contract) — PR #3
- ✅ 0. One check script + githooks + agent self-validation — PR #7 (2026-07)
- ✅ 1. Wire the e2e harness into CI — PR #9 (2026-07)
- ✅ 2. Mid-stream-death fallback test + pinned retry/stream semantics (risk 7) — PR #11 (2026-07)
- ✅ 6. mockd fault modes (429, transient 5xx, malformed tool-call) + pinned retry-before-fallback order + fixed cross-test cooldown flake — PR #13 (2026-07)
- ✅ 7. Tool-calling coverage on the Anthropic surface (`conformance.py --api anthropic`: /v1/messages tools+streaming, full read→edit→bash round-trip + probes, wired into run.sh) — PR #14 (2026-07)
- ✅ 8. Harness self-checks + guardrail automation (mockd-direct conformance step in run.sh; negative-path e2e tests — malformed JSON + unknown alias → clean 4xx on all three surfaces; LiteLLM image-pin guard in check.sh enforced by pre-commit/Stop/CI) — PR #15 (2026-07)
- ✅ 9. Concurrency smoke — parallel streams across 4 aliases with a 503-fault forcing a fallback mid-fleet; asserts each response keeps its own served_model stamp (no cross-request bleed) + clean stream termination, plus a mixed chat/responses/messages variant for interleaved-SSE-across-protocols — PR #16 (2026-07)
- ✅ 10. Dev-mode stack — standing dev profile (docker-compose.dev.yaml): gateway + two distinct mock workbench containers (workbench-a/-b) + mock-foundry, each stamping served_model=<model>@<instance>; dev_smoke.sh proves all 3 surfaces (messages/chat/responses) route to distinct containers; per-instance /__control faults; README wires Claude Code + Codex; real-haiku variant documented but keyless-offline stays default — PR #17 (2026-07)
- ✅ 3. Observability & cost attribution — per-request routing records via a LiteLLM callback (`e2e/obs_callback.py`): `llm_call` per backend attempt (backend, tier, latency, tokens, and on failure the 503/429 that triggered fallback) + `delivered` per request (requested vs served ⇒ fallback flag, tokens). Sinks: stdout `ROUTING_RECORD <json>` (prod) + webhook to mockd `/__observe` (e2e). `test_fallback_is_observable_in_routing_record` proves a fallback is captured; deploy/ wired for parity; doc [docs/09](docs/09-observability.md). Verified quirk: LiteLLM fallback winner fires no success event — captured via `async_post_call_success_hook`. — PR #? (2026-07)
- ✅ 11b. Users, teams, and spend audit — per-model costs on `qwen3-coder` so mockd traffic accrues nonzero spend; keys minted bound to a user grouped into a team (`/team/new` → `/team/member_add` → `/key/generate`); `test_spend_attributed_to_key_user_team` proves a `SpendLogs` row + `/key|user|team/info` aggregates attribute spend to the right key+user+team, `test_spend_survives_gateway_restart` restarts the gateway container and proves the Postgres-backed ledger + issued key persist (the open persistence question answered); audit queries + SQL documented in e2e/README "Spend audit" — PR #? (2026-07)
- ✅ 12. Routing dashboard v1 — "where did my prompt go?" — read-only stdlib dashboard (`e2e/dashboard.py`, `:9300`) over goal-3 routing records: a record **sink** (`POST /records`) + **data endpoint** (`GET /api/records`: per-request view {requested→served, fallback, tier, latency, tokens} + attempt trail) + read-only **page** (`GET /`). Build-vs-reuse fork decided **BUILD** (not LiteLLM's admin UI): the record shape is ours, an owned JSON endpoint is assertable, keeps the zero-dep floor, read-only, reversible — documented in `dashboard.py` header + [docs/09](docs/09-observability.md). `obs_callback` gained comma-separated multi-sink fan-out so e2e feeds both `mockd/__observe` (goal-3 suite unchanged) and the dashboard. Wired into e2e + dev stacks (dev had **no** obs wiring before this). `test_dashboard_data_endpoint_shows_direct_request` / `_shows_fallback_route` / `_page_renders`. — PR #21 (2026-07)
- ✅ 5. Phase-1 control-plane skeleton — stdlib SQLite-backed fleet registry + heartbeat interface (`e2e/control_plane.py`, `:9400`) exposing per-model `{warm, in_flight, healthy, agent_capable}`; `healthy` is DERIVED (reported-healthy AND heartbeat-fresh, TTL decay) so a silent workbench self-decays; push heartbeats + full-snapshot upsert per (workbench,model); aggregates across workbenches (summed in_flight, any-healthy agent_capable). Registry+state+tests ONLY — routing policy + session-stickiness deliberately NOT built (Needs-a-human). 19 stdlib `unittest` tests (`e2e/control_plane_test.py`: Registry state model w/ injected clock + HTTP wire) wired into check.sh fast tier; standing `control-plane` service added to dev compose (empty registry; goal 13 wires producers). Decisions + open questions in [docs/10](docs/10-control-plane.md) — PR #? (2026-07)
- ✅ 13. Fleet dashboard v2 — who's subscribed, with what, under what load — the
  dashboard reads the goal-5 control-plane registry via its own owned `/api/fleet`
  endpoint (server-side proxy of `/models`; reversible call — owned+assertable,
  no CORS, degrades to `available:false`) and renders a Fleet section: per-model
  {warm, healthy/total, in-flight, agent} + a per-workbench instance table with
  derived health. `control-plane` added to the e2e stack; mockd gained an
  optional heartbeat producer (gated on `HEARTBEAT_URL`) + a live in-flight
  counter, wired in the **dev** stack so each workbench beats real state (e2e
  keeps the test as the sole deterministic producer). Assertions:
  `test_dashboard_fleet_reflects_control_plane_registry` +
  `test_dashboard_fleet_surfaces_derived_health` (registry→dashboard path),
  `dashboard_test.py` (offline shaping + degrade, fast tier), `dev_smoke.sh` step
  4 (live fleet). Docs: [docs/09](docs/09-observability.md), [docs/10](docs/10-control-plane.md). — PR #? (2026-07)
- ✅ 11. Budgets + rate limits per virtual key — `default_key_generate_params` (max_budget/rpm/tpm) in litellm-config.e2e.yaml so every issued key inherits a config default; e2e proves a bare key inherits the defaults, an over-budget key (max_budget:0) → clean 400 budget_exceeded, an over-rate-limit key (rpm:1) → clean 429 (never 5xx/hang); README documents the knobs + how to raise them + the goal-11/11b units boundary (dollar-spend accrual is 11b) — PR #? (2026-07)
- ✅ 4. Local-model (Ollama) e2e profile — a dedicated `local` profile
  (`docker-compose.local.yaml` + committed keyless `litellm-config.local.yaml`)
  running Ollama serving `qwen2.5-coder:3b` as the workbench behind the same
  gateway; self-contained bring-up (`ollama-entrypoint.sh` pulls+warms the model,
  healthcheck-gated so the gateway never boots against an empty Ollama);
  `run.local.sh` runs `conformance.py` THROUGH the gateway against the real model
  and surfaces the JSON verdict (alias stays `qwen3-coder` — only the backend
  swaps; `OLLAMA_MODEL` drives both the pull and what the gateway requests).
  **Finding (surfaced, not hidden):** the gate returned `agent_capable=false` —
  qwen2.5-coder (3b *and* 7b) leaks tool calls into content instead of emitting
  structured `tool_calls` because it omits the `<tool_call>` wrapper Ollama's
  template needs (reproduces direct-against-Ollama + both LiteLLM providers, so
  it's the model+engine, not the gateway). That's the gate *working* against a
  real backend — the profile's job. A green is a one-env-var model swap. Hard
  constraint honoured: NEVER in CI — `run.sh`/CI use the mock profile only,
  `docker compose config` validates the file in the fast tier but starts no
  containers. Docs: e2e/README "Profile: local" + docs/08. Merge gate = mock
  `e2e/run.sh` green. — PR #? (2026-07)
- ✅ 14. Azure IaC skeleton — code only, offline-validated — Bicep (decision recorded over Terraform: `bicep build` validates fully offline; Azure-native; stateless) under `deploy/azure/`: `main.bicep` + modules for the gateway Container App (managed identity, Key Vault–referenced secrets, parameterised ingress), PostgreSQL Flexible Server (private persistent store), Key Vault (secrets + MI RBAC), and VNet (delegated subnets + NSG). Secrets are required `@secure()` params with no defaults; `main.example.bicepparam` carries commit-safe placeholders. `scripts/check.sh` fast tier gained an offline `bicep build`/`build-params` step (fails on ANY diagnostic, no cloud calls/creds), CI installs bicep via `az bicep install`, and the litellm image-pin guard now also covers `.bicep`. Parity doc [docs/11](docs/11-azure-iac.md) maps every dev-stack component to its Azure counterpart (with the deliberate gaps named). — PR #? (2026-07)
