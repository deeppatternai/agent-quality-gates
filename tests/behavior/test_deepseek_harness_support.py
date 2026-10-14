from __future__ import annotations

from pathlib import Path

import pytest

from scripts import aqg_client_registry as registry
from scripts import install_aqg_clients as clients
from scripts import install_aqg_deepseek_harness as adapter
from scripts import install_aqg_rules
from scripts import install_aqg_work_clients as work_clients


def _run(
    root: Path,
    dsh_home: Path,
    action: str,
    *,
    mode: str = "link",
    extra: tuple[str, ...] = (),
) -> int:
    return adapter.main(
        [
            "--aqg-root",
            str(root),
            "--dsh-home",
            str(dsh_home),
            "--mode",
            mode,
            action,
            *extra,
        ]
    )


def test_registry_declares_partial_user_scope_without_hooks() -> None:
    spec = registry.get_client("deepseek-harness")

    assert spec.support_status == "partial"
    assert spec.supported_scopes == ("user",)
    assert spec.hook_delivery == "none"
    assert spec.supports_no_hooks is True
    assert all("install_aqg_deepseek_harness.py" in command for command in spec.adapter_actions["apply"])
    assert "--apply" in spec.adapter_actions["apply"][0]
    assert "--verify" in spec.adapter_actions["verify"][0]
    assert "--uninstall" in spec.adapter_actions["uninstall"][0]
    assert "--is-installed" in spec.adapter_actions["is-installed"][0]


def test_installed_supported_requires_product_identity_not_an_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    dsh_home = home / ".dsh"
    dsh_home.mkdir(parents=True)
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    assert "deepseek-harness" not in clients.installed_supported_clients(home)

    (dsh_home / "cordis.patch.yml").write_text("patch: []\n", encoding="utf-8")
    assert "deepseek-harness" in clients.installed_supported_clients(home)


def test_installed_supported_honors_custom_dsh_home_and_path_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    custom = home / "custom-dsh"
    custom.mkdir(parents=True)
    (custom / "cordis.patch.yml").write_text("patch: []\n", encoding="utf-8")
    monkeypatch.setenv("DSH_HOME", str(custom))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    result = clients.detect_clients(home)
    assert "deepseek-harness" in result.selected
    assert str(custom) in result.evidence["deepseek-harness"]

    custom.joinpath("cordis.patch.yml").unlink()
    monkeypatch.setattr(
        clients.shutil,
        "which",
        lambda name: "/usr/local/bin/dsh" if name == "dsh" else None,
    )
    assert "deepseek-harness" in clients.installed_supported_clients(home)


def test_apply_verify_repeat_and_uninstall_preserve_foreign_bytes(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / "custom" / "dsh-home"
    dsh_home.mkdir(parents=True)
    rules = dsh_home / "AGENTS.md"
    prefix = b"# User rules\r\n\r\nKeep this byte-for-byte.\r\n"
    rules.write_bytes(prefix)
    foreign_skill = dsh_home / "skills" / "user-skill"
    foreign_skill.mkdir(parents=True)
    foreign_skill.joinpath("SKILL.md").write_text("foreign\n", encoding="utf-8")

    assert _run(root, dsh_home, "--apply") == 0
    first_rules = rules.read_bytes()
    assert first_rules.startswith(prefix)
    assert b"BEGIN AQG rules" in first_rules
    assert foreign_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"
    assert _run(root, dsh_home, "--verify") == 0
    assert _run(root, dsh_home, "--is-installed") == 0

    assert _run(root, dsh_home, "--apply") == 0
    assert rules.read_bytes() == first_rules

    assert _run(root, dsh_home, "--uninstall") == 0
    assert rules.read_bytes() == prefix
    assert foreign_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"
    assert not list((dsh_home / "skills").glob("aqg-*"))
    assert _run(root, dsh_home, "--is-installed") == 1


def test_rules_round_trip_preserves_file_without_terminal_newline(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"
    dsh_home.mkdir(parents=True)
    rules = dsh_home / "AGENTS.md"
    original = b"Personal instructions without a terminal newline"
    rules.write_bytes(original)

    assert _run(root, dsh_home, "--apply") == 0
    installed = rules.read_bytes()
    assert installed.startswith(original + b"\n")
    assert b"BEGIN AQG rules" in installed
    assert _run(root, dsh_home, "--verify") == 0
    assert _run(root, dsh_home, "--apply") == 0
    assert rules.read_bytes() == installed

    assert _run(root, dsh_home, "--uninstall") == 0
    assert rules.read_bytes() == original


@pytest.mark.parametrize(
    "replacement",
    (
        b"<!-- AQG-ADDED-SEPARATOR: INVALID -->",
        b"<!-- AQG-ADDED-SEPARATOR: LF -->\n<!-- AQG-ADDED-SEPARATOR: LF -->",
    ),
)
def test_separator_metadata_corruption_fails_closed(
    tmp_path: Path, replacement: bytes
) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"
    dsh_home.mkdir(parents=True)
    rules = dsh_home / "AGENTS.md"
    rules.write_bytes(b"Personal instructions without a terminal newline")

    assert _run(root, dsh_home, "--apply") == 0
    corrupted = rules.read_bytes().replace(
        b"<!-- AQG-ADDED-SEPARATOR: LF -->", replacement
    )
    rules.write_bytes(corrupted)

    assert _run(root, dsh_home, "--apply") == 1
    assert rules.read_bytes() == corrupted
    assert _run(root, dsh_home, "--uninstall") == 1
    assert rules.read_bytes() == corrupted


def test_copy_mode_round_trip_uses_owned_directories(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"

    assert _run(root, dsh_home, "--apply", mode="copy") == 0
    installed = list((dsh_home / "skills").glob("aqg-*"))
    assert installed
    assert all(path.is_dir() and not path.is_symlink() for path in installed)
    assert all((path / work_clients.MANAGED_MARKER).is_file() for path in installed)
    assert _run(root, dsh_home, "--verify", mode="copy") == 0

    assert _run(root, dsh_home, "--uninstall", mode="copy") == 0
    assert not list((dsh_home / "skills").glob("aqg-*"))


def test_no_hooks_is_an_explicit_supported_no_op(tmp_path: Path, capsys) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"

    assert _run(root, dsh_home, "--apply", extra=("--no-hooks",)) == 0
    output = capsys.readouterr().out
    assert "lifecycle hooks skipped by --no-hooks" in output
    assert _run(root, dsh_home, "--verify", extra=("--no-hooks",)) == 0


def test_shared_work_installer_rejects_project_scope(tmp_path: Path, capsys) -> None:
    root = Path(__file__).resolve().parents[2]

    rc = work_clients.main(
        [
            "--client",
            "deepseek-harness",
            "--scope",
            "project",
            "--project-root",
            str(tmp_path / "project"),
            "--aqg-root",
            str(root),
            "--apply",
        ]
    )

    assert rc == 1
    assert "supports user scope only" in capsys.readouterr().err


def test_shared_installers_restrict_explicit_client_root_to_deepseek(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = Path(__file__).resolve().parents[2]
    explicit = tmp_path / "explicit-client-root"

    work_rc = work_clients.main(
        [
            "--client",
            "workbuddy",
            "--scope",
            "user",
            "--client-root",
            str(explicit),
            "--aqg-root",
            str(root),
            "--apply",
        ]
    )
    rules_rc = install_aqg_rules.main(
        [
            "--client",
            "codex",
            "--client-root",
            str(explicit),
            "--aqg-root",
            str(root),
            "--apply",
        ]
    )

    assert work_rc == 1
    assert rules_rc == 1
    assert capsys.readouterr().err.count("reserved for deepseek-harness") == 2
    assert not explicit.exists()


def test_verify_rejects_managed_rule_drift(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"

    assert _run(root, dsh_home, "--apply") == 0
    rules = dsh_home / "AGENTS.md"
    rules.write_text(
        rules.read_text(encoding="utf-8").replace(
            "Agent Quality Gates (AQG) engineering discipline",
            "Agent Quality Gates (AQG) edited discipline",
        ),
        encoding="utf-8",
    )

    assert _run(root, dsh_home, "--verify") == 1


def test_apply_rejects_malformed_rule_marker_without_mutation(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"
    rules = dsh_home / "AGENTS.md"
    rules.parent.mkdir(parents=True)
    original = b"# Foreign\r\n<!-- BEGIN AQG rules (managed) -->\r\npartial\r\n"
    rules.write_bytes(original)

    assert _run(root, dsh_home, "--apply") == 1
    assert rules.read_bytes() == original


def test_apply_refuses_foreign_skill_link_without_ownership_marker(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"
    foreign = tmp_path / "foreign-skill"
    foreign.mkdir()
    foreign.joinpath("SKILL.md").write_text("foreign\n", encoding="utf-8")
    target = dsh_home / "skills" / "aqg-audit-adjudication"
    target.parent.mkdir(parents=True)
    target.symlink_to(foreign, target_is_directory=True)

    assert _run(root, dsh_home, "--apply") == 1
    assert target.is_symlink()
    assert target.resolve() == foreign.resolve()
    assert foreign.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"


def test_apply_refuses_foreign_aqg_skill(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    dsh_home = tmp_path / ".dsh"
    target = dsh_home / "skills" / "aqg-audit-adjudication"
    target.mkdir(parents=True)
    target.joinpath("SKILL.md").write_text("foreign\n", encoding="utf-8")

    assert _run(root, dsh_home, "--apply") == 1
    assert target.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"


def test_default_root_expands_home_and_custom_root_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("DSH_HOME", raising=False)
    assert adapter.resolve_dsh_home(None, None) == tmp_path / "home" / ".dsh"

    custom = tmp_path / "custom"
    monkeypatch.setenv("DSH_HOME", str(custom))
    assert adapter.resolve_dsh_home(None, None) == custom
    assert "~" not in adapter.resolve_dsh_home(None, None).parts

