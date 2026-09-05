"""L2 pre-launch hardening regression suite for the append-only event ledger.

Each test pins one finding from the cross-LLM Deep audit (audit b5381a7d gpt-5.5 +
gemini + o3) + the Claude review (workflow PR-A). Failing-before / passing-after:
every assertion reproduces a concrete bug the audits found and would FAIL against
the pre-fix code. IDs (A1..A16) map to docs/audit-evidence/2026-06-01-l2-*.

Two findings were ADJUDICATED-REJECTED and are NOT here (documented in the
evidence ledger): A5 (require inbox filename == event_id) conflicts with the
tested by-embedded-id dedup design (test_store.test_within_batch_duplicate_first
_writer_wins); A14 (require non-empty source_ref) conflicts with the golden valid
fixture progress-manual-empty-ref.json (empty ref is allowed by design).
"""
from __future__ import annotations

import json

import pytest

from ledger import store
from ledger.conformance import validate_incoming_event
from ledger.paths import project_events_path, write_incoming_event
from ledger.project_id import normalize_remote_url
from ledger.projection import load_stored_events, project_events
from ledger.seq import seq_sort_tuple


def _ev(**over) -> dict:
    """A known-VALID IncomingEvent; override one field to isolate a rejection."""
    ev = {
        "schema_version": "1.0",
        "event_id": "eaf:run-1:1",
        "project": "owner/repo",
        "source": "eaf",
        "source_ref": {"run_id": "run-1"},
        "kind": "progress",
        "payload": {"phase_event": "started", "title": "t"},
        "occurred_at": "2026-05-29T10:00:00Z",
    }
    ev.update(over)
    return ev


def _stored(event_id, ledger_seq, *, action, defect_id="F-1", **over) -> dict:
    ev = {
        "schema_version": "1.0",
        "event_id": event_id,
        "project": "owner/repo",
        "source": "eaf",
        "source_ref": {"run_id": "run-1"},
        "kind": "defect",
        "payload": {"defect_id": defect_id, "action": action},
        "occurred_at": "2026-05-29T10:00:00Z",
        "recorded_at": "2026-05-29T12:00:00+00:00",
        "ledger_seq": ledger_seq,
    }
    if action == "opened":
        ev["payload"].update({"title": "t", "severity": "high"})
    ev.update(over)
    return ev


# --- A1: project path traversal (4-source convergent) -------------------------

@pytest.mark.parametrize("bad_project", [
    "../../../etc/cron.d/x",
    "/etc/passwd",
    "..",
    "a/../b",
    "a//b",
    "a/../../b",
    "..\\..\\x",
    "x\n/y",
    "x\x00y",
])
def test_a1_conformance_rejects_traversal_project(bad_project):
    ok, errors = validate_incoming_event(_ev(project=bad_project))
    assert not ok
    assert any("project" in e for e in errors), (bad_project, errors)


@pytest.mark.parametrize("good_project", [
    "owner/repo", "Example-Org/Example-Repo", "group/sub/repo", "p",
    "local:0123456789abcdef", "a.b/c-d_e",
])
def test_a1_conformance_accepts_safe_project(good_project):
    ok, errors = validate_incoming_event(_ev(project=good_project))
    assert ok, (good_project, errors)


def test_a1_belt_project_events_path_refuses_escape():
    # paths.py containment backstop catches an id that bypassed conformance
    # (covers A3, the read-side aqg_project_status caller, via the same sink).
    for bad in ("../../etc/x", "/etc/passwd", "a/../../../../etc"):
        with pytest.raises(ValueError, match="escapes the ledger root"):
            project_events_path(bad)


def test_a1_belt_project_events_path_allows_valid(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    p = project_events_path("owner/repo")
    assert p == tmp_path / "aqg" / "ledger" / "owner" / "repo" / "events.jsonl"


# --- A2: normalize_remote_url drops traversal slugs ---------------------------

@pytest.mark.parametrize("url", [
    "https://host/../../../../etc/x",
    "git@host:../../etc/x",
    "https://host/owner/../repo",
    "git@host:a b/c",          # space → unsafe char → fall back to local hash
])
def test_a2_normalize_remote_url_refuses_unsafe(url):
    assert normalize_remote_url(url) is None


def test_a2_normalize_remote_url_still_ok():
    assert normalize_remote_url("https://github.com/Owner/Repo.git") == "owner/repo"
    assert normalize_remote_url("git@github.com:group/sub/repo.git") == "group/sub/repo"


# --- A4: producer_seq collision silently drops defect events ------------------

def test_a4_seq_sort_tuple_distinguishes_leading_zero():
    assert seq_sort_tuple("1") != seq_sort_tuple("01")
    assert seq_sort_tuple("7") != seq_sort_tuple("07")
    # numeric ordering still holds: '2' < '10'
    assert seq_sort_tuple("7.defect.2") < seq_sort_tuple("7.defect.10")


def test_a4_projection_keeps_distinct_same_int_seq():
    # producer_seq '01' (ledger_seq 1, opened) then '1' (ledger_seq 2, fixed).
    # Pre-fix both normalize to the same key, so 'fixed' is dropped as <= watermark
    # and the defect stays 'open'. Post-fix they are distinct → 'fixed' applies.
    view = project_events([
        _stored("eaf:run-1:01", 1, action="opened"),
        _stored("eaf:run-1:1", 2, action="fixed"),
    ])
    assert len(view.defects) == 1
    assert view.defects[0].status == "fixed", view.warnings


# --- A6: repair must not delete the valid tail past a mid-log corruption -------

def test_a6_repair_defers_on_corrupt_line_with_valid_data_after(tmp_path):
    events_path = tmp_path / "events.jsonl"
    body = '{"a": 1}\n' + "GARBAGE not json\n" + '{"b": 2}\n'  # corruption MID-file
    events_path.write_text(body, encoding="utf-8")
    # must NOT auto-truncate (that would delete {"b": 2}) — defer for a human.
    assert store._repair_log_tail(events_path) is False
    assert events_path.read_text() == body  # file UNTOUCHED


def test_a6_repair_still_truncates_corrupt_last_line(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"a": 1}\n' + "GARBAGE not json\n", encoding="utf-8")
    assert store._repair_log_tail(events_path) is True
    assert events_path.read_text() == '{"a": 1}\n'


# --- A8: unbounded producer_seq must not crash the sort/watermark --------------

def test_a8_seq_sort_tuple_bounds_huge_digit_run():
    huge = "9" * 6000  # > Python int(str) 4300-digit limit → int() would raise
    out = seq_sort_tuple(huge)            # must NOT raise
    assert isinstance(out, tuple) and out
    # a unicode digit that \d matches but int() rejects is ordered as text, no crash
    assert isinstance(seq_sort_tuple("²"), tuple)  # superscript 2


# --- A10: unbounded event_id overflows the inbox filename ----------------------

def test_a10_conformance_rejects_overlong_event_id():
    long_seq = "eaf:run-1:" + ("9" * 300)
    ok, errors = validate_incoming_event(_ev(event_id=long_seq))
    assert not ok
    assert any("at most" in e and "event_id" in e for e in errors), errors


# --- A11: write_incoming_event(validate=False) without event_id ----------------

def test_a11_write_without_event_id_raises_valueerror(tmp_path):
    ev = _ev()
    del ev["event_id"]
    with pytest.raises(ValueError, match="event_id is required"):
        write_incoming_event(ev, inbox_dir=tmp_path, validate=False)


# --- A12: bool ledger_seq must not seed max_seq --------------------------------

def test_a12_existing_event_ids_ignores_bool_ledger_seq(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"event_id": "eaf:run-1:1", "ledger_seq": True}) + "\n",
        encoding="utf-8",
    )
    _ids, max_seq, ok = store._existing_event_ids(events_path)
    assert ok
    assert max_seq == 0  # True (bool) must NOT be read as ledger_seq 1


# --- A13: aqg-hook is progress-only — reject a defect at ingress ---------------

def test_a13_conformance_rejects_aqg_hook_defect():
    ev = _ev(
        event_id="aqg-hook:sess-1:1",
        source="aqg-hook",
        source_ref={"session_id": "sess-1"},
        kind="defect",
        payload={"defect_id": "F-1", "action": "opened", "title": "t", "severity": "high"},
    )
    ok, errors = validate_incoming_event(ev)
    assert not ok
    assert any("aqg-hook" in e and "defect" in e for e in errors), errors


def test_a13_aqg_hook_progress_still_valid():
    ev = _ev(event_id="aqg-hook:sess-1:1", source="aqg-hook",
             source_ref={"session_id": "sess-1"}, kind="progress")
    ok, errors = validate_incoming_event(ev)
    assert ok, errors


# --- A15: occurred_at range validation -----------------------------------------

@pytest.mark.parametrize("bad_ts", [
    "2026-99-99T10:00:00Z",
    "2026-13-01T10:00:00Z",
    "2026-02-30T10:00:00Z",
    "2026-05-29T25:00:00Z",
    "2026-05-29T10:61:00Z",
])
def test_a15_conformance_rejects_impossible_datetime(bad_ts):
    ok, errors = validate_incoming_event(_ev(occurred_at=bad_ts))
    assert not ok
    assert any("occurred_at" in e for e in errors), (bad_ts, errors)


@pytest.mark.parametrize("good_ts", [
    "2026-05-29T10:00:00Z",
    "2026-05-29T10:00:00.123Z",          # fractional still accepted (gemini-f2)
    "2026-05-29T10:00:00.123456+02:00",
])
def test_a15_conformance_accepts_valid_datetime(good_ts):
    ok, errors = validate_incoming_event(_ev(occurred_at=good_ts))
    assert ok, (good_ts, errors)


# --- A9: projection streams the log and stays defensive ------------------------

def test_a9_load_stored_events_streams_and_skips_bad_lines(tmp_path):
    events_path = tmp_path / "events.jsonl"
    events_path.write_text(
        json.dumps({"event_id": "eaf:run-1:1", "ledger_seq": 1}) + "\n"
        + "\n"                                   # blank line skipped
        + "{not json\n"                          # bad line → warning, not crash
        + json.dumps({"event_id": "eaf:run-1:2", "ledger_seq": 2}) + "\n",
        encoding="utf-8",
    )
    events, warnings = load_stored_events(events_path)
    assert [e["event_id"] for e in events] == ["eaf:run-1:1", "eaf:run-1:2"]
    assert any("invalid JSON" in w for w in warnings)
