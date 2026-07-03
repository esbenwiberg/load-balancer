# Landscape — what already exists

Goal: don't reinvent. Map the existing tools onto the two sub-problems (gateway vs router)
and the Spark serving layer.

## The two sub-problems

```
                      ┌─────────────────────────────────────────┐
   Claude Code  ──────▶                                          │
   (Anthropic API)    │   (A) GATEWAY: unify protocols,          │
                      │       auth, fallback, observability      │
   Codex        ──────▶                                          │
   (OpenAI Responses) │   (B) ROUTER: "taste" request → pick     │
                      │       model/backend                      │
   Other        ──────▶                                          │
                      └───────┬─────────────────────┬────────────┘
                              │                     │
                       Spark workbenches       Azure AI Foundry
                       (local models)          (Anthropic/OpenAI, fallback)
```

---

## (A) Gateways / proxies

### LiteLLM — the front-runner ⭐
- OSS Python proxy ("AI Gateway"). Calls 100+ providers behind a **single OpenAI-format
  API**, and also exposes a native **Anthropic `/v1/messages`** endpoint that translates
  to non-Anthropic backends and back.
- Built-in: **load balancing**, **fallbacks** (incl. context-window fallbacks), cooldowns,
  timeouts, retries (exp. backoff), virtual keys, spend tracking, guardrails, admin UI.
- Directly documented for **Claude Code** (`ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`)
  and for **local vLLM** backends. This is basically our (A) out of the box.
- Has a nascent **auto-routing** feature (semantic + a rule-based "complexity router") —
  overlaps with (B), see below.
- ⚠️ **Supply-chain warning:** LiteLLM PyPI `1.82.7` and `1.82.8` were compromised with
  credential-stealing malware. **Pin a known-clean version.** Vet the dependency.

### Others (know they exist, probably not needed)
- **Portkey** — AI gateway w/ routing, load balancing, observability. Hosted or self-host.
- **Bifrost (Maxim)** — fast Go gateway, OpenAI-compatible.
- **Envoy AI Gateway / Kong AI Gateway / Cloudflare AI Gateway** — infra-level, heavier.
- **OpenRouter** — hosted router. Wrong shape: we want self-hosted over *our* hardware.

**Verdict:** start with **LiteLLM** as the gateway. It already speaks both the client
protocols we care about and both backend families.

---

## (B) Routers — "taste the request, pick a model"

Three tiers, cheapest/dumbest → smartest/slowest:

### 1. Rule-based / complexity routing — **<1ms, zero extra calls**
- Score by token count, presence of code, reasoning markers, technical terminology, etc.
- LiteLLM ships a "complexity router" doing exactly this (7 dimensions), routing simple →
  cheap model, complex → capable model.
- Cheapest to run, crudest signal. **Best day-one option.**

### 2. Semantic / embedding routing — **~28–93ms per request**
- Embed the input, compare against example "utterances" per route, pick highest cosine
  match above a threshold.
- LiteLLM **auto-routing** does this (`router.json`: utterances, threshold, target model,
  embedding model). Also **Aurelio Semantic Router**, and the **vLLM Semantic Router**
  (Red Hat, "Athena" release) — a purpose-built OSS mixture-of-models router.
- Routing overhead measured at ~0.4–5% of total request time. Good accuracy/latency
  balance. Needs curated utterance sets per route.

### 3. LLM-based routing — most flexible, adds a model round-trip
- **RouteLLM (LMSYS):** trains a router on preference data to send each query to a strong
  or weak model; big cost savings, minimal quality loss. Ships a lightweight BERT router.
- **Katanemo Arch / `archgw`:** an "intelligent prompt gateway" built on **Envoy** by
  ex-Envoy core contributors. Uses purpose-built small models — **Arch-Router-1.5B**
  (maps queries to *domain/action preferences* → model choice) and **Arch-Function-3B**.
  This is (A) **and** (B) in one box, and it's agent-oriented. Strong alternative to
  LiteLLM if we want routing as a first-class citizen.
- **Not Diamond, Martian, Unify** — hosted/commercial routers. Note but deprioritize
  (we want self-hosted, and hosted routers see our prompts).

**Verdict:** phase it. Day one = no routing or rule-based. Later = semantic. Only reach
for LLM-based (Arch-Router) if the cheaper tiers misroute too often. Note: **Arch could
replace LiteLLM entirely** if we decide routing is the center of gravity — evaluate both.

---

## Spark serving layer — how models actually run on a DGX Spark

The community has already built our exact backend stack. Reference architecture from the
NVIDIA dev forums:

```
Client → LiteLLM (:14000) → llama-swap (:28080) → model container (vLLM | llama.cpp | Ollama)
```

### The hardware reality (this shapes everything)
- **128 GB unified CPU/GPU memory**, ~121.7 GiB usable by CUDA; GB10 has a ~126.5 GB
  system-RAM ceiling.
- **Low-concurrency box, not a serving farm.** NVIDIA's own guidance: keep
  `--max-num-seqs` low; the Spark is "better suited to small-batch inference than
  high-concurrency serving."
- Throughput examples (vLLM batching):
  - Qwen2.5-3B BF16: **26 tok/s** single user → **477 tok/s** aggregate at 16 concurrent.
  - Gemma-class 31B NVFP4: **6 tok/s** single → **92 tok/s** aggregate at 16.
  - i.e. a 30B-class model is **single-digit tok/s for one interactive user.** Painful for
    a coding agent unless expectations are set.
- Can fit up to ~**200B-param NVFP4** models on one box (barely), but only one big model
  at a time.

### `llama-swap` — on-demand model lifecycle / VRAM manager
- You **cannot** keep all models resident in 128 GB. llama-swap spawns a model's container
  on first request, health-checks it, proxies to it, and **evicts it after an idle
  timeout** (600–3600s) to reclaim memory.
- Models grouped into **S/M/L tiers** with `swap: true` / `exclusive: true`: loading an
  L-tier model auto-evicts S/M models to avoid OOM.
- **Cold-start cost is real:** container spin-up + weight load = seconds to tens of
  seconds (Ollama adds a 15s GPU-settle delay). **You do not want to hot-swap models on
  the interactive request path.**

### Inference engines
- **vLLM** — primary; fastest for dense Safetensors models; best multi-request batching.
- **llama.cpp** — GGUF quantized; lighter memory, lower throughput.
- **Ollama** — easy, single-user/prototyping; slower under concurrency.

**Verdict:** on each Spark run `vLLM` (via `llama-swap` if a box must host multiple
models). But prefer **pinning one hot model per Spark** for interactive coding — swapping
is a batch/off-hours luxury, not an interactive-path move.

---

## Client compatibility — the transparency constraint

"User is unaware" only holds if we speak each client's exact wire protocol.

- **Claude Code** → Anthropic **Messages API** (`/v1/messages`). Point it via
  `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN`. LiteLLM exposes this natively. ✅
- **Codex CLI** → **OpenAI Responses API only.** OpenAI **removed `wire_api = "chat"`**;
  `config.toml` must use `wire_api = "responses"`. Configure a custom
  `[model_providers.<id>]` with `base_url` (…/v1) + `OPENAI_API_KEY`/`OPENAI_BASE_URL`.
  ⚠️ **Sharp edge:** many local servers (vLLM/Ollama) speak **Chat Completions**, not
  Responses. Routing Codex → a Spark needs a **Responses ↔ Chat-Completions shim**. Must
  verify LiteLLM's Responses bridge covers this end-to-end. **Open question.**

---

## Sources

- LiteLLM: [repo](https://github.com/BerriAI/litellm) · [routing](https://docs.litellm.ai/docs/routing) · [fallbacks](https://docs.litellm.ai/docs/proxy/reliability) · [auto-routing](https://docs.litellm.ai/docs/proxy/auto_routing) · [Claude Code quickstart](https://docs.litellm.ai/docs/tutorials/claude_responses_api) · [non-Anthropic models](https://docs.litellm.ai/docs/tutorials/claude_non_anthropic_models)
- Routers: [RouteLLM concept](https://blog.n8n.io/llm-routing/) · [vLLM Semantic Router](https://vllm-semantic-router.com/) · [Red Hat semantic router](https://developers.redhat.com/articles/2025/05/20/llm-semantic-router-intelligent-request-routing) · [Arch / archgw](https://github.com/katanemo/archgw) · [Arch-Router paper](https://arxiv.org/html/2506.16655v1) · [Arch-Router-1.5B](https://huggingface.co/katanemo/Arch-Router-1.5B)
- DGX Spark: [full stack forum thread](https://forums.developer.nvidia.com/t/running-a-full-llm-stack-on-dgx-spark-gb10-your-application-litellm-llama-swap-vllm-llama-cpp-ollama/367580) · [vLLM on DGX Spark](https://vllm.ai/blog/2026-06-01-vllm-dgx-spark) · [NVIDIA vLLM playbook](https://build.nvidia.com/spark/vllm) · [inference engine choice](https://medium.com/@michael.hannecke/four-inference-engines-one-box-when-to-use-which-on-the-dgx-spark-6b32a53db768)
- Codex: [config reference](https://developers.openai.com/codex/config-reference) · [custom provider](https://www.morphllm.com/codex-provider-configuration) · [local LLMs](https://unsloth.ai/docs/basics/codex)
