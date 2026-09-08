#!/usr/bin/env python3
"""Install AQG lifecycle hooks for Codex without touching unrelated hooks.

Codex reads user hooks from ``$CODEX_HOME/hooks.json`` (default
``~/.codex/hooks.json``).  This installer is backup-first, idempotent, and
removes only commands routed through ``run_aqg_codex_hook.py`` with one of the
known AQG hook script identifiers.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from install_aqg_hooks import _backup, _write_settings


EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_INVALID = 3

RUNNER_NAME = "run_aqg_codex_hook.py"
MANAGED_ID = "aqg-codex-v1"
VERIFY_CODE = (
    "import hashlib,os,sys;"
    "blob=b''.join(os.path.basename(p).encode('utf-8')+b'\\0'+open(p,'rb').read()+b'\\0' "
    "for p in sys.argv[1:3]);"
    "digest=hashlib.sha256(blob).hexdigest();"
    "digest==sys.argv[4] or sys.exit(3);"
    "os.execv(sys.executable,[sys.executable,sys.argv[1],sys.argv[5],"
    "'--bundle-sha256',sys.argv[4],'--managed-id',sys.argv[7]])"
)
CODEX_HOOK_SCRIPTS = (
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
)


def _default_target() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return codex_home / "hooks.json"


def _resolve_aqg_root(value: str | None) -> Path | None:
    candidates: list[Path] = []
    if value:
        candidates.append(Path(value))
    if os.environ.get("AQG_ROOT"):
        candidates.append(Path(os.environ["AQG_ROOT"]))
    candidates.extend(Path(__file__).absolute().parents)
    for candidate in candidates:
        resolved = candidate.expanduser().absolute()
        if (resolved / "VERSION").is_file() and (resolved / "scripts").is_dir():
            return resolved
    return None


def _load_config(target: Path) -> dict[str, Any]:
    if not target.exists():
        return {}
    if target.is_symlink() or not target.is_file():
        raise ValueError(f"refusing non-regular hooks target: {target}")
    try:
        value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid UTF-8 JSON from {target}: {type(exc).__name__}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{target} must contain a JSON object")
    hooks = value.get("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError(f"{target} 'hooks' must be a JSON object")
    return value


def _bundle_digest(runner: Path, policy_script: Path) -> str:
    digest = hashlib.sha256()
    for path in (runner, policy_script):
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _command(
    python_executable: Path,
    runner: Path,
    policy_script: Path,
    script: str,
    bundle_sha256: str,
) -> tuple[str, str]:
    # The trusted command definition performs integrity verification before the
    # mutable runner is loaded. Any runner/policy change therefore alters the
    # definition digest and requires a new Codex /hooks trust decision.
    argv = [
        str(python_executable),
        "-c",
        VERIFY_CODE,
        str(runner),
        str(policy_script),
        "--bundle-sha256",
        bundle_sha256,
        script,
        "--managed-id",
        MANAGED_ID,
    ]
    posix = " ".join(shlex.quote(part) for part in argv)
    windows = subprocess.list2cmdline(argv)
    return posix, windows


def _hook(
    python_executable: Path,
    runner: Path,
    policy_script: Path,
    script: str,
    message: str,
) -> dict[str, Any]:
    command, command_windows = _command(
        python_executable,
        runner,
        policy_script,
        script,
        _bundle_digest(runner, policy_script),
    )
    return {
        "type": "command",
        "command": command,
        "commandWindows": command_windows,
        "timeout": 60,
        "statusMessage": f"AQG: {message}",
    }


def _aqg_hook_specs(aqg_root: Path, *, python_executable: Path) -> dict[str, list[dict[str, Any]]]:
    # Hooks must follow the managed root when the active version changes.
    root = aqg_root.expanduser().absolute()
    python_executable = python_executable.expanduser().resolve()
    if not python_executable.is_file():
        raise FileNotFoundError(f"Python executable missing: {python_executable}")
    runner = root / "scripts" / RUNNER_NAME
    hooks_dir = root / "agent-packs" / "claude-code" / "hooks"
    if not runner.is_file():
        raise FileNotFoundError(f"Codex hook runner missing: {runner}")
    for script in CODEX_HOOK_SCRIPTS:
        if not (hooks_dir / script).is_file():
            raise FileNotFoundError(f"AQG hook script missing: {hooks_dir / script}")

    make = lambda script, message: _hook(
        python_executable, runner, hooks_dir / script, script, message
    )
    return {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [make("pretooluse_bash_skill_validator.sh", "validate AQG skill changes")],
            },
            {
                "matcher": "apply_patch",
                "hooks": [make("pretooluse_memory_write_guard.sh", "guard local memory writes")],
            },
            {
                "matcher": "Bash|apply_patch",
                "hooks": [make("pretooluse_secret_scan.sh", "scan proposed secrets")],
            },
            {
                # apply_patch is Codex's file-edit tool, the counterpart of the
                # Write|Edit|MultiEdit matcher this guard uses on Claude Code. The
                # self-development carve-out needs cwd, which run_aqg_codex_hook.py
                # forwards as CLAUDE_PROJECT_DIR and as the child's working dir.
                "matcher": "apply_patch",
                "hooks": [make("pretooluse_aqg_tamper_guard.sh", "deny edits that neuter AQG gates")],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [make("posttooluse_bash_error_debugging_reminder.sh", "check shell failures")],
            },
            {
                "matcher": "Bash|apply_patch",
                # Its own entry, and the ONLY reminder mounted on Bash. A Bash heredoc
                # writes files and carries no file_path, which is the defect this closes;
                # but the edit-tool entry below registers three other reminders that all
                # read file_path, so widening THAT entry would invoke them on every shell
                # call for them to exit immediately (aud_oqv59VQ00_EhRJkU opus-f6). One
                # script, one canonical matcher -- mounting it twice makes the installer
                # warn about double-fire.
                "hooks": [
                    make("posttooluse_code_construction_reminder.sh", "check construction discipline"),
                ],
            },
            {
                "matcher": "apply_patch",
                "hooks": [
                    make("posttooluse_skill_edit_reminder.sh", "check AQG skill edits"),
                    make("posttooluse_test_quality_reminder.sh", "check test quality"),
                    make("posttooluse_security_review_reminder.sh", "check security-sensitive edits"),
                ],
            },
        ],
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [
                    make("precompact_closeout_reminder.sh", "check closeout before compaction"),
                    make("wip_checkpoint_save.sh", "save WIP checkpoint"),
                ],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                # `precompact_closeout_reminder.sh` is deliberately NOT here —
                # same reasoning as the Claude installer. Stop fires at the end of
                # every assistant turn, and that hook emits its 14-line completion
                # checklist unconditionally with no dedup, so mounting it here asks
                # "have you answered the six closeout questions?" after every reply.
                # PreCompact keeps it: that is the trigger that means context is
                # about to be lost. Kept in step by
                # test_shared_hooks_mount_on_the_same_events_in_both_installers.
                "hooks": [
                    make("wip_checkpoint_save.sh", "save WIP checkpoint"),
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "startup|resume|clear|compact",
                "hooks": [
                    make("sessionstart_preflight.sh", "run startup preflight"),
                    make("wip_checkpoint_recover.sh", "surface WIP checkpoints"),
                    # Managed update check — a trigger only, and deliberately absent
                    # from BLOCKING_SCRIPTS in run_aqg_codex_hook.py: it starts a
                    # detached process and returns, so a slow remote can never stop
                    # a session from starting. Same mount as the Claude installer,
                    # which test_shared_hooks_mount_on_the_same_events_in_both_installers
                    # keeps it in step with.
                    make("sessionstart_update_check.sh", "check for AQG updates"),
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [make("userpromptsubmit_handoff_mandate.sh", "route handoff requests")],
            }
        ],
    }


def _parse_command(value: Any, *, windows: bool) -> list[str] | None:
    if not isinstance(value, str):
        return None
    try:
        argv = shlex.split(value, posix=not windows)
    except ValueError:
        return None
    return [part.strip('"') for part in argv]


def _owned_script(hook: Any) -> str | None:
    if not isinstance(hook, dict):
        return None
    command = hook.get("command")
    command_windows = hook.get("commandWindows")
    def identify(argv: list[str] | None, *, windows: bool) -> tuple[str, bool] | None:
        if argv is None:
            return None
        path_type = PureWindowsPath if windows else PurePosixPath
        if len(argv) == 10 and argv[1] == "-c":
            runner, policy, digest_flag, bundle, script, marker_flag, marker = argv[3:]
            if (
                path_type(runner).name != RUNNER_NAME
                or script not in CODEX_HOOK_SCRIPTS
                or path_type(policy).name != script
                or digest_flag != "--bundle-sha256"
                or marker_flag != "--managed-id"
                or marker != MANAGED_ID
                or len(bundle) != 64
                or any(ch not in "0123456789abcdef" for ch in bundle)
            ):
                return None
            return script, True
        # Converge the direct-runner format shipped by the first Codex adapter,
        # plus the marker-free legacy form used during development.
        if len(argv) < 3 or path_type(argv[1]).name != RUNNER_NAME:
            return None
        script = argv[2]
        if script not in CODEX_HOOK_SCRIPTS:
            return None
        values: dict[str, str] = {}
        for index in range(3, len(argv) - 1, 2):
            values[argv[index]] = argv[index + 1]
        if len(argv) == 3:
            return script, False
        if values.get("--managed-id") != MANAGED_ID:
            return None
        bundle = values.get("--bundle-sha256", "")
        if len(bundle) != 64 or any(ch not in "0123456789abcdef" for ch in bundle):
            return None
        return script, True

    posix = identify(_parse_command(command, windows=False), windows=False)
    windows = identify(_parse_command(command_windows, windows=True), windows=True)
    # Current hooks carry an exact managed marker. One intact side is enough to
    # converge a partially corrupted definition. Legacy hooks had no marker, so
    # require both command forms to match exactly before claiming ownership.
    marked = [item for item in (posix, windows) if item and item[1]]
    if marked:
        scripts = {item[0] for item in marked}
        return scripts.pop() if len(scripts) == 1 else None
    if posix and windows and not posix[1] and not windows[1] and posix[0] == windows[0]:
        return posix[0]
    return None


def _iter_entries(hooks: dict[str, Any]):
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("hooks", []), list):
                continue
            for hook in block.get("hooks", []):
                yield event, block.get("matcher", ""), hook


def _merge_hooks(
    existing: dict[str, Any], canonical: dict[str, list[dict[str, Any]]]
) -> tuple[dict[str, Any], list[str]]:
    merged, removed = _strip_owned(existing)

    for event, blocks in canonical.items():
        target_blocks = merged.setdefault(event, [])
        if not isinstance(target_blocks, list):
            raise ValueError(f"hooks.{event} must be a list")
        for canonical_block in blocks:
            matcher = canonical_block.get("matcher", "")
            destination = next(
                (
                    block
                    for block in target_blocks
                    if isinstance(block, dict)
                    and block.get("matcher", "") == matcher
                    and isinstance(block.get("hooks", []), list)
                ),
                None,
            )
            if destination is None:
                destination = {"matcher": matcher, "hooks": []}
                target_blocks.append(destination)
            for canonical_hook in canonical_block["hooks"]:
                script = _owned_script(canonical_hook)
                assert script is not None
                destination["hooks"].append(copy.deepcopy(canonical_hook))
    if merged == existing:
        return merged, []
    changes = [f"converged {len(removed)} existing managed hook(s)"] if removed else []
    changes.extend(
        f"installed {event}/{block.get('matcher', '')}: {_owned_script(hook)}"
        for event, blocks in canonical.items()
        for block in blocks
        for hook in block["hooks"]
    )
    return merged, changes


def _strip_owned(hooks: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    result: dict[str, Any] = {}
    removed: list[str] = []
    for event, blocks in copy.deepcopy(hooks).items():
        if not isinstance(blocks, list):
            result[event] = blocks
            continue
        kept_blocks: list[Any] = []
        for block in blocks:
            if not isinstance(block, dict):
                kept_blocks.append(block)
                continue
            if "hooks" not in block or not isinstance(block["hooks"], list):
                raise ValueError(f"hooks.{event} block must contain a hooks list")
            kept_hooks = []
            removed_from_block = False
            for hook in block["hooks"]:
                script = _owned_script(hook)
                if script:
                    removed.append(script)
                    removed_from_block = True
                else:
                    kept_hooks.append(hook)
            if kept_hooks or not removed_from_block:
                block["hooks"] = kept_hooks
                kept_blocks.append(block)
        if kept_blocks:
            result[event] = kept_blocks
    return result, removed


def cmd_apply(target: Path, aqg_root: Path, python_executable: Path) -> int:
    try:
        config = _load_config(target)
        canonical = _aqg_hook_specs(aqg_root, python_executable=python_executable)
        merged_hooks, changes = _merge_hooks(config.get("hooks", {}), canonical)
    except (OSError, ValueError, FileNotFoundError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC
    if not changes:
        print(f"OK: AQG Codex on-disk hook definitions already match in {target} (no changes)")
        print(
            "NOTE: on-disk state does not prove Codex runtime discovery or /hooks trust. "
            "Restart Codex, then review and trust the definitions via /hooks."
        )
        return EXIT_OK
    if target.exists():
        backup = _backup(target, client="codex", installer="install_aqg_codex_hooks.py", aqg_root=aqg_root)
        if backup:
            print(f"OK: backup at {backup}")
    config["hooks"] = merged_hooks
    try:
        _write_settings(target, config)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC
    print(f"OK: wrote {target}")
    for change in changes:
        print(f"  - {change}")
    print("Next: restart Codex, open /hooks, and review/trust the AQG hook definitions.")
    return EXIT_OK


def cmd_uninstall(target: Path) -> int:
    try:
        config = _load_config(target)
        stripped, removed = _strip_owned(config.get("hooks", {}))
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC
    if not removed:
        print(f"OK: no AQG Codex hooks present in {target} (no-op)")
        return EXIT_OK
    if target.exists():
        backup = _backup(target, client="codex", installer="install_aqg_codex_hooks.py")
        if backup:
            print(f"OK: backup at {backup}")
    if stripped:
        config["hooks"] = stripped
    else:
        config.pop("hooks", None)
    try:
        _write_settings(target, config)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC
    print(f"OK: removed {len(removed)} AQG Codex hooks from {target}")
    return EXIT_OK


def cmd_is_installed(target: Path) -> int:
    try:
        config = _load_config(target)
    except ValueError:
        return EXIT_INVALID
    return EXIT_OK if any(_owned_script(hook) for _, _, hook in _iter_entries(config.get("hooks", {}))) else EXIT_GENERIC


def owned_command_paths(target: Path) -> tuple[Path, ...]:
    """Absolute paths the AQG-owned hook commands in *target* will execute.

    These are what makes a Codex install version-PINNED: the command names the
    runner and policy script by absolute path and carries their combined
    ``--bundle-sha256``, so it keeps executing the tree it was written for even
    after the AQG root symlink moves. Two callers need to know that:

    * the apply gate, so a pending hook change for this host does not freeze
      the whole machine (the swap cannot strand a command that never followed
      the root); and
    * ``prune_versions``, so the tree those paths live in is never deleted.

    Read through ``_owned_script`` rather than by pattern-matching the command
    text, because a looser match would also claim hooks a user wrote by hand —
    and neither deferring nor protecting is a decision AQG may make about
    configuration it does not own.

    Unreadable or unowned config yields ``()``. Callers treat "no pins" as
    "no evidence a swap is safe", so silence here is the conservative answer.
    """
    try:
        config = _load_config(target)
    except (ValueError, OSError):
        return ()
    found: list[Path] = []
    for _event, _matcher, hook in _iter_entries(config.get("hooks", {})):
        if not _owned_script(hook):
            continue
        # Only THIS platform's variant. A config carrying both is normal, and
        # its Windows paths do not exist on a POSIX machine — collecting them
        # into one set that a caller then requires to exist IN FULL would make
        # every such host permanently undeferrable, and would ask the pruner to
        # protect trees that are not there.
        key, windows = (
            ("commandWindows", True) if os.name == "nt" else ("command", False)
        )
        argv = _parse_command(hook.get(key), windows=windows)
        # The verified form is `python -c VERIFY_CODE runner policy ...`;
        # argv[3] and argv[4] are the two files the digest covers and the only
        # ones the command opens.
        if argv and len(argv) == 10 and argv[1] == "-c":
            found.extend(Path(part).expanduser() for part in argv[3:5])
    return tuple(sorted(set(found)))


def inspect_install(
    target: Path, aqg_root: Path, python_executable: Path | None = None
) -> tuple[str, str]:
    """Return ``(missing|complete|stale|invalid, detail)`` without printing."""
    try:
        config = _load_config(target)
    except ValueError as exc:
        return "invalid", str(exc)
    if not any(_owned_script(hook) for _, _, hook in _iter_entries(config.get("hooks", {}))):
        return "missing", f"no AQG Codex hooks in {target}"
    try:
        canonical = _aqg_hook_specs(
            aqg_root,
            python_executable=(python_executable or Path(sys.executable)),
        )
    except (OSError, ValueError, FileNotFoundError) as exc:
        return "invalid", str(exc)

    for _event, _matcher, hook in _iter_entries(config.get("hooks", {})):
        if not _owned_script(hook):
            continue
        for key, windows in (("command", False), ("commandWindows", True)):
            argv = _parse_command(hook.get(key), windows=windows)
            if not argv:
                return "stale", f"managed hook has invalid {key}"
            interpreter = Path(argv[0]).expanduser()
            runnable = interpreter.is_file() and (
                os.name == "nt" or os.access(interpreter, os.X_OK)
            )
            if not runnable:
                return "stale", f"managed hook interpreter is missing or not executable: {interpreter}"

    def signatures(hooks: dict[str, Any]) -> list[tuple[Any, ...]]:
        result = []
        for event, matcher, hook in _iter_entries(hooks):
            script = _owned_script(hook)
            if not script:
                continue
            try:
                command = _parse_command(hook.get("command", ""), windows=False) or []
                windows = _parse_command(hook.get("commandWindows", ""), windows=True) or []
            except (TypeError, ValueError):
                command, windows = [], []
            result.append(
                (
                    event,
                    matcher,
                    script,
                    tuple(command[1:]) if command else ("<invalid>",),
                    tuple(windows[1:]) if windows else ("<invalid>",),
                    hook.get("type"),
                    hook.get("timeout"),
                    hook.get("statusMessage"),
                )
            )
        return sorted(result, key=repr)

    installed_signatures = signatures(config.get("hooks", {}))
    canonical_signatures = signatures(canonical)
    if installed_signatures != canonical_signatures:
        return "stale", "managed hook definitions differ from the current reviewed bundle"
    return "complete", f"{len(CODEX_HOOK_SCRIPTS)} managed scripts present"


def cmd_verify(target: Path, aqg_root: Path, python_executable: Path) -> int:
    status, detail = inspect_install(target, aqg_root, python_executable)
    if status == "missing":
        print("AQG Codex hooks: NOT INSTALLED")
        return EXIT_GENERIC
    if status != "complete":
        print(f"AQG Codex hooks: {status.upper()} ({detail})")
        return EXIT_GENERIC
    print(f"AQG Codex hooks: on-disk definitions verified in {target}")
    print(
        "NOTE: on-disk verification does not prove Codex runtime discovery or /hooks trust. "
        "Restart Codex, then review and trust the definitions via /hooks."
    )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install and manage AQG Codex lifecycle hooks")
    parser.add_argument("--target", help="hooks.json path (default: $CODEX_HOME/hooks.json)")
    parser.add_argument("--aqg-root", help="AQG checkout root")
    parser.add_argument("--python", dest="python_executable", default=sys.executable)
    actions = parser.add_mutually_exclusive_group(required=True)
    actions.add_argument("--apply", action="store_true")
    actions.add_argument("--verify", action="store_true")
    actions.add_argument("--uninstall", action="store_true")
    actions.add_argument("--is-installed", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    target = (
        Path(args.target).expanduser().absolute()
        if args.target
        else _default_target().expanduser().absolute()
    )
    if args.is_installed:
        return cmd_is_installed(target)
    if args.uninstall:
        return cmd_uninstall(target)
    root = _resolve_aqg_root(args.aqg_root)
    if root is None:
        print("ERROR: cannot resolve AQG root", file=sys.stderr)
        return EXIT_GENERIC
    python_executable = Path(args.python_executable).expanduser().resolve()
    if args.verify:
        return cmd_verify(target, root, python_executable)
    return cmd_apply(target, root, python_executable)


if __name__ == "__main__":
    raise SystemExit(main())
