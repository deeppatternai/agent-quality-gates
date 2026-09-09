#!/usr/bin/env python3
"""Adapt Qoder hook payloads to the existing AQG Claude-compatible hooks."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


MANAGED_ID = "aqg-qoder-v1"
HOOK_SCRIPTS = frozenset(
    {
        "pretooluse_bash_skill_validator.sh",
        "pretooluse_secret_scan.sh",
        "posttooluse_bash_error_debugging_reminder.sh",
        "posttooluse_skill_edit_reminder.sh",
        "posttooluse_code_construction_reminder.sh",
        "posttooluse_test_quality_reminder.sh",
        "posttooluse_security_review_reminder.sh",
        "precompact_closeout_reminder.sh",
        "sessionstart_preflight.sh",
        "sessionstart_update_check.sh",
        "userpromptsubmit_handoff_mandate.sh",
        "wip_checkpoint_save.sh",
        "wip_checkpoint_recover.sh",
    }
)
BLOCKING_HOOKS = frozenset(
    {
        "pretooluse_bash_skill_validator.sh",
        "pretooluse_secret_scan.sh",
    }
)
PROJECT_ARG_HOOKS = frozenset(
    {
        "sessionstart_preflight.sh",
        "wip_checkpoint_save.sh",
        "wip_checkpoint_recover.sh",
    }
)
#: The one hook whose whole job is to return. It forks a detached updater and
#: exits, so anything but a fast return means something is stuck -- and unlike
#: the other hooks here, nothing is lost by giving up on it. The rest stay
#: unbounded: that predates this trigger, and putting a clock on a preflight
#: that legitimately takes twenty seconds is a different change.
UPDATE_CHECK_HOOK = "sessionstart_update_check.sh"
UPDATE_CHECK_TIMEOUT_SECONDS = 10


def _find_bash() -> str | None:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _degraded(hook: str, reason: str) -> int:
    print(f"[aqg qoder-adapter] degraded: {reason}", file=sys.stderr)
    return 2 if hook in BLOCKING_HOOKS else 0


def _aqg_split_envelope(stdout: str) -> "tuple[str, str]":
    """Split hook stdout into (model-context text, everything else).

    The PostToolUse hooks emit `hookSpecificOutput.additionalContext` on stdout so
    Claude Code can inject it — Claude does not forward hook stderr to the model.
    Every other host reaches the same text through stderr, so an adapter that also
    forwards stdout raw would deliver it twice, once as escaped JSON.

    Line-based on purpose. An all-or-nothing `json.loads(stdout)` fails open: on
    mixed output (an envelope plus any other line) it parses nothing and the
    caller forwards the whole thing, envelope included — recreating the leak this
    exists to stop. Here each line is judged separately, so an envelope is always
    removed and unrelated output is always preserved.

    Kept inline rather than shared: these adapters are standalone per-host
    integrations in two different trees, and a cross-tree import is more fragile
    than this function. `tests/behavior/test_adapter_stdout_handling.py` exercises
    every adapter and fails if one starts behaving differently.
    """
    context_parts: list[str] = []
    residual_lines: list[str] = []
    for line in (stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and "hookSpecificOutput" in stripped:
            try:
                parsed = json.loads(stripped)
            except (ValueError, TypeError):
                parsed = None
            if isinstance(parsed, dict):
                block = parsed.get("hookSpecificOutput")
                if isinstance(block, dict):
                    text = block.get("additionalContext")
                    if isinstance(text, str) and text.strip():
                        context_parts.append(text.strip())
                    # An envelope line is consumed even when its text is empty:
                    # forwarding it raw is the defect, and an empty payload must
                    # fall back to stderr rather than blank out the reminder.
                    continue
        residual_lines.append(line)
    return "\n".join(context_parts), "\n".join(residual_lines)



def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an AQG hook for Qoder")
    parser.add_argument("--aqg-root", required=True)
    parser.add_argument("--hook", required=True, choices=sorted(HOOK_SCRIPTS))
    parser.add_argument("--managed-id", required=True)
    args = parser.parse_args(argv)

    if args.managed_id != MANAGED_ID:
        return _degraded(args.hook, "unrecognized managed hook identity")

    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError:
        return _degraded(args.hook, "hook input is not valid UTF-8")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return _degraded(args.hook, "invalid hook input JSON")
    if not isinstance(payload, dict):
        return _degraded(args.hook, "hook input must be a JSON object")

    aqg_root = Path(args.aqg_root).expanduser().absolute()
    hooks_dir = (aqg_root / "agent-packs" / "claude-code" / "hooks").resolve()
    script = (hooks_dir / args.hook).resolve()
    try:
        script.relative_to(hooks_dir)
    except ValueError:
        return _degraded(args.hook, "hook path escaped the AQG hook directory")
    if not (aqg_root / "VERSION").is_file() or not script.is_file():
        return _degraded(args.hook, "AQG checkout or hook script is unavailable")

    bash = _find_bash()
    if bash is None:
        return _degraded(args.hook, "bash is unavailable")

    cwd = payload.get("cwd")
    project_dir = Path(cwd if isinstance(cwd, str) and cwd else os.getcwd()).resolve()
    if not project_dir.is_dir():
        return _degraded(args.hook, "hook payload cwd is not an existing directory")
    if payload.get("hook_event_name") == "PostToolUseFailure":
        payload["tool_response"] = {
            "is_error": True,
            "error_type": payload.get("error_type", "tool_failure"),
        }

    env = os.environ.copy()
    shell_root = aqg_root.as_posix()
    shell_project_dir = project_dir.as_posix()
    env["AQG_ROOT"] = shell_root
    env["CLAUDE_PROJECT_DIR"] = shell_project_dir
    # Git Bash on Windows treats backslashes in its script-name argument as
    # escapes. Forward slashes are accepted by both Git Bash and POSIX Bash.
    command = [bash, script.as_posix()]
    if args.hook in PROJECT_ARG_HOOKS:
        command.append(shell_project_dir)

    try:
        # Capture rather than inherit: stdout now carries the model-context
        # envelope that exists for Claude Code, and Qoder's stdout contract is
        # not documented in this repo. Conservative — forward stderr unchanged
        # (which is where Qoder has always read the reminder) and drop the
        # envelope. NOTE: capturing is itself a change — the streams were
        # previously inherited, so buffering and interleaving differ even though
        # the CONTENT Qoder receives is the same as before.
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            env=env,
            capture_output=True,
            check=False,
            timeout=UPDATE_CHECK_TIMEOUT_SECONDS if args.hook == UPDATE_CHECK_HOOK else None,
        )
        _, residual_stdout = _aqg_split_envelope(proc.stdout)
        if residual_stdout.strip():
            print(residual_stdout, end="" if residual_stdout.endswith("\n") else "\n")
        if proc.stderr:
            print(proc.stderr, end="", file=sys.stderr)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return _degraded(args.hook, f"hook invocation failed: {type(exc).__name__}")

    if args.hook in BLOCKING_HOOKS:
        return 0 if proc.returncode == 0 else 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
