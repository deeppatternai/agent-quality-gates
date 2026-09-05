#!/usr/bin/env bash
# AQG Ledger v2 — Stop capture wrapper.
#
# Captures git commits made in the session window (committer-date >= the
# SessionStart anchor, reachable from HEAD, --no-merges) and emits one
# low-fidelity `aqg-hook` progress event per commit into the LOCAL ledger inbox
# (never network/cloud). Opt-in (settings.ledger.example.json). NEVER fails
# Claude Code:
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

exec python3 "$AQG_ROOT/scripts/ledger_hook_writer.py" --mode capture
