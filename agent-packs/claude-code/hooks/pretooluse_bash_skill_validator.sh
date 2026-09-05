#!/usr/bin/env bash
# AQG — PreToolUse(Bash matcher) skill-validator gate.
#
# 触发: Claude 准备跑 Bash 工具时调用 (matcher 在 settings.json 控制).
# 作用: 如果 `git commit` 且 staged 改动含 `skills/aqg-X/SKILL.md` 或
#       `scripts/install.sh`, 跑 aqg-skill-validator; 失败则 exit 2 阻塞 commit.
#
# 设计原则 (与 dangerous-guard.example.sh 一致):
# - 输入: PreToolUse JSON payload via stdin (Claude Code spec)
# - 输出: stderr human-readable; exit 0 allow / 2 block (PreToolUse deny code; exit 1
#   is NON-blocking — the tool proceeds. 与 dangerous-guard.example.sh 的 2=block 一致)
# - boundary: AQG_ROOT 未设 → silent exit 0 (不阻塞 Claude)
# - boundary: python3 不在 → silent exit 0 (degraded)
# - boundary: cwd 非 git → silent exit 0
# - boundary: staged 不含 AQG skill → silent exit 0

set -uo pipefail

input="$(cat 2>/dev/null || true)"
if [ -z "$input" ]; then
  exit 0
fi

if [ -z "${AQG_ROOT:-}" ]; then
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  exit 0
fi

# Parse tool_input.command from stdin JSON.
cmd=$(printf '%s' "$input" | python3 -c '
import json, sys
try:
    d = json.loads(sys.stdin.read())
    ti = d.get("tool_input", {}) or {}
    print(ti.get("command", ""))
except Exception:
    pass
' 2>/dev/null)

# Audit gpt-5.5 #1 fix: detect `git commit` after optional env-prefix (`FOO=bar git commit`)
# and optional `git -C <dir>` (`git -C /repo commit`). `([[:space:]]|$)` replaces `\b`
# for POSIX ERE portability (macOS BSD grep vs GNU grep). Conservative scanner — finds
# `git commit` after command start or any separator (`;`, `&&`, `||`, newline) preceded
# by optional env assignments.
#   match: `git commit`, `FOO=bar BAR=baz git commit`, `git -C dir commit`,
#          `cd dir && git commit`, `git add x && git commit`
#   miss: `gitcommit` (no space), `git    commit -m "git commit X"` (we only match first occurrence; fine)
if ! printf '%s' "$cmd" | grep -qE '(^|[[:space:];&|]|&&|\|\|)([[:space:]]*[A-Za-z_][A-Za-z0-9_]*=[^[:space:]]*)*[[:space:]]*git([[:space:]]+-C[[:space:]]+[^[:space:]]+)?[[:space:]]+commit([[:space:]]|$)'; then
  exit 0
fi

project_dir="${CLAUDE_PROJECT_DIR:-$PWD}"
if ! git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  exit 0
fi

# Audit gpt-5.5 #2 fix: also scan working tree when commit form stages at commit time.
# `git commit -a` / `--all` adds tracked-file mods to index AT COMMIT TIME, after the
# PreToolUse hook fires. Multi-cmd chain `git add X && git commit` similarly defers.
# Authoritative gate remains `pre_commit_construction.sh` (git pre-commit level, sees
# final index). This hook is early-warning. We expand coverage by scanning unstaged
# changes when commit form indicates late staging.
staged=$(git -C "$project_dir" diff --cached --name-only 2>/dev/null || true)
unstaged=""
if printf '%s' "$cmd" | grep -qE '[[:space:]](-a|--all)([[:space:]]|$)|[[:space:]]git[[:space:]]+add[[:space:]]'; then
  # `-a` / `--all` adds tracked-file working-tree mods; also handle pre-staging via `git add`.
  unstaged=$(git -C "$project_dir" diff --name-only HEAD 2>/dev/null || true)
fi
combined="$(printf '%s\n%s\n' "$staged" "$unstaged" | sort -u | sed '/^$/d')"
if [ -z "$combined" ]; then
  exit 0
fi

# Identify affected AQG skills (skills/aqg-*/...) and install.sh touches.
# Uses `combined` (staged + unstaged if `-a`/`git add` form). Paths with spaces are
# rare in this repo; `awk -F'/' '{print $2}'` is safe for normal slugs.
affected_skills=$(printf '%s\n' "$combined" \
  | grep -E '^skills/aqg-[^/]+/' \
  | awk -F'/' '{print $2}' \
  | sort -u)
install_sh_touched=$(printf '%s\n' "$combined" | grep -cE '^scripts/install\.sh$' || true)
install_sh_touched="${install_sh_touched:-0}"

if [ -z "$affected_skills" ] && [ "$install_sh_touched" = "0" ]; then
  exit 0
fi

echo "[aqg pretooluse-skill-validator] staged change touches AQG skill scope" >&2

script="$AQG_ROOT/scripts/aqg_skill_validator.py"
if [ ! -f "$script" ]; then
  echo "[aqg pretooluse-skill-validator] WARN: validator missing at $script; skipping" >&2
  exit 0
fi

fail=0
if [ -n "$affected_skills" ]; then
  while IFS= read -r skill; do
    [ -z "$skill" ] && continue
    echo "[aqg pretooluse-skill-validator] validating skill: $skill" >&2
    if ! python3 "$script" "$skill" >&2; then
      fail=1
    fi
  done <<< "$affected_skills"
fi

if [ "$install_sh_touched" != "0" ]; then
  echo "[aqg pretooluse-skill-validator] WARN: scripts/install.sh modified; re-run \`python3 scripts/aqg_doctor.py\` to verify cross-cutting registration" >&2
fi

if [ "$fail" -ne 0 ]; then
  echo "[aqg pretooluse-skill-validator] BLOCK: skill validator reported violations; fix and recommit (or AQG_AGENT=human-opt-in to bypass)" >&2
  # Phase 1: respect AQG_AGENT=human-opt-in escape (per existing construction hook convention).
  if [ "${AQG_AGENT:-}" = "human-opt-in" ]; then
    echo "[aqg pretooluse-skill-validator] AQG_AGENT=human-opt-in set; downgrading to warn" >&2
    exit 0
  fi
  # Deny: PreToolUse blocks a tool call with exit 2 (stderr fed to the model). Exit 1
  # is a NON-blocking error — the commit would proceed. #329 confirmed this on Claude
  # Code 2.1.185, so this gate was dormant while it used exit 1.
  exit 2
fi

echo "[aqg pretooluse-skill-validator] skill validator passed" >&2
exit 0
