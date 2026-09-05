#!/usr/bin/env bash
# AQG — UserPromptSubmit: handoff-intent → mandate aqg-session-handoff (selective inject).
#
# When the user's submitted prompt expresses session-handoff intent (handoff / 交接 /
# 下个 session / next session / 交给下一棒), inject a hookSpecificOutput.additionalContext
# that MANDATES invoking the aqg-session-handoff skill instead of freestyling a handoff.
# Otherwise stay completely silent (no injection). This is the enforcement layer the
# advisory CLAUDE.md rule + probabilistic skill description could not guarantee.
#
# Mechanism note: a UserPromptSubmit hook injects model-visible context ONLY via the
# JSON `hookSpecificOutput.additionalContext` field on exit 0 — plain stdout is NOT
# injected (verified against the Claude Code runtime). So we emit that JSON on a match.
#
# Anti-noise: unlike a blanket every-prompt injector, this fires ONLY on a handoff-intent
# match and is silent otherwise (AQG explicitly disfavors noisy UserPromptSubmit hooks).
#
# Boundaries (all → silent exit 0, no injection, NEVER blocks the prompt):
#   - empty / malformed stdin JSON       → silent
#   - python3 unavailable                → silent (degraded)
#   - prompt absent / no handoff intent  → silent
# stdlib only (python3 json/re; no jq, per AQG hook convention). Never exit 2 (that would
# block the user's prompt); this hook only ADDS context. Warn-only class (not in
# _AQG_BLOCKING_HOOK_SCRIPTS).
#
# Exit: always 0.

set -uo pipefail

input="$(cat 2>/dev/null || true)"
[ -z "$input" ] && exit 0
if ! command -v python3 >/dev/null 2>&1; then exit 0; fi

printf '%s' "$input" | python3 -c '
import json, re, sys

try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)
prompt = d.get("prompt") or ""
if not isinstance(prompt, str) or not prompt:
    sys.exit(0)

# Session-handoff INTENT patterns — tight on purpose: specific to "produce a handoff",
# NOT broad "continue / resume / 下一个" which would false-fire on ordinary prompts. A
# rare false positive is low-harm: the model reads the full prompt and only invokes the
# skill when actually asked.
PATTERNS = [
    r"hand[\s_-]?(off|over)",
    r"交接",
    r"交班",
    r"下一?个?\s*session",
    r"next\s+session",
    r"交给下一棒",
]
if not any(re.search(p, prompt, re.IGNORECASE) for p in PATTERNS):
    sys.exit(0)

mandate = (
    "AQG handoff note — the user prompt mentions a session handoff (handoff / 交接). "
    "IF they are asking you to PRODUCE a handoff for the next session, you MUST invoke the "
    "aqg-session-handoff skill (emit its 8-section skeleton, fill it, then validate) — do NOT "
    "freestyle a handoff (that bypasses the skill structure + secret-scan + "
    "first-step / actor / audit-state checks). If they are only discussing or declining a "
    "handoff, ignore this note. If this repo runs an EAF engine, use eaf-session-handoff "
    "instead. When you do produce one, first finalize any green in-flight trivial work, then "
    "emit the validated handoff."
)
json.dump(
    {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": mandate}},
    sys.stdout,
)
' || exit 0
