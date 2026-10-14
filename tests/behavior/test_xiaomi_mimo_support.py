from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from scripts import aqg_client_registry as registry
from scripts import install_aqg_clients as clients
from scripts import install_aqg_rules
from scripts import install_aqg_work_clients as work_clients
from scripts import install_aqg_xiaomi_mimo as adapter


def _run(
    root: Path,
    config_root: Path,
    action: str,
    *,
    mode: str = "link",
    extra: tuple[str, ...] = (),
) -> int:
    return adapter.main(
        [
            "--aqg-root",
            str(root),
            "--config-root",
            str(config_root),
            "--mode",
            mode,
            action,
            *extra,
        ]
    )


def _write_mimo_bundle(apps: Path, bundle_id: str = "com.xiaomi.mimo.desktop") -> Path:
    contents = apps / "Xiaomi MiMo.app" / "Contents"
    contents.mkdir(parents=True)
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": bundle_id}, handle)
    return contents.parent


def test_registry_declares_partial_user_scope_without_hooks_or_mcp() -> None:
    spec = registry.get_client("xiaomi-mimo")

    assert spec.support_status == "partial"
    assert spec.supported_scopes == ("user",)
    assert spec.skills_install_mode_default == "link"
    assert spec.hook_delivery == "none"
    assert spec.supports_no_hooks is True
    assert all(
        "install_aqg_xiaomi_mimo.py" in command
        for command in spec.adapter_actions["apply"]
    )
    assert "--apply" in spec.adapter_actions["apply"][0]
    assert "--verify" in spec.adapter_actions["verify"][0]
    assert "--uninstall" in spec.adapter_actions["uninstall"][0]
    assert "--is-installed" in spec.adapter_actions["is-installed"][0]
    assert "No managed hooks" in spec.hooks_surface[0]
    assert all("mcp" not in command.lower() for command in spec.installer_command)


def test_config_root_precedence_and_absolute_mimocode_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    explicit = tmp_path / "explicit"
    mimocode_home = tmp_path / "mimocode-home"
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("MIMOCODE_HOME", str(mimocode_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))

    assert adapter.resolve_mimo_config_root(str(explicit), str(home)) == explicit
    assert adapter.resolve_mimo_config_root(None, str(home)) == mimocode_home / "config"

    monkeypatch.delenv("MIMOCODE_HOME")
    assert adapter.resolve_mimo_config_root(None, str(home)) == xdg / "mimocode"

    monkeypatch.delenv("XDG_CONFIG_HOME")
    assert adapter.resolve_mimo_config_root(None, str(home)) == home / ".config" / "mimocode"

    monkeypatch.setenv("MIMOCODE_HOME", "relative/mimo-home")
    with pytest.raises(adapter.InstallError, match="MIMOCODE_HOME must be absolute"):
        adapter.resolve_mimo_config_root(None, str(home))

    monkeypatch.delenv("MIMOCODE_HOME")
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/xdg")
    with pytest.raises(adapter.InstallError, match="XDG_CONFIG_HOME must be absolute"):
        adapter.resolve_mimo_config_root(None, str(home))


def test_installed_supported_requires_product_identity_not_an_empty_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_root = home / ".config" / "mimocode"
    config_root.mkdir(parents=True)
    monkeypatch.setenv("AQG_APP_DIRS", str(tmp_path / "Applications"))
    monkeypatch.delenv("MIMOCODE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    assert "xiaomi-mimo" not in clients.installed_supported_clients(home)

    (config_root / "mimocode.jsonc").write_text("{}\n", encoding="utf-8")
    result = clients.detect_clients(home)
    assert "xiaomi-mimo" in result.selected
    assert "mimocode.jsonc" in result.evidence["xiaomi-mimo"]


def test_installed_supported_detects_exact_macos_bundle_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    apps = tmp_path / "Applications"
    apps.mkdir()
    monkeypatch.setenv("AQG_APP_DIRS", str(apps))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    app = _write_mimo_bundle(apps, "com.example.foreign")
    assert "xiaomi-mimo" not in clients.installed_supported_clients(home)

    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump({"CFBundleIdentifier": "com.xiaomi.mimo.desktop"}, handle)
    result = clients.detect_clients(home)
    assert "xiaomi-mimo" in result.selected
    assert "Xiaomi MiMo.app" in result.evidence["xiaomi-mimo"]


def test_installed_supported_honors_mimocode_and_xdg_roots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    custom_home = home / "custom-mimo"
    custom_config = custom_home / "config"
    custom_config.mkdir(parents=True)
    (custom_config / "config.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("MIMOCODE_HOME", str(custom_home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("AQG_APP_DIRS", str(tmp_path / "Applications"))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    result = clients.detect_clients(home)
    assert "xiaomi-mimo" in result.selected
    assert str(custom_config / "config.json") in result.evidence["xiaomi-mimo"]

    monkeypatch.delenv("MIMOCODE_HOME")
    xdg = home / "xdg"
    xdg_config = xdg / "mimocode"
    xdg_config.mkdir(parents=True)
    (xdg_config / "mimocode.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    result = clients.detect_clients(home)
    assert "xiaomi-mimo" in result.selected
    assert str(xdg_config / "mimocode.json") in result.evidence["xiaomi-mimo"]


def test_installed_supported_uses_one_strict_precedence_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    xdg = home / "xdg"
    xdg_config = xdg / "mimocode"
    xdg_config.mkdir(parents=True)
    (xdg_config / "mimocode.jsonc").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("AQG_APP_DIRS", str(tmp_path / "Applications"))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    monkeypatch.setenv("MIMOCODE_HOME", str(home / "empty-mimo-home"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    assert "xiaomi-mimo" not in clients.installed_supported_clients(home)

    monkeypatch.setenv("MIMOCODE_HOME", "relative/mimo-home")
    assert "xiaomi-mimo" not in clients.installed_supported_clients(home)


def test_installed_supported_rejects_symlink_config_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    config_root = home / ".config" / "mimocode"
    config_root.mkdir(parents=True)
    foreign = tmp_path / "foreign.jsonc"
    foreign.write_text("{}\n", encoding="utf-8")
    (config_root / "mimocode.jsonc").symlink_to(foreign)
    monkeypatch.delenv("MIMOCODE_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("AQG_APP_DIRS", str(tmp_path / "Applications"))
    monkeypatch.setattr(clients.shutil, "which", lambda _name: None)

    assert "xiaomi-mimo" not in clients.installed_supported_clients(home)


def test_apply_verify_repeat_and_uninstall_preserve_foreign_bytes(
    tmp_path: Path,
) -> None:
    root = Path(__file__).resolve().parents[2]
    config_root = tmp_path / "mimocode"
    config_root.mkdir(parents=True)
    rules = config_root / "AGENTS.md"
    prefix = b"# User rules\r\n\r\nKeep this byte-for-byte.\r\n"
    rules.write_bytes(prefix)
    foreign_skill = config_root / "skills" / "user-skill"
    foreign_skill.mkdir(parents=True)
    foreign_skill.joinpath("SKILL.md").write_text("foreign\n", encoding="utf-8")
    config = config_root / "mimocode.jsonc"
    config_bytes = b'{\r\n  // foreign\r\n  "provider": {"token": "SYNTHETIC_SENTINEL"},\r\n}\r\n'
    config.write_bytes(config_bytes)

    assert _run(root, config_root, "--apply") == 0
    first_rules = rules.read_bytes()
    assert first_rules.startswith(prefix)
    assert b"BEGIN AQG rules" in first_rules
    assert config.read_bytes() == config_bytes
    assert foreign_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"
    assert _run(root, config_root, "--verify") == 0
    assert _run(root, config_root, "--is-installed") == 0

    assert _run(root, config_root, "--apply") == 0
    assert rules.read_bytes() == first_rules
    assert config.read_bytes() == config_bytes

    assert _run(root, config_root, "--uninstall") == 0
    assert rules.read_bytes() == prefix
    assert config.read_bytes() == config_bytes
    assert foreign_skill.joinpath("SKILL.md").read_text(encoding="utf-8") == "foreign\n"
    assert not list((config_root / "skills").glob("aqg-*"))
    assert _run(root, config_root, "--is-installed") == 1


def test_copy_fallback_mode_round_trip_uses_owned_directories(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config_root = tmp_path / "mimocode"

    assert _run(root, config_root, "--apply", mode="copy") == 0
    installed = list((config_root / "skills").glob("aqg-*"))
    assert installed
    assert all(path.is_dir() and not path.is_symlink() for path in installed)
    assert all((path / work_clients.MANAGED_MARKER).is_file() for path in installed)
    assert _run(root, config_root, "--verify", mode="copy") == 0

    assert _run(root, config_root, "--uninstall", mode="copy") == 0
    assert not list((config_root / "skills").glob("aqg-*"))


def test_no_hooks_is_an_explicit_supported_no_op(tmp_path: Path, capsys) -> None:
    root = Path(__file__).resolve().parents[2]
    config_root = tmp_path / "mimocode"

    assert _run(root, config_root, "--apply", extra=("--no-hooks",)) == 0
    assert "lifecycle hooks skipped by --no-hooks" in capsys.readouterr().out
    assert _run(root, config_root, "--verify", extra=("--no-hooks",)) == 0


def test_shared_installers_accept_explicit_mimo_config_root(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    config_root = tmp_path / "mimocode"

    work_rc = work_clients.main(
        [
            "--client",
            "xiaomi-mimo",
            "--scope",
            "user",
            "--client-root",
            str(config_root),
            "--aqg-root",
            str(root),
            "--apply",
        ]
    )
    rules_rc = install_aqg_rules.main(
        [
            "--client",
            "xiaomi-mimo",
            "--client-root",
            str(config_root),
            "--aqg-root",
            str(root),
            "--apply",
        ]
    )

    assert work_rc == 0
    assert rules_rc == 0


def test_shared_work_installer_rejects_project_scope(tmp_path: Path, capsys) -> None:
    root = Path(__file__).resolve().parents[2]

    rc = work_clients.main(
        [
            "--client",
            "xiaomi-mimo",
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
