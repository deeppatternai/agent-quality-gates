#!/usr/bin/env bash
# AQG WIP checkpoint save — Claude Code Stop / PreCompact hook wrapper.
#
# 把当前 worktree 改动(含 untracked)快照到 refs/aqg-wip/<session>,**零碰**
# 工作树 / index / 当前分支 / stash。clean → skip;tree 未变 → 去重 skip。
# checkpoint 只躺本地 object store,refs/aqg-wip/* 不在 refs/heads/* → git push
# 默认不推(本地宽外向严)。
#
# 永不 fail Claude Code(warn-only spirit):
# - AQG_ROOT 缺失 → silent exit 0
# - python3 缺失 → silent exit 0
# - save NEVER prints stdout(Stop/PreCompact stdout 会注入 context)

set -uo pipefail

if [ -z "${AQG_ROOT:-}" ]; then
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

# Optional first arg = CLAUDE_PROJECT_DIR; export so wip_checkpoint can pin to
# the real project dir instead of the hook's runtime cwd.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  export AQG_HOOK_PROJECT_DIR="$1"
fi

exec python3 "$AQG_ROOT/scripts/wip_checkpoint.py" save
