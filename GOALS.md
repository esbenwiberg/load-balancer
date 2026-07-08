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
The "complete the idea" arc (harness core, dev stack, observability, wallet
guards, both dashboard halves, control plane, Ollama profile, Azure IaC) is
**done** — see § Done. The 2026-07-07 status audit opened a new arc:
**attribution + observability refinement** — the dashboard shows *where*
prompts went but not *whose* they were, attempts aren't joined to requests,
and there's no repo/session slicing or TTFT. Recommended order:
identity (15) → repo/session attribution (17) → manual profile (19) →
trace join (16) → TTFT (18); the Fugu-inspired pair slots after — overhead
attribution (20, needs 16) and the shadow complexity spike (21, independent).
Spark-infra-shaped work stays parked.
The keystone § Needs-a-human item remains the **routing-granularity decision**
— it unblocks the actual task-aware router (the control plane is a registry
nobody consumes until that call is made); pair it with the LiteLLM-vs-archgw
fork, at the keyboard.

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

### 15. Identity in routing records — *who* asked? — risk: low
**Why:** `obs_callback.py`'s `async_post_call_success_hook` already receives
`user_api_key_dict` and throws it away, so the dashboard shows where prompts
went but never whose they were. Goal 11b proved key→user→team spend attribution
in Postgres, but the routing-record path (goal 3) and the dashboard (goal 12)
are identity-blind. Guardrail: identities in tests are **synthetic** — key
aliases and user ids like `repo-a`/`test-user`, never real names/emails.
**Completion condition:**
```
delivered routing records carry {key_alias, user_id, team_id} sourced from user_api_key_dict (null when the master key / no key store is in play, so bare-pytest and cli-auth profiles keep working); the dashboard's per-request view shows key/user and gains a per-key rollup (requests, fallbacks, tokens, cost); an e2e test proves a request made with a minted key surfaces that key's alias+user+team in the dashboard's /api/records; synthetic identities only, no PII; e2e/run.sh exits 0 with its passing output surfaced in the conversation; squash-merged to main per CLAUDE.md with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 16. Join the attempt trail to its request — trace correlation — risk: medium
**Why:** the verified LiteLLM quirk ([docs/09](docs/09-observability.md)):
`delivered` records carry no `trace_id`, so the dashboard shows attempts
*alongside* requests instead of nested under them. Debugging "why did THIS
request fall back" needs the join. Prereq: none (independent of 15, but lands
on the same dashboard — coordinate if run concurrently).
**Completion condition:**
```
delivered records carry a correlation id (litellm_call_id/trace_id or a documented equivalent recoverable in async_post_call_success_hook), the dashboard's request rows nest/link their llm_call attempt records by that id, and an e2e test proves a forced fallback's request row is joined to its 503 failure attempt; if LiteLLM 1.83.x genuinely cannot surface a shared id on the fallback-winner path, the goal instead completes by documenting the verified limitation in docs/09 plus the best achievable partial join (direct requests joined by id, fallbacks best-effort) — decide and document per CLAUDE.md, do not fork the gateway; e2e/run.sh exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR
```

### 17. Repo-granularity attribution + session-metadata spike — risk: low
**Why:** the status-audit gap: spend and routing can't be sliced by repo or
session today. Repo granularity falls out of the existing key machinery (11b)
as a *pattern* — one minted key per repo — with zero client hacking. Session
granularity needs facts first: what identity/session metadata do Claude Code
and Codex actually send through the gateway? Capture, don't guess.
**Completion condition:**
```
an e2e test proves the key-per-repo pattern: two keys minted with synthetic aliases (repo-a, repo-b) drive traffic and /key/info + SpendLogs attribute spend to each repo key separately, with the pattern documented in e2e/README; PLUS a written spike (docs/09 section or new doc) recording what identity/session metadata real coding agents send through the gateway — captured from mockd/dev-stack request dumps with synthetic prompts only, covering at least the headers/body fields Claude Code emits — and which LiteLLM tag/metadata mechanism could carry a session id end-to-end; findings only, no client-side changes; e2e/run.sh exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR
```

### 18. TTFT for streamed responses — risk: medium
**Why:** the [docs/09](docs/09-observability.md) caveat: `latency_ms` is
time-to-completion. For agent UX, time-to-first-token is the felt latency, and
workbench-vs-Foundry comparisons are meaningless without it (a slow-TTFT local
model "wins" on paper while feeling dead).
**Completion condition:**
```
llm_call records for streamed responses carry ttft_ms measured from LiteLLM's own timestamps (completion_start_time or a verified equivalent — verified against the pinned 1.83.x, not guessed; if the pinned version exposes no usable timestamp, complete by documenting that finding in docs/09 instead), the dashboard surfaces it, and an e2e test asserts a streamed request's record has ttft_ms present and <= latency_ms; non-streamed records may omit it; the docs/09 streaming caveat is updated; e2e/run.sh exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR
```

### 19. Promote the manual try-out stack to a committed profile — risk: low
**Why:** `e2e/docker-compose.manual.yaml` + `e2e/litellm-config.manual.yaml`
are the project's best live demo (Claude Code → gateway → real host-Ollama vs
mock Foundry, fallback + audit dashboard) and sit untracked — one `git clean`
from gone, walkthrough living in one head. The config states and needs no
secrets (Ollama is auth-less; master key from env) — verify that before commit.
**Completion condition:**
```
the two e2e/*.manual.* files are committed with an e2e/README "Profile: manual" walkthrough (host-Ollama prereq, bring-up, the echo-mode step, the kill-Ollama fallback demo, teardown) after a secrets scan of both files shows no credentials; hard constraint: NEVER wired into CI or run.sh — at most `docker compose config` validation in the fast tier like the local profile; e2e/run.sh (mock) exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked, stop after 20 turns and leave a draft PR
```

### 20. Router-overhead attribution — visible vs consumed tokens — risk: low
**Why:** the Fugu lesson (Sakana's orchestration model, reverse-engineered by
Requesty 2026-06): a request returning ~2,200 visible tokens consumed ~22,700
total — a 10x overhead invisible to the client. Our gateway has the same
failure mode in miniature: retries and failed fallback attempts consume
backend tokens the `delivered` record never rolls up. Today attempts and
requests aren't even joined (goal 16), so per-request true cost is
unanswerable. Prereq: **goal 16** (the attempt↔request join is the substrate).
**Completion condition:**
```
the dashboard's per-request view carries {tokens_delivered, tokens_consumed} where tokens_consumed sums tokens across ALL attempts joined to the request (failed + retried + winner; attempts with no usage reported count 0 and that convention is documented), plus a fleet/summary rollup of overhead (consumed vs delivered) so a silently-expensive routing config is visible at a glance; an e2e test proves a forced-fallback request reports tokens_consumed > tokens_delivered while a clean direct request reports them equal; docs/09 gains an "overhead attribution" note recording the Fugu 10x rationale; e2e/run.sh exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked (including: goal 16 not yet on main), stop after 30 turns and leave a draft PR
```

### 21. Complexity-signal spike — shadow-mode request classifier — risk: low
**Why:** Fugu/TRINITY's core routing lever is a *per-request* complexity gate
(trivial → one cheap worker, hard → escalate); TRINITY does it with a ~0.6B
coordinator. Our routing-granularity decision is parked (Needs-a-human), but
the *telemetry* isn't blocked: tag every routing record with a cheap heuristic
complexity score in shadow mode — zero influence on routing — so when the
human decision lands, the router is designed against real request
distributions instead of guesses. Deliberate anti-Fugu constraints: the
heuristic is deterministic + documented (auditable, unlike Fugu's proprietary
routing), and it must never buffer or delay the request path.
**Completion condition:**
```
routing records gain a shadow complexity tag computed in the existing callback from request features only (documented heuristic over e.g. prompt/message token count, presence and size of tools[], message-turn count — no extra model calls, no added latency path, no influence on routing whatsoever); the dashboard surfaces the tag per request plus a distribution rollup; an e2e test proves a trivial one-line prompt and a tool-heavy multi-turn agentic request land in different buckets; a docs section (docs/09 or docs/10) records the Fugu/TRINITY inspiration, the heuristic's exact features, and how the signal would feed the future task-aware router once the routing-granularity decision is made; no routing behavior changes anywhere; e2e/run.sh exits 0 surfaced; squash-merged with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR
```

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
- **Verify-then-escalate as a routing primitive** — quality-based fallback
  (Fugu/TRINITY's Verifier role): try local first, run a cheap verification
  pass, escalate to Foundry only on failure — vs our current
  availability-based fallback. Design fork with real teeth: what verifies
  (heuristic? judge model? conformance-style probe?), loop/token budget,
  latency floor (Fugu Ultra's is 8–160s — the cautionary tale), and it
  interacts directly with the routing-granularity decision above. Decide
  together with that call; afterwards the implementation is likely an
  autonomy-friendly goal. Hard constraints regardless of outcome: routing
  stays deterministic + auditable, and never buffer the stream behind
  orchestration.
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
- ✅ 3. Observability & cost attribution — per-request routing records via a LiteLLM callback (`e2e/obs_callback.py`): `llm_call` per backend attempt (backend, tier, latency, tokens, and on failure the 503/429 that triggered fallback) + `delivered` per request (requested vs served ⇒ fallback flag, tokens). Sinks: stdout `ROUTING_RECORD <json>` (prod) + webhook to mockd `/__observe` (e2e). `test_fallback_is_observable_in_routing_record` proves a fallback is captured; deploy/ wired for parity; doc [docs/09](docs/09-observability.md). Verified quirk: LiteLLM fallback winner fires no success event — captured via `async_post_call_success_hook`. — PR #19 (2026-07)
- ✅ 11b. Users, teams, and spend audit — per-model costs on `qwen3-coder` so mockd traffic accrues nonzero spend; keys minted bound to a user grouped into a team (`/team/new` → `/team/member_add` → `/key/generate`); `test_spend_attributed_to_key_user_team` proves a `SpendLogs` row + `/key|user|team/info` aggregates attribute spend to the right key+user+team, `test_spend_survives_gateway_restart` restarts the gateway container and proves the Postgres-backed ledger + issued key persist (the open persistence question answered); audit queries + SQL documented in e2e/README "Spend audit" — PR #20 (2026-07)
- ✅ 12. Routing dashboard v1 — "where did my prompt go?" — read-only stdlib dashboard (`e2e/dashboard.py`, `:9300`) over goal-3 routing records: a record **sink** (`POST /records`) + **data endpoint** (`GET /api/records`: per-request view {requested→served, fallback, tier, latency, tokens} + attempt trail) + read-only **page** (`GET /`). Build-vs-reuse fork decided **BUILD** (not LiteLLM's admin UI): the record shape is ours, an owned JSON endpoint is assertable, keeps the zero-dep floor, read-only, reversible — documented in `dashboard.py` header + [docs/09](docs/09-observability.md). `obs_callback` gained comma-separated multi-sink fan-out so e2e feeds both `mockd/__observe` (goal-3 suite unchanged) and the dashboard. Wired into e2e + dev stacks (dev had **no** obs wiring before this). `test_dashboard_data_endpoint_shows_direct_request` / `_shows_fallback_route` / `_page_renders`. — PR #21 (2026-07)
- ✅ 5. Phase-1 control-plane skeleton — stdlib SQLite-backed fleet registry + heartbeat interface (`e2e/control_plane.py`, `:9400`) exposing per-model `{warm, in_flight, healthy, agent_capable}`; `healthy` is DERIVED (reported-healthy AND heartbeat-fresh, TTL decay) so a silent workbench self-decays; push heartbeats + full-snapshot upsert per (workbench,model); aggregates across workbenches (summed in_flight, any-healthy agent_capable). Registry+state+tests ONLY — routing policy + session-stickiness deliberately NOT built (Needs-a-human). 19 stdlib `unittest` tests (`e2e/control_plane_test.py`: Registry state model w/ injected clock + HTTP wire) wired into check.sh fast tier; standing `control-plane` service added to dev compose (empty registry; goal 13 wires producers). Decisions + open questions in [docs/10](docs/10-control-plane.md) — PR #22 (2026-07)
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
  4 (live fleet). Docs: [docs/09](docs/09-observability.md), [docs/10](docs/10-control-plane.md). — PR #23 (2026-07)
- ✅ 11. Budgets + rate limits per virtual key — `default_key_generate_params` (max_budget/rpm/tpm) in litellm-config.e2e.yaml so every issued key inherits a config default; e2e proves a bare key inherits the defaults, an over-budget key (max_budget:0) → clean 400 budget_exceeded, an over-rate-limit key (rpm:1) → clean 429 (never 5xx/hang); README documents the knobs + how to raise them + the goal-11/11b units boundary (dollar-spend accrual is 11b) — PR #18 (2026-07)
- ✅ 4. Local-model (Ollama) e2e profile — a dedicated `local` profile
  (`docker-compose.local.yaml` + committed keyless `litellm-config.local.yaml`)
  running Ollama serving a real small coding model as the workbench behind the
  same gateway; self-contained bring-up (`ollama-entrypoint.sh` pulls+warms the
  model, healthcheck-gated so the gateway never boots against an empty Ollama);
  `run.local.sh` runs `conformance.py` THROUGH the gateway against the real model
  and surfaces the JSON verdict (alias stays `qwen3-coder` — only the backend
  swaps; `OLLAMA_MODEL` drives both the pull and what the gateway requests).
  Hard constraint honoured: NEVER in CI — `run.sh`/CI use the mock profile only,
  `docker compose config` validates the file in the fast tier but starts no
  containers. Docs: e2e/README "Profile: local" + docs/08. Merge gate = mock
  `e2e/run.sh` green. — PR #25 (2026-07)
- ✅ 4 (follow-up 2). Local profile GPU fast path — `run.local.sh --native-ollama`
  keeps the gateway containerized (prod parity + pinned image) but points it at a
  **host-run Ollama** on the Mac's Metal GPU via `OLLAMA_API_BASE` (new env knob:
  `api_base: os.environ/OLLAMA_API_BASE`; `docker-compose.local-native.yaml` =
  gateway-only, no Ollama container). Runner preflights the host (install-check,
  starts the daemon on `0.0.0.0`, pulls the model). Verified end-to-end: qwen3:8b
  on Metal (`size_vram=5.6GB`), `agent_capable=true`, ~28 tok/s vs single-digit
  CPU-in-Docker. Fully-containerized default unchanged (portable + CI; the GPU
  path on a Linux/NVIDIA host). Docs: e2e/README "Fast path" + docs/08. — PR #27 (2026-07)
- ✅ 4 (follow-up). Local profile made GREEN — default model → `qwen3:8b`, which
  clears the conformance gate for real (`agent_capable=true`): structured tool
  calls, the full multi-turn Read→Edit→Bash task, and both probes, over streaming.
  Established the model ladder (documented in e2e/README + docs/08): `qwen3:8b`
  passes; `qwen3:4b` structures calls + passes probes but won't drive the loop
  (answers in prose); `qwen2.5-coder:3b/:7b` leak tool calls (no `<tool_call>`
  wrapper). Slow CPU-only (reasoning mode) but never a CI/merge gate. — PR #26 (2026-07)
- ✅ 14. Azure IaC skeleton — code only, offline-validated — Bicep (decision recorded over Terraform: `bicep build` validates fully offline; Azure-native; stateless) under `deploy/azure/`: `main.bicep` + modules for the gateway Container App (managed identity, Key Vault–referenced secrets, parameterised ingress), PostgreSQL Flexible Server (private persistent store), Key Vault (secrets + MI RBAC), and VNet (delegated subnets + NSG). Secrets are required `@secure()` params with no defaults; `main.example.bicepparam` carries commit-safe placeholders. `scripts/check.sh` fast tier gained an offline `bicep build`/`build-params` step (fails on ANY diagnostic, no cloud calls/creds), CI installs bicep via `az bicep install`, and the litellm image-pin guard now also covers `.bicep`. Parity doc [docs/11](docs/11-azure-iac.md) maps every dev-stack component to its Azure counterpart (with the deliberate gaps named). — PR #24 (2026-07)
