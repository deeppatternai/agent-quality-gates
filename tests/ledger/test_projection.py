"""Behavior tests for the ProjectView projection (events.jsonl → ledger view).

Covers DesignSpec §5.6 (ProjectView assembly) + §5.3.1 (defect state machine):
- progress timeline ordered by ledger_seq
- defect lifecycle (opened→fixing→fixed→closed; reopened)
- terminal guards (N2): closed/wontfix only exit via reopened; fixed forward-only
- per-(defect,source) watermark (N6): a defect's late event is ignored, and one
  defect's higher producer_seq does NOT drop another defect's legit late event
- aqg-hook does not drive the defect state machine (N7)
- source distribution + coverage caveat (has_low_fidelity)
- defensive read boundary: malformed lines/events skipped with warnings, never raise

Assertions compare real projected values (status, ids, ordering), so a failing
test means real behavior changed — not just a count drift.
"""
from __future__ import annotations

import json

from ledger.projection import (
    CoverageState,
    DecisionEntry,
    DefectEntry,
    HealthLight,
    MilestoneEntry,
    ProgressEntry,
    ProjectView,
    completion,
    coverage_state,
    health_light,
    load_stored_events,
    project_events,
    project_log,
)

RECORDED_AT = "2026-05-29T12:00:00+00:00"


def _se(
    event_id,
    *,
    kind="progress",
    source="eaf",
    project="o/r",
    payload=None,
    ledger_seq=1,
    occurred_at="2026-05-29T10:00:00Z",
    schema_version="1.0",
):
    """A StoredEvent = IncomingEvent + recorded_at + ledger_seq (DesignSpec §5.1)."""
    return {
        "schema_version": schema_version,
        "event_id": event_id,
        "project": project,
        "source": source,
        "source_ref": {},
        "kind": kind,
        "payload": payload if payload is not None else {"phase_event": "started", "title": "t"},
        "occurred_at": occurred_at,
        "recorded_at": RECORDED_AT,
        "ledger_seq": ledger_seq,
    }


def _progress(ls, title, *, phase="stage_advanced", source="eaf", project="o/r", **extra):
    payload = {"phase_event": phase, "title": title, **extra}
    return _se(f"{source}:run-1:{ls}", kind="progress", source=source, project=project,
               payload=payload, ledger_seq=ls)


def _defect(defect_id, action, ls, *, source="eaf", pseq=None, **payload_extra):
    pseq = pseq if pseq is not None else str(ls)
    payload = {"defect_id": defect_id, "action": action, **payload_extra}
    return _se(f"{source}:run-1:{pseq}", kind="defect", source=source, payload=payload, ledger_seq=ls)


def _handoff(ls, manual_path, *, generated_by="eaf-handoff", source="eaf"):
    payload = {"manual_path": manual_path, "generated_by": generated_by}
    return _se(f"{source}:run-1:{ls}", kind="handoff", source=source, payload=payload, ledger_seq=ls)


def _milestone(milestone_id, action, ls, *, source="eaf", pseq=None,
               occurred_at="2026-05-29T10:00:00Z", **payload_extra):
    """A milestone StoredEvent (a4 §5.2). schema_version 1.1 (the kind is 1.1-only)."""
    pseq = pseq if pseq is not None else str(ls)
    payload = {"milestone_id": milestone_id, "action": action, **payload_extra}
    return _se(f"{source}:run-1:{pseq}", kind="milestone", source=source, payload=payload,
               ledger_seq=ls, occurred_at=occurred_at, schema_version="1.1")


def _decision(decision_id, action, ls, *, source="manual", pseq=None,
              occurred_at="2026-05-29T10:00:00Z", **payload_extra):
    """A decision StoredEvent (a4 §9). schema_version 1.1 (the kind is 1.1-only)."""
    pseq = pseq if pseq is not None else str(ls)
    payload = {"decision_id": decision_id, "action": action, **payload_extra}
    return _se(f"{source}:run-1:{pseq}", kind="decision", source=source, payload=payload,
               ledger_seq=ls, occurred_at=occurred_at, schema_version="1.1")


# planned-action business fields (a4 §5.2 required-on-planned).
_PLANNED = dict(title="t", summary="s", key_outcome="k", estimated_size="~1 person-week; S")


# --- empty / basic ------------------------------------------------------------


def test_empty_events_empty_view():
    view = project_events([])
    assert isinstance(view, ProjectView)
    assert view.project_id == "<unknown>"
    assert view.event_count == 0
    assert view.progress == ()
    assert view.defects == ()
    assert view.milestones == ()
    assert view.decisions == ()
    assert view.milestone_activity == ()
    assert view.open_decisions == ()
    assert view.handoffs == ()
    assert view.sources == ()
    assert view.last_activity is None
    assert view.has_low_fidelity is False
    assert view.warnings == ()


def test_project_id_inferred_from_events():
    view = project_events([_progress(1, "hello", project="acme/widget")])
    assert view.project_id == "acme/widget"


def test_explicit_project_id_overrides():
    view = project_events([_progress(1, "x", project="acme/widget")], project_id="forced/id")
    assert view.project_id == "forced/id"


# --- progress timeline --------------------------------------------------------


def test_progress_ordered_by_ledger_seq_not_input_order():
    # fed out of order; projection must replay by ledger_seq
    events = [_progress(3, "third"), _progress(1, "first"), _progress(2, "second")]
    view = project_events(events)
    assert [p.title for p in view.progress] == ["first", "second", "third"]
    assert all(isinstance(p, ProgressEntry) for p in view.progress)


def test_progress_optional_fields_captured():
    view = project_events([
        _progress(1, "did a thing", phase="completed", detail="long detail",
                  pr_url="https://x/pr/1", commit_sha="abc123"),
    ])
    p = view.progress[0]
    assert p.phase_event == "completed"
    assert p.detail == "long detail"
    assert p.pr_url == "https://x/pr/1"
    assert p.commit_sha == "abc123"


def test_progress_missing_title_skipped_with_warning():
    bad = _se("eaf:run-1:1", kind="progress", payload={"phase_event": "started"}, ledger_seq=1)
    view = project_events([bad, _progress(2, "good")])
    assert [p.title for p in view.progress] == ["good"]
    assert any("missing title" in w for w in view.warnings)


# --- defect lifecycle ---------------------------------------------------------


def test_defect_opened_is_open():
    view = project_events([_defect("D-1", "opened", 1, title="boom", severity="high")])
    (d,) = view.defects
    assert isinstance(d, DefectEntry)
    assert d.defect_id == "D-1"
    assert d.status == "open"
    assert d.severity == "high"
    assert d.title == "boom"
    assert d.is_open is True
    assert d.sources == ("eaf",)


def test_defect_full_lifecycle_to_fixed():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="boom", severity="high"),
        _defect("D-1", "status_changed", 2, pseq="2"),
        _defect("D-1", "fixed", 3, pseq="3", fixed_commit="cafe123", verification="pytest -q"),
    ]
    view = project_events(events)
    (d,) = view.defects
    assert d.status == "fixed"
    assert d.title == "boom"            # carried from opened
    assert d.severity == "high"         # carried from opened
    assert d.fixed_commit == "cafe123"
    assert d.verification == "pytest -q"
    assert d.is_open is False
    assert d.is_resolved is True
    assert d.first_seq == 1 and d.last_seq == 3


def test_defect_severity_updated_by_later_event():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "status_changed", 2, pseq="2", severity="critical"),
    ]
    (d,) = project_events(events).defects
    assert d.status == "fixing"
    assert d.severity == "critical"     # last-applied-wins


# --- terminal guards (§5.3.1 N2) ---------------------------------------------


def test_closed_only_reopens_via_reopened():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "closed", 2, pseq="2"),
        _defect("D-1", "status_changed", 3, pseq="3"),   # guarded: closed absorbs
    ]
    (d,) = project_events(events).defects
    assert d.status == "closed"
    assert d.ignored_events == 1


def test_closed_then_reopened_is_open():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "closed", 2, pseq="2"),
        _defect("D-1", "reopened", 3, pseq="3"),
    ]
    (d,) = project_events(events).defects
    assert d.status == "open"


def test_fixed_rejects_regression_to_fixing():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "fixed", 2, pseq="2"),
        _defect("D-1", "status_changed", 3, pseq="3"),   # guarded: fixed is forward-only
    ]
    (d,) = project_events(events).defects
    assert d.status == "fixed"
    assert d.ignored_events == 1


def test_fixed_allows_forward_to_closed():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "fixed", 2, pseq="2"),
        _defect("D-1", "closed", 3, pseq="3"),
    ]
    (d,) = project_events(events).defects
    assert d.status == "closed"
    assert d.ignored_events == 0


def test_wontfix_is_terminal():
    events = [
        _defect("D-1", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("D-1", "wontfix", 2, pseq="2"),
        _defect("D-1", "fixed", 3, pseq="3"),            # guarded
    ]
    (d,) = project_events(events).defects
    assert d.status == "wontfix"
    assert d.ignored_events == 1


# --- per-(defect,source) watermark (§5.3.1 N6) -------------------------------


def test_late_event_ignored_by_watermark_same_defect():
    # opened@2 → fixed@4 → reopened@8 → late status_changed@3 (first fixing period)
    events = [
        _defect("D-1", "opened", 1, pseq="2", title="t", severity="high"),
        _defect("D-1", "fixed", 2, pseq="4"),
        _defect("D-1", "reopened", 3, pseq="8"),
        _defect("D-1", "status_changed", 4, pseq="3"),   # late: producer_seq 3 <= watermark 8
    ]
    (d,) = project_events(events).defects
    assert d.status == "open"            # reopened won; late status_changed ignored
    assert d.ignored_events == 1


def test_watermark_is_per_defect_not_global():
    # The critical scope test: defect B's higher producer_seq must NOT cause
    # defect A's legit late event (lower producer_seq, later ledger_seq) to drop.
    events = [
        _defect("A", "opened", 1, pseq="2", title="a", severity="high"),
        _defect("B", "opened", 2, pseq="5", title="b", severity="low"),
        _defect("A", "status_changed", 3, pseq="3"),     # 3 < B's 5 but > A's own 2
    ]
    view = project_events(events)
    by_id = {d.defect_id: d for d in view.defects}
    assert by_id["A"].status == "fixing"  # applied — per-defect watermark, not global
    assert by_id["B"].status == "open"


def test_uuid_producer_seq_not_dropped_by_watermark():
    # §5.1.1 escape hatch: a UUID producer_seq has no monotonic order, so the
    # watermark must NOT reject an event whose UUID sort key is lower than a
    # prior one. Here event 2's UUID sorts LOWER than event 1's but must apply
    # (audit 33f6e5df: gpt-f3 + gemini-f1).
    hi = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    lo = "00000000-0000-0000-0000-000000000000"
    events = [
        _defect("D-1", "opened", 1, pseq=hi, title="t", severity="high"),
        _defect("D-1", "status_changed", 2, pseq=lo),   # lower UUID, later ledger_seq
    ]
    view = project_events(events)
    (d,) = view.defects
    assert d.status == "fixing"          # applied, NOT dropped as stale
    assert d.ignored_events == 0
    assert any("UUID producer_seq" in w for w in view.warnings)


def test_32_digit_decimal_seq_is_not_uuid():
    # A 32-digit decimal is a monotonic integer, NOT a UUID — watermark must apply
    # (audit dfdcae4d f3). seq '...02' arriving after '...10' is late → ignored.
    events = [
        _defect("D-1", "opened", 1, pseq="0" * 30 + "10", title="t", severity="high"),
        _defect("D-1", "status_changed", 2, pseq="0" * 30 + "02"),  # lower → stale
    ]
    view = project_events(events)
    (d,) = view.defects
    assert d.status == "open"            # status_changed dropped as stale (watermark applied)
    assert d.ignored_events == 1
    assert not any("UUID" in w for w in view.warnings)


def test_uuid_warning_once_per_source_across_defects():
    # global uuid_warned dedup: 3 UUID-driven defects → ONE source-level warning,
    # not three (which would flood the warnings cap; audit dfdcae4d gemini-f2).
    u = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    events = [
        _defect("D-1", "opened", 1, pseq=u, title="a", severity="high"),
        _defect("D-2", "opened", 2, pseq=u, title="b", severity="high"),
        _defect("D-3", "opened", 3, pseq=u, title="c", severity="high"),
    ]
    view = project_events(events)
    uuid_warnings = [w for w in view.warnings if "UUID producer_seq" in w]
    assert len(uuid_warnings) == 1


def test_uuid_defect_lifecycle_has_no_stale_protection():
    # DOCUMENTED §5.1.1 tradeoff (audit dfdcae4d f2): with UUID producer_seq there
    # is no stale-event protection, so a late 'reopened' (last by ledger_seq)
    # reactivates a closed defect. This asserts the ACCEPTED behavior, not a bug.
    u1 = "11111111-1111-1111-1111-111111111111"
    u2 = "22222222-2222-2222-2222-222222222222"
    u3 = "33333333-3333-3333-3333-333333333333"
    events = [
        _defect("D-1", "opened", 1, pseq=u1, title="t", severity="high"),
        _defect("D-1", "closed", 2, pseq=u2),
        _defect("D-1", "reopened", 3, pseq=u3),   # applied in ledger order → reactivates
    ]
    (d,) = project_events(events).defects
    assert d.status == "open"


def test_mixed_manual_eaf_defect_warns():
    # §5.3.1 'manual > eaf' precedence is unimplemented (v1 EAF-only writer); the
    # mixed-source case must be surfaced loudly, not silently mis-projected
    # (audit 33f6e5df gpt-f2 → needs Owner decision).
    events = [
        _defect("D-1", "opened", 1, source="eaf", pseq="1", title="t", severity="high"),
        _defect("D-1", "closed", 2, source="manual", pseq="1"),
    ]
    view = project_events(events)
    (d,) = view.defects
    assert d.status == "closed"          # ledger-order: manual 'closed' applied last
    assert set(d.sources) == {"eaf", "manual"}
    assert any("manual > eaf" in w for w in view.warnings)


def test_mixed_warning_fires_even_when_manual_event_skipped():
    # gpt-f4 (dfdcae4d): the unresolved-precedence warning is keyed on SEEN sources,
    # so it fires even when the manual event is terminal-guarded and never applied.
    events = [
        _defect("D-1", "opened", 1, source="eaf", pseq="1", title="t", severity="high"),
        _defect("D-1", "closed", 2, source="eaf", pseq="2"),
        _defect("D-1", "status_changed", 3, source="manual", pseq="1"),  # terminal-guarded
    ]
    view = project_events(events)
    (d,) = view.defects
    assert d.status == "closed"
    assert d.sources == ("eaf",)         # manual never applied → not in applied sources
    assert any("manual > eaf" in w for w in view.warnings)   # but seen → warns


def test_compound_producer_seq_ordering_in_watermark():
    # producer_seq '7.defect.10' must be LATER than '7.defect.2' (segment value,
    # not lexical). The status_changed at 7.defect.2 arriving after fixed at
    # 7.defect.10 is therefore late → ignored.
    events = [
        _defect("D-1", "opened", 1, pseq="7.defect.1", title="t", severity="high"),
        _defect("D-1", "fixed", 2, pseq="7.defect.10"),
        _defect("D-1", "status_changed", 3, pseq="7.defect.2"),  # late vs .10
    ]
    (d,) = project_events(events).defects
    assert d.status == "fixed"
    assert d.ignored_events == 1


# --- aqg-hook does not drive defects (§5.3.1 N7) -----------------------------


def test_hook_defect_event_ignored():
    events = [
        _defect("D-1", "opened", 1, source="eaf", pseq="1", title="t", severity="high"),
        _defect("D-1", "fixed", 2, source="aqg-hook", pseq="1"),   # hook can't drive
    ]
    (d,) = project_events(events).defects
    assert d.status == "open"            # hook 'fixed' ignored
    assert d.ignored_events == 1
    assert d.sources == ("eaf",)
    assert any("only eaf/manual drive" in w for w in project_events(events).warnings)


def test_hook_only_defect_not_in_view():
    events = [_defect("D-1", "opened", 1, source="aqg-hook", pseq="1", title="t", severity="high")]
    view = project_events(events)
    assert view.defects == ()            # never driven → not a tracked defect
    assert any("only eaf/manual drive" in w for w in view.warnings)


def test_manual_drives_defect():
    events = [_defect("D-1", "opened", 1, source="manual", pseq="1", title="t", severity="medium")]
    (d,) = project_events(events).defects
    assert d.status == "open"
    assert d.sources == ("manual",)


# --- handoffs -----------------------------------------------------------------


def test_handoff_entries_captured():
    events = [
        _handoff(1, "/repo/HANDOFF.md", generated_by="eaf-handoff"),
        _handoff(2, "/repo/HANDOFF2.md", generated_by="aqg-handoff"),
    ]
    view = project_events(events)
    assert [(h.manual_path, h.generated_by) for h in view.handoffs] == [
        ("/repo/HANDOFF.md", "eaf-handoff"),
        ("/repo/HANDOFF2.md", "aqg-handoff"),
    ]


# --- source distribution + coverage caveat (§5.6 C5) -------------------------


def test_source_stats_and_fidelity():
    events = [
        _progress(1, "a", source="eaf"),
        _progress(2, "b", source="eaf"),
        _progress(3, "c", source="aqg-hook"),
    ]
    view = project_events(events)
    stats = {s.source: s for s in view.sources}
    assert stats["eaf"].event_count == 2
    assert stats["eaf"].fidelity == "high"
    assert stats["aqg-hook"].event_count == 1
    assert stats["aqg-hook"].fidelity == "low"
    assert view.has_low_fidelity is True


def test_no_low_fidelity_when_only_eaf():
    view = project_events([_progress(1, "a", source="eaf")])
    assert view.has_low_fidelity is False


def test_last_activity_is_latest_recorded_event():
    events = [
        _progress(1, "first", source="eaf"),
        _se("eaf:run-1:9", kind="progress", payload={"phase_event": "completed", "title": "last"},
            ledger_seq=2, occurred_at="2026-05-30T08:00:00Z"),
    ]
    view = project_events(events)
    assert view.last_activity == "2026-05-30T08:00:00Z"


# --- defect ordering ----------------------------------------------------------


def test_defects_sorted_open_then_severity():
    events = [
        _defect("low-open", "opened", 1, pseq="1", title="t", severity="low"),
        _defect("crit-open", "opened", 2, pseq="2", title="t", severity="critical"),
        _defect("closed-one", "opened", 3, pseq="3", title="t", severity="critical"),
        _defect("closed-one", "closed", 4, pseq="4"),
    ]
    view = project_events(events)
    order = [d.defect_id for d in view.defects]
    # open work first (critical before low), resolved last
    assert order == ["crit-open", "low-open", "closed-one"]
    assert view.open_defects == tuple(d for d in view.defects if d.status == "open")
    assert [d.defect_id for d in view.open_defects] == ["crit-open", "low-open"]


# --- defensive read boundary --------------------------------------------------


def test_malformed_event_skipped_with_warning():
    events = [
        {"event_id": "eaf:run-1:1"},                       # missing required fields
        _progress(2, "good"),
    ]
    view = project_events(events)
    assert [p.title for p in view.progress] == ["good"]
    assert any("missing/invalid required field" in w for w in view.warnings)


def test_ledger_seq_bool_rejected():
    # bool is an int subclass; True must not be accepted as ledger_seq
    bad = _se("eaf:run-1:1", ledger_seq=True)
    view = project_events([bad])
    assert view.event_count == 0
    assert view.progress == ()


def test_defect_missing_defect_id_skipped():
    bad = _se("eaf:run-1:1", kind="defect", payload={"action": "opened", "title": "t", "severity": "low"})
    view = project_events([bad])
    assert view.defects == ()
    assert any("missing/invalid defect_id" in w for w in view.warnings)


# --- file loading -------------------------------------------------------------


def test_load_stored_events_skips_bad_lines(tmp_path):
    p = tmp_path / "events.jsonl"
    good = json.dumps(_progress(1, "good"))
    p.write_text(good + "\n" + "{not json\n" + "\n" + "[1,2,3]\n", encoding="utf-8")
    events, warnings = load_stored_events(p)
    assert [e["event_id"] for e in events] == ["eaf:run-1:1"]
    assert any("invalid JSON" in w for w in warnings)
    assert any("not a JSON object" in w for w in warnings)


def test_load_missing_file_is_empty(tmp_path):
    events, warnings = load_stored_events(tmp_path / "nope.jsonl")
    assert events == []
    assert warnings == []


def test_project_log_folds_in_load_warnings(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text(json.dumps(_progress(1, "good")) + "\n" + "garbage\n", encoding="utf-8")
    view = project_log(p)
    assert [pp.title for pp in view.progress] == ["good"]
    assert any("invalid JSON" in w for w in view.warnings)


# --- integration with the store (real events.jsonl) --------------------------


def test_projection_over_store_output(tmp_path):
    """End-to-end: write IncomingEvents through the store, then project the log
    it produced — proves the projection reads real StoredEvent output."""
    from ledger.paths import write_incoming_event
    from ledger.store import consume_inbox

    inbox = tmp_path / "_inbox"
    events_path = tmp_path / "o" / "r" / "events.jsonl"

    def _incoming(eid, kind, payload):
        return {
            "schema_version": "1.0",
            "event_id": eid,
            "project": "o/r",
            "source": "eaf",
            "source_ref": {"run_id": "run-1"},
            "kind": kind,
            "payload": payload,
            "occurred_at": "2026-05-29T10:00:00Z",
        }

    write_incoming_event(_incoming("eaf:run-1:1", "progress",
                                   {"phase_event": "started", "title": "kick off"}),
                         inbox_dir=inbox)
    write_incoming_event(_incoming("eaf:run-1:2", "defect",
                                   {"defect_id": "D-1", "action": "opened",
                                    "title": "leak", "severity": "high"}),
                         inbox_dir=inbox)
    rep = consume_inbox(
        recorded_at=RECORDED_AT,
        inbox=inbox,
        processed=inbox / "processed",
        failed=inbox / "failed",
        events_path_for=lambda pid: tmp_path / pid / "events.jsonl",
        lock_path=tmp_path / ".consume.lock",
    )
    assert rep.appended == ["eaf:run-1:1", "eaf:run-1:2"]

    view = project_log(events_path)
    assert view.project_id == "o/r"
    assert [p.title for p in view.progress] == ["kick off"]
    (d,) = view.defects
    assert d.defect_id == "D-1" and d.status == "open" and d.severity == "high"
    assert view.warnings == ()


# =============================================================================
# milestone reduction (a4 §5.3) — action semantic order, cross-source
# =============================================================================


def test_milestone_planned_is_planned():
    view = project_events([_milestone("M0", "planned", 1, source="manual", **_PLANNED,
                                      target_date="2026-07-01")])
    (m,) = view.milestones
    assert isinstance(m, MilestoneEntry)
    assert m.milestone_id == "M0"
    assert m.status == "planned"
    assert m.title == "t" and m.estimated_size == "~1 person-week; S"
    assert m.target_date == "2026-07-01"
    assert m.sources == ("manual",)


def test_milestone_full_lifecycle_to_completed():
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "started", 2, source="eaf", pseq="2"),
        _milestone("M0", "completed", 3, source="eaf", pseq="3",
                   actual_date="2026-06-08", actual_wallclock="14 minutes"),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "completed"
    assert m.title == "t"                       # carried from planned
    assert m.actual_date == "2026-06-08"
    assert m.actual_wallclock == "14 minutes"
    assert m.first_seq == 1 and m.last_seq == 3


def test_milestone_cross_source_action_order_reduces_to_completed():
    # acceptance ⑯: planned from manual (write-plan) + completed from eaf must
    # reduce to completed by ACTION semantic order, NOT cross-source producer_seq.
    # Here eaf completed has producer_seq 1 (< manual planned's 9) — a producer_seq
    # comparison would wrongly pick planned; semantic order picks completed.
    events = [
        _milestone("M0", "completed", 2, source="eaf", pseq="1", actual_date="2026-06-08"),
        _milestone("M0", "planned", 1, source="manual", pseq="9", **_PLANNED),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "completed"
    assert m.title == "t"                       # planned business fields still captured
    assert set(m.sources) == {"manual", "eaf"}


def test_milestone_idempotent_replay():
    # acceptance ①: the projection is order-independent over a set of UNIQUE events
    # (the store guarantees unique event_ids in an append-only log, so a real replay
    # never duplicates an id). Assert the FULL MilestoneEntry is identical, not just
    # status — so ignored_events / seq / fields drift is caught too (audit b0a5a67c
    # claude-f1 + gpt-f4).
    import dataclasses

    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "started", 2, source="eaf", pseq="2"),
        _milestone("M0", "completed", 3, source="eaf", pseq="3", actual_date="2026-06-08"),
    ]
    forward = project_events(events).milestones
    shuffled = project_events([events[2], events[0], events[1]]).milestones
    assert len(forward) == len(shuffled) == 1
    assert dataclasses.astuple(forward[0]) == dataclasses.astuple(shuffled[0])
    assert forward[0].ignored_events == 0            # no spurious ignores on clean replay


def test_milestone_out_of_order_input_reduces_same():
    # acceptance ②: input order irrelevant — projection sorts deterministically.
    forward = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "started", 2, source="eaf", pseq="2"),
        _milestone("M0", "completed", 3, source="eaf", pseq="3", actual_date="2026-06-08"),
    ]
    (m1,) = project_events(forward).milestones
    (m2,) = project_events(list(reversed(forward))).milestones
    assert m1.status == m2.status == "completed"


def test_milestone_duplicate_terminal_ignored():
    # acceptance ③: a second completed (duplicate terminal) is guard-ignored.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2", actual_date="2026-06-08"),
        _milestone("M0", "completed", 3, source="eaf", pseq="3", actual_date="2026-06-09"),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "completed"
    assert m.actual_date == "2026-06-08"        # first terminal wins; second ignored
    assert m.ignored_events == 1


def test_milestone_conflicting_terminal_warns():
    # acceptance ③: completed + cancelled for the same milestone is a conflict —
    # first terminal (by sort key) wins deterministically, the other is warned.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2", actual_date="2026-06-08"),
        _milestone("M0", "cancelled", 3, source="eaf", pseq="3"),
    ]
    view = project_events(events)
    (m,) = view.milestones
    assert m.status == "completed"              # rank tie broken by producer_seq (2 < 3)
    assert m.ignored_events == 1
    assert any("after terminal" in w for w in view.warnings)


def test_milestone_conflicting_terminal_cancelled_wins():
    # acceptance ③ (other direction): with cancelled at the LOWER producer_seq, the
    # earliest-terminal determinism makes cancelled win — pins the tie-break both ways.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "cancelled", 2, source="eaf", pseq="2"),
        _milestone("M0", "completed", 3, source="eaf", pseq="3", actual_date="2026-06-08"),
    ]
    view = project_events(events)
    (m,) = view.milestones
    assert m.status == "cancelled"              # cancelled (seq 2) wins over completed (seq 3)
    assert m.actual_date is None                # not completed → no completion date exposed
    assert m.ignored_events == 1
    assert any("after terminal" in w for w in view.warnings)


def test_milestone_cross_source_terminal_conflict_deterministic():
    # acceptance ③/⑯ + a3:T5: a completed (eaf) and a cancelled (manual) for the SAME
    # milestone is a cross-source conflict. producer_seq is incomparable across
    # sources, so the within-rank tie-break is deterministic (event_id final key),
    # the loser is ignored+warned, AND the ignored event still anchors per-source
    # staleness (milestone_activity is built before reduction).
    events = [
        _milestone("M0", "completed", 1, source="eaf", pseq="1", actual_date="2026-06-08",
                   occurred_at="2026-06-01T10:00:00Z"),
        _milestone("M0", "cancelled", 2, source="manual", pseq="1",
                   occurred_at="2026-06-02T10:00:00Z"),
    ]
    view = project_events(events)
    (m,) = view.milestones
    assert m.status in ("completed", "cancelled")     # deterministic winner (one of the two)
    assert m.ignored_events == 1                       # the loser is ignored
    assert any("after terminal" in w for w in view.warnings)
    # both sources' events anchor staleness despite one being reduction-ignored (D9).
    activity = {sa.source: sa.last_occurred_at for sa in view.milestone_activity}
    assert activity == {"eaf": "2026-06-01T10:00:00Z", "manual": "2026-06-02T10:00:00Z"}


def test_milestone_cross_source_terminal_conflict_is_stable():
    # the deterministic winner must not depend on input order (idempotent under reorder).
    a = _milestone("M0", "completed", 1, source="eaf", pseq="1", actual_date="2026-06-08")
    b = _milestone("M0", "cancelled", 2, source="manual", pseq="1")
    (m1,) = project_events([a, b]).milestones
    (m2,) = project_events([b, a]).milestones
    assert m1.status == m2.status


def test_milestone_replan_after_cancel_reopens():
    # acceptance ④: a replan=true planned after a terminal reopens to planned.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "started", 2, source="eaf", pseq="2"),
        _milestone("M0", "cancelled", 3, source="eaf", pseq="3"),
        _milestone("M0", "planned", 4, source="manual", pseq="4", replan=True, **_PLANNED),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "planned"                # replan reopened it
    assert m.replanned is True


def test_milestone_non_replan_planned_does_not_reopen():
    # a plain planned (no replan flag) does NOT escape a terminal — semantic order
    # folds it before the terminal, so the terminal still wins.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2", actual_date="2026-06-08"),
        _milestone("M0", "planned", 3, source="manual", pseq="3", **_PLANNED),  # no replan
    ]
    (m,) = project_events(events).milestones
    assert m.status == "completed"
    assert m.replanned is False


def test_milestone_post_terminal_planned_does_not_move_occurred_at():
    # audit b0a5a67c gpt-f2: a non-replan planned that arrives AFTER the completion
    # (higher ledger_seq) is absorbed — it must NOT redefine the milestone's current-
    # state timestamp / last_seq, which come from the status-determining terminal.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2", actual_date="2026-06-08",
                   occurred_at="2026-06-02T10:00:00Z"),
        _milestone("M0", "planned", 3, source="manual", pseq="3", **_PLANNED,
                   occurred_at="2026-06-09T10:00:00Z"),   # later, but absorbed
    ]
    (m,) = project_events(events).milestones
    assert m.status == "completed"
    assert m.occurred_at == "2026-06-02T10:00:00Z"   # completion time, NOT the stray planned's
    assert m.last_seq == 2                            # the completed event, not ledger_seq 3


def test_milestone_replan_clears_completion_fields():
    # audit b0a5a67c gemini-f1: a re-plan reopening a COMPLETED milestone must not
    # retain the voided epoch's actual_date / actual_wallclock (status→planned).
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2",
                   actual_date="2026-06-08", actual_wallclock="9 minutes"),
        _milestone("M0", "planned", 3, source="manual", pseq="3", replan=True, **_PLANNED),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "planned"
    assert m.replanned is True
    assert m.actual_date is None                     # voided completion date cleared
    assert m.actual_wallclock is None


def test_milestone_multi_epoch_replan_then_complete_is_planned_v1_deferred():
    # audit b0a5a67c claude-f2: pin the DOCUMENTED v1-deferred behavior — a milestone
    # that re-completes after a replan projects the re-planned `planned` state (the
    # multi-epoch cross-source case action-order cannot disambiguate). If this flips,
    # the deferral contract changed and this test must be revisited.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="eaf", pseq="2", actual_date="2026-06-08"),
        _milestone("M0", "planned", 3, source="manual", pseq="3", replan=True, **_PLANNED),
        _milestone("M0", "completed", 4, source="eaf", pseq="4", actual_date="2026-06-20"),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "planned"                     # documented v1 limitation (not "completed")
    assert m.replanned is True


def test_milestone_jump_direct_completed_no_crash():
    # acceptance ⑥: a milestone that jumps straight to completed (no planned) and
    # is missing optional fields must project best-effort, not crash.
    (m,) = project_events([_milestone("M0", "completed", 1, source="eaf",
                                      actual_date="2026-06-08")]).milestones
    assert m.status == "completed"
    assert m.title is None and m.estimated_size is None and m.target_date is None


def test_milestone_cancelled_status():
    (m,) = project_events([
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "cancelled", 2, source="eaf", pseq="2"),
    ]).milestones
    assert m.status == "cancelled"


def test_milestone_missing_milestone_id_skipped():
    bad = _se("eaf:run-1:1", kind="milestone", payload={"action": "planned"}, ledger_seq=1,
              schema_version="1.1")
    view = project_events([bad])
    assert view.milestones == ()
    assert any("missing/invalid milestone_id" in w for w in view.warnings)


def test_milestone_aqg_hook_event_ignored():
    # aqg-hook is progress-only (conformance rejects it; the projection is also
    # defensive) — a stray aqg-hook milestone does not drive the reduction.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "completed", 2, source="aqg-hook", pseq="1", actual_date="2026-06-08"),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "planned"                # hook completed ignored
    assert any("only eaf/manual drive" in w for w in project_events(events).warnings)


def test_milestone_hook_only_not_tracked():
    # a milestone seen ONLY in non-driving (aqg-hook) events is not a tracked
    # milestone — and contributes no per-source staleness anchor.
    events = [_milestone("M0", "planned", 1, source="aqg-hook", pseq="1", **_PLANNED)]
    view = project_events(events)
    assert view.milestones == ()
    assert view.milestone_activity == ()        # hook source not a milestone-staleness anchor
    assert any("only eaf/manual drive" in w for w in view.warnings)


def test_milestone_time_fields_are_display_only():
    # acceptance ⑰: estimated_size / target_date / actual_wallclock are carried as
    # display strings; target_date does NOT enter completion (stage-count only).
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   target_date="2026-12-31"),
        _milestone("M0", "completed", 2, source="eaf", pseq="2",
                   actual_date="2026-06-08", actual_wallclock="9 minutes"),
    ]
    view = project_events(events)
    (m,) = view.milestones
    assert m.estimated_size == "~1 person-week; S"
    assert m.target_date == "2026-12-31"        # captured, display-only
    assert m.actual_wallclock == "9 minutes"
    # completion is stage-count: 1 completed / 1 effective = 100%, target_date irrelevant.
    assert completion(view).percent == 100


def test_milestones_sorted_by_first_appearance():
    events = [
        _milestone("M1", "planned", 2, source="manual", pseq="2", **_PLANNED),
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
    ]
    view = project_events(events)
    assert [m.milestone_id for m in view.milestones] == ["M0", "M1"]


def test_milestone_replan_within_source_seq_tiebreak():
    # acceptance ⑯ tie-break: two `started` from the same source order by
    # producer_seq within the action (the later seq's fields win).
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "started", 3, source="eaf", pseq="3", phase_label="late"),
        _milestone("M0", "started", 2, source="eaf", pseq="2", phase_label="early"),
    ]
    (m,) = project_events(events).milestones
    assert m.status == "started"
    assert m.phase_label == "late"              # higher producer_seq wins within action


# =============================================================================
# decision reduction (a4 §9) — raised → resolved
# =============================================================================


def test_decision_raised_is_open():
    view = project_events([_decision("D1", "raised", 1, question="Ship it?",
                                     options=["yes", "no"], rationale="weighing", blocks="M0")])
    (d,) = view.decisions
    assert isinstance(d, DecisionEntry)
    assert d.decision_id == "D1"
    assert d.status == "open"
    assert d.is_open is True
    assert d.question == "Ship it?"
    assert d.options == ("yes", "no")
    assert d.blocks == "M0"
    assert view.open_decisions == (d,)


def test_decision_resolved_is_closed():
    events = [
        _decision("D1", "raised", 1, pseq="1", question="Ship it?"),
        _decision("D1", "resolved", 2, pseq="2"),
    ]
    (d,) = project_events(events).decisions
    assert d.status == "resolved"
    assert d.is_open is False
    assert d.question == "Ship it?"             # carried from raised
    assert project_events(events).open_decisions == ()


def test_decision_out_of_order_resolves():
    # acceptance ②: resolved fed before raised → still reduces to resolved.
    events = [
        _decision("D1", "resolved", 2, pseq="2"),
        _decision("D1", "raised", 1, pseq="1", question="q"),
    ]
    (d,) = project_events(events).decisions
    assert d.status == "resolved"


def test_decision_reraise_absorbed_by_semantic_order():
    # A decision has no reopen action (unlike a milestone's replan), and `raised`
    # always sorts before `resolved` in action semantic order, so a same-id raise
    # interleaved with a resolve is absorbed — the terminal `resolved` still wins
    # (a re-decision must use a NEW decision_id). The decision is NOT in the pending-decisions section.
    events = [
        _decision("D1", "raised", 1, pseq="1", question="q1"),
        _decision("D1", "resolved", 2, pseq="2"),
        _decision("D1", "raised", 3, pseq="3", question="q2"),
    ]
    view = project_events(events)
    (d,) = view.decisions
    assert d.status == "resolved"
    assert view.open_decisions == ()


def test_decision_raised_at_anchors_age():
    (d,) = project_events([_decision("D1", "raised", 1, question="q",
                                     occurred_at="2026-06-01T10:00:00Z")]).decisions
    assert d.raised_at == "2026-06-01T10:00:00Z"


def test_decision_reraise_keeps_first_raised_at_for_age():
    # audit b0a5a67c gpt-f3 + deepseek-f1: a later same-id raise must NOT reset the
    # pending-age anchor — raised_at stays the FIRST raise, so an old-but-re-touched
    # open decision still trips the aged-decision red signal.
    events = [
        _decision("D1", "raised", 1, source="manual", pseq="1", question="q1",
                  occurred_at="2026-06-01T10:00:00Z"),   # old
        _decision("D1", "raised", 2, source="manual", pseq="2", question="q2",
                  occurred_at="2026-06-18T10:00:00Z"),   # recent re-raise
    ]
    view = project_events(events)
    (d,) = view.decisions
    assert d.status == "open"
    assert d.raised_at == "2026-06-01T10:00:00Z"         # FIRST raise, not the 06-18 re-raise
    # now=06-20: first-raise age 19d > 7 → red. (The last-raise bug would read 2d → not red.)
    light = health_light(view, now="2026-06-20T00:00:00Z")
    assert light.color == "red"
    assert any(s.code == "aged_decision" for s in light.signals)


def test_decision_missing_decision_id_skipped():
    bad = _se("manual:run-1:1", kind="decision", payload={"action": "raised", "question": "q"},
              ledger_seq=1, schema_version="1.1")
    view = project_events([bad])
    assert view.decisions == ()
    assert any("missing/invalid decision_id" in w for w in view.warnings)


def test_decision_open_sorted_before_resolved():
    events = [
        _decision("D-closed", "raised", 1, pseq="1", question="q"),
        _decision("D-closed", "resolved", 2, pseq="2"),
        _decision("D-open", "raised", 3, pseq="3", question="q2"),
    ]
    view = project_events(events)
    assert [d.decision_id for d in view.decisions] == ["D-open", "D-closed"]


def test_decision_eaf_source_drives():
    # decisions may come from eaf or manual (conformance allows both).
    (d,) = project_events([_decision("D1", "raised", 1, source="eaf", question="q")]).decisions
    assert d.status == "open"
    assert d.sources == ("eaf",)


# =============================================================================
# completion (a4 §5.4) — stage count, no division by zero
# =============================================================================


def _completed(mid, ls, **extra):
    return _milestone(mid, "completed", ls, source="eaf", pseq=str(ls),
                      actual_date="2026-06-08", **extra)


def test_completion_basic_ratio():
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _completed("M0b", 2),
        _completed("M1", 3),
    ]
    # M0 planned (effective, not completed); M0b, M1 completed → 2/3 effective, none cancelled.
    view = project_events(events)
    c = completion(view)
    assert c.total == 3
    assert c.effective == 3
    assert c.completed == 2
    assert abs(c.ratio - 2 / 3) < 1e-9
    assert c.percent == 67


def test_completion_percent_half_up():
    # acceptance: a progress % uses half-up rounding, not banker's — 1/8 reads 13%,
    # not round()'s 12% (audit b0a5a67c qwen-f2).
    events = [_completed("M0", 1)] + [
        _milestone(f"M{i}", "planned", i + 1, source="manual", pseq=str(i + 1), **_PLANNED)
        for i in range(1, 8)
    ]
    c = completion(project_events(events))
    assert c.completed == 1 and c.effective == 8     # 1/8 = 12.5%
    assert c.percent == 13                            # half-up, not banker's 12


def test_completion_all_cancelled_is_na():
    # acceptance ⑤: all milestones cancelled → effective 0 → ratio None (no ZeroDiv).
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "cancelled", 2, source="eaf", pseq="2"),
        _milestone("M1", "planned", 3, source="manual", pseq="3", **_PLANNED),
        _milestone("M1", "cancelled", 4, source="eaf", pseq="4"),
    ]
    c = completion(project_events(events))
    assert c.total == 2
    assert c.effective == 0
    assert c.ratio is None
    assert c.is_na is True
    assert c.percent is None


def test_completion_no_milestones_is_na():
    c = completion(project_events([_progress(1, "x")]))
    assert c.total == 0 and c.effective == 0 and c.ratio is None


def test_completion_excludes_cancelled_from_effective():
    events = [
        _completed("M0", 1),
        _milestone("M1", "planned", 2, source="manual", pseq="2", **_PLANNED),
        _milestone("M1", "cancelled", 3, source="eaf", pseq="3"),
    ]
    c = completion(project_events(events))
    assert c.total == 2 and c.effective == 1 and c.completed == 1
    assert c.ratio == 1.0                       # 1 completed / 1 effective; cancelled excluded


# =============================================================================
# rule-based health light (a4 §5.5) — no target_date dependency
# =============================================================================

# Fixed report times: FRESH is ~1 day after the default event time (not stale),
# FAR is ~3 weeks later (stale / aged). The light is a pure function of (view, now).
NOW_FRESH = "2026-05-30T00:00:00Z"
NOW_FAR = "2026-06-20T00:00:00Z"


def test_health_green_when_clear():
    events = [
        _milestone("M0", "completed", 1, source="eaf", pseq="1", actual_date="2026-05-29"),
        _progress(2, "done", phase="completed"),
    ]
    h = project_events(events)
    light = health_light(h, now=NOW_FRESH)
    assert isinstance(light, HealthLight)
    assert light.color == "green"
    assert light.signals == ()
    assert light.stale_days == 7


def test_health_red_open_critical_defect():
    events = [_defect("D-1", "opened", 1, source="eaf", pseq="1", title="boom", severity="critical")]
    light = health_light(project_events(events), now=NOW_FRESH)
    assert light.color == "red"
    assert any(s.code == "critical_defect" for s in light.signals)


def test_health_red_aged_open_decision():
    # an open decision raised > N days ago → red (a4 §5.5 pending decision > N days).
    events = [_decision("D1", "raised", 1, source="manual", question="q",
                        occurred_at="2026-06-01T10:00:00Z")]
    light = health_light(project_events(events), now=NOW_FAR)   # 19 days later
    assert light.color == "red"
    assert any(s.code == "aged_decision" for s in light.signals)


def test_health_fresh_open_decision_not_red():
    # a recently-raised open decision (<= N days) is NOT aged → no red.
    events = [_decision("D1", "raised", 1, source="manual", question="q",
                        occurred_at="2026-05-29T10:00:00Z")]
    light = health_light(project_events(events), now=NOW_FRESH)  # 1 day later
    assert not any(s.code == "aged_decision" for s in light.signals)


def test_health_resolved_decision_not_aged():
    # a resolved decision is not open → never triggers the aged-decision red.
    events = [
        _decision("D1", "raised", 1, source="manual", pseq="1", question="q",
                  occurred_at="2026-06-01T10:00:00Z"),
        _decision("D1", "resolved", 2, source="manual", pseq="2",
                  occurred_at="2026-06-02T10:00:00Z"),
    ]
    light = health_light(project_events(events), now=NOW_FAR)
    assert not any(s.code == "aged_decision" for s in light.signals)


def test_health_yellow_open_high_defect():
    events = [_defect("D-1", "opened", 1, source="eaf", pseq="1", title="t", severity="high")]
    light = health_light(project_events(events), now=NOW_FRESH)
    assert light.color == "yellow"
    assert any(s.code == "high_defect" for s in light.signals)


def test_health_yellow_blocked_from_latest_progress():
    events = [
        _progress(1, "kick", phase="started"),
        _progress(2, "stuck", phase="blocked"),
    ]
    light = health_light(project_events(events), now=NOW_FRESH)
    assert light.color == "yellow"
    assert any(s.code == "blocked" for s in light.signals)


def test_health_blocked_cleared_by_later_progress():
    # blocked is the CURRENT state — a later stage_advanced clears it (a3:T6).
    events = [
        _progress(1, "stuck", phase="blocked"),
        _progress(2, "unstuck", phase="stage_advanced"),
    ]
    light = health_light(project_events(events), now=NOW_FRESH)
    assert not any(s.code == "blocked" for s in light.signals)


def test_health_yellow_stale_source():
    # a driving source's latest milestone > N days old → stale → yellow.
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    light = health_light(project_events(events), now=NOW_FAR)   # 22 days later
    assert light.color == "yellow"
    assert any(s.code == "stale" for s in light.signals)


def test_health_fresh_milestone_not_stale():
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    light = health_light(project_events(events), now=NOW_FRESH)  # 1 day later
    assert not any(s.code == "stale" for s in light.signals)


def test_health_cancelled_milestone_not_red(_now=NOW_FRESH):
    # acceptance ⑨: a cancelled milestone must NOT trip the health light.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-05-29T10:00:00Z"),
        _milestone("M0", "cancelled", 2, source="eaf", pseq="2",
                   occurred_at="2026-05-29T11:00:00Z"),
    ]
    light = health_light(project_events(events), now=_now)
    assert light.color == "green"


def test_health_no_target_date_dependency():
    # acceptance ⑱: a milestone whose target_date is in the PAST but whose data is
    # FRESH must stay green — overdue target_date is never a red/health input.
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         target_date="2026-01-01",       # long past relative to now
                         occurred_at="2026-05-29T10:00:00Z")]
    light = health_light(project_events(events), now=NOW_FRESH)  # data only 1 day old
    assert light.color == "green"                # not red/yellow despite overdue target_date


def test_health_red_precedence_over_yellow():
    events = [
        _defect("D-c", "opened", 1, source="eaf", pseq="1", title="t", severity="critical"),
        _defect("D-h", "opened", 2, source="eaf", pseq="2", title="t", severity="high"),
    ]
    light = health_light(project_events(events), now=NOW_FRESH)
    assert light.color == "red"                  # critical present → red even with a high defect


def test_health_accepts_date_object_now():
    from datetime import date
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    light = health_light(project_events(events), now=date(2026, 6, 20))
    assert any(s.code == "stale" for s in light.signals)


def test_health_custom_stale_threshold():
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    # now is 5 days later: stale at N=3, fresh at N=7.
    now5 = "2026-06-03T00:00:00Z"
    assert health_light(project_events(events), now=now5, stale_days=3).color == "yellow"
    assert health_light(project_events(events), now=now5, stale_days=7).color == "green"


# =============================================================================
# coverage content-state × staleness (a4 §6.5) — acceptance ⑩
# =============================================================================


def test_coverage_no_milestone():
    cov = coverage_state(project_events([_progress(1, "x")]), now=NOW_FRESH)
    assert isinstance(cov, CoverageState)
    assert cov.content_state == "no-milestone"
    assert cov.is_stale is False


def test_coverage_all_cancelled():
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-05-29T10:00:00Z"),
        _milestone("M0", "cancelled", 2, source="eaf", pseq="2",
                   occurred_at="2026-05-29T11:00:00Z"),
    ]
    cov = coverage_state(project_events(events), now=NOW_FRESH)
    assert cov.content_state == "all-cancelled"


def test_coverage_in_progress():
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-05-29T10:00:00Z"),
        _milestone("M0", "started", 2, source="eaf", pseq="2",
                   occurred_at="2026-05-29T11:00:00Z"),
    ]
    cov = coverage_state(project_events(events), now=NOW_FRESH)
    assert cov.content_state == "in-progress"


def test_coverage_planned_only():
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    cov = coverage_state(project_events(events), now=NOW_FRESH)
    assert cov.content_state == "planned-only"


def test_coverage_in_progress_with_one_cancelled():
    # effective >= 1 with a started one → in-progress (cancelled excluded, not all-cancelled).
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-05-29T10:00:00Z"),
        _milestone("M0", "started", 2, source="eaf", pseq="2",
                   occurred_at="2026-05-29T11:00:00Z"),
        _milestone("M1", "planned", 3, source="manual", pseq="3", **_PLANNED,
                   occurred_at="2026-05-29T12:00:00Z"),
        _milestone("M1", "cancelled", 4, source="eaf", pseq="4",
                   occurred_at="2026-05-29T13:00:00Z"),
    ]
    cov = coverage_state(project_events(events), now=NOW_FRESH)
    assert cov.content_state == "in-progress"


def test_coverage_stale_is_per_source_not_project_max():
    # acceptance ⑩ / a3:T5: a fresh source must NOT mask a stalled source. manual's
    # latest milestone is 3 weeks old; eaf's is recent. Per-source ⇒ manual stale.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-05-29T10:00:00Z"),    # old
        _milestone("M1", "planned", 2, source="eaf", pseq="2", **_PLANNED,
                   occurred_at="2026-06-19T10:00:00Z"),    # fresh
    ]
    cov = coverage_state(project_events(events), now="2026-06-20T00:00:00Z")
    assert cov.is_stale is True
    assert cov.stale_sources == ("manual",)               # eaf fresh, manual stale — not masked


def test_coverage_fresh_not_stale():
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                   occurred_at="2026-06-19T10:00:00Z"),
        _milestone("M1", "planned", 2, source="eaf", pseq="2", **_PLANNED,
                   occurred_at="2026-06-19T11:00:00Z"),
    ]
    cov = coverage_state(project_events(events), now="2026-06-20T00:00:00Z")
    assert cov.is_stale is False
    assert cov.stale_sources == ()


def test_coverage_stale_orthogonal_to_content_state():
    # staleness layers on ANY content state — here planned-only AND stale.
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    cov = coverage_state(project_events(events), now=NOW_FAR)
    assert cov.content_state == "planned-only"
    assert cov.is_stale is True


# =============================================================================
# defensive: malformed timestamps / unknown actions never raise (read boundary)
# =============================================================================


def test_health_and_coverage_tolerate_malformed_occurred_at():
    # a manually-corrupted occurred_at must not crash the date math — it parses to
    # None (treated as not-stale), never raises (mirrors the read-boundary stance).
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="not-a-timestamp")]
    view = project_events(events)
    assert health_light(view, now=NOW_FAR).color == "green"      # unparseable → not stale
    cov = coverage_state(view, now=NOW_FAR)
    assert cov.is_stale is False


def test_health_tolerates_unparseable_now():
    events = [_milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED,
                         occurred_at="2026-05-29T10:00:00Z")]
    # a garbage `now` parses to None → no age-based signal, no crash.
    assert health_light(project_events(events), now="whenever").color == "green"


def test_milestone_unknown_action_ignored():
    # defensive: an action outside the contract enum (conformance rejects it at
    # ingress) is skipped with a warning, never applied.
    events = [
        _milestone("M0", "planned", 1, source="manual", pseq="1", **_PLANNED),
        _milestone("M0", "frobnicate", 2, source="eaf", pseq="2"),
    ]
    view = project_events(events)
    (m,) = view.milestones
    assert m.status == "planned"
    assert m.ignored_events == 1
    assert any("unknown action" in w for w in view.warnings)
