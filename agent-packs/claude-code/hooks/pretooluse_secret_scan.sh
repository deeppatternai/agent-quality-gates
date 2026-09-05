#!/usr/bin/env bash
# AQG — PreToolUse(Write|Edit|MultiEdit|NotebookEdit|Bash|apply_patch) secret-scan guard.
#
# Blocks a tool call that would introduce a recognized secret — written to disk
# (a file or a notebook cell), passed in a Bash command, OR added by a Codex
# apply_patch patch. Reuses scripts/_secret_patterns.py (single source of truth,
# 18 built-in patterns); never redefines patterns.
#
# Extracted text per tool:
#   Write         -> tool_input.content
#   Edit          -> tool_input.new_string
#   MultiEdit     -> tool_input.edits[].new_string
#   NotebookEdit  -> tool_input.new_source       (audit fcd64b84 f1: missed write path)
#   Bash          -> tool_input.command
#   apply_patch   -> added ('+') lines of tool_input.command   (Codex primary file-write
#                                                 path; removals/context excluded so a
#                                                 secret CLEANUP is not blocked; a3f5a4d6)
#
# SCOPE / THREAT MODEL (audit fcd64b84 f3, honest): this is STATIC LITERAL
# scanning. It catches a plaintext secret that appears literally in the content/
# command. It does NOT catch obfuscated/indirect introduction — base64 decode,
# shell-variable assembly, `printf "$AWS_SECRET" > .env`, or copying an existing
# secret-bearing file. Effective against accidental/naive writes; weak against a
# deliberately evasive agent. Fully hardening the hook + pattern bank against tampering is
# a separate concern that ultimately needs a read-only / pinned / signed install. A runtime
# canary (below, #328) adds PARTIAL defense: it
# catches ACCIDENTAL or non-adaptive bank breakage (a refactor that breaks detection,
# a naive neuter), but not an adaptive edit of the hook itself — that still needs the
# read-only install.
#
# FAIL POLICY (audit fcd64b84 f2, convergent 3/3):
#   - gate NOT enabled (AQG_ROOT unset)        -> silent exit 0 (intentional off)
#   - environment lacks python3                -> stderr DEGRADED note + exit 0
#   - gate SHOULD run but is broken
#       (pattern bank unimportable, or
#        secret_counts() raises, or the
#        tamper canary matches nothing, #328)  -> stderr DEGRADED + exit 2 (FAIL-CLOSED;
#                                                  a human may AQG_AGENT=human-opt-in override)
# Distinguishing "off" from "broken" is the point: a misconfigured-but-present
# gate must not silently pass secrets.
#
# On a hit: BLOCK via exit 2 (official PreToolUse deny code; stderr fed to the
# model) naming ONLY the matched pattern TYPE(s) (secret_counts keys are fixed
# pattern names, never matched text), NEVER the secret value. (#329 verified on
# Claude Code 2.1.185 that exit 1 is NON-blocking — the tool proceeds; the sibling
# memory-write-guard + bash-skill-validator gates were migrated from exit 1 to exit 2
# to match this gate, which always used the correct deny code.)
#
# Bypass: a HUMAN pre-setting AQG_AGENT=human-opt-in before launch (a model
# cannot self-apply it — env set in a Bash tool call does not persist).
#
# Exit: 0 allow / 2 block (or fail-closed).

set -uo pipefail

input="$(cat 2>/dev/null || true)"
[ -z "$input" ] && exit 0
# Gate not enabled: no AQG checkout wired. Silent (consistent with all AQG hooks).
if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi
# Environment lacks python3: cannot run the scanner. Make it VISIBLE, fail-open
# (forcing a block here would brick every Write/Edit/Bash on a python-less box).
if ! command -v python3 >/dev/null 2>&1; then
  printf '[aqg secret-scan] DEGRADED: python3 not found; secret scan SKIPPED (gate not enforced).\n' >&2
  exit 0
fi

printf '%s' "$input" | AQG_ROOT="$AQG_ROOT" python3 -c '
import json, os, sys

def degrade_failclosed(msg):
    # Gate should run but is broken -> deny (fail-closed), explain on stderr.
    sys.stderr.write("[aqg secret-scan] DEGRADED (fail-closed, exit 2): " + msg + "\n")
    sys.stderr.write("[aqg secret-scan] The scanner could not verify this call. Fix the AQG install, "
                     "or relaunch with AQG_AGENT=human-opt-in to override.\n")
    sys.exit(2)

try:
    d = json.loads(sys.stdin.read())
except Exception:
    sys.exit(0)
if not isinstance(d, dict):
    sys.exit(0)

tool = d.get("tool_name") or ""
ti = d.get("tool_input") or {}
if not isinstance(ti, dict):
    sys.exit(0)

chunks = []
if tool == "Write":
    c = ti.get("content")
    if isinstance(c, str): chunks.append(c)
elif tool == "Edit":
    ns = ti.get("new_string")
    if isinstance(ns, str): chunks.append(ns)
elif tool == "MultiEdit":
    for e in (ti.get("edits") or []):
        if isinstance(e, dict) and isinstance(e.get("new_string"), str):
            chunks.append(e["new_string"])
elif tool == "NotebookEdit":
    src = ti.get("new_source")
    if isinstance(src, str): chunks.append(src)
elif tool == "Bash":
    cmd = ti.get("command")
    if isinstance(cmd, str): chunks.append(cmd)
elif tool == "apply_patch":
    # Codex CLI primary file-write tool (WS-6 audit a3f5a4d6): tool_input.command is the
    # unified-patch text (NO file_path field). Claude Code never emits apply_patch, so
    # this branch is additive (no Claude-side change). Scan ONLY added ("+") lines,
    # stripping the marker — the analog of the Edit path scanning new_string only
    # (audit 3f2a888a, convergent):
    #   - a secret is introduced only on an ADDED line; a context/removed line is
    #     pre-existing or being deleted, so scanning it would wrongly BLOCK a patch that
    #     REMOVES a secret (a "-AWS_KEY=..." deletion) or edits near a pre-existing one.
    #   - stripping "+" makes the scanner see the exact text written to the file, so no
    #     diff marker can shield a (future line-anchored) pattern from matching.
    # apply_patch envelope headers ("*** Add/Update File:", "*** Begin/End Patch", "@@ ")
    # do not start with "+", so they are naturally excluded.
    # NOTE: this whole block runs inside a bash single-quoted python3 -c heredoc, so it
    # MUST stay free of apostrophes / single-quote characters or the bash quoting breaks.
    cmd = ti.get("command")
    if isinstance(cmd, str):
        for line in cmd.splitlines():
            if line.startswith("+"):
                chunks.append(line[1:])
else:
    sys.exit(0)  # tool out of scope

body = "\n".join(chunks)
if not body.strip():
    sys.exit(0)

# Single source of truth for patterns — never redefine here. Bank present-but-broken
# is a config defect, not "gate off": fail-closed (audit fcd64b84 f2).
sys.path.insert(0, os.path.join(os.environ["AQG_ROOT"], "scripts"))
try:
    from _secret_patterns import secret_counts
except Exception as exc:
    degrade_failclosed("secret pattern bank unimportable (%s)" % type(exc).__name__)

# Tamper canary (#328): confirm the bank still POSITIVELY detects known secret
# shapes via the SAME runtime path the scan uses (secret_counts default =
# get_patterns()). We require each shape to match its OWN pattern name in the counts
# — NOT mere dict truthiness — so a neuter returning a non-empty zero/sentinel dict
# (e.g. {"x": 0}) is still caught (audit 0c5d5555 grok f1). Shapes are built from
# fragments (the proven self_test samples) so this file stays scan-clean.
#
# SCOPE — honest (audit 0c5d5555 claude f1 / grok f4): this catches ACCIDENTAL or
# NON-ADAPTIVE bank breakage — a refactor that breaks detection, or a naive
# `return {}` neuter. It does NOT stop a deliberate, adaptive agent: the shapes live
# in this agent-readable file, so an attacker can special-case them (a canary-only
# allowlist) or edit this hook itself. Those residuals need the read-only/signed
# install path. The samples assume a bank
# detecting common shapes (aws/pem/github); a custom downstream bank should adjust
# them or the gate will fail closed.
_canary_samples = {
    "aws_access_key": "AK" + "IA" + "1234567890ABCDEF",
    "private_key": "-----BEGIN " + "RSA PRIVATE KEY-----",
    "github_token": "gh" + "p_" + "a" * 32,
}
try:
    _detected = sum(1 for _n, _s in _canary_samples.items() if _n in secret_counts(_s))
except Exception as exc:
    degrade_failclosed("pattern bank canary raised (%s)" % type(exc).__name__)
# >=1 of N (not all): robust to a legitimate single-pattern change, still catches a
# bank that positively detects NONE of its own known shapes.
if _detected == 0:
    if os.environ.get("AQG_AGENT") == "human-opt-in":
        sys.stderr.write("[aqg secret-scan] WARN: pattern bank canary detected 0/%d known secret "
                         "shapes (bank may be tampered, #328) but AQG_AGENT=human-opt-in; allowing.\n"
                         % len(_canary_samples))
    else:
        degrade_failclosed("pattern bank canary detected 0/%d known secret shapes — the bank may "
                           "be neutered/tampered (#328)" % len(_canary_samples))

# A scanner exception must NOT silently let the secret through (audit fcd64b84 f2:
# previously uncaught -> exit 1 -> non-blocking pass).
try:
    counts = secret_counts(body)
except Exception as exc:
    degrade_failclosed("secret scan raised (%s)" % type(exc).__name__)

if not counts:
    sys.exit(0)

# Human pre-launch override only (model cannot self-apply; env does not persist).
if os.environ.get("AQG_AGENT") == "human-opt-in":
    sys.stderr.write(
        "[aqg secret-scan] AQG_AGENT=human-opt-in; allowing despite secret-pattern hit: "
        + ", ".join(sorted(counts)) + "\n"
    )
    sys.exit(0)

types = ", ".join(f"{name} x{n}" for name, n in sorted(counts.items()))
w = sys.stderr.write
w(f"[aqg secret-scan] BLOCK: {tool} would introduce a recognized secret pattern: {types}\n")
w("[aqg secret-scan] The value is NOT echoed back. A secret must not be written to a file or a Bash command.\n")
w("[aqg secret-scan] Fix: move it to an env var / secret manager and reference it (os.environ[...] / \"$VAR\").\n")
w("[aqg secret-scan] (Model: do NOT self-bypass via Bash export AQG_AGENT=... — env set in a Bash tool call does\n")
w("[aqg secret-scan]  not persist across tool calls. Remove the secret, or ask the human to relaunch with\n")
w("[aqg secret-scan]  AQG_AGENT=human-opt-in pre-set.)\n")
sys.exit(2)
'
rc=$?
exit "$rc"
