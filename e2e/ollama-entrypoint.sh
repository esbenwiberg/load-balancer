#!/bin/sh
# =============================================================================
# Ollama entrypoint for the e2e 'local' profile (goal 4). Makes the container
# SELF-CONTAINED: start the server, pull the coding model, warm it into memory,
# then hand the server the foreground. The compose healthcheck gates
# `depends_on` until BOTH the daemon answers AND the model is pulled+ready, so
# the gateway never boots against an empty Ollama.
#
# Why an entrypoint (not a sidecar `ollama pull` container): keeps the pull
# INSIDE the one service whose health we gate on, so "healthy" means "the model
# is actually here and loaded", not merely "the daemon is up".
#
# POSIX sh (the image ships /bin/sh); keep it shellcheck-clean.
# =============================================================================
set -eu

MODEL="${OLLAMA_MODEL:-qwen2.5-coder:3b}"
READY_MARKER="/tmp/.ollama-ready"

# A restart must re-prove readiness. The model cache lives on the mounted volume
# (so a re-pull is a no-op), but the marker must reflect THIS process being up.
rm -f "$READY_MARKER"

# Start the server in the background; keep its PID to hand it the foreground.
ollama serve &
SERVER_PID=$!

# Wait for the daemon to accept API calls (`ollama list` hits the local API).
echo "local-profile: waiting for the ollama daemon..."
until ollama list >/dev/null 2>&1; do
  sleep 1
done

echo "local-profile: pulling ${MODEL} (first run downloads a few GB; cached on the volume after)..."
ollama pull "$MODEL"

# Warm it: a tiny generation forces the weights into memory so the FIRST
# conformance request isn't a cold-load timeout. Non-fatal if it hiccups.
echo "local-profile: warming ${MODEL}..."
ollama run "$MODEL" "reply with the single word: ready" >/dev/null 2>&1 || true

touch "$READY_MARKER"
echo "local-profile: ${MODEL} pulled and ready — serving on :11434"

# Hand the server the foreground so the container lifecycle == the daemon.
wait "$SERVER_PID"
