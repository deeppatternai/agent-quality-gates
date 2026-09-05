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

    monkeypatch.setattr(skill_install, "_is_windows_junction", lambda path: path == junction)
    if link is not None:
        assert skill_install.classify_install(link) == "symlink"
    assert skill_install.classify_install(junction) == "junction"
    assert skill_install.classify_install(copied) == "copied"
    assert skill_install.classify_install(plain) == "plain_directory"


def test_junction_creation_passes_paths_via_environment(monkeypatch, tmp_path):
    source = tmp_path / "source & literal"
    target = tmp_path / "target & literal"
    captured = {}

    monkeypatch.setattr(skill_install, "_is_windows_host", lambda: True)
    monkeypatch.setattr(skill_install.shutil, "which", lambda _name: "powershell.exe")

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs["env"]
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr(skill_install.subprocess, "run", fake_run)

    assert skill_install._create_windows_junction(source, target) is True
    assert "cmd.exe" not in captured["command"]
    assert str(source) not in " ".join(captured["command"])
    assert str(target) not in " ".join(captured["command"])
    assert captured["env"]["AQG_JUNCTION_SOURCE"] == str(source)
    assert captured["env"]["AQG_JUNCTION_TARGET"] == str(target)
    assert captured["env"]["USERPROFILE"] != os.environ.get("USERPROFILE")
    assert captured["env"]["LOCALAPPDATA"] != os.environ.get("LOCALAPPDATA")


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


def test_existing_link_mode_is_refreshable_without_force(monkeypatch, tmp_path):
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
    monkeypatch.setattr(skill_install, "_remove_existing", remove)
    monkeypatch.setattr(skill_install, "_create_symlink", create)

    assert skill_install.install_skill(
        source,
        target,
        requested_mode="link",
        force=False,
        aqg_root=tmp_path,
    ) == "linked"
    assert removed == [target]


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
