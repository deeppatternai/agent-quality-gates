"""Behavior contracts for the host-adapter `verify` verb.

`verify` is the first of the four verbs (docs/UPDATE_ARCHITECTURE.md §6.2) and
the only one this slice implements: it answers "what is installed on this host,
right now" in one shape, whatever the host's config format is. The dispatcher
consumes that shape; it must never learn that Claude Code keeps hooks in
`settings.json` while Codex keeps them in `hooks.json`.

The fixtures drive the real installer rather than a mock: a mocked settings file
would prove only that the adapter can read what the test wrote, not that it
agrees with what AQG actually installs.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.aqg_update import hosts
from scripts.aqg_update.hosts import base, claude_code

REPO_ROOT = Path(__file__).resolve().parents[2]
INSTALLER = REPO_ROOT / "scripts" / "install_aqg_hooks.py"


def _apply_canonical_hooks(target: Path) -> None:
    """Install the real managed hook set into *target*."""
    env = dict(os.environ)
    env["AQG_BACKUP_DIR"] = str(target.parent / ".aqg-central")
    proc = subprocess.run(
        [
            sys.executable,
            str(INSTALLER),
            "--apply",
            "--target",
            str(target),
            "--aqg-root",
            str(REPO_ROOT),
        ],
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr


def _adapter(target: Path) -> base.HostAdapter:
    """Construct the concrete adapter directly.

    `adapter_for` deliberately takes no host-specific arguments (a temp
    settings path is Claude-shaped mechanics and must not appear in the neutral
    lookup), so a test that needs one reaches below that boundary.
    """
    return claude_code.ClaudeCodeAdapter(settings_path=target, aqg_root=REPO_ROOT)


# --- adapter lookup -----------------------------------------------------------


def test_unknown_client_is_refused_not_silently_skipped(tmp_path):
    """A dispatcher iterating the registry must not mistake "no adapter yet" for
    "nothing to do on this host"."""
    with pytest.raises(base.AdapterError, match="no-such-host"):
        hosts.adapter_for("no-such-host")


def test_available_clients_lists_what_can_actually_be_driven(tmp_path):
    assert "claude-code" in hosts.available_clients()


# --- verify: hook state -------------------------------------------------------


def test_absent_settings_reports_missing(tmp_path):
    evidence = _adapter(tmp_path / "settings.json").verify()
    assert evidence.client_id == "claude-code"
    assert evidence.hooks_status == "missing"


def test_canonical_install_reports_complete(tmp_path):
    target = tmp_path / "settings.json"
    _apply_canonical_hooks(target)
    assert _adapter(target).verify().hooks_status == "complete"


def test_a_tampered_command_reports_stale_not_complete(tmp_path):
    """The whole point of verify: a hook whose command drifted from canonical is
    not a working install, and reporting it as complete would let an update
    skip the host it most needs to fix."""
    target = tmp_path / "settings.json"
    _apply_canonical_hooks(target)
    settings = json.loads(target.read_text(encoding="utf-8"))
    event = next(iter(settings["hooks"]))
    settings["hooks"][event][0]["hooks"][0]["command"] = "bash /tmp/not-aqg.sh"
    target.write_text(json.dumps(settings), encoding="utf-8")
    assert _adapter(target).verify().hooks_status == "stale"


def test_malformed_settings_reports_invalid_rather_than_crashing(tmp_path):
    target = tmp_path / "settings.json"
    target.write_text("{not json", encoding="utf-8")
    evidence = _adapter(target).verify()
    assert evidence.hooks_status == "invalid"
    assert evidence.hooks_detail


def test_an_unrecognized_status_from_the_host_helper_fails_closed(
    tmp_path, monkeypatch
):
    """If the wrapped helper grows a status this contract does not know, the
    adapter must refuse rather than pass an unmodelled value to the dispatcher."""
    target = tmp_path / "settings.json"
    adapter = _adapter(target)
    monkeypatch.setattr(
        adapter, "_inspect", lambda: ("brand-new-status", "from a future helper")
    )
    with pytest.raises(base.AdapterError, match="brand-new-status"):
        adapter.verify()


# --- verify: what install state recorded for this host ------------------------


def test_recorded_version_comes_from_install_state(tmp_path):
    target = tmp_path / "settings.json"
    state = {
        "schema": 1,
        "channel": "stable",
        "installed_version": "0.15.0",
        "installed_commit": "a1b2c3d",
        "release_sequence": 7,
        "installed_at": "2026-09-02T12:00:00Z",
        "applied_by": "context-helper",
        "hosts": {"claude-code": {"last_applied_version": "0.14.0"}},
        "pending": [],
    }
    assert _adapter(target).verify(state=state).recorded_version == "0.14.0"


def test_no_state_means_no_recorded_version(tmp_path):
    """A first run has no record; that is absence, not a version of 'unknown'."""
    assert _adapter(tmp_path / "settings.json").verify(state=None).recorded_version is None


def test_state_without_a_record_for_this_host_is_absence_not_an_error(tmp_path):
    state = {"hosts": {"codex": {"last_applied_version": "0.15.0"}}}
    assert _adapter(tmp_path / "settings.json").verify(state=state).recorded_version is None


def test_a_malformed_host_record_fails_closed(tmp_path):
    """Reading a corrupt per-host record as 'never applied' would make the
    planner re-apply a host that is actually configured."""
    state = {"hosts": {"claude-code": "0.14.0"}}
    with pytest.raises(base.AdapterError, match="claude-code"):
        _adapter(tmp_path / "settings.json").verify(state=state)


def test_a_non_string_version_in_the_record_fails_closed(tmp_path):
    state = {"hosts": {"claude-code": {"last_applied_version": 14}}}
    with pytest.raises(base.AdapterError, match="last_applied_version"):
        _adapter(tmp_path / "settings.json").verify(state=state)


# --- fixes from audit aud_TNso2gDWPRzS8psH -----------------------------------


def test_state_that_is_not_a_mapping_fails_closed(tmp_path):
    """A corrupted state file can parse as a JSON array; `.get` on it raised
    AttributeError, so AdapterError was not in fact the whole failure surface."""
    with pytest.raises(base.AdapterError, match="mapping"):
        _adapter(tmp_path / "settings.json").verify(state=["not", "a", "mapping"])


def test_a_record_without_a_version_is_malformed_not_absent(tmp_path):
    """The record exists, so this host WAS recorded; reading it as 'never
    applied' would make the planner re-apply an already-configured host."""
    state = {"hosts": {"claude-code": {}}}
    with pytest.raises(base.AdapterError, match="last_applied_version"):
        _adapter(tmp_path / "settings.json").verify(state=state)


def test_a_null_version_is_malformed_not_absent(tmp_path):
    state = {"hosts": {"claude-code": {"last_applied_version": None}}}
    with pytest.raises(base.AdapterError, match="last_applied_version"):
        _adapter(tmp_path / "settings.json").verify(state=state)


def test_adapter_for_takes_no_host_shaped_arguments():
    """The neutral lookup must not encode one host's configuration vocabulary
    onto every future adapter."""
    import inspect

    params = set(inspect.signature(hosts.adapter_for).parameters) - {"client_id"}
    assert params == set(), f"host-shaped parameters leaked into adapter_for: {params}"


def test_an_unresolvable_aqg_root_is_the_adapters_fault_not_the_hosts(
    tmp_path, monkeypatch
):
    """Reporting 'invalid' here would blame the host's config for the adapter's
    own misconfiguration — and a default-constructed adapter would report every
    host as broken."""
    monkeypatch.delenv("AQG_ROOT", raising=False)
    monkeypatch.setattr(
        claude_code.install_aqg_hooks, "_resolve_aqg_root", lambda _=None: None
    )
    adapter = claude_code.ClaudeCodeAdapter(settings_path=tmp_path / "settings.json")
    with pytest.raises(base.AdapterError, match="AQG root"):
        adapter.verify()


def test_evidence_rejects_an_unmodelled_status_however_it_is_built():
    """The guard must be a property of the type, not a convention about which
    constructor an adapter happened to use."""
    with pytest.raises(base.AdapterError, match="partially-upgraded"):
        base.Evidence(
            client_id="claude-code",
            hooks_status="partially-upgraded",
            hooks_detail="",
            recorded_version=None,
        )


def test_an_adapter_without_a_client_id_is_rejected_at_definition_time():
    with pytest.raises(base.AdapterError, match="client_id"):

        class _Forgetful(base.HostAdapter):
            def verify(self, *, state=None):  # pragma: no cover - never defined
                raise NotImplementedError
