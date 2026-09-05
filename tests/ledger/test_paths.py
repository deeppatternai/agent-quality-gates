"""Path resolution + atomic inbox write tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.paths import (
    _decode_path_segment,
    _encode_path_segment,
    aqg_data_dir,
    inbox_event_filename,
    ledger_failed_dir,
    ledger_inbox_dir,
    ledger_processed_dir,
    project_events_path,
    write_incoming_event,
)


def _valid_event(event_id: str = "eaf:run-x:1") -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "project": "example-org/example-repo",
        "source": "eaf",
        "source_ref": {"run_id": "run-x"},
        "kind": "progress",
        "payload": {"phase_event": "started", "title": "Run started"},
        "occurred_at": "2026-05-29T10:00:00Z",
    }


def test_aqg_data_dir_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert aqg_data_dir() == tmp_path / "aqg"


def test_aqg_data_dir_fallback(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert aqg_data_dir() == Path.home() / ".aqg"


def test_inbox_tree_structure(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    root = tmp_path / "aqg" / "ledger" / "_inbox"
    assert ledger_inbox_dir() == root
    assert ledger_processed_dir() == root / "processed"
    assert ledger_failed_dir() == root / "failed"


def test_write_atomic_named_by_event_id(tmp_path):
    ev = _valid_event("eaf:run-x:1")
    p = write_incoming_event(ev, inbox_dir=tmp_path)
    assert p == tmp_path / f"{inbox_event_filename('eaf:run-x:1')}.json"
    assert p.exists()
    assert json.loads(p.read_text()) == ev
    # no tmp residue left behind
    assert list(tmp_path.glob(".tmp-*")) == []


def test_write_creates_inbox_dir(tmp_path):
    nested = tmp_path / "a" / "b" / "_inbox"
    write_incoming_event(_valid_event(), inbox_dir=nested)
    assert nested.is_dir()


def test_write_rejects_invalid_event(tmp_path):
    bad = _valid_event()
    del bad["project"]  # missing required → conformance fails
    with pytest.raises(ValueError, match="conformance"):
        write_incoming_event(bad, inbox_dir=tmp_path)
    assert list(tmp_path.glob("*.json")) == []  # nothing written


def test_write_idempotent_same_event_id(tmp_path):
    ev = _valid_event("eaf:run-x:7")
    write_incoming_event(ev, inbox_dir=tmp_path)
    write_incoming_event(ev, inbox_dir=tmp_path)  # retry same business action
    assert len(list(tmp_path.glob("*.json"))) == 1


def test_write_event_id_with_compound_producer_seq(tmp_path):
    # producer_seq form '<transition>.<kind>.<index>' (per N1) must round-trip.
    ev = _valid_event("eaf:run-x:3.defect.2")
    ev["kind"] = "defect"
    ev["payload"] = {"defect_id": "F-1", "action": "opened", "title": "x", "severity": "high"}
    p = write_incoming_event(ev, inbox_dir=tmp_path)
    assert p.name == f"{inbox_event_filename('eaf:run-x:3.defect.2')}.json"
    assert json.loads(p.read_text())["event_id"] == "eaf:run-x:3.defect.2"


def test_safe_filename_encodes_windows_reserved_colon_round_trips(tmp_path):
    ev = _valid_event("eaf:r2:9")
    safe = inbox_event_filename(ev["event_id"])
    assert safe == "eaf%3Ar2%3A9"
    p = write_incoming_event(ev, inbox_dir=tmp_path)
    assert p.name == f"{safe}.json"
    assert json.loads(p.read_text(encoding="utf-8"))["event_id"] == "eaf:r2:9"


def test_write_can_skip_validation(tmp_path):
    # validate=False is an escape hatch (e.g. AQG re-dropping a known-good event)
    ev = _valid_event()
    del ev["project"]
    p = write_incoming_event(ev, inbox_dir=tmp_path, validate=False)
    assert p.exists()


def test_unsafe_event_id_rejected(tmp_path):
    """gpt-f5: a '/' in event_id must be REFUSED (not lossily collapsed onto a
    colliding filename), even with validate=False."""
    ev = _valid_event("eaf:run:a/b")
    with pytest.raises(ValueError, match="filename-safe"):
        write_incoming_event(ev, inbox_dir=tmp_path, validate=False)


def test_path_segment_percent_encoding_round_trips_colon_and_percent():
    for raw in ("eaf:r2:9", "a%3Ab", "plain._-AZaz09"):
        assert _decode_path_segment(_encode_path_segment(raw)) == raw


def test_path_segment_percent_encoding_is_injective():
    assert _encode_path_segment("a%3Ab") != _encode_path_segment("a:b")
    assert _encode_path_segment("a%3Ab") == "a%253Ab"
    assert _encode_path_segment("a:b") == "a%3Ab"


def test_encoded_event_filename_has_no_windows_illegal_chars(tmp_path):
    ev = _valid_event("eaf:r2:9")
    p = write_incoming_event(ev, inbox_dir=tmp_path)
    assert not any(ch in p.name for ch in '<>:"/\\|?*')
    assert p.name == "eaf%3Ar2%3A9.json"


def test_project_events_path_encodes_each_segment(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    assert project_events_path("owner/repo") == (
        tmp_path / "aqg" / "ledger" / "owner" / "repo" / "events.jsonl"
    )
    assert project_events_path("local:0123456789abcdef") == (
        tmp_path / "aqg" / "ledger" / "local%3A0123456789abcdef" / "events.jsonl"
    )
