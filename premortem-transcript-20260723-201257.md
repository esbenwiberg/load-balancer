# Premortem transcript — LLM load-balancer go-real transition

**Date:** 2026-07-23 20:12
**Target:** The LLM load-balancer / task-aware router, the day after it goes live on Azure with real Foundry creds, real dev prompts, and a publicly-reachable endpoint.
**Method:** Gary Klein prospective-hindsight. Frame: "it's 6 months out, this has failed — why?" 8 failure modes, one investigator each, run in parallel, focused on failures the all-synthetic e2e harness structurally cannot exercise.

## Context

- **What:** LiteLLM v1.83.14 OpenAI/Anthropic-compatible gateway (Azure Container App) fronting Azure Foundry + a fleet of workbenches. Owned shadow/enforce routing policy behind `ROUTER_POLICY` (default shadow; enforce rewrites the requested model pre-call). Sticky session pins in a container-scoped SQLite file. Upward-only single escalation hop via a `router:escalate` tag. Read-only stdlib observability dashboard (:9300, no auth). Per-key budgets/rate-limits via LiteLLM. Bicep IaC: Container App + Postgres Flexible Server (LiteLLM key/spend ledger) + Key Vault + VNet.
- **Who for:** coding agents (Claude Code, Codex, generic OpenAI clients) that connect to one endpoint; devs across Projectum/Context&/Delegate/Consit/Sulava. Operator: Esben. External gate: DISCO (data governance).
- **Success:** a hosted OpenAI-compatible endpoint people connect to that routes correctly, stays inside data-governance rules, never leaks keys/creds, and doesn't blow the budget.
- **Key rules:** no personal/customer data through real-model paths (Foundry); the policy governance filter is the SOLE guard post-rewrite under enforce (LiteLLM doesn't re-check the allowlist after a rewrite); auto-merge from main valid ONLY while nothing deploys from main (tripwire). The entire e2e harness is synthetic — mock backends, mock Foundry, synthetic prompts, one container, localhost. Real prompts, real creds, a public endpoint, and multiple replicas have never been exercised.

## Raw premortem — the eight failure reasons

1. **Leaked key → free LLM / cred drain.** Public endpoint + Foundry creds; a virtual or master key escapes.
2. **PII flows to Foundry.** The governance rule exists; the technical control (content inspection) doesn't.
3. **Governance filter fails open under enforce.** Sole post-rewrite guard; policy silently degrades to shadow on any error; "no allowlist" == "unrestricted".
4. **Pin store breaks across Azure replicas.** Container-local SQLite in `/tmp`; scale/restart = fresh store; multiple replicas = divergent stores. Postgres deferred to "replica time," never built.
5. **Cost blowout.** Budgets/rate-limits never validated against real pricing, real concurrency, or agentic loops + uncached escalation re-ingestion.
6. **Unauthenticated dashboard exposed.** Read-only stdlib server, no auth, leaks routing/identity/session/fleet metadata.
7. **Auto-merge tripwire is prose, not a gate.** An unattended run ships a change straight to the live endpoint.
8. **Real Foundry breaks mock-tuned assumptions.** Capacity-429 vs rate-429, cooldowns, latency, unverified prompt-caching wreck fallback/health/cost.

## Code verification (2 findings confirmed against source)

- **`e2e/dashboard.py:1810`** — `host = os.environ.get("DASH_HOST", "0.0.0.0")`. Docstring (line 96) says "localhost / an internal compose network only"; the code default binds all interfaces. Confirmed.
- **`e2e/obs_callback.py:561-571`** — `allow = None` unless `key_models` is a non-empty list of real names without wildcards; otherwise "governance: key unrestricted" keeps every candidate incl. Foundry. `key_models = getattr(user_api_key_dict, "models", None)` (line 1394) → fails OPEN if the allowlist is missing/unloadable. Confirmed.

---

## Deep-dives

### #1 — Leaked key → free LLM / cred drain (Likelihood: medium · Severity: high)

**Failure story.** The master key never leaked — a virtual key did, which is worse because nobody was watching it. A dev at Consit wired the gateway into a Claude Code project, dropped `LITELLM_API_KEY=sk-...` into a `.env`, and the repo's `.gitignore` didn't cover the variant they used. Pushed to a client-shared GitHub repo. Because the Container App shipped with a permissive ingress, the endpoint was reachable from anywhere the key was — no VNet-only lock, no IP allowlist enforced. GitHub secret-scanning doesn't flag `sk-` LiteLLM keys the way it flags OpenAI's, so no automated alert fired.

Within days a scraper found the repo. The virtual key had a per-key budget, but budgets are a spend cap, not a breach alarm — the attacker ran the key flat out against Foundry-backed models right up to the ceiling, then it reset next cycle and they did it again. Spend looked "within budget" on the dashboard, so nothing screamed. The read-only :9300 dashboard has no auth either, so once someone probed the host they could enumerate live sessions, model names, and traffic shape — free reconnaissance.

Discovery came ~5 weeks later when Esben noticed Foundry monthly spend pinned at the budget ceiling every cycle with traffic from prompts nobody recognized. By then the key had processed thousands of real requests against a Projectum/Context&-only path — now an open DISCO data-governance question.

**Underlying assumption.** That a key in the hands of a trusted internal dev stays private, so budgets/rate-limits are sufficient and key *exposure* needs no detection.

**Early warning signs.** A virtual key hitting its budget ceiling at a steady max every cycle, or requests from IPs/user-agents outside the known dev fleet. Any key value appearing in a git push, CI log, or the :9300 dashboard reachable off-VNet.

### #2 — Real customer data reaches Foundry (Likelihood: very high · Severity: critical)

**Failure story.** It went live in shadow, then flipped to enforce, and for six weeks everything looked clean — because the only prompts anyone measured were the synthetic ones from `e2e/`. Then real devs at Consit pointed Claude Code at a live client repo to debug a payments integration. The agent did what agents do: slurped surrounding context — a config file with a customer's real name, a `.env` fragment, a stack trace carrying a production DB row — and streamed it through the one endpoint to Foundry. The gateway routed perfectly. The governance filter checked which backend the key could reach, waved it through, and logged metadata only — which meant no content inspected, ever. The rule "no customer data on a real-model path" was never code; it was a sentence in CLAUDE.md and a habit of using fake prompts in tests.

Nobody caught it because the dashboard showed exactly what it was built to show: green budgets, healthy routes, zero prompt bodies. It surfaced three months later when a Sulava client ran a routine vendor-data-flow review and asked, "does Projectum's shared LLM gateway send our data to Azure Foundry?" The honest answer was yes, thousands of times. DISCO's sign-off — granted on the understanding that a control enforced the boundary — was now void. They pulled approval, mandated a breach review across all five orgs, and every dev who touched the endpoint became a possible disclosure. The "no prompt content" design that felt like privacy hygiene made the blast radius unknowable: you couldn't even prove what had leaked.

**Underlying assumption.** That a governance rule enforced by test discipline would hold once real, uncontrolled human+agent traffic replaced the synthetic harness.

**Early warning signs.** The harness is 100% synthetic and no one can point to a single real dev prompt that ever flowed — the control has never been tested against its actual threat. DISCO's approval references "content controls / data inspection" that don't exist anywhere in the codebase; the filter only gates backends by key.

### #3 — Governance filter fails open under enforce (Likelihood: high · Severity: critical · CONFIRMED IN CODE)

**Failure story.** The leak rode in on the Codex/Responses-bridge path. `_policy_stateless` step 1 only builds an allowlist when `key_models` is a non-empty list of real model names: `if names and not any(w in names for w in _ALLOWLIST_WILDCARDS): allow = set(names)`. On any other shape — `None`, `[]`, or a wildcard — `allow` stays `None`, the code logs `"governance: key unrestricted"`, and keeps every candidate, Foundry included. That branch is correct for master-key profiles. It is catastrophic for a restricted key whose `models` list simply fails to arrive. And it didn't arrive: `UserAPIKeyAuth.models` surfaces differently across the three inbound protocols, and on the Responses-bridge path the key allowlist came back empty. Every Codex request from a local-only key was silently treated as unrestricted.

The rewrite then did the rest. An agentic Codex turn hits step 2's `agent_capable` gate; the local workbenches were either not `agent_capable` or heartbeat-unhealthy, so they were filtered out and only the Foundry backend survived. Step 4 chose it, `_apply_enforcement` rewrote `data["model"]` to the Foundry deployment, and — exactly as designed — LiteLLM never re-checked the allowlist against the rewritten model. There was no second wall by construction, so nothing downstream objected.

It ran for weeks. The single governance e2e test used a chat/completions key with a populated `models` list, so it stayed green; the broken path had no coverage. Detection came from Foundry's billing console — premium spend from orgs provisioned local-only.

**Underlying assumption.** That a restricted key's allowlist always reaches the policy as a populated list, so "no allowlist" safely means "unrestricted" rather than "failed to load."

**Early warning signs.** `ROUTING_RECORD` lines where a known-restricted `key_alias` carries `shadow_policy.reason` containing `"governance: key unrestricted"`, or a `candidate_set`/`chosen` on the foundry tier — visible in shadow, before enforce ever flipped. `delivered` records with a local-only `key_alias` and a `served_model`/enforced `chosen` on a Foundry backend.

### #4 — Pin store breaks across Azure replicas (Likelihood: high · Severity: medium–high)

**Failure story.** We shipped with `--num_workers 2` and a shared `/tmp` SQLite file, and every test proved it worked — because every test ran in one container. The guarded UPDATE genuinely gave pin-once and exactly-once escalation across both workers sharing one inode. What we never simulated was a second container. Then load arrived, Azure Container Apps scaled us to three replicas, and the guarantee that was "atomic within one file" quietly became "atomic within one of three files." Replica A pinned a session to the workbench tier; the next turn hit replica B, which had no row for that session and re-pinned it — sometimes to a different tier. Multi-turn sessions started flip-flopping mid-conversation: a client streaming a tool-formatted exchange got its format contract broken when turn 4 landed on a backend that never saw turns 1–3 (visible as latency spikes and a step-change in input-token cost), or the response schema changed shape mid-stream.

Escalation was worse because it was silent. "Exactly once per session" held per file, so a session bouncing across replicas escalated once on A and again on B — double escalation, double cost, and in the client-signaled path duplicate side-effects. Scale-to-zero and deploy rolls compounded it: idle traffic wiped `/tmp`, so returning sessions re-pinned and could re-escalate as if brand new. None of this tripped the gate because `e2e/run.sh` and `conformance/selftest.py` exercise a single process against a single store — the exact configuration where container-scoped state is indistinguishable from global state. Going live on Azure WAS "replica time," and nobody made the deferred Postgres call.

**Underlying assumption.** That "one container, many workers" and "many containers" are the same trust boundary for shared state — so a store global to a process tree is global to the deployment.

**Early warning signs.** Escalation/pin counters exceed distinct-session counts once replica count > 1. Same session_id logged with conflicting pinned-tier decisions within one conversation, correlated with pod identity; pin-DB rows resetting to zero after every scale-to-zero or deploy.

### #5 — Cost blowout (Likelihood: high · Severity: high)

**Failure story.** The dominant driver was the escalation hop. Agentic coding sessions are long and tool-heavy, so by the time a session got escalated the transcript was already huge — and the upward hop re-ingests the whole thing uncached into premium Foundry. Every escalation was a full-transcript re-bill at premium rates, and multi-turn agents triggered it repeatedly within one session. The "route cheap locally, escalate rarely" premise assumed prompt-caching absorbed the re-reads — but caching on the Anthropic/Foundry route was never verified and in fact wasn't active, so every re-ingest paid full freight. A single stuck Codex loop retrying tool calls compounded it: thousands of premium input tokens per minute, times several concurrent devs.

The synthetic tests gave false confidence because they proved the wrong thing. `max_budget:0 → 400` and `rpm:1 → 429` confirm the enforcement plumbing fires — they say nothing about whether the default budget numbers are sane against real Foundry pricing, real concurrency, or real re-ingestion volume. Green enforcement tests read as "budgets work," so nobody re-derived the actual per-key ceiling. Nobody caught it because the dashboard shows spend but is a pull, not a push. The bill accrued overnight — long agentic runs at 2am, no alert wired to SpendLogs. First signal was the Azure invoice.

**Underlying assumption.** That enforcement plumbing passing synthetic extremes meant the budget NUMBERS were calibrated for real agentic + escalation volume — and that caching made re-ingestion cheap.

**Early warning signs.** SpendLogs shows a single session's cost jumping stepwise on each escalation event, with input-token counts that never drop across turns. Foundry premium input tokens climbing while cache-hit rate sits near zero.

### #6 — Unauthenticated dashboard exposed (Likelihood: high · Severity: high · CONFIRMED IN CODE)

**Failure story.** The tripwire held for the gateway but not the dashboard. When the Azure stack went live, the exposure model was still a Needs-a-human item, so nobody explicitly bound the dashboard down — and the code default at `e2e/dashboard.py:1810` is `DASH_HOST=0.0.0.0`, not localhost. The docstring says "bind it to localhost / an internal compose network only," but a comment isn't a control. In the container it listened on all interfaces. The NSG config that fronted the gateway on :443 got copied to cover :9300 "temporarily" during a rushed cutover, and the temporary stuck. No auth, because it never had any.

It got found by boring automation — an internet-wide port scanner indexed the open :9300, and `GET /api/records` returns clean JSON to anyone. No exploit, just an HTTP GET. What they read: which `user_id`s and key aliases requested which models, session stickiness keys, per-team spend, escalation events, and — the reconnaissance jackpot — the fleet registry with every workbench's `api_base` URL and health. That last one is a live map of internal endpoints to attack next.

"Metadata only" was the fatal comfort. No prompt bodies leaked — but user_ids mapping to real Projectum/Delegate people plus their model-usage patterns is personal data under the org's own governance rule, and api_base + health + load is a target list. Aggregate spend-per-team is org-structure intel. The absence of prompt *content* was the thing everyone pointed at while the topology walked out the door.

**Underlying assumption.** That a tool's intended deployment context ("localhost test daemon") constrains its actual one, so a documented convention substitutes for an enforced bind and auth check.

**Early warning signs.** The `0.0.0.0` default shipped in code while every doc/comment claimed localhost-only — a divergence any config review or `grep DASH_HOST` would surface. The exposure model stayed an open Needs-a-human item *after* the go-live date was set — a blocker that didn't block.

### #7 — Auto-merge tripwire missed → ships to live (Likelihood: medium · Severity: critical)

**Failure story.** Someone wired the Azure deploy the obvious way: a GitHub Action on push to `main` that ships the gateway image to the Foundry-fronting endpoint. It worked, the demo landed, everyone moved on. Nobody flipped the CLAUDE.md tripwire from prose to reality — the release-model item stayed "Needs-a-human / fine as-is for now," because flipping it meant killing the auto-merge flow that was shipping goals every night. The tripwire lived in a markdown file the CI pipeline never reads and the overnight agent had no reason to re-evaluate mid-run. "The moment a real gateway serves traffic off main" was a moment no code observed.

Then a routine goal — the enforce-in-prod flip or a governance-guard tweak — branched at 02:00. It passed `e2e/run.sh` (no real backends) and `conformance/selftest.py` (all mocks). Green. Squash-merged, branch deleted, per contract. The Action fired and the change hit the live endpoint. But the synthetic gate never exercised real Foundry auth, real streaming fallback, or the actual allowlist path — so the change that weakened the sole governance guard sailed through. Live traffic started routing prompts past the data-governance check toward a real-model path. Discovery came from users, not tests.

**Underlying assumption.** A safety rule written as prose in CLAUDE.md will be honored at the exact moment it matters, even by an unattended agent that never re-reads it against changed infra.

**Early warning signs.** A deploy workflow triggering on push/merge to `main` exists while GOALS.md still lists the release model as undecided. Auto-merged PRs touching routing/enforce/governance whose only evidence is the synthetic gate — zero prod smoke test between merge and live.

### #8 — Real Foundry breaks mock-tuned assumptions (Likelihood: high · Severity: high)

**Failure story.** We shipped green. Every fault mockd could inject — 429, 503, malformed — was handled, cooldowns tuned, fallback order proven. Then real Foundry met a Tuesday-morning peak. The 429s that arrived weren't the rate-limit 429s mockd threw; they were regional capacity-429s ("no capacity"), and our fallback treated them identically — retry, cool down, hop to the next backend. But the next backend was the same Foundry region, also out of capacity. The chain didn't fail over, it fanned out: every capacity-429 triggered N retries across N backends, multiplying load precisely when Foundry had none to give. Cooldowns tuned against a mock that recovered in milliseconds were far too short; backends flapped in and out of the health set every heartbeat. Requests thrashed across a chain of exhausted endpoints and surfaced to users as opaque 5xx after seconds of silence.

Meanwhile the escalation hop — heavy sessions to a 30B-class model — streamed at single-digit tok/s. Interactive coding sessions hung mid-completion. And the cost premise collapsed: prompt-caching, never verified on the Anthropic/Foundry route, silently didn't apply. Every sticky/escalated turn re-ingested the full uncached transcript, so "escalate rarely" turns cost 3–5x estimate. Token expiry mid-session returned auth failures our code mapped to generic 5xx. Net: erratic latency, mystery errors, a bill multiples over forecast.

**Underlying assumption.** A deterministic mock's fault taxonomy and timing are a valid proxy for a real capacity-constrained, latency-variable, caching-uncertain cloud backend.

**Early warning signs.** Fallback-attempt count per request > 1 on average, correlated with time-of-day (peak) — the chain is fanning out, not failing over. Cost-per-escalated-turn diverging upward, i.e. cached-token ratio near zero on the Foundry route.

---

## Synthesis

**Most likely failure:** #2 — real customer data reaches Foundry. Near-certain the instant real traffic flows, because nothing technical has to break; the system routes perfectly and still breaches the rule, because the rule was never a control.

**Most dangerous failure:** #3 — governance guard fails open under enforce. A confirmed code path (obs_callback.py:561), silent, no backstop by design, routing restricted traffic to premium/governance-sensitive Foundry. Compounds directly with #2.

**Hidden assumption:** "A rule we wrote down is a control we enforce." Every top failure is a documented intention standing in for an enforced mechanism — governance rule (a sentence, not DLP), dashboard "localhost only" (a docstring, while code binds 0.0.0.0), auto-merge tripwire (prose, not a CI gate), enforce "sole guard" (a comment, removed by the degrade-on-error path). Beneath it: "green against the synthetic harness = safe in prod," when the harness is green *precisely because* it tests the one configuration where the go-real threats can't exist.

**Revised plan:**
1. Flip governance to fail CLOSED — missing/empty/unloadable `key_models` ⇒ local-only, never "unrestricted." Wildcard must be explicit.
2. Foundry opt-in, default-deny per key, until DISCO signs off on Foundry for a named data class — against the control that actually exists (backend-gating), not an assumed DLP.
3. `DASH_HOST=127.0.0.1` + auth; startup refuses 0.0.0.0 unless auth set; :9300 never on public ingress.
4. Postgres pin store before >1 replica (the deferred docs/12 §3 call) — or replicas=1 + scale-to-zero off, explicitly, with migration as the scale gate.
5. Turn the tripwire into a CI gate that fails the merge if a deploy-from-main workflow exists; decide the release model before wiring any deploy; add a prod smoke between merge and live.
6. Push-based spend alert (SpendLogs → X%/hour); real per-key budgets from real pricing; verify prompt-caching on the real route before trusting the escalation cost model.
7. Real-Foundry soak test: separate capacity-429 from rate-429 (don't fan out on capacity), re-tune cooldowns, cap fallback fan-out, map auth/quota errors to honest client errors.

**Pre-launch checklist:**
1. Governance fails closed — test proves a key with no/empty/unloadable allowlist cannot reach Foundry under enforce, on all three inbound protocols (esp. Codex/Responses). Until green, do not flip enforce in prod.
2. DISCO sign-off matches the actual control (backend-gating, no content inspection); Foundry default-deny per key; data classes named in writing.
3. Dashboard not public — `DASH_HOST` bound + authed, verified by an external port scan of the deployed endpoint.
4. Pin store decided — Postgres-backed, or replicas=1 + scale-to-zero off, documented as the scale gate.
5. No deploy-from-main until the release model is decided AND a technical gate enforces the tripwire; spend alert live before real creds; caching verified or escalation re-priced.
