"""Windows directory-link lifecycle contracts for the managed updater.

The fake below models only Win32 junction directory-entry behavior on macOS.
The shared module's own tests and a later Windows-native run remain separate
gates; passing this file is not evidence that the Win32 implementation works.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from scripts import aqg_directory_links as directory_links
from scripts.aqg_update import dispatch, migrate, plan, skills_route, stage


UPDATER_CAPABILITIES = Path("scripts/aqg_update/updater-capabilities-v1.json")
JUNCTION_ROOT_CAPABILITY = {
    "schema": 1,
    "capabilities": {"windows_directory_junction_root": 1},
}


class _JunctionModel:
    def __init__(self) -> None:
        self._junction_inodes: set[tuple[int, int]] = set()
        self.other_reparse: set[Path] = set()

    @staticmethod
    def _absolute(path: Path) -> Path:
        return Path(os.path.normpath(str(Path(path).absolute())))

    @staticmethod
    def _inode(path: Path) -> tuple[int, int] | None:
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return None
        return info.st_dev, info.st_ino

    def link_kind(self, path: Path) -> str:
        path = self._absolute(path)
        if path in self.other_reparse:
            return "other_reparse"
        inode = self._inode(path)
        if inode is None:
            return "missing"
        if inode in self._junction_inodes:
            return "junction"
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "directory"
        return "other"

    def read_link_target(self, path: Path) -> Path:
        path = self._absolute(path)
        if self.link_kind(path) not in {"symlink", "junction"}:
            raise directory_links.DirectoryLinkError(f"not a directory link: {path}")
        raw = Path(os.readlink(path))
        return self._absolute(raw if raw.is_absolute() else path.parent / raw)

    def create_junction(self, source: Path, target: Path) -> None:
        source, target = self._absolute(source), self._absolute(target)
        if self.link_kind(target) != "missing":
            raise directory_links.DirectoryLinkError(f"target exists: {target}")
        target.symlink_to(source, target_is_directory=True)
        inode = self._inode(target)
        assert inode is not None
        self._junction_inodes.add(inode)

    def remove_directory_link(
        self, path: Path, *, expected_kind: str, expected_target: Path
    ) -> None:
        first = (self.link_kind(path), self.read_link_target(path))
        expected = (expected_kind, self._absolute(expected_target))
        if first != expected:
            raise directory_links.DirectoryLinkError(
                f"directory link changed: {path} ({first!r} != {expected!r})"
            )
        second = (self.link_kind(path), self.read_link_target(path))
        if second != first:
            raise directory_links.DirectoryLinkError(f"directory link changed: {path}")
        Path(path).unlink()


@pytest.fixture
def windows_links(monkeypatch) -> _JunctionModel:
    model = _JunctionModel()
    monkeypatch.setattr(directory_links, "link_kind", model.link_kind)
    monkeypatch.setattr(directory_links, "read_link_target", model.read_link_target)
    monkeypatch.setattr(directory_links, "create_junction", model.create_junction)
    monkeypatch.setattr(
        directory_links, "remove_directory_link", model.remove_directory_link
    )
    for module in (stage, migrate, skills_route):
        monkeypatch.setattr(module, "_is_windows", lambda: True, raising=False)
    monkeypatch.setattr(stage, "_replace_root_link", lambda source, root: os.replace(source, root))
    monkeypatch.setattr(skills_route, "_windows_print_name", lambda link: os.readlink(link))
    return model


def _version(
    path: Path,
    marker: str,
    *,
    junction_aware: bool = True,
    updater_capable: bool = True,
) -> Path:
    (path / "skills" / "aqg-code-construction").mkdir(parents=True)
    (path / "VERSION").write_text(marker + "\n", encoding="utf-8")
    (path / "skills" / "aqg-code-construction" / "marker").write_text(
        marker, encoding="utf-8"
    )
    if junction_aware:
        shared = path / "scripts" / "aqg_directory_links.py"
        shared.parent.mkdir(parents=True)
        shared.write_text("# compatibility sentinel\n", encoding="utf-8")
    if updater_capable:
        capability = path / UPDATER_CAPABILITIES
        capability.parent.mkdir(parents=True, exist_ok=True)
        capability.write_text(
            json.dumps(JUNCTION_ROOT_CAPABILITY) + "\n", encoding="utf-8"
        )
    return path


def test_fresh_windows_swap_creates_a_junction(tmp_path, windows_links):
    target = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"

    assert stage.swap_root(root=root, target=target) is None
    assert windows_links.link_kind(root) == "junction"
    assert stage.current_target(root) == target


def test_existing_windows_symlink_keeps_its_type(tmp_path, windows_links):
    old = _version(
        tmp_path / "versions" / "v1", "old",
        junction_aware=False, updater_capable=False,
    )
    new = _version(
        tmp_path / "versions" / "v2", "new",
        junction_aware=True, updater_capable=False,
    )
    root = tmp_path / "agent-quality-gates"
    root.symlink_to(old, target_is_directory=True)

    assert stage.swap_root(root=root, target=new) == old
    assert windows_links.link_kind(root) == "symlink"


def test_same_version_junction_activation_is_unchanged(tmp_path, windows_links):
    target = _version(tmp_path / "versions" / "v1", "current")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(target, root)
    resources = dispatch.Resources(target=target, root=root)
    action = plan.Action("activate_root", None, None, 0, "activate")

    outcome = dispatch.execute(
        plan.Plan(actions=(action,)), resources=resources, apply=True
    )[0]
    assert outcome.status == "unchanged"
    assert windows_links.link_kind(root) == "junction"


def test_unknown_root_reparse_is_refused_without_mutation(tmp_path, windows_links):
    target = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.other_reparse.add(root.absolute())

    with pytest.raises(stage.StageError, match="other_reparse"):
        stage.swap_root(root=root, target=target)
    assert windows_links.link_kind(root) == "other_reparse"


def test_junction_root_cannot_be_repointed_out_of_its_versions_directory(
    tmp_path, windows_links
):
    old = _version(tmp_path / "foreign" / "v1", "old")
    new = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(old, root)

    with pytest.raises(stage.StageError, match="not a version tree under"):
        stage.swap_root(root=root, target=new)
    assert stage.current_target(root) == old


def test_junction_rollback_refuses_an_updater_that_cannot_read_junctions(
    tmp_path, windows_links
):
    old = _version(
        tmp_path / "versions" / "v1", "old",
        junction_aware=False, updater_capable=False,
    )
    current = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(current, root)

    with pytest.raises(stage.StageError, match="junction-aware"):
        stage.swap_root(root=root, target=old)
    assert stage.current_target(root) == current


def test_a_only_tree_with_shared_module_but_no_updater_capability_is_refused(
    tmp_path, windows_links
):
    old = _version(
        tmp_path / "versions" / "v1", "old",
        junction_aware=True, updater_capable=False,
    )
    shared = old / "scripts/aqg_directory_links.py"
    shutil.copyfile(Path(directory_links.__file__), shared)
    assert shared.read_bytes() == Path(directory_links.__file__).read_bytes()
    current = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(current, root)

    with pytest.raises(stage.StageError, match="junction-aware"):
        stage.swap_root(root=root, target=old)
    assert stage.current_target(root) == current


def test_junction_rollback_refuses_a_linked_updater_capability_declaration(
    tmp_path, windows_links
):
    old = _version(tmp_path / "versions" / "v1", "old")
    declaration = old / UPDATER_CAPABILITIES
    declaration.unlink()
    external = tmp_path / "claimed-capabilities.json"
    external.write_text(
        json.dumps(JUNCTION_ROOT_CAPABILITY) + "\n", encoding="utf-8"
    )
    declaration.symlink_to(external)
    current = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(current, root)

    with pytest.raises(stage.StageError, match="junction-aware"):
        stage.swap_root(root=root, target=old)
    assert stage.current_target(root) == current


@pytest.mark.parametrize(
    "declaration",
    [
        "{not-json\n",
        json.dumps(
            {
                "schema": 2,
                "capabilities": {"windows_directory_junction_root": 1},
            }
        ),
        json.dumps(
            {
                "schema": 1,
                "capabilities": {"windows_directory_junction_root": 2},
            }
        ),
        (
            '{"schema":1,"schema":1,"capabilities":'
            '{"windows_directory_junction_root":1}}'
        ),
        json.dumps(
            {
                "schema": 1,
                "capabilities": {"windows_directory_junction_root": 1},
                "unrecognized": True,
            }
        ),
    ],
    ids=(
        "malformed",
        "unknown-schema",
        "unknown-capability-version",
        "duplicate-key",
        "extra-field",
    ),
)
def test_junction_rollback_refuses_invalid_updater_capability_declarations(
    tmp_path, windows_links, declaration
):
    old = _version(tmp_path / "versions" / "v1", "old")
    (old / UPDATER_CAPABILITIES).write_text(declaration, encoding="utf-8")
    current = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(current, root)

    with pytest.raises(stage.StageError, match="junction-aware"):
        stage.swap_root(root=root, target=old)
    assert stage.current_target(root) == current


def test_junction_rollback_to_a_declared_b_updater_preserves_junction(
    tmp_path, windows_links
):
    old = _version(tmp_path / "versions" / "v1", "old")
    current = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(current, root)

    assert stage.swap_root(root=root, target=old) == current
    assert windows_links.link_kind(root) == "junction"
    assert stage.current_target(root) == old


def test_root_target_change_before_replace_is_refused(tmp_path, windows_links, monkeypatch):
    old = _version(tmp_path / "versions" / "v1", "old")
    new = _version(tmp_path / "versions" / "v2", "new")
    intruder = _version(tmp_path / "versions" / "v3", "changed")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(old, root)
    real_create = stage._create_directory_link

    def create_then_change(source, target, *, kind):
        real_create(source, target, kind=kind)
        windows_links.remove_directory_link(
            root, expected_kind="junction", expected_target=old
        )
        windows_links.create_junction(intruder, root)

    monkeypatch.setattr(stage, "_create_directory_link", create_then_change)
    with pytest.raises(stage.StageError, match="changed while the swap"):
        stage.swap_root(root=root, target=new)
    assert stage.current_target(root) == intruder


def test_failed_atomic_replace_preserves_old_junction_and_cleans_temp(
    tmp_path, windows_links, monkeypatch
):
    old = _version(tmp_path / "versions" / "v1", "old")
    new = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(old, root)
    monkeypatch.setattr(
        stage, "_replace_root_link",
        lambda *_args: (_ for _ in ()).throw(OSError("rename unsupported")),
    )

    with pytest.raises(stage.StageError, match="rename unsupported"):
        stage.swap_root(root=root, target=new)
    assert stage.current_target(root) == old
    assert not list(tmp_path.glob(".aqg-root-*"))


def test_migration_stages_a_junction_before_the_target_exists(tmp_path, windows_links):
    root = _version(tmp_path / "agent-quality-gates", "new")
    versions = tmp_path / "versions"
    target = versions / "v1"

    migrate._perform(root, versions, target)

    assert windows_links.link_kind(root) == "junction"
    assert stage.current_target(root) == target
    assert target.is_dir()


def test_versions_reparse_is_never_followed_by_retention(tmp_path, windows_links):
    versions = tmp_path / "versions"
    windows_links.other_reparse.add(versions.absolute())

    with pytest.raises(stage.StageError, match="other_reparse"):
        stage.prune_versions(
            versions_dir=versions, keep=0, protected=(), repo=tmp_path
        )


def test_skill_route_uses_logical_root_and_survives_old_tree_retention(
    tmp_path, windows_links
):
    old = _version(tmp_path / "versions" / "v1", "old")
    new = _version(tmp_path / "versions" / "v2", "new")
    root = tmp_path / "agent-quality-gates"
    windows_links.create_junction(old, root)
    dest = tmp_path / "host" / "skills"
    resources = dispatch.Resources(
        target=new, root=root, skills_dest={"codex": dest}
    )
    action = plan.Action(
        kind="route_skill",
        client_id="codex",
        subject="aqg-code-construction",
        payload_class=3,
        detail="route",
    )

    outcomes = dispatch.execute(plan.Plan(actions=(action,)), resources=resources, apply=True)
    route = dest / "aqg-code-construction"
    assert outcomes[0].status == "applied"
    assert windows_links.read_link_target(route) == root / "skills" / action.subject
    stage.swap_root(root=root, target=new)
    shutil.rmtree(old)
    assert (route / "marker").read_text(encoding="utf-8") == "new"


def test_existing_skill_symlink_is_preserved_on_windows(tmp_path, windows_links):
    source = tmp_path / "agent-quality-gates" / "skills"
    (source / "aqg-code-construction").mkdir(parents=True)
    dest = tmp_path / "host" / "skills"
    dest.mkdir(parents=True)
    link = dest / "aqg-code-construction"
    link.symlink_to(source / link.name, target_is_directory=True)

    assert skills_route.route(name=link.name, source_root=source, dest_root=dest) is False
    assert windows_links.link_kind(link) == "symlink"
