#!/usr/bin/env bash
# =============================================================================
# smoke_cli.sh — OPT-IN, high-fidelity smoke test: drive the REAL Claude Code
# and/or Codex binaries against the gateway (cli-auth profile). This is the
# "both layered" second driver — the raw-HTTP pytest suite is the CI gate; this
# is the manual "does an actual coding agent work through the balancer" check.
#
# Prereqs:
#   1. ./borrow_creds.sh          # writes .env.cliauth with real API keys
#   2. docker compose --env-file .env.cliauth -f docker-compose.cliauth.yaml up -d
#   3. gateway healthy on :4000
#
# It mints a per-user virtual key, then points each installed CLI at the gateway
# and runs a trivial synthetic prompt (NO real/customer data — org policy).
#
#   ./smoke_cli.sh            # run whichever CLIs are installed
#   ./smoke_cli.sh claude     # just Claude Code
#   ./smoke_cli.sh codex      # just Codex
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

[[ -f .env.cliauth ]] || { echo "ERROR: .env.cliauth missing — run ./borrow_creds.sh first." >&2; exit 1; }
set -a; . ./.env.cliauth; set +a
GATEWAY_URL="${GATEWAY_URL:-http://localhost:4000}"

curl -sf "$GATEWAY_URL/health/liveliness" >/dev/null || {
  echo "ERROR: gateway not healthy at $GATEWAY_URL. Bring the cli-auth stack up first." >&2; exit 1; }

# Mint a scoped per-user virtual key (attribution + revocation).
echo "--- minting a virtual key ---"
VKEY=$(curl -s -X POST "$GATEWAY_URL/key/generate" \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"models":["haiku","claude-sonnet","gpt"],"user_id":"smoke-cli"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['key'])")
echo "  virtual key minted."

WHICH="${1:-all}"
PROMPT='Reply with exactly the single word: PONG. Do not use any tools.'

run_claude() {
  command -v claude >/dev/null || { echo "claude: not installed, skipping"; return; }
  echo "--- Claude Code -> gateway (model=haiku) ---"
  ANTHROPIC_BASE_URL="$GATEWAY_URL" \
  ANTHROPIC_AUTH_TOKEN="$VKEY" \
  ANTHROPIC_MODEL="haiku" \
    claude -p "$PROMPT" 2>&1 | tail -5 || echo "  (claude exited non-zero — inspect above)"
}

run_codex() {
  command -v codex >/dev/null || { echo "codex: not installed, skipping"; return; }
  echo "--- Codex -> gateway (model=gpt, wire_api=responses) ---"
  # Codex reads ~/.codex/config.toml for providers; drive it inline via env +
  # flags so we don't clobber the user's real config.
  OPENAI_BASE_URL="$GATEWAY_URL/v1" \
  OPENAI_API_KEY="$VKEY" \
    codex exec --model gpt "$PROMPT" 2>&1 | tail -8 || echo "  (codex exited non-zero — inspect above; check wire_api=responses + the Responses bridge)"
}

case "$WHICH" in
  claude) run_claude ;;
  codex)  run_codex ;;
  all)    run_claude; echo; run_codex ;;
  *) echo "usage: $0 [claude|codex|all]" >&2; exit 2 ;;
esac

echo
echo "Smoke done. A clean PONG through the gateway = the real client path works end-to-end."
