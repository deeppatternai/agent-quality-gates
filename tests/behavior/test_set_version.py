"""Release version markers must follow VERSION without rewriting history."""

import re
import runpy
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
MARKERS = {"README.md": "Current version: ", "README.zh-CN.md": "当前版本："}


def test_repository_current_versions_agree():
    version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
    for name, prefix in MARKERS.items():
        content = (ROOT / name).read_text(encoding="utf-8")
        assert re.findall(rf"(?m)^{re.escape(prefix)}`([^`\r\n]+)`", content) == [version], name


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/set_version.py", tmp_path / "scripts/set_version.py")
    (tmp_path / "VERSION").write_bytes(b"1.2.3\n")
    for name, prefix in MARKERS.items():
        (tmp_path / name).write_bytes(
            f"# AQG\r\n\r\n{prefix}`1.0.0` (VERSION).\r\n\r\nHistory: `1.0.0`.\r\n".encode()
        )
    (tmp_path / "CHANGELOG.md").write_bytes(b"## 1.0.0\nHistorical notes\n")
    return tmp_path


def run(repo, *args):
    return subprocess.run(
        [sys.executable, str(repo / "scripts/set_version.py"), *args],
        cwd=repo.parent, capture_output=True, text=True, encoding="utf-8",
    )


def snapshot(repo):
    return {p.name: p.read_bytes() for p in repo.iterdir() if p.is_file()}


def test_sync_preserves_unmanaged_bytes_and_is_idempotent(repo):
    before = snapshot(repo)
    assert run(repo).returncode == 0
    for name, prefix in MARKERS.items():
        expected = before[name].replace(
            f"{prefix}`1.0.0`".encode(), f"{prefix}`1.2.3`".encode(), 1
        )
        assert (repo / name).read_bytes() == expected
    assert (repo / "CHANGELOG.md").read_bytes() == before["CHANGELOG.md"]
    synced = snapshot(repo)
    mtimes = {p.name: p.stat().st_mtime_ns for p in repo.iterdir() if p.is_file()}
    assert run(repo).returncode == 0
    assert snapshot(repo) == synced
    assert {p.name: p.stat().st_mtime_ns for p in repo.iterdir() if p.is_file()} == mtimes


def test_check_reports_drift_and_never_writes(repo):
    before = snapshot(repo)
    result = run(repo, "--check")
    assert result.returncode == 1
    assert all(name in result.stderr for name in MARKERS)
    assert snapshot(repo) == before
    assert run(repo).returncode == 0
    assert run(repo, "--check").returncode == 0


@pytest.mark.parametrize("version", ["2.0.0", "v2.0.0-rc.1+build.01"])
def test_explicit_version_updates_source_and_markers(repo, version):
    assert run(repo, version).returncode == 0
    expected = version.removeprefix("v")
    assert (repo / "VERSION").read_bytes() == (expected + "\n").encode()
    for name, prefix in MARKERS.items():
        assert f"{prefix}`{expected}`" in (repo / name).read_text(encoding="utf-8")
    assert run(repo, "--check").returncode == 0


@pytest.mark.parametrize("args", [
    ["1.2"], ["01.2.3"], ["1.2.3-01"], ["vv1.2.3"], ["１.2.3"],
    ["1.2.3", "2.0.0"], ["2.0.0", "--check"], ["--unknown"],
])
def test_invalid_arguments_leave_all_files_unchanged(repo, args):
    before = snapshot(repo)
    assert run(repo, *args).returncode != 0
    assert snapshot(repo) == before


@pytest.mark.parametrize("check", [False, True])
@pytest.mark.parametrize("broken", ["missing", "duplicate", "unreadable", "absent"])
def test_bad_target_fails_before_any_write(repo, check, broken):
    path = repo / "README.zh-CN.md"
    if broken == "missing":
        path.write_text("no marker\n", encoding="utf-8")
    elif broken == "duplicate":
        path.write_bytes(path.read_bytes() * 2)
    elif broken == "unreadable":
        path.write_bytes(b"\xff")
    else:
        path.unlink()
    before = snapshot(repo)
    args = ["--check"] if check else ["2.0.0"]
    result = run(repo, *args)
    assert result.returncode == 1
    assert result.stderr
    assert snapshot(repo) == before


def test_invalid_source_version_is_rejected_in_sync_and_check(repo):
    (repo / "VERSION").write_bytes(b"not-semver\n")
    before = snapshot(repo)
    for args in [[], ["--check"]]:
        result = run(repo, *args)
        assert result.returncode == 1
        assert "invalid version" in result.stderr
        assert snapshot(repo) == before


@pytest.mark.parametrize("raw", [b"1.2.3", b" 1.2.3\r\n"])
def test_normalization_only_drift_is_reported_and_repaired(repo, raw):
    assert run(repo).returncode == 0
    (repo / "VERSION").write_bytes(raw)
    before = snapshot(repo)
    result = run(repo, "--check")
    assert result.returncode == 1
    assert "VERSION: not normalized" in result.stderr
    assert snapshot(repo) == before
    assert run(repo).returncode == 0
    assert snapshot(repo) == {**before, "VERSION": b"1.2.3\n"}


@pytest.mark.parametrize("args", [[], ["--check"], ["2.0.0"]])
def test_missing_source_is_not_bootstrapped(repo, args):
    (repo / "VERSION").unlink()
    before = snapshot(repo)
    assert run(repo, *args).returncode == 1
    assert snapshot(repo) == before


def test_unreadable_source_and_write_error_are_reported(repo, monkeypatch, capsys):
    (repo / "VERSION").write_bytes(b"\xff")
    before = snapshot(repo)
    assert run(repo).returncode == run(repo, "--check").returncode == 1
    assert snapshot(repo) == before
    (repo / "VERSION").write_bytes(b"1.2.3\n")
    main = runpy.run_path(str(repo / "scripts/set_version.py"))["main"]

    def fail_write(path, data):
        raise OSError("injected write failure")

    monkeypatch.setattr(Path, "write_bytes", fail_write)
    assert main([]) == 1
    captured = capsys.readouterr()
    assert "version sync failed: injected write failure" in captured.err
    assert "all markers agree" not in captured.out
