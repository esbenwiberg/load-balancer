#!/usr/bin/env bash
# Phase-0 gateway launcher. Sanity-checks the environment, refuses known-bad
# LiteLLM versions, then brings the proxy up. Not a substitute for RUNBOOK.md.
set -euo pipefail

cd "$(dirname "$0")"

# --- 1. .env must exist and not be the example ------------------------------
if [[ ! -f .env ]]; then
  echo "ERROR: .env not found. Copy .env.example to .env and fill it in." >&2
  exit 1
fi

# --- 2. Refuse to run with placeholder secrets ------------------------------
if grep -qE '^LITELLM_MASTER_KEY=sk-CHANGE-ME$' .env; then
  echo "ERROR: LITELLM_MASTER_KEY is still the placeholder. Set a real value." >&2
  exit 1
fi

# --- 3. Hard block the malicious LiteLLM versions ---------------------------
# (docs/03 risk 8) 1.82.7 / 1.82.8 shipped a credential stealer.
if grep -qE 'litellm:(v)?1\.82\.(7|8)\b' docker-compose.yaml; then
  echo "ERROR: docker-compose pins a KNOWN-MALICIOUS LiteLLM version (1.82.7/1.82.8)." >&2
  echo "       Pin a vetted 1.83.x-stable and verify its digest first." >&2
  exit 1
fi

PINNED=$(grep -E '^\s*image:\s*ghcr.io/berriai/litellm' docker-compose.yaml | head -1 | sed 's/.*litellm/litellm/')
echo "Using LiteLLM image: ${PINNED}"
echo "Reminder: confirm this exact tag/digest is vetted against LiteLLM's security guidance."

# --- 4. Up ------------------------------------------------------------------
if command -v docker &>/dev/null && docker compose version &>/dev/null; then
  docker compose up -d
  echo
  echo "Gateway starting on http://localhost:4000"
  echo "Liveness:  curl http://localhost:4000/health/liveliness"
  echo "Models:     curl -H \"Authorization: Bearer \$LITELLM_MASTER_KEY\" http://localhost:4000/v1/models"
  echo "Logs:       docker compose logs -f litellm"
else
  echo "ERROR: 'docker compose' not available. Install Docker, or run LiteLLM directly:" >&2
  echo "       pip install 'litellm[proxy]==<vetted-version>' && litellm --config litellm-config.yaml --port 4000" >&2
  exit 1
fi
