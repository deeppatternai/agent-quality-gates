"""Conformance tests — golden fixtures drive the IncomingEvent validator.

The same validator is shared by the AQG inbox consumer and the EAF exporter
(self-test before push). valid/ must all pass; invalid/ must all fail with at
least one error; a few targeted tests pin the high-value rejections (defect
status leak, StoredEvent-only fields).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ledger.conformance import validate_incoming_event

_FIX = Path(__file__).resolve().parent.parent.parent / "contracts" / "ledger" / "fixtures"
_VALID = sorted((_FIX / "valid").glob("*.json"))
_INVALID = sorted((_FIX / "invalid").glob("*.json"))


def _load(p: Path) -> dict:
    return json.loads(p.read_text())


def test_fixtures_present():
    assert _VALID, "no valid fixtures found"
    assert _INVALID, "no invalid fixtures found"


@pytest.mark.parametrize("path", _VALID, ids=lambda p: p.stem)
def test_valid_fixtures_pass(path: Path):
    ok, errors = validate_incoming_event(_load(path))
    assert ok, f"{path.name} should be VALID but got errors: {errors}"
    assert errors == []


@pytest.mark.parametrize("path", _INVALID, ids=lambda p: p.stem)
def test_invalid_fixtures_fail(path: Path):
    ok, errors = validate_incoming_event(_load(path))
    assert not ok, f"{path.name} should be INVALID but passed conformance"
    assert errors, f"{path.name}: invalid fixture must report >=1 error"


def test_defect_status_field_rejected():
    """The single most important invariant: a defect payload must NOT carry
    'status' (append-only purity — status is projected, not stored)."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "defect-with-status.json"))
    assert not ok
    assert any("status" in e.lower() for e in errors), errors


def test_stored_only_fields_rejected():
    """recorded_at / ledger_seq are AQG-generated (StoredEvent) — a producer
    must not send them."""
    for name in ("has-ledger-seq-storedonly", "has-recorded-at-storedonly"):
        ok, errors = validate_incoming_event(_load(_FIX / "invalid" / f"{name}.json"))
        assert not ok, name
        assert any(
            ("ledger_seq" in e) or ("recorded_at" in e) or ("unknown" in e.lower())
            for e in errors
        ), (name, errors)


def test_bad_event_id_format_rejected():
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "bad-event-id-no-colons.json"))
    assert not ok
    assert any("event_id" in e for e in errors), errors


def test_returns_all_errors_not_first():
    """Validator accumulates errors (doesn't short-circuit) so a producer fixing
    one issue sees the rest."""
    bad = {"schema_version": "9.9", "source": "codex", "kind": "note"}  # many violations
    ok, errors = validate_incoming_event(bad)
    assert not ok
    assert len(errors) >= 3, errors


# --- audit fd14014a regressions ---


def test_source_prefix_must_match_source():
    """gpt-f2: event_id source-prefix must equal the top-level source."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "event-id-source-mismatch.json"))
    assert not ok
    assert any("source-prefix" in e for e in errors), errors


def test_source_ref_value_must_be_string():
    """gpt-f1a / gemini-f3: schema types source_ref values as string."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "source-ref-int-value.json"))
    assert not ok
    assert any("source_ref.run_id" in e for e in errors), errors


def test_event_id_filename_unsafe_rejected():
    """gpt-f5: a '/' in event_id (would collide inbox filenames) is rejected."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "event-id-with-slash.json"))
    assert not ok
    assert any("event_id" in e for e in errors), errors


def test_occurred_at_accepts_fractional_seconds():
    """gemini-f2: RFC3339 3-digit fractional seconds (JS/Node producers) pass."""
    ok, errors = validate_incoming_event(_load(_FIX / "valid" / "occurred-at-millis.json"))
    assert ok, errors


@pytest.mark.parametrize("bad", [
    {"source": []},                                  # unhashable list at top-level enum
    {"kind": {}},                                    # unhashable dict at top-level enum
    {"kind": "progress", "payload": {"phase_event": {}, "title": "x"}},  # nested enum
    {"schema_version": []},   # unhashable on the new 1.1 version gate (audit 996d6911 C1)
    {"schema_version": {}},   # "
])
def test_no_raise_on_unhashable_enum(bad):
    """gpt-f3: set-membership on an unhashable value must NOT raise TypeError —
    the validator must always return (False, errors)."""
    ok, errors = validate_incoming_event(bad)  # must not raise
    assert not ok
    assert errors


def test_schema_and_conformance_do_not_drift():
    """Anti-drift cross-check (audit f1, CONVERGENT). The JSON Schema and the
    stdlib validator are two representations of ONE contract. Invariant:

      whatever the SCHEMA rejects, conformance MUST ALSO reject

    (conformance may be stricter — e.g. occurred_at RFC3339 shape, which schema
    leaves as an advisory `format` — but it must never MISS a schema constraint,
    which was exactly the drift the audit found). Plus every golden 'valid'
    fixture must pass BOTH. Runs the real jsonschema lib against the schema and
    diffs verdicts per fixture. Skipped if jsonschema is absent (it is not an AQG
    runtime dep — OQ-10 — but CI/dev installs it to run this cross-check)."""
    jsonschema = pytest.importorskip("jsonschema")
    schema = json.loads((_FIX.parent / "incoming-event.schema.json").read_text())
    # FormatChecker so `format: date` on milestone dates is actually enforced in
    # the cross-check (default jsonschema treats format as annotation-only — audit
    # 996d6911 C3a). date-time has no checker without rfc3339-validator, so
    # occurred_at stays conformance-stricter (pre-existing baseline, tolerated by
    # the asymmetric `if not schema_ok` assert below).
    validator = jsonschema.Draft7Validator(schema, format_checker=jsonschema.FormatChecker())
    for path in _VALID + _INVALID:
        ev = _load(path)
        schema_ok = validator.is_valid(ev)
        conf_ok, errs = validate_incoming_event(ev)
        if not schema_ok:
            assert not conf_ok, (
                f"{path.name}: schema REJECTS but conformance ACCEPTS — "
                f"conformance misses a schema constraint (DRIFT)"
            )
        if path.parent.name == "valid":
            assert schema_ok, f"{path.name}: valid fixture rejected by schema"
            assert conf_ok, f"{path.name}: valid fixture rejected by conformance: {errs}"


# --- milestone + decision contract (DesignSpec a4 §5.2/§5.6/§9; G0 acceptance
#     ⑪ reject-UUID / ⑯ cross-source / ⑰ size+target; pin the semantics, not just
#     the fixture filename, so a future loosening of conformance is caught) ------


def test_milestone_decision_reject_uuid_producer_seq():
    """⑪ a3:T1: milestone/decision lifecycles (4-state + terminal + re-plan /
    raised→resolved) need a monotonic producer_seq; the §5.1.1 UUID escape hatch
    does NOT apply — a UUID has no order and would let a stale action reorder the
    lifecycle."""
    for name in ("milestone-uuid-producer-seq", "decision-uuid-producer-seq"):
        ok, errors = validate_incoming_event(_load(_FIX / "invalid" / f"{name}.json"))
        assert not ok, name
        assert any("UUID" in e and "producer_seq" in e for e in errors), (name, errors)


def test_milestone_decision_require_schema_1_1():
    """Version×kind gate (a4 §5.6): a 1.0 event must not carry milestone/decision
    (the capability handshake — 1.0 has no such semantics)."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "milestone-on-schema-1.0.json"))
    assert not ok
    assert any("1.1" in e and "schema_version" in e for e in errors), errors


def test_unknown_future_version_still_rejected():
    """G0 (a4 §5.6 main clause 'keep strict-reject/dead-letter'): an unsupported
    FUTURE version (2.0) is rejected → dead-letter (replayable after upgrade), NOT
    silently accepted. Kept as reject (store control-flow untouched); the no-
    silent-drop safety invariant matches a4's 'ignore' intent on the property that
    matters (no permanent loss). Adjudicated in de_audit deep."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "wrong-schema-version.json"))
    assert not ok
    assert any("schema_version" in e for e in errors), errors


def test_old_kinds_backward_compatible_under_1_1():
    """Rollout safety (a4 §5.6): 1.1 is a strict superset, so an OLD kind under
    1.1 stays valid (the existing 1.0 fixtures, covered by the param test, prove
    the other direction)."""
    ok, errors = validate_incoming_event(_load(_FIX / "valid" / "progress-schema-1.1.json"))
    assert ok, errors


def test_milestone_cross_source_same_id_allowed():
    """a4:S / ⑯: a milestone_id MAY recur across sources (write-plan plans 'M0',
    EAF executes 'M0') — conformance must NOT reject either; the projection (P2)
    reduces by action semantics, not by cross-source seq."""
    planned = _load(_FIX / "valid" / "milestone-planned.json")       # source=manual, M0
    completed = _load(_FIX / "valid" / "milestone-completed.json")   # source=eaf, M0
    assert planned["payload"]["milestone_id"] == completed["payload"]["milestone_id"] == "M0"
    assert planned["source"] != completed["source"]
    assert validate_incoming_event(planned)[0], "manual-planned M0 must validate"
    assert validate_incoming_event(completed)[0], "eaf-completed M0 must validate"


def test_milestone_planned_requires_size_target_optional():
    """⑰ a4:T: estimated_size required on planned; target_date is OPTIONAL (it
    does not drive progress/health)."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "milestone-planned-missing-size.json"))
    assert not ok
    assert any("estimated_size" in e for e in errors), errors
    planned = _load(_FIX / "valid" / "milestone-planned.json")
    no_target = {**planned,
                 "payload": {k: v for k, v in planned["payload"].items() if k != "target_date"}}
    assert validate_incoming_event(no_target)[0], "target_date must be optional"


def test_milestone_no_status_field():
    """append-only purity (a4 §5.2/§5.3): status is projected, never stored — a
    payload 'status' is rejected (symmetry with the defect guard)."""
    ok, errors = validate_incoming_event(_load(_FIX / "invalid" / "milestone-with-status.json"))
    assert not ok
    assert any("status" in e.lower() for e in errors), errors


def test_aqg_hook_cannot_emit_milestone():
    """aqg-hook is progress-only; a milestone from it would be a projection-
    ignored dead event (generalizes the existing aqg-hook defect guard)."""
    base = _load(_FIX / "valid" / "milestone-started.json")
    hook_ms = {**base, "source": "aqg-hook", "event_id": "aqg-hook:sess-1:2",
               "source_ref": {"session_id": "sess-1"}}
    ok, errors = validate_incoming_event(hook_ms)
    assert not ok
    assert any("aqg-hook" in e and "milestone" in e for e in errors), errors
