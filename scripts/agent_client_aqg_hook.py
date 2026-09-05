#!/usr/bin/env python3
"""Thin AQG hook adapter for Trae-family and Devin CLI clients."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


MANAGED_ID = "aqg-agent-client-v1"
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
BLOCKING_HOOKS = frozenset(
    {"pretooluse_bash_skill_validator.sh", "pretooluse_secret_scan.sh"}
)
PROJECT_ARG_HOOKS = frozenset(
    {"sessionstart_preflight.sh", "wip_checkpoint_save.sh", "wip_checkpoint_recover.sh"}
)


def _find_bash() -> str | None:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _read_payload() -> dict[str, object]:
    try:
        raw = sys.stdin.buffer.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("hook input is not valid UTF-8") from exc
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def _project_dir(payload: dict[str, object]) -> Path:
    cwd = payload.get("cwd") or payload.get("workspaceRoot") or payload.get("workspace_root")
    if isinstance(cwd, str) and cwd:
        return Path(cwd).expanduser().resolve()
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



def _emit(client: str, *, denied: bool = False, context: str = "") -> None:
    if client.startswith("trae"):
        payload: dict[str, object] = {}
        if denied:
            payload["permissionDecision"] = "deny"
            payload["message"] = "AQG blocked this tool call; see hook stderr for the policy result."
        elif context:
            payload["additionalContext"] = context
        print(json.dumps(payload, ensure_ascii=False))
        return

    payload = {}
    if denied:
        payload["decision"] = "block"
        payload["reason"] = "AQG blocked this tool call; see hook stderr for the policy result."
    elif context:
        payload["additional_context"] = context
    print(json.dumps(payload, ensure_ascii=False))


def _degraded(client: str, hook: str, reason: str) -> int:
    print(f"[aqg agent-client-adapter] degraded: {reason}", file=sys.stderr)
    if hook in BLOCKING_HOOKS:
        _emit(client, denied=True)
        return 2
    _emit(client, context=f"AQG hook degraded: {reason}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run an AQG hook for Trae or Devin")
    parser.add_argument("--client", choices=("trae", "trae-cn", "devin"), required=True)
    parser.add_argument("--aqg-root", required=True)
    parser.add_argument("--hook", choices=sorted(HOOK_SCRIPTS), required=True)
    parser.add_argument("--managed-id", required=True)
    args = parser.parse_args(argv)

    if args.managed_id != MANAGED_ID:
        return _degraded(args.client, args.hook, "unrecognized managed hook identity")
    try:
        payload = _read_payload()
    except (ValueError, json.JSONDecodeError) as exc:
        return _degraded(args.client, args.hook, str(exc))

    aqg_root = Path(args.aqg_root).expanduser().resolve()
    hooks_dir = (aqg_root / "agent-packs" / "claude-code" / "hooks").resolve()
    script = (hooks_dir / args.hook).resolve()
    try:
        script.relative_to(hooks_dir)
    except ValueError:
        return _degraded(args.client, args.hook, "hook path escaped the AQG hook directory")
    if not (aqg_root / "VERSION").is_file() or not script.is_file():
        return _degraded(args.client, args.hook, "AQG checkout or hook script is unavailable")

    project_dir = _project_dir(payload)
    if not project_dir.is_dir():
        return _degraded(args.client, args.hook, "hook payload cwd is not an existing directory")
    bash = _find_bash()
    if bash is None:
        return _degraded(args.client, args.hook, "bash is unavailable")

    env = os.environ.copy()
    env["AQG_ROOT"] = aqg_root.as_posix()
    env["CLAUDE_PROJECT_DIR"] = project_dir.as_posix()
    command = [bash, script.as_posix()]
    if args.hook in PROJECT_ARG_HOOKS:
        command.append(project_dir.as_posix())
    try:
        proc = subprocess.run(
            command,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            capture_output=True,
            env=env,
            check=False,
        )
    except OSError as exc:
        return _degraded(args.client, args.hook, f"hook invocation failed: {type(exc).__name__}")

    # stdout may now carry the model-context envelope. Prefer its decoded text:
    # concatenating both channels shipped Trae and Devin the same reminder twice,
    # once as a raw escaped-JSON blob.
    envelope, residual_stdout = _aqg_split_envelope(proc.stdout)
    stderr = proc.stderr.strip()
    # stderr is normally the SAME text the envelope carries, so including both
    # would deliver the reminder twice. But it is not guaranteed to be: a hook can
    # write a DEGRADED line there, and an envelope with an empty payload must not
    # blank out a reminder that stderr still holds. Keep stderr whenever it says
    # something the envelope does not.
    parts = [p for p in (envelope, residual_stdout.strip()) if p]
    if stderr and stderr not in envelope:
        parts.append(stderr)
    context = "\n".join(parts)
    if args.hook in BLOCKING_HOOKS and proc.returncode != 0:
        if context:
            print(context, file=sys.stderr)
        _emit(args.client, denied=True)
        return 2
    _emit(args.client, context=context)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
