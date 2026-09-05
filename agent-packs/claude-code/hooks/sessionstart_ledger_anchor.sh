#!/usr/bin/env bash
# AQG Ledger v2 — SessionStart capture-anchor wrapper.
#
# Records {baseline_sha, session_start_time} so the Stop capture knows the
# session window. Opt-in (copy settings.ledger.example.json into settings.json).
# NEVER fails Claude Code:
# - AQG_ROOT unset / python3 absent / git absent → silent exit 0
# - the writer itself always exits 0 (best-effort capture, never a gate)

set -uo pipefail

if [ -z "${AQG_ROOT:-}" ]; then
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi
if ! command -v git >/dev/null 2>&1; then
  exit 0
fi

# Optional first arg = CLAUDE_PROJECT_DIR; export so the writer pins to the real
# project dir instead of the hook's runtime cwd (wip_save pattern).
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  export AQG_HOOK_PROJECT_DIR="$1"
fi

exec python3 "$AQG_ROOT/scripts/ledger_hook_writer.py" --mode anchor
