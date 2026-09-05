#!/usr/bin/env bash
# AQG Smoke Test — chains all 4 local verification layers.
#
# No API calls. No external services. < 30s on a modern machine.
# Run from anywhere inside the AQG checkout (or pass --aqg-root).
#
# Layers:
#   1. aqg_doctor.py        — symlink registration (all aqg-* skills → claude + codex)
#   2. validate_agent_pack  — pack structure, hook contracts, install.sh
#   3. run_skill_self_tests — per-skill Python helper self_test.py (standalone)
#   4. pytest tests/        — 1296+ unit/integration (hooks, validator, drift, ...)
#
# Exit: 0 = all layers passed; 1 = one or more layers failed.
# To stop at first failure: export AQG_SMOKE_FAIL_FAST=1

set -uo pipefail

# ── Resolve repo root ─────────────────────────────────────────────────────────
if [ "${1:-}" = "--aqg-root" ] && [ -n "${2:-}" ]; then
  repo_root="$(cd "$2" && pwd -P)"
  shift 2
else
  repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
fi

[ -f "$repo_root/VERSION" ] || {
  echo "ERROR: VERSION not found; not an AQG checkout: $repo_root" >&2
  exit 1
}
cd "$repo_root"

# ── Color helpers (graceful fallback when stdout is not a tty) ───────────────
if [ -t 1 ] && command -v tput >/dev/null 2>&1 && tput setaf 1 >/dev/null 2>&1; then
  C_GREEN="$(tput setaf 2)"
  C_RED="$(tput setaf 1)"
  C_BOLD="$(tput bold)"
  C_RESET="$(tput sgr0)"
else
  C_GREEN="" C_RED="" C_BOLD="" C_RESET=""
fi

SEP="──────────────────────────────────────────────────────────────"

_pass() { printf "${C_GREEN}  ✓ PASS${C_RESET}  %s\n" "$*"; }
_fail() { printf "${C_RED}  ✗ FAIL${C_RESET}  %s\n" "$*"; }
_step() { printf "\n${C_BOLD}[%s/4] %s${C_RESET}\n" "$1" "$2"; }

# Skip `claude mcp list` subprocess in all child processes (inventory.py uses this).
# The MCP self-test path is covered by self_test.py's own subprocess stub.
export AQG_SKIP_MCP=1

fail_fast="${AQG_SMOKE_FAIL_FAST:-0}"
failed=0
failed_layers=()
total_start=$(date +%s)

printf "${C_BOLD}AQG Smoke Test${C_RESET}  (repo: %s)\n" "$repo_root"
printf "%s\n" "$SEP"

# ── Layer 1: Doctor check ─────────────────────────────────────────────────────
_step 1 "Doctor check — symlink registration"
t=$(date +%s)
if out="$(python3 scripts/aqg_doctor.py 2>&1)"; then
  counts=$(printf '%s' "$out" | grep "^Summary:" | head -1 || true)
  _pass "${counts:-all doctor checks passed}"
else
  _fail "doctor check"
  printf '%s\n' "$out" | tail -20 | sed 's/^/        /'
  failed=$((failed + 1))
  failed_layers+=("doctor")
  [ "$fail_fast" = "1" ] && { printf "\n%s\n${C_RED}${C_BOLD}FAIL FAST — stopped at layer 1${C_RESET}\n" "$SEP"; exit 1; }
fi
printf "      %.0fs\n" "$(( $(date +%s) - t ))"

# ── Layer 2: Agent pack validation ───────────────────────────────────────────
_step 2 "Agent pack validation — structure + hook contracts"
t=$(date +%s)
if out="$(python3 scripts/validate_agent_pack.py --agent claude-code --pack agent-packs/claude-code 2>&1)"; then
  skill_count=$(printf '%s' "$out" | grep -c "^OK skill" || true)
  hook_count=$(printf '%s' "$out" | grep -c "^OK hook" || true)
  _pass "${skill_count} skills + ${hook_count} hook examples validated"
else
  _fail "agent pack validation"
  printf '%s\n' "$out" | sed 's/^/        /'
  failed=$((failed + 1))
  failed_layers+=("validate_agent_pack")
  [ "$fail_fast" = "1" ] && { printf "\n%s\n${C_RED}${C_BOLD}FAIL FAST — stopped at layer 2${C_RESET}\n" "$SEP"; exit 1; }
fi
printf "      %.0fs\n" "$(( $(date +%s) - t ))"

# ── Layer 3: Skill self-tests ─────────────────────────────────────────────────
_step 3 "Skill self-tests — each skill × scripts/self_test.py"
t=$(date +%s)
if out="$(bash scripts/run_skill_self_tests.sh 2>&1)"; then
  summary=$(printf '%s' "$out" | grep "^skill self-tests:" | head -1 || true)
  _pass "${summary:-all skill self-tests passed}"
else
  _fail "skill self-tests"
  printf '%s\n' "$out" | grep -E "^\s*FAIL|^failed:" | sed 's/^/        /'
  failed=$((failed + 1))
  failed_layers+=("skill_self_tests")
  [ "$fail_fast" = "1" ] && { printf "\n%s\n${C_RED}${C_BOLD}FAIL FAST — stopped at layer 3${C_RESET}\n" "$SEP"; exit 1; }
fi
printf "      %.0fs\n" "$(( $(date +%s) - t ))"

# ── Layer 4: Full pytest suite ────────────────────────────────────────────────
_step 4 "Full test suite — pytest tests/"
t=$(date +%s)
if out="$(python3 -m pytest tests/ -q --tb=short 2>&1)"; then
  summary=$(printf '%s' "$out" | tail -1)
  _pass "$summary"
else
  _fail "full test suite"
  # Show last 30 lines (includes failures + summary)
  printf '%s\n' "$out" | tail -30 | sed 's/^/        /'
  failed=$((failed + 1))
  failed_layers+=("pytest")
  [ "$fail_fast" = "1" ] && { printf "\n%s\n${C_RED}${C_BOLD}FAIL FAST — stopped at layer 4${C_RESET}\n" "$SEP"; exit 1; }
fi
printf "      %.0fs\n" "$(( $(date +%s) - t ))"

# ── Summary ───────────────────────────────────────────────────────────────────
total_elapsed=$(( $(date +%s) - total_start ))
printf "\n%s\n" "$SEP"

if [ "$failed" -eq 0 ]; then
  printf "${C_GREEN}${C_BOLD}ALL 4 LAYERS PASSED${C_RESET}  (%ds)\n" "$total_elapsed"
  exit 0
else
  printf "${C_RED}${C_BOLD}%d LAYER(S) FAILED${C_RESET}  (%ds)  — %s\n" \
    "$failed" "$total_elapsed" "${failed_layers[*]}"
  exit 1
fi
