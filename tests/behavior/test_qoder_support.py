from __future__ import annotations

import importlib.util
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path, PureWindowsPath

import pytest

REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_aqg_qoder.py"
ADAPTER = REPO / "agent-packs" / "qoder" / "hooks" / "qoder_hook_adapter.py"
MANAGED_MARKER = ".aqg-qoder-managed.json"
MANAGED_ID = "aqg-qoder-v1"
LINK_MARKER_DIR = "managed-links"

PROFILE_ROOTS = {
    "qoder": ".qoder",
    "qoder-cli": ".qoder",
    # R24-03: Qoder CN Desktop is `Qoder CN.app` (com.qodercn.app) and reads
    # `~/.qoder-cn`; `.lingma` is the legacy Tongyi Lingma root, not this product.
    "qoder-cn": ".qoder-cn",
    "qoder-cli-cn": ".qoder-cn",
}

PROFILE_LEVELS = {
    "qoder": "partial",
    "qoder-cli": "full",
    "qoder-cn": "partial",
    "qoder-cli-cn": "full",
}

IDE_EVENTS = {
    "UserPromptSubmit",
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "Stop",
}


CENTRAL_DIRNAME = ".aqg-central"


@pytest.fixture(autouse=True)
def _isolate_central_backups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Route every installer run's central backup store into this test's tmp dir.

    Both the subprocess installer (inherits ``os.environ``) and in-process module
    loads (read ``AQG_BACKUP_DIR`` live) pick this up, so backups never land in
    ``dirname(AQG_ROOT)/aqg-backups`` (the repo).
    """
    central = tmp_path / CENTRAL_DIRNAME
    monkeypatch.setenv("AQG_BACKUP_DIR", str(central))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)
    return central


def central_relpaths(central: Path) -> list[str]:
    """Every backed-up entry relpath across all manifests in the central store."""
    rels: list[str] = []
    for manifest in central.rglob("manifest.json"):
        data = json.loads(manifest.read_text(encoding="utf-8"))
        rels.extend(entry["relpath"] for entry in data.get("entries", []))
    return rels


def run_installer(
    *args: str, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    process_env = os.environ.copy()
    process_env.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if env:
        process_env.update(env)
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        cwd=REPO,
        env=process_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=60,
    )


def apply(
    profile: str,
    home: Path,
    *extra: str,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return run_installer(
        "--client",
        profile,
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
        *extra,
        env=env,
    )


def aqg_commands(settings: dict) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for event, blocks in settings.get("hooks", {}).items():
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            for hook in block.get("hooks", []):
                command = hook.get("command", "") if isinstance(hook, dict) else ""
                if "qoder_hook_adapter.py" in command and "aqg-qoder-v1" in command:
                    found.append((event, block.get("matcher", ""), command))
    return found


def load_installer_module():
    spec = importlib.util.spec_from_file_location("install_aqg_qoder_test", INSTALLER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def managed_link_marker(skill: Path) -> Path:
    return skill.parent.parent / LINK_MARKER_DIR / f"{skill.name}.json"


def legacy_managed_link_marker(skill: Path) -> Path:
    return skill.parent / f".{skill.name}{MANAGED_MARKER}"


def is_link_install(path: Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return path.is_symlink() or bool(isjunction is not None and isjunction(path))


def create_link(source: Path, target: Path) -> None:
    try:
        target.symlink_to(source, target_is_directory=True)
        return
    except OSError as exc:
        if os.name != "nt":
            pytest.skip(f"directory symlinks unavailable: {exc}")
    cmd = shutil.which("cmd.exe") or shutil.which("cmd")
    if cmd is None:
        pytest.skip("cmd unavailable for junction fixture")
    proc = subprocess.run(
        [cmd, "/c", "mklink", "/J", str(target), str(source)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
        timeout=30,
    )
    if proc.returncode != 0 or not is_link_install(target):
        pytest.skip("directory link fixtures unavailable")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"\\?\C:\AQG\skill", r"C:\AQG\skill"),
        (r"\\?\UNC\server\share\skill", r"\\server\share\skill"),
        (r"\??\C:\AQG\skill", r"C:\AQG\skill"),
        (r"\??\UNC\server\share\skill", r"\\server\share\skill"),
    ],
)
def test_windows_link_target_normalization(raw: str, expected: str) -> None:
    installer = load_installer_module()

    assert installer._normalize_windows_link_target(raw) == expected


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length path behavior")
def test_managed_link_accepts_extended_length_readlink_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer_module()
    source = tmp_path / "source" / "aqg-fixture"
    source.mkdir(parents=True)
    target = tmp_path / "home" / ".qoder" / "skills" / source.name
    target.parent.mkdir(parents=True)
    marker = managed_link_marker(target)
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps(
            {
                "managed_by": MANAGED_ID,
                "mode": "link",
                "source": str(source.resolve()),
            }
        ),
        encoding="utf-8",
    )
    original_is_symlink = Path.is_symlink
    monkeypatch.setattr(
        Path,
        "is_symlink",
        lambda path: path == target or original_is_symlink(path),
    )
    monkeypatch.setattr(installer.os, "readlink", lambda _path: rf"\\?\{source.resolve()}")

    assert installer._managed_link_skill(target) is not None


def legacy_powershell_managed_command(
    script: str, *, python: str | None = None, root: Path = REPO
) -> str:
    fallback = 2 if script in {"pretooluse_bash_skill_validator.sh", "pretooluse_secret_scan.sh"} else 0
    values = [
        python or sys.executable,
        str(ADAPTER),
        "--aqg-root",
        str(root),
        "--hook",
        script,
        "--managed-id",
        MANAGED_ID,
    ]
    quoted = " ".join("'" + value.replace("'", "''") + "'" for value in values)
    return (
        "$ErrorActionPreference='Stop'; try { & "
        f"{quoted}; if ($null -eq $LASTEXITCODE) {{ exit {fallback} }}; "
        f"exit $LASTEXITCODE }} catch {{ exit {fallback} }}"
    )


def hook_command(settings: dict, script: str) -> str:
    return next(command for _, _, command in aqg_commands(settings) if script in command)


@pytest.mark.parametrize("profile", sorted(PROFILE_ROOTS))
def test_apply_installs_profile_without_touching_real_home(tmp_path: Path, profile: str) -> None:
    real_home = tmp_path / "real-home"
    requested_home = tmp_path / "fixture-home"
    sentinel = real_home / "sentinel.txt"
    real_home.mkdir()
    sentinel.write_text("keep", encoding="utf-8")

    proc = apply(
        profile,
        requested_home,
        env={"HOME": str(real_home), "USERPROFILE": str(real_home)},
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"support_level: {PROFILE_LEVELS[profile]}" in proc.stdout
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert list(real_home.iterdir()) == [sentinel]

    client_root = requested_home / PROFILE_ROOTS[profile]
    settings = json.loads((client_root / "settings.json").read_text(encoding="utf-8"))
    commands = aqg_commands(settings)
    events = {event for event, _, _ in commands}
    if "cli" in profile:
        assert events == IDE_EVENTS | {"SessionStart", "PreCompact"}
        assert any("wip_checkpoint_save.sh" in command for _, _, command in commands)
        assert any("wip_checkpoint_recover.sh" in command for _, _, command in commands)
    else:
        assert events == IDE_EVENTS
        assert "SessionStart" not in settings["hooks"]
        assert "PreCompact" not in settings["hooks"]
        assert not any("wip_checkpoint_save.sh" in command for _, _, command in commands)
        assert not any("wip_checkpoint_recover.sh" in command for _, _, command in commands)

    source_skills = sorted(path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir())
    installed_skills = sorted(path.name for path in (client_root / "skills").glob("aqg-*"))
    assert installed_skills == source_skills
    for skill_name in installed_skills:
        skill_dir = client_root / "skills" / skill_name
        source_dir = REPO / "skills" / skill_name
        assert is_link_install(skill_dir)
        assert skill_dir.resolve() == source_dir.resolve()
        marker = json.loads(managed_link_marker(skill_dir).read_text(encoding="utf-8"))
        assert marker["managed_by"] == MANAGED_ID
        assert marker["mode"] == "link"
        assert marker["source"] == str(source_dir.resolve())
        assert not legacy_managed_link_marker(skill_dir).exists()
    assert not any(path.name.startswith(".aqg-") for path in (client_root / "skills").iterdir())

    rule_path = client_root / "rules" / "aqg.md"
    if "cli" in profile:
        rule = rule_path.read_text(encoding="utf-8")
        assert "AQG-MANAGED: aqg-qoder-v1" in rule
        assert "Agent Quality Gates (AQG) engineering discipline" in rule
    else:
        assert not rule_path.exists()


def test_windows_hook_generation_uses_bash_compatible_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = load_installer_module()
    monkeypatch.setattr(installer.os, "name", "nt")
    monkeypatch.setattr(
        installer.sys,
        "executable",
        r"C:\Users\Admin\AppData\Local\Python\pythoncore-3.14-64\python.exe",
    )
    aqg_root = PureWindowsPath(r"C:\Users\Admin\.deeppattern\agent-quality-gates")

    blocking = installer._hook_command(aqg_root, "pretooluse_secret_scan.sh")
    nonblocking = installer._hook_command(aqg_root, "userpromptsubmit_handoff_mandate.sh")

    for command in (blocking, nonblocking):
        assert "$ErrorActionPreference" not in command
        assert "try {" not in command
        assert "catch {" not in command
        assert "qoder_hook_adapter.py" in command
        shlex.split(command.removesuffix(" || exit 0").split("; aqg_rc=$?;", 1)[0])
    assert blocking.endswith(" || exit 2")
    assert nonblocking.endswith(" || exit 0")


def test_installed_hook_commands_parse_and_preserve_exit_semantics(tmp_path: Path) -> None:
    home = tmp_path / "home"
    installed = apply("qoder", home)
    assert installed.returncode == 0, installed.stdout + installed.stderr
    settings = json.loads((home / ".qoder" / "settings.json").read_text(encoding="utf-8"))
    commands = [command for _, _, command in aqg_commands(settings)]
    assert commands
    for command in commands:
        assert "$ErrorActionPreference" not in command
        assert "try {" not in command
        assert "catch {" not in command

    bash = shutil.which("bash")
    if bash is None:
        adapter_proc = subprocess.run(
            [
                sys.executable,
                str(ADAPTER),
                "--aqg-root",
                str(REPO),
                "--hook",
                "pretooluse_secret_scan.sh",
                "--managed-id",
                MANAGED_ID,
            ],
            input="not-json",
            text=True,
            encoding="utf-8",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
        assert adapter_proc.returncode == 2
        return

    process_env = os.environ.copy()
    process_env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    user_prompt = subprocess.run(
        [bash, "-c", hook_command(settings, "userpromptsubmit_handoff_mandate.sh")],
        input=json.dumps({"cwd": str(tmp_path), "hook_event_name": "UserPromptSubmit"}),
        text=True,
        encoding="utf-8",
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert user_prompt.returncode == 0, user_prompt.stdout + user_prompt.stderr

    blocking = subprocess.run(
        [bash, "-c", hook_command(settings, "pretooluse_secret_scan.sh")],
        input="not-json",
        text=True,
        encoding="utf-8",
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert blocking.returncode == 2
    assert "invalid hook input json" in blocking.stderr.lower()


def test_apply_replaces_legacy_powershell_managed_command(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings_path = home / ".qoder" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    old_command = legacy_powershell_managed_command("userpromptsubmit_handoff_mandate.sh")
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {"matcher": "", "hooks": [{"type": "command", "command": old_command}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = hook_command(settings, "userpromptsubmit_handoff_mandate.sh")
    assert command != old_command
    assert "$ErrorActionPreference" not in command
    assert "try {" not in command


def test_partial_profile_detects_sibling_hooks_and_shared_apply_preserves_them(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0

    drifted = run_installer("--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--verify")
    assert drifted.returncode == 1
    assert "extra managed hook" in drifted.stdout
    assert "SessionStart" in drifted.stdout
    assert "PreCompact" in drifted.stdout

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    settings = json.loads((home / ".qoder" / "settings.json").read_text(encoding="utf-8"))
    commands = aqg_commands(settings)
    events = {event for event, _, _ in commands}
    assert events == IDE_EVENTS | {"SessionStart", "PreCompact"}
    assert any("wip_checkpoint_save.sh" in command for _, _, command in commands)
    assert any("wip_checkpoint_recover.sh" in command for _, _, command in commands)

    verified = run_installer("--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--verify")
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_partial_profile_cleanup_preserves_user_lookalike_hook(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    settings_path = home / ".qoder" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    lookalike = (
        "echo qoder_hook_adapter.py aqg-qoder-v1 "
        "wip_checkpoint_save.sh -- this is user-owned"
    )
    settings["hooks"].setdefault("PreCompact", []).append({"matcher": "", "hooks": [{"command": lookalike}]})
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    remaining = [
        hook.get("command")
        for blocks in settings["hooks"].values()
        for block in blocks
        if isinstance(block, dict)
        for hook in block.get("hooks", [])
        if isinstance(hook, dict)
    ]
    assert lookalike in remaining
    assert any(
        command != lookalike and "wip_checkpoint_save.sh" in command
        for command in remaining
        if isinstance(command, str)
    )


def test_apply_explicit_copy_installs_managed_skill_directories(tmp_path: Path) -> None:
    home = tmp_path / "home"
    proc = apply("qoder-cli", home, "--mode", "copy")
    assert proc.returncode == 0, proc.stdout + proc.stderr

    client_root = home / ".qoder"
    source_skills = sorted(path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir())
    installed_skills = sorted(path.name for path in (client_root / "skills").glob("aqg-*"))
    assert installed_skills == source_skills
    for skill_name in installed_skills:
        skill_dir = client_root / "skills" / skill_name
        assert skill_dir.is_dir()
        assert not skill_dir.is_symlink()
        marker = json.loads((skill_dir / MANAGED_MARKER).read_text(encoding="utf-8"))
        assert marker["managed_by"] == MANAGED_ID
        assert marker["mode"] == "copy"


def test_apply_is_idempotent_and_preserves_user_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings_path = home / ".qoder" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "theme": "user-choice",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo user-hook"}],
                        }
                    ],
                    "Notification": [
                        {"hooks": [{"type": "command", "command": "echo notify"}]}
                    ],
                },
            }
        ),
        encoding="utf-8",
    )

    first = apply("qoder", home)
    assert first.returncode == 0, first.stdout + first.stderr
    first_bytes = settings_path.read_bytes()
    first_tree = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    first_mtime = settings_path.stat().st_mtime_ns

    second = apply("qoder", home)
    assert second.returncode == 0, second.stdout + second.stderr
    assert "already installed" in second.stdout.lower()
    assert settings_path.read_bytes() == first_bytes
    assert settings_path.stat().st_mtime_ns == first_mtime
    assert sorted(str(path.relative_to(home)) for path in home.rglob("*")) == first_tree

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert data["theme"] == "user-choice"
    all_commands = [command for _, _, command in aqg_commands(data)]
    assert all_commands
    user_commands = [
        hook["command"]
        for blocks in data["hooks"].values()
        for block in blocks
        if isinstance(block, dict)
        for hook in block.get("hooks", [])
        if isinstance(hook, dict) and "command" in hook
    ]
    assert "echo user-hook" in user_commands
    assert "echo notify" in user_commands
    assert "settings.json" in central_relpaths(tmp_path / CENTRAL_DIRNAME)


def test_uninstall_backs_up_and_removes_only_managed_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    settings_path = home / ".qoder" / "settings.json"
    settings_path.parent.mkdir(parents=True)
    settings_path.write_text(
        json.dumps(
            {
                "theme": "keep",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Bash",
                            "hooks": [{"type": "command", "command": "echo user-hook"}],
                        }
                    ],
                    "Notification": [{"hooks": [{"command": "echo notify"}]}],
                },
            }
        ),
        encoding="utf-8",
    )
    custom_skill = home / ".qoder" / "skills" / "custom-skill"
    custom_skill.mkdir(parents=True)
    (custom_skill / "SKILL.md").write_text("custom", encoding="utf-8")
    custom_link_source = home / "user-owned-link-source"
    custom_link_source.mkdir()
    unknown_symlink = home / ".qoder" / "skills" / "aqg-user-link"
    try:
        create_link(custom_link_source, unknown_symlink)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    assert apply("qoder-cli", home, "--mode", "copy").returncode == 0

    proc = run_installer(
        "--client",
        "qoder-cli",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--uninstall",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr

    client_root = home / ".qoder"
    assert (custom_skill / "SKILL.md").read_text(encoding="utf-8") == "custom"
    assert is_link_install(unknown_symlink)
    assert unknown_symlink.resolve() == custom_link_source.resolve()
    remaining_managed = [
        path for path in (client_root / "skills").glob("aqg-*") if path.name != "aqg-user-link"
    ]
    assert remaining_managed == []
    assert not any((client_root / LINK_MARKER_DIR).glob("aqg-*.json"))
    assert not (client_root / "rules" / "aqg.md").exists()
    settings = json.loads((client_root / "settings.json").read_text(encoding="utf-8"))
    assert aqg_commands(settings) == []
    assert settings["theme"] == "keep"
    remaining = [
        hook.get("command")
        for blocks in settings["hooks"].values()
        for block in blocks
        for hook in block.get("hooks", [])
    ]
    assert "echo user-hook" in remaining
    assert "echo notify" in remaining
    backups = list((tmp_path / CENTRAL_DIRNAME).rglob("*"))
    assert any(path.name == "aqg.md" for path in backups)
    assert any(path.name == "SKILL.md" for path in backups)


def test_qoder_migrates_and_removes_legacy_inplace_backups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / ".qoder"
    # Seed both pre-central legacy forms: the snapshot dir and an adjacent .bak.
    legacy_run = client_root / ".aqg-backups" / "20250101T000000Z-123"
    legacy_run.mkdir(parents=True)
    (legacy_run / "old-skill-snapshot.txt").write_text("SNAP", encoding="utf-8")
    settings_bak = client_root / "settings.json.aqg-qoder.bak"
    settings_bak.write_text("OLDSETTINGS", encoding="utf-8")

    assert apply("qoder", home).returncode == 0

    assert not (client_root / ".aqg-backups").exists(), "legacy snapshot dir not removed"
    assert not settings_bak.exists(), "legacy adjacent settings backup not removed"
    central = tmp_path / CENTRAL_DIRNAME
    snap = list(central.rglob("old-skill-snapshot.txt"))
    assert snap and snap[0].read_text(encoding="utf-8") == "SNAP"
    assert any("migrated-" in part for part in snap[0].parts)
    migrated_settings = list(central.rglob("settings.json.aqg-qoder.bak"))
    assert migrated_settings and migrated_settings[0].read_text(encoding="utf-8") == "OLDSETTINGS"


def test_apply_refuses_unmanaged_skill_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    collision = home / ".qoder-cn" / "skills" / "aqg-startup-preflight"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user-owned", encoding="utf-8")

    proc = apply("qoder-cn", home)
    assert proc.returncode == 1
    assert "refusing to overwrite unmanaged skill" in proc.stderr.lower()
    assert (collision / "SKILL.md").read_text(encoding="utf-8") == "user-owned"
    assert not (home / ".qoder-cn" / "settings.json").exists()


def test_apply_refuses_unknown_skill_symlink_collision(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside-skill"
    outside.mkdir()
    collision = home / ".qoder" / "skills" / "aqg-startup-preflight"
    collision.parent.mkdir(parents=True)
    try:
        create_link(outside, collision)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")
    managed_link_marker(collision).parent.mkdir(parents=True)
    managed_link_marker(collision).write_text(
        json.dumps(
            {
                "managed_by": MANAGED_ID,
                "mode": "link",
                "source": str((REPO / "skills" / "aqg-startup-preflight").resolve()),
            }
        ),
        encoding="utf-8",
    )

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "refusing to overwrite unmanaged skill" in proc.stderr.lower()
    assert is_link_install(collision)
    assert collision.resolve() == outside.resolve()
    assert not (home / ".qoder" / "settings.json").exists()


def test_qoder_verify_accepts_legacy_link_markers_and_apply_migrates_them(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    client_root = home / ".qoder"
    skills_root = client_root / "skills"
    skills_root.mkdir(parents=True)
    source_skills = sorted(path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir())
    for skill_name in source_skills:
        skill_dir = skills_root / skill_name
        source_dir = REPO / "skills" / skill_name
        create_link(source_dir, skill_dir)
        legacy_managed_link_marker(skill_dir).write_text(
            json.dumps(
                {
                    "managed_by": MANAGED_ID,
                    "mode": "link",
                    "source": str(source_dir.resolve()),
                    "source_digest": "legacy",
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    verify = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert verify.returncode == 1
    assert "missing or stale hook" in verify.stdout
    assert "missing managed skill" not in verify.stdout

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert sorted(
        path.name
        for path in (client_root / LINK_MARKER_DIR).glob("*.json")
        if path.name != ".aqg-qoder-owners.json"
    ) == [
        f"{name}.json" for name in source_skills
    ]
    assert not any(path.name.startswith(".aqg-") for path in skills_root.iterdir())


def test_ide_user_scope_preserves_existing_rules(tmp_path: Path) -> None:
    home = tmp_path / "home"
    rule = home / ".qoder" / "rules" / "aqg.md"
    rule.parent.mkdir(parents=True)
    rule.write_text("user-owned rule\n", encoding="utf-8")

    proc = apply("qoder", home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert rule.read_text(encoding="utf-8") == "user-owned rule\n"


def test_apply_refuses_malformed_hooks_without_partial_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / ".qoder"
    client_root.mkdir(parents=True)
    settings = client_root / "settings.json"
    settings.write_text('{"hooks": []}', encoding="utf-8")

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "settings hooks must be a json object" in proc.stderr.lower()
    assert settings.read_text(encoding="utf-8") == '{"hooks": []}'
    assert not (client_root / "skills").exists()


def test_apply_refuses_malformed_managed_event_without_partial_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    client_root = home / ".qoder"
    client_root.mkdir(parents=True)
    settings = client_root / "settings.json"
    settings.write_text('{"hooks": {"PreToolUse": {}}}', encoding="utf-8")

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "hook event must be an array" in proc.stderr.lower()
    assert settings.read_text(encoding="utf-8") == '{"hooks": {"PreToolUse": {}}}'
    assert not (client_root / "skills").exists()


def test_failed_staged_skill_publish_leaves_no_wedged_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installer = load_installer_module()
    source = tmp_path / "source" / "aqg-fixture"
    source.mkdir(parents=True)
    (source / "SKILL.md").write_text("fixture\n", encoding="utf-8")
    client_root = tmp_path / "home" / ".qoder"
    target = client_root / "skills" / source.name

    def fail_marker_write(path: Path, text: str) -> None:
        del path, text
        raise OSError("simulated marker failure")

    monkeypatch.setattr(installer, "_atomic_write", fail_marker_write)
    with pytest.raises(OSError, match="simulated marker failure"):
        installer._install_skill(
            source,
            target,
            installer._tree_digest(source),
        )

    assert not target.exists()
    assert not list(target.parent.glob(".aqg-fixture.aqg-qoder-*"))


def test_uninstall_removes_retired_managed_skill(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    retired = home / ".qoder" / "skills" / "aqg-retired"
    retired.mkdir()
    (retired / "SKILL.md").write_text("retired\n", encoding="utf-8")
    (retired / MANAGED_MARKER).write_text(
        json.dumps({"managed_by": MANAGED_ID, "source_digest": "retired"}),
        encoding="utf-8",
    )

    proc = run_installer("--client", "qoder", "--home", str(home), "--uninstall")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not retired.exists()


def test_uninstall_removes_retired_managed_link_markers(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    client_root = home / ".qoder"
    retired = client_root / "skills" / "aqg-retired"
    create_link(REPO / "skills" / "aqg-startup-preflight", retired)
    managed_link_marker(retired).write_text(
        json.dumps(
            {
                "managed_by": MANAGED_ID,
                "mode": "link",
                "source": str((REPO / "skills" / "aqg-startup-preflight").resolve()),
                "source_digest": "retired",
            }
        ),
        encoding="utf-8",
    )
    legacy_managed_link_marker(retired).write_text(
        json.dumps(
            {
                "managed_by": MANAGED_ID,
                "mode": "link",
                "source": str((REPO / "skills" / "aqg-startup-preflight").resolve()),
                "source_digest": "retired",
            }
        ),
        encoding="utf-8",
    )

    proc = run_installer("--client", "qoder", "--home", str(home), "--uninstall")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not retired.exists()
    assert not managed_link_marker(retired).exists()
    assert not legacy_managed_link_marker(retired).exists()


def test_uninstall_preserves_user_command_with_managed_substrings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    settings_path = home / ".qoder" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    lookalike = (
        "echo qoder_hook_adapter.py aqg-qoder-v1 "
        "pretooluse_secret_scan.sh -- this is user-owned"
    )
    settings["hooks"]["Notification"] = [{"hooks": [{"command": lookalike}]}]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    proc = run_installer("--client", "qoder", "--home", str(home), "--uninstall")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    remaining = json.loads(settings_path.read_text(encoding="utf-8"))
    assert remaining["hooks"]["Notification"][0]["hooks"][0]["command"] == lookalike


def test_apply_refuses_symlinked_client_root(tmp_path: Path) -> None:
    home = tmp_path / "home"
    outside = tmp_path / "outside"
    home.mkdir()
    outside.mkdir()
    try:
        (home / ".qoder").symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "managed path through symlink" in proc.stderr.lower()
    assert list(outside.iterdir()) == []

    verify = run_installer("--client", "qoder", "--home", str(home), "--verify")
    assert verify.returncode == 1
    assert "managed path through symlink" in verify.stdout.lower()
    assert "missing managed skill" not in verify.stdout.lower()


def test_project_scope_uses_documented_project_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()

    global_proc = apply("qoder-cli-cn", home, "--scope", "project", "--project-root", str(project))
    assert global_proc.returncode == 0, global_proc.stdout + global_proc.stderr
    assert (project / ".qoder" / "settings.json").is_file()
    assert is_link_install(project / ".qoder" / "skills" / "aqg-startup-preflight")
    assert (project / ".qoder" / LINK_MARKER_DIR / "aqg-startup-preflight.json").is_file()
    assert not any(path.name.startswith(".aqg-") for path in (project / ".qoder" / "skills").iterdir())
    assert (project / ".qoder" / "rules" / "aqg.md").is_file()
    assert not (home / ".qoder-cn").exists()

    cn_project = tmp_path / "cn-project"
    cn_project.mkdir()
    cn_proc = apply("qoder-cn", home, "--scope", "project", "--project-root", str(cn_project))
    assert cn_proc.returncode == 0, cn_proc.stdout + cn_proc.stderr
    assert (cn_project / ".qoder-cn" / "settings.json").is_file()
    assert (cn_project / ".qoder-cn" / LINK_MARKER_DIR / "aqg-startup-preflight.json").is_file()
    assert not any(path.name.startswith(".aqg-") for path in (cn_project / ".qoder-cn" / "skills").iterdir())
    assert (cn_project / ".qoder-cn" / "rules" / "aqg.md").is_file()
    assert not (cn_project / ".lingma").exists()


@pytest.mark.parametrize("profile", ["qoder", "qoder-cn"])
def test_desktop_user_scope_installs_skills_and_hooks_without_project_rules(
    tmp_path: Path, profile: str
) -> None:
    # R24-09: a Desktop user-scope install is a complete deliverable on its own —
    # 16 skills plus the partial hook set. Only the project rules file is scoped
    # out, and its absence must not be confused with "nothing was installed".
    home = tmp_path / "home"

    proc = apply(profile, home)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = home / PROFILE_ROOTS[profile]
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir()
    )
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {event for event, _, _ in aqg_commands(settings)} == IDE_EVENTS
    assert not (root / "rules" / "aqg.md").exists()

    verify = run_installer(
        "--client", profile, "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_qoder_cn_desktop_writes_only_its_own_root_and_no_cli_hooks(tmp_path: Path) -> None:
    # R24-03: CN Desktop installs under `~/.qoder-cn` with the partial (IDE) hook
    # set only. Writing `.lingma`, or the CLI-only SessionStart/PreCompact/WIP
    # events, would mean AQG claims a runtime contract this product never proved.
    home = tmp_path / "home"

    proc = apply("qoder-cn", home)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not (home / ".lingma").exists()
    root = home / ".qoder-cn"
    installed = sorted(path.name for path in (root / "skills").glob("aqg-*"))
    assert installed == sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir()
    )
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    commands = aqg_commands(settings)
    assert {event for event, _, _ in commands} == IDE_EVENTS
    assert not any("wip_checkpoint" in command for _, _, command in commands)


def test_qoder_cn_desktop_and_international_desktop_do_not_cross_write(tmp_path: Path) -> None:
    # R24-03: the two Qoder Desktop editions are isolated surfaces.
    home = tmp_path / "home"

    assert apply("qoder", home).returncode == 0
    assert apply("qoder-cn", home).returncode == 0

    skill_names = sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir()
    )
    for root_name in (".qoder", ".qoder-cn"):
        root = home / root_name
        assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == skill_names
    assert not (home / ".lingma").exists()

    # Uninstalling the CN edition must leave the international edition intact.
    removed = run_installer(
        "--client", "qoder-cn", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not list((home / ".qoder-cn" / "skills").glob("aqg-*"))
    assert sorted(
        path.name for path in (home / ".qoder" / "skills").glob("aqg-*")
    ) == skill_names
    survivor = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert survivor.returncode == 0, survivor.stdout + survivor.stderr


@pytest.mark.parametrize(
    ("desktop", "cli"),
    [("qoder", "qoder-cli"), ("qoder-cn", "qoder-cli-cn")],
)
def test_shared_qoder_root_desktop_then_cli_uninstall_preserves_cli_owner(
    tmp_path: Path, desktop: str, cli: str,
) -> None:
    """Removing Desktop must not remove AQG assets still used by the CLI."""
    home = tmp_path / "home"

    assert apply(desktop, home).returncode == 0
    assert apply(cli, home).returncode == 0

    removed = run_installer(
        "--client", desktop, "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr

    root = home / PROFILE_ROOTS[desktop]
    installed_skills = sorted(path.name for path in (root / "skills").glob("aqg-*"))
    expected_skills = sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir()
    )
    assert len(expected_skills) == 16
    assert installed_skills == expected_skills
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {event for event, _, _ in aqg_commands(settings)} == IDE_EVENTS | {
        "SessionStart",
        "PreCompact",
    }
    assert (root / "rules" / "aqg.md").is_file()
    survivor = run_installer(
        "--client", cli, "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert survivor.returncode == 0, survivor.stdout + survivor.stderr


@pytest.mark.parametrize(
    ("desktop", "cli"),
    [("qoder", "qoder-cli"), ("qoder-cn", "qoder-cli-cn")],
)
def test_shared_qoder_root_cli_then_desktop_uninstall_preserves_desktop_owner(
    tmp_path: Path, desktop: str, cli: str,
) -> None:
    """Removing CLI must not remove AQG assets still used by Desktop."""
    home = tmp_path / "home"

    assert apply(cli, home).returncode == 0
    assert apply(desktop, home).returncode == 0

    removed = run_installer(
        "--client", cli, "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr

    root = home / PROFILE_ROOTS[desktop]
    installed_skills = sorted(path.name for path in (root / "skills").glob("aqg-*"))
    expected_skills = sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if path.is_dir()
    )
    assert len(expected_skills) == 16
    assert installed_skills == expected_skills
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {event for event, _, _ in aqg_commands(settings)} == IDE_EVENTS
    assert not (root / "rules" / "aqg.md").exists()
    survivor = run_installer(
        "--client", desktop, "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert survivor.returncode == 0, survivor.stdout + survivor.stderr


def test_shared_qoder_root_cleans_skills_only_after_last_owner_uninstalls(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    assert apply("qoder-cli", home).returncode == 0

    first = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert list((home / ".qoder" / "skills").glob("aqg-*"))
    assert (home / ".qoder" / "managed-links" / ".aqg-qoder-owners.json").is_file()

    last = run_installer(
        "--client", "qoder-cli", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert last.returncode == 0, last.stdout + last.stderr
    assert not list((home / ".qoder" / "skills").glob("aqg-*"))
    assert not (home / ".qoder" / "managed-links").exists()


def test_legacy_shared_qoder_root_uninstall_fails_closed_until_migrated(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    root = home / ".qoder"
    ledger = root / "managed-links" / ".aqg-qoder-owners.json"
    ledger.unlink()
    settings_before = (root / "settings.json").read_bytes()
    skills_before = sorted(path.name for path in (root / "skills").glob("aqg-*"))

    removed = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 1
    assert "legacy ownership" in removed.stderr.lower()
    assert not ledger.exists()
    assert (root / "settings.json").read_bytes() == settings_before
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == skills_before

    migrated = apply("qoder", home)
    assert migrated.returncode == 0, migrated.stdout + migrated.stderr
    removed = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not list((root / "skills").glob("aqg-*"))


@pytest.mark.parametrize(
    ("legacy_owner", "new_owner"),
    [("qoder", "qoder-cli"), ("qoder-cli-cn", "qoder")],
)
def test_legacy_project_shared_root_is_adopted_before_sibling_uninstall(
    tmp_path: Path, legacy_owner: str, new_owner: str,
) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    project_args = ("--scope", "project", "--project-root", str(project))

    assert apply(legacy_owner, home, *project_args).returncode == 0
    ledger = project / ".qoder" / "managed-links" / ".aqg-qoder-owners.json"
    ledger.unlink()

    assert apply(legacy_owner, home, *project_args).returncode == 0
    assert apply(new_owner, home, *project_args).returncode == 0
    removed = run_installer(
        "--client",
        new_owner,
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        *project_args,
        "--uninstall",
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr

    survivor = run_installer(
        "--client",
        legacy_owner,
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        *project_args,
        "--verify",
    )
    assert survivor.returncode == 0, survivor.stdout + survivor.stderr


def test_project_desktop_survivor_keeps_project_rule(tmp_path: Path) -> None:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    project_args = ("--scope", "project", "--project-root", str(project))

    assert apply("qoder", home, *project_args).returncode == 0
    assert apply("qoder-cli", home, *project_args).returncode == 0
    removed = run_installer(
        "--client",
        "qoder-cli",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        *project_args,
        "--uninstall",
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert (project / ".qoder" / "rules" / "aqg.md").is_file()
    survivor = run_installer(
        "--client",
        "qoder",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        *project_args,
        "--verify",
    )
    assert survivor.returncode == 0, survivor.stdout + survivor.stderr


@pytest.mark.parametrize("remaining", ["hooks", "rule"])
def test_partial_legacy_root_uninstall_fails_closed_without_mutation(
    tmp_path: Path, remaining: str,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    root = home / ".qoder"
    (root / "managed-links" / ".aqg-qoder-owners.json").unlink()
    shutil.rmtree(root / "skills")
    shutil.rmtree(root / "managed-links")
    if remaining == "hooks":
        (root / "rules" / "aqg.md").unlink()
        preserved = root / "settings.json"
    else:
        (root / "settings.json").unlink()
        preserved = root / "rules" / "aqg.md"
    before = preserved.read_bytes()

    removed = run_installer(
        "--client", "qoder-cli", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 1
    assert "legacy ownership" in removed.stderr.lower()
    assert preserved.read_bytes() == before


def test_legacy_desktop_apply_does_not_claim_or_install_cli_surface(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    root = home / ".qoder"
    ledger = root / "managed-links" / ".aqg-qoder-owners.json"
    ledger.unlink()

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    owners = json.loads(ledger.read_text(encoding="utf-8"))["owners"]
    assert owners == ["qoder"]
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {event for event, _, _ in aqg_commands(settings)} == IDE_EVENTS
    assert not (root / "rules" / "aqg.md").exists()


def test_legacy_cli_hooks_are_claimed_when_desktop_repairs_shared_root(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    root = home / ".qoder"
    ledger = root / "managed-links" / ".aqg-qoder-owners.json"
    ledger.unlink()

    repaired = apply("qoder", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    assert json.loads(ledger.read_text(encoding="utf-8"))["owners"] == [
        "qoder",
        "qoder-cli",
    ]
    settings = json.loads((root / "settings.json").read_text(encoding="utf-8"))
    assert {event for event, _, _ in aqg_commands(settings)} == IDE_EVENTS | {
        "SessionStart",
        "PreCompact",
    }
    assert (root / "rules" / "aqg.md").is_file()


def test_uninstalling_non_owner_does_not_change_sibling_install(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    root = home / ".qoder"
    ledger = root / "managed-links" / ".aqg-qoder-owners.json"
    before = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    removed = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert "no aqg-managed" in removed.stdout.lower()
    assert json.loads(ledger.read_text(encoding="utf-8"))["owners"] == ["qoder-cli"]
    after = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }
    assert after == before


def test_shared_owner_preflight_validates_sibling_only_hook_events(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    assert apply("qoder", home).returncode == 0
    settings_path = home / ".qoder" / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    settings["hooks"]["SessionStart"] = {}
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    before = settings_path.read_bytes()

    repaired = apply("qoder", home)
    assert repaired.returncode == 1
    assert "hook event must be an array: sessionstart" in repaired.stderr.lower()
    assert settings_path.read_bytes() == before


@pytest.mark.parametrize("action", ["--apply", "--uninstall", "--verify"])
@pytest.mark.parametrize("symlink_part", ["ledger", "parent"])
def test_ownership_ledger_symlinks_fail_closed_without_external_mutation(
    tmp_path: Path, action: str, symlink_part: str,
) -> None:
    home = tmp_path / "home"
    root = home / ".qoder"
    marker_dir = root / "managed-links"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / ".aqg-qoder-owners.json"
    sentinel.write_text("outside\n", encoding="utf-8")
    marker_dir.parent.mkdir(parents=True)
    try:
        if symlink_part == "parent":
            marker_dir.symlink_to(outside, target_is_directory=True)
        else:
            marker_dir.mkdir()
            (marker_dir / ".aqg-qoder-owners.json").symlink_to(sentinel)
    except OSError as exc:
        pytest.skip(f"symlink fixtures unavailable: {exc}")

    proc = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), action
    )
    assert proc.returncode == 1
    assert "symlink" in (proc.stdout + proc.stderr).lower() or "ownership ledger" in (
        proc.stdout + proc.stderr
    ).lower()
    assert sentinel.read_text(encoding="utf-8") == "outside\n"
    assert not (root / "skills").exists()


def test_final_owner_uninstall_cleans_markers_when_skills_directory_is_missing(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder", home).returncode == 0
    root = home / ".qoder"
    shutil.rmtree(root / "skills")

    removed = run_installer(
        "--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--uninstall"
    )
    assert removed.returncode == 0, removed.stdout + removed.stderr
    assert not (root / "managed-links").exists()


def test_apply_owner_ledger_failure_is_retryable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    installer = load_installer_module()
    home = tmp_path / "home"
    ledger = home / ".qoder" / "managed-links" / ".aqg-qoder-owners.json"
    original_atomic_write = installer._atomic_write

    def fail_ledger(path: Path, text: str) -> None:
        if path == ledger:
            raise OSError("simulated owner ledger failure")
        original_atomic_write(path, text)

    monkeypatch.setattr(installer, "_atomic_write", fail_ledger)
    args = ["--client", "qoder", "--home", str(home), "--aqg-root", str(REPO), "--apply"]
    assert installer.main(args) == 1
    assert not ledger.exists()
    assert (home / ".qoder" / "settings.json").is_file()
    assert len(list((home / ".qoder" / "skills").glob("aqg-*"))) == 16

    monkeypatch.setattr(installer, "_atomic_write", original_atomic_write)
    assert installer.main(args) == 0
    assert ledger.is_file()


def test_uninstall_failure_keeps_owner_ledger_and_retry_converges(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    root = home / ".qoder"
    ledger = root / "managed-links" / ".aqg-qoder-owners.json"
    installer = load_installer_module()
    original_atomic_write = installer._atomic_write

    def fail_settings(path: Path, text: str) -> None:
        if path == root / "settings.json":
            raise OSError("simulated settings failure")
        original_atomic_write(path, text)

    monkeypatch.setattr(installer, "_atomic_write", fail_settings)
    args = [
        "--client",
        "qoder-cli",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--uninstall",
    ]
    assert installer.main(args) == 1
    assert json.loads(ledger.read_text(encoding="utf-8"))["owners"] == ["qoder-cli"]

    monkeypatch.setattr(installer, "_atomic_write", original_atomic_write)
    assert installer.main(args) == 0
    assert not ledger.exists()
    assert not list((root / "skills").glob("aqg-*"))


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"managed_by": MANAGED_ID, "schema_version": 2, "owners": ["qoder"]},
        {"managed_by": MANAGED_ID, "schema_version": 1, "owners": "qoder"},
        {"managed_by": MANAGED_ID, "schema_version": 1, "owners": [7]},
        {"managed_by": MANAGED_ID, "schema_version": 1, "owners": ["unknown"]},
        {
            "managed_by": MANAGED_ID,
            "schema_version": 1,
            "owners": ["qoder-cn"],
        },
    ],
)
def test_invalid_qoder_ownership_ledger_fails_closed(
    tmp_path: Path, payload: dict,
) -> None:
    home = tmp_path / "home"
    ledger = home / ".qoder" / "managed-links" / ".aqg-qoder-owners.json"
    ledger.parent.mkdir(parents=True)
    ledger.write_text(json.dumps(payload), encoding="utf-8")

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "ownership ledger" in proc.stderr.lower()
    assert not (home / ".qoder" / "settings.json").exists()
    assert not (home / ".qoder" / "skills").exists()


def test_ownership_ledger_directory_fails_closed(tmp_path: Path) -> None:
    home = tmp_path / "home"
    ledger = home / ".qoder" / "managed-links" / ".aqg-qoder-owners.json"
    ledger.mkdir(parents=True)

    proc = apply("qoder", home)
    assert proc.returncode == 1
    assert "ownership ledger" in proc.stderr.lower()
    assert ledger.is_dir()
    assert not (home / ".qoder" / "skills").exists()


@pytest.mark.parametrize("profile", ["qoder", "qoder-cli", "qoder-cli-cn"])
def test_verify_and_is_installed_require_complete_profile(
    tmp_path: Path, profile: str
) -> None:
    home = tmp_path / "home"
    missing = run_installer("--client", profile, "--home", str(home), "--verify")
    assert missing.returncode == 1
    assert "not installed" in missing.stdout.lower()
    assert apply(profile, home).returncode == 0

    verify = run_installer("--client", profile, "--home", str(home), "--verify")
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert f"support_level: {PROFILE_LEVELS[profile]}" in verify.stdout
    detected = run_installer(
        "--client", profile, "--home", str(home), "--is-installed"
    )
    assert detected.returncode == 0


@pytest.mark.parametrize("profile", ["qoder", "qoder-cn"])
def test_partial_profiles_report_partial_not_full(tmp_path: Path, profile: str) -> None:
    home = tmp_path / "home"
    proc = apply(profile, home)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "support_level: partial" in proc.stdout
    assert "support_level: full" not in proc.stdout


def test_verify_detects_drift_and_apply_repairs_managed_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli-cn", home, "--mode", "copy").returncode == 0
    client_root = home / ".qoder-cn"

    settings_path = client_root / "settings.json"
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    command = next(
        hook["command"]
        for blocks in settings["hooks"].values()
        for block in blocks
        for hook in block.get("hooks", [])
        if "qoder_hook_adapter.py" in hook.get("command", "")
    )
    for blocks in settings["hooks"].values():
        for block in blocks:
            for hook in block.get("hooks", []):
                if hook.get("command") == command:
                    hook["command"] = command.replace(str(REPO), str(tmp_path / "stale-root"))
                    break
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    rule_path = client_root / "rules" / "aqg.md"
    rule_path.write_text(rule_path.read_text(encoding="utf-8") + "\nuser drift\n", encoding="utf-8")
    skill_path = client_root / "skills" / "aqg-startup-preflight" / "SKILL.md"
    skill_path.write_text(
        skill_path.read_text(encoding="utf-8") + "\nuser drift\n", encoding="utf-8"
    )

    drifted = run_installer(
        "--client", "qoder-cli-cn", "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert drifted.returncode == 1
    assert "stale hook" in drifted.stdout
    assert "missing managed rule" in drifted.stdout
    assert "missing managed skill: aqg-startup-preflight" in drifted.stdout

    repaired = apply("qoder-cli-cn", home)
    assert repaired.returncode == 0, repaired.stdout + repaired.stderr
    verified = run_installer(
        "--client", "qoder-cli-cn", "--home", str(home), "--aqg-root", str(REPO), "--verify"
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    central = tmp_path / CENTRAL_DIRNAME
    assert "settings.json" in central_relpaths(central)
    backups = list(central.rglob("*"))
    assert any(path.name == "aqg.md" for path in backups)
    assert any(path.name == "SKILL.md" for path in backups)


def test_uninstall_invalid_rule_encoding_does_not_partially_rewrite_settings(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    assert apply("qoder-cli", home).returncode == 0
    client_root = home / ".qoder"
    settings_path = client_root / "settings.json"
    before = settings_path.read_bytes()
    (client_root / "rules" / "aqg.md").write_bytes(b"\xff\xfe")

    proc = run_installer("--client", "qoder-cli", "--home", str(home), "--uninstall")
    assert proc.returncode == 1
    assert "cannot read managed rule" in proc.stderr.lower()
    assert settings_path.read_bytes() == before


@pytest.mark.parametrize(
    ("hook", "expected"),
    [
        ("pretooluse_secret_scan.sh", 2),
        ("posttooluse_test_quality_reminder.sh", 0),
    ],
)
def test_adapter_fails_closed_only_for_blocking_hooks(
    tmp_path: Path, hook: str, expected: int
) -> None:
    process_env = os.environ.copy()
    process_env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    proc = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--aqg-root",
            str(REPO),
            "--hook",
            hook,
            "--managed-id",
            "aqg-qoder-v1",
        ],
        input="not-json",
        text=True,
        encoding="utf-8",
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert proc.returncode == expected
    assert "invalid hook input json" in proc.stderr.lower()


@pytest.mark.skipif(shutil.which("bash") is None, reason="AQG hook runtime requires bash")
def test_adapter_maps_qoder_failure_event_to_existing_debug_hook(tmp_path: Path) -> None:
    payload = {
        "session_id": "fixture-session",
        "cwd": str(tmp_path),
        "hook_event_name": "PostToolUseFailure",
        "tool_name": "Bash",
        "tool_input": {"command": "false"},
        "error": "命令失败: exit status 1",
    }
    process_env = os.environ.copy()
    process_env.update({"HOME": str(tmp_path), "USERPROFILE": str(tmp_path)})
    proc = subprocess.run(
        [
            sys.executable,
            str(ADAPTER),
            "--aqg-root",
            str(REPO),
            "--hook",
            "posttooluse_bash_error_debugging_reminder.sh",
            "--managed-id",
            "aqg-qoder-v1",
        ],
        input=json.dumps(payload),
        text=True,
        encoding="utf-8",
        env=process_env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "aqg-systematic-debugging" in proc.stderr
    assert "exit status 1" not in proc.stdout + proc.stderr
