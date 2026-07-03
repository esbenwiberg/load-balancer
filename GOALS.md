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
harness core (0 → 1 → 2 → 6 → 7 → 8 → 9) → dev stack (10) → observability +
wallet guards (3 → 11 → 11b) → dashboard v1 (12) → Ollama (4) → control plane
(5) → dashboard v2 (13) → Azure IaC (14). Spark-infra-shaped work is parked.

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

### 0. One check script + githooks + agent self-validation — risk: low
**Why:** today the only arbiter is `e2e/run.sh`, which costs a full docker
stack — there is nothing between "save file" and a multi-minute e2e run, so
agents get zero cheap feedback and the no-secrets guardrail is enforced by
vibes. One `scripts/check.sh` that hooks, CI (goal 1), and agents all call
means the definition of "green" can never drift between them.
**Completion condition:**
```
scripts/check.sh exists with a --fast tier (ruff lint+format, shellcheck, docker compose config validation for all compose files, conformance/selftest.py, gitleaks secret scan — starting no docker containers) and a full tier that adds e2e/run.sh; a checked-in .githooks/pre-commit runs the fast tier via a scripts/setup-dev.sh that sets core.hooksPath and reports missing tools; a Claude Code Stop hook in .claude/settings.json runs the fast tier; missing tools warn-and-skip locally but the script hard-fails on real findings — demonstrate this by running the fast tier once with one tool absent from PATH and surfacing the warn-and-skip output in the conversation; hard constraint: NO docker containers in any git hook — full e2e stays the merge gate (CI, goal 1), never the commit gate, because slow hooks train everyone to --no-verify; both tiers exit 0 on the repo's current HEAD with their exit status surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR describing the decision needed
```

### 1. Wire the e2e harness into CI — risk: low
**Why:** `e2e/run.sh` is exit-code clean but nothing runs it automatically; a
regression (like the `tier`→`backend_tier` bug) could sneak back in.
**Completion condition:**
```
a GitHub Actions workflow runs e2e/run.sh and conformance/selftest.py on every PR to main — calling scripts/check.sh if it exists on main rather than duplicating its steps; the workflow passes on this repo's current HEAD, proven by surfacing the green check run (gh pr checks or gh run view output) in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 2. Add a mid-stream-death fallback test + pin retry/stream semantics — risk: low
**Why:** [docs/03 risk 7](docs/03-open-questions-and-risks.md) — a retry that
re-sends a partially-streamed request is a correctness bug; mockd already has a
`hangup` mode to exercise it.
**Completion condition:**
```
e2e/test_e2e.py has a passing test that injects mockd hangup mid-stream and asserts the gateway's behaviour (clean fallback, or a documented reason it can't); the observed retry/stream semantics per hop are written into docs/03-open-questions-and-risks.md; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

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

### 6. Exercise mockd's remaining fault modes + pin retry-vs-fallback order — risk: low
**Why:** mockd can already inject 429, latency, count-limited transient faults,
and malformed tool calls ([e2e/mockd.py](e2e/mockd.py)) — but `test_e2e.py`
only ever uses a persistent 503. Worse, LiteLLM's retry-before-fallback order
is unpinned: a config change could silently turn one backend fault into N
duplicate upstream requests and nothing would catch it.
**Completion condition:**
```
e2e/test_e2e.py has passing tests for (a) 429 -> fallback/cooldown behaviour, (b) a count-limited transient 5xx that documents whether LiteLLM retries the same backend before advancing the fallback chain, and (c) a malformed tool-call surfaced through the Responses bridge; the observed retry-vs-fallback order is written into docs/03-open-questions-and-risks.md; this overlaps goal 2 (hangup) and may share a PR with it, but this condition is judged on its own clauses; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 7. Tool-calling coverage on the Anthropic surface — risk: medium
**Why:** Claude Code's real path is `/v1/messages` **with tools**, streaming.
e2e only proves plain-text translation there; the conformance harness speaks
`chat` and `responses` but has no `anthropic` transport. Our single biggest
client path has no tool-call gate at all.
**Completion condition:**
```
conformance.py gains an --api anthropic transport (or e2e gains an equivalent full read->edit->bash tool round-trip over streaming /v1/messages); it passes through the gateway against mockd and run.sh executes it as part of the suite; constraint: mockd needs no changes — the gateway translates anthropic->chat toward the backend, so the new transport targets the gateway's /v1/messages; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 40 turns and leave a draft PR describing the decision needed
```

### 8. Harness self-checks + guardrail automation — risk: low
**Why:** run.sh only tests *through* the gateway, so a mockd regression is
indistinguishable from a gateway regression. And the LiteLLM digest pin
([docs/03 risk 8](docs/03-open-questions-and-risks.md) — the malware one) is a
hard guardrail enforced only by eyeball.
**Completion condition:**
```
run.sh gains a mockd-direct conformance step (isolating mockd regressions from gateway regressions); e2e gains negative-path tests (malformed JSON body and unknown model alias -> clean 4xx, no hang); CI fails if the LiteLLM image tag/digest deviates from the vetted pin; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 9. Concurrency smoke — parallel streams must not cross-talk — risk: low
**Why:** every e2e test runs serially, but the gateway's whole job is serving
concurrent agents. A cross-request bleed (wrong `served_model` stamp,
interleaved SSE chunks) would be catastrophic and is currently invisible.
**Completion condition:**
```
an e2e test fires concurrent streaming requests across different model aliases with a fault injected on one of them, and asserts every response carries the correct served_model stamp and terminates its stream cleanly; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
```

### 10. Dev-mode stack — the self-validation fleet — risk: low
**Why:** the endgame's harness. Agents building features must be able to spin
up the full topology locally — gateway + **two separate mock workbench
containers** + a **mock-Foundry container** — leave it running, and point a
real client at it. Today the e2e compose runs ONE mockd serving every alias:
you can't tell instances apart, exercise per-instance load/faults, or use it
as a standing dev fixture.
**Completion condition:**
```
a documented dev profile brings up the gateway plus two distinct mock workbench containers and a mock-foundry container (each stamping its own instance identity in served_model), staying up until explicitly torn down; a smoke script proves all three client surfaces (anthropic messages, chat completions, responses) route through it, with its passing output surfaced in the conversation; the README documents how to point Claude Code and Codex at it (base url + key); constraint: a variant where one workbench slot is backed by real haiku via the existing cli-auth borrow (e2e/borrow_creds.sh) is documented but is NOT the default — the default stays keyless and offline; e2e/run.sh exits 0 with its passing output surfaced; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 50 turns and leave a draft PR describing the decision needed
```

### 11. Budgets + rate limits per virtual key — vacation-proof the wallet — risk: low
**Why:** unattended goal runs + (eventually) a hosted endpoint = runaway-spend
risk, and the whole point is burning *subscription*, not invoice. LiteLLM
supports `max_budget` / tpm / rpm per key; nothing configures or tests it.
**Completion condition:**
```
every virtual key the gateway issues gets a default budget and rate limit from config; an e2e test proves an over-budget key and an over-limit key are refused with a clean 4xx (no hang, no 5xx); the knobs and how to raise them are documented; e2e/run.sh exits 0 with its passing output surfaced in the conversation; the change is squash-merged to main per CLAUDE.md's contract with the merge confirmation surfaced; if blocked, stop after 30 turns and leave a draft PR describing the decision needed
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
