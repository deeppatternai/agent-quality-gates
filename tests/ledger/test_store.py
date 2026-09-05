"""Behavior tests for the AQG ledger store (inbox consumer + events.jsonl).

Covers the DesignSpec §7.1 consume flow + §5.1.1 ordering, plus the I/O failure
hardening from audits 874863cc (C1-C6) and def7e984 (R1-R3):
- ledger_seq assigned AFTER sorting by (source, producer_seq), not readdir order
- cross-batch idempotency — a re-dropped event_id is never double-appended
- dead-letter poison data; defer transient read/append I/O errors (left in inbox)
- torn-tail recovery incl. tails LONGER than the 64KB repair window → defer, not
  blind-truncate
- single-consumer lock

Assertions compare against real computed values (event_id lists, file contents)
rather than len()==const, so a test failing means real behavior changed.
"""
from __future__ import annotations

import json
import os

from ledger import store
from ledger.paths import _encode_path_segment, write_incoming_event
from ledger.store import consume_inbox

RECORDED_AT = "2026-05-29T12:00:00+00:00"


def _event(event_id, project="o/r", source="eaf", *, kind="progress", payload=None):
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "project": project,
        "source": source,
        "source_ref": {"run_id": "run-1"},
        "kind": kind,
        "payload": payload or {"phase_event": "started", "title": "t"},
        "occurred_at": "2026-05-29T10:00:00Z",
    }


def _dirs(tmp_path):
    inbox = tmp_path / "_inbox"
    return {
        "inbox": inbox,
        "processed": inbox / "processed",
        "failed": inbox / "failed",
        "events_path_for": lambda pid: tmp_path / pid / "events.jsonl",
        "lock_path": tmp_path / ".consume.lock",
    }


def _drain(tmp_path, **over):
    d = _dirs(tmp_path)
    d.update(over)
    return consume_inbox(recorded_at=RECORDED_AT, **d)


def _read_events(tmp_path, project="o/r"):
    p = tmp_path / project / "events.jsonl"
    if not p.is_file():
        return []
    return [json.loads(line) for line in p.read_text().splitlines() if line.strip()]


def _ids(tmp_path, project="o/r"):
    return [e["event_id"] for e in _read_events(tmp_path, project)]


def _event_file(event_id: str) -> str:
    return f"{_encode_path_segment(event_id)}.json"


# --- happy path + ordering ----------------------------------------------------

def test_empty_inbox_is_noop(tmp_path):
    rep = _drain(tmp_path)
    assert rep.appended == []
    assert rep.deduped == []
    assert rep.failed == []
    assert rep.deferred == []


def test_single_event_appended_with_stored_fields(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    rep = _drain(tmp_path)
    assert rep.appended == ["eaf:run-1:1"], rep.as_dict()
    events = _read_events(tmp_path)
    assert [e["event_id"] for e in events] == ["eaf:run-1:1"]
    rec = events[0]
    # StoredEvent = IncomingEvent + recorded_at + ledger_seq (DesignSpec §5.1)
    assert rec["recorded_at"] == RECORDED_AT
    assert rec["ledger_seq"] == 1
    assert (d["processed"] / _event_file("eaf:run-1:1")).is_file()
    assert not (d["inbox"] / _event_file("eaf:run-1:1")).is_file()


def test_colon_event_id_uses_safe_inbox_filename_and_round_trips(tmp_path):
    d = _dirs(tmp_path)
    p = write_incoming_event(_event("eaf:r2:9"), inbox_dir=d["inbox"])
    assert ":" not in p.name
    assert p.name == _event_file("eaf:r2:9")

    rep = _drain(tmp_path)
    assert rep.appended == ["eaf:r2:9"], rep.as_dict()
    assert _ids(tmp_path) == ["eaf:r2:9"]
    assert (d["processed"] / _event_file("eaf:r2:9")).is_file()


def test_ledger_seq_monotonic_per_project(tmp_path):
    d = _dirs(tmp_path)
    for n in (1, 2, 3):
        write_incoming_event(_event(f"eaf:run-1:{n}"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    seqs = [r["ledger_seq"] for r in _read_events(tmp_path)]
    assert seqs == [1, 2, 3], seqs


def test_sort_before_seq_numeric_not_readdir(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:10"), inbox_dir=d["inbox"])
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    by_seq = {r["event_id"]: r["ledger_seq"] for r in _read_events(tmp_path)}
    assert by_seq["eaf:run-1:2"] < by_seq["eaf:run-1:10"], by_seq


def test_compound_producer_seq_numeric_order(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:7.defect.10"), inbox_dir=d["inbox"])
    write_incoming_event(_event("eaf:run-1:7.defect.2"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    by_seq = {r["event_id"]: r["ledger_seq"] for r in _read_events(tmp_path)}
    assert by_seq["eaf:run-1:7.defect.2"] < by_seq["eaf:run-1:7.defect.10"], by_seq


def test_multi_project_independent_seq(tmp_path):
    # distinct event_ids (real EAF runs in different repos have different run_ids)
    # — the inbox filename is keyed on event_id alone.
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-a:1", project="o/a"), inbox_dir=d["inbox"])
    write_incoming_event(_event("eaf:run-b:1", project="o/b"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    assert [r["ledger_seq"] for r in _read_events(tmp_path, "o/a")] == [1]
    assert [r["ledger_seq"] for r in _read_events(tmp_path, "o/b")] == [1]


# --- idempotency --------------------------------------------------------------

def test_cross_batch_idempotency(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    rep1 = _drain(tmp_path)
    assert rep1.appended == ["eaf:run-1:1"]
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])  # retry
    rep2 = _drain(tmp_path)
    assert rep2.appended == [], rep2.as_dict()
    assert rep2.deduped == ["eaf:run-1:1"], rep2.as_dict()
    assert _ids(tmp_path) == ["eaf:run-1:1"]


def test_within_batch_duplicate_first_writer_wins(tmp_path):
    d = _dirs(tmp_path)
    d["inbox"].mkdir(parents=True, exist_ok=True)
    ev = _event("eaf:run-1:1")
    (d["inbox"] / "first.json").write_text(json.dumps(ev), encoding="utf-8")
    (d["inbox"] / "second.json").write_text(json.dumps(ev), encoding="utf-8")
    rep = _drain(tmp_path)
    assert rep.appended == ["eaf:run-1:1"], rep.as_dict()
    assert rep.deduped == ["eaf:run-1:1"], rep.as_dict()
    assert _ids(tmp_path) == ["eaf:run-1:1"]


# --- dead-letter / poison handling --------------------------------------------

def test_poison_file_quarantined_does_not_block(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    d["inbox"].mkdir(parents=True, exist_ok=True)
    (d["inbox"] / "broken.json").write_text("{not json", encoding="utf-8")
    invalid = _event("eaf:run-1:2")
    invalid["payload"] = {"phase_event": "started"}  # missing title → schema-invalid
    (d["inbox"] / "invalid.json").write_text(json.dumps(invalid), encoding="utf-8")
    rep = _drain(tmp_path)
    assert rep.appended == ["eaf:run-1:1"], rep.as_dict()
    assert sorted(f[0] for f in rep.failed) == ["broken.json", "invalid.json"], rep.as_dict()
    assert _ids(tmp_path) == ["eaf:run-1:1"]
    assert (d["failed"] / "broken.json").is_file()
    assert (d["failed"] / "invalid.json").is_file()


def test_non_utf8_file_quarantined_drain_continues(tmp_path):
    # audit C3: a non-UTF-8 .json is POISON (dead-letter), not transient.
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    d["inbox"].mkdir(parents=True, exist_ok=True)
    (d["inbox"] / "binary.json").write_bytes(b"\xff\xfe\x00\x01not utf8")
    rep = _drain(tmp_path)
    assert rep.appended == ["eaf:run-1:1"], rep.as_dict()
    assert [f[0] for f in rep.failed] == ["binary.json"], rep.as_dict()
    assert "UTF-8" in rep.failed[0][1]
    assert (d["failed"] / "binary.json").is_file()


def test_malformed_encoded_filename_quarantined(tmp_path):
    d = _dirs(tmp_path)
    d["inbox"].mkdir(parents=True, exist_ok=True)
    bad_name = "eaf%3Arun-1%ZZ1.json"
    (d["inbox"] / bad_name).write_text(json.dumps(_event("eaf:run-1:1")), encoding="utf-8")
    rep = _drain(tmp_path)
    assert rep.appended == []
    assert [f[0] for f in rep.failed] == [bad_name], rep.as_dict()
    assert "percent escape" in rep.failed[0][1]
    assert (d["failed"] / bad_name).is_file()


def test_stored_only_field_rejected_to_failed(tmp_path):
    d = _dirs(tmp_path)
    d["inbox"].mkdir(parents=True, exist_ok=True)
    bad = _event("eaf:run-1:1")
    bad["ledger_seq"] = 5
    (d["inbox"] / _event_file("eaf:run-1:1")).write_text(json.dumps(bad), encoding="utf-8")
    rep = _drain(tmp_path)
    assert rep.appended == []
    assert [f[0] for f in rep.failed] == [_event_file("eaf:run-1:1")], rep.as_dict()
    assert "ledger_seq" in rep.failed[0][1]


def test_read_io_error_defers_not_dead_lettered(tmp_path, monkeypatch):
    # audit def7e984 R2: a transient read OSError must DEFER (leave in inbox),
    # not dead-letter a valid event to failed/.
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    real_read_text = type(d["inbox"]).read_text

    def flaky_read_text(self, *a, **k):
        if self.name == _event_file("eaf:run-1:1"):
            raise OSError("EMFILE: too many open files")
        return real_read_text(self, *a, **k)

    monkeypatch.setattr(type(d["inbox"]), "read_text", flaky_read_text)
    rep = _drain(tmp_path)
    assert rep.appended == [], rep.as_dict()
    assert rep.failed == [], rep.as_dict()  # NOT dead-lettered
    assert [f[0] for f in rep.deferred] == [_event_file("eaf:run-1:1")], rep.as_dict()
    assert (d["inbox"] / _event_file("eaf:run-1:1")).is_file()  # still retryable
    assert not (d["failed"] / _event_file("eaf:run-1:1")).exists()


def test_dedup_move_collision_suffixed(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    assert (d["processed"] / _event_file("eaf:run-1:1")).is_file()
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])  # retry
    _drain(tmp_path)
    assert (d["processed"] / _event_file("eaf:run-1:1")).is_file()
    assert (d["processed"] / "eaf%3Arun-1%3A1.dup-2.json").is_file()


# --- torn-tail recovery (audit C1/C5/R1) --------------------------------------

def test_torn_trailing_line_recovered_no_duplicate(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)  # one clean line (seq 1)
    events_path = tmp_path / "o/r" / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"event_id": "eaf:run-1:2", "ledg')  # torn append
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])  # retry
    _drain(tmp_path)
    assert _ids(tmp_path) == ["eaf:run-1:1", "eaf:run-1:2"]
    assert [r["ledger_seq"] for r in _read_events(tmp_path)] == [1, 2]


def test_corrupt_terminated_tail_recovered(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    events_path = tmp_path / "o/r" / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write("this is not json at all\n")  # corrupt but newline-terminated
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    assert _ids(tmp_path) == ["eaf:run-1:1", "eaf:run-1:2"]


def test_repair_preserves_clean_log_prefix(tmp_path):
    # audit C4: exercise repair on a clean log (second drain has a same-project
    # event so per-project repair actually runs) and assert byte-preservation.
    d = _dirs(tmp_path)
    for n in (1, 2):
        write_incoming_event(_event(f"eaf:run-1:{n}"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    events_path = tmp_path / "o/r" / "events.jsonl"
    first_two = events_path.read_text()
    write_incoming_event(_event("eaf:run-1:3"), inbox_dir=d["inbox"])
    _drain(tmp_path)  # repair runs (batch has an o/r event), must not corrupt
    after = events_path.read_text()
    assert after.startswith(first_two)
    assert _ids(tmp_path) == ["eaf:run-1:1", "eaf:run-1:2", "eaf:run-1:3"]


def test_repair_log_tail_clean_file_byte_identical(tmp_path):
    events_path = tmp_path / "events.jsonl"
    body = '{"a": 1}\n{"b": 2}\n'
    events_path.write_text(body, encoding="utf-8")
    assert store._repair_log_tail(events_path) is True
    assert events_path.read_text() == body


def test_repair_log_tail_missing_file_ok(tmp_path):
    assert store._repair_log_tail(tmp_path / "nope.jsonl") is True


def test_repair_log_tail_short_torn_tail_truncated(tmp_path):
    # a short torn tail (within the window, after a real newline) is truncated
    events_path = tmp_path / "events.jsonl"
    events_path.write_text('{"a": 1}\n{"torn', encoding="utf-8")
    assert store._repair_log_tail(events_path) is True
    assert events_path.read_text() == '{"a": 1}\n'


def test_repair_log_tail_large_log_clean_byte_identical(tmp_path):
    # a clean log far larger than the 64KB window must be left byte-identical
    # (repair only inspects the tail, finds it clean, touches nothing).
    events_path = tmp_path / "events.jsonl"
    body = "".join(
        json.dumps({"event_id": f"eaf:run-1:{i}", "ledger_seq": i + 1}) + "\n"
        for i in range(4000)  # ~200KB
    )
    events_path.write_text(body, encoding="utf-8")
    assert store._repair_log_tail(events_path) is True
    assert events_path.read_text() == body


def test_repair_log_tail_torn_tail_within_window_large_log(tmp_path):
    # clean >200KB log + a SHORT torn tail: the 64KB window still contains many
    # newlines, so repair truncates the torn tail and keeps every clean line.
    events_path = tmp_path / "events.jsonl"
    clean = "".join(
        json.dumps({"event_id": f"eaf:run-1:{i}", "ledger_seq": i + 1}) + "\n"
        for i in range(4000)
    )
    events_path.write_text(clean + '{"event_id": "torn', encoding="utf-8")
    assert store._repair_log_tail(events_path) is True
    assert events_path.read_text() == clean


def test_repair_log_tail_torn_tail_exceeds_window_defers(tmp_path):
    # audit def7e984 R1 (THE critical case): a torn tail LONGER than the 64KB
    # window has no newline in the last 64KB. Repair must NOT truncate to an
    # arbitrary offset (which would fuse the next append onto garbage); it must
    # return False so the caller defers and a human/next-pass handles it.
    events_path = tmp_path / "events.jsonl"
    clean = '{"event_id": "eaf:run-1:1", "ledger_seq": 1}\n'
    huge_torn = "x" * (store._REPAIR_TAIL_BYTES + 5000)  # > 64KB, no newline
    events_path.write_text(clean + huge_torn, encoding="utf-8")
    before = events_path.read_text()
    assert store._repair_log_tail(events_path) is False  # defer, do not corrupt
    assert events_path.read_text() == before  # file UNTOUCHED, not truncated


def test_oversized_torn_tail_defers_in_drain(tmp_path):
    # end-to-end: an oversized torn tail makes the project defer (no append, no
    # corruption, source left in inbox).
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)  # seq 1 stored
    events_path = tmp_path / "o/r" / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write("y" * (store._REPAIR_TAIL_BYTES + 5000))  # oversized torn tail
    before = events_path.read_text()
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])
    rep = _drain(tmp_path)
    assert rep.appended == [], rep.as_dict()
    assert [f[0] for f in rep.deferred] == [_event_file("eaf:run-1:2")], rep.as_dict()
    assert events_path.read_text() == before  # log not corrupted
    assert (d["inbox"] / _event_file("eaf:run-1:2")).is_file()  # retryable


# --- I/O failure → deferred (audit C1/C2) -------------------------------------

def test_append_failure_defers_and_poisons_project(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])

    def boom(events_path, record):
        raise OSError("disk full")

    monkeypatch.setattr(store, "_append_line", boom)
    rep = _drain(tmp_path)
    assert rep.appended == [], rep.as_dict()
    assert rep.failed == [], rep.as_dict()  # NOT dead-lettered
    assert sorted(f[0] for f in rep.deferred) == [
        _event_file("eaf:run-1:1"),
        _event_file("eaf:run-1:2"),
    ]
    assert (d["inbox"] / _event_file("eaf:run-1:1")).is_file()
    assert (d["inbox"] / _event_file("eaf:run-1:2")).is_file()
    assert _read_events(tmp_path) == []


def test_append_failure_then_success_next_pass(tmp_path, monkeypatch):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    calls = {"n": 0}
    real_append = store._append_line

    def flaky(events_path, record):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("transient")
        return real_append(events_path, record)

    monkeypatch.setattr(store, "_append_line", flaky)
    rep1 = _drain(tmp_path)
    assert [f[0] for f in rep1.deferred] == [_event_file("eaf:run-1:1")]
    rep2 = _drain(tmp_path)  # flaky now succeeds
    assert rep2.appended == ["eaf:run-1:1"], rep2.as_dict()
    assert _ids(tmp_path) == ["eaf:run-1:1"]


def test_unreadable_existing_log_defers_no_reset(tmp_path, monkeypatch):
    # audit C2: existing log that can't be read must NOT be treated as empty
    # (which would reset ledger_seq to 1); the event is deferred instead.
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)  # seq 1 stored
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])

    def unreadable(events_path):
        return set(), 0, False  # exists but unreadable

    monkeypatch.setattr(store, "_existing_event_ids", unreadable)
    rep = _drain(tmp_path)
    assert rep.appended == [], rep.as_dict()
    assert [f[0] for f in rep.deferred] == [_event_file("eaf:run-1:2")], rep.as_dict()
    assert [r["ledger_seq"] for r in _read_events(tmp_path)] == [1]  # no reset
    assert (d["inbox"] / _event_file("eaf:run-1:2")).is_file()


def test_truncate_failure_in_repair_defers(tmp_path, monkeypatch):
    # if the real _truncate_to fails (OSError), _repair_log_tail returns False
    # and the drain defers — exercises the truncate failure path (not bypassed).
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    _drain(tmp_path)
    events_path = tmp_path / "o/r" / "events.jsonl"
    with events_path.open("a", encoding="utf-8") as fh:
        fh.write('{"torn')  # short torn tail → would normally truncate

    def truncate_fails(p, length):
        return False

    monkeypatch.setattr(store, "_truncate_to", truncate_fails)
    write_incoming_event(_event("eaf:run-1:2"), inbox_dir=d["inbox"])
    rep = _drain(tmp_path)
    assert rep.appended == [], rep.as_dict()
    assert [f[0] for f in rep.deferred] == [_event_file("eaf:run-1:2")], rep.as_dict()


# --- single-consumer lock -----------------------------------------------------

def test_lock_skips_concurrent_drain(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    lock_path = d["lock_path"]
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    held = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
    assert store._try_lock_fd(held) is True
    try:
        rep = _drain(tmp_path, lock_path=lock_path)
        assert rep.skipped_locked is True, rep.as_dict()
        assert rep.appended == []
        assert _read_events(tmp_path) == []
    finally:
        store._unlock_fd(held)
        os.close(held)
    rep2 = _drain(tmp_path, lock_path=lock_path)
    assert rep2.appended == ["eaf:run-1:1"]


def test_report_as_dict_shape(tmp_path):
    d = _dirs(tmp_path)
    write_incoming_event(_event("eaf:run-1:1"), inbox_dir=d["inbox"])
    rep = _drain(tmp_path)
    dd = rep.as_dict()
    assert dd["appended_count"] == 1
    assert dd["deduped_count"] == 0
    assert dd["failed_count"] == 0
    assert dd["deferred_count"] == 0
    assert dd["skipped_locked"] is False
