#!/usr/bin/env bash
# =============================================================================
# LOCAL profile runner (goal 4) — the manual, heavy, NEVER-in-CI counterpart to
# run.sh. Brings up the gateway in front of a REAL small coding model on Ollama,
# waits until the model is actually served, runs the conformance harness THROUGH
# the gateway against that real model, prints the JSON verdict, then tears down.
#
# This runs the agent_capable gate against a REAL model (Read -> Edit -> Bash) —
# the verdict mockd can't give (it would always "pass"). It is NOT a merge gate
# (that's run.sh, the mock profile). Nothing here runs in CI; Ollama + a multi-GB
# model + CPU inference is too heavy. See e2e/README "Profile: local" + docs/08.
#
# NOTE: the default qwen3:8b CLEARS the gate (agent_capable=true) — but it's slow
# CPU-only (reasoning mode); a full run can take 10-20+ min. Lighter models are a
# one-var swap but go red (see e2e/README "The model ladder"). This is never a
# merge gate regardless — that's the mock e2e/run.sh.
#
#   ./run.local.sh                 # up -> conformance (anthropic, 1 run) -> down
#   ./run.local.sh --keep          # leave the stack up to poke :4000 / :11434
#   ./run.local.sh --api chat      # wire protocol: chat | responses | anthropic
#   ./run.local.sh --runs 3        # more runs for a stabler error rate
#   ./run.local.sh --no-probes     # skip the parallel + tool_choice:required probes
#   ./run.local.sh --native-ollama # GPU FAST PATH: gateway in Docker -> host Ollama
#   OLLAMA_MODEL=qwen2.5-coder:3b ./run.local.sh   # swap the model (see the ladder)
#
# --native-ollama: skip the CPU-only Ollama CONTAINER and point the (still
# containerized) gateway at a REAL Ollama running natively on the host, which
# uses the Mac's Metal GPU — 10-50x faster. Requires host Ollama (`brew install
# ollama`); this preflights it, starts the daemon if needed, and pulls the model.
# Why keep the gateway in Docker: prod parity + the vetted pinned image (never a
# host `pip install litellm` — docs/03 risk 8). See e2e/README "Profile: local".
#
# Exit code is conformance.py's verdict (0 == agent_capable). A sub-threshold
# real model still SURFACES its JSON — that's the evidence; the merge gate is
# the mock run.sh, not this.
# =============================================================================
set -euo pipefail
cd "$(dirname "$0")"

KEEP=0
API="anthropic"
RUNS=1
NATIVE=0
EXTRA_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --keep|--no-down) KEEP=1; shift ;;
    --api) API="$2"; shift 2 ;;
    --runs) RUNS="$2"; shift 2 ;;
    --no-probes) EXTRA_ARGS+=("--no-probes"); shift ;;
    --native-ollama) NATIVE=1; shift ;;
    *) EXTRA_ARGS+=("$1"); shift ;;
  esac
done

export LITELLM_MASTER_KEY="${LITELLM_MASTER_KEY:-sk-local-master-test-key}"
export GATEWAY_URL="${GATEWAY_URL:-http://localhost:4000}"
export OLLAMA_MODEL="${OLLAMA_MODEL:-qwen3:8b}"
REPORT="${CONFORMANCE_JSON_OUT:-conformance.local.json}"

if [[ "$NATIVE" -eq 1 ]]; then
  COMPOSE="docker compose -f docker-compose.local-native.yaml"
else
  COMPOSE="docker compose -f docker-compose.local.yaml"
fi

cleanup() {
  if [[ "$KEEP" -eq 0 ]]; then
    echo "--- tearing down (models persist on the named volume; use 'down -v' to wipe) ---"
    $COMPOSE down --remove-orphans >/dev/null 2>&1 || true
  else
    echo "--- leaving stack up (--keep). Tear down with: $COMPOSE down ---"
  fi
}
trap cleanup EXIT

# --- venv for the conformance harness (openai + httpx) ----------------------
VENV="../.venv-e2e"
if [[ ! -x "$VENV/bin/python" ]]; then
  echo "--- creating venv $VENV ---"
  python3 -m venv "$VENV"
fi
"$VENV/bin/pip" install -q --disable-pip-version-check -r requirements.txt

# --- native-ollama preflight: ensure a host daemon has the model -------------
# In native mode there is NO Ollama container; the gateway talks to a host-run
# Ollama over host.docker.internal. Make sure it's installed, running (bound so
# the VM can reach it), and has the model pulled — otherwise the gateway would
# come up healthy but every request would fail to connect.
if [[ "$NATIVE" -eq 1 ]]; then
  echo "--- native-ollama: preflighting host Ollama (GPU fast path) ---"
  if ! command -v ollama >/dev/null 2>&1; then
    echo "ERROR: --native-ollama needs Ollama installed on the HOST." >&2
    echo "  Install it:  brew install ollama   (or https://ollama.com/download)" >&2
    echo "  Then re-run. Or drop --native-ollama to use the CPU-only container." >&2
    exit 1
  fi
  if ! curl -sf http://localhost:11434/api/tags >/dev/null 2>&1; then
    echo "host Ollama daemon not up — starting it (OLLAMA_HOST=0.0.0.0 so the VM can reach it)..."
    # 0.0.0.0 so the container can reach it via host.docker.internal, not just 127.0.0.1.
    OLLAMA_HOST=0.0.0.0 nohup ollama serve >/tmp/ollama-native.log 2>&1 &
    for i in $(seq 1 30); do
      curl -sf http://localhost:11434/api/tags >/dev/null 2>&1 && break
      [[ "$i" -eq 30 ]] && { echo "ERROR: host Ollama did not come up; see /tmp/ollama-native.log" >&2; exit 1; }
      sleep 1
    done
  fi
  echo "host Ollama up — pulling ${OLLAMA_MODEL} (no-op if already present; GPU, fast)..."
  ollama pull "$OLLAMA_MODEL"
fi

# --- up + wait for health ---------------------------------------------------
if [[ "$NATIVE" -eq 1 ]]; then
  echo "--- bringing up gateway only (native Ollama on the host, GPU) ---"
else
  echo "--- bringing up LOCAL stack (Ollama pulls the model on first run — be patient) ---"
fi
$COMPOSE up -d

echo "--- waiting for the gateway to become healthy ---"
# Container mode: the compose healthcheck gates litellm on the Ollama container
# being ready, so gateway-healthy implies model-served. Native mode: the model is
# already pulled+served on the host (preflight above), so this is quick.
for i in $(seq 1 180); do
  if curl -sf "$GATEWAY_URL/health/liveliness" >/dev/null 2>&1; then
    echo "gateway healthy (Ollama model is served)"
    break
  fi
  if [[ "$i" -eq 180 ]]; then
    echo "ERROR: gateway did not become healthy in time" >&2
    $COMPOSE logs --tail=60 ollama >&2 || true
    $COMPOSE logs --tail=30 litellm >&2 || true
    exit 1
  fi
  sleep 5
done

# --- conformance THROUGH the gateway against the REAL model ------------------
echo "--- conformance harness through the gateway (real model, --api ${API}, ${RUNS} run(s)) ---"
set +e
"$VENV/bin/python" ../conformance/conformance.py \
  --base-url "$GATEWAY_URL/v1" \
  --api "$API" \
  --model qwen3-coder \
  --api-key "$LITELLM_MASTER_KEY" \
  --runs "$RUNS" \
  --json-out "$REPORT" \
  ${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}
RC=$?
set -e

echo
echo "--- conformance JSON verdict ($REPORT) ---"
cat "$REPORT" 2>/dev/null || echo "(no report written)"
echo
if [[ "$RC" -eq 0 ]]; then
  echo "LOCAL PROFILE: agent_capable=true — the real model cleared the gate through the gateway"
else
  echo "LOCAL PROFILE: agent_capable=false (exit $RC) — the gate ran against the real model and the JSON above is the verdict."
  echo "  The default qwen3:8b is expected to PASS; a red here usually means a swapped-in lighter model"
  echo "  (qwen3:4b won't drive the loop; qwen2.5-coder leaks tool calls). See e2e/README 'The model ladder'."
fi
# Exit with conformance's verdict WITHOUT an explicit `exit` keyword: that would
# make shellcheck mark the `trap cleanup EXIT` handler unreachable (SC2317 on
# older shellcheck, SC2329 on newer). conformance.py returns exactly 0/1, so this
# final test under `set -e` yields the same status; the EXIT trap still tears down.
[ "$RC" -eq 0 ]
