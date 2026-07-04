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
wallet guards (3 → 11b) → dashboard v1 (12) → Ollama (4) → control plane
(5) → dashboard v2 (13) → Azure IaC (14). Spark-infra-shaped work is parked.

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

### 3. Observability & cost attribution — risk: low
**Why:** [docs/03 risk 11](docs/03-open-questions-and-risks.md) — without
per-request {chosen backend, why, latency, tokens, fallback-hit} we can't tune
routing or prove savings.
**Completion condition:**
```
the LiteLLM config captures per-request backend/latency/token/fallback data (logging config or callback); a doc shows how to read it; an e2e assertion proves a fallback is observable in the record; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 4. Add a local-model (Ollama) e2e profile — risk: medium
**Why:** [docs/08 decision 1](docs/08-e2e-testing.md) — the one thing the mock
profile can't give is *real* tool-calling with no keys/ToS. With Spark intake
parked, Ollama serving a small coding model isn't just the closest analog to a
workbench — it's the stand-in until real Sparks arrive.
**Completion condition:**
```
e2e has a documented 'local' profile (compose + config) running Ollama with a small coding model as the workbench, and conformance.py can be pointed at it; hard constraint: Ollama must NOT run in CI (too heavy) — the deliverable is the profile + docs, machine-verified by the mock profile still passing; e2e/run.sh (mock profile) exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR describing the decision needed
```

### 5. Phase-1 control-plane skeleton — risk: medium (design-bearing)
**Why:** [docs/06 decision 8](docs/06-recommendation.md) — the one genuinely
novel component. A skeleton is autonomy-friendly; the hard routing *policy*
isn't (see § Needs-a-human).
**Completion condition:**
```
a minimal control-plane service exists (SQLite or Redis + a heartbeat interface) exposing per-model {warm, in_flight, healthy, agent_capable}; it has unit tests that pass, with the passing output surfaced in the conversation; its open design decisions are documented in a new docs file; hard constraint: build the registry + state + tests ONLY — do NOT implement the routing policy or session-stickiness rule, those are Needs-a-human decisions; e2e/run.sh exits 0 with its passing output surfaced; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR describing the decision needed
```

### 11b. Users, teams, and spend audit — who spent what — risk: medium
**Why:** budgets (goal 11) cap the damage; this makes spend *attributable*:
every key belongs to a user, users group into teams, and spend is queryable
per key/user/team after the fact. LiteLLM has all of it natively (internal
users, teams, spend logs) — but it needs a real Postgres behind the gateway,
which today runs stateless. That's also where the open persistence question
(do keys survive a restart?) gets answered for good.
**Completion condition:**
```
prerequisite: goal 10's dev profile must already be merged to main — if it is not, stop immediately and report that instead of building the stack ad hoc; the dev/e2e stack includes a Postgres the gateway uses; keys are issued bound to a user and users can be grouped into teams; per-model costs are configured so mockd traffic produces nonzero spend; an e2e test proves spend for a request is attributed to the right key+user+team and survives a gateway restart; the audit queries are documented; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 50 turns and leave a draft PR describing the decision needed
```

### 12. Routing dashboard v1 — "where did my prompt go?" — risk: medium
**Why:** the endgame's visible face: per-request {alias asked, backend served,
fallback hit?, latency, tokens} you can actually look at. Build-vs-reuse is a
real fork (LiteLLM ships an admin UI; a thin read-only page over goal-3 data
may serve better) — it's *reversible*, so decide and document per CLAUDE.md.
**Completion condition:**
```
prerequisite: goals 3 and 10 must already be merged to main — if either is not, stop immediately and report that; with the dev stack up, a dashboard (LiteLLM's UI configured, or a small read-only page — a reversible build-vs-reuse call: decide it and document the reasons) shows per-request routing records for prompts just sent through the gateway; an e2e assertion covers the data endpoint feeding the dashboard; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 50 turns and leave a draft PR describing the decision needed
```

### 13. Fleet dashboard v2 — who's subscribed, with what, under what load — risk: medium
**Why:** the other half of the vision: which workbenches are registered, which
models they carry, warm/healthy/in-flight right now. Reads from the
control-plane skeleton — the *display* is autonomy-friendly even though the
routing policy behind it isn't.
**Completion condition:**
```
prerequisite: goals 5 and 12 must already be merged to main — if either is not, stop immediately and report that; the dashboard shows the control-plane registry live for the dev stack (per-workbench models, health, in-flight/load); an assertion covers the registry-to-dashboard data path; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 50 turns and leave a draft PR describing the decision needed
```

### 14. Azure IaC skeleton — code only, no deploy — risk: medium
**Why:** the balancer must end up Azure-hosted, but a real deploy needs creds
and a network-exposure decision (§ Needs-a-human). The IaC itself is just
code: author it, validate it offline, and pin local↔cloud parity so the dev
stack stays a faithful miniature.
**Completion condition:**
```
IaC (bicep or terraform — a reversible call: decide it and document the reasons) describes the gateway container, its persistent store, key-vault wiring for secrets, and networking parameters; it validates in CI with offline tooling only (bicep build / terraform validate — no cloud calls, no deploy, no credentials); a parity doc maps every dev-stack component to its Azure counterpart; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR describing the decision needed
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
- ✅ 11. Budgets + rate limits per virtual key — `default_key_generate_params` (max_budget/rpm/tpm) in litellm-config.e2e.yaml so every issued key inherits a config default; e2e proves a bare key inherits the defaults, an over-budget key (max_budget:0) → clean 400 budget_exceeded, an over-rate-limit key (rpm:1) → clean 429 (never 5xx/hang); README documents the knobs + how to raise them + the goal-11/11b units boundary (dollar-spend accrual is 11b) — PR #? (2026-07)
