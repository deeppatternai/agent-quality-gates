from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO / "scripts" / "aqg_skill_install.py"
SPEC = importlib.util.spec_from_file_location("aqg_skill_install", MODULE_PATH)
assert SPEC and SPEC.loader
skill_install = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = skill_install
SPEC.loader.exec_module(skill_install)


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    (source / "SKILL.md").write_text("# test\n", encoding="utf-8")
    return source


def test_classify_distinguishes_symlink_junction_copy_and_plain(monkeypatch, tmp_path):
    source = _source(tmp_path)
    link = tmp_path / "link"
    try:
        link.symlink_to(source, target_is_directory=True)
    except OSError:
        link = None

    junction = tmp_path / "junction"
    junction.mkdir()
    copied = tmp_path / "copied"
    copied.mkdir()
    (copied / ".aqg-root").write_text(str(source), encoding="utf-8")
    plain = tmp_path / "plain"
    plain.mkdir()

    real_link_kind = skill_install.link_kind
    monkeypatch.setattr(
        skill_install,
        "link_kind",
        lambda path: "junction" if path == junction else real_link_kind(path),
    )
    if link is not None:
        assert skill_install.classify_install(link) == "symlink"
    assert skill_install.classify_install(junction) == "junction"
    assert skill_install.classify_install(copied) == "copied"
    assert skill_install.classify_install(plain) == "plain_directory"


def test_junction_creation_delegates_paths_to_native_api(monkeypatch, tmp_path):
    source = tmp_path / "source & literal"
    target = tmp_path / "target & literal"
    captured = []

    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)
    monkeypatch.setattr(
        skill_install, "create_junction", lambda actual_source, actual_target: captured.append((actual_source, actual_target))
    )

    assert skill_install._create_windows_junction(source, target) is True
    assert captured == [(source, target)]


def test_plain_directory_created_by_link_attempt_falls_back_to_copy(monkeypatch, tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"

    def fake_symlink(_source: Path, destination: Path) -> None:
        destination.mkdir()

    monkeypatch.setattr(skill_install, "_create_symlink", fake_symlink)
    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)
    monkeypatch.setattr(skill_install, "_create_windows_junction", lambda _source, _target: False)

    mode = skill_install.install_skill(source, target, requested_mode="link", force=False)

    assert mode == "copied"
    assert skill_install.classify_install(target) == "copied"
    assert (target / "SKILL.md").is_file()


def test_cli_reports_copy_not_linked_after_plain_directory_link_result(
    monkeypatch, tmp_path, capsys
):
    source = _source(tmp_path)
    target = tmp_path / "target"

    monkeypatch.setattr(
        skill_install, "_create_symlink", lambda _source, destination: destination.mkdir()
    )
    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)
    monkeypatch.setattr(skill_install, "_create_windows_junction", lambda _source, _target: False)

    assert skill_install.main(
        [
            "--source",
            str(source),
            "--target",
            str(target),
            "--aqg-root",
            str(tmp_path),
        ]
    ) == 0
    output = capsys.readouterr().out
    assert output.startswith("copied ")
    assert "linked" not in output


def test_cli_names_existing_unmanaged_directory(monkeypatch, tmp_path, capsys):
    source = _source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    (target / "SKILL.md").write_text("# unmanaged\n", encoding="utf-8")

    assert skill_install.main(
        [
            "--source",
            str(source),
            "--target",
            str(target),
            "--aqg-root",
            str(tmp_path),
        ]
    ) == 1
    assert "existing plain directory" in capsys.readouterr().err


def test_install_refuses_overlapping_source_and_target(tmp_path):
    source = _source(tmp_path)

    for target in (source, source / "nested", tmp_path):
        try:
            skill_install.install_skill(
                source,
                target,
                requested_mode="link",
                force=True,
                aqg_root=tmp_path,
            )
        except skill_install.InstallError as exc:
            assert "must not overlap" in str(exc)
        else:
            raise AssertionError(f"overlapping target was accepted: {target}")


def test_successful_junction_is_reported_as_junctioned(monkeypatch, tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"

    monkeypatch.setattr(skill_install, "_create_symlink", lambda _source, _target: None)
    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)

    def fake_junction(_source: Path, destination: Path) -> bool:
        destination.mkdir()
        return True

    monkeypatch.setattr(skill_install, "_create_windows_junction", fake_junction)
    monkeypatch.setattr(
        skill_install,
        "classify_install",
        lambda path: "junction" if path == target and path.exists() else "missing",
    )

    assert skill_install.install_skill(source, target, requested_mode="link", force=False) == "junctioned"


def test_real_link_install_reports_the_type_created(tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"

    reported = skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    )
    actual = skill_install.classify_install(target)

    assert (reported, actual) in {
        ("linked", "symlink"),
        ("junctioned", "junction"),
        ("copied", "copied"),
    }


def test_real_link_install_handles_spaces_and_cmd_metacharacters(tmp_path):
    source = tmp_path / "source & literal"
    source.mkdir()
    (source / "SKILL.md").write_text("# test\n", encoding="utf-8")
    target = tmp_path / "target & literal"

    reported = skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    )

    assert reported in {"linked", "junctioned", "copied"}
    assert (target / "SKILL.md").is_file()


def test_force_reinstall_preserves_source_and_unrelated_directory(tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"
    unrelated = tmp_path / "unrelated"
    unrelated.mkdir()
    (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

    first = skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    )
    second = skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=True,
        aqg_root=tmp_path,
    )

    assert first in {"linked", "junctioned", "copied"}
    assert second in {"linked", "junctioned", "copied"}
    assert (source / "SKILL.md").read_text(encoding="utf-8") == "# test\n"
    assert (unrelated / "keep.txt").read_text(encoding="utf-8") == "keep"


@pytest.mark.skipif(os.name != "nt", reason="Windows junction behavior")
def test_broken_junction_is_replaced_without_touching_its_referent(tmp_path):
    first_source = _source(tmp_path)
    target = tmp_path / "target"
    first = skill_install.install_skill(
        first_source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    )
    if first != "junctioned":
        pytest.skip("native install did not select junction mode")

    (first_source / "SKILL.md").unlink()
    first_source.rmdir()
    assert skill_install.classify_install(target) == "junction"

    second_source = tmp_path / "second-source"
    second_source.mkdir()
    (second_source / "SKILL.md").write_text("# second\n", encoding="utf-8")
    second = skill_install.install_skill(
        second_source,
        target,
        requested_mode="link",
        force=True,
        aqg_root=tmp_path,
    )
    assert second in {"linked", "junctioned", "copied"}
    assert (target / "SKILL.md").read_text(encoding="utf-8") == "# second\n"


def test_existing_matching_link_is_reused_without_force(monkeypatch, tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    removed = []

    def classify(path: Path) -> str:
        return "symlink" if path == target else "missing"

    def remove(path: Path) -> None:
        removed.append(path)
        path.rmdir()

    def create(_source: Path, destination: Path) -> None:
        destination.mkdir()

    monkeypatch.setattr(skill_install, "classify_install", classify)
    monkeypatch.setattr(skill_install, "read_link_target", lambda _path: source)
    monkeypatch.setattr(skill_install, "_remove_existing", remove)
    monkeypatch.setattr(skill_install, "_create_symlink", create)

    assert skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    ) == "linked"
    assert removed == []


def test_windows_directory_symlink_removal_falls_back_to_rmdir(monkeypatch, tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    removed = []

    def fail_unlink(_path):
        raise OSError

    monkeypatch.setattr(skill_install, "classify_install", lambda _path: "symlink")
    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)
    monkeypatch.setattr(Path, "unlink", fail_unlink)
    monkeypatch.setattr(skill_install.os, "rmdir", lambda path: removed.append(path))

    skill_install._remove_existing(target)
    assert removed == [target]


def test_batch_preflights_every_item_before_mutating(tmp_path):
    first_source = _source(tmp_path)
    missing_source = tmp_path / "missing source"
    first_target = tmp_path / "first target"
    second_target = tmp_path / "second target"

    with pytest.raises(skill_install.InstallError, match="invalid skill source"):
        skill_install.install_skills(
            [(first_source, first_target), (missing_source, second_target)],
            requested_mode="link",
            force=False,
            aqg_root=tmp_path,
        )

    assert skill_install.classify_install(first_target) == "missing"
    assert skill_install.classify_install(second_target) == "missing"


def test_batch_failure_restores_replaced_entry_and_removes_new_entries(
    monkeypatch, tmp_path
):
    first_source = _source(tmp_path)
    second_source = tmp_path / "second source"
    second_source.mkdir()
    (second_source / "SKILL.md").write_text("# second\n", encoding="utf-8")
    first_target = tmp_path / "first target"
    first_target.mkdir()
    (first_target / "keep.txt").write_text("owned by user", encoding="utf-8")
    second_target = tmp_path / "second target"

    real_create = skill_install._create_symlink

    def create(source: Path, target: Path) -> None:
        if target == second_target:
            raise OSError("injected link failure")
        real_create(source, target)

    def fail_copy(_source: Path, target: Path, _root: Path) -> None:
        if target == second_target:
            raise OSError("injected copy failure")
        raise AssertionError(f"unexpected copy: {target}")

    monkeypatch.setattr(skill_install, "_create_symlink", create)
    monkeypatch.setattr(skill_install, "_copy_skill", fail_copy)

    with pytest.raises(OSError, match="injected copy failure"):
        skill_install.install_skills(
            [(first_source, first_target), (second_source, second_target)],
            requested_mode="link",
            force=True,
            aqg_root=tmp_path,
        )

    assert first_target.is_dir() and not first_target.is_symlink()
    assert (first_target / "keep.txt").read_text(encoding="utf-8") == "owned by user"
    assert skill_install.classify_install(second_target) == "missing"


def test_batch_reuses_correct_links_without_mutation(tmp_path):
    source = _source(tmp_path)
    target = tmp_path / "target"
    items = [(source, target)]

    first = skill_install.install_skills(
        items, requested_mode="link", force=False, aqg_root=tmp_path
    )
    inode = os.lstat(target).st_ino
    second = skill_install.install_skills(
        items, requested_mode="link", force=False, aqg_root=tmp_path
    )

    assert first == ["linked"]
    assert second == ["linked"]
    assert os.lstat(target).st_ino == inode


def test_batch_refuses_external_link_without_force(tmp_path):
    source = _source(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    target = tmp_path / "target"
    target.symlink_to(external, target_is_directory=True)

    with pytest.raises(skill_install.InstallError, match="external symlink"):
        skill_install.install_skills(
            [(source, target)],
            requested_mode="link",
            force=False,
            aqg_root=tmp_path,
        )

    assert target.is_symlink()
    assert target.resolve() == external


def test_classify_and_remove_refuse_unknown_reparse(monkeypatch, tmp_path):
    target = tmp_path / "unknown"
    target.mkdir()
    monkeypatch.setattr(skill_install, "link_kind", lambda _path: "other_reparse")

    assert skill_install.classify_install(target) == "other_reparse"
    with pytest.raises(skill_install.InstallError, match="unsupported reparse"):
        skill_install.remove_install(target)
    assert target.is_dir()
