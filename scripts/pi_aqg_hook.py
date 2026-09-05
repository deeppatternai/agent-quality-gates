#!/usr/bin/env python3
"""Run one managed AQG policy script from the Pi TypeScript extension."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


MANAGED_ID = "aqg-pi-v1"
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
        "userpromptsubmit_handoff_mandate.sh",
        "wip_checkpoint_save.sh",
        "wip_checkpoint_recover.sh",
    }
)
PROJECT_ARG_HOOKS = frozenset(
    {"sessionstart_preflight.sh", "wip_checkpoint_save.sh", "wip_checkpoint_recover.sh"}
)


def _read_payload() -> dict[str, object]:
    raw = sys.stdin.buffer.read().decode("utf-8")
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook payload must be a JSON object")
    return value


def _find_bash() -> str | None:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _cwd(payload: dict[str, object]) -> Path:
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).expanduser().resolve()
    event = payload.get("event")
    if isinstance(event, dict):
        nested = event.get("cwd") or event.get("workspaceRoot") or event.get("workspace_root")
        if isinstance(nested, str) and nested:
            return Path(nested).expanduser().resolve()
    return Path.cwd().resolve()


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
    parser = argparse.ArgumentParser(description="Run a managed AQG hook for Pi")
    parser.add_argument("--aqg-root", required=True)
    parser.add_argument("--hook", choices=sorted(HOOK_SCRIPTS), required=True)
    parser.add_argument("--managed-id", required=True)
    args = parser.parse_args(argv)

    if args.managed_id != MANAGED_ID:
        print("ERROR: unrecognized AQG Pi managed identity", file=sys.stderr)
        return 2
    try:
        payload = _read_payload()
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: invalid Pi hook payload: {exc}", file=sys.stderr)
        return 2

    aqg_root = Path(args.aqg_root).expanduser().resolve()
    hooks_dir = (aqg_root / "agent-packs" / "claude-code" / "hooks").resolve()
    script = (hooks_dir / args.hook).resolve()
    try:
        script.relative_to(hooks_dir)
    except ValueError:
        print("ERROR: hook path escaped the AQG hook directory", file=sys.stderr)
        return 2
    project_dir = _cwd(payload)
    if not (aqg_root / "VERSION").is_file() or not script.is_file():
        print("ERROR: AQG checkout or hook script is unavailable", file=sys.stderr)
        return 2
    if not project_dir.is_dir():
        print(f"ERROR: Pi hook cwd is not a directory: {project_dir}", file=sys.stderr)
        return 2
    bash = _find_bash()
    if bash is None:
        print("ERROR: bash is unavailable for AQG hook execution", file=sys.stderr)
        return 2

    env = os.environ.copy()
    env["AQG_ROOT"] = aqg_root.as_posix()
    env["CLAUDE_PROJECT_DIR"] = project_dir.as_posix()
    command = [bash, script.as_posix()]
    if args.hook in PROJECT_ARG_HOOKS:
        command.append(project_dir.as_posix())
    proc = subprocess.run(
        command,
        input=json.dumps(payload, ensure_ascii=False),
        text=True,
        encoding="utf-8",
        capture_output=True,
        env=env,
        check=False,
    )
    # Conservative: PI's stdout contract is not documented in this repo, and
    # before the hooks became model-visible its stdout was always empty. Suppress
    # the envelope so PI sees exactly what it saw before rather than a guess;
    # the reminder still reaches it on stderr, unchanged.
    _, residual_stdout = _aqg_split_envelope(proc.stdout)
    if residual_stdout.strip():
        print(residual_stdout, end="" if residual_stdout.endswith("\n") else "\n")
    if proc.stderr:
        print(proc.stderr, end="", file=sys.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
