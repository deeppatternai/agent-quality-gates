#!/usr/bin/env bash
# AQG — SessionStart preflight auto-run + reminder.
#
# 触发: Claude session 启动时.
# 作用: 在 git repo cwd 下自动跑 aqg-startup-preflight; 把决策摘要经
#       hookSpecificOutput.additionalContext (JSON) 注入 Claude context. 关键: 走 JSON
#       不走纯文本 stdout —— 纯文本会污染 `--input-format stream-json` / headless claude
#       的 JSON 事件流 (摘要被当畸形事件行 → session abort 无 init/assistant/result); JSON
#       由 hook-output 层解析、注入在事件流之外, 交互 + stream-json 两模式都安全.
#       meta/reminder 框架行仍走 stderr (与 wip_recover.sh 的通道约定一致).
# Boundary: 总是 exit 0 (SessionStart 不能 block); 摘要 cap 到 ~40 行避免噪声.
#
# 与 wip_recover.sh 互补: wip_recover 拉 WIP markdown, 本 hook 给 fresh git/GH state.

set -uo pipefail

project_dir="${CLAUDE_PROJECT_DIR:-$PWD}"

# python3 is checked FIRST and is the only hard exit left, because the single JSON
# emitter at the bottom IS a python3 heredoc: without it there is no model-visible
# channel at all and nothing to salvage.
if ! command -v python3 >/dev/null 2>&1; then
  echo "[aqg session-preflight] skip: python3 not available" >&2
  exit 0
fi

# The other three conditions used to `exit 0` right here, which meant the discipline
# lines below could never reach a session that was not a git worktree, had no
# AQG_ROOT, or had an incomplete install -- the cases where an agent is MOST likely
# to be flying blind. They are now STATES passed to the emitter, not exits.
#
# They are not separate emitters either. A SessionStart hook process may write
# exactly ONE JSON object: every reader in this repo does `json.loads(proc.stdout)`
# over the whole stream, and two objects concatenate to `{...}{...}`, which raises
# `Extra data`. A second emitter would therefore not add the discipline -- it would
# destroy the preflight summary that already works. Four auditors converged on this
# (aud_oqv59VQ00_EhRJkU); it was also reproduced directly before they returned.
script=""
preflight_state="ok"
if [ -z "${AQG_ROOT:-}" ]; then
  preflight_state="no-aqg-root"
  echo "[aqg session-preflight] skip preflight: AQG_ROOT not set (export AQG_ROOT=/path/to/agent-quality-gates to enable)" >&2
elif ! git -C "$project_dir" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  preflight_state="not-a-worktree"
  echo "[aqg session-preflight] skip preflight: $project_dir not a git worktree (preflight runs on git repos only)" >&2
else
  script="$AQG_ROOT/skills/aqg-startup-preflight/scripts/aqg_preflight.py"
  if [ ! -f "$script" ]; then
    preflight_state="script-missing"
    echo "[aqg session-preflight] skip preflight: script missing at $script" >&2
  fi
fi

echo "[aqg session-preflight] auto-running aqg-startup-preflight on $project_dir" >&2
echo "[aqg session-preflight] reminder: production / secrets / Owner-admin actions remain separate authorization gates" >&2

# Audit gpt-5.5 #5 fix: bound preflight to 20s via Python subprocess.run(timeout=).
# Shell `|| true` only catches non-zero exit, not hangs. Preflight does `git fetch`
# + GH live state lookups which can block on slow remote or credential prompt.
# Subprocess timeout kills the whole tree; never blocks the session.
# Output cap also done in Python (drops the shell `head -50` race on partial captures).
#
# The summary is emitted as JSON (hookSpecificOutput.additionalContext) by the block
# below — NOT plain stdout — so it injects into context WITHOUT corrupting stream-json
# / headless claude (a SessionStart hook's plain-text stdout is read as a malformed
# event line and aborts the session; see the block comment). The two meta echoes above
# stay on stderr (framing noise, redundant with preflight's authorization_scope line).
python3 - "$script" "$project_dir" "$preflight_state" "${AQG_ROOT:-}" <<'PYEOF'
import json
import pathlib
import subprocess
import sys


class _SkipPreflight(Exception):
    """Not an error — the shell already decided the preflight cannot run."""


SCRIPT, PROJECT_DIR, PREFLIGHT_STATE, AQG_ROOT = sys.argv[1:5]
TIMEOUT_S = 20
MAX_LINES = 50

parts = []
if PREFLIGHT_STATE != "ok":
    # Say so in the CONTEXT channel, not only on stderr: a session told nothing at
    # all reads as a session that was checked and found clean.
    parts.append(
        "[aqg session-preflight] preflight did not run "
        f"({PREFLIGHT_STATE}) — worktree and GitHub state are UNKNOWN, not clean."
    )
try:
    if PREFLIGHT_STATE != "ok":
        raise _SkipPreflight
    proc = subprocess.run(
        # --force: this SessionStart run is the per-session authority (WS-8 dedup) --
        # always run + refresh the marker so a rapid session restart is not deduped by
        # a prior session; the redundant rule-driven skill invocation is what dedups.
        # --project-root PROJECT_DIR: key the dedup marker on the SAME resolved root the
        # rule-driven skill will use (cwd git-root), so the two agree and dedup matches.
        [sys.executable, SCRIPT, "--project-root", PROJECT_DIR, "--repo", PROJECT_DIR, "--force"],
        timeout=TIMEOUT_S,
        capture_output=True,
        text=True,
        # Audit 7f7b8a48 claude f2: errors="replace" so non-UTF-8 repo output (e.g. a
        # git status with non-UTF-8 filenames) degrades to replacement chars instead of
        # raising UnicodeDecodeError (⊂ ValueError) — which the except below would
        # otherwise swallow, discarding the ENTIRE summary (and letting a crafted repo
        # suppress all preflight warnings).
        errors="replace",
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    lines = out.splitlines()
    parts.extend(lines[:MAX_LINES])
    if len(lines) > MAX_LINES:
        parts.append(f"[aqg session-preflight] ... (truncated; {len(lines)} total lines; run preflight manually for full)")
    # Audit 046e4118 f1: a non-zero exit with no output would otherwise read as a
    # clean empty summary, leaving the session blind. Surface it in the context
    # channel; the shell-level `exit 0` guarantee below is unchanged.
    if proc.returncode != 0:
        parts.append(f"[aqg session-preflight] preflight exited with status {proc.returncode}; state unknown")
except _SkipPreflight:
    pass
except subprocess.TimeoutExpired:
    parts.append(f"[aqg session-preflight] timed out after {TIMEOUT_S}s; skipping (slow remote / credential prompt / large repo)")
except (OSError, ValueError) as exc:
    parts.append(f"[aqg session-preflight] preflight invocation error: {type(exc).__name__}: {exc}")

# ---- discipline, appended to the SAME parts list, never a second emitter --------
#
# The two policy sentences are READ from docs/policies/audit-trigger.md, which
# publishes them for exactly this purpose (PR #87), so this channel is a carrier
# held by the same guard as the shell hook and the Cursor adapter rather than a
# fourth hand-maintained copy. Everything here is best-effort: any failure leaves
# the entry-point line, which needs no file at all.
discipline = [
    "",
    "[aqg discipline] Before writing or modifying code, invoke the "
    "aqg-code-construction skill — it is the entry point for a coding task, and the "
    "audit-before-commit gate sits inside it.",
]
policy_ref = "UNRESOLVED"
if AQG_ROOT:
    root = pathlib.Path(AQG_ROOT)
    # Computed BEFORE the try and by string join only, so it cannot raise. The
    # first version told the reader to "read the file directly" and then gave the
    # location as UNRESOLVED two lines down -- advice with no address
    # (aud_NinV9t0Cx3GiQxzh opus-f5).
    conventional = root / "docs" / "policies" / "audit-trigger.md"
    reader = root / "scripts" / "aqg_policy_markers.py"
    # Gate on the module being a regular file: the common missing-install case
    # then costs no import attempt at all, and SessionStart executes nothing from
    # AQG_ROOT that is not there (opus-f6). `append`, not `insert(0, ...)`, so a
    # module under AQG_ROOT/scripts can never shadow a stdlib name for this
    # process -- the same shadowing class a previous audit caught in a test.
    if reader.is_file():
        try:
            sys.path.append(str(root / "scripts"))
            from aqg_policy_markers import policy_path, reminder_clauses

            clauses = reminder_clauses(root)
            discipline.append(f"[aqg discipline] {clauses['skip-clause']}")
            discipline.append(f"[aqg discipline] {clauses['gate-a-clause']}")
            resolved = policy_path(root)
            if resolved.is_file():
                policy_ref = str(resolved)
        except BaseException as exc:  # aqg: top-level boundary
            # BaseException, not Exception: a SystemExit or a KeyboardInterrupt
            # raised inside an imported module must not cost the session its
            # entry-point line either. Reminder path, always fail-open.
            discipline.append(
                f"[aqg discipline] policy clauses unavailable "
                f"({type(exc).__name__}); read the file directly"
            )
            policy_ref = f"{conventional} (unverified)"
    else:
        discipline.append(
            "[aqg discipline] policy reader not installed at "
            f"{reader}; read the file directly"
        )
        policy_ref = f"{conventional} (unverified)"
discipline.append(f"[aqg discipline] Ladder + full list: {policy_ref}")
parts.extend(discipline)

# Emit via the SessionStart hookSpecificOutput.additionalContext JSON form, NOT plain
# stdout. Plain text on stdout corrupts headless claude (`--input-format stream-json`
# / `--output-format json`): the parser reads the summary as a malformed event line
# and aborts the session with no init/assistant/result. The JSON form is consumed by
# Claude Code's hook-output layer and injected OUTSIDE the event stream, so it works
# in interactive AND stream-json / image-attach / headless modes. (Verified for audit
# 7f7b8a48 against a real `claude --print` run: the JSON additionalContext IS injected
# — claude received the preflight summary; the old plain-stdout form was NOT injected
# in --print mode, so this is strictly wider coverage AND stream-json-safe.) Empty
# summary -> no stdout (nothing to inject; never an empty/invalid line).
summary = "\n".join(parts)
if summary:
    # cwd-scope hint (Owner 2026-06-11): this auto-preflight only covers the session's
    # cwd repo. When the work repo differs (a handoff says the task lives in another
    # repo), the auto-preflight ran on the WRONG repo — nudge re-invoking the skill on
    # the actual work repo instead of doing manual git checks (the gap that bit us: cwd
    # = one repo while the work actually lived in another).
    # Sanitize the cwd path before it enters model-facing additionalContext: backticks
    # would break the inline code span (and could spill following text), newlines could
    # read as separate instruction lines (audit 2c4654ce convergent f2 — cwd is usually
    # trusted but not guaranteed for cloned / attacker-named dirs).
    safe_cwd = PROJECT_DIR.replace("`", "'").replace("\n", " ").replace("\r", " ")
    summary += (
        f"\n\n[aqg session-preflight] ↑ 此 preflight 仅覆盖当前 cwd `{safe_cwd}`。"
        "若本任务工作仓不同（如交接 / handoff 指向别的 repo），对**那个工作仓**重跑 "
        "aqg-startup-preflight —— 直接 invoke skill，别拿手动 git status 当等价。"
    )
    json.dump(
        {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": summary}},
        sys.stdout,
    )
PYEOF

exit 0
