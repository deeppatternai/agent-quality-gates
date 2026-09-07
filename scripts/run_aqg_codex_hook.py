#!/usr/bin/env python3
"""Codex schema adapter for the existing AQG hook scripts.

The policy logic stays in ``agent-packs/claude-code/hooks``.  This runner
normalizes Codex ``apply_patch`` and Bash payloads, supplies stable runtime
paths, and converts legacy hook text into Codex's model-visible JSON output.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


FILE_REMINDER_SCRIPTS = frozenset(
    {
        "posttooluse_skill_edit_reminder.sh",
        "posttooluse_code_construction_reminder.sh",
        "posttooluse_test_quality_reminder.sh",
        "posttooluse_security_review_reminder.sh",
    }
)
BLOCKING_SCRIPTS = frozenset(
    {
        "pretooluse_bash_skill_validator.sh",
        "pretooluse_memory_write_guard.sh",
        "pretooluse_secret_scan.sh",
        "pretooluse_aqg_tamper_guard.sh",
    }
)
PROJECT_ARG_SCRIPTS = frozenset(
    {"sessionstart_preflight.sh", "wip_checkpoint_save.sh", "wip_checkpoint_recover.sh"}
)
MAX_CONTEXT_CHARS = 12_000
MANAGED_ID = "aqg-codex-v1"
MANAGED_SCRIPTS = frozenset(
    {
        "pretooluse_bash_skill_validator.sh",
        "pretooluse_memory_write_guard.sh",
        "pretooluse_secret_scan.sh",
        "pretooluse_aqg_tamper_guard.sh",
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


@dataclass(frozen=True)
class HookRun:
    returncode: int
    stdout: str
    stderr: str


def _extract_patch_files(command: str) -> list[tuple[str, str, bool]]:
    files: list[tuple[str, list[str], bool]] = []
    current: tuple[str, list[str], bool] | None = None
    header = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$")
    move_header = re.compile(r"^\*\*\* Move to: (.+)$")
    for line in command.splitlines():
        match = header.match(line)
        if match:
            current = (match.group(1).strip(), [], False)
            files.append(current)
            continue
        move_match = move_header.match(line)
        if move_match and current is not None:
            current = (move_match.group(1).strip(), current[1], True)
            files[-1] = current
            continue
        if current is not None and line.startswith("+"):
            current[1].append(line[1:])
    # apply_patch exposes only added lines, never the complete post-move file.
    # Any move is therefore uninspectable even when the patch also adds text.
    return [(path, "\n".join(lines), moved) for path, lines, moved in files]


def _resolve_patch_path(path: str, payload: dict[str, Any]) -> str:
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    return os.path.abspath(os.path.normpath(str(candidate)))


def _normalize_bash_response(payload: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(payload)
    response = result.get("tool_response")
    if isinstance(response, dict):
        normalized = copy.deepcopy(response)
        exit_code = normalized.get("exit_code", normalized.get("exitCode"))
        normalized["is_error"] = bool(normalized.get("is_error")) or (
            isinstance(exit_code, int) and exit_code != 0
        )
        result["tool_response"] = normalized
    return result


def _translate_payloads(script: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    tool_name = payload.get("tool_name")
    if tool_name == "Bash":
        translated = _normalize_bash_response(payload)
        tool_input = translated.get("tool_input")
        if not isinstance(tool_input, dict):
            return [] if script in BLOCKING_SCRIPTS else [translated]
        command = tool_input.get("command")
        if isinstance(command, list) and all(isinstance(part, str) for part in command):
            tool_input["command"] = " ".join(command)
        elif not isinstance(command, str):
            return [] if script in BLOCKING_SCRIPTS else [translated]
        return [translated]
    if tool_name != "apply_patch":
        return [copy.deepcopy(payload)]
    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str):
        command = ""
    files = _extract_patch_files(command)
    if script == "pretooluse_secret_scan.sh":
        if not files:
            return []
        translated = copy.deepcopy(payload)
        translated["tool_name"] = "Write"
        paths = [_resolve_patch_path(path, payload) for path, _body, _unavailable in files]
        translated["tool_input"] = {
            "file_path": paths[0],
            "file_paths": paths,
            "content": "\n".join(body for _path, body, _unavailable in files),
        }
        return [translated]
    if script == "pretooluse_memory_write_guard.sh":
        translated_items = []
        for path, body, content_unavailable in files:
            translated = copy.deepcopy(payload)
            translated["tool_name"] = "Write"
            translated["tool_input"] = {
                "file_path": _resolve_patch_path(path, payload),
                "content": body,
                "aqg_content_unavailable": content_unavailable,
            }
            translated_items.append(translated)
        return translated_items
    if script in FILE_REMINDER_SCRIPTS:
        translated_items = []
        for path, _body, _content_unavailable in files:
            translated = copy.deepcopy(payload)
            translated["tool_name"] = "Edit"
            translated["tool_input"] = {"file_path": _resolve_patch_path(path, payload)}
            translated_items.append(translated)
        return translated_items
    return [copy.deepcopy(payload)]


def _hook_environment(
    aqg_root: Path, payload: dict[str, Any], *, base_env: Mapping[str, str] | None = None
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    home = env.get("HOME") or env.get("USERPROFILE") or str(Path.home())
    codex_home = env.get("CODEX_HOME") or str(Path(home) / ".codex")
    env.update(
        {
            "AQG_ROOT": str(aqg_root),
            "AQG_CLIENT": "codex",
            "CLAUDE_PROJECT_DIR": cwd,
            "AQG_MEMORY_ROOT": str(Path(codex_home) / "memories"),
        }
    )
    return env


def _find_bash() -> str | None:
    explicit = os.environ.get("AQG_BASH")
    if explicit and Path(explicit).is_file():
        return explicit
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for candidate in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _compact_text(results: list[HookRun]) -> str:
    parts = []
    for result in results:
        for value in (result.stdout, result.stderr):
            clean = value.strip()
            if clean:
                parts.append(clean)
    return "\n".join(parts)[:MAX_CONTEXT_CHARS]


def _json_stdout(results: list[HookRun]) -> dict[str, Any] | None:
    for result in results:
        text = result.stdout.strip()
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _render_output(
    event: str,
    results: list[HookRun],
    original_payload: dict[str, Any],
    script: str | None = None,
) -> dict[str, Any] | str | None:
    if event == "PreToolUse":
        denied = next((result for result in results if result.returncode == 2), None)
        if denied:
            reason = (denied.stderr or denied.stdout or "AQG policy denied this tool call").strip()
            return {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason[:MAX_CONTEXT_CHARS],
                }
            }
    parsed = _json_stdout(results)
    if parsed is not None:
        return parsed
    text = _compact_text(results)
    if event == "PreToolUse":
        if not text:
            return None
        if any(result.returncode == 3 for result in results):
            return {"systemMessage": text}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": text,
            }
        }
    if event == "PostToolUse":
        if not text:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": text,
            }
        }
    if event in {"SessionStart", "UserPromptSubmit"}:
        if not text:
            return None
        return {
            "hookSpecificOutput": {
                "hookEventName": event,
                "additionalContext": text,
            }
        }
    if event == "Stop":
        if original_payload.get("stop_hook_active"):
            return {}
        if not text:
            return {}
        if script == "precompact_closeout_reminder.sh":
            return {"decision": "block", "reason": text}
        return {"systemMessage": text}
    if event in {"PreCompact", "PostCompact"}:
        return {"systemMessage": text} if text else None
    return {"systemMessage": text} if text else None


def _run_hook(
    script: str, payload: dict[str, Any], aqg_root: Path, *, timeout: float = 50
) -> HookRun:
    bash = _find_bash()
    if not bash:
        return HookRun(3, "", "[aqg codex-hook] DEGRADED: bash runtime not found")
    script_path = aqg_root / "agent-packs" / "claude-code" / "hooks" / script
    if not script_path.is_file():
        return HookRun(3, "", f"[aqg codex-hook] DEGRADED: hook script missing: {script}")
    argv = [bash, str(script_path)]
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else os.getcwd()
    if script in PROJECT_ARG_SCRIPTS:
        argv.append(cwd)
    try:
        completed = subprocess.run(
            argv,
            input=json.dumps(payload, ensure_ascii=False),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            cwd=cwd,
            env=_hook_environment(aqg_root, payload),
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return HookRun(3, "", f"[aqg codex-hook] DEGRADED: {type(exc).__name__}")
    return HookRun(completed.returncode, completed.stdout, completed.stderr)


def _run_translated(
    script: str,
    payloads: list[dict[str, Any]],
    aqg_root: Path,
    *,
    total_timeout: float = 50,
) -> list[HookRun]:
    """Run translated payloads within one hook-level wall-clock budget."""
    started = time.monotonic()
    results: list[HookRun] = []
    for payload in payloads:
        remaining = total_timeout - (time.monotonic() - started)
        if remaining <= 0:
            results.append(HookRun(3, "", "[aqg codex-hook] DEGRADED: hook time budget exhausted"))
            break
        results.append(_run_hook(script, payload, aqg_root, timeout=remaining))
    return results


def _bundle_digest(aqg_root: Path, script: str) -> str:
    digest = hashlib.sha256()
    for path in (
        Path(__file__).resolve(),
        aqg_root / "agent-packs" / "claude-code" / "hooks" / script,
    ):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _deny(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason[:MAX_CONTEXT_CHARS],
        }
    }


def _write_json(value: dict[str, Any]) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in {1, 5}:
        print(
            "usage: run_aqg_codex_hook.py <managed-script> "
            "[--bundle-sha256 SHA256 --managed-id aqg-codex-v1]",
            file=sys.stderr,
        )
        return 2
    script = args[0]
    if script not in MANAGED_SCRIPTS:
        print(f"ERROR: not a managed AQG hook: {script}", file=sys.stderr)
        return 2
    raw_payload = sys.stdin.read()
    try:
        payload = json.loads(raw_payload or "{}")
    except json.JSONDecodeError:
        if script in BLOCKING_SCRIPTS:
            _write_json(_deny("invalid hook payload; refusing to bypass AQG gate"))
            return 0
        payload = {}
    if not isinstance(payload, dict):
        if script in BLOCKING_SCRIPTS:
            _write_json(_deny("invalid hook payload shape; refusing to bypass AQG gate"))
            return 0
        payload = {}
    if script in BLOCKING_SCRIPTS and (
        payload.get("hook_event_name") != "PreToolUse"
        or not isinstance(payload.get("tool_name"), str)
        or not isinstance(payload.get("tool_input"), dict)
    ):
        _write_json(_deny("invalid PreToolUse payload; refusing to bypass AQG gate"))
        return 0
    aqg_root = Path(__file__).resolve().parent.parent
    if len(args) == 5:
        provided = dict(zip(args[1::2], args[2::2]))
        try:
            expected_digest = _bundle_digest(aqg_root, script)
        except OSError:
            event = str(payload.get("hook_event_name", ""))
            output = _render_output(
                event,
                [HookRun(3, "", "[aqg codex-hook] DEGRADED: cannot verify managed bundle; re-run installer")],
                payload,
                script,
            )
            if output is not None:
                _write_json(output if isinstance(output, dict) else {"systemMessage": output})
            return 0
        if (
            provided.get("--managed-id") != MANAGED_ID
            or provided.get("--bundle-sha256") != expected_digest
        ):
            event = str(payload.get("hook_event_name", ""))
            output = _render_output(
                event,
                [HookRun(3, "", "[aqg codex-hook] DEGRADED: managed bundle digest mismatch; re-run installer")],
                payload,
                script,
            )
            if output is not None:
                _write_json(output if isinstance(output, dict) else {"systemMessage": output})
            return 0
    translated = _translate_payloads(script, payload)
    if script in BLOCKING_SCRIPTS and not translated:
        _write_json(_deny("unrecognized blocking hook payload; refusing to bypass AQG gate"))
        return 0
    results = _run_translated(script, translated, aqg_root)
    output = _render_output(str(payload.get("hook_event_name", "")), results, payload, script)
    if output is not None:
        if isinstance(output, str):
            sys.stdout.write(output)
        else:
            json.dump(output, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
