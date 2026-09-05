"""Behavior contracts for Pi client support."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[2]
INSTALLER = REPO / "scripts" / "install_aqg_pi.py"
REPORT_MARKER = "<!-- AQG PI SUPPORT REPORT: managed by AQG -->"
EXT_MARKER = "// AQG PI MANAGED EXTENSION: do not edit generated content"
SKILL_MARKER = ".aqg-pi-managed.json"


def _expected_skills() -> list[str]:
    return sorted(
        path.name for path in (REPO / "skills").glob("aqg-*") if (path / "SKILL.md").is_file()
    )


def _run_installer(*args: str) -> subprocess.CompletedProcess[str]:
    # Route the central backup store into the per-test tmp tree so backups never
    # leak to the real store or to dirname(--aqg-root)/aqg-backups.
    env = os.environ.copy()
    for flag in ("--home", "--project-root"):
        if flag in args:
            env["AQG_BACKUP_DIR"] = str(Path(args[args.index(flag) + 1]) / ".aqg-central")
            break
    return subprocess.run(
        [sys.executable, str(INSTALLER), *args],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )


def test_pi_user_scope_installs_skills_extension_and_report_without_real_home(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--mode",
        "copy",
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "support_level: partial" in proc.stdout
    root = home / ".pi" / "agent"
    assert root.exists()
    assert sorted(path.name for path in (root / "skills").glob("aqg-*")) == _expected_skills()
    assert all((root / "skills" / name / SKILL_MARKER).is_file() for name in _expected_skills())
    extension = root / "extensions" / "aqg-extension.ts"
    text = extension.read_text(encoding="utf-8")
    assert EXT_MARKER in text
    assert 'pi.on("tool_call"' in text
    assert 'return { block: true' in text
    assert "Record<string, string[]>" in text
    assert "Bun.spawn(command" in text
    assert "shell: true" not in text
    assert 'pi.on("input"' in text
    assert 'pi.on("session_start"' in text
    assert 'pi.on("session_before_compact"' in text
    assert 'pi.on("session_shutdown"' in text
    assert "pretooluse_secret_scan.sh" in text
    assert "userpromptsubmit_handoff_mandate.sh" in text
    assert REPORT_MARKER in (root / "aqg-support-report.md").read_text(encoding="utf-8")
    assert not (Path.home() / ".pi" / "agent" / "aqg-support-report.md").exists()

    verify = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--mode",
        "copy",
        "--verify",
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr
    assert _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--mode",
        "copy",
        "--is-installed",
    ).returncode == 0


def test_pi_project_scope_uses_project_pi_directory(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()

    proc = _run_installer(
        "--scope",
        "project",
        "--project-root",
        str(project),
        "--aqg-root",
        str(REPO),
        "--mode",
        "copy",
        "--apply",
        "--no-hooks",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = project / ".pi"
    assert (root / "skills" / "aqg-code-construction" / "SKILL.md").is_file()
    assert not (root / "extensions" / "aqg-extension.ts").exists()
    assert "hooks skipped" in proc.stdout


def test_pi_link_mode_installs_marker_sidecars_and_verifies(tmp_path: Path) -> None:
    home = tmp_path / "home"

    proc = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    root = home / ".pi" / "agent"
    marker_dir = root / "managed-links"
    assert sorted(path.name for path in marker_dir.glob("aqg-*.json")) == [
        f"{name}.json" for name in _expected_skills()
    ]
    first_marker = json.loads((marker_dir / "aqg-code-construction.json").read_text(encoding="utf-8"))
    assert first_marker["managed_by"] == "aqg-pi-v1"
    assert first_marker["mode"] == "link"
    verify = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--verify",
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr


def test_pi_uninstall_removes_only_managed_assets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".pi" / "agent"
    root.mkdir(parents=True)
    user_file = root / "user-note.txt"
    user_file.write_text("keep\n", encoding="utf-8")

    common = ("--scope", "user", "--home", str(home), "--aqg-root", str(REPO), "--mode", "copy")
    assert _run_installer("--apply", *common).returncode == 0
    proc = _run_installer("--uninstall", *common)

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert user_file.read_text(encoding="utf-8") == "keep\n"
    assert not any((root / "skills").glob("aqg-*"))
    assert not (root / "extensions" / "aqg-extension.ts").exists()
    assert not (root / "aqg-support-report.md").exists()
    assert _run_installer("--is-installed", *common).returncode == 1


def test_pi_uninstall_backups_land_in_central_store(tmp_path: Path) -> None:
    home = tmp_path / "home"
    common = ("--scope", "user", "--home", str(home), "--aqg-root", str(REPO), "--mode", "copy")
    assert _run_installer("--apply", *common).returncode == 0
    assert _run_installer("--uninstall", *common).returncode == 0

    central = home / ".aqg-central"
    # No legacy in-place backups anywhere under the client root.
    assert not (home / ".pi" / "agent" / ".aqg-backups").exists()
    runs = sorted((central / "pi" / "user").glob("*"))
    assert runs, "expected a central backup run dir under <base>/pi/user/"
    manifests = list(central.rglob("manifest.json"))
    assert manifests, "central store should record a manifest"
    entries = json.loads(manifests[0].read_text(encoding="utf-8"))
    assert entries["client"] == "pi"
    assert entries["scope"] == "user"
    assert Path(entries["source_root"]) == (home / ".pi" / "agent")
    rels = {item["relpath"] for item in entries["entries"]}
    assert "aqg-support-report.md" in rels
    assert "extensions/aqg-extension.ts" in rels
    assert any(rel.startswith("skills/aqg-") for rel in rels)


def test_pi_migrates_and_removes_legacy_inplace_backups(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".pi" / "agent"
    legacy = root / ".aqg-backups"
    legacy.mkdir(parents=True)
    (legacy / "aqg-support-report.md.aqg-pi.bak").write_text("stale\n", encoding="utf-8")

    common = ("--scope", "user", "--home", str(home), "--aqg-root", str(REPO), "--mode", "copy")
    assert _run_installer("--apply", *common).returncode == 0

    # Legacy dir is migrated into the central store, then deleted in place.
    assert not legacy.exists()
    central = home / ".aqg-central"
    migrated = list(central.rglob("aqg-support-report.md.aqg-pi.bak"))
    assert migrated, "legacy in-place backup should be preserved in the central store"


def test_pi_apply_refuses_unmanaged_skill_collision_before_other_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".pi" / "agent"
    collision = root / "skills" / "aqg-code-construction"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("user skill\n", encoding="utf-8")

    proc = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 1
    assert "refusing to overwrite unmanaged skill" in proc.stderr
    assert not (root / "extensions" / "aqg-extension.ts").exists()
    assert not (root / "aqg-support-report.md").exists()


def test_pi_uninstall_preserves_user_extension(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".pi" / "agent"
    extension = root / "extensions" / "aqg-extension.ts"
    extension.parent.mkdir(parents=True)
    extension.write_text("user extension\n", encoding="utf-8")

    proc = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--uninstall",
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert extension.read_text(encoding="utf-8") == "user extension\n"


def test_pi_apply_refuses_unmanaged_support_report_before_other_writes(tmp_path: Path) -> None:
    home = tmp_path / "home"
    root = home / ".pi" / "agent"
    root.mkdir(parents=True)
    (root / "aqg-support-report.md").write_text("user report\n", encoding="utf-8")

    proc = _run_installer(
        "--scope",
        "user",
        "--home",
        str(home),
        "--aqg-root",
        str(REPO),
        "--apply",
    )

    assert proc.returncode == 1
    assert "refusing to overwrite unmanaged support report" in proc.stderr
    assert not (root / "extensions" / "aqg-extension.ts").exists()


def test_pi_hook_runner_rejects_bad_identity_and_bad_payload(tmp_path: Path) -> None:
    runner = REPO / "scripts" / "pi_aqg_hook.py"

    bad_identity = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--aqg-root",
            str(REPO),
            "--hook",
            "pretooluse_secret_scan.sh",
            "--managed-id",
            "not-aqg",
        ],
        input="{}",
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad_identity.returncode == 2
    assert "unrecognized AQG Pi managed identity" in bad_identity.stderr

    bad_payload = subprocess.run(
        [
            sys.executable,
            str(runner),
            "--aqg-root",
            str(REPO),
            "--hook",
            "pretooluse_secret_scan.sh",
            "--managed-id",
            "aqg-pi-v1",
        ],
        input="[]",
        text=True,
        capture_output=True,
        check=False,
    )
    assert bad_payload.returncode == 2
    assert "invalid Pi hook payload" in bad_payload.stderr
