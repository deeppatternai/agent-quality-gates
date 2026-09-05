"""L2 pre-launch hardening regression suite for the construction-hook installer.

Each test pins one finding from audit 48a01f42 (gpt-5.5 #7/#8/#9 + gemini #6/#7)
+ the Claude review (workflow PR-C). Failing-before / passing-after — the
behavioural repro tests FAIL against the pre-fix
scripts/install_aqg_construction_hook.py (proven via `git stash push` of the
source file, leaving this test in place).

Findings covered:
  * C7 — relative core.hooksPath was set while the hook was written under the
    *invocation* dir; from a subdirectory git looked at the repo root and never
    found the hook. Fix anchors every .aqg op at `git rev-parse --show-toplevel`.
  * C9 — `_is_git_repo` trusted rc==0 from --is-inside-work-tree; a bare repo
    exits 0 printing "false". Fix requires literal "true" + a real top-level.
  * C8 — `shutil.copy` + chmod followed a symlink at the destination (--force
    out-of-tree clobber) and was non-atomic. Fix rejects a symlink dest and
    writes via tempfile + fsync + chmod + os.replace.
  * C11 — the core.hooksPath backup used a bare write_text (non-atomic, and it
    too followed a symlink). Same atomic-write fix.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import install_aqg_construction_hook as iac  # noqa: E402
import _aqg_backup as backup  # noqa: E402

HOOK_REL = Path(iac.HOOK_DIR_REL) / iac.HOOK_FILE  # .aqg/hooks/pre-commit

CENTRAL_DIRNAME = ".aqg-central"


# ----------------------------------------------------------------------------- fixtures


@pytest.fixture(autouse=True)
def _isolate_central_backups(tmp_path, monkeypatch):
    """Pin the central backup store under tmp so no test touches the real store.

    ``install``/``uninstall`` now stash the overwritten ``core.hooksPath`` in the
    central store (``scripts/_aqg_backup.py``). Without this, a session would
    ``mkdir`` ``dirname(AQG_ROOT)/aqg-backups`` outside the repo and tests would
    read each other's (and the real) runs. ``AQG_BACKUP_DIR`` wins over every
    other base source, covering both the in-process installer and any subprocess.
    """
    monkeypatch.setenv("AQG_BACKUP_DIR", str(tmp_path / CENTRAL_DIRNAME))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)


# ----------------------------------------------------------------------------- helpers


def _central_runs(repo: Path) -> list[Path]:
    """All completed central run dirs for construction-hook / this repo's scope."""
    base = Path(os.environ["AQG_BACKUP_DIR"])
    scope_dir = base / iac.CLIENT_ID / backup.scope_key("project", repo)
    if not scope_dir.is_dir():
        return []
    return [
        d
        for d in scope_dir.iterdir()
        if d.is_dir() and (d / "manifest.json").is_file()
    ]


def _central_hookspath_value(repo: Path) -> str | None:
    """The core.hooksPath value recorded by the newest central run, or None."""
    run = backup.latest_run(iac.CLIENT_ID, scope="project", project_root=repo)
    if run is None:
        return None
    for entry in backup.git_config_entries(run):
        if entry["key"] == iac.HOOKSPATH_KEY:
            return entry["value"].strip()
    return None


# ----------------------------------------------------------------------------- helpers


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )


def _make_repo(tmp_path: Path, name: str = "work") -> Path:
    repo = tmp_path / name
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.com")
    _git(repo, "config", "user.name", "t")
    return repo


def _hooks_path(repo: Path) -> str | None:
    p = subprocess.run(
        ["git", "-C", str(repo), "config", "--get", "core.hooksPath"],
        text=True,
        capture_output=True,
        timeout=10,
    )
    return p.stdout.strip() if p.returncode == 0 else None


# ----------------------------------------------------------------------------- C7


def test_c7_install_from_subdir_writes_hook_at_repo_top(tmp_path):
    # Running from a nested subdir must land the hook at <repo-top>/.aqg/hooks,
    # because git resolves the relative core.hooksPath against the work-tree root.
    repo = _make_repo(tmp_path)
    sub = repo / "pkg" / "deep"
    sub.mkdir(parents=True)

    rc = iac.install(sub, REPO)
    assert rc == iac.EXIT_OK, rc

    assert (repo / HOOK_REL).is_file(), "hook must be installed at the repo top-level"
    assert not (sub / HOOK_REL).exists(), "hook must NOT land under the subdir"
    assert _hooks_path(repo) == iac.HOOK_DIR_REL


def test_c7_uninstall_from_subdir_removes_top_level_hook(tmp_path):
    repo = _make_repo(tmp_path)
    assert iac.install(repo, REPO) == iac.EXIT_OK
    assert (repo / HOOK_REL).is_file()

    sub = repo / "pkg"
    sub.mkdir()
    rc = iac.uninstall(sub)
    assert rc == iac.EXIT_OK
    assert not (repo / HOOK_REL).exists(), "uninstall from a subdir must remove the top-level hook"


def test_c7_resolve_git_top_from_subdir_returns_root(tmp_path):
    repo = _make_repo(tmp_path)
    sub = repo / "a" / "b"
    sub.mkdir(parents=True)
    assert iac._resolve_git_top(sub) == repo.resolve()


# ----------------------------------------------------------------------------- C9


def test_c9_bare_repo_is_not_a_work_tree(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q")
    # A bare repo answers `--is-inside-work-tree` with "false" at rc==0.
    rc = iac.install(bare, REPO)
    assert rc == iac.EXIT_NOT_GIT, f"bare repo must be rejected, got {rc}"
    assert not (bare / HOOK_REL).exists()


def test_c9_resolve_git_top_rejects_bare(tmp_path):
    bare = tmp_path / "bare.git"
    bare.mkdir()
    _git(bare, "init", "--bare", "-q")
    assert iac._resolve_git_top(bare) is None


def test_c9_resolve_git_top_rejects_non_repo(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert iac._resolve_git_top(plain) is None


# ----------------------------------------------------------------------------- C8


def test_c8_install_refuses_symlink_hook_dest(tmp_path):
    # An attacker-planted symlink at the hook path must NOT be followed: --force
    # must refuse rather than write the hook content through it to an out-of-tree file.
    repo = _make_repo(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("PRECIOUS", encoding="utf-8")

    hook_path = repo / HOOK_REL
    hook_path.parent.mkdir(parents=True, exist_ok=True)
    hook_path.symlink_to(outside)

    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_GENERIC, f"symlink dest must be refused, got {rc}"
    assert outside.read_text(encoding="utf-8") == "PRECIOUS", "must not write through the symlink"


def test_c8_install_is_atomic_and_executable(tmp_path):
    # GREEN guard: a normal install yields a 0o755 regular file matching the
    # source, with no leftover temp file in the hooks dir.
    repo = _make_repo(tmp_path)
    rc = iac.install(repo, REPO)
    assert rc == iac.EXIT_OK
    hook_path = repo / HOOK_REL
    assert hook_path.is_file() and not hook_path.is_symlink()
    assert hook_path.stat().st_mode & 0o777 == 0o755
    source = (REPO / iac.HOOK_SOURCE_REL).read_bytes()
    assert hook_path.read_bytes() == source
    residue = list((repo / iac.HOOK_DIR_REL).glob(".aqg-tmp-*"))
    assert residue == [], f"atomic temp file leaked: {residue}"


def test_c8_force_reinstall_over_regular_file_ok(tmp_path):
    # The normal idempotent --force path (regular file, not a symlink) must work.
    repo = _make_repo(tmp_path)
    assert iac.install(repo, REPO) == iac.EXIT_OK
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK
    assert (repo / HOOK_REL).is_file()


# ----------------------------------------------------------------------------- C11


def test_c11_backup_ignores_stray_intree_symlink(tmp_path):
    # C11 (post-centralization): the overwritten core.hooksPath is stashed in the
    # central store, never an in-tree file. A stray symlink at the legacy in-tree
    # backup path is left untouched (never written through), install still
    # succeeds, and the prior value is recorded centrally.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    outside = tmp_path / "outside_backup.txt"
    outside.write_text("KEEPME", encoding="utf-8")

    stray = repo / iac.HOOKSPATH_BACKUP_REL
    stray.parent.mkdir(parents=True, exist_ok=True)
    stray.symlink_to(outside)

    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_OK, rc
    assert outside.read_text(encoding="utf-8") == "KEEPME", "must not write through the stray symlink"
    assert _central_hookspath_value(repo) == ".husky"
    assert _hooks_path(repo) == iac.HOOK_DIR_REL


def test_c11_backup_recorded_centrally_and_restored_on_uninstall(tmp_path):
    # GREEN guard: --force over a non-AQG hooksPath records the prior value in the
    # central store (atomically, no leaked temp file) and uninstall restores it.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")

    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_OK
    assert _central_hookspath_value(repo) == ".husky"
    assert _hooks_path(repo) == iac.HOOK_DIR_REL
    assert not (repo / iac.HOOKSPATH_BACKUP_REL).exists(), "nothing is stashed in-tree anymore"
    run = backup.latest_run(iac.CLIENT_ID, scope="project", project_root=repo)
    assert run is not None
    residue = list((run / "_values").glob(".aqg-bk-*"))
    assert residue == [], f"atomic temp file leaked: {residue}"

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky", "uninstall must restore the prior core.hooksPath"


def test_c11_migrates_and_removes_legacy_intree_backup(tmp_path):
    # A legacy in-tree .aqg/.hookspath_backup (written by an older installer) is
    # folded into the central store and deleted (migrate-then-delete); a later
    # uninstall restores the value from the migrated central run, then consumes
    # the whole slot so nothing stale is left behind.
    repo = _make_repo(tmp_path)
    assert iac.install(repo, REPO) == iac.EXIT_OK  # core.hooksPath = .aqg/hooks
    legacy = repo / iac.HOOKSPATH_BACKUP_REL
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(".husky\n", encoding="utf-8")

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert not legacy.exists(), "legacy in-tree backup must be removed after migration"
    assert _hooks_path(repo) == ".husky", "value must be restored from the migrated central backup"
    assert _central_runs(repo) == [], "uninstall must consume the backup slot"


# ----------------------------------------------------------------------------- positive baseline


def test_install_from_root_baseline(tmp_path):
    repo = _make_repo(tmp_path)
    rc = iac.install(repo, REPO)
    assert rc == iac.EXIT_OK
    hook_path = repo / HOOK_REL
    assert hook_path.is_file()
    assert os.access(hook_path, os.X_OK)
    assert _hooks_path(repo) == iac.HOOK_DIR_REL


# ===========================================================================
# Fix-review (audit 0ff891b1, gpt-5.5 + gemini) — second-order findings on the
# round-1 fix. Each FAILS against the original HEAD installer.
# ===========================================================================


# --- fix-review #1 (convergent): symlinked PARENT directory bypass -----------


def test_f1_install_refuses_symlinked_parent_hooks_dir(tmp_path):
    # `.aqg/hooks` checked in as a symlink to an out-of-tree dir must NOT be
    # traversed — the round-1 final-component is_symlink() guard missed this and
    # mkstemp/os.replace would write into the external dir.
    repo = _make_repo(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (repo / ".aqg").mkdir()
    (repo / ".aqg" / "hooks").symlink_to(external, target_is_directory=True)

    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_GENERIC, f"symlinked parent must be refused, got {rc}"
    assert not (external / iac.HOOK_FILE).exists(), "must not write into the symlinked-out dir"
    assert list(external.iterdir()) == [], "external dir must stay empty"


def test_f1_install_refuses_symlinked_dot_aqg(tmp_path):
    # `.aqg` itself symlinked out of tree (affects hook + backup paths).
    repo = _make_repo(tmp_path)
    external = tmp_path / "ext2"
    external.mkdir()
    (repo / ".aqg").symlink_to(external, target_is_directory=True)

    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_GENERIC
    assert not (external / "hooks").exists(), "must not create the hooks dir out of tree"


def test_f1_contained_helper(tmp_path):
    repo = _make_repo(tmp_path)
    assert iac._contained(repo / ".aqg" / "hooks" / "pre-commit", repo) is True
    external = tmp_path / "out"
    external.mkdir()
    (repo / ".aqg").mkdir()
    (repo / ".aqg" / "hooks").symlink_to(external, target_is_directory=True)
    assert iac._contained(repo / ".aqg" / "hooks" / "pre-commit", repo) is False


# --- fix-review #2 (gemini): uninstall must not read a symlinked backup ------


def test_uninstall_ignores_symlinked_backup(tmp_path):
    # An attacker-checked-in `.aqg/.hookspath_backup` symlink must not have its
    # target read and injected into core.hooksPath on --uninstall.
    repo = _make_repo(tmp_path)
    assert iac.install(repo, REPO) == iac.EXIT_OK
    evil = tmp_path / "evil.txt"
    evil.write_text("/attacker/controlled/hooks\n", encoding="utf-8")
    backup_path = repo / iac.HOOKSPATH_BACKUP_REL
    if backup_path.exists() or backup_path.is_symlink():
        backup_path.unlink()
    backup_path.symlink_to(evil)

    rc = iac.uninstall(repo)
    assert rc == iac.EXIT_OK
    assert _hooks_path(repo) != "/attacker/controlled/hooks", "must not inject the symlink target"


# --- fix-review #2 (gpt): failed install must not leave a stale backup -------


def test_failed_config_set_discards_backup(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    real_run = iac._safe_run

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:  # let _resolve_git_top work for real
            return real_run(cmd, *a, **k)
        if "config" in cmd and "--get" in cmd:  # report a pre-existing non-AQG hooksPath
            return (0, ".husky")
        if "config" in cmd and iac.HOOK_DIR_REL in cmd:  # FAIL the AQG config set
            return (1, "simulated git config failure")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(iac, "_safe_run", fake_run)
    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_GENERIC
    assert _central_runs(repo) == [], "central backup run must be discarded when config-set fails"


# --- fix-review #5 (gemini): empirically prove git runs the resolved hook ----


def test_hook_fires_from_subdir_after_install(tmp_path):
    repo = _make_repo(tmp_path)
    (repo / "seed.txt").write_text("seed\n")
    _git(repo, "add", "seed.txt")
    _git(repo, "commit", "-q", "-m", "seed")

    sub = repo / "pkg"
    sub.mkdir()
    assert iac.install(sub, REPO) == iac.EXIT_OK
    hook = repo / HOOK_REL
    assert hook.is_file(), "precondition: hook installed at the repo top-level"

    sentinel = repo / "HOOK_FIRED.marker"
    hook.write_text(f"#!/bin/sh\ntouch '{sentinel}'\nexit 0\n", encoding="utf-8")
    hook.chmod(0o755)

    (sub / "f.txt").write_text("hi\n")
    _git(sub, "add", "f.txt")
    _git(sub, "commit", "-q", "-m", "trigger")
    assert sentinel.exists(), "git must resolve+run the hook via the relative core.hooksPath"


# ===========================================================================
# Round-2 VERIFICATION audit (49b58659, gpt-5.5 + gemini, convergent) — the
# round-2 fix was applied thoroughly to install() but left two gaps.
# ===========================================================================


def test_f1_uninstall_refuses_symlinked_parent_hooks_dir(tmp_path):
    # round-2 verify #1 (convergent, blocking): uninstall's is_symlink-first
    # branch followed an escaping parent and would unlink an out-of-tree entry.
    repo = _make_repo(tmp_path)
    external = tmp_path / "ext_uninstall"
    external.mkdir()
    target = external / "target.txt"
    target.write_text("victim\n", encoding="utf-8")
    victim = external / iac.HOOK_FILE  # external/pre-commit, itself a symlink
    victim.symlink_to(target)

    (repo / ".aqg").mkdir()
    (repo / ".aqg" / "hooks").symlink_to(external, target_is_directory=True)

    rc = iac.uninstall(repo)
    assert rc == iac.EXIT_OK
    assert victim.is_symlink(), "uninstall must NOT delete the out-of-tree symlink entry"


def test_f2_hook_write_oserror_discards_backup(tmp_path, monkeypatch):
    # round-2 verify #2 (convergent): a post-backup OSError on the hook write
    # must discard the backup and exit cleanly (not propagate, not strand it).
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    real_aw = iac._atomic_write

    def fake_aw(dest, data, *, mode):
        if dest.name == iac.HOOK_FILE:  # fail only the hook write
            raise OSError("simulated ENOSPC")
        return real_aw(dest, data, mode=mode)

    monkeypatch.setattr(iac, "_atomic_write", fake_aw)
    rc = iac.install(repo, REPO, force=True)
    assert rc == iac.EXIT_GENERIC
    assert _central_runs(repo) == [], "central backup run must be discarded on a hook-write OSError"


# ===========================================================================
# External audit aud_XQ2a3NORHvh0281E (4 voices, all "has-serious-issues") — the
# latest_run-based restore was defeated in several ordinary paths. Each test
# FAILS against the pre-fix installer (newest-run-wins + no consume).
# ===========================================================================


def test_uninstall_prefers_install_over_uninstall_time_migration(tmp_path):
    # Cluster A (V0f1/V3f5): a legacy in-tree file that survives into --uninstall
    # is migrated at uninstall time (the newest run of all). It must NOT shadow the
    # value stashed by the last install; the install value wins.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK  # install run = .husky

    # a stale legacy in-tree backup with a DIFFERENT value appears before uninstall
    legacy = repo / iac.HOOKSPATH_BACKUP_REL
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(".other\n", encoding="utf-8")

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky", "install run must outrank the uninstall-time migration"
    assert _central_runs(repo) == [], "slot consumed"


def test_install_run_beats_newer_migrated_run(tmp_path):
    # Cluster B (V0f2/V1f2/V2f1/V3f2): selection is origin-based, not (mtime,name).
    # A migrated run with a strictly NEWER mtime than the install run must still
    # lose — proving the same-second / 'migrated-'-sorts-after tie can't win.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK  # install run = .husky

    # fabricate a migrated (legacy-inplace) run holding a different value, newer mtime
    sess = iac._new_session(repo, REPO, origin="legacy-inplace")
    sess._run_prefix = "migrated-"
    sess.backup_value(iac.HOOKSPATH_KEY, ".legacy", repo=repo)
    migrated = sess.close()
    assert migrated is not None
    future = os.stat(migrated).st_mtime + 10_000
    os.utime(migrated, (future, future))

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky", "install-origin run must beat a newer migrated run"


def test_reinstall_after_unset_does_not_restore_stale(tmp_path):
    # Cluster C (V0f3/V1f1/V3f1): after a full install/uninstall cycle restores the
    # user's value and they unset it, a plain reinstall (no prior value) must
    # --unset on uninstall, NOT restore the earlier husky from a stale run.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK
    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky"

    _git(repo, "config", "--unset", "core.hooksPath")  # user drops husky
    assert iac.install(repo, REPO) == iac.EXIT_OK       # plain install, no prior value
    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) is None, "must unset, not restore a stale husky"


def test_redundant_reinstall_preserves_prior_value(tmp_path):
    # Cluster C guard (why consume, not always-write-sentinel): a redundant --force
    # reinstall while core.hooksPath is already AQG-owned records NO new run and
    # must not shadow the genuine prior value — uninstall still restores husky.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK   # run = .husky
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK   # core already .aqg/hooks -> no new run

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky", "the genuine prior value must survive a redundant reinstall"


def test_double_uninstall_keeps_restored_value(tmp_path):
    # Cluster D (V2f3): a second --uninstall must not clobber the value the first
    # one restored. Once core.hooksPath is no longer AQG-owned, it is left alone.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK
    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky"

    assert iac.uninstall(repo) == iac.EXIT_OK, "second uninstall is a clean no-op"
    assert _hooks_path(repo) == ".husky", "must not unset/overwrite the user's restored value"


def test_uninstall_leaves_user_reassigned_hookspath(tmp_path):
    # Cluster D: if the user re-points core.hooksPath after install, uninstall must
    # remove the AQG hook but leave their value untouched (owned-guard).
    repo = _make_repo(tmp_path)
    assert iac.install(repo, REPO) == iac.EXIT_OK
    _git(repo, "config", "core.hooksPath", ".config/git-hooks")  # user takes it over

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".config/git-hooks", "user's reassigned value must be preserved"
    assert not (repo / HOOK_REL).exists(), "the AQG hook artifact is still removed"


def test_uninstall_restore_failure_returns_nonzero(tmp_path, monkeypatch):
    # Cluster H (V1f4): a failed git-config restore must abort non-zero with the
    # install left intact (hook + backup kept) — never a silent success.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK
    real_run = iac._safe_run

    def fake_run(cmd, *a, **k):
        if "rev-parse" in cmd:
            return real_run(cmd, *a, **k)
        if "--get" in cmd and "core.hooksPath" in cmd:  # AQG owns it
            return (0, iac.HOOK_DIR_REL)
        if "config" in cmd and ".husky" in cmd:         # FAIL the restore set
            return (1, "simulated restore failure")
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(iac, "_safe_run", fake_run)
    rc = iac.uninstall(repo)
    assert rc == iac.EXIT_GENERIC, "restore failure must surface as non-zero"
    assert (repo / HOOK_REL).is_file(), "hook must be left in place on restore failure"
    assert _central_runs(repo) != [], "backup must be kept for a retry"


def test_read_hookspath_no_follow_rejects_symlink(tmp_path):
    # Cluster F (V3f4): the legacy reader must not follow a symlink to read an
    # out-of-tree target (TOCTOU-hardened) — even if the is_symlink precheck were
    # bypassed, the fd read refuses a non-regular / symlinked open.
    secret = tmp_path / "secret.txt"
    secret.write_text("/attacker/hooks\n", encoding="utf-8")
    link = tmp_path / "link"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this host")
    assert iac._read_hookspath_no_follow(link) is None

    # a real regular file reads back verbatim minus the trailing newline
    plain = tmp_path / "plain"
    plain.write_text(".githooks\n", encoding="utf-8")
    assert iac._read_hookspath_no_follow(plain) == ".githooks"


# ===========================================================================
# Round-2 re-audit aud_85-b-9x-RMFTlZMf — consume-on-owned, snapshot-bounded
# consume, and local-scope isolation. Failing-before / passing-after.
# ===========================================================================


def test_uninstall_not_owned_preserves_backup_slot(tmp_path):
    # round-2 V1f2/V4f3: an uninstall that restores NOTHING (the user re-pointed
    # core.hooksPath elsewhere) must NOT destroy the last record of the pre-install
    # value. Consume runs only on the owned path. FAILS against the pre-round-2
    # installer, which consumed the whole slot unconditionally.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK  # backs up .husky
    assert _central_runs(repo) != [], "precondition: a backup run exists"

    _git(repo, "config", "core.hooksPath", ".config/git-hooks")  # user takes it over
    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".config/git-hooks", "user value left untouched"
    assert not (repo / HOOK_REL).exists(), "AQG hook artifact still removed"
    assert _central_runs(repo) != [], "not-owned uninstall must NOT consume the slot"


def test_uninstall_consume_is_snapshot_bounded(tmp_path, monkeypatch):
    # round-2 V4f1: consume deletes exactly the snapshot taken at uninstall start,
    # never a fresh listing — so an install-origin run written by a *concurrent*
    # install AFTER the snapshot is neither restored-from nor deleted. We simulate
    # the race by materialising that run inside _hookspath_runs, right after the
    # real snapshot is captured.
    repo = _make_repo(tmp_path)
    _git(repo, "config", "core.hooksPath", ".husky")
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK  # the selected run

    real = iac._hookspath_runs
    concurrent: dict = {}

    def snapshot_then_race(target_top, aqg_root):
        installs, legacies = real(target_top, aqg_root)  # snapshot excludes the run below
        sess = iac._new_session(target_top, aqg_root)
        sess.backup_value(iac.HOOKSPATH_KEY, ".concurrent", repo=target_top)
        concurrent["run"] = sess.close()
        return installs, legacies

    monkeypatch.setattr(iac, "_hookspath_runs", snapshot_then_race)

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _hooks_path(repo) == ".husky", "the snapshot's install run is still restored"
    assert concurrent["run"] is not None and concurrent["run"].exists(), (
        "a concurrent install's run created after the snapshot must survive consume"
    )


def test_install_uninstall_ignores_global_hookspath(tmp_path, monkeypatch):
    # round-2 V2f8: install mutates the LOCAL scope only, so a core.hooksPath that
    # lives in GLOBAL config must never be captured into a run nor rewritten into
    # the local scope on uninstall — the global value stays put, no local shadow.
    # FAILS against the pre-round-2 installer, which captured the effective value.
    repo = _make_repo(tmp_path)
    global_cfg = tmp_path / "gitconfig-global"
    global_cfg.write_text("[core]\n\thooksPath = /global/hooks\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_cfg))
    if _hooks_path(repo) != "/global/hooks":
        pytest.skip("git does not honor GIT_CONFIG_GLOBAL on this host")

    def _local(repo_: Path):
        return subprocess.run(
            ["git", "-C", str(repo_), "config", "--local", "--get", "core.hooksPath"],
            text=True, capture_output=True, timeout=10,
        )

    # effective value is the global one -> the gate trips; --force overrides it
    assert iac.install(repo, REPO, force=True) == iac.EXIT_OK
    assert _central_runs(repo) == [], "a global-only value must not be backed up"
    assert _local(repo).stdout.strip() == iac.HOOK_DIR_REL, "local now points at AQG"

    assert iac.uninstall(repo) == iac.EXIT_OK
    assert _local(repo).returncode != 0, "local core.hooksPath must be unset after uninstall"
    assert _hooks_path(repo) == "/global/hooks", "the global value must survive intact"
