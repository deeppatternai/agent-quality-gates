"""Route skills into a host's skills directory, and remove only our own.

docs/UPDATE_ARCHITECTURE.md §8. A skill is delivered as a symlink from the
host's skills directory into the AQG checkout, which is why editing a skill's
content needs no update work at all — only the roster ever changes.

**Every rule here is about ownership.** That directory is shared: it holds the
user's own skills, other tools' skills, and ours. Removing something we did not
create is unrecoverable, so the test for "ours" is narrow — a symlink whose
target is exactly ``<source>/<one component>``, the only shape this module ever
creates. Anything else is left alone, including a real directory, a link
elsewhere, and a link one level deeper.

The check is on the **raw link text**, exactly like the shell installer's, and
for the reason its comment records: ``<source>/../scripts`` matches a text
prefix and resolves *outside*. Anything that parses the target and compares the
pieces is broader than what is written — a lexically collapsed
``<source>/<x>/../<name>``, a link whose own name differs from its target's, and
relative text were each claimed as ours by such a version, and the first two
were reproduced deleting entries this module never created. Comparing against
the string ``route`` would write is narrower and shorter than parsing the one it
reads.

``source_root`` must therefore be **absolute and spelled canonically**: a
relative root, or one reached through a symlink, produces different text and the
module stops recognising its own past work — refusing to route over links it
created and never pruning them. That is enforced, not assumed.

The shared destination is assumed to have a single mutator during an apply. The
check-then-unlink in `prune` is re-validated immediately before the unlink, but
real serialisation is the update lock's job, held by the transaction layer.

On Windows this creates a directory **symlink** via ``os.symlink(...,
target_is_directory=True)``, which needs Developer Mode or elevation. There is
no junction code here. No Windows runner exists in this repo, so that path is
unverified.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Tuple


class RouteError(RuntimeError):
    """A refusal from the routing layer. Always fail-closed."""


def _require_safe_name(name: str) -> str:
    """Refuse anything but a single bare path component.

    The name reaches here from a plan built out of `install-state.json` and the
    target tree's roster, and it is joined to a directory that is not ours.
    """
    if (
        not name
        or name in {".", ".."}
        or os.sep in name
        or "/" in name
        or (os.altsep and os.altsep in name)
        or Path(name).name != name
    ):
        raise RouteError(f"invalid skill name {name!r}: expected one path component")
    return name


def _require_absolute_root(source_root: Path) -> Path:
    """Refuse a non-absolute source root.

    Recognition is a text comparison against what `route` writes, so a differently
    spelled root silently stops matching this module's own links.
    """
    source_root = Path(source_root)
    if not source_root.is_absolute():
        raise RouteError(
            f"source_root must be an absolute path, got {source_root}; ownership "
            f"is decided by comparing link text, so a different spelling would "
            f"make this layer stop recognising the routes it created"
        )
    return source_root


def _expected_link_text(source_root: Path, name: str) -> str:
    """The exact string `route` writes for *name*. The only shape we own."""
    return str(source_root / name)


def _windows_print_name(link: Path) -> str:
    """Read the original spelling; Windows normalizes the substitution name.

    os.readlink alone loses '..' and doubled separators on Windows. Ownership
    needs both the effective target and the exact text written by our installer.
    """
    import ctypes
    import struct
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                  wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.DeviceIoControl.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD,
                                      ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID)
    kernel.DeviceIoControl.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.CreateFileW(str(link), 0, 7, None, 3, 0x02200000, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        buffer = ctypes.create_string_buffer(16384)
        count = wintypes.DWORD()
        if not kernel.DeviceIoControl(handle, 0x900A8, None, 0, buffer, len(buffer),
                                      ctypes.byref(count), None):
            raise ctypes.WinError(ctypes.get_last_error())
        data = buffer.raw[:count.value]
        if len(data) < 20:
            raise OSError("truncated symlink reparse data")
        tag, size, _, _, _, offset, length, flags = struct.unpack_from("<IHHHHHHI", data)
        start, end = 20 + offset, 20 + offset + length
        if (tag != 0xA000000C or flags != 0 or size + 8 > len(data)
                or offset % 2 or length % 2 or not length or end > size + 8):
            raise OSError("not an absolute symlink with a valid print name")
        return data[start:end].decode("utf-16-le")
    finally:
        kernel.CloseHandle(handle)


def _is_our_route(link: Path, source_root: Path) -> bool:
    """Whether *link* is a symlink whose text is exactly what `route` writes.

    Deliberately not a parse-and-compare. Decomposing the target admits shapes
    this module never creates — a collapsed ``<source>/<x>/../<name>``, a name
    that differs from its target's, relative text — each of which was claimed as
    ours by the previous version. An exact comparison against the string we
    would write cannot be fooled lexically, and is three lines instead of
    fifteen.
    """
    if not link.is_symlink():
        return False
    try:
        raw = os.readlink(link)
    except OSError:
        return False
    expected = _expected_link_text(source_root, link.name)
    if os.name != "nt":
        return raw == expected
    try:
        if _windows_print_name(link) != expected:
            return False
    except (OSError, UnicodeError):
        return False
    if raw == expected:
        return True
    # Windows readlink returns the native substitution name. Accept only the
    # exact extended spelling of OUR expected path; never resolve or collapse
    # the supplied text (which could hide '..', aliases, or another skill).
    extended = "\\\\?\\UNC\\" + expected[2:] if expected.startswith("\\\\") else "\\\\?\\" + expected
    return raw == extended


def route(*, name: str, source_root: Path, dest_root: Path) -> bool:
    """Point ``dest_root/name`` at ``source_root/name``. Returns whether it was
    created.

    An already-correct route is left untouched rather than re-created: churning
    the link on every run buys nothing and races a concurrent session reading
    through it. Anything at that path which is not our own route is a refusal —
    a directory there is the user's own skill, and replacing it is
    unrecoverable.
    """
    _require_safe_name(name)
    source_root = _require_absolute_root(source_root)
    dest_root = Path(dest_root)
    source = source_root / name
    link = dest_root / name

    if not source.is_dir():
        raise RouteError(
            f"the checkout does not ship {name!r} at {source}; a dangling route "
            f"would make the host list a skill it cannot read"
        )
    if _is_our_route(link, source_root):
        return False
    if link.exists() or link.is_symlink():
        raise RouteError(
            f"{link} exists and is not a route this layer created; refusing to "
            f"replace it"
        )
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        # The text written here is what `_is_our_route` compares against.
        os.symlink(_expected_link_text(source_root, name), link,
                   target_is_directory=True)
    except OSError as exc:
        raise RouteError(f"cannot route {name} into {dest_root}: {exc}") from exc
    return True


def prune(*, name: str, source_root: Path, dest_root: Path) -> bool:
    """Remove ``dest_root/name`` if it is our own route. Returns whether it was.

    Absence is not an error: a prune planned from recorded state may name a
    route the user already removed by hand, and that is the desired end state.
    A dangling route of ours IS removed — the tree it pointed into is gone, but
    the shape is still ours and the entry makes the host list an unreadable
    skill.
    """
    _require_safe_name(name)
    source_root = _require_absolute_root(source_root)
    link = Path(dest_root) / name
    if not _is_our_route(link, source_root):
        return False
    # Re-checked immediately before the unlink: `os.unlink` on a path that
    # stopped being a symlink deletes that file. This narrows the window; the
    # update lock is what actually serialises AQG's own callers.
    try:
        if not stat.S_ISLNK(os.lstat(link).st_mode):
            return False
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RouteError(f"cannot re-check the route at {link}: {exc}") from exc
    try:
        os.unlink(link)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise RouteError(f"cannot remove the route at {link}: {exc}") from exc
    return True


def owned_routes(*, source_root: Path, dest_root: Path) -> Tuple[str, ...]:
    """Names in *dest_root* that are routes this layer created, sorted."""
    dest_root = Path(dest_root)
    source_root = _require_absolute_root(source_root)
    if not dest_root.is_dir():
        return ()
    try:
        children = sorted(dest_root.iterdir())
    except OSError as exc:
        raise RouteError(f"cannot read {dest_root}: {exc}") from exc
    return tuple(
        child.name for child in children if _is_our_route(child, source_root)
    )
