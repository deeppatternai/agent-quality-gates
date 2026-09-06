"""Keep entry-point docs aligned with shipped client installer contracts."""

from pathlib import Path
import re
import runpy
import subprocess
import sys

import pytest


REPO = Path(__file__).resolve().parents[2]
ENTRY_DOCS = (
    "README.md",
    "README.zh-CN.md",
    "AI_SETUP.md",
    "AI_SETUP.zh-CN.md",
)
EXPECTED_QODER_LEVELS = {
    "qoder": "partial",
    "qoder-cli": "full",
    "qoder-cn": "partial",
    "qoder-cli-cn": "full",
}
INSTALLER_FLAGS = {
    "install_aqg_hooks.py": {"--apply", "--verify", "--uninstall", "--is-installed"},
    "install_aqg_codex_hooks.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--aqg-root",
    },
    "install_cursor_support.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--scope",
        "--project-root",
        "--mode",
        "--no-hooks",
    },
    "install_aqg_qoder.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--client",
        "--scope",
        "--project-root",
        "--aqg-root",
        "--mode",
    },
    "install_aqg_work_clients.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--client",
        "--scope",
        "--project-root",
        "--home",
        "--aqg-root",
        "--mode",
        "--skills-root",
        "--strict-link",
        "--no-hooks",
    },
    "install_aqg_agent_clients.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--client",
        "--scope",
        "--project-root",
        "--home",
        "--aqg-root",
        "--mode",
        "--no-hooks",
    },
    "install_aqg_pi.py": {
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--scope",
        "--project-root",
        "--home",
        "--aqg-root",
        "--mode",
        "--no-hooks",
    },
    "install_aqg_clients.py": {
        "--clients",
        "--core",
        "--all-registry",
        "--all-supported",
        "--installed-supported",
        "--project-root",
        "--home",
        "--apply",
        "--verify",
        "--uninstall",
        "--is-installed",
        "--hooks",
        "--no-hooks",
        "--aqg-root",
    },
}


def _text(relative_path: str) -> str:
    return (REPO / relative_path).read_text(encoding="utf-8")


def test_qoder_profile_levels_match_implementation_and_readme_matrices() -> None:
    namespace = runpy.run_path(str(REPO / "scripts" / "install_aqg_qoder.py"))
    actual = {name: profile["level"] for name, profile in namespace["PROFILES"].items()}
    assert actual == EXPECTED_QODER_LEVELS

    expected_rows = {
        "README.md": (
            "| Qoder CLI / Qoder CLI CN (`qoder-cli` / `qoder-cli-cn`) | full |",
            "| Qoder IDE / Tongyi Lingma (`qoder` / `qoder-cn`) | partial |",
        ),
        "README.zh-CN.md": (
            "| Qoder CLI / Qoder CLI CN（`qoder-cli` / `qoder-cli-cn`）| full |",
            "| Qoder IDE / 通义灵码（`qoder` / `qoder-cn`）| partial |",
        ),
    }
    for relative_path, rows in expected_rows.items():
        text = _text(relative_path)
        assert all(row in text for row in rows)
        assert "`SessionStart`" in text and "`PreCompact`" in text and "WIP save/recover" in text


@pytest.mark.parametrize("installer, flags", INSTALLER_FLAGS.items())
def test_documented_installers_exist_and_expose_flags(installer: str, flags: set[str]) -> None:
    path = REPO / "scripts" / installer
    assert path.is_file()
    result = subprocess.run(
        [sys.executable, str(path), "--help"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    help_flags = {token.rstrip(",") for token in result.stdout.split()}
    assert flags <= help_flags


@pytest.mark.parametrize("relative_path", ENTRY_DOCS)
def test_entry_docs_reference_shipped_support_installers(relative_path: str) -> None:
    text = _text(relative_path)
    assert "install_cursor_support.py" in text
    assert "install_aqg_qoder.py" in text
    assert "install_aqg_work_clients.py" in text
    assert "install_aqg_agent_clients.py" in text
    assert "install_aqg_pi.py" in text
    for action in ("--apply", "--verify", "--uninstall", "--is-installed"):
        assert action in text


@pytest.mark.parametrize("relative_path", ("AI_SETUP.md", "AI_SETUP.zh-CN.md"))
def test_ai_setup_commands_lock_scope_profile_and_hook_boundaries(relative_path: str) -> None:
    text = _text(relative_path)
    assert 'QODER_CLIENT="$CURRENT_CLIENT"' in text
    assert 'case "$QODER_CLIENT" in qoder-cli|qoder-cli-cn)' in text
    assert 'PROJECT_ROOT="/absolute/path/to/project"' in text
    assert 'test -d "$PROJECT_ROOT"' in text
    assert "--apply --scope user --mode link" in text
    assert "--apply --scope project --project-root \"$PROJECT_ROOT\" --mode link" in text
    assert "--client \"$QODER_CLIENT\" --scope user --aqg-root \"$AQG_ROOT\" --mode link --apply" in text
    assert "--scope project --project-root \"$PROJECT_ROOT\" --aqg-root \"$AQG_ROOT\" --mode link --apply" in text
    assert "--no-hooks" in text
    assert "no --no-hooks mode" in text or "没有\n# --no-hooks 模式" in text
    assert "`qoder` / `qoder-cn` | partial" in text
    assert "`SessionStart`" in text and "`PreCompact`" in text and "WIP save/recover" in text
    assert re.search(r"2[^\n]*blocking preToolUse", text)


@pytest.mark.parametrize("relative_path", ("AI_SETUP.md", "AI_SETUP.zh-CN.md"))
def test_ai_setup_defaults_to_installed_supported_and_registry_first(relative_path: str) -> None:
    text = _text(relative_path)

    assert "CURRENT_CLIENT" in text
    assert "SUPPORTED_CLIENTS" in text
    assert "SELECTED_CLIENTS" in text
    assert "installed-supported" in text
    assert "INSTALL_MODE=installed-supported" in text
    assert "--installed-supported" in text
    assert "INSTALL_MODE=multi-client-all" in text
    assert "--installed-supported" in text
    assert "--all-registry" in text
    assert "scripts/aqg_client_registry.py" in text
    assert "scripts/install_aqg_clients.py" in text
    assert "support_status" in text
    assert "supported" in text and "full" in text and "partial" in text
    assert "unsupported" in text and "unknown" in text
    assert "select exactly one adapter" not in text
    assert "current-host-only" not in text
    assert "only the current client" not in text
    assert "Config directories prove only local presence" in text or "配置目录" in text

@pytest.mark.parametrize("relative_path", ("AI_SETUP.md", "AI_SETUP.zh-CN.md"))
def test_ai_setup_uses_persistent_checkout_and_codex_hook_activation_contract(relative_path: str) -> None:
    text = _text(relative_path)

    assert "$HOME/.deeppattern/agent-quality-gates" in text
    assert "C:\\Users\\<user>\\.deeppattern\\agent-quality-gates" in text
    assert ".deeppatternai" in text
    assert "not `.deeppatternai`" in text or "不是 `.deeppatternai`" in text
    assert "AQG_ROOT=$HOME/.deeppattern/agent-quality-gates" in text
    assert "current workspace" in text or "当前 workspace" in text
    assert "Documents/Codex" in text
    assert "~/.codex/hooks.json" in text
    assert "~/.codex/hooks/" in text
    assert "scripts/run_aqg_codex_hook.py" in text
    assert "agent-packs/claude-code/hooks/*.sh" in text
    assert "on-disk" in text or "落盘" in text
    assert "runtime discovery" in text
    assert "/hooks" in text and "trust" in text
    assert "Codex version" in text or "Codex 版本" in text


def test_codex_rules_template_matches_installed_user_prompt_submit_hook() -> None:
    text = _text("examples/aqg-codex-agents.example.md")
    assert "does not install a `UserPromptSubmit` handoff hook" not in text
    assert "managed `UserPromptSubmit` handoff-routing hook" in text


@pytest.mark.parametrize("relative_path", ("README.md", "README.zh-CN.md"))
def test_readmes_document_explicit_multi_client_wrapper_contract(relative_path: str) -> None:
    text = _text(relative_path)

    assert "scripts/aqg_client_registry.py" in text
    assert "installed-supported" in text
    assert "scripts/install_aqg_clients.py" in text
    assert "--installed-supported" in text
    assert "--all-registry" in text
    assert "--all-supported" in text
    assert "--project-root" in text
    assert "registry adapter" in text or "registry client" in text
    assert "support-status" in text or "support_status" in text
    assert 'python3 "$AQG_ROOT/scripts/install_aqg_clients.py" --all-registry --project-root /path/to/project --apply' in text
    assert (
        'python3 "$AQG_ROOT/scripts/install_aqg_clients.py" '
        "--clients codex,claude-code,cursor,qoder-cli --apply"
    ) in text
    assert "dry-run" in text
    assert "--apply" in text
    assert "--verify" in text
    assert "--uninstall" in text
    assert "--is-installed" in text
    assert "--core" in text
    assert "codex,claude-code" in text
    assert "`qoder`" in text and "`qoder-cn`" in text
    assert "project-scope" in text.lower()
    assert "link/symlink" in text
    assert "--copy" in text
    assert "--mode copy" in text
    assert "no `--no-hooks`" in text or "没有 `--no-hooks`" in text
    assert "rejects Qoder-family profiles" in text or "遇到 Qoder 家族 profiles 会拒绝" in text


def test_qoder_cn_desktop_root_is_qoder_cn_across_code_and_docs() -> None:
    # R24-12: `.lingma` is the legacy Tongyi Lingma root. Qoder CN Desktop
    # (`com.qodercn.app`) reads `~/.qoder-cn`, and the docs must not send an
    # operator to a directory the installer no longer writes.
    namespace = runpy.run_path(str(REPO / "scripts" / "install_aqg_qoder.py"))
    profiles = namespace["PROFILES"]

    assert profiles["qoder-cn"]["user_dir"] == ".qoder-cn"
    assert profiles["qoder-cn"]["project_dir"] == ".qoder-cn"
    assert profiles["qoder"]["user_dir"] == ".qoder"
    assert profiles["qoder-cli"]["user_dir"] == ".qoder"

    for relative_path in ("README.md", "AI_SETUP.md"):
        text = _text(relative_path)
        assert "~/.qoder-cn" in text
        assert "`.lingma` is not a Qoder CN install target" in text


def test_trae_work_profiles_are_owned_by_the_agent_client_adapter() -> None:
    # R24-12: one adapter owns the four Trae products; the retired work-client
    # path must not still be advertised for `trae-work`.
    from scripts.aqg_client_registry import get_client

    for client_id in ("trae", "trae-cn", "trae-work", "trae-work-cn"):
        spec = get_client(client_id)
        assert all(
            "install_aqg_agent_clients.py" in command
            for command in spec.installer_command
        ), (client_id, spec.installer_command)
        assert all(
            "install_aqg_work_clients.py" not in command
            for command in spec.installer_command
        ), (client_id, spec.installer_command)

    work = runpy.run_path(str(REPO / "scripts" / "install_aqg_work_clients.py"))
    assert "trae-work" not in work["PROFILES"]
    assert "trae-work" in work["RETIRED_PROFILES"]

    readme = _text("README.md")
    assert "work-client adapter already present on this baseline" not in readme
    ai_setup = _text("AI_SETUP.md")
    assert "workbuddy|codebuddy|trae-work|kimi-work" not in ai_setup
    assert "trae|trae-cn|trae-work|trae-work-cn|zed|devin" in ai_setup


def test_shared_trae_skills_roots_are_documented_for_work_profiles() -> None:
    # R24-12: the shared-root contract is the whole reason Work installs land
    # where the product can see them; it has to be written down.
    matrix = _text("docs/CLIENT_SUPPORT_MATRIX_TRAE_ZED_DEVIN.md")

    assert "install_aqg_agent_clients.py" in matrix
    assert "~/.trae/skills" in matrix
    assert "~/.trae-cn/skills" in matrix
    assert "com.trae.solo.app" in matrix
    assert "cn.trae.solo.app" in matrix

    zh_matrix = _text("docs/client-support-matrix.zh-CN.md")
    assert "install_aqg_work_clients.py" not in zh_matrix.split("Trae Work")[1].split("\n")[0]


def test_qoder_desktop_profiles_expose_user_scope_in_registry_and_docs() -> None:
    # R24-09/R24-12: the registry must offer the Desktop user-scope surface, or
    # `--installed-supported` silently drops the product without PROJECT_ROOT.
    from scripts.aqg_client_registry import get_client

    for client_id in ("qoder", "qoder-cn"):
        spec = get_client(client_id)
        assert "user" in spec.supported_scopes
        assert "project" in spec.supported_scopes
        # The user-scope install is a command of its own that needs no project
        # root, and the project rules are a second, separately skippable command.
        user_commands = [
            command
            for command in spec.installer_command
            if "$PROJECT_ROOT" not in command
        ]
        project_commands = [
            command for command in spec.installer_command if "$PROJECT_ROOT" in command
        ]
        assert len(user_commands) == 1, spec.installer_command
        assert "--scope user" in user_commands[0]
        assert len(project_commands) == 1, spec.installer_command
        assert "--scope project" in project_commands[0]

    readme = _text("README.md")
    assert "Qoder IDE user-scope skills and hooks install without `--project-root`" in readme


def test_macos_product_identities_are_single_sourced_and_exact() -> None:
    # R24-12: bundle identity is the detection contract; docs and code must not
    # drift into two different bundle-id tables.
    from scripts.install_aqg_clients import MACOS_PRODUCT_IDENTITIES

    expected = {
        "qoder": (
            ("Qoder.app", "com.qoder.app"),
            ("Qoder IDE.app", "com.qoder.ide"),
        ),
        "qoder-cn": (
            ("Qoder CN.app", "com.qodercn.app"),
            ("Qoder CN IDE.app", "com.aliyun.lingma.ide"),
        ),
        "trae": (("Trae.app", "com.trae.app"),),
        "trae-cn": (("Trae CN.app", "cn.trae.app"),),
        "trae-work": (("TRAE SOLO.app", "com.trae.solo.app"),),
        "trae-work-cn": (("TRAE SOLO CN.app", "cn.trae.solo.app"),),
    }
    actual = {
        client_id: tuple((product.app_name, product.bundle_id) for product in products)
        for client_id, products in MACOS_PRODUCT_IDENTITIES.items()
    }
    assert actual == expected

    readme = _text("README.md")
    for products in expected.values():
        for _app_name, bundle_id in products:
            assert bundle_id in readme


@pytest.mark.parametrize(
    "relative_path",
    (
        "README.md",
        "README.zh-CN.md",
        "AI_SETUP.md",
        "AI_SETUP.zh-CN.md",
        "docs/client-support-matrix.zh-CN.md",
    ),
)
def test_workbuddy_ai_is_visible_in_support_matrix_and_install_docs(relative_path: str) -> None:
    # AQG-026 R8: workbuddy-ai must be consistently documented in English and
    # Chinese support-matrix / install docs, not only in code.
    text = _text(relative_path)
    assert "workbuddy-ai" in text


def test_workbuddy_ai_registry_spec_is_reachable_through_wrapper_routing() -> None:
    # AQG-026 R8: the wrapper must route workbuddy-ai the same way as the other
    # work-client profiles, through install_aqg_work_clients.py.
    from scripts.aqg_client_registry import get_client

    spec = get_client("workbuddy-ai")
    assert all(
        "install_aqg_work_clients.py" in command for command in spec.installer_command
    )
    assert spec.support_status == "full"


def test_ai_setup_bootstrap_lists_required_client_runtime_paths() -> None:
    required_paths = (
        "scripts/install_aqg_hooks.py",
        "scripts/install_aqg_codex_hooks.py",
        "scripts/install_cursor_support.py",
        "scripts/install_aqg_qoder.py",
        "scripts/install_aqg_work_clients.py",
        "scripts/install_aqg_agent_clients.py",
        "scripts/install_aqg_pi.py",
        "scripts/agent_client_aqg_hook.py",
        "scripts/pi_aqg_hook.py",
        "scripts/aqg_client_registry.py",
        "scripts/install_aqg_clients.py",
        "scripts/run_aqg_codex_hook.py",
        "scripts/cursor_aqg_hook.py",
        "agent-packs/claude-code/hooks/",
        "agent-packs/qoder/hooks/",
    )
    for relative_path in ("AI_SETUP.md", "AI_SETUP.zh-CN.md"):
        text = _text(relative_path)
        for required in required_paths:
            assert required in text
            assert (REPO / required.rstrip("/")).exists()
