#!/usr/bin/env bash
# AQG Ledger v2.1 — PostToolUse(Bash) PR-creation capture wrapper.
#
# Observes Claude's own `gh pr create` and emits one low-fidelity `aqg-hook`
# progress event (with the PR URL) into the LOCAL ledger inbox (never network/cloud).
# Opt-in (settings.ledger.example.json). NEVER fails Claude Code.
#
# This hook fires on EVERY Bash command, so it pre-filters cheaply before python:
# - capture the PostToolUse JSON from stdin ONCE (text JSON; no NUL)
# - cheap grep fast-reject; only `gh pr create`-looking commands reach python
# - feed the SAME bytes to grep and python via `printf '%s'` (NOT `echo` — preserves
#   JSON backslashes; impl-audit R2 gemini-f3/gpt-f3); never grep raw stdin (would
#   consume the stream and starve python; R2 gemini-f1)
# - NO `exec`; always exit 0 (a python crash cannot propagate; R2 gemini-f3)

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

payload="$(cat 2>/dev/null || true)"
if [ -z "$payload" ]; then
  exit 0
fi

# Cheap fast-reject: skip the 99% of bash commands that aren't a PR creation.
# Pure-bash regex (no pipe) — a piped `grep -q` would early-close the pipe, SIGPIPE
# the printf, and under `pipefail` fail the whole pipeline, silently dropping a large
# MATCHING payload (impl-audit b86ee8f3 A). RHS must stay UNQUOTED for regex matching.
if [[ ! "$payload" =~ gh[[:space:]]+pr[[:space:]]+create ]]; then
  exit 0
fi

# Optional first arg = CLAUDE_PROJECT_DIR; export so the writer pins to the project dir.
if [ "$#" -ge 1 ] && [ -n "${1:-}" ]; then
  export AQG_HOOK_PROJECT_DIR="$1"
fi

printf '%s' "$payload" | python3 "$AQG_ROOT/scripts/ledger_hook_writer.py" --mode pr-capture || true
exit 0
