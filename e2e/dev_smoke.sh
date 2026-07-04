#!/usr/bin/env bash
# =============================================================================
# dev_smoke.sh (goal 10) — prove the standing dev stack works end-to-end.
#
# With the dev stack up (docker-compose.dev.yaml), this mints a scoped virtual
# key (the real client flow) and drives ALL THREE client surfaces through the
# gateway, each routed to a DIFFERENT mock container, then asserts the
# served_model=<model>@<instance> stamp so you can see WHICH container answered:
#
#   Anthropic  /v1/messages        model=qwen3-coder-a -> workbench-a
#   OpenAI     /v1/chat/completions model=qwen3-coder-b -> workbench-b
#   OpenAI     /v1/responses        model=claude-sonnet -> mock-foundry
#
# Anthropic is driven STREAMING on purpose: it's what Claude Code does, and
# LiteLLM 1.83.14's NON-stream /v1/messages over an openai backend drops text
# (see README > "Findings"). The other two surfaces are fine non-streaming.
#
# Prereq:  docker compose -f docker-compose.dev.yaml up -d   (wait ~healthy)
# Usage:   ./dev_smoke.sh
# Exit 0 iff every surface routed and carried its expected instance stamp.
# =============================================================================
set -uo pipefail
cd "$(dirname "$0")" || exit 1

GATEWAY_URL="${GATEWAY_URL:-http://localhost:4000}"
LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-dev-master-test-key}"
PROMPT='Reply in one short line. Do not use any tools.'

fail() { echo "  ✗ $1" >&2; FAILS=$((FAILS + 1)); }
ok()   { echo "  ✓ $1"; }
FAILS=0

# --- gateway must be up -----------------------------------------------------
if ! curl -sf "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
  echo "ERROR: gateway not healthy at $GATEWAY_URL." >&2
  echo "Bring the dev stack up first:  docker compose -f docker-compose.dev.yaml up -d" >&2
  exit 1
fi
echo "gateway healthy at $GATEWAY_URL"

# --- mint a scoped per-user virtual key (attribution + revocation) ----------
echo "--- minting a virtual key ---"
VKEY=$(curl -s -X POST "$GATEWAY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"models":["qwen3-coder-a","qwen3-coder-b","claude-sonnet"],"user_id":"dev-smoke"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin).get('key',''))" 2>/dev/null)
if [[ -z "$VKEY" ]]; then
  echo "  (key mint failed — falling back to master key)"
  VKEY="$LITELLM_MASTER_KEY"
else
  ok "virtual key minted"
fi

# --- 1. Anthropic /v1/messages (streaming) -> workbench-a -------------------
echo "--- [1/3] Anthropic /v1/messages (stream)  model=qwen3-coder-a ---"
A_OUT=$(curl -sN "$GATEWAY_URL/v1/messages" \
  -H "Authorization: Bearer $VKEY" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen3-coder-a\",\"max_tokens\":128,\"stream\":true,\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}]}" \
  | grep -o 'served_model=[^"\\ ]*' | head -1)
echo "  served: ${A_OUT:-<none>}"
if [[ "$A_OUT" == "served_model=qwen3-coder-a@workbench-a" ]]; then
  ok "anthropic messages -> workbench-a"
else
  fail "anthropic messages: expected served_model=qwen3-coder-a@workbench-a, got '${A_OUT:-<none>}'"
fi

# --- 2. OpenAI /v1/chat/completions -> workbench-b --------------------------
echo "--- [2/3] OpenAI /v1/chat/completions       model=qwen3-coder-b ---"
C_OUT=$(curl -s "$GATEWAY_URL/v1/chat/completions" \
  -H "Authorization: Bearer $VKEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"qwen3-coder-b\",\"messages\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print((d.get('choices',[{}])[0].get('message',{}).get('content') or ''))" 2>/dev/null)
echo "  content: ${C_OUT:-<none>}"
if [[ "$C_OUT" == *"served_model=qwen3-coder-b@workbench-b"* ]]; then
  ok "chat completions -> workbench-b"
else
  fail "chat completions: expected served_model=qwen3-coder-b@workbench-b, got '${C_OUT:-<none>}'"
fi

# --- 3. OpenAI /v1/responses -> mock-foundry --------------------------------
echo "--- [3/3] OpenAI /v1/responses (Codex path) model=claude-sonnet ---"
R_OUT=$(curl -s "$GATEWAY_URL/v1/responses" \
  -H "Authorization: Bearer $VKEY" -H "Content-Type: application/json" \
  -d "{\"model\":\"claude-sonnet\",\"input\":[{\"role\":\"user\",\"content\":\"$PROMPT\"}]}" \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print(''.join(c.get('text','') for it in d.get('output',[]) for c in (it.get('content') or [])))" 2>/dev/null)
echo "  output: ${R_OUT:-<none>}"
if [[ "$R_OUT" == *"served_model=claude-sonnet@mock-foundry"* ]]; then
  ok "responses bridge -> mock-foundry"
else
  fail "responses: expected served_model=claude-sonnet@mock-foundry, got '${R_OUT:-<none>}'"
fi

echo
if [[ "$FAILS" -eq 0 ]]; then
  echo "DEV SMOKE PASSED — all three surfaces routed through the gateway to distinct containers."
  exit 0
else
  echo "DEV SMOKE FAILED — $FAILS surface(s) did not route/stamp as expected." >&2
  exit 1
fi
