# Goals backlog — the holistic plan

Pick one and run it with the built-in **`/goal`** command. Each goal below has a
**completion condition** written to paste straight in:

```
/goal <paste the completion condition>
```

`/goal` re-checks the condition after every turn and keeps working until it
holds. How it *behaves* while doing so (branch → PR → auto-merge-if-green,
document reversible calls, when to stop) is defined once in
[`CLAUDE.md`](CLAUDE.md) — read that before your first unattended run.

## How to pick

- **On vacation / unattended → only pick from § Autonomy-friendly.** These are
  self-contained and machine-verifiable (`e2e/run.sh` is the arbiter), so a run
  can complete and merge without you.
- **At the keyboard → anything.** The § Needs-a-human goals require real infra,
  external sign-off, or an irreversible design decision that is *yours* to make.
- Respect dependencies (noted per goal). Lower number ≈ higher priority / fewer
  prerequisites.
- Blast radius today is "the repo" — nothing deploys from `main` yet. That's the
  standing assumption behind auto-merge (CLAUDE.md). Revisit when it changes.

Source roadmap: [`docs/02`](docs/02-architecture.md) (phased delivery),
[`docs/06`](docs/06-recommendation.md) (decision), [`docs/03`](docs/03-open-questions-and-risks.md) (risks).

---

## § Autonomy-friendly (safe to run unattended)

### 1. Wire the e2e harness into CI — risk: low
**Why:** `e2e/run.sh` is exit-code clean but nothing runs it automatically; a
regression (like the `tier`→`backend_tier` bug) could sneak back in.
**Completion condition:**
```
a GitHub Actions workflow runs e2e/run.sh and conformance/selftest.py on every PR to main, it passes on this repo's current HEAD, and the change is merged to main
```

### 2. Add a mid-stream-death fallback test + pin retry/stream semantics — risk: low
**Why:** [docs/03 risk 7](docs/03-open-questions-and-risks.md) — a retry that
re-sends a partially-streamed request is a correctness bug; mockd already has a
`hangup` mode to exercise it.
**Completion condition:**
```
e2e/test_e2e.py has a passing test that injects mockd hangup mid-stream and asserts the gateway's behaviour (clean fallback, or a documented reason it can't), the retry/stream semantics per hop are written into docs/03, e2e/run.sh is green, and it's merged to main
```

### 3. Observability & cost attribution — risk: low
**Why:** [docs/03 risk 11](docs/03-open-questions-and-risks.md) — without
per-request {chosen backend, why, latency, tokens, fallback-hit} we can't tune
routing or prove savings.
**Completion condition:**
```
the LiteLLM config captures per-request backend/latency/token/fallback data (logging config or callback), a doc shows how to read it, an e2e assertion proves a fallback is observable in the record, e2e/run.sh is green, and it's merged to main
```

### 4. Add a local-model (Ollama) e2e profile — risk: medium
**Why:** [docs/08 decision 1](docs/08-e2e-testing.md) — the one thing the mock
profile can't give is *real* tool-calling with no keys/ToS. Ollama serving a
small coding model is the closest offline analog to a Spark workbench.
**Completion condition:**
```
e2e has a documented 'local' profile (compose + config) running Ollama with a small coding model as the workbench, conformance.py can be pointed at it, the mock profile still passes e2e/run.sh, and it's merged to main
```
*Note: don't require Ollama to run in CI (too heavy) — the deliverable is the
profile + docs, verified by the mock suite still being green.*

### 5. Phase-1 control-plane skeleton — risk: medium (design-bearing)
**Why:** [docs/06 decision 8](docs/06-recommendation.md) — the one genuinely
novel component. A skeleton is autonomy-friendly; the hard routing *policy*
isn't (see § Needs-a-human).
**Completion condition:**
```
a minimal control-plane service exists (SQLite or Redis + a heartbeat interface) exposing per-model {warm, in_flight, healthy, agent_capable}, it has unit tests that pass, its open design decisions are documented in a new docs file, e2e/run.sh is still green, and it's merged to main
```
*Note: build the registry + state + tests only. Do NOT bake in the routing
policy or session-stickiness rule — those are Needs-a-human decisions.*

---

## § Needs-a-human (do NOT run unattended)

These block on real infra, external sign-off, or an irreversible call. Bring
them up when you're present; several become autonomy-friendly *after* the
decision is made.

- **Real Spark inventory** ([RUNBOOK step 0](deploy/RUNBOOK.md)) — needs actual
  boxes, pinned models, memory headroom, vLLM tool-call parser. Infra.
- **Data-governance sign-off with DISCO** ([docs/03 risk 10](docs/03-open-questions-and-risks.md))
  — is Foundry OK for the intended work; residency/retention. External.
- **Routing granularity decision** ([docs/03 risks 1–2](docs/03-open-questions-and-risks.md))
  — session-only vs allow-one-escalation. Irreversible-ish design call; drives
  the whole router. Decide with a human, *then* the implementation becomes an
  autonomy-friendly goal.
- **LiteLLM-only vs `archgw` evaluation** — architecture fork; research + a call.
- **Verify prompt-caching on the Azure/Anthropic route** ([docs/03 risk 5](docs/03-open-questions-and-risks.md))
  — needs real Foundry creds. Infra.

---

## Done

- ✅ Phase-0 groundwork (blockers A & B, conformance harness, deploy scaffold) — PR #1
- ✅ E2E test harness (mock + cli-auth profiles) — PR #2
