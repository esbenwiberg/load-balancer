# Project: LLM Load Balancer / Task-Aware Router

Research + build workspace for a task-aware router that puts one endpoint in
front of local Spark workbenches and Azure Foundry. Design lives in
[`docs/`](docs/); the decision is [`docs/06`](docs/06-recommendation.md).
Runnable pieces: [`deploy/`](deploy/) (Phase-0 gateway), [`conformance/`](conformance/)
(the `agent_capable` gate), [`e2e/`](e2e/) (test with no real backends).

## Running a goal (the unattended contract)

Kick off large work with the built-in **`/goal <condition>`**. Pull conditions
from [`GOALS.md`](GOALS.md). This contract defines *how* to work toward one —
especially when I'm away and can't answer questions:

- **Ship to a PR, then auto-merge if green.** Branch off `main` → open a PR →
  run `e2e/run.sh` **and** `conformance/selftest.py` → if both pass, squash-merge
  and delete the branch. This is the goal's finish line.
- **Never merge red, never skip the gate.** If `e2e/run.sh` fails, fix it or stop
  — do not merge, do not disable a test to go green. Keep `main` releasable.
- **⚠️ Auto-merge is valid ONLY while nothing deploys from `main`.** Blast radius
  today is the repo (every merge is a revertible squash). The moment a real
  gateway serves traffic off `main`, STOP auto-merging — switch to draft-PR-only
  and flag it to me. Treat this as a hard tripwire, not a preference.
- **Decide and document, don't block.** For a *reversible* decision, make the
  reasonable call and record it in the PR body ("chose X over Y because…"). I may
  be unreachable — a blocked goal that could have proceeded is a failure.
- **Stop at a DRAFT PR (do not merge) when:** the goal is tagged
  *Needs-a-human*, or you hit something *irreversible*, needs real
  creds/infra/external sign-off, or a design fork that changes the architecture.
  Leave the draft PR + a clear "here's the decision I need" note.
- **Keep `GOALS.md` honest.** Tick off what you finish; add follow-up goals you
  discover (tagged autonomy-friendly vs needs-a-human) so the next morning's run
  has something vetted to grab.

## Guardrails (always, not just goal runs)

- **Data governance:** no personal/customer data through any real-model path
  (Foundry, cli-auth). Only Context& / Delegate / Projectum / Consit work. Keep
  test prompts synthetic. In doubt → **DISCO**.
- **Secrets:** never commit keys/tokens. `.env*` and creds are gitignored; real
  values live only in local env / gitignored files.
- **LiteLLM version:** never `1.82.7`/`1.82.8` (credential-stealing malware);
  pin a vetted `1.83.x-stable` and verify the digest ([docs/03 risk 8](docs/03-open-questions-and-risks.md)).
- **Verify, don't assume:** `e2e/run.sh` is the arbiter that a change works;
  run it before claiming done.
