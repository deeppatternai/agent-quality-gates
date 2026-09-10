"""Link ownership must survive AQG's managed entrance changing releases."""

from __future__ import annotations

import json
import builtins
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import install_cursor_support as cursor
from scripts import install_aqg_work_clients as work
from scripts import install_aqg_qoder as qoder
from scripts import install_aqg_agent_clients as agents
from scripts import install_aqg_pi as pi
from scripts.aqg_skill_install import (
    same_skill_source, skill_link_source, create_windows_junction, classify_install,
)


@pytest.fixture
def layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    roots = [home / ".deeppattern" / "versions" / v for v in ("0.14.12", "0.14.13")]
    for root in roots:
        (root / "scripts").mkdir(parents=True)
        (root / "VERSION").write_text(root.name, encoding="utf-8")
        skill = root / "skills" / "aqg-example"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(root.name, encoding="utf-8")
    entrance = home / ".deeppattern" / "agent-quality-gates"
    try:
        entrance.symlink_to(roots[0], target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"managed update root requires directory symlink capability: {exc}")
    return entrance, roots, home / ".cursor"


def test_cursor_legacy_marker_verifies_after_swap_without_writes(layout):
    entrance, roots, client = layout
    source = entrance / "skills" / "aqg-example"
    target = client / "skills" / source.name
    target.parent.mkdir(parents=True)
    target.symlink_to(source, target_is_directory=True)
    marker = cursor._link_marker_path(target)
    marker.parent.mkdir()
    marker.write_text(json.dumps(cursor._marker_for(source, "link")), encoding="utf-8")
    # Explicitly retain the physical marker written by releases <= 0.14.13.
    data = json.loads(marker.read_text())
    data["source_path"] = str(roots[0] / "skills" / source.name)
    marker.write_text(json.dumps(data), encoding="utf-8")
    before = marker.read_bytes()
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    assert (target / "SKILL.md").read_text() == "0.14.13"
    assert cursor._is_current_managed_link(target, source, cursor._marker_for(source, "link"))
    assert marker.read_bytes() == before


def install(family, source, client):
    target = client / "skills" / source.name
    if family == "cursor":
        cursor._install_skill(source, target.parent, client, "link")
    elif family == "work":
        work._install_skill(source, target.parent, client, requested_mode="link",
                            effective_mode="link", target_client="workbuddy",
                            discovery_source="client root skills directory")
    elif family == "qoder":
        qoder._install_skill(source, target, qoder._tree_digest(source))
    else:
        {"agents": agents, "pi": pi}[family]._install_skill(source, target, client, "link")
    return target


def verify(family, source, target):
    if family == "cursor":
        return cursor._is_current_managed_link(target, source, cursor._marker_for(source, "link"))
    if family == "work":
        return work._skill_current(source, target.parent, profile=work.PROFILES["workbuddy"],
                                   requested_mode="link", discovery_source="client root skills directory")[0]
    if family == "qoder":
        return qoder._installed_skill_ok(target, source, qoder._tree_digest(source))
    if family == "pi":
        return pi._skill_current(source, target, "link")
    paths = agents.ClientPaths(target.parent.parent, None, target.parent, None, None)
    return agents._verify(SimpleNamespace(scope="user", no_hooks=True),
                          agents.PROFILES["trae"], paths, source.parent.parent) == 0


@pytest.mark.parametrize("family,legacy", [(f, False) for f in ["cursor", "work", "qoder", "agents", "pi"]]
                         + [(f, True) for f in ["cursor", "work", "qoder"]])
def test_installed_link_survives_release_swap(layout, family, legacy):
    entrance, roots, client = layout
    source = entrance / "skills" / "aqg-example"
    target = install(family, source, client)
    marker = client / "managed-links" / f"{source.name}.json"
    if legacy:
        data = json.loads(marker.read_text())
        for key in ("source", "source_path"):
            if key in data:
                data[key] = str(roots[0] / "skills" / source.name)
        marker.write_text(json.dumps(data), encoding="utf-8")
        # Reproduce the observed machine: physical marker, entrance-spelled link.
        target.unlink()
        target.symlink_to(source, target_is_directory=True)
    before = marker.read_bytes()
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    assert (target / "SKILL.md").read_text() == "0.14.13"
    assert verify(family, source, target)
    assert marker.read_bytes() == before
    # Retention removes the original version, then another release is activated.
    roots[0].rename(roots[0].parent.parent / "retired-release")
    third = roots[1].with_name("0.14.14")
    (third / "scripts").mkdir(parents=True)
    (third / "VERSION").write_text("0.14.14")
    (third / "skills" / source.name).mkdir(parents=True)
    (third / "skills" / source.name / "SKILL.md").write_text("0.14.14")
    entrance.unlink()
    entrance.symlink_to(third, target_is_directory=True)
    assert (target / "SKILL.md").read_text() == "0.14.14"
    assert verify(family, source, target)
    assert marker.read_bytes() == before


@pytest.mark.parametrize("family", ["cursor", "work", "qoder"])
@pytest.mark.parametrize("fault", ["wrong-link", "broken-link", "wrong-skill", "other-install", "unknown-label"])
def test_verification_rejects_unrelated_marker_or_target(layout, family, fault):
    entrance, roots, client = layout
    source = entrance / "skills" / "aqg-example"
    target = install(family, source, client)
    marker = client / "managed-links" / f"{source.name}.json"
    data = json.loads(marker.read_text())
    bad = roots[0] / "skills" / "aqg-other"
    if fault == "other-install":
        bad = client / "versions" / roots[0].name / "skills" / source.name
    elif fault == "unknown-label":
        bad = roots[0].with_name("unrelated") / "skills" / source.name
    if fault in {"wrong-link", "broken-link"}:
        if fault == "wrong-link":
            bad.mkdir()
            (bad / "SKILL.md").write_text("foreign skill")
        target.unlink()
        target.symlink_to(bad, target_is_directory=True)
    else:
        for key in ("source", "source_path"):
            if key in data:
                data[key] = str(bad)
        marker.write_text(json.dumps(data), encoding="utf-8")
    assert not verify(family, source, target)


def test_legacy_marker_cannot_escape_through_release_symlink(layout):
    entrance, roots, client = layout
    source = entrance / "skills" / "aqg-example"
    fake = roots[0].with_name("0.1.0")
    client.mkdir(parents=True)
    fake.symlink_to(client, target_is_directory=True)
    assert not same_skill_source(fake / "skills" / source.name, source)


def test_plain_checkout_does_not_accept_neighbor_versions(layout):
    _, roots, _ = layout
    source = roots[0] / "skills" / "aqg-example"
    assert same_skill_source(source, source)
    assert not same_skill_source(Path("relative/skills/aqg-example"), source)
    # Version switching is required; a plain checkout has no release lineage.
    entrance = roots[0].parent.parent / "agent-quality-gates"
    entrance.unlink()
    assert not same_skill_source(roots[1] / "skills" / source.name, source)


@pytest.mark.parametrize("error", [ImportError, ModuleNotFoundError])
def test_unavailable_release_validator_fails_closed(layout, monkeypatch, error):
    entrance, roots, _ = layout
    source = entrance / "skills" / "aqg-example"
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    original_import = builtins.__import__

    def unavailable(name, *args, **kwargs):
        if name in {"scripts.aqg_update.stage", "aqg_update.stage"}:
            raise error("release validator unavailable")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", unavailable)
    assert not same_skill_source(roots[0] / "skills" / source.name, source)
    assert same_skill_source(source, source)


def test_tilde_source_keeps_unmanaged_checkout_path(layout):
    _, _, client = layout
    source = client.parent / "project" / "skills" / "aqg-example"
    source.mkdir(parents=True)
    assert skill_link_source(Path("~/project/skills/aqg-example")) == source.resolve()


def test_legacy_marker_with_symlinked_home(layout, monkeypatch):
    entrance, roots, client = layout
    alias = client.parent.parent / "home-alias"
    alias.symlink_to(client.parent, target_is_directory=True)
    monkeypatch.setenv("HOME", str(alias))
    monkeypatch.setenv("USERPROFILE", str(alias))
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    source = alias / ".deeppattern/agent-quality-gates/skills/aqg-example"
    assert same_skill_source(roots[0] / "skills/aqg-example", source)


@pytest.mark.parametrize("family", ["cursor", "work", "qoder"])
def test_physically_pinned_old_link_is_still_drift(layout, family):
    entrance, roots, client = layout
    source = entrance / "skills/aqg-example"
    target = install(family, source, client)
    marker = client / "managed-links" / f"{source.name}.json"
    data = json.loads(marker.read_text())
    for key in ("source", "source_path"):
        if key in data:
            data[key] = str(roots[0] / "skills" / source.name)
    marker.write_text(json.dumps(data))
    target.unlink()
    target.symlink_to(roots[0] / "skills" / source.name, target_is_directory=True)
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    assert (target / "SKILL.md").read_text() == "0.14.12"
    assert not verify(family, source, target)


@pytest.mark.skipif(os.name != "nt", reason="native Windows directory junction")
@pytest.mark.parametrize("family", ["cursor", "work", "qoder"])
def test_windows_client_junction_with_legacy_marker(layout, family):
    entrance, roots, client = layout
    source = entrance / "skills/aqg-example"
    target = install(family, source, client)
    target.unlink()
    assert create_windows_junction(source, target)
    assert classify_install(target) == "junction" and not target.is_symlink()
    marker = client / "managed-links" / f"{source.name}.json"
    data = json.loads(marker.read_text())
    for key in ("source", "source_path"):
        if key in data:
            data[key] = str(roots[0] / "skills" / source.name)
    marker.write_text(json.dumps(data))
    before = marker.read_bytes()
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    roots[0].rename(roots[0].parent.parent / "retired-release")
    assert (target / "SKILL.md").read_text() == "0.14.13"
    assert verify(family, source, target)
    assert marker.read_bytes() == before


@pytest.mark.skipif(os.name != "nt", reason="native Windows path spelling")
def test_windows_case_and_extended_length_marker_paths(layout):
    entrance, roots, _ = layout
    source = entrance / "skills/aqg-example"
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    recorded = roots[0] / "skills/aqg-example"
    assert same_skill_source(Path(str(recorded).upper()), source)
    assert same_skill_source(Path("\\\\?\\" + str(recorded)), source)


@pytest.mark.skipif(os.name != "nt", reason="native Windows update entrance contract")
def test_junction_update_entrance_is_not_a_supported_swap_root(layout):
    entrance, roots, _ = layout
    entrance.unlink()
    assert create_windows_junction(roots[1], entrance)
    assert classify_install(entrance) == "junction" and entrance.resolve() == roots[1]
    assert not same_skill_source(roots[0] / "skills/aqg-example", entrance / "skills/aqg-example")


def test_qoder_uninstall_after_swap_preserves_foreign_link(layout, monkeypatch):
    entrance, roots, client = layout
    source = entrance / "skills/aqg-example"
    target = install("qoder", source, client)
    marker = client / "managed-links" / f"{source.name}.json"
    data = json.loads(marker.read_text())
    data["source"] = str(roots[0] / "skills" / source.name)
    marker.write_text(json.dumps(data))
    foreign_source = client.parent / "foreign-skill"
    foreign_source.mkdir()
    (foreign_source / "SKILL.md").write_text("user-owned")
    foreign = target.with_name("aqg-foreign")
    foreign.symlink_to(foreign_source, target_is_directory=True)
    qoder._write_owners(client, ["qoder-cli"])
    entrance.unlink()
    entrance.symlink_to(roots[1], target_is_directory=True)
    monkeypatch.setenv("AQG_BACKUP_DIR", str(client.parent / "backups"))
    qoder._open_backups(SimpleNamespace(scope="user", client="qoder-cli", project_root=None), client, entrance)
    try:
        assert qoder._uninstall(client, entrance, client="qoder-cli", scope="user") == 0
    finally:
        qoder._close_backups(ok=True)
    assert not target.exists() and not target.is_symlink()
    assert (source / "SKILL.md").read_text() == "0.14.13"
    assert (foreign / "SKILL.md").read_text() == "user-owned"
