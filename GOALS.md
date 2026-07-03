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
  prerequisites — but see **Current focus** below, which overrides raw numbering.
- Blast radius today is "the repo" — nothing deploys from `main` yet. That's the
  standing assumption behind auto-merge (CLAUDE.md). Revisit when it changes.

**Current focus (2026-07):** we are **not** taking in Spark workbenches yet.
Priority is (a) completing the idea — control plane, observability — and
(b) hardening the harness + test setup. Recommended order:
1 → 2 → 6 → 7 → 8 → 3 → 9 → 4 → 5. Anything Spark-infra-shaped is parked.

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
profile can't give is *real* tool-calling with no keys/ToS. With Spark intake
parked, Ollama serving a small coding model isn't just the closest analog to a
workbench — it's the stand-in until real Sparks arrive.
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

### 6. Exercise mockd's remaining fault modes + pin retry-vs-fallback order — risk: low
**Why:** mockd can already inject 429, latency, count-limited transient faults,
and malformed tool calls ([e2e/mockd.py](e2e/mockd.py)) — but `test_e2e.py`
only ever uses a persistent 503. Worse, LiteLLM's retry-before-fallback order
is unpinned: a config change could silently turn one backend fault into N
duplicate upstream requests and nothing would catch it.
**Completion condition:**
```
e2e/test_e2e.py has passing tests for (a) 429 -> fallback/cooldown behaviour, (b) a count-limited transient 5xx that documents whether LiteLLM retries the same backend before advancing the fallback chain, and (c) a malformed tool-call surfaced through the Responses bridge; the observed retry/fallback order is written into docs/03; e2e/run.sh is green; and it's merged to main
```
*Note: overlaps goal 2 (hangup) — fine to tackle together in one PR, but the
conditions are checked independently.*

### 7. Tool-calling coverage on the Anthropic surface — risk: medium
**Why:** Claude Code's real path is `/v1/messages` **with tools**, streaming.
e2e only proves plain-text translation there; the conformance harness speaks
`chat` and `responses` but has no `anthropic` transport. Our single biggest
client path has no tool-call gate at all.
**Completion condition:**
```
conformance.py gains an --api anthropic transport (or e2e gains an equivalent full read->edit->bash tool round-trip over streaming /v1/messages), it passes through the gateway against mockd, run.sh executes it as part of the suite, e2e/run.sh is green, and it's merged to main
```
*Note: mockd needs no changes — the gateway translates anthropic→chat toward
the backend. The new transport targets the gateway's `/v1/messages`.*

### 8. Harness self-checks + guardrail automation — risk: low
**Why:** run.sh only tests *through* the gateway, so a mockd regression is
indistinguishable from a gateway regression. And the LiteLLM digest pin
([docs/03 risk 8](docs/03-open-questions-and-risks.md) — the malware one) is a
hard guardrail enforced only by eyeball.
**Completion condition:**
```
run.sh gains a mockd-direct conformance step (isolates mockd regressions from gateway regressions), e2e gains negative-path tests (malformed JSON body and unknown model alias -> clean 4xx, no hang), CI fails if the LiteLLM image tag/digest deviates from the vetted pin, e2e/run.sh is green, and it's merged to main
```

### 9. Concurrency smoke — parallel streams must not cross-talk — risk: low
**Why:** every e2e test runs serially, but the gateway's whole job is serving
concurrent agents. A cross-request bleed (wrong `served_model` stamp,
interleaved SSE chunks) would be catastrophic and is currently invisible.
**Completion condition:**
```
an e2e test fires concurrent streaming requests across different model aliases with a fault injected on one of them, asserts every response carries the correct served_model stamp and terminates its stream cleanly, e2e/run.sh is green, and it's merged to main
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
- **Verify prompt-caching on the Azure/Anthropic route** ([docs/03 risk 5](docs/03-open-questions-and-risks.md))
  — needs real Foundry creds. Infra.

---

## Done

- ✅ Phase-0 groundwork (blockers A & B, conformance harness, deploy scaffold) — PR #1
- ✅ E2E test harness (mock + cli-auth profiles) — PR #2
