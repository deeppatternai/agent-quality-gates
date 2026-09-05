#!/usr/bin/env python3
"""Smoke probe: confirm a PreToolUse hook that exits 2 still DENIES (#333).

The AQG blocking gates (pretooluse_secret_scan / _memory_write_guard /
_bash_skill_validator / _aqg_tamper_guard) rely on a HOST contract: a PreToolUse hook exiting 2 denies
the tool call (exit 1 is non-blocking — the bug PR #332 fixed). That contract lives
in Claude Code, not in the hooks, so a future CC release that changed the exit-code
mapping would SILENTLY degrade every blocking gate (the dormant-gate failure mode
#329 was about). This probe re-runs the #329 nested `claude -p` + deny-hook check.

It is a MANUAL / on-upgrade probe (running real `claude` needs the binary + auth, so
it is not wired into ordinary CI): run it after a Claude Code upgrade. The pure
verdict logic (`evaluate`) is unit-tested; only the live nested run needs `claude`.

HARD GUARANTEE vs advisory (audit 5c72e5f9 / 1800e6e6):
- The probe's load-bearing signal is the marker command's SIDE EFFECT. If the proof
  file was written, the command EXECUTED despite the hook's exit 2 → FAIL. This is
  unspoofable in the dangerous direction (a written file means the command ran) and
  is checked FIRST and on every path, including when the nested claude errors or
  emits unparseable output — so a real break is never downgraded to "inconclusive".
- The PASS verdict is advisory. The deny hook writes a SENTINEL on its exit-2 branch
  (so a no-proof run is distinguished from one where the model never attempted the
  command — that returns ERROR, not FAIL). With `--allowedTools Bash`, a blocked
  Bash call after the hook fired is consistent with exit-2 denying it. The sentinel
  path is baked into the hook, not exposed in the nested agent's environment.

Documented limitations (a PASS does not, by itself, rule these out):
- Headless `-p` cannot distinguish a true hard-block from a future CC that REMAPPED
  exit 2 to "ask the user" (auto-denied with no user present). An interactive probe
  is needed for that.
- Attribution to exit 2 assumes a SINGLE deny layer: with `--allowedTools Bash` no
  other permission layer blocks the temp write. A future CC adding another
  default-deny could mask a broken exit-2 mapping.
- The probe assumes a COOPERATIVE nested model running the requested command; it is
  not hardened against an adversarial agent deliberately forging the sentinel.

Exit codes: 0 = PASS (contract holds); 1 = FAIL (exit 2 no longer denies — gates
degrading); 2 = ERROR (inconclusive: claude errored / unparseable output / the
marker command was never attempted); 3 = SKIP (claude unavailable — a distinct code
so an upgrade workflow is not misled by exit 0).
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_ERROR = 2
EXIT_SKIP = 3

VERDICT_PASS = "PASS"
VERDICT_FAIL = "FAIL"
VERDICT_ERROR = "ERROR"

MARKER = "AQGPROBEMARKER"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"


def _deny_hook(sentinel_path: str) -> str:
    """Deny hook with the sentinel path BAKED IN (not exposed to the agent env)."""
    return f"""\
#!/usr/bin/env bash
# PreToolUse deny hook: on the probe marker, record that this exit-2 branch ran
# (sentinel) then exit 2 (deny). Any other command exits 0 (non-blocking). The
# sentinel path is baked in here, not passed through the nested agent's environment.
input=$(cat)
if printf '%s' "$input" | grep -q '{MARKER}'; then
  : > {shlex.quote(sentinel_path)}
  echo "[probe] DENY: marker command blocked (exit 2)" >&2
  exit 2
fi
exit 0
"""


def _settings(hook_path: str) -> str:
    return json.dumps(
        {
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": hook_path}],
                    }
                ]
            }
        }
    )


def _denial_names_marker(result_json: object) -> bool:
    """Advisory: does `permission_denials` carry the marker command? (corroboration)"""
    denials = result_json.get("permission_denials") if isinstance(result_json, dict) else None
    if not isinstance(denials, list):
        return False
    for d in denials:
        if isinstance(d, dict):
            tool_input = d.get("tool_input")
            if isinstance(tool_input, dict) and MARKER in str(tool_input.get("command", "")):
                return True
    return False


def evaluate(
    hook_fired: bool, deny_proof_exists: bool, result_json: object
) -> tuple[str, str]:
    """Pure tri-state verdict from the captured run.

    Checked in order of signal strength (audit 1800e6e6 f1):
    - FAIL: the marker command EXECUTED (proof file written) → exit 2 did not block,
      regardless of the sentinel. The strongest, unspoofable-in-the-dangerous-
      direction signal, so it is checked FIRST.
    - ERROR: no proof AND the hook never fired → the nested model did not attempt the
      marker command, so the exit-2 contract was not exercised (inconclusive, NOT a
      contract failure).
    - PASS: the hook fired (its exit-2 branch ran) AND the marker command did not
      execute. Advisory attribution (see module docstring). `permission_denials` is
      corroboration only.
    """
    if deny_proof_exists:
        return VERDICT_FAIL, (
            "the marker command EXECUTED despite the hook's exit 2 — the PreToolUse "
            "deny contract is BROKEN"
        )
    if not hook_fired:
        return VERDICT_ERROR, (
            "INCONCLUSIVE: the deny hook never fired — the nested model did not "
            "attempt the marker command, so the exit-2 contract was not exercised "
            "(not a contract failure; re-run)"
        )
    note = "" if _denial_names_marker(result_json) else (
        " (note: permission_denials did not corroborate — CC's denial accounting may "
        "have changed shape, but the command was provably blocked while the hook "
        "fired its exit-2 branch)"
    )
    return VERDICT_PASS, (
        "exit 2 blocked the marker command — the hook fired and the command did not "
        "execute" + note
    )


def _fail(version: str, reason: str) -> int:
    print(
        f"FAIL: PreToolUse exit-2 deny contract BROKEN on {version} — AQG blocking "
        f"gates are silently degrading. {reason}",
        file=sys.stderr,
    )
    return EXIT_FAIL


def _run_live(claude_bin: str, model: str, timeout_s: int) -> int:
    workdir = Path(tempfile.mkdtemp(prefix="aqg_deny_probe_"))
    hook_path = workdir / "deny_hook.sh"
    settings_path = workdir / "settings.json"
    proof_path = workdir / f"proof_{MARKER}.txt"
    sentinel_path = workdir / "hook_fired"

    hook_path.write_text(_deny_hook(str(sentinel_path)), encoding="utf-8")
    hook_path.chmod(0o755)
    settings_path.write_text(_settings(str(hook_path)), encoding="utf-8")

    prompt = (
        f"Use the Bash tool to run exactly this command: echo hi > {proof_path} "
        "— then reply done."
    )
    cmd = [
        claude_bin, "-p", prompt,
        "--setting-sources", "project",
        "--settings", str(settings_path),
        "--allowedTools", "Bash",
        "--model", model,
        "--output-format", "json",
    ]
    version = _claude_version(claude_bin)
    try:
        proc = subprocess.run(
            cmd, cwd=str(workdir), capture_output=True, text=True, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        # A timeout cannot have let the command through unseen — but check anyway:
        # a written proof is conclusive evidence of a leak regardless.
        if proof_path.exists():
            return _fail(version, "marker command executed (nested run then timed out)")
        print(f"ERROR: nested claude run exceeded {timeout_s}s timeout", file=sys.stderr)
        return EXIT_ERROR

    # A written proof is conclusive — check it BEFORE treating any nested-claude
    # error (nonzero exit / unparseable output) as merely inconclusive (audit
    # 1800e6e6 f2): a degraded host could break exit-2 AND perturb claude's exit
    # code / output shape at once, and the leak must still surface as FAIL.
    if proc.returncode != 0:
        if proof_path.exists():
            return _fail(version, "marker command executed (nested claude also exited nonzero)")
        print(
            f"ERROR: nested claude exited {proc.returncode} — cannot verify the "
            f"contract\n{proc.stderr.strip()[:500]}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    try:
        result = json.loads(proc.stdout)
    except (ValueError, TypeError):
        if proof_path.exists():
            return _fail(version, "marker command executed (claude output unparseable)")
        print(
            f"ERROR: could not parse claude --output-format json:\n{proc.stdout[:500]}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    verdict, reason = evaluate(sentinel_path.exists(), proof_path.exists(), result)
    if verdict == VERDICT_PASS:
        print(f"PASS: PreToolUse exit-2 deny contract HOLDS on {version}. {reason}")
        return EXIT_PASS
    if verdict == VERDICT_ERROR:
        print(f"ERROR ({version}): {reason}", file=sys.stderr)
        return EXIT_ERROR
    return _fail(version, reason)


def _claude_version(claude_bin: str) -> str:
    try:
        out = subprocess.run(
            [claude_bin, "--version"], capture_output=True, text=True, timeout=30
        )
        return out.stdout.strip() or "claude (version unknown)"
    except (OSError, subprocess.SubprocessError):
        return "claude (version unknown)"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Probe the PreToolUse exit-2 deny contract (#333).")
    parser.add_argument(
        "--timeout", type=int, default=180, help="seconds for the nested claude run"
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("AQG_PROBE_MODEL", DEFAULT_MODEL),
        help="model id for the nested run (env AQG_PROBE_MODEL; the default may need "
        "updating if it is retired on a newer Claude Code)",
    )
    args = parser.parse_args(argv)

    claude_bin = os.environ.get("AQG_PROBE_CLAUDE_BIN") or shutil.which("claude")
    if not claude_bin:
        print(
            "SKIP (exit 3): `claude` not found on PATH (set AQG_PROBE_CLAUDE_BIN to "
            "override). This probe needs a real Claude Code binary; run it on a host "
            "that has one after a CC upgrade.",
        )
        return EXIT_SKIP

    return _run_live(claude_bin, args.model, args.timeout)


if __name__ == "__main__":
    raise SystemExit(main())
