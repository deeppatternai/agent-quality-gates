#!/usr/bin/env bash
# run_warn_only.sh — Claude Code Stop hook 的 warn-only 实现
#
# 设计目标（应对原 hook example 的 6 个 precondition 全塞在一行 JSON 的痛点）：
#   1. 每个 precondition 单独检查；任一失败 → stderr 输出 1 行 hint 后退出 0
#   2. 退出码始终 0（永不阻塞 Claude Code 会话），warn-only 语义彻底
#   3. 把 redacted JSON 写到 mktemp 临时文件，路径由用户在日志里看到
#   4. 任何子命令失败也不向上传播（`|| true` + 末尾 exit 0）
#
# 调用约定：
#   bash run_warn_only.sh [project_dir]
# project_dir 默认取 $CLAUDE_PROJECT_DIR；没传也没设 → 静默 hint + exit 0。
#
# 必需环境：
#   AQG_ROOT       — Agent Quality Gates checkout 绝对路径
#   CLAUDE_PROJECT_DIR — 当前 Claude Code 项目目录（hook caller 注入）
#
# 期望 project_dir 包含：
#   git worktree             — 可为普通 checkout、linked worktree 或 submodule
#   quality-gates.json       — AQG 配置
#   .aqg/pr-body.md          — 本地"PR body" markdown（用户提供）

# 故意不开 set -e：单个 precondition 失败应继续走到 hint + exit 0
set -u

project_dir="${1:-${CLAUDE_PROJECT_DIR:-}}"

hint() {
  # 用前缀让 hook 输出可识别
  printf '[aqg warn-only hook] %s\n' "$*" >&2
}

if [ -z "${AQG_ROOT:-}" ]; then
  hint "skip: AQG_ROOT is not set; export it to your agent-quality-gates checkout to enable the warn-only hook"
  exit 0
fi
if [ -z "$project_dir" ]; then
  hint "skip: CLAUDE_PROJECT_DIR is unset and no project dir argument was provided"
  exit 0
fi
if ! git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  hint "skip: $project_dir is not a git worktree"
  exit 0
fi

run_script="$AQG_ROOT/scripts/run_quality_gates.py"
if [ ! -f "$run_script" ]; then
  hint "skip: $run_script not found; check AQG_ROOT points at a real checkout"
  exit 0
fi

config="$project_dir/quality-gates.json"
if [ ! -f "$config" ]; then
  hint "skip: $config not found; copy examples/quality-gates.json into the project root to enable gates"
  exit 0
fi

body="$project_dir/.aqg/pr-body.md"
if [ ! -f "$body" ]; then
  hint "skip: $body not found; create it with your PR body content (and add .aqg/ to .gitignore to avoid leaking PR text)"
  exit 0
fi

# 全部 precondition 满足，跑 gates 把 redacted JSON 写到 mktemp。
# 退出码不向上传播（`|| true`），warn-only 永不阻塞会话。
out_template="${TMPDIR:-/tmp}/aqg-claude-warn-only.XXXXXX.json"
out="$(mktemp "$out_template" 2>/dev/null || mktemp)"
hint "running gates -> $out"
python3 "$run_script" \
  --config "$config" \
  --repo "$project_dir" \
  --pr-body-file "$body" \
  --output-json "$out" \
  >&2 2>&1 || true

exit 0
