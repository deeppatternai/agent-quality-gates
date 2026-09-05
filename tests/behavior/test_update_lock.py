"""Behavior contracts for the managed-update admission lock.

One apply at a time, across processes (docs/UPDATE_ARCHITECTURE.md §5.2, §5.4).
The rule this file exists to pin, borrowed from DE's `update_coordination`:
**file existence is never authority.** A lock implemented as "is the file there?"
breaks in the two ways that matter most — a crashed updater leaves a file that
blocks every future run, and a stale file invites someone to delete it, which
silently permits two concurrent applies.

So the lock is an OS advisory lock held on an open handle. The kernel releases it
when the holding process dies, whatever killed it, and a leftover file is inert.

The other half of the contract is what a hook must do: a SessionStart hook that
finds the lock held has to no-op immediately. It must never wait — blocking there
would stall the user's session behind an update they did not ask for.
"""

from __future__ import annotations

import os
import signal
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from scripts.aqg_update import lock as lock_mod

REPO_ROOT = Path(__file__).resolve().parents[2]


def _holder_process(lock_path: Path, ready: Path) -> subprocess.Popen:
    """Spawn a process that takes the lock and holds it until killed."""
    program = textwrap.dedent(
        f"""
        import pathlib, sys, time
        sys.path.insert(0, {str(REPO_ROOT)!r})
        from scripts.aqg_update import lock as lock_mod
        with lock_mod.install_lock(path=pathlib.Path({str(lock_path)!r})):
            pathlib.Path({str(ready)!r}).write_text("held")
            time.sleep(60)
        """
    )
    proc = subprocess.Popen([sys.executable, "-c", program])
    deadline = time.monotonic() + 15
    while not ready.exists():
        if time.monotonic() > deadline:  # pragma: no cover - CI hang guard
            proc.kill()
            raise AssertionError("holder process never acquired the lock")
        if proc.poll() is not None:
            raise AssertionError(f"holder exited early: {proc.returncode}")
        time.sleep(0.01)
    return proc


# --- one at a time ------------------------------------------------------------


def test_a_second_acquire_is_refused_while_the_first_is_held(tmp_path):
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target):
        with pytest.raises(lock_mod.LockBusy):
            with lock_mod.install_lock(path=target):
                pass  # pragma: no cover - the acquire must not succeed


def test_the_lock_is_available_again_after_release(tmp_path):
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target):
        pass
    with lock_mod.install_lock(path=target):
        pass


def test_a_failure_inside_the_lock_still_releases_it(tmp_path):
    """An apply that raises must not leave the machine unable to update."""
    target = tmp_path / "update.lock"
    with pytest.raises(ZeroDivisionError):
        with lock_mod.install_lock(path=target):
            1 / 0
    with lock_mod.install_lock(path=target):
        pass


def test_another_process_is_refused_while_one_holds_it(tmp_path):
    target = tmp_path / "update.lock"
    ready = tmp_path / "ready"
    proc = _holder_process(target, ready)
    try:
        with pytest.raises(lock_mod.LockBusy):
            with lock_mod.install_lock(path=target):
                pass  # pragma: no cover
    finally:
        proc.kill()
        proc.wait(timeout=15)


# --- existence is never authority ---------------------------------------------


def test_a_leftover_lock_file_does_not_block(tmp_path):
    """The whole reason this is an OS lock and not a sentinel file: a crashed
    updater must not wedge every future run."""
    target = tmp_path / "update.lock"
    target.write_bytes(b"")
    with lock_mod.install_lock(path=target):
        pass


def test_the_kernel_releases_the_lock_when_the_holder_is_killed(tmp_path):
    """Not a graceful shutdown — SIGKILL, the case a cleanup handler cannot
    cover. This is the property a sentinel file cannot provide at all."""
    target = tmp_path / "update.lock"
    ready = tmp_path / "ready"
    proc = _holder_process(target, ready)
    proc.send_signal(signal.SIGKILL)
    proc.wait(timeout=15)
    with lock_mod.install_lock(path=target):
        pass


# --- what a hook needs --------------------------------------------------------


def test_is_locked_reports_the_state_without_taking_the_lock(tmp_path):
    """A hook asks this to decide whether to no-op. Asking must not itself
    acquire, or the check would lock out the real updater."""
    target = tmp_path / "update.lock"
    assert lock_mod.is_locked(path=target) is False
    with lock_mod.install_lock(path=target):
        assert lock_mod.is_locked(path=target) is True
        assert lock_mod.is_locked(path=target) is True
    assert lock_mod.is_locked(path=target) is False


def test_is_locked_does_not_wait_for_a_holder_to_finish(tmp_path):
    """Blocking here would stall a user's session behind an update.

    The functional half is the real proof: the holder sleeps for a minute, so a
    probe that waited could not return at all. The bound is a backstop.
    """
    target = tmp_path / "update.lock"
    ready = tmp_path / "ready"
    proc = _holder_process(target, ready)
    try:
        started = time.monotonic()
        assert lock_mod.is_locked(path=target) is True
        assert time.monotonic() - started < 1.0
    finally:
        proc.kill()
        proc.wait(timeout=15)


# --- the lock file itself ------------------------------------------------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_the_lock_file_is_owner_only(tmp_path):
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_a_symlinked_lock_path_is_refused(tmp_path):
    """Following a link would let anything writable redirect where the lock is
    taken, so two updaters could each hold a different 'lock'."""
    elsewhere = tmp_path / "elsewhere.lock"
    elsewhere.write_bytes(b"")
    target = tmp_path / "update.lock"
    target.symlink_to(elsewhere)
    with pytest.raises(lock_mod.LockError, match="link"):
        with lock_mod.install_lock(path=target):
            pass  # pragma: no cover


def test_a_directory_where_the_lock_belongs_fails_closed(tmp_path):
    target = tmp_path / "update.lock"
    target.mkdir()
    with pytest.raises(lock_mod.LockError):
        with lock_mod.install_lock(path=target):
            pass  # pragma: no cover


def test_the_default_lock_lives_beside_install_state(tmp_path, monkeypatch):
    """Same root as the state file, and outside AQG_ROOT for the same reason:
    the version swap replaces that tree wholesale."""
    monkeypatch.setenv("AQG_STATE_ROOT", str(tmp_path / "state"))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    assert lock_mod.lock_path().parent == tmp_path / "state"


# --- fixes from audit aud_clmuZ890iK2gS5UT ------------------------------------


def test_a_holder_notices_its_lock_file_was_deleted(tmp_path):
    """The classic file-lock bypass, and it needs no hostile process: an
    operator tidying up a file this module used to call "inert" is enough.

    The acquiring side cannot defend against it — a second acquirer creates a
    fresh inode at the same path and everything it can check is consistent. What
    the holder can do is notice its own file is gone and stop, which is what an
    apply must do before each irreversible step.
    """
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target) as held:
        held.still_held()
        target.unlink()
        with pytest.raises(lock_mod.LockLost, match="deleted or replaced"):
            held.still_held()


def test_a_holder_notices_its_lock_file_was_replaced(tmp_path):
    """Replacement reports the same way as deletion, and that is correct rather
    than imprecise: on POSIX a rename over the path unlinks the held inode too,
    so there is one observable condition, not two."""
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target) as held:
        target.unlink()
        target.write_bytes(b"")
        with pytest.raises(lock_mod.LockLost, match="deleted or replaced"):
            held.still_held()


def test_a_second_acquirer_after_a_delete_is_the_documented_residual(tmp_path):
    """Stated as a test so the limit is recorded rather than implied: once the
    file is gone, a second acquire DOES succeed. That is why the holder-side
    check above exists."""
    target = tmp_path / "update.lock"
    with lock_mod.install_lock(path=target) as held:
        target.unlink()
        with lock_mod.install_lock(path=target):
            pass
        with pytest.raises(lock_mod.LockLost):
            held.still_held()


def test_a_broken_state_root_does_not_escape_is_locked(monkeypatch, tmp_path):
    """`lock_path()` resolves the state root, which fails closed. A hook must
    still get an answer rather than an exception out of a SessionStart path."""
    aqg_root = tmp_path / "checkout"
    aqg_root.mkdir()
    monkeypatch.setenv("AQG_ROOT", str(aqg_root))
    monkeypatch.setenv("AQG_STATE_ROOT", str(aqg_root / "inside"))
    assert lock_mod.is_locked() is False


def test_a_broken_state_root_fails_closed_for_the_apply_path(monkeypatch, tmp_path):
    aqg_root = tmp_path / "checkout"
    aqg_root.mkdir()
    monkeypatch.setenv("AQG_ROOT", str(aqg_root))
    monkeypatch.setenv("AQG_STATE_ROOT", str(aqg_root / "inside"))
    with pytest.raises(lock_mod.LockError):
        with lock_mod.install_lock():
            pass  # pragma: no cover


@pytest.mark.parametrize("kind", ["symlink", "directory"])
def test_a_structurally_broken_lock_reads_as_not_locked(tmp_path, kind):
    """The other half of the deliberate asymmetry, which had no coverage: no
    apply can be holding a path that cannot hold a lock at all."""
    target = tmp_path / "update.lock"
    if kind == "symlink":
        other = tmp_path / "other.lock"
        other.write_bytes(b"")
        target.symlink_to(other)
    else:
        target.mkdir()
    assert lock_mod.is_locked(path=target) is False
    with pytest.raises(lock_mod.LockError):
        with lock_mod.install_lock(path=target):
            pass  # pragma: no cover


def test_an_environmental_failure_reads_as_locked(tmp_path, monkeypatch):
    """Opposite direction from a structural one: "I could not check" is not
    "nobody is running". A hook standing down for one run is harmless; missing a
    live apply is not."""
    target = tmp_path / "update.lock"

    def _refuse(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(lock_mod.os, "open", _refuse)
    assert lock_mod.is_locked(path=target) is True


def test_install_lock_retries_once_before_declaring_busy(tmp_path, monkeypatch):
    """A hook's probe genuinely takes the lock while it is free, so an updater
    starting at that instant would see a spurious busy and skip silently."""
    target = tmp_path / "update.lock"
    calls = {"n": 0}
    real_try = lock_mod._try_lock

    def _busy_once(fd):
        calls["n"] += 1
        if calls["n"] == 1:
            return False
        return real_try(fd)

    monkeypatch.setattr(lock_mod, "_try_lock", _busy_once)
    with lock_mod.install_lock(path=target):
        pass
    assert calls["n"] == 2


def test_a_pre_existing_world_readable_lock_is_made_private(tmp_path):
    """The private mode used to apply only on creation, and this design reuses
    a pre-existing file by design."""
    target = tmp_path / "update.lock"
    target.write_bytes(b"")
    target.chmod(0o644)
    with lock_mod.install_lock(path=target):
        assert stat.S_IMODE(target.stat().st_mode) == 0o600
