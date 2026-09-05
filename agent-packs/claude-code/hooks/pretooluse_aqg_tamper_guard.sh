#!/usr/bin/env bash
# AQG — PreToolUse(Write|Edit|MultiEdit) tamper-resistance guard (#328).
#
# Threat model: an agent working in ANOTHER project reaches into the AQG install
# ($AQG_ROOT) and edits a gate-bearing file — a hook script or the secret-pattern /
# redaction bank — to NEUTER a quality/secret gate before doing the thing the gate
# would have caught (e.g. make secret_counts() return {} so every secret write
# passes). This denies such a Write/Edit/MultiEdit.
#
# Protected (under $AQG_ROOT):
#   - agent-packs/*/hooks/*.sh                                  (the hook scripts)
#   - scripts/_secret_patterns.py                               (secret pattern bank)
#   - scripts/_redaction_common.py, *_redaction.py,
#     scripts/aqg_dangerous_guard.py                            (gate detection core)
#
# ALLOW (exit 0) when:
#   - AQG_ROOT unset / python3 missing / bad stdin / no file_path  (consistent with
#     all AQG hooks — gate not wired or cannot run)
#   - target is NOT a protected file under AQG_ROOT
#   - cwd is INSIDE AQG_ROOT  → editing AQG from within the AQG checkout is
#     self-development (AQG is where these files are authored). This is the
#     chicken-and-egg carve-out: without it, the guard would brick AQG's own
#     development of its hooks.
#   - AQG_AGENT=human-opt-in  → human pre-launch override (a model cannot
#     self-apply it — env set in a Bash tool call does not persist across calls)
#
# SCOPE — honest, in-band PARTIAL (same class as the secret-scan canary, audit
# fcd64b84 / 0c5d5555): this raises the bar against a NAIVE neuter (Edit/Write a
# hook or the bank). It does NOT stop an adaptive agent who edits THIS guard,
# mutates files via Bash (mv / tee / `python -c` / redirection — out of
# Write/Edit/MultiEdit scope), or manipulates cwd. Full tamper-resistance needs the
# read-only / signed install path.
# Defense-in-depth, not a boundary.
#
# INVARIANTS (audit 5d4d64d7):
#   - The cwd carve-out trusts cwd as a HOST-populated session signal (the
#     PreToolUse payload `cwd` / CLAUDE_PROJECT_DIR), NOT model-controllable. If an
#     agent can spoof cwd, the carve-out is moot — that is the adaptive class above.
#   - The protected set is BY NAME. The secret gate is self-contained
#     (_secret_patterns.py imports only stdlib — verified), so the by-name set is
#     complete today; a NEW gate-bearing script must be added to
#     PROTECTED_SCRIPT_NAMES or it will not be protected.
#   - Path matching is case-folded: macOS/Windows FS are case-insensitive and
#     realpath does NOT normalize case, so a case-variant path to a protected file
#     is still caught (fail-secure).
#   - An unhandled logic error exits 1 = NON-blocking (fail-open), consistent with
#     the AQG hook family — this is defense-in-depth, not a sole boundary.
#
# Exit: 0 allow / 2 block. (PreToolUse denies a tool call with exit 2 — stderr is
# fed back to the model; exit 1 is NON-blocking, #329. Matches the sibling
# secret-scan / memory-write gates.)

set -uo pipefail

input="$(cat 2>/dev/null || true)"
[ -z "$input" ] && exit 0
if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi
if ! command -v python3 >/dev/null 2>&1; then exit 0; fi

# The python program is a single-quoted block: the $-tokens and %s inside it are
# Python / literal model-facing text, intentionally NOT shell-expanded.
# shellcheck disable=SC2016
printf '%s' "$input" | AQG_ROOT="$AQG_ROOT" python3 -c '
import json, os, sys

try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
ti = d.get("tool_input") or {}
if not isinstance(ti, dict):
    sys.exit(0)
fp = ti.get("file_path") or ""
if not isinstance(fp, str) or not fp:
    sys.exit(0)

aqg_root = os.environ.get("AQG_ROOT") or ""
if not aqg_root:
    sys.exit(0)


def _real(p):
    try:
        return os.path.realpath(p)
    except Exception:
        return os.path.normpath(p)


def _within(child, parent):
    # Case-fold: macOS/Windows filesystems are case-insensitive but realpath does
    # NOT normalize case (verified on APFS), so a case-variant path to a protected
    # file would otherwise slip the match. Fail-secure — on a case-sensitive FS this
    # can over-match a harmless DISTINCT file, but never under-match the real one.
    c, p = child.lower(), parent.lower()
    return c == p or c.startswith(p + os.sep)


fp_real = _real(fp)
root_real = _real(aqg_root)

# Only files under the AQG install are this guard concern.
if not _within(fp_real, root_real):
    sys.exit(0)

# Case-fold the relative path too (see _within): a case-variant component such as
# Scripts/ vs scripts/ on a case-insensitive FS must still classify as protected.
rel = os.path.relpath(fp_real.lower(), root_real.lower())
parts = rel.split(os.sep)

PROTECTED_SCRIPT_NAMES = {
    "_secret_patterns.py",
    "_redaction_common.py",
    "aqg_dangerous_guard.py",
}

is_hook = (
    len(parts) >= 4
    and parts[0] == "agent-packs"
    and parts[2] == "hooks"
    and parts[-1].endswith(".sh")
)
is_core = parts[0] == "scripts" and (
    parts[-1] in PROTECTED_SCRIPT_NAMES or parts[-1].endswith("_redaction.py")
)
if not (is_hook or is_core):
    sys.exit(0)

# Chicken-and-egg carve-out: editing AQG from inside the AQG checkout itself is
# self-development, not a cross-project neuter. cwd comes from the PreToolUse
# payload, with CLAUDE_PROJECT_DIR as fallback.
cwd = d.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or ""
if isinstance(cwd, str) and cwd and _within(_real(cwd), root_real):
    sys.exit(0)

# Human pre-launch override only.
if os.environ.get("AQG_AGENT") == "human-opt-in":
    sys.stderr.write(
        "[aqg tamper-guard] AQG_AGENT=human-opt-in; allowing edit to AQG gate file %s\n" % fp
    )
    sys.exit(0)

kind = "hook script" if is_hook else "gate pattern/redaction bank"
w = sys.stderr.write
w("[aqg tamper-guard] BLOCK: this would edit an AQG %s under the install root:\n" % kind)
w("[aqg tamper-guard]   %s\n" % fp)
w("[aqg tamper-guard] Editing a gate-bearing file from outside the AQG checkout can NEUTER a\n")
w("[aqg tamper-guard] quality/secret gate (e.g. make the secret scan pass everything). Refused.\n")
w("[aqg tamper-guard] If you legitimately need to change AQG itself, do it INSIDE the AQG checkout\n")
w("[aqg tamper-guard] (cwd under $AQG_ROOT), or ask the human to relaunch with AQG_AGENT=human-opt-in.\n")
w("[aqg tamper-guard] (Model: do NOT self-bypass via Bash export AQG_AGENT=... — env set in a Bash\n")
w("[aqg tamper-guard]  tool call does not persist across tool calls.)\n")
sys.exit(2)
'
rc=$?
exit "$rc"
