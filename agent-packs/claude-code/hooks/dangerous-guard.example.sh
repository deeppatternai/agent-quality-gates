#!/usr/bin/env bash
# AQG Dangerous Command Guard — Claude Code PreToolUse hook EXAMPLE wrapper.
#
# Per framework v2.1 §3 + §7 #9 + docs/policies/dangerous-command-guard.md:
# - AQG owns the policy + this example hook script.
# - AQG is NOT an active enforcer.
# - The Claude Code client owns the actual interception decision (i.e., whether
#   to honor exit codes 1/2 by blocking the tool call).
#
# This wrapper:
#   1. Reads the PreToolUse JSON payload from stdin (Claude Code spec).
#   2. Pipes it to the AQG rule engine `scripts/aqg_dangerous_guard.py`.
#   3. Forwards the engine's stdout (structured decision JSON) to stdout.
#   4. Forwards the engine's stderr (human-readable reasoning) to stderr.
#   5. Returns the engine's exit code (0 allow / 1 warn / 2 block).
#
# To install:
#   1. Copy or symlink this file into your Claude Code hooks directory.
#   2. Wire it into ~/.claude/settings.json under `hooks.PreToolUse`.
#      Per audit gpt #2 + SL-5/6/7 tool-agnostic claim, register for the tools
#      that may carry literal secrets (Edit / Write) AND command shapes (Bash):
#        {"hooks": {"PreToolUse": [{"matcher": "Bash|Edit|Write",
#         "command": "/path/to/dangerous-guard.example.sh"}]}}
#      If you only register matcher="Bash", SL-5/6/7 won't fire on Edit/Write.
#   3. (Optional) Override defaults via env var:
#        export AQG_DANGEROUS_GUARD_RULES=/path/to/custom-rules.json
#
# Boundary safeguards:
# - If AQG_ROOT is unset → silent exit 0 (don't block Claude when AQG missing).
# - If python3 is unavailable → silent exit 0 (degraded, not blocked).
# - If the rule engine script is missing → silent exit 0 (don't block).
# - All "infrastructure missing" cases default to ALLOW so a broken AQG install
#   never breaks the user's flow. The cost is the guard provides no protection
#   in those cases — but that is preferable to silently breaking tool execution.

set -uo pipefail

# Capture the PreToolUse JSON once: we need it for BOTH the tamper canary (#328) and
# the real evaluation, so we can no longer `exec` straight through.
input="$(cat 2>/dev/null || true)"
# Empty stdin: nothing to evaluate -> silent allow (consistent with the sibling AQG
# hooks; the previously-exec'd engine returned a non-blocking usage error on empty in).
[ -z "$input" ] && exit 0

# --- Infrastructure-MISSING cases stay fail-OPEN (audit gemini #8: WARN, never break
# Claude flow). "Missing" (guard not installed/usable) is distinct from "neutered"
# (present but silenced), handled by the canary below.
if [ -z "${AQG_ROOT:-}" ]; then
  echo "[AQG dangerous-guard] WARN: AQG_ROOT unset; guard INACTIVE for this tool call" >&2
  exit 0
fi
if ! command -v python3 >/dev/null 2>&1; then
  echo "[AQG dangerous-guard] WARN: python3 not found; guard INACTIVE" >&2
  exit 0
fi

script="$AQG_ROOT/scripts/aqg_dangerous_guard.py"
if [ ! -f "$script" ]; then
  echo "[AQG dangerous-guard] WARN: rule engine missing at $script; guard INACTIVE" >&2
  exit 0
fi

# --- Tamper canary (#328): the engine is PRESENT — confirm it still BLOCKs the known
# critical sample (default rule RM-1, root delete) before we trust any allow verdict.
# The kind of clean-diff edit an agent could use to silence the guard before a dangerous
# command (no static scan would flag it). Faithful to the #336 secret-scan canary: the
# THIN wrapper canaries the FAT engine (a SEPARATE file), so neutering the engine alone
# is caught here.
#
# Scope is HONEST and narrow — a single RM-1 sample, not a full rule sweep:
#  - CAUGHT: a TOTAL neuter (load_rules() -> [], or the engine raising / always-allowing)
#    and any break of RM-1 itself — RM-1 stops blocking, so the canary fails closed.
#  - NOT caught: a PARTIAL neuter that keeps RM-1 blocking but breaks some OTHER rule;
#    nor a deliberate adaptive agent who edits THIS wrapper or special-cases the sample
#    (the same residual as #336). The durable fix is the read-only/signed install path
#    (DesignSpec 2026-06-21 §5). Single-sample is a deliberate MED/LOW-stakes
#    YAGNI call; the canary DEPENDS on RM-1 remaining a critical-block (exit 2).
#
# The sample is built from fragments so this example file stays clean for content
# scanners + the Owner-only bash allowlist (the literal never appears contiguously). RM-1
# is ALWAYS present (the AQG_DANGEROUS_GUARD_RULES overlay is additive-only, cannot remove
# defaults), so a non-block here means the DEFAULT bank itself is broken.
_canary_cmd="rm -""rf ""/"   # fragments assemble the RM-1 critical sample (root delete)
_canary_payload="{\"tool_name\":\"Bash\",\"tool_input\":{\"command\":\"$_canary_cmd\"}}"
printf '%s' "$_canary_payload" | python3 "$script" >/dev/null 2>&1
_canary_rc=${PIPESTATUS[1]}   # engine's exit code (not printf's), matching the real path
if [ "$_canary_rc" -ne 2 ]; then
  # Engine did NOT block a known-critical sample: present-but-broken, not "off".
  if [ "${AQG_AGENT:-}" = "human-opt-in" ]; then
    echo "[AQG dangerous-guard] WARN: tamper canary — rule engine did not block a known-dangerous sample (bank may be neutered/broken, #328); AQG_AGENT=human-opt-in, allowing." >&2
    # fall through to the real check (a human accepts the risk)
  else
    echo "[AQG dangerous-guard] DEGRADED (fail-closed, exit 2): tamper canary — the rule engine did not block a known-dangerous sample; the rule bank may be neutered or broken (#328)." >&2
    echo "[AQG dangerous-guard] Fix the AQG install, or relaunch with AQG_AGENT=human-opt-in to override." >&2
    exit 2
  fi
fi

# --- Real evaluation: replay the captured payload through the engine. Engine prints
# decision JSON to stdout, reasoning to stderr, exits severity-mapped (0/1/2). We
# forward all three; PIPESTATUS[1] is the engine's exit code (not printf's).
printf '%s' "$input" | python3 "$script"
exit "${PIPESTATUS[1]}"
