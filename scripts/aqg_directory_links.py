#!/usr/bin/env python3
"""Fail-closed directory-link primitives shared by installers and updates."""

from __future__ import annotations

import ctypes
import os
import stat
import struct
from pathlib import Path


FILE_ATTRIBUTE_DIRECTORY = 0x0010
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003

_FSCTL_SET_REPARSE_POINT = 0x000900A4
_FSCTL_GET_REPARSE_POINT = 0x000900A8
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_MAXIMUM_REPARSE_DATA_BUFFER_SIZE = 16 * 1024


class DirectoryLinkError(OSError):
    """A directory-link operation could not be proven safe."""


def _is_windows() -> bool:
    return os.name == "nt"


def _error(action: str, path: Path, exc: BaseException) -> DirectoryLinkError:
    return DirectoryLinkError(f"{action} {path}: {exc}")


def _normal_absolute(path: Path, *, relative_to: Path | None = None) -> Path:
    value = Path(path).expanduser()
    if not value.is_absolute():
        value = (relative_to or Path.cwd()) / value
    return Path(os.path.normpath(os.path.abspath(str(value))))


def _is_volume_target(path: Path | str) -> bool:
    value = str(path).replace("/", "\\").lower()
    return value.startswith("\\??\\volume{") or value.startswith("\\\\?\\volume{")


def _is_unc_target(path: Path | str) -> bool:
    value = str(path).replace("/", "\\").lower()
    if value.startswith("\\??\\unc\\") or value.startswith("\\\\?\\unc\\"):
        return True
    if value.startswith("\\??\\") or value.startswith("\\\\?\\"):
        return False
    return value.startswith("\\\\")


def _is_local_junction_substitute(value: str) -> bool:
    if _is_volume_target(value) or _is_unc_target(value):
        return False
    stripped = _strip_windows_namespace(value).replace("/", "\\")
    return len(stripped) >= 3 and stripped[0].isalpha() and stripped[1:3] == ":\\"


def _strip_windows_namespace(value: str) -> str:
    lowered = value.lower()
    if lowered.startswith("\\??\\unc\\"):
        return "\\\\" + value[8:]
    if lowered.startswith("\\??\\"):
        return value[4:]
    if lowered.startswith("\\\\?\\unc\\"):
        return "\\\\" + value[8:]
    if lowered.startswith("\\\\?\\"):
        return value[4:]
    return value


def _kernel32() -> object:
    try:
        return ctypes.WinDLL("kernel32", use_last_error=True)
    except (AttributeError, OSError) as exc:
        raise DirectoryLinkError(f"Win32 directory-link API unavailable: {exc}") from exc


def _open_reparse_point(path: Path, *, writable: bool) -> tuple[object, int]:
    kernel32 = _kernel32()
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        ctypes.c_wchar_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
    )
    create_file.restype = ctypes.c_void_p
    handle = create_file(
        _windows_api_path(path),
        _GENERIC_WRITE if writable else 0,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_FLAG_OPEN_REPARSE_POINT | _FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel32, handle


def _device_io_control(
    kernel32: object,
    handle: int,
    code: int,
    in_buffer: bytes | None = None,
) -> bytes:
    device_io = kernel32.DeviceIoControl
    device_io.argtypes = (
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_void_p,
    )
    device_io.restype = ctypes.c_int
    incoming = ctypes.create_string_buffer(in_buffer) if in_buffer is not None else None
    outgoing = ctypes.create_string_buffer(_MAXIMUM_REPARSE_DATA_BUFFER_SIZE)
    returned = ctypes.c_uint32()
    ok = device_io(
        handle,
        code,
        incoming,
        len(in_buffer) if in_buffer is not None else 0,
        outgoing if in_buffer is None else None,
        len(outgoing) if in_buffer is None else 0,
        ctypes.byref(returned),
        None,
    )
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    return outgoing.raw[: returned.value]


def _close_handle(kernel32: object, handle: int) -> None:
    close = kernel32.CloseHandle
    close.argtypes = (ctypes.c_void_p,)
    close.restype = ctypes.c_int
    close(handle)


def _get_windows_reparse_data(path: Path) -> bytes:
    kernel32, handle = _open_reparse_point(path, writable=False)
    try:
        return _device_io_control(kernel32, handle, _FSCTL_GET_REPARSE_POINT)
    finally:
        _close_handle(kernel32, handle)


def _windows_api_path(path: Path) -> str:
    value = str(path)
    if value.startswith("\\\\?\\"):
        return value
    return "\\\\?\\" + value


def _decode_mount_point_target(raw: bytes) -> str:
    if len(raw) < 16:
        raise DirectoryLinkError("truncated mount-point reparse data")
    tag, data_length, _reserved, sub_offset, sub_length, print_offset, print_length = (
        struct.unpack_from("<IHHHHHH", raw)
    )
    if tag != IO_REPARSE_TAG_MOUNT_POINT:
        raise DirectoryLinkError(f"unexpected reparse tag: 0x{tag:08x}")
    if data_length < 8:
        raise DirectoryLinkError("invalid mount-point data length")
    end = 8 + data_length
    if end > len(raw):
        raise DirectoryLinkError("truncated mount-point path buffer")
    path_buffer = raw[16:end]

    def decode(offset: int, length: int) -> str:
        if offset + length > len(path_buffer) or offset % 2 or length % 2:
            raise DirectoryLinkError("invalid mount-point path offsets")
        return path_buffer[offset : offset + length].decode("utf-16-le")

    substitute = decode(sub_offset, sub_length)
    # PrintName is display-only and can disagree with the actual referent.
    # SubstituteName is the kernel-resolved target and is the sole authority.
    decode(print_offset, print_length)  # validate the complete record
    return substitute


def _read_windows_mount_point_substitute(path: Path) -> str:
    try:
        return _decode_mount_point_target(_get_windows_reparse_data(path))
    except DirectoryLinkError:
        raise
    except (OSError, UnicodeError, struct.error) as exc:
        raise _error("cannot read directory link", path, exc) from exc


def _read_windows_reparse_target(path: Path) -> Path:
    value = _read_windows_mount_point_substitute(path)
    if not _is_local_junction_substitute(value):
        raise DirectoryLinkError(f"unsupported mount-point target: {value}")
    return Path(_strip_windows_namespace(value))


def link_kind(path: Path) -> str:
    """Return missing/directory/symlink/junction/other_reparse/other."""
    path = Path(path)
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as exc:
        raise _error("cannot inspect", path, exc) from exc

    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    attributes = getattr(info, "st_file_attributes", 0)
    if _is_windows() and attributes & FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(info, "st_reparse_tag", None)
        if tag is None:
            try:
                raw = _get_windows_reparse_data(path)
                if len(raw) < 4:
                    raise DirectoryLinkError("truncated reparse tag")
                tag = struct.unpack_from("<I", raw)[0]
            except OSError as exc:
                raise _error("cannot read reparse tag", path, exc) from exc
        if tag != IO_REPARSE_TAG_MOUNT_POINT:
            return "other_reparse"
        try:
            substitute = _read_windows_mount_point_substitute(path)
        except OSError as exc:
            raise _error("cannot classify reparse point", path, exc) from exc
        return "junction" if _is_local_junction_substitute(substitute) else "other_reparse"
    if stat.S_ISDIR(info.st_mode) or attributes & FILE_ATTRIBUTE_DIRECTORY:
        return "directory"
    return "other"


def read_link_target(path: Path) -> Path:
    """Read a supported directory link without resolving its logical spelling."""
    path = _normal_absolute(Path(path))
    kind = link_kind(path)
    try:
        if kind == "symlink":
            raw = Path(_strip_windows_namespace(os.readlink(path)))
        elif kind == "junction":
            raw = _read_windows_reparse_target(path)
            if _is_volume_target(raw):
                raise DirectoryLinkError(f"volume mount point is not a junction: {path}")
        else:
            raise DirectoryLinkError(f"not a supported directory link ({kind}): {path}")
    except DirectoryLinkError:
        raise
    except OSError as exc:
        raise _error("cannot read directory link", path, exc) from exc
    return _normal_absolute(raw, relative_to=path.parent)


def _validate_local_absolute(path: Path, role: str) -> Path:
    raw = str(Path(path).expanduser()).replace("/", "\\")
    if _is_volume_target(raw) or _is_unc_target(raw):
        raise DirectoryLinkError(f"{role} must be an absolute local path: {path}")
    path = _normal_absolute(Path(_strip_windows_namespace(raw)))
    value = str(path).replace("/", "\\")
    if not path.is_absolute() or value.startswith("\\\\"):
        raise DirectoryLinkError(f"{role} must be an absolute local path: {path}")
    if _is_windows() and (len(value) < 3 or value[1:3] != ":\\"):
        raise DirectoryLinkError(f"{role} must name a local drive path: {path}")
    if _is_volume_target(value):
        raise DirectoryLinkError(f"{role} must not be a volume mount path: {path}")
    return path


def _junction_reparse_data(source: Path) -> bytes:
    printed = str(source)
    substitute = "\\??\\" + printed
    substitute_bytes = substitute.encode("utf-16-le")
    printed_bytes = printed.encode("utf-16-le")
    path_buffer = substitute_bytes + b"\x00\x00" + printed_bytes + b"\x00\x00"
    mount_data_length = 8 + len(path_buffer)
    try:
        header = struct.pack(
            "<IHHHHHH",
            IO_REPARSE_TAG_MOUNT_POINT,
            mount_data_length,
            0,
            0,
            len(substitute_bytes),
            len(substitute_bytes) + 2,
            len(printed_bytes),
        )
    except struct.error as exc:
        raise DirectoryLinkError(f"junction target path is too long: {source}") from exc
    return header + path_buffer


def _create_windows_junction_entry(source: Path, target: Path) -> None:
    raw = _junction_reparse_data(source)
    kernel32, handle = _open_reparse_point(target, writable=True)
    try:
        _device_io_control(kernel32, handle, _FSCTL_SET_REPARSE_POINT, raw)
    finally:
        _close_handle(kernel32, handle)


def create_junction(source: Path, target: Path) -> None:
    """Create only target as a junction; source may intentionally be missing."""
    if not _is_windows():
        raise DirectoryLinkError("junction creation requires Windows")
    source = _validate_local_absolute(Path(source), "junction source")
    target = _validate_local_absolute(Path(target), "junction target entry")
    source_kind = link_kind(source)
    if source_kind not in {"missing", "directory"}:
        raise DirectoryLinkError(
            f"junction source must be a directory or future directory ({source_kind}): {source}"
        )
    if link_kind(target) != "missing":
        raise DirectoryLinkError(f"junction target entry already exists: {target}")
    if link_kind(target.parent) != "directory":
        raise DirectoryLinkError(f"junction target parent is not a directory: {target.parent}")

    created = False
    try:
        os.mkdir(target)
        created = True
        _create_windows_junction_entry(source, target)
        if link_kind(target) != "junction" or read_link_target(target) != source:
            raise DirectoryLinkError(f"created junction failed verification: {target}")
    # aqg: top-level boundary
    except BaseException as exc:
        cleanup_error: OSError | None = None
        if created:
            try:
                os.rmdir(target)
            except OSError as remove_exc:
                cleanup_error = remove_exc
        if cleanup_error is not None:
            raise DirectoryLinkError(
                f"cannot create junction {target}: {exc}; cleanup failed: {cleanup_error}"
            ) from exc
        if isinstance(exc, DirectoryLinkError):
            raise
        raise _error("cannot create junction", target, exc) from exc


def remove_directory_link(
    path: Path, *, expected_kind: str, expected_target: Path
) -> None:
    """Remove one proven link entry without recursively touching its referent."""
    path = _normal_absolute(Path(path))
    expected_target = _normal_absolute(Path(expected_target))
    if expected_kind not in {"symlink", "junction"}:
        raise DirectoryLinkError(f"unsupported expected directory-link kind: {expected_kind}")

    first = (link_kind(path), read_link_target(path))
    if first != (expected_kind, expected_target):
        raise DirectoryLinkError(
            f"directory link does not match removal expectation: {path} ({first[0]} -> {first[1]})"
        )
    second = (link_kind(path), read_link_target(path))
    if second != first:
        raise DirectoryLinkError(f"directory link changed during removal: {path}")
    try:
        if expected_kind == "junction" or _is_windows():
            os.rmdir(path)
        else:
            path.unlink()
    except OSError as exc:
        raise _error("cannot remove directory-link entry", path, exc) from exc
