# Phase-0 RUNBOOK — one endpoint, 1 Spark + Foundry fallback

Stand up the LiteLLM gateway, point Claude Code (and optionally Codex) at it, and
prove protocol translation + fallback + auth work end to end. **No smart routing
yet** — the client asks for a model alias; the alias resolves to a Spark or
Foundry; Foundry is the tail of every fallback chain. See
[../docs/06-recommendation.md](../docs/06-recommendation.md).

## What Phase 0 proves
- One stable endpoint per user — no more swapping `ANTHROPIC_BASE_URL` / API keys by hand.
- Claude Code → Anthropic `/v1/messages` → translated to a Spark or Foundry backend.
- Codex → OpenAI `/v1/responses` → bridged to a Chat-Completions Spark (see caveat) or Foundry-OpenAI.
- Spark unhealthy / busy / timing out → automatic fallback to Foundry.

---

## 0. Prerequisites (ASK a human — do not invent)

Fill these in before you start; they're the values `.env` needs:

- [ ] **Spark inventory:** hostname/IP of at least one Spark, the model pinned on
      it, and its vLLM OpenAI endpoint (`http://<host>:8000/v1`). Confirm the
      vLLM tool-call parser is correct (Qwen3-Coder → `qwen3_xml`, **not** the
      default `qwen3_coder` — it has the `!!!!` runaway bug).
- [ ] **Azure AI Foundry:** resource name, the `/anthropic` base URL, the Claude
      **deployment names**, and how creds are provided (API key vs Entra ID).
- [ ] **Azure OpenAI (GPT):** resource base URL, deployment name, `api-version`, key.
- [ ] **Scope:** Claude Code only, or Codex too? (Codex→Spark carries the Blocker-A
      caveat below; Codex→Foundry-OpenAI is unaffected.)
- [ ] **Data governance:** confirmed with DISCO that Foundry usage is OK for the
      intended work (Context& / Delegate / Projectum / Consit; **no personal or
      customer data**). Sparks are local/private; Foundry is Azure — verify
      residency/retention. (docs/03 risk 10.)

---

## 1. Configure

```bash
cd deploy
cp .env.example .env
# Edit .env with the values from step 0. Generate a strong master key:
python3 -c "import secrets;print('sk-'+secrets.token_urlsafe(32))"
```

`litellm-config.yaml` reads `.env` two ways — both are already wired:
- `os.environ/VAR` — the whole field value comes from the env var.
- `${VAR}` — inline substitution (used where a prefix precedes the value, e.g.
  `model: openai/${SPARK_A_MODEL}`).

> If your pinned LiteLLM doesn't render inline `${VAR}`, replace those with the
> literal value, or move the whole field to `os.environ/`. Check the rendered
> config with `docker compose logs litellm` on first boot.

## 2. Pin a vetted LiteLLM version — READ THIS

`docker-compose.yaml` pins `ghcr.io/berriai/litellm:v1.83.14-stable` as a
**starting point you must verify**. Non-negotiables (docs/03 risk 8):

- **Never** run `1.82.7` or `1.82.8` — both shipped a credential stealer. `run.sh`
  hard-blocks them, but don't rely on that alone.
- The Codex→Spark Responses bridge (`use_chat_completions_api`) only exists in the
  **1.83.x-stable** line (≈ `1.83.14`+), which **post-dates** the malware incident
  — so you can't pin the pre-incident `1.82.6` *and* get Codex→Spark. Pick a vetted
  1.83.x-stable, verify its digest against LiteLLM's security guidance, and pin by
  digest once cleared.
- If you only need **Claude Code** (no Codex→Spark), you can drop
  `use_chat_completions_api` and pin an earlier vetted stable — your call.

## 3. Launch

```bash
./run.sh
# or directly:  docker compose up -d
```

Verify:
```bash
curl http://localhost:4000/health/liveliness           # -> {"status":"healthy"}
curl -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
     http://localhost:4000/v1/models                    # lists the aliases
```

Issue a per-user virtual key (attribution + revocation) off the master key:
```bash
curl -X POST http://localhost:4000/key/generate \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" \
  -H "Content-Type: application/json" \
  -d '{"models": ["qwen3-coder","claude-sonnet","claude-opus","gpt"], "user_id": "ewi"}'
# -> {"key": "sk-...."}   hand this to the user; never the master key.
```

## 4. Point Claude Code at it

```bash
export ANTHROPIC_BASE_URL="http://localhost:4000"
export ANTHROPIC_AUTH_TOKEN="<the sk-... virtual key from step 3>"
export ANTHROPIC_MODEL="qwen3-coder"      # or claude-sonnet / claude-opus
claude
```

LiteLLM exposes a native Anthropic `/v1/messages` surface, so Claude Code speaks
its own protocol and LiteLLM translates to the Spark/Foundry backend. If the
Spark is down/busy, the fallback chain lands on Claude-on-Foundry (same family →
tool-call format stays consistent), then GPT.

## 5. (Optional) Point Codex at it

```toml
# ~/.codex/config.toml
[model_providers.balancer]
base_url = "http://localhost:4000/v1"
wire_api = "responses"          # Codex dropped "chat"; MUST be responses
# OPENAI_API_KEY = the sk-... virtual key, via env
```

⚠️ **Blocker A caveat (verify before trusting Codex→Spark):** LiteLLM's
`/v1/responses` → Chat-Completions bridge (enabled by `use_chat_completions_api`
in the config) supports streaming + tool calls *per source*, but the feature is
young and has a bug history. Confirmed on paper, **not** by an observed round
trip. Before relying on it, smoke-test: streaming, parallel tool calls, and mixed
text+tool-call output against the actual vLLM model. Codex→Foundry-OpenAI (`gpt`)
is unaffected and works today.

## 6. Earn `agent_capable` for the Spark model

The config asserts `agent_capable: true` optimistically — **prove it**:

```bash
cd ../conformance && pip install -r requirements.txt
python conformance.py \
  --base-url "$SPARK_A_API_BASE" \
  --model "$SPARK_A_MODEL" \
  --runs 5 --json-out spark-a.json
```

Exit `0` = the model drives a multi-tool streaming session cleanly. Exit `1` =
tool calls leak/malform → treat the model as **chat-only** and route agents past
it to Foundry. Wire this into a cron so the flag is continuously measured (drift
happens on vLLM/model bumps). See [../conformance/README.md](../conformance/README.md).

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `401` from the gateway | Using master key as auth token, or bad virtual key | Use the per-user `sk-...` from step 3 |
| Claude Code gets garbage / raw tool-call text | Wrong vLLM tool-call parser on the Spark | Set `--tool-call-parser qwen3_xml`; re-run conformance harness |
| `${VAR}` appears literally in requests | Pinned LiteLLM doesn't do inline substitution | Use literal value or `os.environ/` for that field |
| Codex tool calls broken on Spark | Responses→Chat bridge edge case (Blocker A) | Fall back to Codex→Foundry-OpenAI; file findings in docs/03 |
| Foundry Claude `400` about `max_tokens` | Azure Anthropic API requires it | LiteLLM defaults to 4096; set explicitly if a client omits it |
| Everything down | Single gateway = SPOF (docs/03 risk 12) | Phase 0 is single-instance by design; add HA before real reliance |

## Scope & guardrails
- **No personal/customer data** through these models. Only Context& / Delegate /
  Projectum / Consit work. If unsure about Foundry data governance → **DISCO**.
- Secrets live only in `.env` (gitignored). Never commit keys.
