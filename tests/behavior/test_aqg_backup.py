#!/usr/bin/env python3
"""Behavior tests for the centralized backup store (scripts/_aqg_backup.py).

Each test pins one invariant from the construction ledger (I1..I8). The store
is redirected to a tmp dir via AQG_BACKUP_DIR so nothing touches the real
~/.deeppattern.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime as real_datetime, timezone
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _aqg_backup as b  # noqa: E402


# ---------------------------------------------------------------------------
# fixtures / helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path, monkeypatch):
    """Redirect the backup base to a tmp dir and clear host env bleed."""
    base = tmp_path / "central"
    monkeypatch.setenv("AQG_BACKUP_DIR", str(base))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)
    return base


@pytest.fixture
def src(tmp_path):
    """A source_root with a file and a nested dir asset."""
    root = tmp_path / "client_root"
    (root / "sub").mkdir(parents=True)
    (root / "hooks.json").write_text('{"v":1}', encoding="utf-8")
    (root / "sub" / "a.txt").write_text("alpha", encoding="utf-8")
    (root / "tree").mkdir()
    (root / "tree" / "inner.txt").write_text("inner", encoding="utf-8")
    return root


def _freeze_time(monkeypatch, when="20260802T101530Z"):
    dt = real_datetime.strptime(when, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)

    class _Frozen:
        @staticmethod
        def now(tz=None):
            return dt.astimezone(tz) if tz else dt

    monkeypatch.setattr(b, "datetime", _Frozen)


def _try_symlink(link: Path, target: Path) -> bool:
    try:
        link.symlink_to(target)
        return link.is_symlink()
    except (OSError, NotImplementedError):
        return False


# ---------------------------------------------------------------------------
# I8 base resolution
# ---------------------------------------------------------------------------

def test_i8_base_env_override(tmp_path, monkeypatch):
    target = tmp_path / "override"
    monkeypatch.setenv("AQG_BACKUP_DIR", str(target))
    assert b.backup_base(create=False) == target


def test_i8_base_follows_aqg_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AQG_BACKUP_DIR", raising=False)
    aqg_root = tmp_path / "x" / "agent-quality-gates"
    monkeypatch.setenv("AQG_ROOT", str(aqg_root))
    assert b.backup_base(create=False) == tmp_path / "x" / "aqg-backups"


def test_i8_base_home_fallback(tmp_path, monkeypatch):
    monkeypatch.delenv("AQG_BACKUP_DIR", raising=False)
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert b.backup_base(create=False) == tmp_path / ".deeppattern" / "aqg-backups"


def test_i8_base_explicit_arg_beats_env_root(tmp_path, monkeypatch):
    monkeypatch.delenv("AQG_BACKUP_DIR", raising=False)
    monkeypatch.setenv("AQG_ROOT", str(tmp_path / "env" / "agent-quality-gates"))
    explicit = tmp_path / "explicit" / "agent-quality-gates"
    assert b.backup_base(explicit, create=False) == tmp_path / "explicit" / "aqg-backups"


# ---------------------------------------------------------------------------
# I3 relpath mirror
# ---------------------------------------------------------------------------

def test_i3_relpath_mirror_and_manifest(store, src):
    sess = b.BackupSession("cursor", src, scope="user", installer="test")
    dest = sess.backup(src / "sub" / "a.txt")
    run = sess.close()

    assert dest == run / "sub" / "a.txt"
    assert dest.read_text(encoding="utf-8") == "alpha"

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["client"] == "cursor"
    assert manifest["scope"] == "user"
    assert manifest["source_root"] == str(src.resolve())
    entry = next(e for e in manifest["entries"] if e["relpath"] == "sub/a.txt")
    assert entry["kind"] == "file"
    assert entry["sha256"]


def test_i3_absent_source_is_noop(store, src):
    sess = b.BackupSession("cursor", src)
    assert sess.backup(src / "does-not-exist.json") is None


def test_backup_refuses_source_outside_root(store, src, tmp_path):
    outsider = tmp_path / "outside.txt"
    outsider.write_text("x", encoding="utf-8")
    sess = b.BackupSession("cursor", src)
    with pytest.raises(b.BackupError, match="source_root"):
        sess.backup(outsider)


# ---------------------------------------------------------------------------
# I7 no-op leaves nothing behind
# ---------------------------------------------------------------------------

def test_i7_empty_session_creates_no_run(store, src):
    sess = b.BackupSession("cursor", src)
    assert sess.close() is None
    scope_dir = store / "cursor" / "user"
    assert not scope_dir.exists() or not any(scope_dir.iterdir())


def test_context_manager_discards_on_error(store, src):
    scope_dir = store / "cursor" / "user"
    with pytest.raises(RuntimeError, match="boom"):
        with b.BackupSession("cursor", src) as sess:
            sess.backup(src / "hooks.json")
            assert sess.run_dir is not None and sess.run_dir.exists()
            raise RuntimeError("boom")
    assert not scope_dir.exists() or not any(scope_dir.iterdir())


# ---------------------------------------------------------------------------
# I1 never-clobber (ported from audit 48a01f42 gemini-f4: same-second collision)
# ---------------------------------------------------------------------------

def test_i1_never_clobber_same_second(store, src, monkeypatch):
    _freeze_time(monkeypatch)  # both sessions compute the identical stamp+pid

    s1 = b.BackupSession("cursor", src)
    r1 = s1.backup(src / "hooks.json") and s1.close()
    s2 = b.BackupSession("cursor", src)
    (src / "hooks.json").write_text('{"v":2}', encoding="utf-8")
    r2 = s2.backup(src / "hooks.json") and s2.close()

    assert r1 != r2
    assert r2.name.endswith("-1")
    # both snapshots survive intact — the second did not overwrite the first
    assert json.loads((r1 / "hooks.json").read_text())["v"] == 1
    assert json.loads((r2 / "hooks.json").read_text())["v"] == 2
    runs = sorted((store / "cursor" / "user").iterdir())
    assert len(runs) == 2


def test_one_session_reuses_one_run(store, src):
    sess = b.BackupSession("cursor", src)
    sess.backup(src / "hooks.json")
    sess.backup(src / "sub" / "a.txt")
    sess.close()
    runs = list((store / "cursor" / "user").iterdir())
    assert len(runs) == 1


# ---------------------------------------------------------------------------
# I2 symlink refusal
# ---------------------------------------------------------------------------

def test_i2_refuses_symlink_source(store, src):
    link = src / "link.json"
    if not _try_symlink(link, src / "hooks.json"):
        pytest.skip("symlinks not permitted on this host")
    sess = b.BackupSession("cursor", src)
    with pytest.raises(b.BackupError, match="symlink"):
        sess.backup(link)


def test_i2_restore_refuses_symlink_target(store, src, tmp_path):
    sess = b.BackupSession("cursor", src)
    sess.backup(src / "hooks.json")
    run = sess.close()

    # replace the restore target with a symlink -> restore must refuse it
    (src / "hooks.json").unlink()
    if not _try_symlink(src / "hooks.json", tmp_path / "elsewhere"):
        pytest.skip("symlinks not permitted on this host")
    with pytest.raises(b.BackupError, match="symlink"):
        b.restore(run, force=True)


# ---------------------------------------------------------------------------
# I4 manifest-driven restore
# ---------------------------------------------------------------------------

def test_i4_restore_file_and_dir(store, src):
    sess = b.BackupSession("cursor", src)
    sess.backup(src / "hooks.json")
    sess.backup(src / "tree")
    run = sess.close()

    (src / "hooks.json").write_text("CHANGED", encoding="utf-8")
    (src / "tree" / "inner.txt").write_text("CHANGED", encoding="utf-8")

    restored = b.restore(run, force=True)
    assert (src / "hooks.json").read_text(encoding="utf-8") == '{"v":1}'
    assert (src / "tree" / "inner.txt").read_text(encoding="utf-8") == "inner"
    assert str((src / "hooks.json").resolve()) in {str(Path(p).resolve()) for p in restored}


def test_i4_restore_refuses_existing_without_force(store, src):
    sess = b.BackupSession("cursor", src)
    sess.backup(src / "hooks.json")
    run = sess.close()
    with pytest.raises(b.BackupError, match="exists"):
        b.restore(run)  # target still present, force=False


def test_i4_git_config_value_roundtrip(store, src, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sess = b.BackupSession("construction-hook", src, scope="project", project_root=repo)
    sess.backup_value("core.hooksPath", ".githooks", repo=repo)
    run = sess.close()

    entries = b.git_config_entries(run)
    assert entries == [
        {"key": "core.hooksPath", "value": ".githooks", "repo": str(repo.resolve())}
    ]
    # git-config entries are skipped by file/dir restore
    assert b.restore(run, force=True) == []


def test_project_scope_hash_differs_from_user(store, src, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    sess = b.BackupSession("construction-hook", src, scope="project", project_root=repo)
    assert sess.scope.startswith("project-")
    assert sess.scope != "user"


# ---------------------------------------------------------------------------
# I5 migrate-then-delete
# ---------------------------------------------------------------------------

def test_i5_migrate_then_delete(store, src):
    legacy = src / "hooks.json.aqg-hooks.bak"
    legacy.write_text('{"legacy":true}', encoding="utf-8")

    run = b.migrate_legacy("claude-code", src, [(legacy, "hooks.json")], scope="user")

    assert run is not None
    assert run.name.startswith("migrated-")
    assert not legacy.exists()  # in-place copy removed after migration

    manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["origin"] == "legacy-inplace"

    # migrated run restores to the ORIGINAL relative location
    (src / "hooks.json").write_text("CHANGED", encoding="utf-8")
    b.restore(run, force=True)
    assert json.loads((src / "hooks.json").read_text())["legacy"] is True


def test_i5_migrate_empty_is_noop(store, src):
    assert b.migrate_legacy("claude-code", src, [(src / "nope.bak", "hooks.json")]) is None
    assert not (store / "claude-code").exists()


# ---------------------------------------------------------------------------
# I6 retention gc
# ---------------------------------------------------------------------------

def _make_runs(store, client, n):
    scope_dir = store / client / "user"
    scope_dir.mkdir(parents=True, exist_ok=True)
    made = []
    for i in range(n):
        d = scope_dir / f"run{i:02d}"
        d.mkdir()
        (d / "manifest.json").write_text("{}", encoding="utf-8")
        os.utime(d, (1000 + i, 1000 + i))  # ascending mtime: run00 oldest
        made.append(d)
    return made


def test_i6_gc_keeps_newest_n(store):
    made = _make_runs(store, "cursor", 5)
    removed = b.gc("cursor", scope="user", keep=2)
    survivors = {d.name for d in (store / "cursor" / "user").iterdir()}
    assert survivors == {"run03", "run04"}  # two newest kept
    assert {d.name for d in removed} == {"run00", "run01", "run02"}
    assert made[-1].exists()  # newest always retained


def test_i6_gc_keep_zero_disables(store):
    _make_runs(store, "cursor", 3)
    assert b.gc("cursor", scope="user", keep=0) == []
    assert len(list((store / "cursor" / "user").iterdir())) == 3


def test_i6_gc_env_default(store, monkeypatch):
    monkeypatch.setenv("AQG_BACKUP_KEEP", "1")
    _make_runs(store, "cursor", 3)
    b.gc("cursor", scope="user")
    survivors = {d.name for d in (store / "cursor" / "user").iterdir()}
    assert survivors == {"run02"}


def test_latest_run_picks_newest_with_manifest(store):
    made = _make_runs(store, "cursor", 3)
    assert b.latest_run("cursor", scope="user") == made[-1]


def test_latest_run_none_when_absent(store):
    assert b.latest_run("cursor", scope="user") is None


# ---------------------------------------------------------------------------
# list_runs / remove_run — origin-filtered selection + consume
# ---------------------------------------------------------------------------

def _run_with_origin(store, client, name, origin, mtime):
    scope_dir = store / client / "user"
    scope_dir.mkdir(parents=True, exist_ok=True)
    d = scope_dir / name
    d.mkdir()
    (d / "manifest.json").write_text(
        json.dumps({"schema": b.SCHEMA, "origin": origin, "entries": []}),
        encoding="utf-8",
    )
    os.utime(d, (mtime, mtime))
    return d


def test_list_runs_filters_by_origin(store):
    inst = _run_with_origin(store, "construction-hook", "20260101T000000Z-1", "install", 2000)
    _run_with_origin(store, "construction-hook", "migrated-20260101T000000Z-1", "legacy-inplace", 9000)

    only_install = b.list_runs("construction-hook", scope="user", origins=("install",))
    assert only_install == [inst]
    # unfiltered still returns both, newest (the migrated one) first
    assert len(b.list_runs("construction-hook", scope="user")) == 2


def test_latest_run_origin_beats_mtime(store):
    # An install run selected by origin wins even though a legacy run is newer.
    inst = _run_with_origin(store, "construction-hook", "20260101T000000Z-1", "install", 2000)
    _run_with_origin(store, "construction-hook", "migrated-x", "legacy-inplace", 9000)
    assert b.latest_run("construction-hook", scope="user", origins=("install",)) == inst


def test_remove_run_is_idempotent(store):
    # remove_run reports whether the run is GONE, not whether it did work: a real
    # removal and a no-op on an already-absent run both return True (round-2 V2f4).
    made = _make_runs(store, "cursor", 1)
    assert b.remove_run(made[0]) is True
    assert not made[0].exists()
    assert b.remove_run(made[0]) is True  # already gone == still "gone"
