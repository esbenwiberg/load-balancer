# Tool-calling conformance harness

The gate that earns `agent_capable`. It drives a model through a real multi-tool
coding task (**Read → Edit → Bash**) **under streaming** against any
OpenAI-compatible `base_url`, and measures how reliably it emits *clean,
structured* tool calls.

**Why it exists:** a model can chat fine and still be useless as a coding-agent
backend. See [../docs/04-tool-calling.md](../docs/04-tool-calling.md). "Does a
reply come back" is not the bar. The bar is: does **this exact model + engine +
parser + chat-template, under streaming, drive a multi-tool session with clean
structured tool calls** — not tool calls leaked into plain text, malformed JSON,
hallucinated tool names, or runaway output (`!!!!`).

The output sets `agent_capable` in the control-plane registry. The running
`tool_call_error_rate` is the OpenRouter-style signal you deprioritize a backend
on when it drifts.

## What it detects

| Failure mode | How it shows up in the wild |
|---|---|
| `leaked_in_content` | Wrong vLLM `--tool-call-parser` / chat template → the tool call comes back as `<tool_call>…`, `<function=…>`, or a JSON blob in `content` instead of structured `tool_calls`. |
| `invalid_json_args` | Model emits tool-call arguments that don't parse as JSON. |
| `unknown_tool` | Model hallucinates a tool name not in the provided set. |
| `missing_required_arg` | Args parse but a required parameter is absent. |
| `runaway` | Degenerate generation — the Qwen3-Coder `!!!!!!` infinite stream, or low-entropy loops. |
| `api_error` | Backend 400s (e.g. the Qwen3 + reasoning + `tool_choice:required` bug). |

Grading is split: **tool-call mechanics** (the error rate above) vs **task
progress** (did it actually read → correctly edit 8000→9000 → run tests → report).
`agent_capable` requires both: a low error rate *and* the model can finish *and*
no probe defect (below).

### Two single-turn probes (on by default; `--no-probes` to skip)

- **parallel** — invites two tool calls in one turn. A model that reads serially
  is fine (not a defect), but if it *does* parallelize, the calls must survive
  with distinct ids + valid known names. Catches the LiteLLM Responses-bridge
  **index-collision bug (#21331)** that collapses parallel calls.
- **tool_choice:required** — forces a tool call. Catches the **Qwen3 + reasoning
  + `tool_choice:"required"` HTTP-400** (doc 04) and models that ignore `required`.

A probe defect forces `agent_capable=false`.

### Recovery

When a tool call returns an `error:` result, the harness tracks whether the model
recovers and still finishes (`recoveries` in the summary). A backend that can't
recover from a bad edit is fragile in real agent use.

## Install & run

```bash
pip install -r requirements.txt

# Chat Completions, straight at a Spark's vLLM endpoint (the Claude Code path):
python conformance.py \
  --base-url http://spark-a.internal:8000/v1 \
  --model Qwen/Qwen3-Coder-30B-A3B-Instruct \
  --runs 5 \
  --json-out report.json

# Responses API THROUGH LiteLLM — validates the Codex→Spark bridge end-to-end
# (Blocker A). Point at the LiteLLM proxy, not vLLM directly:
python conformance.py \
  --base-url http://litellm.internal:4000/v1 \
  --api responses \
  --model qwen3-coder \
  --api-key "$LITELLM_VIRTUAL_KEY" \
  --runs 5
```

`--api chat` (default) speaks Chat Completions; `--api responses` speaks the
OpenAI Responses API — the endpoint Codex uses and the one that exercises
LiteLLM's Responses→ChatCompletions bridge.

Exit code is `0` if `agent_capable`, `1` otherwise — wire it into CI / a cron so
the flag is *continuously* measured, not earned once.

### Key flags

- `--runs N` — repeat for a stable error rate (default 5).
- `--no-stream` — disable streaming. **Leave streaming ON**; it's the realistic
  path and where several vLLM parser bugs only appear.
- `--fail-threshold` — max `tool_call_error_rate` still counted as capable
  (default `0.02`).
- `--min-completion` — min fraction of runs that must finish the task (default `0.8`).
- `-v` — print each tool call as it happens.

All flags have env fallbacks: `CONFORMANCE_BASE_URL`, `CONFORMANCE_API_KEY`,
`CONFORMANCE_MODEL`, `CONFORMANCE_RUNS`, `CONFORMANCE_NO_STREAM=1`.

## Offline self-test

`python selftest.py` — replays scripted good/bad model turns through the real
grading path (no network, no `openai` dep). Proves the happy path passes clean
and every failure mode is caught. Run it after touching the detectors.

## Targeting a Spark model — the config that must match

vLLM only returns structured tool calls with the **right per-model parser**. If
this harness reports high `leaked_in_content`, the parser/chat-template is almost
certainly wrong. Starting points (verify against current vLLM):

- Qwen3-Coder-30B → `--enable-auto-tool-choice --tool-call-parser qwen3_xml`
  (the default `qwen3_coder` parser has the `!!!!` runaway bug on long inputs).

Record the parser + chat template that *passed* alongside the `agent_capable`
flag — that's the validated config, and it can regress on a vLLM/model bump.
