from __future__ import annotations

import os
import struct
from pathlib import Path

import pytest

from scripts import aqg_directory_links as links


def test_link_kind_and_read_target_preserve_lexical_symlink_spelling(tmp_path: Path) -> None:
    source = tmp_path / "logical" / ".." / "logical" / "future"
    entry = tmp_path / "route"
    entry.symlink_to(source, target_is_directory=True)

    assert links.link_kind(entry) == "symlink"
    assert links.read_link_target(entry) == tmp_path / "logical" / "future"
    assert links.link_kind(tmp_path / "missing") == "missing"
    assert links.link_kind(tmp_path) == "directory"


def test_link_kind_does_not_turn_read_failure_into_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_path: Path) -> os.stat_result:
        raise PermissionError("denied")

    monkeypatch.setattr(links.os, "lstat", denied)
    with pytest.raises(links.DirectoryLinkError, match="denied"):
        links.link_kind(Path("blocked"))


def test_windows_mount_point_tag_distinguishes_junction_from_volume_mount(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = type(
        "Stat",
        (),
        {
            "st_mode": 0,
            "st_file_attributes": links.FILE_ATTRIBUTE_DIRECTORY
            | links.FILE_ATTRIBUTE_REPARSE_POINT,
            "st_reparse_tag": links.IO_REPARSE_TAG_MOUNT_POINT,
        },
    )()
    monkeypatch.setattr(links, "_is_windows", lambda: True)
    monkeypatch.setattr(links.os, "lstat", lambda _path: fake)
    monkeypatch.setattr(links, "_read_windows_mount_point_substitute", lambda _path: "\\??\\C:\\next")
    assert links.link_kind(Path("entry")) == "junction"

    monkeypatch.setattr(
        links,
        "_read_windows_mount_point_substitute",
        lambda _path: "\\??\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\",
    )
    assert links.link_kind(Path("entry")) == "other_reparse"


def _mount_point_data(substitute: str, printed: str) -> bytes:
    substitute_bytes = substitute.encode("utf-16-le")
    printed_bytes = printed.encode("utf-16-le")
    paths = substitute_bytes + b"\x00\x00" + printed_bytes + b"\x00\x00"
    return struct.pack(
        "<IHHHHHH",
        links.IO_REPARSE_TAG_MOUNT_POINT,
        8 + len(paths),
        0,
        0,
        len(substitute_bytes),
        len(substitute_bytes) + 2,
        len(printed_bytes),
    ) + paths


def test_mount_point_substitute_is_authoritative_over_mismatched_print_name() -> None:
    raw = _mount_point_data("\\??\\C:\\trusted", "C:\\attacker-controlled-display")
    assert links._decode_mount_point_target(raw) == "\\??\\C:\\trusted"


@pytest.mark.parametrize(
    "substitute",
    [
        "\\??\\Volume{01234567-89ab-cdef-0123-456789abcdef}\\",
        "\\??\\UNC\\server\\share\\route",
    ],
)
def test_volume_and_unc_mount_points_are_not_classified_as_junctions(
    monkeypatch: pytest.MonkeyPatch, substitute: str
) -> None:
    fake = type(
        "Stat",
        (),
        {
            "st_mode": 0,
            "st_file_attributes": links.FILE_ATTRIBUTE_DIRECTORY
            | links.FILE_ATTRIBUTE_REPARSE_POINT,
            "st_reparse_tag": links.IO_REPARSE_TAG_MOUNT_POINT,
        },
    )()
    monkeypatch.setattr(links, "_is_windows", lambda: True)
    monkeypatch.setattr(links.os, "lstat", lambda _path: fake)
    monkeypatch.setattr(links, "_read_windows_mount_point_substitute", lambda _path: substitute)
    assert links.link_kind(Path("entry")) == "other_reparse"


@pytest.mark.parametrize(
    "raw",
    [
        b"short",
        struct.pack(
            "<IHHHHHH",
            links.IO_REPARSE_TAG_MOUNT_POINT,
            0,
            0,
            0,
            0,
            0,
            0,
        ),
        struct.pack(
            "<IHHHHHH",
            links.IO_REPARSE_TAG_MOUNT_POINT,
            8,
            0,
            0,
            4,
            0,
            0,
        ),
    ],
)
def test_malformed_mount_point_data_is_rejected(raw: bytes) -> None:
    with pytest.raises(links.DirectoryLinkError, match="truncated|invalid"):
        links._decode_mount_point_target(raw)


def test_read_target_rejects_unknown_reparse(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(links, "link_kind", lambda _path: "other_reparse")
    with pytest.raises(links.DirectoryLinkError, match="not a supported directory link"):
        links.read_link_target(Path("entry"))


def test_create_junction_accepts_missing_source_and_cleans_only_new_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "future source"
    target = tmp_path / "route"
    calls: list[tuple[Path, Path]] = []

    monkeypatch.setattr(links, "_is_windows", lambda: True)
    monkeypatch.setattr(links, "_validate_local_absolute", lambda path, role: path)
    monkeypatch.setattr(
        links, "link_kind", lambda path: "directory" if path == target.parent else "missing"
    )

    def fail_after_create(actual_source: Path, actual_target: Path) -> None:
        calls.append((actual_source, actual_target))
        raise OSError("set reparse failed")

    monkeypatch.setattr(links, "_create_windows_junction_entry", fail_after_create)
    with pytest.raises(links.DirectoryLinkError, match="set reparse failed"):
        links.create_junction(source, target)

    assert calls == [(source, target)]
    assert not source.exists()
    assert not target.exists()


def test_remove_directory_link_revalidates_and_never_removes_referent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    referent = tmp_path / "referent"
    referent.mkdir()
    (referent / "keep.txt").write_text("keep", encoding="utf-8")
    entry = tmp_path / "entry"
    entry.symlink_to(referent, target_is_directory=True)
    observed: list[str] = []

    real_kind = links.link_kind
    real_target = links.read_link_target

    def kind(path: Path) -> str:
        observed.append("kind")
        return real_kind(path)

    def target(path: Path) -> Path:
        observed.append("target")
        return real_target(path)

    monkeypatch.setattr(links, "link_kind", kind)
    monkeypatch.setattr(links, "read_link_target", target)
    links.remove_directory_link(entry, expected_kind="symlink", expected_target=referent)

    assert observed == ["kind", "target", "kind", "kind", "target", "kind"]
    assert not entry.exists()
    assert (referent / "keep.txt").read_text(encoding="utf-8") == "keep"


def test_remove_directory_link_refuses_changed_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    entry = tmp_path / "entry"
    expected = tmp_path / "expected"
    calls = iter(["junction", "symlink"])
    monkeypatch.setattr(links, "link_kind", lambda _path: next(calls))
    monkeypatch.setattr(links, "read_link_target", lambda _path: expected)

    with pytest.raises(links.DirectoryLinkError, match="changed during removal"):
        links.remove_directory_link(
            entry, expected_kind="junction", expected_target=expected
        )


@pytest.mark.skipif(os.name != "nt", reason="requires native Windows junction support")
def test_native_windows_dangling_junction_round_trip(tmp_path: Path) -> None:
    source = tmp_path / "future source ; '中文"
    entry = tmp_path / "route ; '中文"

    links.create_junction(source, entry)
    assert links.link_kind(entry) == "junction"
    assert links.read_link_target(entry) == source
    assert not source.exists()

    links.remove_directory_link(entry, expected_kind="junction", expected_target=source)
    assert links.link_kind(entry) == "missing"
