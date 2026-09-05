"""Behavior contracts for the AQG multi-client installer wrapper."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from scripts import install_aqg_clients as installer


# Product identity fixtures (R24-02, R24-05, R24-06). Bundle ids are the ones
# verified on the 2026-09-03 read-only host inventory; a profile directory is
# never identity on its own.
MACOS_BUNDLES = {
    "qoder": ("Qoder.app", "com.qoder.app"),
    "qoder-cn": ("Qoder CN.app", "com.qodercn.app"),
    "trae": ("Trae.app", "com.trae.app"),
    "trae-cn": ("Trae CN.app", "cn.trae.app"),
    "trae-work": ("TRAE SOLO.app", "com.trae.solo.app"),
    "trae-work-cn": ("TRAE SOLO CN.app", "cn.trae.solo.app"),
}
QODER_BUNDLE_ALIASES = {
    "qoder": (
        ("Qoder.app", "com.qoder.app"),
        ("Qoder IDE.app", "com.qoder.ide"),
    ),
    "qoder-cn": (
        ("Qoder CN.app", "com.qodercn.app"),
        ("Qoder CN IDE.app", "com.aliyun.lingma.ide"),
    ),
}
DESKTOP_CLIENTS = tuple(MACOS_BUNDLES)


def _write_bundle(
    apps: Path,
    app_name: str,
    bundle_id: str | None,
    *,
    raw: bytes | None = None,
) -> Path:
    """Materialise an `<app>.app/Contents/Info.plist` fixture under ``apps``."""
    contents = apps / app_name / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    plist = contents / "Info.plist"
    if raw is not None:
        plist.write_bytes(raw)
        return plist
    with plist.open("wb") as handle:
        plistlib.dump(
            {
                "CFBundleIdentifier": bundle_id,
                "CFBundleName": app_name.removesuffix(".app"),
            },
            handle,
        )
    return plist


def _install_bundle(apps: Path, client_id: str) -> Path:
    app_name, bundle_id = MACOS_BUNDLES[client_id]
    return _write_bundle(apps, app_name, bundle_id)


@pytest.fixture
def app_dirs(tmp_path, monkeypatch) -> Path:
    """Point product-identity probing at a fixture Applications dir."""
    apps = tmp_path / "Applications"
    apps.mkdir()
    monkeypatch.setenv("AQG_APP_DIRS", str(apps))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)
    return apps


def _fake_spec(
    client_id: str,
    support_status: str,
    commands: tuple[str, ...] = ("echo install",),
    supports_no_hooks: bool = True,
) -> installer.ClientAdapterSpec:
    return installer.ClientAdapterSpec(
        client_id=client_id,
        support_status=support_status,
        supported_scopes=("user",),
        skills_source="skills/",
        skills_install_mode_default="link",
        installer_command=commands,
        verify_command=commands,
        uninstall_command=commands,
        is_installed_command=commands,
        rules_surface=("rules",),
        hooks_surface=("hooks",),
        hook_delivery="managed-merge",
        supports_no_hooks=supports_no_hooks,
        capability_evidence=("test evidence",),
        adapter_actions={
            "plan": commands,
            "apply": commands,
            "verify": commands,
            "uninstall": commands,
            "is-installed": commands,
        },
    )


def _expected_qoder_command(
    client_id: str,
    action: str,
    *,
    scope: str = "user",
) -> str:
    mode = " --mode link" if action in ("apply", "verify") else ""
    if scope == "project":
        return (
            'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" '
            f'--client {client_id} --scope project --project-root "$PROJECT_ROOT" '
            f'--aqg-root "$AQG_ROOT"{mode} --{action}'
        )
    return (
        'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" '
        f'--client {client_id} --scope user --aqg-root "$AQG_ROOT"{mode} --{action}'
    )


def test_dry_run_prints_plan_without_executing_commands(monkeypatch, capsys) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="git@example.test/repo.git",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex"])

    assert rc == 0
    assert executed == []
    out = capsys.readouterr().out
    assert "mode: dry-run" in out
    assert "supported lifecycle hooks install by default" in out
    assert '"$AQG_ROOT/scripts/install.sh" --force --no-hooks' in out
    assert "install_aqg_codex_hooks.py\" --apply" in out


@pytest.mark.parametrize(
    "client_id",
    [
        "cursor",
        "workbuddy",
        "codebuddy",
        "trae-work",
        "kimi-work",
        "kimi-code",
        "qoder-cli",
        "qoder-cli-cn",
        "qoder",
        "qoder-cn",
        "trae",
        "trae-cn",
        "trae-work-cn",
        "zed",
        "devin",
        "qoderwork",
        "qoderwake",
        "pi",
    ],
)
def test_wrapper_accepts_explicit_registry_clients(client_id: str) -> None:
    args = installer.parse_args(["--clients", client_id])

    assert installer.selected_clients(args) == (client_id,)


def test_core_expands_to_codex_and_claude_code() -> None:
    args = installer.parse_args(["--core"])

    assert installer.selected_clients(args) == ("codex", "claude-code")


def test_all_supported_expands_from_registry() -> None:
    args = installer.parse_args(["--all-supported"])

    assert installer.selected_clients(args) == (
        "codex",
        "claude-code",
        "cursor",
        "workbuddy",
        "codebuddy",
        "trae-work",
        "kimi-work",
        "kimi-code",
        "qoder-cli",
        "qoder-cli-cn",
        "qoder",
        "qoder-cn",
        "trae",
        "trae-cn",
        "trae-work-cn",
        "zed",
        "devin",
        "qoderwork",
        "qoderwake",
        "pi",
    )


def test_all_registry_alias_expands_from_registry() -> None:
    args = installer.parse_args(["--all-registry"])

    assert installer.selected_clients(args) == installer.registry_batch_clients()


@pytest.mark.parametrize(
    "argv",
    [
        ["--installed-supported", "--clients", "qoder"],
        ["--installed-supported", "--all-registry"],
        ["--installed-supported", "--all-supported"],
    ],
)
def test_installed_supported_cannot_be_combined_with_other_selection_modes(
    argv: list[str],
) -> None:
    with pytest.raises(SystemExit):
        installer.parse_args(argv)


def test_all_supported_uses_every_registry_adapter(monkeypatch) -> None:
    monkeypatch.setattr(
        installer,
        "CLIENT_REGISTRY",
        {
            "synthetic-supported": _fake_spec("synthetic-supported", "supported"),
            "synthetic-full": _fake_spec("synthetic-full", "full"),
            "synthetic-partial": _fake_spec("synthetic-partial", "partial"),
            "synthetic-unknown": _fake_spec("synthetic-unknown", "unknown"),
        },
    )
    args = installer.parse_args(["--all-supported"])

    assert installer.selected_clients(args) == (
        "synthetic-supported",
        "synthetic-full",
        "synthetic-partial",
        "synthetic-unknown",
    )


def test_all_supported_includes_partial_profiles() -> None:
    args = installer.parse_args(["--all-supported"])

    selected = installer.selected_clients(args)

    assert "qoder" in selected
    assert "qoder-cn" in selected
    assert "workbuddy" in selected
    assert "codebuddy" in selected
    assert "trae-work" in selected
    assert "trae" in selected
    assert "trae-cn" in selected
    assert "trae-work-cn" in selected
    assert "zed" in selected
    assert "devin" in selected
    assert "kimi-work" in selected
    assert "kimi-code" in selected
    assert "qoderwork" in selected
    assert "qoderwake" in selected
    assert "pi" in selected


def test_installed_supported_filters_to_detected_supported_clients(
    monkeypatch,
    tmp_path,
) -> None:
    home = tmp_path / "home"
    for dirname in (".claude", ".cursor", ".qoder", ".qoder-cn", ".lingma"):
        (home / dirname).mkdir(parents=True)
    for dirname in (
        ".workbuddy",
        ".codebuddy",
        ".trae",
        ".trae-cn",
        ".config/zed",
        ".config/devin",
        ".kimi-code",
        ".qoderwork",
        ".qoderwake",
        ".pi/agent",
    ):
        (home / dirname).mkdir(parents=True)
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    kimi_work_skills = tmp_path / "kimi-work-skills"
    kimi_work_skills.mkdir()
    monkeypatch.setenv("KIMI_WORK_SKILLS_ROOT", str(kimi_work_skills))
    monkeypatch.setattr(installer.shutil, "which", lambda name: None)

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    # R24-01/R24-04: Qoder- and Trae-family profiles are product-identity gated,
    # so a leftover config directory alone never selects them any more. Every
    # other client keeps its existing directory contract.
    assert installer.selected_clients(args) == (
        "codex",
        "claude-code",
        "cursor",
        "workbuddy",
        "codebuddy",
        "kimi-work",
        "kimi-code",
        "zed",
        "devin",
        "qoderwork",
        "qoderwake",
        "pi",
    )


def test_installed_supported_detects_zed_appdata_roaming_root(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    (home / "AppData" / "Roaming" / "Zed").mkdir(parents=True)

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("zed",)


@pytest.mark.parametrize("dirname", ["Zed", "zed"])
def test_installed_supported_detects_zed_env_appdata_root(
    dirname: str,
    monkeypatch,
    tmp_path,
) -> None:
    # Official evidence: Zed personal instructions on Windows live at
    # https://zed.dev/docs/ai/rules#user-rules, `%APPDATA%\Zed\AGENTS.md`.
    # Detection accepts a lowercase directory too; installation remains `Zed`.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (appdata / dirname).mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("zed",)


@pytest.mark.parametrize("binary_name", ["zed", "zed.exe"])
def test_installed_supported_detects_zed_path_binary(
    binary_name: str,
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: str(tmp_path / "bin" / binary_name) if name == binary_name else None,
    )
    home = tmp_path / "home"

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("zed",)


def test_installed_supported_detects_real_zed_exe_on_path(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    binary = bin_dir / "zed.exe"
    binary.write_text("", encoding="utf-8")
    try:
        binary.chmod(0o755)
    except OSError:
        pass
    monkeypatch.setenv("PATH", str(bin_dir))

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("zed",)


def test_installed_supported_detects_zed_config_fallback_root(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".config" / "zed").mkdir(parents=True)

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("zed",)


@pytest.mark.parametrize("dirname", ["devin", "Devin"])
def test_installed_supported_detects_devin_env_appdata_root(
    dirname: str,
    monkeypatch,
    tmp_path,
) -> None:
    # Official evidence: Devin CLI skills on Windows are under
    # https://docs.devin.ai/cli/extensibility/skills/creating-skills,
    # `%APPDATA%\devin\skills\<name>\SKILL.md`, not `~/.config/devin`.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (appdata / dirname).mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("devin",)


def test_installed_supported_detects_devin_home_dotdir(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    (home / ".devin").mkdir(parents=True)

    args = installer.parse_args(["--installed-supported", "--home", str(home)])

    assert installer.selected_clients(args) == ("devin",)


def test_installed_supported_does_not_treat_kimi_desktop_appdata_as_kimi_code(
    monkeypatch,
    tmp_path,
) -> None:
    # Official evidence: Kimi Code skills use `$KIMI_CODE_HOME/skills`,
    # defaulting to `~/.kimi-code/skills`; Kimi Desktop AppData is not CLI evidence.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (appdata / "kimi-desktop").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert "kimi-code" not in installer.installed_supported_clients(home)


def test_installed_supported_detects_kimi_work_env_skills_root(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    skills_root = tmp_path / "kimi-skills"
    skills_root.mkdir()
    monkeypatch.setenv("KIMI_WORK_SKILLS_ROOT", str(skills_root))

    detected = installer.installed_supported_clients(tmp_path / "home")

    assert detected == ("kimi-work",)


def test_installed_supported_detects_kimi_work_appdata_log(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    share = tmp_path / "KimiData" / "daimon-share"
    (share / "daimon").mkdir(parents=True)
    log = appdata / "kimi-desktop" / "logs" / "main.log"
    log.parent.mkdir(parents=True)
    log.write_text(f"install-drive seed: shareDir seeded to {share}\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))

    detected = installer.installed_supported_clients(home)

    assert detected == ("kimi-work",)
    assert "kimi-code" not in detected


def test_kimi_work_dry_run_prints_resolved_root_and_discovery_source(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    skills_root = tmp_path / "kimi-skills"
    skills_root.mkdir()
    monkeypatch.setenv("KIMI_WORK_SKILLS_ROOT", str(skills_root))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "kimi-work"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "client_id=kimi-work" in out
    assert f"resolved_skills_root={skills_root.resolve()}" in out
    assert "discovery_source=env KIMI_WORK_SKILLS_ROOT" in out
    assert "kimi-desktop[" not in out


@pytest.mark.parametrize(
    ("dirname", "forbidden_client"),
    [
        ("TRAE SOLO", "trae-work"),
        ("TRAE SOLO CN", "trae-work-cn"),
    ],
)
def test_installed_supported_does_not_auto_select_trae_work_from_solo_appdata(
    dirname: str,
    forbidden_client: str,
    monkeypatch,
    tmp_path,
) -> None:
    # Official evidence currently found for TRAE SOLO skills is the shared
    # `%USERPROFILE%/.trae/skills` root, not independent `.trae-work*` roots.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (appdata / dirname).mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))

    detected = installer.installed_supported_clients(home)

    assert forbidden_client not in detected
    assert "trae-work" not in detected
    assert "trae-work-cn" not in detected


def test_installed_supported_does_not_treat_cursor_appdata_as_cursor(
    monkeypatch,
    tmp_path,
) -> None:
    # Cursor support remains anchored to the existing `.cursor` contract unless
    # official evidence says the install root should move to AppData.
    monkeypatch.delenv("CODEX_HOME", raising=False)
    home = tmp_path / "home"
    appdata = home / "AppData" / "Roaming"
    (appdata / "Cursor").mkdir(parents=True)
    monkeypatch.setenv("APPDATA", str(appdata))

    assert "cursor" not in installer.installed_supported_clients(home)


def test_installed_supported_reports_no_clients_separately_from_usage_error(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setenv("PATH", "")

    rc = installer.main(["--installed-supported", "--home", str(tmp_path)])

    assert rc == installer.EXIT_NO_CLIENTS
    assert rc != installer.EXIT_USAGE
    assert "no locally installed supported AQG clients detected" in capsys.readouterr().err

def test_default_aqg_root_is_wrapper_checkout_not_ambient_env(monkeypatch) -> None:
    monkeypatch.setenv("AQG_ROOT", "/outside/checkout")
    monkeypatch.setattr(installer, "_git_value", lambda root, *args: "git-value")

    context = installer._collect_context(None)

    assert context.aqg_root == str(Path(installer.__file__).resolve().parent.parent)


def test_wrapper_rejects_unknown_clients(capsys) -> None:
    rc = installer.main(["--clients", "not-a-client"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "unknown client(s): not-a-client" in err
    assert "Accepted registry clients" in err


def test_all_supported_dry_run_outputs_every_registry_command(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--all-supported"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "mode: dry-run" in out
    assert (
        "- clients: codex[supported], claude-code[supported], "
        "cursor[supported], workbuddy[partial], codebuddy[full], "
        "trae-work[partial], kimi-work[partial], kimi-code[partial], "
        "qoder-cli[full], qoder-cli-cn[full], qoder[partial], "
        "qoder-cn[partial], trae[partial], trae-cn[partial], "
        "trae-work-cn[partial], zed[partial], devin[partial], "
        "qoderwork[partial], qoderwake[partial], pi[partial]"
    ) in out
    assert "- workbuddy: support_status=partial" in out
    assert "- kimi-work: support_status=partial" in out
    assert "- qoder: support_status=partial" in out
    assert "- qoder-cn: support_status=partial" in out
    assert "- trae: support_status=partial" in out
    assert "- zed: support_status=partial" in out
    assert "- devin: support_status=partial" in out
    assert "- pi: support_status=partial" in out
    assert "install.sh\" --force" in out
    assert "claude-code/install.sh\" --scope user --mode link --force" in out
    assert "install_cursor_support.py\" --apply --scope user --mode link" in out
    assert "install_aqg_work_clients.py\" --client codebuddy" in out
    assert "install_aqg_work_clients.py\" --client kimi-code" in out
    assert "install_aqg_qoder.py\" --client qoder-cli" in out
    assert "install_aqg_qoder.py\" --client qoder-cli-cn" in out
    assert "install_aqg_qoder.py\" --client qoder --scope project" in out
    assert "install_aqg_qoder.py\" --client qoder-cn --scope project" in out
    assert "install_aqg_agent_clients.py\" --client trae" in out
    assert "install_aqg_agent_clients.py\" --client zed" in out
    assert "install_aqg_agent_clients.py\" --client devin" in out
    assert "install_aqg_pi.py\" --scope user" in out


def test_apply_no_hooks_omits_hook_installers(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code", "--apply", "--no-hooks"])

    assert rc == 0
    assert executed == [
        '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for codex; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
        '"$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for claude-code; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
    ]


def test_apply_hooks_includes_hook_installers(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex", "--apply", "--hooks"])

    assert rc == 0
    assert executed == [
        '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for codex; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
    ]


def test_apply_defaults_to_hook_installers_for_supported_core_clients(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code", "--apply"])

    assert rc == 0
    assert executed == [
        '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for codex; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
        '"$AQG_ROOT/agent-packs/claude-code/install.sh" --scope user --mode link --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_hooks.py" --apply --aqg-root "$AQG_ROOT"',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client claude-code --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for claude-code; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
    ]


def test_apply_hook_failure_fails_the_client_without_propagating_child_code(
    monkeypatch,
) -> None:
    executed: list[str] = []

    def fail_on_codex_hooks(command: str, env: dict[str, str]) -> None:
        executed.append(command)
        if "install_aqg_codex_hooks.py" in command:
            raise subprocess.CalledProcessError(17, command)

    monkeypatch.setattr(installer, "_run_command", fail_on_codex_hooks)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex", "--apply"])

    assert rc == installer.EXIT_ALL_FAILED
    assert rc != 17
    assert executed == [
        '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"',
    ]


def test_apply_isolates_failure_to_the_failing_client(monkeypatch, capsys) -> None:
    executed: list[str] = []

    def fail_on_codex(command: str, env: dict[str, str]) -> None:
        executed.append(command)
        if "scripts/install.sh" in command:
            raise subprocess.CalledProcessError(9, command)

    monkeypatch.setattr(installer, "_run_command", fail_on_codex)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code", "--apply"])

    assert rc == installer.EXIT_PARTIAL_FAILURE
    # codex fails on its first command, so its hook install is skipped ...
    assert not any("install_aqg_codex_hooks.py" in command for command in executed)
    # ... while claude-code still gets fully installed.
    assert any("agent-packs/claude-code/install.sh" in command for command in executed)
    assert any("install_aqg_hooks.py" in command for command in executed)
    err = capsys.readouterr().err
    assert "[codex] command failed with exit 9" in err
    assert "continuing with the remaining clients" in err


def test_apply_returns_all_failed_when_every_client_fails(monkeypatch) -> None:
    def always_fail(command: str, env: dict[str, str]) -> None:
        raise subprocess.CalledProcessError(3, command)

    monkeypatch.setattr(installer, "_run_command", always_fail)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code", "--apply"])

    assert rc == installer.EXIT_ALL_FAILED


def test_apply_returns_ok_when_every_client_succeeds(monkeypatch) -> None:
    monkeypatch.setattr(installer, "_run_command", lambda command, env: None)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code,cursor", "--apply"])

    assert rc == installer.EXIT_OK


def test_apply_runs_selected_cursor_and_qoder_cli_commands(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "cursor,qoder-cli,qoder-cli-cn", "--apply"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link',
        'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder-cli --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder-cli-cn --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
    ]


def test_apply_runs_selected_work_and_code_client_commands(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codebuddy,kimi-code,qoderwork", "--apply"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client codebuddy --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client kimi-code --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client qoderwork --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
    ]


def test_apply_runs_selected_agent_client_commands(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "trae,zed,devin", "--apply"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client trae --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client zed --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client devin --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
    ]


def test_apply_runs_explicit_zed_devin_and_trae_work_clients(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "zed,devin,trae-work,trae-work-cn", "--apply"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client zed --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client devin --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client trae-work --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
        'python3 "$AQG_ROOT/scripts/install_aqg_agent_clients.py" --client trae-work-cn --scope user --aqg-root "$AQG_ROOT" --mode link --apply',
    ]


def test_apply_exports_selected_aqg_root_to_child_commands(monkeypatch) -> None:
    observed: dict[str, str] = {}

    def record_env(command: str, env: dict[str, str]) -> None:
        observed["command"] = command
        observed["aqg_root"] = env["AQG_ROOT"]

    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/selected/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "cursor", "--apply"])

    assert rc == 0
    assert observed["aqg_root"] == "/selected/repo"
    assert "install_cursor_support.py" in observed["command"]


def test_apply_exports_selected_project_root_to_child_commands(
    monkeypatch,
    tmp_path,
) -> None:
    executed: list[str] = []
    observed: dict[str, str] = {}

    def record_env(command: str, env: dict[str, str]) -> None:
        executed.append(command)
        observed["project_root"] = env["PROJECT_ROOT"]

    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(
        [
            "--clients",
            "qoder",
            "--apply",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert observed["project_root"] == str(tmp_path)
    # With a project root available the Desktop profile runs both surfaces:
    # the user-scope skills/hooks install and the separate project-rules command.
    assert executed == [
        _expected_qoder_command("qoder", "apply"),
        _expected_qoder_command("qoder", "apply", scope="project"),
    ]


@pytest.mark.parametrize("action_flag", ["--apply", "--verify", "--uninstall", "--is-installed"])
def test_installed_supported_execution_skips_project_scope_commands_without_project_root(
    action_flag: str,
    app_dirs: Path,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    # R24-09: without PROJECT_ROOT the Desktop profiles still run their user-scope
    # command; only the project-rules command is skipped, and the plan says so.
    executed: list[str] = []
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "qoder")

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    action = action_flag[2:]
    rc = installer.main(["--installed-supported", "--home", str(home), action_flag])

    assert rc == 0
    assert executed == [_expected_qoder_command("qoder", action)]
    out = capsys.readouterr().out
    assert "- skipped project-scope clients:" in out
    assert (
        "qoder: project-scope command(s) require --project-root "
        "/absolute/path/to/project" in out
    )


def test_installed_supported_apply_with_project_root_runs_project_scope_qoder(
    app_dirs: Path,
    monkeypatch,
    tmp_path,
) -> None:
    executed: list[str] = []
    observed: dict[str, str] = {}
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "qoder")
    _install_bundle(app_dirs, "qoder-cn")

    def record_env(command: str, env: dict[str, str]) -> None:
        executed.append(command)
        observed["project_root"] = env["PROJECT_ROOT"]

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(
        [
            "--installed-supported",
            "--home",
            str(home),
            "--apply",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert rc == 0
    assert observed["project_root"] == str(tmp_path)
    assert executed == [
        _expected_qoder_command("qoder", "apply"),
        _expected_qoder_command("qoder", "apply", scope="project"),
        _expected_qoder_command("qoder-cn", "apply"),
        _expected_qoder_command("qoder-cn", "apply", scope="project"),
    ]


def test_installed_supported_apply_with_env_project_root_runs_project_scope_qoder(
    app_dirs: Path,
    monkeypatch,
    tmp_path,
) -> None:
    executed: list[str] = []
    observed: dict[str, str] = {}
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "qoder")
    _install_bundle(app_dirs, "qoder-cn")

    def record_env(command: str, env: dict[str, str]) -> None:
        executed.append(command)
        observed["project_root"] = env["PROJECT_ROOT"]

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home), "--apply"])

    assert rc == 0
    assert observed["project_root"] == str(tmp_path)
    assert executed == [
        _expected_qoder_command("qoder", "apply"),
        _expected_qoder_command("qoder", "apply", scope="project"),
        _expected_qoder_command("qoder-cn", "apply"),
        _expected_qoder_command("qoder-cn", "apply", scope="project"),
    ]


def test_installed_supported_apply_with_empty_env_project_root_fails_closed(
    app_dirs: Path,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    executed: list[str] = []
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "qoder")

    monkeypatch.setenv("PROJECT_ROOT", "")
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home), "--apply"])

    assert rc == 2
    assert executed == []
    assert "PROJECT_ROOT required" in capsys.readouterr().err


def test_installed_supported_skips_only_the_commands_that_need_a_project_root(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    # R24-09: a client with both a user-scope and a project-scope command still
    # gets its user-scope install when PROJECT_ROOT is absent. Dropping the whole
    # client silently skipped the Desktop skills and hooks as well.
    executed: list[str] = []
    home = tmp_path / "home"
    (home / ".mixed-auto").mkdir(parents=True)
    fake = _fake_spec(
        "mixed-auto",
        "full",
        commands=("echo user-step", 'echo "$PROJECT_ROOT"'),
    )

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "CLIENT_REGISTRY", {"mixed-auto": fake})
    monkeypatch.setattr(
        installer,
        "CLIENT_DETECTION_CANDIDATES",
        {"mixed-auto": (installer._home_relative(".mixed-auto"),)},
    )
    monkeypatch.setattr(installer, "get_client", lambda client_id: fake)
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home), "--apply"])

    assert rc == 0
    assert executed == ["echo user-step"]
    out = capsys.readouterr().out
    assert (
        "mixed-auto: project-scope command(s) require --project-root "
        "/absolute/path/to/project" in out
    )
    assert 'echo "$PROJECT_ROOT"' not in out.split("- commands:")[1]


def test_clients_qoder_apply_without_project_root_still_fails_before_commands(
    monkeypatch,
    capsys,
) -> None:
    executed: list[str] = []

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "qoder", "--apply"])

    assert rc == 2
    assert executed == []
    assert "PROJECT_ROOT required" in capsys.readouterr().err


def test_all_registry_apply_without_project_root_still_fails_before_commands(
    monkeypatch,
    capsys,
) -> None:
    executed: list[str] = []

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--all-registry", "--apply"])

    assert rc == 2
    assert executed == []
    assert "PROJECT_ROOT required" in capsys.readouterr().err


def test_installed_supported_dry_run_shows_project_root_requirement_and_skip_notice(
    app_dirs: Path,
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    executed: list[str] = []
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "qoder")
    _install_bundle(app_dirs, "qoder-cn")

    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home)])

    assert rc == 0
    assert executed == []
    out = capsys.readouterr().out
    assert "- PROJECT_ROOT: <required for apply/verify/uninstall/is-installed>" in out
    assert "- skipped project-scope clients:" in out
    assert (
        "qoder: project-scope command(s) require --project-root "
        "/absolute/path/to/project" in out
    )
    assert (
        "qoder-cn: project-scope command(s) require --project-root "
        "/absolute/path/to/project" in out
    )
    # The user-scope Desktop commands are still planned; only the project ones go.
    assert _expected_qoder_command("qoder", "apply") in out
    assert _expected_qoder_command("qoder-cn", "apply") in out
    assert _expected_qoder_command("qoder", "apply", scope="project") not in out
    assert _expected_qoder_command("qoder-cn", "apply", scope="project") not in out


def test_apply_fails_before_commands_when_project_root_required(
    monkeypatch,
    capsys,
) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--all-supported", "--apply"])

    assert rc == 2
    assert executed == []
    assert "PROJECT_ROOT required" in capsys.readouterr().err


def test_invalid_ambient_project_root_is_ignored_when_not_required(
    monkeypatch,
) -> None:
    executed: list[str] = []

    monkeypatch.setenv("PROJECT_ROOT", "relative/stale/path")
    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex", "--apply"])

    assert rc == 0
    assert executed == [
        '"$AQG_ROOT/scripts/install.sh" --force --no-hooks',
        'python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply --aqg-root "$AQG_ROOT"',
        'python3 "$AQG_ROOT/scripts/install_aqg_rules.py" --client codex --apply --aqg-root "$AQG_ROOT" || echo "WARNING: AQG rules block not installed for codex; skills and hooks are on disk but Gate A will not fire until this is fixed - see the error above" >&2',
    ]


def test_env_project_root_satisfies_project_scope_commands(
    monkeypatch,
    tmp_path,
) -> None:
    observed: dict[str, str] = {}

    def record_env(command: str, env: dict[str, str]) -> None:
        observed["command"] = command
        observed["project_root"] = env["PROJECT_ROOT"]

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "qoder", "--apply"])

    assert rc == 0
    assert observed["project_root"] == str(tmp_path)
    assert "--project-root \"$PROJECT_ROOT\"" in observed["command"]


def test_project_scope_requirement_uses_registry_metadata(monkeypatch, tmp_path) -> None:
    fake = _fake_spec("metadata-project", "partial", commands=("echo install",))
    fake = installer.ClientAdapterSpec(
        **{
            **fake.__dict__,
            "supported_scopes": ("project",),
        }
    )

    monkeypatch.setattr(installer, "CLIENT_REGISTRY", {"metadata-project": fake})
    monkeypatch.setattr(installer, "get_client", lambda client_id: fake)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(
        [
            "--clients",
            "metadata-project",
            "--apply",
            "--project-root",
            str(tmp_path),
        ]
    )

    assert rc == 0


def test_uninstall_runs_selected_registry_uninstall_commands(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "cursor,qoder-cli", "--uninstall"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --uninstall --scope user',
        'python3 "$AQG_ROOT/scripts/install_aqg_qoder.py" --client qoder-cli --scope user --aqg-root "$AQG_ROOT" --uninstall',
    ]


def test_child_command_failure_reports_client_failure_not_child_code(monkeypatch) -> None:
    def fail(command: str, env: dict[str, str]) -> None:
        raise subprocess.CalledProcessError(7, command)

    monkeypatch.setattr(installer, "_run_command", fail)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "cursor", "--apply"])

    # Child return codes are never propagated: 7 would collide with a future
    # wrapper-level code and hide "one client failed" from decision-engine.
    assert rc == installer.EXIT_ALL_FAILED
    assert rc != 7


def test_empty_planned_command_set_fails_closed(monkeypatch, capsys) -> None:
    fake = _fake_spec("empty-client", "full", commands=())

    monkeypatch.setattr(installer, "CLIENT_REGISTRY", {"empty-client": fake})
    monkeypatch.setattr(installer, "get_client", lambda client_id: fake)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "empty-client", "--verify"])

    assert rc == 2
    assert "no commands available" in capsys.readouterr().err


def test_no_hooks_adds_cursor_flag_when_supported(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "cursor", "--apply", "--no-hooks"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_cursor_support.py" --apply --scope user --mode link --no-hooks',
    ]


def test_no_hooks_adds_work_client_flag_when_supported(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codebuddy", "--apply", "--no-hooks"])

    assert rc == 0
    assert executed == [
        'python3 "$AQG_ROOT/scripts/install_aqg_work_clients.py" --client codebuddy --scope user --aqg-root "$AQG_ROOT" --mode link --apply --no-hooks',
    ]


def test_all_supported_no_hooks_rejects_qoder(capsys) -> None:
    rc = installer.main(["--all-supported", "--apply", "--no-hooks"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "selected adapter(s) do not expose --no-hooks" in err
    assert "qoder-cli" in err and "qoder-cli-cn" in err
    assert "qoder" in err and "qoder-cn" in err


def test_no_hooks_rejects_qoder_full_profile_without_command_name_dependency(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        installer,
        "CLIENT_REGISTRY",
        {
            "qoder-cloud": _fake_spec(
                "qoder-cloud",
                "full",
                commands=("python3 custom_qoder_wrapper.py --apply",),
                supports_no_hooks=False,
            )
        },
    )

    with pytest.raises(installer.ClientSelectionError, match="do not expose --no-hooks"):
        installer._validate_hooks_request(
            ("qoder-cloud",),
            action="apply",
            no_hooks_requested=True,
        )


@pytest.mark.parametrize("client_id", ["qoder-cli", "qoder-cli-cn", "qoder", "qoder-cn"])
@pytest.mark.parametrize("action", ["--apply", "--verify", "--uninstall", "--is-installed"])
def test_no_hooks_rejects_qoder_without_inventing_flag(
    client_id: str,
    action: str,
    capsys,
) -> None:
    rc = installer.main(["--clients", client_id, action, "--no-hooks"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "selected adapter(s) do not expose --no-hooks" in err
    assert "--no-hooks --" not in err


@pytest.mark.parametrize(
    ("argv", "expected_fragment", "forbidden_fragment"),
    [
        (
            ["--clients", "codex", "--verify", "--hooks"],
            "install_aqg_codex_hooks.py\" --verify",
            "install_aqg_codex_hooks.py\" --apply",
        ),
        (
            ["--clients", "claude-code", "--is-installed", "--hooks"],
            "install_aqg_hooks.py\" --is-installed",
            "install_aqg_hooks.py\" --apply",
        ),
    ],
)
def test_verify_and_is_installed_select_read_only_commands(
    argv: list[str],
    expected_fragment: str,
    forbidden_fragment: str,
    monkeypatch,
) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(argv)

    assert rc == 0
    assert any(expected_fragment in command for command in executed)
    assert all(forbidden_fragment not in command for command in executed)


def test_default_verify_includes_core_hook_verification(monkeypatch) -> None:
    executed: list[str] = []

    monkeypatch.setattr(installer, "_run_command", lambda command, env: executed.append(command))
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--clients", "codex,claude-code", "--verify"])

    assert rc == 0
    assert executed
    assert any("install_aqg_codex_hooks.py" in command for command in executed)
    assert any("install_aqg_hooks.py" in command for command in executed)
    assert all("--apply" not in command for command in executed)


# --------------------------------------------------------------------------
# AQG-024 product-identity disambiguation (R24-01, R24-02, R24-04, R24-05, R24-06)
# --------------------------------------------------------------------------


def test_qoder_profile_dir_alone_selects_neither_desktop_nor_cli(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-01: `~/.qoder` is shared by Qoder Desktop and Qoder CLI, so on its own
    # it proves neither product. Selecting both would install CLI-only hooks
    # into the Desktop's settings surface.
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)

    detected = installer.installed_supported_clients(home)

    assert "qoder" not in detected
    assert "qoder-cli" not in detected


def test_qoder_cn_profile_dir_alone_selects_neither_desktop_nor_cli(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-01 (CN half): `~/.qoder-cn` is shared by Qoder CN Desktop and CLI CN.
    home = tmp_path / "home"
    (home / ".qoder-cn").mkdir(parents=True)

    detected = installer.installed_supported_clients(home)

    assert "qoder-cn" not in detected
    assert "qoder-cli-cn" not in detected


def test_qoder_desktop_bundle_selects_only_international_desktop(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-02: Qoder.app / com.qoder.app is the international Desktop and nothing else.
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    _install_bundle(app_dirs, "qoder")

    detected = installer.installed_supported_clients(home)

    assert "qoder" in detected
    assert "qoder-cli" not in detected
    assert "qoder-cn" not in detected
    assert "qoder-cli-cn" not in detected


def test_qoder_cn_desktop_bundle_selects_only_cn_desktop(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-02 (CN half): Qoder CN.app / com.qodercn.app is the CN Desktop only.
    home = tmp_path / "home"
    (home / ".qoder-cn").mkdir(parents=True)
    _install_bundle(app_dirs, "qoder-cn")

    detected = installer.installed_supported_clients(home)

    assert "qoder-cn" in detected
    assert "qoder" not in detected
    assert "qoder-cli-cn" not in detected


@pytest.mark.parametrize(
    ("client_id", "app_name", "bundle_id"),
    (
        (client_id, app_name, bundle_id)
        for client_id, products in QODER_BUNDLE_ALIASES.items()
        for app_name, bundle_id in products
    ),
)
def test_each_qoder_desktop_bundle_alias_selects_existing_profile_once(
    client_id: str,
    app_name: str,
    bundle_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    home = tmp_path / "home"
    _write_bundle(app_dirs, app_name, bundle_id)

    result = installer.detect_clients(home)

    assert result.selected.count(client_id) == 1
    assert app_name in result.evidence[client_id]
    assert bundle_id in result.evidence[client_id]


@pytest.mark.parametrize("client_id", tuple(QODER_BUNDLE_ALIASES))
def test_qoder_bundle_aliases_are_deduplicated_per_profile(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    home = tmp_path / "home"
    for app_name, bundle_id in QODER_BUNDLE_ALIASES[client_id]:
        _write_bundle(app_dirs, app_name, bundle_id)

    result = installer.detect_clients(home)

    assert result.selected.count(client_id) == 1


@pytest.mark.parametrize(
    ("client_id", "app_name", "bundle_id"),
    (
        ("qoder", "Qoder IDE.app", "com.qoder.wrong"),
        ("qoder-cn", "Qoder CN IDE.app", "com.aliyun.lingma.wrong"),
    ),
)
def test_qoder_ide_alias_with_wrong_bundle_id_is_rejected(
    client_id: str,
    app_name: str,
    bundle_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    home = tmp_path / "home"
    _write_bundle(app_dirs, app_name, bundle_id)

    detected = installer.installed_supported_clients(home)

    assert client_id not in detected


@pytest.mark.parametrize(
    ("client_id", "cli_client_id", "app_name", "bundle_id", "executable"),
    (
        ("qoder", "qoder-cli", "Qoder IDE.app", "com.qoder.ide", "qoder"),
        (
            "qoder-cn",
            "qoder-cli-cn",
            "Qoder CN IDE.app",
            "com.aliyun.lingma.ide",
            "qoder-cn",
        ),
    ),
)
def test_qoder_ide_alias_and_cli_evidence_still_fail_closed(
    client_id: str,
    cli_client_id: str,
    app_name: str,
    bundle_id: str,
    executable: str,
    app_dirs: Path,
    tmp_path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    _write_bundle(app_dirs, app_name, bundle_id)
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: f"/usr/local/bin/{executable}" if name == executable else None,
    )

    result = installer.detect_clients(home)

    assert client_id not in result.selected
    assert cli_client_id not in result.selected
    assert (client_id, cli_client_id) in result.conflicts


def test_detection_reports_the_bundle_evidence_it_used(
    app_dirs: Path,
    tmp_path,
) -> None:
    # WorkPacket 3.2.1: selection must be explainable, not just a client id list.
    home = tmp_path / "home"
    _install_bundle(app_dirs, "trae")

    result = installer.detect_clients(home)

    assert "trae" in result.selected
    assert "com.trae.app" in result.evidence["trae"]
    assert "Trae.app" in result.evidence["trae"]


def test_cli_runtime_evidence_selects_cli_profile_without_desktop(
    app_dirs: Path,
    tmp_path,
    monkeypatch,
) -> None:
    # R24-04: an actual `qoder` executable on PATH is CLI runtime evidence.
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/local/bin/qoder" if name == "qoder" else None,
    )

    detected = installer.installed_supported_clients(home)

    assert "qoder-cli" in detected
    assert "qoder" not in detected


def test_conflicting_desktop_and_cli_evidence_fails_closed(
    app_dirs: Path,
    tmp_path,
    monkeypatch,
) -> None:
    # R24-04: Desktop and CLI write different hook contracts into the same
    # `~/.qoder/settings.json`. With both proven, auto-selection must pick
    # neither and demand an explicit --clients choice.
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    _install_bundle(app_dirs, "qoder")
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/local/bin/qoder" if name == "qoder" else None,
    )

    result = installer.detect_clients(home)

    assert "qoder" not in result.selected
    assert "qoder-cli" not in result.selected
    assert ("qoder", "qoder-cli") in result.conflicts


def test_conflicting_desktop_and_cli_evidence_is_reported_to_the_operator(
    app_dirs: Path,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    # A fail-closed skip that says nothing is indistinguishable from "not installed".
    home = tmp_path / "home"
    (home / ".qoder").mkdir(parents=True)
    _install_bundle(app_dirs, "qoder")
    monkeypatch.setattr(
        installer.shutil,
        "which",
        lambda name: "/usr/local/bin/qoder" if name == "qoder" else None,
    )
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    installer.main(["--installed-supported", "--home", str(home)])

    err = capsys.readouterr().err
    assert "qoder" in err and "qoder-cli" in err
    assert "--clients" in err


@pytest.mark.parametrize("client_id", DESKTOP_CLIENTS)
def test_each_desktop_bundle_selects_only_its_own_product(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-05: the six Desktop bundles must not cross-select each other.
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, client_id)

    detected = installer.installed_supported_clients(home)

    assert client_id in detected
    assert set(detected) & set(DESKTOP_CLIENTS) == {client_id}


@pytest.mark.parametrize("client_id", DESKTOP_CLIENTS)
def test_wrong_bundle_id_under_right_app_name_is_not_identity(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-05: a renamed or impostor app must not inherit the product identity.
    home = tmp_path / "home"
    home.mkdir()
    app_name, _bundle_id = MACOS_BUNDLES[client_id]
    _write_bundle(app_dirs, app_name, "com.example.impostor")

    detected = installer.installed_supported_clients(home)

    assert client_id not in detected


@pytest.mark.parametrize("client_id", DESKTOP_CLIENTS)
def test_corrupt_bundle_metadata_is_not_identity(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-05: an unreadable Info.plist fails closed instead of guessing.
    home = tmp_path / "home"
    home.mkdir()
    app_name, _bundle_id = MACOS_BUNDLES[client_id]
    _write_bundle(app_dirs, app_name, None, raw=b"\x00not-a-plist\xff")

    detected = installer.installed_supported_clients(home)

    assert client_id not in detected


@pytest.mark.parametrize("client_id", DESKTOP_CLIENTS)
def test_leftover_profile_dir_without_bundle_is_not_identity(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-05: uninstalling the app leaves the dot-directory behind; that residue
    # must not keep reporting the product as installed.
    home = tmp_path / "home"
    for dirname in (".qoder", ".qoder-cn", ".lingma", ".trae", ".trae-cn"):
        (home / dirname).mkdir(parents=True)

    detected = installer.installed_supported_clients(home)

    assert client_id not in detected


@pytest.mark.parametrize("client_id", ("trae-work", "trae-work-cn"))
def test_trae_solo_bundles_are_detected_as_work_profiles(
    client_id: str,
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-06: TRAE SOLO / SOLO CN are real, separately installable products.
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, client_id)

    detected = installer.installed_supported_clients(home)

    assert client_id in detected


def test_trae_and_solo_bundles_select_both_shared_root_profiles(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-06/R24-08: Trae and TRAE SOLO share `~/.trae/skills` but are distinct
    # products; both must be selected, not deduplicated away.
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, "trae")
    _install_bundle(app_dirs, "trae-work")

    detected = installer.installed_supported_clients(home)

    assert "trae" in detected
    assert "trae-work" in detected
    assert "trae-cn" not in detected
    assert "trae-work-cn" not in detected


@pytest.mark.parametrize("client_id", ("trae-work", "trae-work-cn"))
def test_detected_solo_profiles_are_planned_through_the_agent_client_adapter(
    client_id: str,
    app_dirs: Path,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    # R24-06: detection alone is not enough — a detected SOLO profile must be
    # planned through the agent-client adapter that owns the shared `.trae`
    # roots, not the retired work-client adapter that writes `.trae-work*`.
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, client_id)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home)])

    assert rc == 0
    out = capsys.readouterr().out
    planned = [line for line in out.splitlines() if f"[{client_id}]" in line]
    assert planned, out
    assert all("install_aqg_agent_clients.py" in line for line in planned)
    assert all("install_aqg_work_clients.py" not in line for line in planned)


def test_plan_reports_the_detection_evidence_for_each_selected_product(
    app_dirs: Path,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    # WorkPacket 3.2.1: `--installed-supported` must show the evidence it used,
    # so an operator can tell a proven product from a leftover directory.
    home = tmp_path / "home"
    (home / ".cursor").mkdir(parents=True)
    _install_bundle(app_dirs, "trae")
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home)])

    assert rc == 0
    out = capsys.readouterr().out
    assert "detection evidence:" in out
    evidence_line = next(
        line for line in out.splitlines() if line.strip().startswith("trae:")
    )
    assert "com.trae.app" in evidence_line
    assert "Trae.app" in evidence_line


@pytest.mark.parametrize("client_id", ("qoder", "qoder-cn"))
def test_qoder_desktop_user_install_is_not_skipped_without_project_root(
    client_id: str,
    app_dirs: Path,
    tmp_path,
    monkeypatch,
    capsys,
) -> None:
    # R24-09: the Desktop profiles own a real user-scope surface (16 skills plus
    # the partial hook set). Missing PROJECT_ROOT may skip project rules; it must
    # not drop the whole client from the plan.
    home = tmp_path / "home"
    home.mkdir()
    _install_bundle(app_dirs, client_id)
    monkeypatch.delenv("PROJECT_ROOT", raising=False)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo",
            git_remote="origin",
            git_commit="abc123",
        ),
    )

    rc = installer.main(["--installed-supported", "--home", str(home)])

    assert rc == 0
    out = capsys.readouterr().out
    planned = [line for line in out.splitlines() if f"[{client_id}]" in line]
    assert planned, out
    # The user-scope install is still planned ...
    assert any("--scope user" in line for line in planned)
    # ... and only the project-rules command was dropped.
    assert all("$PROJECT_ROOT" not in line for line in planned)
    assert "project-scope command(s) require --project-root" in out


def test_non_target_clients_keep_their_directory_detection_contract(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-11: only the Qoder/Trae families move to product-identity gating.
    home = tmp_path / "home"
    for dirname in (
        ".claude",
        ".cursor",
        ".workbuddy",
        ".codebuddy",
        ".kimi-code",
        ".qoderwork",
        ".qoderwake",
        ".config/zed",
        ".config/devin",
        ".pi/agent",
    ):
        (home / dirname).mkdir(parents=True)

    detected = installer.installed_supported_clients(home)

    for client_id in (
        "claude-code",
        "cursor",
        "workbuddy",
        "codebuddy",
        "kimi-code",
        "qoderwork",
        "qoderwake",
        "zed",
        "devin",
        "pi",
    ):
        assert client_id in detected, detected


def test_desktop_bundle_evidence_only_adds_its_own_product_to_the_selection(
    app_dirs: Path,
    tmp_path,
) -> None:
    # R24-11: introducing product-identity evidence must be purely additive for
    # every client outside the Qoder/Trae families.
    home = tmp_path / "home"
    for dirname in (".claude", ".cursor", ".codebuddy", ".pi/agent"):
        (home / dirname).mkdir(parents=True)

    baseline = installer.installed_supported_clients(home)
    _install_bundle(app_dirs, "trae")
    after = installer.installed_supported_clients(home)

    assert set(after) - set(baseline) == {"trae"}
    assert set(baseline) - set(after) == set()


# --------------------------------------------------------------------------
# AQG-024 host-recovery blocker 1 (2026-09-03 incident report): --home must
# constrain the real child-adapter execution environment, not just detection
# and display. Independent Test Owner found --apply with --home still wrote
# to the real ~/.qoder* and ~/.trae* because _execution_env() never set HOME.
# --------------------------------------------------------------------------


def test_execution_env_overrides_home_for_child_processes(tmp_path) -> None:
    fixture_home = tmp_path / "fixture-home"
    fixture_home.mkdir()
    context = installer.RunContext(aqg_root="/repo", git_remote="origin", git_commit="abc123")

    env = installer._execution_env(context, None, fixture_home)

    assert env["HOME"] == str(fixture_home)


@pytest.mark.parametrize("action_flag", ["--apply", "--verify", "--uninstall", "--is-installed"])
def test_explicit_clients_execution_env_carries_the_given_home(
    action_flag: str,
    monkeypatch,
    tmp_path,
) -> None:
    fixture_home = tmp_path / "fixture-home"
    fixture_home.mkdir()
    observed: dict[str, str] = {}

    def record_env(command: str, env: dict[str, str]) -> None:
        observed["home"] = env["HOME"]

    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo", git_remote="origin", git_commit="abc123"
        ),
    )

    rc = installer.main(["--clients", "cursor", action_flag, "--home", str(fixture_home)])

    assert rc == 0
    assert observed["home"] == str(fixture_home)


@pytest.mark.parametrize("action_flag", ["--apply", "--verify", "--uninstall", "--is-installed"])
def test_installed_supported_execution_env_carries_the_given_home(
    action_flag: str,
    app_dirs: Path,
    monkeypatch,
    tmp_path,
) -> None:
    fixture_home = tmp_path / "fixture-home"
    fixture_home.mkdir()
    _install_bundle(app_dirs, "trae")
    observed: dict[str, str] = {}

    def record_env(command: str, env: dict[str, str]) -> None:
        observed["home"] = env["HOME"]

    monkeypatch.setattr(installer, "_run_command", record_env)
    monkeypatch.setattr(
        installer,
        "_collect_context",
        lambda value: installer.RunContext(
            aqg_root="/repo", git_remote="origin", git_commit="abc123"
        ),
    )

    rc = installer.main(
        ["--installed-supported", "--home", str(fixture_home), action_flag]
    )

    assert rc == 0
    assert observed["home"] == str(fixture_home)


def test_apply_with_synthetic_home_never_touches_the_real_home(
    monkeypatch,
    tmp_path,
    capsys,
) -> None:
    # A decoy HOME is monkeypatched before invoking main() so that, if the fix
    # regresses and the wrapper falls back to the real process HOME, the leak
    # lands in this decoy sentinel instead of the developer's actual home. No
    # child command is mocked here: this exercises the real
    # install_aqg_agent_clients.py subprocess end to end.
    decoy_home = tmp_path / "decoy-real-home"
    decoy_home.mkdir()
    fixture_home = tmp_path / "fixture-home"
    fixture_home.mkdir()
    monkeypatch.setenv("HOME", str(decoy_home))

    rc = installer.main(
        [
            "--clients",
            "trae",
            "--apply",
            "--no-hooks",
            "--home",
            str(fixture_home),
        ]
    )

    out_err = capsys.readouterr()
    assert rc == 0, out_err
    assert (fixture_home / ".trae" / "skills").is_dir()
    assert not (decoy_home / ".trae").exists()

    rc = installer.main(
        [
            "--clients",
            "trae",
            "--uninstall",
            "--home",
            str(fixture_home),
        ]
    )
    assert rc == 0, capsys.readouterr()
    assert not (decoy_home / ".trae").exists()
