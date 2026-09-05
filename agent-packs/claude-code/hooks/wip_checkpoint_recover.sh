#!/usr/bin/env bash
# AQG WIP checkpoint recover — Claude Code SessionStart hook wrapper.
#
# 列 refs/aqg-wip/* 下未恢复的 checkpoint,GC 超龄/已 finalize 的,打印恢复提示
# (计数 + 命令模板,不含 raw 文件名/diff/secret)到 stdout 给 Claude 看。
# **只提示,绝不自动改工作树** —— 用户决定何时 `git checkout <ref> -- .`。
#
# 永不 fail Claude Code(warn-only spirit):AQG_ROOT / python3 缺失 → silent exit 0。

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

exec python3 "$AQG_ROOT/scripts/wip_checkpoint.py" recover
