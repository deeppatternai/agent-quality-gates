#!/usr/bin/env python3
"""AQG code construction pre-commit hook installer.

Per sketch a1 audit D4 fix: install via git core.hooksPath (per-repo opt-in),
not by writing to .git/hooks/ directly. Reversible via --uninstall.

Per audit gemini #1 fix (option c): hook is gated by AQG_AGENT env var;
install-time setup is harmless to humans (silent pass unless env set).

Exit codes:
  0  OK
  1  generic fail
  2  usage error
  3  not a git repo
  4  hook already installed (use --force)
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

try:
    from _aqg_backup import (
        BackupError,
        BackupSession,
        git_config_entries,
        list_runs,
        remove_run,
    )
except ModuleNotFoundError:  # pragma: no cover - packaged import path
    from scripts._aqg_backup import (
        BackupError,
        BackupSession,
        git_config_entries,
        list_runs,
        remove_run,
    )


# ===== Exit codes =====
EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2
EXIT_NOT_GIT = 3
EXIT_ALREADY_INSTALLED = 4

# ===== Constants =====
HOOK_DIR_REL = ".aqg/hooks"
HOOK_FILE = "pre-commit"
HOOK_SOURCE_REL = "agent-packs/claude-code/hooks/pre_commit_construction.sh"
TIMEOUT_GIT_S = 5

# Centralized backup store (scripts/_aqg_backup.py). The prior core.hooksPath
# value that ``--force`` overwrites is a git-config *value*, not a file — it is
# stashed via ``BackupSession.backup_value`` under this client id, project scope,
# so ``--uninstall`` can restore it from the central store instead of an in-tree
# ``.aqg/.hookspath_backup``.
CLIENT_ID = "construction-hook"
INSTALLER_NAME = "install_aqg_construction_hook.py"
HOOKSPATH_KEY = "core.hooksPath"


def _safe_run(
    cmd: list[str], cwd: Optional[Path] = None, timeout: int = TIMEOUT_GIT_S
) -> tuple[int, str]:
    """Run subprocess with timeout + non-interactive env, return (rc, stdout)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
        return proc.returncode, proc.stdout.strip()
    except subprocess.TimeoutExpired:
        return 124, "timeout"
    except FileNotFoundError:
        return 127, "command not found"


def _resolve_aqg_root() -> Optional[Path]:
    """Walk up from this script to find AQG repo root (sentinel: VERSION + scripts/)."""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "VERSION").is_file() and (p / "scripts").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    # Fallback: AQG_ROOT env var
    env_root = os.environ.get("AQG_ROOT")
    if env_root:
        candidate = Path(env_root).resolve()
        if (candidate / "VERSION").is_file():
            return candidate
    return None


class _SymlinkDestError(Exception):
    """A write destination is a symlink; refuse to follow it (--force clobber guard)."""


def _resolve_git_top(target: Path) -> Optional[Path]:
    """Resolve *target*'s work-tree top-level, or None if it is not a work tree.

    Fixes two convergent installer findings:
      * C7 (gpt-5.5 #7) — install() wrote ``.aqg/`` under the *invocation*
        directory and set a relative ``core.hooksPath``, but git resolves a
        relative ``core.hooksPath`` against the work-tree root. Run from a
        subdirectory, the hook landed at ``<subdir>/.aqg/hooks`` while git
        looked under ``<root>/.aqg/hooks`` — so the hook never fired. Anchoring
        every ``.aqg`` operation at ``--show-toplevel`` keeps them in sync.
      * C9 (gpt-5.5 #9) — ``_is_git_repo`` trusted ``rc == 0`` from
        ``--is-inside-work-tree``, but a bare repo / a path inside ``.git/``
        exits 0 while printing ``false``. Require the literal ``true`` *and* a
        real top-level directory.
    """
    rc_wt, wt = _safe_run(
        ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"]
    )
    if rc_wt != 0 or wt.strip() != "true":
        return None
    rc_top, top = _safe_run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"]
    )
    top = top.strip()
    if rc_top != 0 or not top:
        return None
    top_path = Path(top)
    if not top_path.is_dir():
        return None
    return top_path.resolve()


def _atomic_write(dest: Path, data: bytes, *, mode: int) -> None:
    """Atomically write *data* to *dest* with *mode*, refusing a symlink at dest.

    Fixes C8 (gpt-5.5 #8 + gemini #7) and C11 (gemini #6): the hook was
    installed via ``shutil.copy`` + ``chmod`` — non-atomic (a concurrent commit
    could exec a half-written hook) and it followed a symlink at the
    destination, so ``--force`` could clobber an out-of-tree target — while the
    ``core.hooksPath`` backup used a bare ``write_text`` (a crash mid-write lost
    the original config, breaking ``--uninstall`` restore). Write to a temp file
    in the same directory, fsync, ``fchmod`` the *open fd*, then ``os.replace``
    (an atomic rename that replaces the directory entry rather than writing
    through a symlink).

    The ``fchmod``-on-fd (vs a path-based ``os.chmod(tmp, ...)``) closes the
    TOCTOU window (gemini fix-review #3) where a concurrent process could swap
    the temp path for a symlink between close and chmod. The *parent* directory
    must already be proven in-tree by the caller (see ``_contained``) — this
    helper only guards the final component.
    """
    if dest.is_symlink():
        raise _SymlinkDestError(dest)
    fd, tmp = tempfile.mkstemp(prefix=".aqg-tmp-", dir=str(dest.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
            os.fchmod(fh.fileno(), mode)
        os.replace(tmp, dest)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _contained(path: Path, root: Path) -> bool:
    """True if *path*, fully resolved (following symlinks), stays within *root*.

    Mirrors ``aqg_construction_check._ledger_path_within_bounds``. Closes the
    symlinked-parent bypass (gpt-5.5 + gemini fix-review #1, convergent): the
    per-destination ``is_symlink`` guard only covered the final component, so a
    checked-in ``.aqg`` or ``.aqg/hooks`` symlinking out of tree would let
    ``mkdir``/``mkstemp``/``os.replace`` write outside the repo. A symlinked
    parent resolves outside *root* and is rejected here, before any mkdir.
    """
    try:
        resolved = path.resolve()
        root_resolved = root.resolve()
    except OSError:
        return False
    try:
        try:
            return resolved.is_relative_to(root_resolved)
        except AttributeError:  # Python < 3.9 fallback
            resolved.relative_to(root_resolved)
            return True
    except (ValueError, OSError):
        return False


HOOKSPATH_BACKUP_REL = ".aqg/.hookspath_backup"


def _new_session(target_top: Path, aqg_root: Optional[Path], *, origin: str = "install") -> BackupSession:
    """A project-scoped central backup session for *target_top*'s core.hooksPath."""
    return BackupSession(
        CLIENT_ID,
        target_top,
        scope="project",
        project_root=target_top,
        installer=INSTALLER_NAME,
        aqg_root=aqg_root,
        origin=origin,
    )


def _read_hookspath_no_follow(path: Path) -> Optional[str]:
    """Read a legacy ``.hookspath_backup`` value without following a symlink.

    Closes the TOCTOU window (audit V3f4): between an ``is_symlink()`` precheck
    and a plain ``read_text`` an attacker could swap the path for a symlink and
    have its target read into ``core.hooksPath``. Defenses, in order: an
    ``lstat``/``S_ISLNK`` precheck rejects a symlink on every platform (the only
    guard available where ``O_NOFOLLOW`` is absent, e.g. Windows); ``O_NOFOLLOW``
    additionally makes the *open itself* refuse a final-component symlink
    atomically on POSIX; and an ``fstat`` confirms the opened fd is a *regular*
    file before reading. Only trailing line-ending framing (``\r``/``\n``, incl.
    a Windows CRLF) is stripped; the value is otherwise returned verbatim so a
    restore is byte-preserving (V1f5). Returns ``None`` on any error / symlink /
    non-regular file / undecodable content.
    """
    try:
        if stat.S_ISLNK(os.lstat(str(path)).st_mode):
            return None
    except OSError:
        return None
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        fd = os.open(str(path), flags)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None
        data = os.read(fd, 65536)
    except OSError:
        return None
    finally:
        os.close(fd)
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return text.rstrip("\r\n")


def _hookspath_runs(target_top: Path, aqg_root: Optional[Path]):
    """Snapshot the repo's central runs once, split by origin (each newest-first).

    Returns ``(install_runs, legacy_runs)``. Taking a single snapshot and using it
    for *both* selection and consumption closes the select→consume race: a run
    written by a concurrent ``install`` *after* this snapshot appears in neither
    list, so it is neither restored from nor deleted (external audit round-2
    V4f1/V1f3/V2f3). Splitting by origin also encodes precedence — an ``install``
    run (the value present immediately before the last install) always outranks a
    ``legacy-inplace`` migrated run *regardless of timestamp*, so an
    uninstall-time / same-second migration can never shadow it (audit A/B); a
    migrated run is used only when no install run exists.
    """
    installs = list_runs(
        CLIENT_ID,
        scope="project",
        project_root=target_top,
        aqg_root=aqg_root,
        origins=("install",),
    )
    legacies = list_runs(
        CLIENT_ID,
        scope="project",
        project_root=target_top,
        aqg_root=aqg_root,
        origins=("legacy-inplace",),
    )
    return installs, legacies


def _hookspath_value_from_run(run) -> Optional[str]:
    """The recorded ``core.hooksPath`` from *run* (verbatim), or ``None`` if the
    run is unreadable / has no such entry — the caller treats ``None`` on a
    *selected* run as a hard error rather than silently unsetting (audit H)."""
    try:
        for entry in git_config_entries(run):
            if entry.get("key") == HOOKSPATH_KEY:
                return entry.get("value", "")
    except (OSError, BackupError, KeyError, ValueError):
        return None
    return None


def _consume_hookspath_runs(runs, target_top: Path) -> None:
    """Delete exactly the runs in *runs* (the uninstall snapshot), warning on any
    that could not be removed.

    Consuming only the snapshot — not a fresh listing — means a concurrent
    ``install``'s newer run is never deleted (round-2 V4f1). Consumption is the
    caller's responsibility to invoke *only* on the owned path: an uninstall that
    restored nothing (a value the user re-pointed elsewhere) must leave the backup
    slot intact rather than destroy the last record of the pre-install value
    (round-2 V1f2/V4f3)."""
    failed = [run for run in runs if not remove_run(run)]
    if failed:
        print(
            f"WARN: could not remove {len(failed)} stale backup run(s) under "
            f"{target_top}; they will be pruned by retention on a later install",
            file=sys.stderr,
        )


def _migrate_legacy_hookspath_backup(target_top: Path, aqg_root: Optional[Path]) -> None:
    """Fold a legacy in-tree ``.aqg/.hookspath_backup`` into the central store.

    Older installs saved the overwritten ``core.hooksPath`` to an in-tree file;
    the value now lives in the central store as a git-config entry. Re-record the
    legacy value via :meth:`BackupSession.backup_value` (a generic file-copy
    migration would not be restorable *as config*), then delete the in-tree copy
    (migrate-then-delete). Idempotent: a no-op once the legacy file is gone.

    Recorded with ``origin="legacy-inplace"``, which :func:`_hookspath_runs`
    ranks *below* an ``install`` run — so a migrated value can only be restored
    when no install run exists, never shadowing a real capture.

    A symlinked / escaping legacy path is left untouched — the read itself also
    refuses to follow a symlink (see :func:`_read_hookspath_no_follow`).
    """
    legacy = target_top / HOOKSPATH_BACKUP_REL
    if legacy.is_symlink() or not legacy.is_file():
        return
    if not _contained(legacy, target_top):
        return
    value = _read_hookspath_no_follow(legacy)
    if value is None:
        return
    if not value.strip():
        try:
            legacy.unlink()
        except OSError:
            pass
        return

    session = _new_session(target_top, aqg_root, origin="legacy-inplace")
    session._run_prefix = "migrated-"
    try:
        session.backup_value(HOOKSPATH_KEY, value, repo=target_top)
        session.close()
    except (BackupError, OSError) as exc:
        session.discard()
        print(
            f"WARN: could not migrate legacy core.hooksPath backup {legacy}: {exc}",
            file=sys.stderr,
        )
        return
    try:
        legacy.unlink()
    except OSError as exc:
        print(
            f"WARN: migrated core.hooksPath backup but could not remove {legacy}: {exc}",
            file=sys.stderr,
        )


def install(target: Path, aqg_root: Path, force: bool = False) -> int:
    """Install hook + set core.hooksPath; idempotent if --force.

    Per PR-B audit C3 (gpt #4 + gemini #2 convergent): check for existing
    core.hooksPath before overwriting (e.g., husky / pre-commit framework).
    On --force, save existing path to .aqg/.hookspath_backup for restore on uninstall.
    """
    target_top = _resolve_git_top(target)
    if target_top is None:
        print(f"ERROR: {target} is not inside a git work tree", file=sys.stderr)
        return EXIT_NOT_GIT

    hooks_dir = target_top / HOOK_DIR_REL
    hook_path = hooks_dir / HOOK_FILE
    if not _contained(hook_path, target_top):
        print(
            f"ERROR: refusing to install through a path that escapes the repo "
            f"(symlinked parent?): {hook_path}",
            file=sys.stderr,
        )
        return EXIT_GENERIC

    if hook_path.exists() and not force:
        print(
            f"ERROR: hook already installed at {hook_path}; use --force to overwrite",
            file=sys.stderr,
        )
        return EXIT_ALREADY_INSTALLED

    # PR-B audit C3: refuse to silently override a pre-existing hook framework.
    # The *gate* reads the EFFECTIVE value (any scope) so a globally-configured
    # husky / pre-commit still trips the --force guard.
    rc_eff, existing_eff = _safe_run(
        ["git", "-C", str(target_top), "config", "--get", "core.hooksPath"]
    )
    has_existing_eff = rc_eff == 0 and existing_eff and existing_eff != HOOK_DIR_REL
    if has_existing_eff and not force:
        print(
            f"ERROR: core.hooksPath already set to '{existing_eff}' (non-AQG; "
            "e.g., husky / pre-commit framework). Use --force to override.",
            file=sys.stderr,
        )
        print(
            "  (--force will stash the current value in the central AQG backup "
            "store and restore it on --uninstall)",
            file=sys.stderr,
        )
        return EXIT_GENERIC

    source = aqg_root / HOOK_SOURCE_REL
    if not source.exists():
        print(f"ERROR: hook source missing at {source}", file=sys.stderr)
        return EXIT_GENERIC
    try:
        source_bytes = source.read_bytes()
    except OSError as exc:
        print(f"ERROR: cannot read hook source {source}: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    hooks_dir.mkdir(parents=True, exist_ok=True)

    # Fold any legacy in-tree .hookspath_backup into the central store first
    # (migrate-then-delete). It is recorded as a `legacy-inplace` run, which
    # `uninstall` ranks *below* the `install` run written next — so a stale in-tree
    # copy can only be restored when no install run exists, never shadowing the
    # value stashed here.
    _migrate_legacy_hookspath_backup(target_top, aqg_root)

    # Back up the value we are about to overwrite, reading the LOCAL scope only —
    # install mutates the local core.hooksPath, so the local value is exactly what
    # uninstall must be able to restore. Reading the effective value here would
    # capture a global husky path we never overwrite and then wrongly re-write it
    # into the local scope on uninstall (a shadow that never existed): captured and
    # restored state must be the SAME scope (round-2 V2f8). A global value needs no
    # backup — install shadows it locally, uninstall unsets local, the global
    # re-emerges untouched.
    rc_local, existing_local = _safe_run(
        ["git", "-C", str(target_top), "config", "--local", "--get", "core.hooksPath"]
    )
    has_local_backup = rc_local == 0 and existing_local and existing_local != HOOK_DIR_REL

    # Make the backup DURABLE (manifest written) *before* mutating the hook / git
    # config, so a later failure can never leave core.hooksPath pointing at AQG with
    # no restorable backup (audit E: the old close()-after-mutation window). A plain
    # install (no local value) writes no run — uninstall then unsets local, and a
    # still-valid earlier backup is left intact rather than shadowed by an empty
    # sentinel. On any later failure the closed run is discarded so a crashed
    # install strands no value for --uninstall to restore.
    session = None
    if has_local_backup:
        session = _new_session(target_top, aqg_root)
        try:
            session.backup_value(HOOKSPATH_KEY, existing_local, repo=target_top)
            session.close()
        except (BackupError, OSError) as exc:
            session.discard()
            print(f"ERROR: central backup failed: {exc}", file=sys.stderr)
            return EXIT_GENERIC
        print(
            f"OK: stashed existing local core.hooksPath '{existing_local}' "
            "in the central AQG backup store"
        )

    try:
        _atomic_write(hook_path, source_bytes, mode=0o755)
    except _SymlinkDestError:
        if session is not None:
            session.discard()
        print(
            f"ERROR: refusing to install the hook through a symlink: {hook_path}",
            file=sys.stderr,
        )
        return EXIT_GENERIC
    except OSError as exc:
        if session is not None:
            session.discard()
        print(f"ERROR: failed to write the hook {hook_path}: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    rc, out = _safe_run(
        ["git", "-C", str(target_top), "config", "--local", "core.hooksPath", HOOK_DIR_REL]
    )
    if rc != 0:
        if session is not None:
            session.discard()
        print(f"ERROR: failed to set core.hooksPath: {out}", file=sys.stderr)
        return EXIT_GENERIC

    if session is not None:
        run_dir = session.run_dir
        session.gc()
        if run_dir is not None:
            print(f"OK: backup: {run_dir}")

    print(f"OK: AQG construction hook installed at {hook_path}")
    print(f"OK: git config core.hooksPath = {HOOK_DIR_REL}")
    print()
    print("Next steps:")
    print("  1. Add `.aqg/` to .gitignore (so workspace state isn't committed)")
    print("  2. Set AQG_AGENT env var when committing as agent:")
    print("       AQG_AGENT=codex git commit -m '...'")
    print("       AQG_AGENT=claude git commit -m '...'")
    print("  3. Without AQG_AGENT set, hook silent-passes (humans transparent)")
    return EXIT_OK


def uninstall(target: Path, aqg_root: Optional[Path] = None) -> int:
    """Remove hook + restore prior core.hooksPath (or unset if no backup).

    Per PR-B audit C3: restore the overwritten core.hooksPath. The value lives in
    the central AQG backup store (git-config entry); any legacy in-tree copy is
    migrated in first (migrate-then-delete). Ordering and guards harden the
    restore (external audit aud_XQ2a3NORHvh0281E):

    * config is reverted **before** the hook is removed, and a failed revert
      aborts non-zero with the install left intact (never a false ``OK`` or a
      silent unset — audit H);
    * an ``install`` run always outranks a migrated ``legacy-inplace`` run, so an
      uninstall-time / same-second migration can't shadow it (audit A/B);
    * core.hooksPath is reverted **only if AQG currently owns it** (points at our
      hook dir); a value the user has since set is left untouched (audit D:
      re-uninstall / path-reuse must not clobber a foreign value);
    * on success the whole backup slot is consumed, so nothing stale remains for a
      later run to restore (audit C/D).
    """
    if aqg_root is None:
        aqg_root = _resolve_aqg_root()

    target_top = _resolve_git_top(target)
    if target_top is None:
        print(f"ERROR: {target} is not inside a git work tree", file=sys.stderr)
        return EXIT_NOT_GIT

    hooks_dir = target_top / HOOK_DIR_REL
    hook_path = hooks_dir / HOOK_FILE

    # An attacker-checked-in symlink (or an escaping path) at the legacy in-tree
    # backup must never be read and injected into core.hooksPath (gemini
    # fix-review #2). Warn and leave it untouched; otherwise fold the legacy value
    # into the central store so the selection below can see it.
    legacy_backup = target_top / HOOKSPATH_BACKUP_REL
    if legacy_backup.is_symlink() or (
        legacy_backup.exists() and not _contained(legacy_backup, target_top)
    ):
        print(
            f"WARN: ignoring an untrusted core.hooksPath backup "
            f"(symlink or escapes the repo): {legacy_backup}",
            file=sys.stderr,
        )
    else:
        _migrate_legacy_hookspath_backup(target_top, aqg_root)

    # Revert core.hooksPath ONLY if AQG currently owns the LOCAL value. Read/restore
    # in the same (local) scope install wrote (round-2 V2f8). If the user has
    # pointed it elsewhere (or unset it) since install, leave their value alone —
    # and never restore a backup over it. Reverting before the hook is removed keeps
    # the repo consistent if the git op fails.
    rc_cur, cur_val = _safe_run(
        ["git", "-C", str(target_top), "config", "--local", "--get", "core.hooksPath"]
    )
    owns = rc_cur == 0 and cur_val == HOOK_DIR_REL

    # Runs to consume once the revert succeeds. Populated ONLY on the owned path so
    # an uninstall that restored nothing leaves the backup slot intact rather than
    # destroying the last record of the pre-install value (round-2 V1f2/V4f3).
    consume: list = []

    if owns:
        # One snapshot drives both selection and consumption, so a concurrent
        # install's newer run is neither restored-from nor deleted (round-2 V4f1).
        installs, legacies = _hookspath_runs(target_top, aqg_root)
        consume = installs + legacies
        run = installs[0] if installs else (legacies[0] if legacies else None)
        if run is not None:
            value = _hookspath_value_from_run(run)
            if value is None:
                print(
                    f"WARN: found a core.hooksPath backup but could not read it; "
                    f"leaving the install in place: {run}",
                    file=sys.stderr,
                )
                return EXIT_GENERIC
            if value.strip():
                rc, out = _safe_run(
                    ["git", "-C", str(target_top), "config", "--local", "core.hooksPath", value]
                )
                if rc != 0:
                    print(
                        f"WARN: failed to restore core.hooksPath to '{value}' ({out}); "
                        "leaving the install in place",
                        file=sys.stderr,
                    )
                    return EXIT_GENERIC
                print(
                    f"OK: restored core.hooksPath to '{value}' from the central backup"
                )
            else:
                rc, out = _safe_run(
                    ["git", "-C", str(target_top), "config", "--local", "--unset", "core.hooksPath"]
                )
                if rc not in (0, 5):  # 5 == key already absent (nothing to unset)
                    print(
                        f"WARN: failed to unset core.hooksPath ({out}); "
                        "leaving the install in place",
                        file=sys.stderr,
                    )
                    return EXIT_GENERIC
                print("OK: git config core.hooksPath unset")
        else:
            rc, out = _safe_run(
                ["git", "-C", str(target_top), "config", "--local", "--unset", "core.hooksPath"]
            )
            if rc not in (0, 5):
                print(
                    f"WARN: failed to unset core.hooksPath ({out}); "
                    "leaving the install in place",
                    file=sys.stderr,
                )
                return EXIT_GENERIC
            print("OK: git config core.hooksPath unset")
    else:
        print(
            f"INFO: core.hooksPath is not AQG-owned "
            f"(value: {cur_val or '(unset)'}); leaving it unchanged"
        )

    # Config reverted (or intentionally left) — now remove the hook artifact.
    # round-2 verify (gpt-5.5 + gemini, convergent): containment-check the PARENT
    # BEFORE any unlink. A bare `hook_path.is_symlink()` follows an escaping parent
    # (e.g. .aqg/hooks symlinked out of tree) and would unlink an out-of-tree
    # entry. With the parent proven in-tree, unlinking hook_path is safe whether it
    # is a regular file or a final-component symlink (unlink removes only the
    # in-tree link entry, never its target).
    if not _contained(hook_path.parent, target_top):
        print(
            f"WARN: refusing to remove a hook path whose parent escapes the repo "
            f"(symlinked parent?): {hook_path}",
            file=sys.stderr,
        )
    elif hook_path.is_symlink():
        hook_path.unlink()
        print(f"OK: removed symlinked hook entry {hook_path}")
    elif hook_path.exists():
        hook_path.unlink()
        print(f"OK: removed {hook_path}")
    else:
        print(f"INFO: hook not present at {hook_path} (no-op)")

    # Consume exactly the snapshot selected above — and only when AQG owned the
    # value (consume is empty otherwise). After an owned uninstall nothing stale
    # remains for a later run to restore (audit C/D); a not-owned uninstall keeps
    # the slot so the pre-install value survives (round-2 V1f2/V4f3); a concurrent
    # install's newer run is outside the snapshot and is left alone (round-2 V4f1).
    _consume_hookspath_runs(consume, target_top)
    return EXIT_OK


def verify(target: Path) -> int:
    """Show install state, no changes."""
    target_top = _resolve_git_top(target)
    if target_top is None:
        print(f"NOT_GIT_REPO: {target}")
        return EXIT_NOT_GIT
    rc, out = _safe_run(
        ["git", "-C", str(target_top), "config", "--get", "core.hooksPath"]
    )
    hook_path = target_top / HOOK_DIR_REL / HOOK_FILE
    print(f"target: {target_top}")
    print(f"core.hooksPath: {out if rc == 0 else '(unset)'}")
    print(f"hook file: {hook_path} (exists={hook_path.exists()})")
    print(
        f"AQG_AGENT env: "
        f"{os.environ.get('AQG_AGENT', '(unset; hook will silent pass)')}"
    )
    return EXIT_OK


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_aqg_construction_hook",
        description="Install/uninstall/verify AQG code construction pre-commit hook",
    )
    parser.add_argument("--target-repo", default=".", help="Target git repo (default: cwd)")
    parser.add_argument("--force", action="store_true", help="Overwrite existing hook")
    parser.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove the hook + reset core.hooksPath",
    )
    parser.add_argument(
        "--verify", action="store_true", help="Show install state, no changes"
    )
    args = parser.parse_args(argv)

    target = Path(args.target_repo).resolve()
    aqg_root = _resolve_aqg_root()
    if aqg_root is None:
        print(
            "ERROR: cannot resolve AQG root "
            "(set AQG_ROOT env var or run from AQG checkout)",
            file=sys.stderr,
        )
        return EXIT_GENERIC

    if args.verify:
        return verify(target)
    if args.uninstall:
        return uninstall(target, aqg_root)
    return install(target, aqg_root, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
