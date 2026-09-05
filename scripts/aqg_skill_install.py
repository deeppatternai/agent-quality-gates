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


def install_skill(
    source: Path,
    target: Path,
    *,
    requested_mode: str,
    force: bool,
    aqg_root: Path | None = None,
) -> str:
    """Install a skill and return linked, junctioned, or copied."""
    source = source.resolve(strict=True)
    if not source.is_dir() or not (source / "SKILL.md").is_file():
        raise InstallError(f"invalid skill source (SKILL.md required): {source}")
    target = target.absolute()
    # Resolve the parent to catch symlinked-parent escapes without following an
    # existing final symlink/junction that the installer may need to replace.
    target = target.parent.resolve(strict=False) / target.name
    if source == target or source in target.parents or target in source.parents:
        raise InstallError(f"source and target must not overlap: {source} / {target}")
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
    root = (aqg_root or source.parent.parent).resolve()
    if requested_mode == "copy":
        _copy_skill(source, target, root)
        return "copied"

    try:
        _create_symlink(source, target)
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
    if _is_windows_host() and _create_windows_junction(source, target):
        if classify_install(target) == "junction":
            return "junctioned"
        _remove_existing(target)

    _copy_skill(source, target, root)
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
