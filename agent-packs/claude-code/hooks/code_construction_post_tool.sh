#!/usr/bin/env bash
# AQG Code Construction — Claude Code PostToolUse hook wrapper.
#
# Per sketch a1 audit gemini #1 fix (option c) + D1 (Codex post-hoc weakening):
# - Enforces only when AQG_AGENT env var set (silent pass if missing).
# - PostToolUse fires after Edit/Write tools; we run the checker against
#   worktree (uncommitted) diff for real-time process discipline (advantage
#   over Codex which only has commit-time discipline).
#
# Boundary: warn-only output to stderr (PostToolUse hook output goes to
# Claude context window). Does not block tool execution; the pre-commit
# hook is the hard gate.

set -uo pipefail

# PR-B audit gpt #6 fix: surface setup miss visibly (not silent) so user
# knows AQG isn't actually running. Still exit 0 to never block tool execution.
if [ -z "${AQG_ROOT:-}" ]; then
  echo "AQG inactive: AQG_ROOT not set; PostToolUse skipped (this is OK if AQG not used in this repo, otherwise: export AQG_ROOT=/path/to/aqg)" >&2
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "AQG inactive: python3 not found; PostToolUse skipped" >&2
  exit 0
fi

# Set AQG_AGENT for this hook subprocess (Claude doesn't propagate env across calls)
export AQG_AGENT="${AQG_AGENT:-claude}"

script="$AQG_ROOT/skills/aqg-code-construction/scripts/aqg_construction_check.py"
if [ ! -f "$script" ]; then
  exit 0
fi

# Pin to project dir if Claude passes it as $1
project_dir="${1:-${CLAUDE_PROJECT_DIR:-$PWD}}"

# Run in worktree mode; warn-only on hook so Claude sees output but doesn't fail
python3 "$script" --mode worktree --cwd "$project_dir" --quiet 2>&1 || true
exit 0
