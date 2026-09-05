#!/usr/bin/env bash
# AQG Code Construction — git pre-commit hook.
#
# Per sketch a1 audit gemini #1 fix (option c): silent pass unless AQG_AGENT
# set in {codex, claude, human-opt-in, ci-rerun}. This makes humans
# transparent (no friction) while AI agent commits are fail-closed.
#
# Per audit D4 fix: invoked via core.hooksPath managed by
# install_aqg_construction_hook.py (not bypassable via --no-verify silently;
# git pre-commit standard behavior preserved — --no-verify still works as
# escape hatch but the user explicitly chose to bypass).
#
# Exit codes (forwarded from checker):
#   0 OK / pass / silent (AQG_AGENT not set)
#   1 generic check failure (fix and rerun)
#   2 secret detected in ledger (CRITICAL — redact before commit)
#   3 ledger missing or malformed
#   4 size mismatch (declared path vs actual diff)
#   5 anti-pattern hard block
#   6 objection malformed
#   70 usage error

set -uo pipefail

# gemini #1 option c: silent pass when AQG_AGENT not set (human transparent)
if [ -z "${AQG_AGENT:-}" ]; then
  exit 0
fi

if [ -z "${AQG_ROOT:-}" ]; then
  echo "AQG_AGENT set but AQG_ROOT missing; install AQG before committing" >&2
  exit 1
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "python3 missing; cannot run AQG construction check" >&2
  exit 1
fi

script="$AQG_ROOT/skills/aqg-code-construction/scripts/aqg_construction_check.py"
if [ ! -f "$script" ]; then
  echo "AQG checker missing at $script" >&2
  exit 1
fi

repo_root="$(git rev-parse --show-toplevel 2>/dev/null)"
if [ -z "$repo_root" ]; then
  exit 0  # not in a git repo (e.g., bare git init mid-setup)
fi

# Pre-commit always uses --mode=staged
exec python3 "$script" --mode staged --cwd "$repo_root"
