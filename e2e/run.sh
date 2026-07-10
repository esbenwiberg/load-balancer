#!/usr/bin/env bash
# =============================================================================
# One-shot e2e: bring up the mock stack, wait healthy, run the pytest suite AND
# the conformance harness THROUGH the gateway (the Blocker-A plumbing gate),
# then tear down. Exit non-zero if anything fails.
#
#   ./run.sh              # up -> test -> down
#   ./run.sh --keep       # leave the stack running afterwards (for poking)
#   ./run.sh --no-down    # alias for --keep
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

KEEP=0
for arg in "$@"; do
  case "$arg" in
    --keep|--no-down) KEEP=1 ;;
  esac
done

COMPOSE="docker compose -f docker-compose.e2e.yaml"
export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-e2e-master-test-key}"
export GATEWAY_URL="${GATEWAY_URL:-http://localhost:4000}"
export GATEWAY_ENFORCE_URL="${GATEWAY_ENFORCE_URL:-http://localhost:4001}"  # goal-26 enforce-mode gateway
export MOCKD_URL="${MOCKD_URL:-http://localhost:9100}"
export DASH_URL="${DASH_URL:-http://localhost:9300}"   # goal-12 dashboard sink + data endpoint
export CONTROL_PLANE_URL="${CONTROL_PLANE_URL:-http://localhost:9400}"  # goal-13 fleet registry

cleanup() {
  if [[ "$KEEP" -eq 0 ]]; then
    echo "--- tearing down ---"
    $COMPOSE down -v --remove-orphans >/dev/null 2>&1 || true
  else
    echo "--- leaving stack up (--keep). Tear down with: $COMPOSE down -v ---"
  fi
}
trap cleanup EXIT

# --- venv for the test tooling (openai + httpx + pytest) --------------------
VENV="../.venv-e2e"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "--- creating venv $VENV ---"
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --disable-pip-version-check -r requirements.txt

# --- up + wait for health ---------------------------------------------------
echo "--- bringing up stack ---"
$COMPOSE up -d

echo "--- waiting for gateway health ---"
for i in $(seq 1 60); do
  if curl -sf "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
    echo "gateway healthy"
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "ERROR: gateway did not become healthy" >&2
    $COMPOSE logs --tail=50 litellm >&2 || true
    exit 1
  fi
  sleep 2
done

# The enforce-mode gateway (goal 26) boots AFTER the shadow one (depends_on:
# migrations). Wait for it too — the dedicated enforce tests hit it directly.
echo "--- waiting for enforce-gateway health ---"
for i in $(seq 1 60); do
  if curl -sf "$GATEWAY_ENFORCE_URL/health/liveliness" >/dev/null 2>&1; then
    echo "enforce gateway healthy"
    break
  fi
  if [[ "$i" -eq 60 ]]; then
    echo "ERROR: enforce gateway did not become healthy" >&2
    $COMPOSE logs --tail=50 litellm-enforce >&2 || true
    exit 1
  fi
  sleep 2
done

# --- pytest suite (raw-HTTP client emulation) -------------------------------
# E2E_ALLOW_RESTART lets the spend-durability test (goal 11b) restart the gateway
# CONTAINER to prove Postgres-backed spend survives a cold gateway process. It's
# opt-in so a bare `pytest` against a manual/remote stack can't kill the gateway;
# run.sh owns the compose stack, so it's always safe to grant here.
export E2E_ALLOW_RESTART=1
export E2E_LITELLM_CONTAINER="${E2E_LITELLM_CONTAINER:-litellm-e2e}"
echo "--- pytest e2e suite ---"
"$VENV/bin/python" -m pytest test_e2e.py -v

# --- conformance DIRECT against mockd: isolate the backend (goal 8) ----------
# The other conformance steps run THROUGH the gateway, so a mockd regression is
# indistinguishable from a gateway regression — both surface as the same red.
# This step hits mockd's OpenAI chat endpoint directly (no gateway hop): if the
# scenario breaks HERE, the fault is in mockd; if it breaks only in the gateway
# steps below, the fault is in the gateway/translation. mockd doesn't auth, so
# the key is a placeholder. This runs FIRST so an isolated backend regression is
# attributed before the gateway steps can muddy the signal.
echo "--- conformance harness DIRECT against mockd (chat, no gateway) ---"
"$VENV/bin/python" ../conformance/conformance.py \
  --base-url "$MOCKD_URL/v1" \
  --api chat \
  --model qwen3-coder \
  --api-key "sk-mockd-direct-placeholder" \
  --runs 3

# --- conformance THROUGH the gateway: the Blocker-A plumbing gate -----------
# Responses -> Chat bridge (Codex path) end-to-end, deterministically green
# because mockd plays the scenario by the rules. Proves the bridge mechanics,
# not a real model's quality.
echo "--- conformance harness through the gateway (Responses bridge) ---"
"$VENV/bin/python" ../conformance/conformance.py \
  --base-url "$GATEWAY_URL/v1" \
  --api responses \
  --model qwen3-coder \
  --api-key "$LITELLM_MASTER_KEY" \
  --runs 3

# --- conformance THROUGH the gateway: the Anthropic tool-call gate (goal 7) --
# Claude Code's REAL path — /v1/messages with tools, streaming — driven through
# the full read->edit->bash scenario AND both probes (parallel tool calls,
# tool_choice:required). The gateway translates anthropic->chat toward mockd and
# back, so this is the ONLY gate on tool-call translation over our single
# biggest client surface. mockd is unchanged; it just sees a chat request.
echo "--- conformance harness through the gateway (Anthropic /v1/messages, tools+stream) ---"
"$VENV/bin/python" ../conformance/conformance.py \
  --base-url "$GATEWAY_URL/v1" \
  --api anthropic \
  --model qwen3-coder \
  --api-key "$LITELLM_MASTER_KEY" \
  --runs 3

echo
echo "ALL E2E CHECKS PASSED"
