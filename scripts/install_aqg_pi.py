#!/usr/bin/env python3
"""Install managed AQG support for the Pi coding agent."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

try:
    from aqg_skill_install import classify_install, create_windows_junction, remove_install, skill_link_source
except ModuleNotFoundError:
    from scripts.aqg_skill_install import classify_install, create_windows_junction, remove_install, skill_link_source

try:
    from _aqg_backup import BackupError, BackupSession, migrate_legacy
except ModuleNotFoundError:
    from scripts._aqg_backup import BackupError, BackupSession, migrate_legacy

try:
    from _aqg_interpreter import hook_interpreter
except ModuleNotFoundError:
    from scripts._aqg_interpreter import hook_interpreter


EXIT_OK = 0
EXIT_ERROR = 1
MANAGED_ID = "aqg-pi-v1"
CLIENT_ID = "pi"
SKILL_MARKER = ".aqg-pi-managed.json"
LINK_MARKER_DIR = "managed-links"
EXTENSION_NAME = "aqg-extension.ts"
EXTENSION_MARKER = "// AQG PI MANAGED EXTENSION: do not edit generated content"
REPORT_MARKER = "<!-- AQG PI SUPPORT REPORT: managed by AQG -->"
BACKUP_DIR = ".aqg-backups"  # legacy in-place dir, migrated into the central store
SKILL_INSTALL_MODES = ("link", "copy")

# Active central-store session for the current apply/uninstall run (set by
# _open_backups, cleared by _close_backups). All _backup calls route here.
_BACKUP: "BackupSession | None" = None
HOOK_SCRIPTS = (
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
)


class InstallError(RuntimeError):
    pass


def _resolve_aqg_root(value: str | None) -> Path:
    # The generated extension must follow the entrance across version swaps.
    root = Path(value).expanduser().absolute() if value else Path(__file__).absolute().parent.parent
    if not (root / "VERSION").is_file() or not (root / "skills").is_dir():
        raise InstallError(f"invalid AQG root: {root}")
    return root


def _client_root(args: argparse.Namespace) -> Path:
    if args.scope == "user":
        return Path(args.home).expanduser().resolve() / ".pi" / "agent"
    if args.project_root is None:
        raise InstallError("--project-root is required with --scope project")
    project = Path(args.project_root).expanduser().resolve()
    if not project.is_dir():
        raise InstallError(f"project root does not exist: {project}")
    return project / ".pi"


def _skill_sources(aqg_root: Path) -> list[Path]:
    return sorted(
        path for path in (aqg_root / "skills").glob("aqg-*") if (path / "SKILL.md").is_file()
    )


def _atomic_write(path: Path, text: str) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing to write through symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".tmp-aqg-pi-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _open_backups(root: Path, args: argparse.Namespace, aqg_root: Path) -> None:
    """Start a central-store session for this run, migrating any legacy dir first."""
    global _BACKUP
    scope = args.scope
    # Hash the abs project root (the dir the user targets), not the client subdir,
    # so the same project maps to the same scope namespace across all clients.
    project_root = Path(args.project_root).expanduser().resolve() if scope == "project" else None
    legacy = root / BACKUP_DIR
    if legacy.is_dir() and not legacy.is_symlink():
        migrate_legacy(
            CLIENT_ID,
            root,
            [(legacy, "_legacy/aqg-backups")],
            scope=scope,
            project_root=project_root,
            installer="install_aqg_pi.py",
            aqg_root=aqg_root,
        )
    _BACKUP = BackupSession(
        CLIENT_ID,
        root,
        scope=scope,
        project_root=project_root,
        installer="install_aqg_pi.py",
        aqg_root=aqg_root,
    )


def _close_backups(ok: bool) -> None:
    """Finalize the run's session: keep+prune on success, discard on failure."""
    global _BACKUP
    session = _BACKUP
    _BACKUP = None
    if session is None:
        return
    if ok:
        session.close()
        session.gc()
    else:
        session.discard()


def _backup(path: Path, root: Path) -> Path | None:
    if _BACKUP is None:  # defensive: apply/uninstall always open a session first
        raise InstallError("internal error: backup session not open")
    return _BACKUP.backup(path)


def _copytree_digest(root: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise InstallError(f"refusing to hash symlinked skill content: {path}")
        if not path.is_file() or path.name == SKILL_MARKER:
            continue
        digest.update(path.relative_to(root).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _is_link_install(path: Path) -> bool:
    return classify_install(path) in {"symlink", "junction"}


def _marker_path(target: Path) -> Path:
    if _is_link_install(target):
        return target.parent.parent / LINK_MARKER_DIR / f"{target.name}.json"
    return target / SKILL_MARKER


def _read_marker_file(marker: Path) -> dict[str, object] | None:
    if marker.is_symlink() or not marker.is_file():
        return None
    try:
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) and value.get("managed_by") == MANAGED_ID else None


def _read_marker(target: Path) -> dict[str, object] | None:
    return _read_marker_file(_marker_path(target))


def _same_path(left: Path, right: Path) -> bool:
    return os.path.normcase(os.path.realpath(left)) == os.path.normcase(os.path.realpath(right))


def _create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError:
        if not create_windows_junction(source, target):
            raise


def _remove_link(path: Path) -> None:
    marker = _marker_path(path)
    remove_install(path)
    if _read_marker_file(marker) is not None:
        marker.unlink()


def _install_skill(source: Path, target: Path, root: Path, mode: str) -> bool:
    digest = _copytree_digest(source)
    expected = {
        "managed_by": MANAGED_ID,
        "mode": mode,
        "source": str(skill_link_source(source) if mode == "link" else source.resolve()),
        "source_digest": digest,
    }
    marker = _read_marker(target) if target.exists() or target.is_symlink() else None
    if marker == expected:
        if mode == "link" and _is_link_install(target) and _same_path(target, source):
            return False
        if mode == "copy" and target.is_dir() and _copytree_digest(target) == digest:
            return False
    if target.exists() or target.is_symlink():
        if marker is None:
            raise InstallError(f"refusing to overwrite unmanaged skill: {target}")
        if _is_link_install(target):
            _remove_link(target)
        else:
            _backup(target, root)
            shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    if mode == "link":
        _create_link(skill_link_source(source), target)
        _atomic_write(_marker_path(target), json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
        return True
    shutil.copytree(source, target)
    _atomic_write(_marker_path(target), json.dumps(expected, ensure_ascii=False, indent=2) + "\n")
    return True


def _command_argv(aqg_root: Path, script: str) -> list[str]:
    return [
        hook_interpreter(),
        str((aqg_root / "scripts" / "pi_aqg_hook.py").absolute()),
        "--aqg-root",
        str(aqg_root.absolute()),
        "--hook",
        script,
        "--managed-id",
        MANAGED_ID,
    ]


def _render_extension(aqg_root: Path) -> str:
    for script in HOOK_SCRIPTS:
        if not (aqg_root / "agent-packs" / "claude-code" / "hooks" / script).is_file():
            raise InstallError(f"AQG hook script is missing: {script}")
    commands = {script: _command_argv(aqg_root, script) for script in HOOK_SCRIPTS}
    return (
        f"{EXTENSION_MARKER}\n"
        "import type { ExtensionAPI } from \"@earendil-works/pi-coding-agent\";\n\n"
        f"const AQG_MANAGED_ID = {json.dumps(MANAGED_ID)};\n"
        f"const AQG_COMMANDS: Record<string, string[]> = {json.dumps(commands, ensure_ascii=False, indent=2)};\n\n"
        "async function runAqg(script: string, payload: unknown): Promise<{ blocked: boolean; text: string }> {\n"
        "  const command = AQG_COMMANDS[script];\n"
        "  const proc = Bun.spawn(command, { stdin: \"pipe\", stdout: \"pipe\", stderr: \"pipe\" });\n"
        "  await proc.stdin.write(JSON.stringify(payload));\n"
        "  proc.stdin.end();\n"
        "  const [stdout, stderr, exitCode] = await Promise.all([new Response(proc.stdout).text(), new Response(proc.stderr).text(), proc.exited]);\n"
        "  return { blocked: exitCode !== 0, text: [stdout.trim(), stderr.trim()].filter(Boolean).join(\"\\n\") };\n"
        "}\n\n"
        "export default function (pi: ExtensionAPI) {\n"
        "  pi.on(\"session_start\", async (event, ctx) => {\n"
        "    await runAqg(\"sessionstart_preflight.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "    await runAqg(\"wip_checkpoint_recover.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        # Managed update check. `runAqg` reports `blocked` on a non-zero exit and
        # this call ignores it, which is the whole point: the trigger backgrounds
        # its work and returns, and a slow remote must never delay or fail a
        # session start.
        "    await runAqg(\"sessionstart_update_check.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "  });\n\n"
        "  pi.on(\"tool_call\", async (event, ctx) => {\n"
        "    const payload = { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID };\n"
        "    if ([\"bash\", \"shell\", \"write\", \"edit\", \"multiedit\"].includes(String(event.toolName).toLowerCase())) {\n"
        "      const secret = await runAqg(\"pretooluse_secret_scan.sh\", payload);\n"
        "      if (secret.blocked) return { block: true, reason: secret.text || \"AQG secret scan blocked this tool call\" };\n"
        "    }\n"
        "    if ([\"bash\", \"shell\"].includes(String(event.toolName).toLowerCase())) {\n"
        "      const skill = await runAqg(\"pretooluse_bash_skill_validator.sh\", payload);\n"
        "      if (skill.blocked) return { block: true, reason: skill.text || \"AQG skill validator blocked this tool call\" };\n"
        "    }\n"
        "  });\n\n"
        "  pi.on(\"tool_result\", async (event, ctx) => {\n"
        "    const payload = { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID };\n"
        "    await runAqg(\"posttooluse_bash_error_debugging_reminder.sh\", payload);\n"
        "    await runAqg(\"posttooluse_skill_edit_reminder.sh\", payload);\n"
        "    await runAqg(\"posttooluse_code_construction_reminder.sh\", payload);\n"
        "    await runAqg(\"posttooluse_test_quality_reminder.sh\", payload);\n"
        "    await runAqg(\"posttooluse_security_review_reminder.sh\", payload);\n"
        "  });\n\n"
        "  pi.on(\"input\", async (event, ctx) => {\n"
        "    await runAqg(\"userpromptsubmit_handoff_mandate.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "  });\n\n"
        "  pi.on(\"session_before_compact\", async (event, ctx) => {\n"
        "    await runAqg(\"precompact_closeout_reminder.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "    await runAqg(\"wip_checkpoint_save.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "  });\n\n"
        "  pi.on(\"session_shutdown\", async (event, ctx) => {\n"
        "    await runAqg(\"precompact_closeout_reminder.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "    await runAqg(\"wip_checkpoint_save.sh\", { event, cwd: ctx.cwd, managedId: AQG_MANAGED_ID });\n"
        "  });\n"
        "}\n"
    )


def _install_extension(path: Path, root: Path, aqg_root: Path) -> bool:
    rendered = _render_extension(aqg_root)
    if path.exists() or path.is_symlink():
        if path.is_symlink():
            raise InstallError(f"refusing to write extension through symlink: {path}")
        current = path.read_text(encoding="utf-8")
        if EXTENSION_MARKER not in current:
            raise InstallError(f"refusing to overwrite unmanaged extension: {path}")
        if current == rendered:
            return False
        _backup(path, root)
    _atomic_write(path, rendered)
    return True


def _render_report() -> str:
    return (
        f"{REPORT_MARKER}\n\n"
        "# AQG client support report: pi\n\n"
        "- support_level: partial\n"
        "- skills: supported\n"
        "- rules/instructions: partial (Pi skills carry model-visible AQG instructions; no separate AQG-managed rules file is installed)\n"
        "- MCP/connectors: unsupported (no stable Pi MCP configuration contract is implemented by AQG)\n"
        "- lifecycle hooks: partial (Pi extensions cover startup, tool_call, tool_result, input, compaction, shutdown)\n"
        "- blocking hook gate: partial (tool_call can block shell/secret/skill-validator gates; post-tool reminders are advisory)\n"
        "- degradation: Stop/closeout and WIP save depend on Pi session_before_compact/session_shutdown delivery; host runtime discovery/trust remains a user action.\n\n"
        "Official evidence:\n"
        "- https://pi.dev/docs/latest/extensions#events\n"
        "- https://pi.dev/docs/latest/skills\n"
        "- https://pi.dev/docs/latest\n"
    )


def _install_report(path: Path, root: Path) -> bool:
    rendered = _render_report()
    if path.exists():
        current = path.read_text(encoding="utf-8")
        if REPORT_MARKER not in current:
            raise InstallError(f"refusing to overwrite unmanaged support report: {path}")
        if current == rendered:
            return False
        _backup(path, root)
    _atomic_write(path, rendered)
    return True


def _check_collisions(root: Path, aqg_root: Path, no_hooks: bool) -> None:
    for source in _skill_sources(aqg_root):
        target = root / "skills" / source.name
        if (target.exists() or target.is_symlink()) and _read_marker(target) is None:
            raise InstallError(f"refusing to overwrite unmanaged skill: {target}")
    extension = root / "extensions" / EXTENSION_NAME
    if not no_hooks and (extension.exists() or extension.is_symlink()):
        if extension.is_symlink() or EXTENSION_MARKER not in extension.read_text(encoding="utf-8"):
            raise InstallError(f"refusing to overwrite unmanaged extension: {extension}")
    report = root / "aqg-support-report.md"
    if report.exists() or report.is_symlink():
        if report.is_symlink() or REPORT_MARKER not in report.read_text(encoding="utf-8"):
            raise InstallError(f"refusing to overwrite unmanaged support report: {report}")


def _apply(args: argparse.Namespace, root: Path, aqg_root: Path) -> int:
    _check_collisions(root, aqg_root, args.no_hooks)
    changed = False
    for source in _skill_sources(aqg_root):
        changed = _install_skill(source, root / "skills" / source.name, root, args.mode) or changed
    if not args.no_hooks:
        changed = _install_extension(root / "extensions" / EXTENSION_NAME, root, aqg_root) or changed
    changed = _install_report(root / "aqg-support-report.md", root) or changed
    print(f"OK: AQG pi {args.scope} support {'updated' if changed else 'already current'} at {root}")
    print("support_level: partial")
    print("degradation: MCP/connectors unsupported; closeout/WIP save depend on Pi extension lifecycle delivery.")
    if args.no_hooks:
        print("INFO: lifecycle hooks skipped by --no-hooks")
    return EXIT_OK


def _skill_current(source: Path, target: Path, mode: str) -> bool:
    marker = _read_marker(target) if target.exists() or target.is_symlink() else None
    if marker is None or marker.get("mode") != mode:
        return False
    if mode == "link":
        return _is_link_install(target) and _same_path(target, source)
    return target.is_dir() and _copytree_digest(target) == marker.get("source_digest")


def _verify(args: argparse.Namespace, root: Path, aqg_root: Path) -> int:
    problems: list[str] = []
    for source in _skill_sources(aqg_root):
        if not _skill_current(source, root / "skills" / source.name, args.mode):
            problems.append(f"skill drift/missing: {source.name}")
    if not args.no_hooks:
        extension = root / "extensions" / EXTENSION_NAME
        if not extension.is_file() or EXTENSION_MARKER not in extension.read_text(encoding="utf-8"):
            problems.append("extension drift/missing")
    report = root / "aqg-support-report.md"
    if not report.is_file() or REPORT_MARKER not in report.read_text(encoding="utf-8"):
        problems.append("support report drift/missing")
    if problems:
        print("AQG pi support: NOT INSTALLED")
        for problem in problems:
            print(f"  - {problem}")
        return EXIT_ERROR
    print("AQG pi support: INSTALLED")
    print("support_level: partial")
    print(f"target: {root}")
    return EXIT_OK


def _remove_managed_skill(target: Path, root: Path) -> bool:
    marker = _read_marker(target) if target.exists() or target.is_symlink() else None
    if marker is None:
        return False
    if _is_link_install(target):
        _remove_link(target)
    else:
        _backup(target, root)
        shutil.rmtree(target)
    return True


def _remove_managed_file(path: Path, marker: str, root: Path) -> bool:
    if path.is_symlink():
        raise InstallError(f"refusing to remove through symlink: {path}")
    if not path.is_file():
        return False
    if marker not in path.read_text(encoding="utf-8"):
        return False
    _backup(path, root)
    path.unlink()
    return True


def _uninstall(args: argparse.Namespace, root: Path, aqg_root: Path) -> int:
    changed = False
    skills_root = root / "skills"
    if skills_root.is_dir():
        for target in sorted(skills_root.glob("aqg-*")):
            changed = _remove_managed_skill(target, root) or changed
    marker_root = root / LINK_MARKER_DIR
    if marker_root.is_dir() and not marker_root.is_symlink():
        for marker in marker_root.glob("aqg-*.json"):
            if _read_marker_file(marker) is not None:
                marker.unlink()
                changed = True
        if not any(marker_root.iterdir()):
            marker_root.rmdir()
    changed = _remove_managed_file(root / "extensions" / EXTENSION_NAME, EXTENSION_MARKER, root) or changed
    changed = _remove_managed_file(root / "aqg-support-report.md", REPORT_MARKER, root) or changed
    print(f"OK: AQG pi {args.scope} support {'uninstalled' if changed else 'not installed'} at {root}")
    return EXIT_OK


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Install AQG support for Pi")
    parser.add_argument("--scope", choices=("user", "project"), default="user")
    parser.add_argument("--home", type=Path, default=Path.home())
    parser.add_argument("--project-root", type=Path)
    parser.add_argument("--aqg-root")
    parser.add_argument("--mode", choices=SKILL_INSTALL_MODES, default="link")
    parser.add_argument("--no-hooks", action="store_true")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        aqg_root = _resolve_aqg_root(args.aqg_root)
        root = _client_root(args)
        if args.is_installed:
            return EXIT_OK if _verify(args, root, aqg_root) == EXIT_OK else EXIT_ERROR
        if args.verify:
            return _verify(args, root, aqg_root)
        # apply / uninstall mutate managed files -> run inside a central-store session
        action = _apply if args.apply else _uninstall
        _open_backups(root, args, aqg_root)
        ok = False
        try:
            result = action(args, root, aqg_root)
            ok = result == EXIT_OK
            return result
        finally:
            _close_backups(ok)
    except (OSError, InstallError, BackupError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
