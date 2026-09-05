#!/usr/bin/env bash
# AQG WIP recover — Claude Code SessionStart hook wrapper.
#
# 三审 14 accepted findings (audit_id 656913cb) 已纳入：
# - python3 missing → silent exit 0 (gemini #3)
# - AQG_ROOT 缺失 → silent exit 0 (gemini #1, settings.wip.example.json 也 inline check)
# - 永不 fail Claude Code (warn-only spirit)

set -uo pipefail

if [ -z "${AQG_ROOT:-}" ]; then
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

# Optional first arg = CLAUDE_PROJECT_DIR; export so wip_recover.py can
# filter snapshots by the real project dir instead of the hook runtime cwd.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  export AQG_HOOK_PROJECT_DIR="$1"
fi

exec python3 "$AQG_ROOT/scripts/wip_recover.py"
