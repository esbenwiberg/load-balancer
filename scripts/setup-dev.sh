#!/usr/bin/env bash
# =============================================================================
# One-time dev setup:
#   1. Point git at the checked-in hooks (core.hooksPath = .githooks), so the
#      fast-tier pre-commit runs without anyone symlinking anything.
#   2. Make the hooks and scripts executable.
#   3. Report which check tools are present and which are missing (missing ones
#      warn-and-skip in check.sh — this just tells you what you're not covering
#      locally). CI installs all of them.
#
#   ./scripts/setup-dev.sh
# =============================================================================
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

git config core.hooksPath .githooks
chmod +x .githooks/* scripts/*.sh 2>/dev/null || true
echo "✓ core.hooksPath → .githooks  (pre-commit runs: scripts/check.sh --fast)"

echo
echo "check-tool availability (missing → warn-and-skip in check.sh):"
# tool:install-hint pairs
for pair in \
  "ruff:pip install ruff" \
  "shellcheck:brew install shellcheck" \
  "gitleaks:brew install gitleaks" \
  "docker:https://docs.docker.com/get-docker/ (needed for --full e2e)" \
  "python3:system python3"; do
  tool="${pair%%:*}"
  hint="${pair#*:}"
  if command -v "$tool" >/dev/null 2>&1; then
    printf '  ✓ %-11s %s\n' "$tool" "$(command -v "$tool")"
  else
    printf '  ⚠ %-11s MISSING — %s\n' "$tool" "$hint"
  fi
done

echo
echo "Done. Sanity-check the fast tier now with:  ./scripts/check.sh --fast"
