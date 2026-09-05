"""Behavior contracts for the AQG supported-client registry."""

from dataclasses import fields, replace

import pytest

from scripts.aqg_client_registry import (
    ADAPTER_ACTIONS,
    CLIENT_REGISTRY,
    VALID_SUPPORT_STATUSES,
    ClientAdapterSpec,
    get_client,
    list_clients,
    validate_registry,
)


EXPECTED_CLIENTS = {
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
}

REQUIRED_FIELDS = {
    "client_id",
    "support_status",
    "supported_scopes",
    "skills_source",
    "skills_install_mode_default",
    "installer_command",
    "verify_command",
    "uninstall_command",
    "is_installed_command",
    "rules_surface",
    "hooks_surface",
    "supports_no_hooks",
    "capability_evidence",
}


def test_registry_exposes_required_schema_and_validates() -> None:
    validate_registry()

    assert REQUIRED_FIELDS <= {field.name for field in fields(ClientAdapterSpec)}
    assert set(CLIENT_REGISTRY) == EXPECTED_CLIENTS


def test_client_ids_are_unique_and_statuses_are_legal() -> None:
    clients = list_clients()
    ids = [client.client_id for client in clients]

    assert len(ids) == len(set(ids))
    for client in clients:
        assert client.support_status in VALID_SUPPORT_STATUSES


def test_every_client_has_evidence_and_adapter_action_contract() -> None:
    for client in list_clients():
        assert client.capability_evidence
        assert all(item.strip() for item in client.capability_evidence)
        assert tuple(client.adapter_actions) == ADAPTER_ACTIONS
        assert client.adapter_actions["apply"] == client.installer_command
        assert client.adapter_actions["verify"] == client.verify_command
        assert client.adapter_actions["uninstall"] == client.uninstall_command
        assert client.adapter_actions["is-installed"] == client.is_installed_command


@pytest.mark.parametrize("client_id", sorted(EXPECTED_CLIENTS))
def test_every_registry_adapter_has_install_verify_uninstall_commands(
    client_id: str,
) -> None:
    client = get_client(client_id)
    assert client.installer_command
    assert client.verify_command
    assert client.uninstall_command
    assert client.adapter_actions["is-installed"]


def test_qoder_ide_profiles_remain_partial_not_full() -> None:
    for client_id in ("qoder", "qoder-cn"):
        client = get_client(client_id)

        assert client.support_status == "partial"
        assert any("partial" in item for item in client.capability_evidence)
        assert any("without SessionStart" in item for item in client.hooks_surface)


def test_new_work_and_code_clients_have_explicit_support_levels() -> None:
    expected = {
        "workbuddy": "partial",
        "codebuddy": "full",
        "trae-work": "partial",
        "kimi-work": "partial",
        "kimi-code": "partial",
        "qoderwork": "partial",
        "qoderwake": "partial",
    }

    for client_id, support_status in expected.items():
        client = get_client(client_id)
        assert client.support_status == support_status
        assert any(
            "official docs" in item
            or "official product site" in item
            or "Daimon" in item
            for item in client.capability_evidence
        )


def test_no_hooks_capability_is_explicit_in_registry() -> None:
    assert get_client("codex").supports_no_hooks is True
    assert get_client("claude-code").supports_no_hooks is True
    assert get_client("cursor").supports_no_hooks is True
    assert get_client("qoder-cli").supports_no_hooks is False
    assert get_client("qoder-cli-cn").supports_no_hooks is False
    assert get_client("qoder").supports_no_hooks is False
    assert get_client("qoder-cn").supports_no_hooks is False
    assert get_client("codebuddy").supports_no_hooks is True
    assert get_client("kimi-code").supports_no_hooks is True
    assert get_client("workbuddy").supports_no_hooks is True
    assert get_client("kimi-work").supports_no_hooks is True
    assert get_client("trae-work").supports_no_hooks is True
    for client_id in ("trae", "trae-cn", "trae-work-cn", "zed", "devin"):
        assert get_client(client_id).supports_no_hooks is True
    assert get_client("pi").supports_no_hooks is True


def test_codex_registry_uses_link_mode_with_shared_installer_helper() -> None:
    client = get_client("codex")
    assert client.skills_install_mode_default == "link"
    assert "scripts/aqg_skill_install.py" in client.capability_evidence


@pytest.mark.parametrize(
    "client_id",
    [
        "cursor",
        "qoder-cli",
        "qoder-cli-cn",
        "qoder",
        "qoder-cn",
        "trae",
        "trae-cn",
        "trae-work-cn",
        "zed",
        "devin",
    ],
)
def test_cursor_and_qoder_profiles_default_to_link_mode(client_id: str) -> None:
    client = get_client(client_id)

    assert client.skills_install_mode_default == "link"


@pytest.mark.parametrize(
    "client_id",
    ["workbuddy", "codebuddy", "trae-work", "kimi-work", "kimi-code", "qoderwork", "qoderwake"],
)
def test_new_work_and_code_profiles_default_to_link_mode(client_id: str) -> None:
    client = get_client(client_id)

    assert client.skills_install_mode_default == "link"


@pytest.mark.parametrize(
    "client_id",
    [
        "cursor",
        "qoder-cli",
        "qoder-cli-cn",
        "qoder",
        "qoder-cn",
        "trae",
        "trae-cn",
        "trae-work-cn",
        "zed",
        "devin",
    ],
)
def test_cursor_and_qoder_apply_commands_pin_link_mode(client_id: str) -> None:
    client = get_client(client_id)

    assert all("--mode link" in command for command in client.installer_command)


@pytest.mark.parametrize(
    "client_id",
    ["workbuddy", "codebuddy", "trae-work", "kimi-work", "kimi-code", "qoderwork", "qoderwake"],
)
def test_new_work_and_code_apply_commands_pin_link_mode(client_id: str) -> None:
    client = get_client(client_id)

    assert all("--mode link" in command for command in client.installer_command)


def test_cursor_verify_command_pins_link_mode() -> None:
    client = get_client("cursor")

    assert all("--mode link" in command for command in client.verify_command)


def test_new_agent_client_profiles_are_partial_with_limitations() -> None:
    for client_id in ("trae", "trae-cn", "trae-work", "trae-work-cn", "zed", "devin"):
        client = get_client(client_id)

        assert client.support_status == "partial"
        assert any("partial" in item for item in client.capability_evidence)


def test_registry_rejects_empty_is_installed_command_for_any_registry_adapter() -> None:
    client = get_client("cursor")
    bad_client = replace(
        client,
        support_status="partial",
        is_installed_command=(),
        adapter_actions={**client.adapter_actions, "is-installed": ()},
    )

    with pytest.raises(ValueError, match="is_installed_command required"):
        validate_registry([bad_client])


def test_get_client_rejects_unknown_client_id() -> None:
    with pytest.raises(KeyError, match="unknown AQG client_id"):
        get_client("qoder-workbench")


def test_pi_profile_is_partial_with_skills_and_extension_evidence() -> None:
    client = get_client("pi")

    assert client.support_status == "partial"
    assert client.supported_scopes == ("user", "project")
    assert client.skills_install_mode_default == "link"
    assert all("--mode link" in command for command in client.installer_command)
    assert any("extensions#events" in item for item in client.capability_evidence)
    assert any("install_aqg_pi.py" in item for item in client.capability_evidence)
    assert "TypeScript extension" in client.hooks_surface[0]
