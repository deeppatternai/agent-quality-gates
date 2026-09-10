#!/usr/bin/env python3
"""Install one AQG skill and report the filesystem mode actually created.

Host-neutral: every client installer routes skill installation through here,
not just one host. Mode link (the default) creates a symlink, falling back to
a Windows junction and then to a copy; mode copy copies outright.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


class InstallError(RuntimeError):
    """A skill install could not be completed without overwriting user state."""


def _is_windows_host() -> bool:
    return os.name == "nt"


def _is_windows_junction(path: Path) -> bool:
    if not _is_windows_host() or path.is_symlink():
        return False
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))

    # Python 3.9-3.11 do not expose os.path.isjunction(). Read the reparse tag
    # without following the entry so broken junctions remain detectable.
    class FileTime(ctypes.Structure):
        _fields_ = (("low", ctypes.c_uint32), ("high", ctypes.c_uint32))

    class FindData(ctypes.Structure):
        _fields_ = (
            ("attributes", ctypes.c_uint32),
            ("creation_time", FileTime),
            ("access_time", FileTime),
            ("write_time", FileTime),
            ("size_high", ctypes.c_uint32),
            ("size_low", ctypes.c_uint32),
            ("reparse_tag", ctypes.c_uint32),
            ("reserved", ctypes.c_uint32),
            ("file_name", ctypes.c_wchar * 260),
            ("alternate_file_name", ctypes.c_wchar * 14),
        )

    # Tests and compatibility layers can report a Windows-like host while the
    # interpreter does not expose Win32 APIs. Treat that as "not a junction" so
    # installation safely falls back to a copy instead of crashing.
    windll = getattr(ctypes, "windll", None)
    if windll is None:
        return False
    kernel32 = windll.kernel32
    find_first = kernel32.FindFirstFileW
    find_first.argtypes = (ctypes.c_wchar_p, ctypes.POINTER(FindData))
    find_first.restype = ctypes.c_void_p
    find_close = kernel32.FindClose
    find_close.argtypes = (ctypes.c_void_p,)
    find_close.restype = ctypes.c_int
    data = FindData()
    handle = find_first(str(path), ctypes.byref(data))
    invalid_handle = ctypes.c_void_p(-1).value
    if handle == invalid_handle:
        return False
    find_close(handle)
    file_attribute_directory = 0x0010
    file_attribute_reparse_point = 0x0400
    io_reparse_tag_mount_point = 0xA0000003
    return bool(
        data.attributes & file_attribute_directory
        and data.attributes & file_attribute_reparse_point
        and data.reparse_tag == io_reparse_tag_mount_point
    )


def classify_install(path: Path) -> str:
    """Return symlink, junction, copied, plain_directory, missing, or other."""
    if path.is_symlink():
        return "symlink"
    if _is_windows_junction(path):
        return "junction"
    if path.is_dir():
        return "copied" if (path / ".aqg-root").is_file() else "plain_directory"
    if not path.exists():
        return "missing"
    return "other"


def _remove_existing(path: Path) -> None:
    mode = classify_install(path)
    if mode == "missing":
        return
    if mode == "symlink":
        try:
            path.unlink()
        except OSError:
            if not _is_windows_host():
                raise
            os.rmdir(path)
    elif mode == "junction":
        os.rmdir(path)
    elif mode in {"copied", "plain_directory"}:
        if _is_windows_junction(path):
            os.rmdir(path)
        else:
            shutil.rmtree(path)
    else:
        path.unlink()


def remove_install(path: Path) -> None:
    """Remove one classified install entry without following its referent."""
    _remove_existing(path)


def _create_symlink(source: Path, target: Path) -> None:
    os.symlink(source, target, target_is_directory=True)


def _create_windows_junction(source: Path, target: Path) -> bool:
    if not _is_windows_host():
        return False
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if powershell is None:
        return False
    env = os.environ.copy()
    env["AQG_JUNCTION_SOURCE"] = str(source)
    env["AQG_JUNCTION_TARGET"] = str(target)
    with tempfile.TemporaryDirectory(prefix="aqg-junction-profile-") as profile:
        env["HOME"] = profile
        env["USERPROFILE"] = profile
        env["APPDATA"] = str(Path(profile) / "AppData" / "Roaming")
        env["LOCALAPPDATA"] = str(Path(profile) / "AppData" / "Local")
        completed = subprocess.run(
            [
                powershell,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "$ErrorActionPreference='Stop'; New-Item -ItemType Junction "
                "-Path $env:AQG_JUNCTION_TARGET -Target $env:AQG_JUNCTION_SOURCE "
                "| Out-Null",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            check=False,
        )
    return completed.returncode == 0


def create_windows_junction(source: Path, target: Path) -> bool:
    """Create a Windows directory junction without interpolating its paths."""
    return _create_windows_junction(source, target)


def _copy_skill(source: Path, target: Path, aqg_root: Path) -> None:
    shutil.copytree(source, target)
    (target / ".aqg-root").write_text(str(aqg_root.resolve()) + "\n", encoding="utf-8")


def _logical_root_of(root: Path) -> Path:
    """`aqg_update.migrate.logical_root`, or the root unchanged if unavailable.

    This installer has to keep working in a checkout with no update engine —
    and a checkout with no update engine has no version swap to survive, so the
    spelling it was given is already the stable one.
    """
    try:
        from scripts.aqg_update.migrate import logical_root
    except ImportError:  # invoked with scripts/ itself on sys.path
        try:
            from aqg_update.migrate import logical_root  # type: ignore[no-redef]
        except ImportError:
            # Loud, because the two sides are contractually required to produce
            # one string and the reader cannot even import without this module.
            # Degrading quietly here writes version-pinned links that the update
            # path will never recognise — the exact silent failure this change
            # exists to remove.
            print(
                "NOTE: the AQG update engine is not importable, so skill links "
                "are written with the path as given. In a managed install this "
                "pins them to one version and they will not receive automatic "
                "updates.",
                file=sys.stderr,
            )
            return root
    return logical_root(root)


def _link_text_for(source: Path, real: Path, aqg_root: Path | None) -> Path:
    """The spelling to write into the link.

    Ownership of a route is an exact link-text comparison, so this installer and
    `aqg_update.plan` must arrive at the same string or they will disagree about
    which links are ours — which is exactly what happened.

    The reader builds ``logical_root(root) / <subdir> / <name>`` **lexically**.
    So this prefers the lexical relationship too, and falls back to the physical
    one only when the given spellings do not share a prefix. Deriving it from
    resolved paths first looked equivalent and is not: any symlinked component
    *inside* the checkout resolves away on this side and stays on the reader's,
    and the two strings differ for a route that is perfectly correct.

    ``expanduser`` on both sides, and before anything else. ``Path.resolve()``
    does not expand ``~`` — it makes ``~/x`` into ``<cwd>/~/x`` — so an
    unexpanded tilde root sent this down the fallback path and wrote the
    physical spelling, silently restoring the stall.

    A source outside the root is left alone: there is no root to re-spell it
    against, and guessing one would write a link naming a directory the caller
    never mentioned.
    """
    if aqg_root is None:
        return _normal(real)
    root_given = _normal(Path(aqg_root).expanduser())
    source_given = _normal(Path(source).expanduser())
    try:
        rel = source_given.relative_to(root_given)
    except ValueError:
        try:
            rel = real.relative_to(Path(aqg_root).expanduser().resolve())
        except (ValueError, OSError):
            return _normal(real)
    return _logical_root_of(root_given) / rel


def _normal(path: Path) -> Path:
    """Absolute and lexically normalised, never resolved."""
    return Path(os.path.normpath(str(Path(path).expanduser().absolute())))


def _resolved_skill_path(path: Path) -> Path:
    """One resolved spelling, including Win32 extended-length path aliases."""
    value = str(path.resolve())
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def skill_link_source(source: Path) -> Path:
    """Use the managed entrance for link identity; leave other checkouts alone."""
    managed = Path.home() / ".deeppattern" / "agent-quality-gates"
    source = source.expanduser()
    return _link_text_for(source, _resolved_skill_path(source), managed)


def same_skill_source(recorded: Path, expected: Path) -> bool:
    """Compare link marker identity, including old physical release markers.

    Directional: recorded is possibly historical metadata; expected belongs to
    the current release (a source or, for ownership, an actual destination).
    This is NOT a target check: callers must also check the actual link against
    the current source. Legacy compatibility is limited to the canonical home
    installation's versions directory and the exact same skill suffix. It is
    read-only, including when retention has already removed the old release.
    """
    if not recorded.is_absolute() or not expected.is_absolute():
        return False
    try:
        if _resolved_skill_path(recorded) == _resolved_skill_path(expected):
            return True
        # Match migrate.logical_root's spelling when HOME itself is a link.
        managed = _resolved_skill_path(Path.home() / ".deeppattern") / "agent-quality-gates"
        if not managed.is_symlink():
            return False
        versions = managed.parent.resolve() / "versions"
        current = _resolved_skill_path(managed)
        if current.parent != versions:
            return False
        relative = skill_link_source(expected).relative_to(managed)
        if len(relative.parts) != 2 or relative.parts[0] != "skills":
            return False
        if not relative.name.startswith("aqg-"):
            return False
        # Resolve aliases/prefixes but reject a surviving release redirected
        # outside this installation. A removed release still resolves lexically.
        old = _resolved_skill_path(recorded).relative_to(versions)
        if len(old.parts) != 3 or Path(*old.parts[1:]) != relative:
            return False
        try:
            from scripts.aqg_update.stage import is_release_name
        except ModuleNotFoundError:
            from aqg_update.stage import is_release_name

        label = old.parts[0]
        return is_release_name(label) or (
            len(label) == 40 and all(c in "0123456789abcdef" for c in label)
        )
    except (OSError, RuntimeError, ValueError, ImportError):
        return False


def install_skill(
    source: Path,
    target: Path,
    *,
    requested_mode: str,
    force: bool,
    aqg_root: Path | None = None,
) -> str:
    """Install a skill and return linked, junctioned, or copied.

    Two spellings of one directory, and which is used where is the whole point.

    ``real`` is the physical path. **Every filesystem operation uses it** —
    every check, and every read. A check that runs on one path while the work
    runs on another is not a check: it leaves a window in which the link under
    a logical path can be swapped between the two.

    ``link_text`` is written into the symlink and is used for nothing else. It
    must stay logical, because under the managed layout the physical path is
    ``versions/<sha>/...``: a link naming that stops being recognised as ours at
    the next release (`aqg_update.skills_route` compares raw link text), is
    never re-pointed, and dangles once retention drops the tree it names.
    Deriving it here rather than trusting the caller's spelling is the point —
    the reader derives the same way, and the two must agree.
    """
    real = source.resolve(strict=True)
    link_text = _link_text_for(source, real, aqg_root)
    if not real.is_dir() or not (real / "SKILL.md").is_file():
        raise InstallError(f"invalid skill source (SKILL.md required): {real}")
    target = target.absolute()
    # Resolve the parent to catch symlinked-parent escapes without following an
    # existing final symlink/junction that the installer may need to replace.
    target = target.parent.resolve(strict=False) / target.name
    # Overlap is asked of the PHYSICAL paths: two different spellings of the
    # same directory must not slip past by looking unalike.
    if real == target or real in target.parents or target in real.parents:
        raise InstallError(f"source and target must not overlap: {real} / {target}")
    existing = classify_install(target)
    if existing != "missing":
        refreshable_link = requested_mode == "link" and existing in {"symlink", "junction"}
        if not force and not refreshable_link:
            display = {
                "symlink": "existing symlink",
                "junction": "existing junction",
                "copied": "existing copied directory",
                "plain_directory": "existing plain directory",
            }.get(existing, "existing file")
            raise InstallError(f"{display}: {target} (use --force to replace)")
        _remove_existing(target)

    target.parent.mkdir(parents=True, exist_ok=True)
    # `.aqg-root` is written into a copied skill so it can find its checkout
    # again. With no `--aqg-root` there is nothing to derive it from: two levels
    # up from a skill is the pack directory, not the root, for every layout this
    # repo ships. Every caller that ships passes the flag; this fallback exists
    # for a hand-run copy and is wrong for anything deeper than a flat layout.
    root = Path(aqg_root or real.parent.parent).expanduser().resolve()
    if requested_mode == "copy":
        # Copies read from `real`, never from `link_text`. Copy mode leaves no
        # link text, so it is outside what `skills_route` can recognise as
        # owned — it is not part of the managed-update roster and is documented
        # that way rather than half-supported.
        _copy_skill(real, target, root)
        return "copied"

    try:
        _create_symlink(link_text, target)
    except OSError:
        pass
    created = classify_install(target)
    if created == "symlink":
        return "linked"
    if created == "junction":
        return "junctioned"

    # Some MSYS ln implementations create a plain directory instead of an NTFS
    # link. Remove only the target just created, then try the Windows-native path.
    if created != "missing":
        _remove_existing(target)
    if _is_windows_host() and _create_windows_junction(real, target):
        if classify_install(target) == "junction":
            return "junctioned"
        _remove_existing(target)

    _copy_skill(real, target, root)
    if requested_mode == "link":
        # Not silent. A copy carries no link text, so `aqg_update.skills_route`
        # cannot recognise it as ours: the update path will neither re-point nor
        # prune it, and it is frozen at this version's content. The install is
        # usable; automatic maintenance of it is not, and the user is the only
        # one who can decide whether that matters.
        print(
            f"NOTE: {target.name} was copied, not linked — this host will not "
            f"receive automatic skill updates for it. Symlink creation failed; "
            f"on Windows this usually means Developer Mode is off.",
            file=sys.stderr,
        )
    return "copied"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--aqg-root", required=True)
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    source = Path(args.source)
    target = Path(args.target)
    try:
        actual = install_skill(
            source,
            target,
            requested_mode=args.mode,
            force=args.force,
            aqg_root=Path(args.aqg_root),
        )
    except (InstallError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"{actual} {source.name} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
