"""Behavior contracts for moving an install onto the version-tree layout.

docs/UPDATE_ARCHITECTURE.md §5. Every install in the field is a plain checkout
at ``AQG_ROOT``; the managed-update engine needs a symlink pointing into a
``versions/<sha>/`` directory. Nothing bridges the two, which means the
automatic channel shipped in PR6 cannot apply anything to any existing install.
This is that bridge.

It is a **one-way, one-time** change to a directory the user owns and that every
running session dereferences, so the contracts here are about refusing:

* refuse anything that is not a clean, ordinary git checkout — a dirty tree, a
  worktree, a symlink already, a directory that is not a repository at all;
* leave the checkout exactly where it was if any step fails;
* never destroy the user's tree — the migration MOVES it and then points at it,
  so the same inode holds the same files at the end.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import migrate as migrate_mod


def _checkout(tmp_path: Path, name: str = "aqg") -> Path:
    root = tmp_path / name
    root.mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "scripts" / "_aqg_context.sh").write_text("# helper\n", encoding="utf-8")
    (root / "VERSION").write_text("0.16.0\n", encoding="utf-8")

    def git(*args):
        return subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=True
        )

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "T")
    git("add", "-A")
    git("commit", "-m", "installed")
    return root


def _head(root: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "rev-parse", "HEAD"],
        capture_output=True, check=True,
    ).stdout.decode().strip()


# --- the migration itself --------------------------------------------------------------


def test_a_plain_checkout_becomes_a_symlink_into_versions(tmp_path):
    root = _checkout(tmp_path)
    commit = _head(root)
    result = migrate_mod.migrate(root)

    assert root.is_symlink()
    assert result.versions_dir == tmp_path / "versions"
    assert os.path.realpath(root) == str((tmp_path / "versions" / commit).resolve())


def test_the_files_survive_and_are_the_same_ones(tmp_path):
    """A migration that copied would double the disk and, worse, leave two trees
    that can drift. This MOVES: the same inode holds the same files at the end.
    """
    root = _checkout(tmp_path)
    marker = root / "scripts" / "_aqg_context.sh"
    before = marker.stat().st_ino
    migrate_mod.migrate(root)
    assert (root / "scripts" / "_aqg_context.sh").read_text() == "# helper\n"
    assert (root / "scripts" / "_aqg_context.sh").stat().st_ino == before


def test_the_git_checkout_still_works_afterwards(tmp_path):
    """The engine fetches through this root, so it has to still be a repository."""
    root = _checkout(tmp_path)
    commit = _head(root)
    migrate_mod.migrate(root)
    assert _head(root) == commit


def test_the_result_is_what_the_engine_expects(tmp_path):
    """The whole point: `stage.current_target` must resolve, because the runner
    refuses an install where it does not."""
    from scripts.aqg_update import stage

    root = _checkout(tmp_path)
    migrate_mod.migrate(root)
    live = stage.current_target(root)
    assert live is not None
    assert live.parent == tmp_path / "versions"


# --- what it refuses -------------------------------------------------------------------


def test_an_already_migrated_install_is_left_alone(tmp_path):
    """Re-running must be safe: a user who is not sure whether they migrated
    should be able to just run it again."""
    root = _checkout(tmp_path)
    first = migrate_mod.migrate(root)
    again = migrate_mod.migrate(root)
    assert again.already is True
    assert again.target == first.target


def test_a_dirty_checkout_is_refused(tmp_path):
    """Uncommitted work in the install is work the user put there. Moving the
    tree is safe for it, but the version name would be a lie: the directory
    would be named after a commit whose contents it does not hold."""
    root = _checkout(tmp_path)
    (root / "VERSION").write_text("edited\n", encoding="utf-8")
    with pytest.raises(migrate_mod.MigrateError, match="uncommitted"):
        migrate_mod.migrate(root)
    assert not root.is_symlink()


def test_an_untracked_file_is_refused(tmp_path):
    """Same reason, and the one people actually hit: a scratch file in the
    install would be carried into a tree named after a commit that never had it.
    """
    root = _checkout(tmp_path)
    (root / "notes.txt").write_text("mine\n", encoding="utf-8")
    with pytest.raises(migrate_mod.MigrateError, match="untracked"):
        migrate_mod.migrate(root)


def test_something_that_is_not_a_git_checkout_is_refused(tmp_path):
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    with pytest.raises(migrate_mod.MigrateError, match="git"):
        migrate_mod.migrate(plain)


def test_a_missing_root_is_refused(tmp_path):
    with pytest.raises(migrate_mod.MigrateError):
        migrate_mod.migrate(tmp_path / "nothing-here")


def test_an_existing_versions_directory_that_is_not_ours_is_refused(tmp_path):
    """`versions/` beside the install may be someone else's. Writing into it
    would put AQG's tree inside a directory with another owner."""
    root = _checkout(tmp_path)
    (tmp_path / "versions").mkdir()
    (tmp_path / "versions" / "something-else").mkdir()
    with pytest.raises(migrate_mod.MigrateError, match="versions"):
        migrate_mod.migrate(root)


def test_a_checkout_that_is_itself_a_git_worktree_is_refused(tmp_path):
    """A worktree's `.git` is a file pointing at another repository's gitdir.
    Moving it breaks the link in a way that is not obvious until a fetch fails.
    """
    main = _checkout(tmp_path, name="main-repo")
    linked = tmp_path / "linked"
    subprocess.run(
        ["git", "-C", str(main), "worktree", "add", "--detach", str(linked), "HEAD"],
        capture_output=True, check=True,
    )
    with pytest.raises(migrate_mod.MigrateError, match="worktree"):
        migrate_mod.migrate(linked)


# --- failure leaves the install where it was ---------------------------------------------


def test_a_failure_after_the_move_puts_the_tree_back(tmp_path, monkeypatch):
    """There is a window where the root does not exist: no filesystem can turn a
    directory into a symlink atomically. If the second step fails, the first is
    undone, because leaving a user with no AQG_ROOT at all is the worst outcome
    available.
    """
    root = _checkout(tmp_path)
    # The failure has to happen AFTER the tree has moved, which is the only
    # place the undo matters. An earlier version made `os.symlink` fail — and
    # since the link is now prepared BEFORE the window, nothing had moved yet
    # and this passed without the undo existing at all.
    real_rename = os.rename
    calls = []

    def fail_second_rename(src, dst, **kwargs):
        calls.append(src)
        if len(calls) == 2:
            raise OSError("interrupted with the tree already moved")
        return real_rename(src, dst, **kwargs)

    monkeypatch.setattr(os, "rename", fail_second_rename)
    with pytest.raises(migrate_mod.MigrateError):
        migrate_mod.migrate(root)
    monkeypatch.setattr(os, "rename", real_rename)
    # Three: the tree moved, the link rename failed, and the undo moved it back.
    assert len(calls) == 3, f"the failure did not land inside the window: {len(calls)} renames"

    assert root.is_dir() and not root.is_symlink(), "the install was left missing"
    assert (root / "VERSION").read_text() == "0.16.0\n"
    assert not (tmp_path / "versions").exists() or not any(
        (tmp_path / "versions").iterdir()
    )


def test_a_dry_run_changes_nothing(tmp_path):
    """A one-way change to somebody's install deserves a way to see it first."""
    root = _checkout(tmp_path)
    plan = migrate_mod.migrate(root, dry_run=True)
    assert plan.target.name == _head(root)
    assert root.is_dir() and not root.is_symlink()
    assert not (tmp_path / "versions").exists()


def test_a_symlink_pointing_somewhere_else_is_not_called_already_migrated(tmp_path):
    """"Already a symlink" is not the same as "already on the layout".

    An install symlinked to a checkout elsewhere — a developer pointing AQG_ROOT
    at a working copy is the obvious case — would have been reported as migrated
    and left alone, after which the engine would stage into that copy's parent.
    """
    elsewhere = _checkout(tmp_path, name="somewhere-else")
    root = tmp_path / "aqg"
    os.symlink(elsewhere, root)
    with pytest.raises(migrate_mod.MigrateError, match="not into a versions"):
        migrate_mod.migrate(root)


def test_a_symlink_already_into_versions_is_reported_as_done(tmp_path):
    root = _checkout(tmp_path)
    first = migrate_mod.migrate(root)
    again = migrate_mod.migrate(root)
    assert again.already is True and again.target == first.target


# --- the user-facing entry point ------------------------------------------------------


UPGRADE = Path(__file__).resolve().parents[2] / "scripts" / "upgrade.sh"


def test_upgrade_offers_the_migration():
    """A module with no caller is a module nobody can run.

    The migration is one-way, so it is behind an explicit flag rather than
    happening as a side effect of an ordinary upgrade.
    """
    body = UPGRADE.read_text(encoding="utf-8")
    assert "--migrate" in body
    assert "from scripts.aqg_update import migrate" in body, (
        "the flag exists but nothing calls the migration"
    )


def test_upgrade_describes_the_migration_in_its_help():
    """One-way changes to somebody's install belong in `--help`, not only in a
    commit message."""
    done = subprocess.run(
        ["bash", str(UPGRADE), "--help"], capture_output=True, timeout=30
    )
    assert done.returncode == 0
    assert "--migrate" in done.stdout.decode()


def test_upgrade_migrate_is_a_dry_run_without_confirmation(tmp_path):
    """It must show what it would do before it moves anything, because the
    window where AQG_ROOT does not exist is not one to enter by accident."""
    body = UPGRADE.read_text(encoding="utf-8")
    assert "--dry-run" in body or "dry_run" in body


def test_an_install_path_with_a_quote_in_it_still_works(tmp_path):
    """The install path used to be interpolated into an inline python program.

    An apostrophe — `/Users/example/o'brien-checkout` is not exotic — broke it outright, and a
    directory named to close the quote and open a statement would have RUN,
    which makes the checkout's own location an injection point. The path goes
    through argv now.
    """
    awkward = tmp_path / "it's a dir"
    install = awkward / "install"
    (install / "scripts").mkdir(parents=True)
    for name in ("aqg_update",):
        shutil.copytree(UPGRADE.parent / name, install / "scripts" / name)
    shutil.copy2(UPGRADE, install / "scripts")
    (install / "VERSION").write_text("1\n", encoding="utf-8")
    for args in (
        ("init", "-q", "-b", "main"), ("config", "user.email", "t@e.com"),
        ("config", "user.name", "T"), ("add", "-A"), ("commit", "-qm", "x"),
    ):
        subprocess.run(["git", "-C", str(install), *args], capture_output=True, check=True)

    done = subprocess.run(
        ["bash", str(install / "scripts" / "upgrade.sh"), "--migrate"],
        input=b"no\n", capture_output=True, timeout=60,
    )
    output = (done.stdout + done.stderr).decode()
    assert "SyntaxError" not in output, output[-400:]
    assert "would move" in output or "cannot migrate" in output, output[-400:]


# =============================================================================
# Added after audit aud_0XJK4PX_VGVROrKK. See .aqg/adjudication/.
# =============================================================================


def test_a_signpost_is_left_where_the_user_will_look(tmp_path, monkeypatch):
    """The recovery tool lives INSIDE the directory that vanishes.

    If the process is killed between the two renames, AQG_ROOT does not exist —
    and `upgrade.sh`, which is the only thing that could put it back, is inside
    it. So the note has to be beside the root, not in it, and it has to carry
    the literal command rather than a description of one.
    """
    root = _checkout(tmp_path)
    seen = {}
    real_rename = os.rename

    def capture_then_fail(src, dst, **kwargs):
        # After the tree has moved: exactly the state a kill would leave.
        real_rename(src, dst, **kwargs)
        note = tmp_path / migrate_mod.SIGNPOST_FILENAME
        seen["exists"] = note.exists()
        seen["text"] = note.read_text(encoding="utf-8") if note.exists() else ""
        raise OSError("killed here")

    monkeypatch.setattr(os, "rename", capture_then_fail)
    with pytest.raises(migrate_mod.MigrateError):
        migrate_mod.migrate(root)
    monkeypatch.setattr(os, "rename", real_rename)

    assert seen.get("exists") is True, "no signpost existed while the root was gone"
    assert "mv " in seen["text"]
    assert str(root) in seen["text"]


def test_the_signpost_is_removed_when_the_migration_succeeds(tmp_path):
    """A note that outlives the problem it describes is a false alarm somebody
    acts on."""
    root = _checkout(tmp_path)
    migrate_mod.migrate(root)
    assert not (tmp_path / migrate_mod.SIGNPOST_FILENAME).exists()


def test_the_link_is_made_before_the_window_not_inside_it(tmp_path, monkeypatch):
    """Creating the symlink needs an inode; failing to allocate one is a way to
    fail INSIDE the window, where the root does not exist. It is created under a
    temporary name first, so the only syscalls between the tree moving and the
    root existing again are two renames in one directory.
    """
    root = _checkout(tmp_path)
    order = []
    real_symlink, real_rename = os.symlink, os.rename
    monkeypatch.setattr(os, "symlink", lambda s, d, **k: (order.append("symlink"), real_symlink(s, d, **k))[1])
    monkeypatch.setattr(os, "rename", lambda s, d, **k: (order.append("rename"), real_rename(s, d, **k))[1])
    migrate_mod.migrate(root)
    assert order[0] == "symlink", f"the link was not made first: {order}"


def test_an_unwritable_parent_is_refused_before_anything_moves(tmp_path):
    """The rename needs write on the PARENT, not on the root. Learning that
    after the confirmation, with the tree already moved, is the wrong order.
    """
    if os.name == "nt":
        pytest.skip("POSIX mode bits do not make a Windows directory unwritable")
    if os.geteuid() == 0:
        pytest.skip("root can write regardless of mode")
    root = _checkout(tmp_path)
    tmp_path.chmod(0o555)
    try:
        with pytest.raises(migrate_mod.MigrateError, match="not writable"):
            migrate_mod.migrate(root, dry_run=True)
    finally:
        tmp_path.chmod(0o755)


def test_the_migration_runs_under_the_install_lock(tmp_path, monkeypatch):
    """A background update check starts at every session start, and this changes
    the very layout that check inspects. They have to be serialised."""
    root = _checkout(tmp_path)
    from scripts.aqg_update import lock as lock_mod

    taken = []
    real = lock_mod.install_lock

    def observing(**kwargs):
        taken.append(True)
        return real(**kwargs)

    monkeypatch.setattr(migrate_mod.lock, "install_lock", observing)
    migrate_mod.migrate(root)
    assert taken, "the migration ran without taking the install lock"


def test_cleanliness_is_rechecked_under_the_lock(tmp_path, monkeypatch):
    """`git status` then `rename` is a check and a use with a gap between them.
    The gap is now inside the lock, and the check runs again once it is held."""
    root = _checkout(tmp_path)
    calls = []
    real_status = migrate_mod._require_clean_checkout

    def counting(target):
        calls.append(target)
        return real_status(target)

    monkeypatch.setattr(migrate_mod, "_require_clean_checkout", counting)
    migrate_mod.migrate(root)
    assert len(calls) >= 2, "the tree was checked once and used later"
