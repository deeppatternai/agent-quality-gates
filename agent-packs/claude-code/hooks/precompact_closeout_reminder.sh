#!/usr/bin/env bash
# AQG — PreCompact / Stop closeout + handoff reminder.
#
# 触发: Claude session 进入 PreCompact (即将压缩 transcript) 或 Stop (session 结束).
# 作用: stderr 提示 (warn-only Phase 1) —
#       (a) evidence-closeout 6 问 (任务做完了 → 回顾给证据);
#       (b) aqg-session-handoff (任务没完 + context 将丢 → 给下一棒前瞻交接;
#           被动 compaction / CTX 紧张没 budget → 指向 new --minimal 降级 3 段).
#       与 wip_save.sh 互补: wip_save 持久化 WIP state, 本 hook 提醒答闭环 + 留交接.
# Boundary: never block; always exit 0.
#
# Phase 2 (future): scan transcript_path 文件，自动检测 ledger keyword
# ("closeout" / "evidence" / "smoke pass" / "已 merge") 是否出现; 缺 → 升级提示
# 但仍不 block (Anthropic 设计上 Stop hook 不能 block, PreCompact 也不应 block).

set -uo pipefail

# transcript_path 从 stdin JSON 取 (Phase 2 用); Phase 1 不读, 总是 emit reminder.
# stdin 读完丢弃避免管道阻塞.
cat >/dev/null 2>&1 || true

echo "[aqg closeout-reminder] session compact / stop incoming" >&2
echo "[aqg closeout-reminder] reminder: aqg-evidence-closeout 6 问已答?" >&2
echo "  1. scope completed      — 这次改了什么 (file paths + intent)" >&2
echo "  2. verification run     — 跑了哪些 fresh check (commands + outputs)" >&2
echo "  3. audit adjudicated    — 含 audit findings 的决定 + 行动" >&2
echo "  4. durable state updated — memory / docs / repos 推上没" >&2
echo "  5. production boundary  — 没碰 prod / secrets / Owner-admin (列举边界)" >&2
echo "  6. remaining blockers   — 还差什么 / 下一步是什么 (point to actor)" >&2
echo "[aqg closeout-reminder] 调 aqg-evidence-closeout skill OR write inline ledger before handoff" >&2
echo "[aqg handoff-reminder] 若任务未完 且 context 将丢 → 给下一棒留 paste-ready handoff:" >&2
echo "  有 budget → aqg-session-handoff new → 填 8 段 → validate (stdin) → 粘进下个 window" >&2
echo "  CTX 紧张 / 被动 compaction (没 budget 走完整流程) → aqg-session-handoff new --minimal" >&2
echo "    → 填 3 段 (使命/🟡停点/第一步) → validate --minimal (secret-scan 仍跑) → 粘" >&2
echo "  区分: closeout = 完工回顾 (给证据); handoff = 没完工时给下一棒的前瞻交接" >&2
exit 0
