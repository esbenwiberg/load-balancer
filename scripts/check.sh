#!/usr/bin/env bash
# =============================================================================
# scripts/check.sh — the ONE definition of "green" for this repo.
#
# Called from three places so the bar can never drift between them:
#   - .githooks/pre-commit        (fast tier, wired by scripts/setup-dev.sh)
#   - .claude/settings.json Stop  (fast tier)
#   - CI / goal 1                 (full tier)
#
# Tiers:
#   --fast (default)  ruff lint + format-check, shellcheck, `docker compose
#                     config` validation for every compose file (NO
#                     containers), conformance/selftest.py, gitleaks secret
#                     scan. Seconds; needs no docker daemon.
#   --full            everything in --fast, then e2e/run.sh (the docker stack).
#
# Contract:
#   - A MISSING tool WARNS and is SKIPPED (yellow) — it never fails the run, so
#     a dev without gitleaks can still commit. CI installs everything, so there
#     nothing is skipped.
#   - A PRESENT tool that finds a real problem HARD-FAILS the run (exit 1).
#   - HARD CONSTRAINT: the fast tier starts NO docker containers. `docker
#     compose config` is pure client-side YAML validation (no daemon, no
#     `up`). Slow hooks train people to `--no-verify`; the full e2e stack is
#     the MERGE gate (CI), never the commit gate.
#
# Written for bash 3.2 (macOS default): no mapfile, no associative arrays,
# empty-array expansions guarded for `set -u`.
# =============================================================================
set -uo pipefail   # deliberately NOT -e: run EVERY check, then report together.

TIER="fast"
for arg in "$@"; do
  case "$arg" in
    --fast) TIER="fast" ;;
    --full) TIER="full" ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^#\{1,\} \{0,1\}//; s/^#\{1,\}$//'
      exit 0 ;;
    *) echo "unknown arg: $arg (use --fast or --full)" >&2; exit 2 ;;
  esac
done

cd "$(git rev-parse --show-toplevel 2>/dev/null || dirname "$(dirname "$0")")" || exit 1
REPO_ROOT="$PWD"

# --- pretty output (disabled when not a TTY or NO_COLOR set) -----------------
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  RED=$'\033[31m'; GRN=$'\033[32m'; YEL=$'\033[33m'; BLD=$'\033[1m'; RST=$'\033[0m'
else
  RED=""; GRN=""; YEL=""; BLD=""; RST=""
fi

FAILURES=()
SKIPS=()
step() { printf '\n%s=== %s ===%s\n' "$BLD" "$1" "$RST"; }
ok()   { printf '%s  ✓ %s%s\n'      "$GRN" "$1" "$RST"; }
fail() { printf '%s  ✗ %s%s\n'      "$RED" "$1" "$RST"; FAILURES+=("$1"); }
skip() { printf '%s  ⚠ SKIP %s — %s%s\n' "$YEL" "$1" "$2" "$RST"; SKIPS+=("$1: $2"); }
have() { command -v "$1" >/dev/null 2>&1; }

echo "${BLD}check.sh: ${TIER} tier @ $(git rev-parse --short HEAD 2>/dev/null || echo '?')${RST}"

# --- ruff: lint -------------------------------------------------------------
step "ruff lint (ruff check)"
if have ruff; then
  if ruff check; then ok "ruff check"; else fail "ruff check"; fi
else
  skip "ruff" "not installed — pip install ruff"
fi

# --- ruff: format (check only, never rewrites) ------------------------------
step "ruff format --check"
if have ruff; then
  if ruff format --check; then ok "ruff format"; else fail "ruff format (fix with: ruff format)"; fi
else
  skip "ruff" "not installed — pip install ruff"
fi

# --- shellcheck: every shell script + the git hook --------------------------
step "shellcheck"
if have shellcheck; then
  SH=()
  while IFS= read -r f; do SH+=("$f"); done < <(
    find . -path ./.venv-e2e -prune -o -path ./.git -prune -o -name '*.sh' -print | sort)
  [ -f .githooks/pre-commit ] && SH+=(".githooks/pre-commit")
  # -e SC1091: our scripts `source` runtime-only env files (.env.cliauth etc.)
  # that don't exist at lint time — "can't follow sourced file" is inherent, not
  # a defect. Everything else stays at full strictness.
  if [ "${#SH[@]}" -eq 0 ]; then
    ok "no shell scripts found"
  elif shellcheck -e SC1091 "${SH[@]}"; then
    ok "shellcheck (${#SH[@]} files)"
  else
    fail "shellcheck"
  fi
else
  skip "shellcheck" "not installed — brew install shellcheck"
fi

# --- docker compose config: schema validation, NO containers ----------------
step "docker compose config (all compose files — NO containers started)"
if have docker; then
  CF=()
  while IFS= read -r f; do CF+=("$f"); done < <(
    find . -path ./.venv-e2e -prune -o -path ./.git -prune -o \
      \( -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' \
         -o -name 'compose*.yml' -o -name 'compose*.yaml' \) -print | sort)
  if [ "${#CF[@]}" -eq 0 ]; then
    ok "no compose files found"
  else
    # --no-interpolate: validate STRUCTURE without demanding real secrets or
    # gitignored .env files. Trade-off: variable interpolation itself is not
    # exercised (that needs the real env, i.e. the runtime path), but YAML /
    # schema mistakes are still caught — which is the point of a fast gate.
    for f in "${CF[@]}"; do
      if out="$(docker compose -f "$f" config -q --no-interpolate 2>&1)"; then
        ok "config: $f"
      else
        fail "config: $f"
        printf '%s\n' "$out" | sed 's/^/      /'
      fi
    done
  fi
else
  skip "docker" "not installed — compose files not validated"
fi

# --- litellm image pin guard (docs/03 risk 8 — the malware tags) ------------
# 1.82.7/1.82.8 shipped credential-stealing malware; the bridge needs a vetted
# 1.83.x-stable. That pin was enforced ONLY by eyeball. Here it's machine-checked
# everywhere check.sh runs (pre-commit, Stop hook, CI): every ACTIVE reference to
# the litellm image across compose AND bicep files must be EXACTLY the vetted tag.
# Comments (the digest-example line, bicep `//` notes) and doc prose (the malware
# warnings) are not image references, so they're ignored. If you deliberately move
# the pin — a new vetted stable, or a verified @sha256 digest — bump VETTED_LITELLM
# here in the SAME change, so the guard, the compose files, and the Azure IaC
# (deploy/azure/modules/gateway.bicep default) can never silently diverge.
step "litellm image pin (docs/03 risk 8 — never 1.82.7/1.82.8)"
VETTED_LITELLM="ghcr.io/berriai/litellm:v1.83.14-stable"
PINF=()
while IFS= read -r f; do PINF+=("$f"); done < <(
  find . -path ./.venv-e2e -prune -o -path ./.git -prune -o \
    \( -name 'docker-compose*.yml' -o -name 'docker-compose*.yaml' \
       -o -name 'compose*.yml' -o -name 'compose*.yaml' \
       -o -name '*.bicep' \) -print | sort)
if [ "${#PINF[@]}" -eq 0 ]; then
  ok "no compose files to check"
else
  PIN_BAD=0
  # file:line:content for every litellm image mention, minus comment lines
  # (YAML `#` and bicep `//`), minus the exact vetted pin -> whatever remains is
  # an offending reference.
  while IFS= read -r hit; do
    [ -z "$hit" ] && continue
    PIN_BAD=1
    printf '      offending: %s\n' "$hit"
  done < <(
    grep -nE 'ghcr\.io/berriai/litellm' "${PINF[@]}" 2>/dev/null \
      | grep -vE '^[^:]+:[0-9]+:[[:space:]]*(#|//)' \
      | grep -vF "$VETTED_LITELLM" || true)
  if [ "$PIN_BAD" -eq 0 ]; then
    ok "litellm pin ($VETTED_LITELLM)"
  else
    fail "litellm image pin deviates from vetted $VETTED_LITELLM"
  fi
fi

# --- Azure IaC: offline bicep build (goal 14 — no cloud calls, no creds) -----
# The Azure IaC skeleton (deploy/azure/*.bicep) must compile offline. `bicep
# build` transpiles Bicep -> ARM JSON purely locally: it makes NO cloud calls,
# needs NO credentials, and does NOT deploy. We build every .bicep (main + each
# module standalone) and every .bicepparam, all to stdout so no JSON artifacts
# land in the tree. Two runners are supported — standalone `bicep` (positional
# syntax) or `az bicep` (--file syntax); prefer standalone, fall back to az.
# Missing both -> skip (fast-tier contract); CI installs bicep so nothing skips.
step "bicep IaC build (deploy/azure — offline, no cloud calls, no creds)"
BICEP_KIND=""
if have bicep; then
  BICEP_KIND="standalone"
elif have az && az bicep version >/dev/null 2>&1; then
  BICEP_KIND="az"
fi
bicep_build() {        # $1 = .bicep file
  if [ "$BICEP_KIND" = "az" ]; then az bicep build --file "$1" --stdout
  else bicep build "$1" --stdout; fi
}
bicep_build_params() { # $1 = .bicepparam file
  if [ "$BICEP_KIND" = "az" ]; then az bicep build-params --file "$1" --stdout
  else bicep build-params "$1" --stdout; fi
}
if [ -z "$BICEP_KIND" ]; then
  skip "bicep" "not installed — 'az bicep install' or https://aka.ms/bicep-install"
else
  BC=()
  while IFS= read -r f; do BC+=("$f"); done < <(
    find . -path ./.venv-e2e -prune -o -path ./.git -prune -o -name '*.bicep' -print | sort)
  BP=()
  while IFS= read -r f; do BP+=("$f"); done < <(
    find . -path ./.venv-e2e -prune -o -path ./.git -prune -o -name '*.bicepparam' -print | sort)
  if [ "${#BC[@]}" -eq 0 ] && [ "${#BP[@]}" -eq 0 ]; then
    ok "no bicep files found"
  else
    BICEP_BAD=0
    for f in "${BC[@]}"; do
      if out="$(bicep_build "$f" 2>&1 >/dev/null)"; then
        # build succeeds on warnings; treat any diagnostic line as a hard fail so
        # the IaC stays lint-clean (no accidental secure-default / unused params).
        if [ -n "$out" ]; then
          BICEP_BAD=1; fail "bicep build (diagnostics): $f"
          printf '%s\n' "$out" | sed 's/^/      /'
        else
          ok "bicep build: $f"
        fi
      else
        BICEP_BAD=1; fail "bicep build: $f"
        printf '%s\n' "$out" | sed 's/^/      /'
      fi
    done
    for f in "${BP[@]}"; do
      if out="$(bicep_build_params "$f" 2>&1 >/dev/null)"; then
        if [ -n "$out" ]; then
          BICEP_BAD=1; fail "bicep build-params (diagnostics): $f"
          printf '%s\n' "$out" | sed 's/^/      /'
        else
          ok "bicep build-params: $f"
        fi
      else
        BICEP_BAD=1; fail "bicep build-params: $f"
        printf '%s\n' "$out" | sed 's/^/      /'
      fi
    done
    [ "$BICEP_BAD" -eq 0 ] && ok "bicep IaC compiles clean (offline)"
  fi
fi

# --- conformance self-test (offline, no network) ----------------------------
step "conformance/selftest.py"
if have python3; then
  if python3 conformance/selftest.py; then ok "selftest"; else fail "conformance/selftest.py"; fi
else
  skip "python3" "not installed"
fi

# --- control-plane unit tests (goal 5 — offline, stdlib only) ----------------
# The Phase-1 control-plane skeleton (e2e/control_plane.py) is stdlib-only and
# has no docker footprint, so its unit tests belong in the FAST tier (like
# selftest.py) — not gated behind the e2e docker stack. Registry state model +
# TTL decay + the HTTP wire adapter, run in-process on an ephemeral port.
step "control-plane unit tests (e2e/control_plane_test.py)"
if have python3; then
  if (cd e2e && python3 control_plane_test.py); then
    ok "control_plane_test.py"
  else
    fail "e2e/control_plane_test.py"
  fi
else
  skip "python3" "not installed"
fi

# --- dashboard fleet-view unit tests (goal 13 — offline, stdlib only) --------
# The dashboard's fleet endpoint (_fetch_fleet) shapes the control-plane registry
# and degrades gracefully when it's unreachable. Those pure/offline branches
# belong in the fast tier; the e2e stack proves the live registry->dashboard path.
step "dashboard fleet-view unit tests (e2e/dashboard_test.py)"
if have python3; then
  if (cd e2e && python3 dashboard_test.py); then
    ok "dashboard_test.py"
  else
    fail "e2e/dashboard_test.py"
  fi
else
  skip "python3" "not installed"
fi

# --- gitleaks: secret scan of the working tree ------------------------------
step "gitleaks secret scan"
if have gitleaks; then
  # --no-git scans files as they are on disk (committed OR staged OR just
  # written) — the same result in a pre-commit hook, a Stop hook, and CI.
  if gitleaks detect --source "$REPO_ROOT" --no-git --no-banner --redact; then
    ok "gitleaks (no leaks)"
  else
    fail "gitleaks — potential secret detected (see above)"
  fi
else
  skip "gitleaks" "not installed — brew install gitleaks"
fi

# --- full tier only: the docker e2e stack (the MERGE gate) ------------------
if [ "$TIER" = "full" ]; then
  step "e2e/run.sh (docker stack — the MERGE gate, never a commit gate)"
  if have docker; then
    if "$REPO_ROOT/e2e/run.sh"; then ok "e2e/run.sh"; else fail "e2e/run.sh"; fi
  else
    skip "docker" "not installed — cannot run e2e"
  fi
fi

# --- summary ----------------------------------------------------------------
step "summary (${TIER} tier)"
NSKIP="${#SKIPS[@]}"
NFAIL="${#FAILURES[@]}"
if [ "$NSKIP" -gt 0 ]; then
  for s in "${SKIPS[@]}"; do printf '%s  ⚠ skipped %s%s\n' "$YEL" "$s" "$RST"; done
fi

if [ "$NFAIL" -eq 0 ]; then
  printf '%s%s  ALL CHECKS PASSED — %s tier, %d skipped%s\n' "$BLD" "$GRN" "$TIER" "$NSKIP" "$RST"
  exit 0
else
  printf '%s%s  %d CHECK(S) FAILED:%s\n' "$BLD" "$RED" "$NFAIL" "$RST"
  for f in "${FAILURES[@]}"; do printf '%s    ✗ %s%s\n' "$RED" "$f" "$RST"; done
  exit 1
fi
