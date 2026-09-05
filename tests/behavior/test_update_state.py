"""Behavior contracts for the managed-update install-state file.

`install-state.json` is the machine-side record of what AQG actually installed
(see docs/UPDATE_ARCHITECTURE.md §4). Everything the updater later decides —
whether a skill was added, removed or renamed; which hosts are behind; what is
still pending — is a diff against this file. A wrong answer here is not a
cosmetic bug: it makes the planner prune a symlink it does not own, or silently
skip a host. So the contract is fail-closed on every ambiguity.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from scripts.aqg_update import state as state_mod


def _minimal(**overrides) -> dict:
    payload = {
        "schema": state_mod.STATE_SCHEMA,
        "channel": "stable",
        "installed_version": "0.15.0",
        "installed_commit": "a1b2c3d",
        "release_sequence": 7,
        "installed_at": "2026-09-02T12:00:00Z",
        "applied_by": "context-helper",
        "hosts": {},
        "pending": [],
    }
    payload.update(overrides)
    return payload


# --- R1: state root resolution ------------------------------------------------


def test_state_root_honours_the_env_override(tmp_path, monkeypatch):
    monkeypatch.setenv("AQG_STATE_ROOT", str(tmp_path / "custom"))
    assert state_mod.state_root(create=False) == tmp_path / "custom"


def test_state_root_defaults_beside_the_checkout_not_inside_it(monkeypatch):
    monkeypatch.delenv("AQG_STATE_ROOT", raising=False)
    monkeypatch.delenv("AQG_ROOT", raising=False)
    root = state_mod.state_root(create=False)
    assert root == Path.home() / ".deeppattern" / "aqg-state"


def test_state_root_inside_aqg_root_is_refused(tmp_path, monkeypatch):
    """The atomic swap replaces AQG_ROOT wholesale; state kept inside it would
    vanish or revert at exactly the moment a rollback needs it."""
    aqg_root = tmp_path / "agent-quality-gates"
    aqg_root.mkdir()
    monkeypatch.setenv("AQG_STATE_ROOT", str(aqg_root / "nested" / "state"))
    monkeypatch.setenv("AQG_ROOT", str(aqg_root))
    with pytest.raises(state_mod.StateError, match="inside AQG_ROOT"):
        state_mod.state_root(create=False)


def test_relative_state_root_override_is_refused(monkeypatch):
    """A relative override resolves against the caller's CWD, so a SessionStart
    hook and upgrade.sh would read different files — each seeing the other's
    install as absent."""
    monkeypatch.setenv("AQG_STATE_ROOT", "relative/aqg-state")
    monkeypatch.delenv("AQG_ROOT", raising=False)
    with pytest.raises(state_mod.StateError, match="absolute"):
        state_mod.state_root(create=False)


def test_symlinked_state_root_is_refused_when_only_reading(tmp_path, monkeypatch):
    """The symlink refusal used to run only on the create path, leaving reads
    through a redirected root unchecked."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)
    monkeypatch.setenv("AQG_STATE_ROOT", str(link))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    with pytest.raises(state_mod.StateError, match="symlink"):
        state_mod.state_root(create=False)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_explicit_path_inside_aqg_root_is_refused(tmp_path, monkeypatch, operation):
    """The invariant must hold for every call, not only the default root —
    otherwise it is a property of one branch, not of the module."""
    aqg_root = tmp_path / "agent-quality-gates"
    aqg_root.mkdir()
    monkeypatch.setenv("AQG_ROOT", str(aqg_root))
    inside = aqg_root / "install-state.json"
    with pytest.raises(state_mod.StateError, match="inside AQG_ROOT"):
        if operation == "read":
            state_mod.read_state(path=inside)
        else:
            state_mod.write_state(_minimal(), path=inside)
    assert not inside.exists()


# --- R2: round trip -----------------------------------------------------------


def test_write_then_read_returns_the_same_state(tmp_path):
    target = tmp_path / "install-state.json"
    payload = _minimal(hosts={"claude-code": {"last_applied_version": "0.15.0"}})
    state_mod.write_state(payload, path=target)
    assert state_mod.read_state(path=target) == payload


def test_absent_file_reads_as_no_recorded_install_not_an_error(tmp_path):
    """A machine that has never run the updater is the normal first case, not a
    failure — the caller must be able to tell it apart from a corrupt file."""
    assert state_mod.read_state(path=tmp_path / "nope.json") is None


# --- R3: corruption fails closed ---------------------------------------------


def test_malformed_json_fails_closed(tmp_path):
    target = tmp_path / "install-state.json"
    target.write_text('{"schema": 1, "channel":', encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.read_state(path=target)


def test_truncated_but_valid_json_fails_closed(tmp_path):
    """Valid JSON missing required keys must not read as a partial state — the
    planner would treat the absent hosts as 'nothing installed' and re-route
    every skill."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps({"schema": state_mod.STATE_SCHEMA}), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="missing"):
        state_mod.read_state(path=target)


def test_non_object_json_fails_closed(tmp_path):
    target = tmp_path / "install-state.json"
    target.write_text("[]", encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.read_state(path=target)


def test_non_utf8_bytes_fail_closed(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError — a corrupt file was
    crashing the caller instead of being refused."""
    target = tmp_path / "install-state.json"
    target.write_bytes(b'{"schema": 1, "channel": "\xff\xfe stable"}')
    with pytest.raises(state_mod.StateError):
        state_mod.read_state(path=target)


def test_deeply_nested_json_fails_closed(tmp_path):
    """A pathological document must be refused, not raise RecursionError out of
    the JSON parser."""
    target = tmp_path / "install-state.json"
    target.write_text("[" * 60000 + "]" * 60000, encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.read_state(path=target)


def test_broken_symlink_is_corruption_not_absence(tmp_path):
    """The dangerous case: read as absence, the planner concludes 'nothing
    installed' and re-routes or prunes every host."""
    target = tmp_path / "install-state.json"
    target.symlink_to(tmp_path / "gone.json")
    with pytest.raises(state_mod.StateError, match="symlink"):
        state_mod.read_state(path=target)


def test_unserializable_payload_fails_closed_without_creating_a_file(tmp_path):
    """`hosts` is only checked to be a mapping, so a value json cannot encode
    survives validation and must still not escape as a raw TypeError."""
    target = tmp_path / "install-state.json"
    with pytest.raises(state_mod.StateError):
        state_mod.write_state(_minimal(hosts={"claude-code": {1, 2}}), path=target)
    assert not target.exists()


def test_unusable_state_root_fails_closed(tmp_path, monkeypatch):
    """A regular file where the state root belongs must be a refusal, not a raw
    FileExistsError from mkdir."""
    occupied = tmp_path / "aqg-state"
    occupied.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("AQG_STATE_ROOT", str(occupied))
    monkeypatch.delenv("AQG_ROOT", raising=False)
    with pytest.raises(state_mod.StateError):
        state_mod.state_root(create=True)


# --- R4: schema guard ---------------------------------------------------------


def test_future_schema_fails_closed_and_names_both_versions(tmp_path):
    """An older AQG must refuse a newer state file rather than misread it —
    downgrading is how a rollback leaves a new state file behind an old reader."""
    target = tmp_path / "install-state.json"
    target.write_text(
        json.dumps(_minimal(schema=state_mod.STATE_SCHEMA + 1)), encoding="utf-8"
    )
    with pytest.raises(state_mod.StateError) as excinfo:
        state_mod.read_state(path=target)
    message = str(excinfo.value)
    assert str(state_mod.STATE_SCHEMA + 1) in message
    assert str(state_mod.STATE_SCHEMA) in message


def test_missing_schema_fails_closed(tmp_path):
    target = tmp_path / "install-state.json"
    payload = _minimal()
    del payload["schema"]
    target.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(state_mod.StateError):
        state_mod.read_state(path=target)


def test_unknown_channel_fails_closed(tmp_path):
    """`channel` decides whether a signature is required (§9). An unrecognized
    value must never be treated as permissive."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(channel="whatever")), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="channel"):
        state_mod.read_state(path=target)


def test_non_integer_release_sequence_fails_closed(tmp_path):
    """The sequence is the anti-rollback comparison. A string "7" compares
    against an int by raising, or worse, lexically — either way the guard is
    gone, so the type is part of the contract."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(release_sequence="7")), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="release_sequence"):
        state_mod.read_state(path=target)


def test_hosts_must_be_a_mapping(tmp_path):
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(hosts=[])), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="hosts"):
        state_mod.read_state(path=target)


@pytest.mark.parametrize(
    "field, bad_value",
    [
        ("installed_version", None),
        ("installed_commit", 42),
        ("installed_at", {}),
        ("applied_by", 0),
    ],
)
def test_identity_fields_must_be_strings(tmp_path, field, bad_value):
    """Presence is not enough: these four are compared and rendered downstream,
    so a wrongly-typed one validates here and fails somewhere far away."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(**{field: bad_value})), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match=field):
        state_mod.read_state(path=target)


def test_unhashable_channel_fails_closed(tmp_path):
    """`channel not in CHANNELS` raises TypeError for an unhashable value —
    a crash, not a refusal."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(channel=[])), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="channel"):
        state_mod.read_state(path=target)


def test_schema_below_the_known_set_fails_closed(tmp_path):
    """Rejecting only *newer* schemas lets an unrecognized older one be read as
    if it were current."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(schema=0)), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="schema"):
        state_mod.read_state(path=target)


@pytest.mark.parametrize("field", ["schema", "release_sequence"])
def test_bool_is_not_an_integer_for_numeric_fields(tmp_path, field):
    """`True` is an int in Python; accepting it would make release_sequence
    compare as 1."""
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(**{field: True})), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match=field):
        state_mod.read_state(path=target)


def test_pending_must_be_a_list(tmp_path):
    target = tmp_path / "install-state.json"
    target.write_text(json.dumps(_minimal(pending={})), encoding="utf-8")
    with pytest.raises(state_mod.StateError, match="pending"):
        state_mod.read_state(path=target)


def test_write_refuses_a_payload_it_could_not_read_back(tmp_path):
    """Writing an invalid state would strand the next reader in fail-closed
    forever, with no way back except manual deletion."""
    target = tmp_path / "install-state.json"
    with pytest.raises(state_mod.StateError):
        state_mod.write_state({"schema": state_mod.STATE_SCHEMA}, path=target)
    assert not target.exists()


# --- R5: write is atomic, private, and does not follow a symlink --------------


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode bits")
def test_written_file_is_owner_only(tmp_path):
    target = tmp_path / "install-state.json"
    state_mod.write_state(_minimal(), path=target)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_refuses_to_follow_a_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    target = tmp_path / "install-state.json"
    target.symlink_to(outside)
    with pytest.raises(state_mod.StateError, match="symlink"):
        state_mod.write_state(_minimal(), path=target)
    assert outside.read_text(encoding="utf-8") == "{}"


def test_write_leaves_no_temp_file_behind(tmp_path):
    target = tmp_path / "install-state.json"
    state_mod.write_state(_minimal(), path=target)
    assert [p.name for p in tmp_path.iterdir()] == ["install-state.json"]


def test_rewrite_replaces_rather_than_appends(tmp_path):
    target = tmp_path / "install-state.json"
    state_mod.write_state(_minimal(installed_version="0.15.0"), path=target)
    state_mod.write_state(_minimal(installed_version="0.15.1"), path=target)
    assert state_mod.read_state(path=target)["installed_version"] == "0.15.1"
