"""Real installed clients must survive reinstall and consume a signed release.

No substitute planner, acquisition, installer, or transaction. The remote and
signing key are local test fixtures; every host path is under tmp_path.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from scripts.aqg_update import acquire, run, stage
from tests.behavior.test_update_acquire import _KEY, _KEY_ID, _git, _git_init, _publish, _sign

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def installation(tmp_path_factory, monkeypatch):
    # Keep the profile comparable to a real user home. Git for Windows has a
    # separate GIT_DIR limit for worktree fetches, even with core.longpaths.
    tmp_path = tmp_path_factory.mktemp("aqg")
    home = tmp_path / "profile"
    home.mkdir()
    root = home / ".deeppattern" / "agent-quality-gates"
    for name, value in {
        "HOME": home, "USERPROFILE": home, "CODEX_HOME": home / ".codex",
        "AQG_ROOT": root, "AQG_STATE_ROOT": home / ".deeppattern" / "aqg-state",
        "AQG_BACKUP_DIR": home / "backups", "AQG_NO_UPDATE_CHECK": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    }.items():
        monkeypatch.setenv(name, str(value))
    for name in ("AQG_NO_MIGRATE", "AQG_MIGRATE", "GIT_DIR", "GIT_WORK_TREE"):
        monkeypatch.delenv(name, raising=False)
    origin = tmp_path / "origin"
    origin.mkdir()
    # Snapshot actual working sources, including the candidate fixes. Git owns
    # only synthetic fixture commits; no commit is made in the developer repo.
    names = _git(REPO, "ls-files", "-z").stdout.decode().split("\0")
    for required in (
        "scripts/aqg_directory_links.py",
        "scripts/aqg_update/updater-capabilities-v1.json",
    ):
        if required not in names:
            names.append(required)
    for name in filter(None, names):
        source = REPO / name
        if source.is_file():
            dest = origin / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, dest)
    test_trust = json.dumps({"schema": 1, "keys": [{
        "key_id": _KEY_ID, "algorithm": "rsa-pkcs1v15-sha256",
        "modulus_hex": _KEY["modulus_hex"], "exponent": _KEY["exponent"],
        "revoked": False,
    }]})
    # Only the isolated test distribution trusts the public fixture key. This
    # also lets its real detached SessionStart child use the default keyring.
    (origin / "scripts/aqg_update/release-trust.json").write_text(test_trust, encoding="utf-8")
    _git_init(origin)
    _git(origin, "config", "core.autocrlf", "false")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "installed fixture")
    root.parent.mkdir(parents=True)
    _git(tmp_path, "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(root))
    _git(root, "config", "core.autocrlf", "false")
    keyring = tmp_path / "test-trust.json"
    keyring.write_text(test_trust, encoding="utf-8")
    return home, root, origin, keyring


def _install(home, root, action):
    result = subprocess.run([
        sys.executable, "-B", str(root / "scripts/install_aqg_clients.py"),
        "--clients", "codex,claude-code", "--aqg-root", str(root),
        "--home", str(home), action,
    ], cwd=home, capture_output=True, text=True, encoding="utf-8", errors="replace",
       env={**os.environ, "AQG_NO_UPDATE_CHECK": "1"})
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def _release(origin, *, version, sequence):
    (origin / "VERSION").write_text(version + "\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "next fixture")
    commit = _git(origin, "rev-parse", "HEAD").stdout.decode().strip()
    manifest = {
        "schema": 1, "channel": "stable", "version": version,
        "commit": commit, "release_sequence": sequence, "key_id": _KEY_ID,
        "files": acquire.commit_roster(origin, commit),
    }
    _publish(origin, "stable", manifest, _sign(manifest))
    return commit


def test_reinstall_then_signed_content_release_really_updates(installation, monkeypatch):
    home, root, origin, keyring = installation
    installed = _install(home, root, "--apply")
    assert root.is_symlink(), installed.stdout + installed.stderr
    _install(home, root, "--uninstall")
    assert "aqg-codex-v1" not in (home / ".codex/hooks.json").read_text()
    assert "sessionstart_update_check.sh" not in (home / ".claude/settings.json").read_text()
    # Quarantine the uninstalled fixture, then clone again as a user reinstall
    # does. Retain the external state/config, as the real uninstall does.
    old = root.resolve()
    assert old.is_relative_to(home / ".deeppattern/versions")
    root.unlink()
    old.rename(home / ".deeppattern/uninstalled-fixture")
    _git(home, "clone", "-q", "-c", "core.autocrlf=false", str(origin), str(root))
    _install(home, root, "--apply")
    assert root.is_symlink()
    _install(home, root, "--verify")

    configs = {p: p.read_bytes() for p in (
        home / ".codex/hooks.json", home / ".claude/settings.json",
    )}
    marker = "\nReinstall update fixture content.\n"
    for subdir in ("skills", "agent-packs/claude-code/skills"):
        skill = origin / subdir / "aqg-code-construction/SKILL.md"
        with skill.open("a", encoding="utf-8") as stream:
            stream.write(marker)
    commit = _release(origin, version="0.14.4", sequence=8)
    # The context helper pins AQG_ROOT to a physical generation before launching
    # an update. Ownership and hook inspection must still use the logical root.
    monkeypatch.setenv("AQG_ROOT", str(root.resolve()))
    monkeypatch.delenv("AQG_NO_UPDATE_CHECK")
    result = run.check(root=root.resolve(), remote="origin", channel="stable", keyring_path=keyring, now=10000)
    assert result.outcome == "applied", (result, result.pending)
    assert root.resolve().name == "0.14.4"
    assert stage.version_commit(root.resolve()) == commit
    assert (root / "VERSION").read_text().strip() == "0.14.4"
    for host in (".codex", ".claude"):
        assert marker in (home / host / "skills/aqg-code-construction/SKILL.md").read_text(encoding="utf-8")
    assert all(p.read_bytes() == before for p, before in configs.items())
    state = json.loads((home / ".deeppattern/aqg-state/install-state.json").read_text())
    assert state["installed_commit"] == commit
    assert state["release_sequence"] == 8
    assert run.check(root=root, remote="origin", channel="stable", keyring_path=keyring, now=20000).outcome == "current"
    _install(home, root, "--verify")

    # A further release must arrive via the shipped background trigger too,
    # not merely when a test directly calls the update library.
    next_commit = _release(origin, version="0.14.5", sequence=9)
    monkeypatch.setenv("AQG_ROOT", str(root))
    fired = subprocess.run([
        "bash", str(root / "agent-packs/claude-code/hooks/sessionstart_update_check.sh"),
    ], cwd=home, capture_output=True, text=True)
    assert fired.returncode == 0 and not fired.stdout
    deadline = time.monotonic() + 30
    state_path = home / ".deeppattern/aqg-state/install-state.json"
    while time.monotonic() < deadline:
        applied = json.loads(state_path.read_text())
        if applied["installed_commit"] == next_commit:
            break
        time.sleep(0.05)  # wait on the detached child's durable completion signal
    else:
        pytest.fail((home / ".deeppattern/aqg-state/update-last-check.json").read_text())
    assert root.resolve().name == "0.14.5"
    assert stage.version_commit(root.resolve()) == next_commit
    assert applied["release_sequence"] == 9
    _install(home, root, "--verify")


def test_changed_codex_hook_digest_stays_pending(installation, monkeypatch):
    home, root, origin, keyring = installation
    _install(home, root, "--apply")
    before = root.resolve()
    hook = origin / "agent-packs/claude-code/hooks/sessionstart_update_check.sh"
    with hook.open("a", encoding="utf-8") as stream:
        stream.write("\n# changed signed hook policy\n")
    _release(origin, version="0.14.4", sequence=8)
    monkeypatch.delenv("AQG_NO_UPDATE_CHECK")
    for now in (10000, 20000):
        result = run.check(root=root, remote="origin", channel="stable", keyring_path=keyring, now=now)
        assert result.outcome == "pending", result
        assert any("codex: merge_hooks" in item for item in result.pending)
        assert root.resolve() == before
    _install(home, root, "--verify")
