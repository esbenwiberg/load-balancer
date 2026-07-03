# Tool calling — the make-or-break detail

Coding agents are **tool-call machines**. Claude Code does *everything* through tool calls
(Read, Edit, Bash, Grep, ...). Codex is the same. If tool calling degrades, the router is
worthless no matter how clean the routing is. So this gets its own doc.

There are **three independent layers** where tool calls break, and they compound.

---

## Layer 1 — Wire-format translation (LiteLLM's job; fiddly, mostly solved)

Each client/backend expresses tools differently, and every field must map **both
directions, including mid-stream**:

| Concept | Anthropic (Claude Code) | OpenAI Chat (vLLM/Ollama) | OpenAI Responses (Codex) |
|--------|--------------------------|----------------------------|---------------------------|
| Tool def | `tools[].input_schema` | `tools[].function.parameters` | `tools[].parameters` |
| Model call | `tool_use` block, args = **object** | `tool_calls[]`, args = **JSON string** | `function_call` item |
| Result | `tool_result` block, `tool_use_id` | `role:"tool"` msg, `tool_call_id` | `function_call_output`, `call_id` |

Bugs that live here:
- **args object vs JSON string** mismatch (Anthropic parsed object ↔ OpenAI stringified).
- **tool-call ID remapping** must be consistent across the whole transcript.
- **streaming partial tool-JSON** deltas must reassemble correctly.
- **parallel tool calls** (multiple `tool_use` in one assistant turn) surviving translation.
- **`tool_choice`** (`auto`/`required`/named) fidelity across formats.

LiteLLM handles this, but it's the fiddliest surface it maintains. Test it; don't assume.

---

## Layer 2 — The local model + engine emitting *structured* tool calls (the real minefield)

The part everyone underestimates. A local serving engine only returns structured
`tool_calls` if the **exact right per-model parser + chat template** is configured.
Otherwise the tool call comes back as **raw text in the `content` field** and the client
sees garbage.

### vLLM reality
- Requires `--enable-auto-tool-choice --tool-call-parser <X> [--chat-template ...]`.
- Parser is **per-model** (`hermes`, `llama3_json`, `mistral`, `qwen3_coder`, `qwen3_xml`, ...).
  Wrong parser → tool calls silently leak into content.

### Qwen3-Coder specifically — live, open bugs (as of this research)
- Default `qwen3_coder` parser can emit an **infinite stream of `!!!!!!`** on long inputs
  containing a tool call → fix is `--tool-call-parser qwen3_xml`.
- "Tool calling sometimes not parsed, remains in plain content" — open vLLM issue.
- `tool_choice:"required"` + reasoning + Qwen3 → **HTTP 400** — open vLLM issue.
- **Hermes parser in streaming mode returns raw text instead of parsed `tool_calls`** —
  open vLLM issue. (Coding agents *always* stream, so this matters.)

### Implication
"Does the model support tools?" is the wrong question. The right one:
> Does **this exact model + engine + parser + chat-template**, **under streaming**, produce
> clean structured tool calls for a **multi-tool** session?

Every model on every Spark is a separate config to get right — and to *keep* right as vLLM
and the models update. Smaller models also degrade on tool fidelity itself: malformed args,
hallucinated tool names, weak parallel-tool support, drift over long tool-heavy sessions.

---

## Layer 3 — Mid-session format switching (hard argument for sticky sessions)

Tool state is **per-backend**. If turns 1–3 ran on Foundry-Anthropic, the transcript is
full of Anthropic `tool_use`/`tool_result` blocks with Anthropic IDs. Routing turn 4 to a
local model via OpenAI translation means:
- the new model must continue a tool-call history **it didn't generate**,
- IDs must be remapped consistently on the fly,
- prompt cache is blown.

This is an independent, strong reason for **route sessions, not requests** (see
[03-open-questions-and-risks.md](03-open-questions-and-risks.md#1-mid-session-re-routing-breaks-agents)).

---

## Consequence for the design

### Acceptance test is not "does a reply come back"
Phase-0 conformance for **each Spark model** must be:
> Run Claude Code (and Codex) against the model doing a **real multi-tool task**
> (Read → Edit → Bash), **under streaming**, and count **malformed / unparsed tool calls**
> and recovery failures.

If that's not rock-solid, the model is **chat-only, not agent-capable** → route agents past
it to Foundry. Track a per-model capability flag: `agent_capable: true|false`.

### Add to the control-plane registry
Per model, not just "is it loaded" but:
- `agent_capable` (passed the tool-calling conformance test)
- `tool_parser` / `chat_template` in use (so we know the config that was validated)
- known limitations (e.g. no parallel tools, `tool_choice:required` unsafe)

### Routing rule
Never route a **tool-using** session to a model not flagged `agent_capable`. A model can be
great for plain completions and still fail as a coding-agent backend.

---

## How does OpenRouter achieve this? (it confirms the thesis)

OpenRouter routes tool-calling across hundreds of models and "just works" — so does that
undercut the minefield argument? No. It *validates* it. Here's how they actually do it:

1. **One-protocol normalization (their version of Layer 1).** You send OpenAI-shaped
   `tools`; they transform to each backend's format and normalize responses, including
   standardizing `finish_reason` to a fixed set (`tool_calls`/`stop`/`length`/...). Same
   translation layer we'd get from LiteLLM. Not magic.

2. **They don't run the metal — this is the whole trick.** OpenRouter is an aggregator on
   top of *professional inference providers* (Fireworks, Together, DeepInfra, Lambda, ...).
   Those providers already solved **Layer 2**: right vLLM parser + chat template, the
   `qwen3_xml`-vs-`qwen3_coder` bug fights, streaming validation. OpenRouter **inherits**
   solved tool calling; it never configures a parser itself.
   → **We are proposing to *be* the inference provider on the Sparks.** So the exact Layer-2
   problem OpenRouter outsourced lands on us. "How does OpenRouter do it" does not rescue us
   — they solved it by standing on providers who already did.

3. **They gate routing on tool support — our `agent_capable` flag, already invented.** Two
   mechanisms to steal verbatim:
   - **`require_parameters` / "only route to providers that support tool use":** when a
     request carries `tools`/`tool_choice`, OpenRouter won't even route to a provider that
     can't do it. Exactly "never send a tool-using session to a non-agent-capable model."
   - **Tool Call Error Rate:** they *continuously measure* how reliably each provider
     completes tool calls, surface it per model, and use it to order providers ("Auto
     Exacto"). This is the data-driven upgrade to a one-shot conformance test: **track
     malformed-tool-call rate per Spark model over time and deprioritize on drift**, don't
     just pass/fail once.

4. **Their "load balancing" ≠ our "tasting."** OpenRouter's default balancing is
   *provider-level for the same model* (inverse-square-of-price weighting, 30s outage
   window, fallbacks) — it picks *who serves model X cheapest/most reliably*, not *which
   model the task needs*. Task-based routing ("Auto Router") is a separate, coarser, opt-in
   feature. Reinforces: the reliability layer is the easy/valuable core; the tasting is the
   hard, optional part.

Their own docs admit the ceiling: *"OpenRouter can normalize the interface, but differences
in model capability, schema adherence, and tool-use reliability still matter."* Even the
market leader routes *around* bad tool-callers rather than fixing them.

**Design takeaways adopted from OpenRouter:**
- Capability-gate routing: tool-bearing request → only `agent_capable` backends.
- Track a live **tool-call error rate** per Spark model; feed it into routing + eviction.
- Normalize `finish_reason` and tool schemas centrally (LiteLLM) — one client contract.

## Open items
- [ ] Verify LiteLLM's Anthropic↔OpenAI **and** Responses↔ChatCompletions tool mapping under
      streaming, incl. parallel tool calls and `tool_choice`.
- [ ] Pin & validate the tool-call parser per Spark model (start: Qwen3-Coder → `qwen3_xml`).
- [ ] Build the multi-tool streaming conformance test as the gate for `agent_capable`.
- [ ] Decide client-by-client: is Codex→Spark viable given Responses-API tool bridging, or
      Codex→Foundry-only initially?

## Sources
- [vLLM tool calling docs](https://docs.vllm.ai/en/stable/features/tool_calling/)
- [Qwen3-Coder-30B — tool parser not working in vLLM](https://huggingface.co/Qwen/Qwen3-Coder-30B-A3B-Instruct/discussions/19)
- [vLLM #22975 — tool call left in plain content (qwen3 coder)](https://github.com/vllm-project/vllm/issues/22975)
- [vLLM #19051 — 400 on Qwen3 + reasoning + tool_choice required](https://github.com/vllm-project/vllm/issues/19051)
- [vLLM #31871 — hermes streaming returns raw text not tool_calls](https://github.com/vllm-project/vllm/issues/31871)
- [vLLM #29192 — parsers fail to populate tool_calls (Qwen2.5-Coder)](https://github.com/vllm-project/vllm/issues/29192)
