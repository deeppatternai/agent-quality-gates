#!/usr/bin/env python3
"""Self-test for aqg_project_status.py — runs the CLI end-to-end against a temp
XDG_DATA_HOME ledger (no real ~/.aqg touched).

Covers:
- happy path: text / json / html render a real project's events
- empty / missing ledger → exit 0 with an empty report (+ stderr note)
- drain-on-view: a pending inbox event is drained + surfaced in the report
- --no-drain: strictly-read view leaves the pending event in the inbox
- no-op when empty: nothing to drain → no events.jsonl is fabricated
- bad --format → argparse exit 2
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import aqg_project_status as cli

_REPO_ROOT = Path(__file__).resolve().parents[3]

_EVENTS = [
    '{"schema_version":"1.0","event_id":"eaf:r1:1","project":"o/r","source":"eaf",'
    '"source_ref":{"run_id":"r1"},"kind":"progress",'
    '"payload":{"phase_event":"started","title":"Kick off"},'
    '"occurred_at":"2026-05-29T10:00:00Z","recorded_at":"2026-05-29T12:00:00+00:00","ledger_seq":1}',
    '{"schema_version":"1.0","event_id":"eaf:r1:2","project":"o/r","source":"eaf",'
    '"source_ref":{"run_id":"r1"},"kind":"defect",'
    '"payload":{"defect_id":"D-1","action":"opened","title":"Leak","severity":"critical"},'
    '"occurred_at":"2026-05-29T10:05:00Z","recorded_at":"2026-05-29T12:00:00+00:00","ledger_seq":2}',
]


@contextmanager
def _env(name: str, value: str):
    """Set env var `name`=`value` for the duration, restoring the prior value."""
    prev = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


@contextmanager
def _data_home(tmp: str):
    # Keep self-tests bound to the checkout under test even when the caller's
    # shell has AQG_ROOT pointing at another AQG clone.
    with _env("XDG_DATA_HOME", tmp), _env("AQG_ROOT", str(_REPO_ROOT)):
        yield


@contextmanager
def _patch_consume(fake):
    """Swap ledger.store.consume_inbox for the duration. The script's _drain_inbox does
    a lazy `from ledger.store import consume_inbox`, so patching the module attribute
    takes effect at call time. Bootstraps the contracts path first."""
    cli._bootstrap_contracts()
    import ledger.store as store_mod
    orig = store_mod.consume_inbox
    store_mod.consume_inbox = fake
    try:
        yield
    finally:
        store_mod.consume_inbox = orig


@contextmanager
def _patch_collect_run(fake):
    """Swap collect_repo_reality.run so --with-repo-reality exercises the banner with
    no real git/gh subprocess. _bootstrap_contracts first (collect imports ledger.*)."""
    cli._bootstrap_contracts()
    import collect_repo_reality as cr
    orig = cr.run
    cr.run = fake
    try:
        yield
    finally:
        cr.run = orig


def _seed_ledger(data_home: str, project_id: str = "o/r") -> Path:
    cli._bootstrap_contracts()
    from ledger.paths import project_events_path

    events = project_events_path(project_id)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("\n".join(_EVENTS) + "\n", encoding="utf-8")
    return events


# A pending IncomingEvent (no recorded_at/ledger_seq — those are added on drain)
# dropped into the inbox to exercise drain-on-view.
_PENDING = {
    "schema_version": "1.0", "event_id": "eaf:r2:9", "project": "o/r", "source": "eaf",
    "source_ref": {"run_id": "r2"}, "kind": "progress",
    "payload": {"phase_event": "completed", "title": "Drained on view"},
    "occurred_at": "2026-05-29T11:00:00Z",
}


def _seed_inbox(data_home: str, event: dict) -> Path:
    """Drop an IncomingEvent through the production writer so drain-on-view consumes it."""
    cli._bootstrap_contracts()
    from ledger.paths import write_incoming_event

    inbox = Path(data_home) / "aqg" / "ledger" / "_inbox"
    return write_incoming_event(event, inbox_dir=inbox)


def _run(argv) -> "tuple[int, str, str]":
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


def test_text_render_happy_path() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, out
    assert "Project Ledger — o/r" in out
    assert "Kick off" in out
    assert "D-1" in out and "CRITICAL" in out


def test_json_render_parses() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--json"])
    assert rc == 0
    data = json.loads(out)
    assert data["project_id"] == "o/r"
    assert data["event_count"] == 2


def test_html_default_is_complete_doc() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r"])   # html is the default
    assert rc == 0
    assert out.strip().startswith("<!doctype html>")


def test_empty_ledger_is_exit_0() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        rc, out, err = _run(["--project-id", "no/such", "--format", "text"])
    assert rc == 0, (out, err)
    assert "0 events" in out
    assert "no ledger yet" in err


def test_no_op_when_inbox_empty_creates_no_ledger() -> None:
    # drain-on-view is a no-op when there is nothing to drain: a project with no log
    # AND no pending inbox events gets no fabricated events.jsonl.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _run(["--project-id", "ghost/repo", "--format", "text"])
        created = Path(tmp) / "aqg" / "ledger" / "ghost" / "repo" / "events.jsonl"
        assert not created.exists(), "empty inbox must not fabricate a ledger"


def test_drain_on_view_surfaces_pending_inbox_event() -> None:
    # default behavior: the report drains the inbox first, so an event pushed since
    # the last drain (EAF exporter / v2 hook) shows up in the rendered report.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)                 # 2 already-stored events
        _seed_inbox(tmp, _PENDING)        # 1 pending inbox event for the same project
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, out
    assert "Drained on view" in out


def test_no_drain_flag_is_strictly_read() -> None:
    # --no-drain: the pending event is NOT surfaced and stays in the inbox (unconsumed).
    # NB: the inbox-file assertion must run INSIDE the tempdir context (the dir is
    # deleted on exit, which would make .exists() spuriously False).
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        inbox_file = _seed_inbox(tmp, _PENDING)
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text", "--no-drain"])
        assert rc == 0, out
        assert "Drained on view" not in out
        assert inbox_file.exists(), "--no-drain must leave the inbox event unconsumed"


def test_drain_failure_renders_stored_state() -> None:
    # never-fail: if the drain RAISES, the report still renders stored events (rc 0)
    # with a stderr note (impl-audit bd6440a5 C).
    def _boom(**_):
        raise OSError("simulated drain failure")

    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), _patch_consume(_boom):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--format", "text"])
        assert rc == 0, (out, err)
        assert "Kick off" in out               # stored events still rendered
        assert "drain skipped" in err          # degradation noted on stderr


def test_drain_skipped_locked_warns() -> None:
    # consume_inbox REPORTS lock-held (does not raise) → the skill notes a possibly-
    # stale view on stderr and renders current stored state (impl-audit bd6440a5 A).
    class _Report:
        skipped_locked = True
        deferred: list = []
        failed: list = []

    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), _patch_consume(lambda **_: _Report()):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--format", "text"])
        assert rc == 0, (out, err)
        assert "Kick off" in out
        assert "another consumer holds the lock" in err


class _DrainReport:
    """A fake consume_inbox report (the real one does not raise on lock/poison/
    deferred — it REPORTS them)."""
    def __init__(self, *, skipped_locked=False, deferred=None, failed=None):
        self.skipped_locked = skipped_locked
        self.deferred = deferred or []
        self.failed = failed or []


# B7-inband (#183): data-quality notes (lock-skipped / deferred / dead-lettered /
# drain-exception) must render IN-BAND in the report, not ONLY on stderr — a
# consumer who only reads the report (redirected to a file, piped) must still see
# that events were dropped (EAF eaf-runtime-status telemetry_errors pattern).


def test_drain_skipped_locked_renders_in_band() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), \
            _patch_consume(lambda **_: _DrainReport(skipped_locked=True)):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, (out, err)
    assert "Kick off" in out                              # stored events still render
    assert "another consumer holds the lock" in err       # stderr unchanged
    assert "Data notes" in out                             # in-band section present
    assert "another consumer holds the lock" in out, out   # AND in-band


def test_drain_dead_lettered_renders_in_band() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), \
            _patch_consume(lambda **_: _DrainReport(failed=[("e9.json", "bad json")])):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, (out, err)
    assert "dead-lettered" in err                          # stderr unchanged
    assert "Data notes" in out and "dead-lettered" in out, out


def test_drain_deferred_renders_in_band() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), \
            _patch_consume(lambda **_: _DrainReport(deferred=[("e8.json", "transient")])):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, out
    assert "deferred" in out, out


def test_drain_exception_renders_in_band() -> None:
    def _boom(**_):
        raise OSError("simulated drain failure")

    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), _patch_consume(_boom):
        _seed_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0, (out, err)
    assert "drain skipped" in err                          # stderr unchanged
    assert "Data notes" in out and "drain skipped" in out, out


def test_drain_notes_in_band_json() -> None:
    # machine consumers (JSON) must see the dropped-event notes in `warnings` too
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), \
            _patch_consume(lambda **_: _DrainReport(skipped_locked=True)):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--json"])
    assert rc == 0, out
    data = json.loads(out)
    assert any("another consumer holds the lock" in w for w in data["warnings"]), data["warnings"]


def test_drain_note_untrusted_text_escaped_in_band() -> None:
    # audit f93ffaab gpt-5.5 f3: the in-band drain note now carries runtime text
    # (e.g. an exception message). Untrusted angle-bracket content must be escaped
    # by the renderer on this NEW warnings source — pin the contract in html.
    def _boom(**_):
        raise ValueError("<script>alert(1)</script>")

    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), _patch_consume(_boom):
        _seed_ledger(tmp)
        rc, html_out, _ = _run(["--project-id", "o/r", "--format", "html"])
        _seed_ledger(tmp)
        rc_md, md_out, _ = _run(["--project-id", "o/r", "--format", "markdown"])
    assert rc == 0 and rc_md == 0
    assert "&lt;script&gt;" in html_out, html_out          # html-escaped
    assert "<script>alert(1)</script>" not in html_out      # raw tag neutralized
    assert "<script>alert(1)</script>" not in md_out        # markdown escapes it too


def test_no_drain_adds_no_inband_notes() -> None:
    # --no-drain skips the drain entirely → no drain notes injected; a clean ledger
    # renders without a Data notes section.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text", "--no-drain"])
    assert rc == 0, out
    assert "Data notes" not in out, out


def test_with_repo_reality_renders_banner() -> None:
    # --with-repo-reality appends the reconciliation banner (counts only); collect's
    # run is stubbed so no real git/gh subprocess fires (issue #245).
    def _fake(cmd, cwd=None, timeout=30):
        if cmd[0] == "git":
            return (0, "false", "") if "rev-parse" in cmd else (0, "8", "")
        return (0, '[{"mergedAt":"2026-06-02T00:00:00Z"}]', "")

    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp), _patch_collect_run(_fake):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--repo", tmp, "--format", "text", "--with-repo-reality"])
    assert rc == 0, out
    assert "8 commits" in out
    assert "coverage gap" in out


def test_default_path_has_no_repo_reality_banner() -> None:
    # opt-in: without the flag the report carries no reconciliation banner.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_ledger(tmp)
        rc, out, _ = _run(["--project-id", "o/r", "--format", "text"])
    assert rc == 0
    assert "coverage gap" not in out


def test_bad_format_exits_2() -> None:
    try:
        _run(["--project-id", "o/r", "--format", "pdf"])
    except SystemExit as exc:
        assert exc.code == 2, exc.code
    else:
        raise AssertionError("expected SystemExit(2) for an invalid --format")


def test_json_and_format_conflict_exits_2() -> None:
    # --json and --format are mutually exclusive (audit ed629637 f3)
    try:
        with redirect_stderr(io.StringIO()):
            cli.main(["--project-id", "o/r", "--json", "--format", "text"])
    except SystemExit as exc:
        assert exc.code == 2, exc.code
    else:
        raise AssertionError("expected SystemExit(2) for --json combined with --format")


def test_missing_contracts_exits_3() -> None:
    # AQG_ROOT pointing at a tree without contracts/ledger/ → config error exit 3,
    # not an uncaught traceback (audit ed629637 f2).
    with tempfile.TemporaryDirectory() as tmp, _env("AQG_ROOT", tmp):
        try:
            with redirect_stderr(io.StringIO()):
                cli.main(["--project-id", "o/r", "--format", "text"])
        except SystemExit as exc:
            assert exc.code == 3, exc.code
        else:
            raise AssertionError("expected SystemExit(3) when contracts are unresolvable")


# --- §4.2 injection-proof translation CLI -------------------------------------

_MILESTONE_EVENTS = [
    '{"schema_version":"1.1","event_id":"manual:wp:1","project":"o/r","source":"manual",'
    '"source_ref":{},"kind":"milestone","payload":{"milestone_id":"M0","action":"planned",'
    '"title":"Ship the API","summary":"add the endpoint","key_outcome":"users can call it",'
    '"estimated_size":"about 2 weeks"},'
    '"occurred_at":"2026-06-05T10:00:00Z","recorded_at":"2026-06-05T12:00:00+00:00","ledger_seq":1}',
    '{"schema_version":"1.1","event_id":"eaf:r:2","project":"o/r","source":"eaf",'
    '"source_ref":{},"kind":"milestone","payload":{"milestone_id":"M0","action":"started"},'
    '"occurred_at":"2026-06-06T10:00:00Z","recorded_at":"2026-06-06T12:00:00+00:00","ledger_seq":2}',
]


def _seed_milestone_ledger(data_home: str, project_id: str = "o/r") -> Path:
    cli._bootstrap_contracts()
    from ledger.paths import project_events_path

    events = project_events_path(project_id)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text("\n".join(_MILESTONE_EVENTS) + "\n", encoding="utf-8")
    return events


def test_emit_translation_segments() -> None:
    # §4.2 step 1: emit ONLY producer free-text — never the facts.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_milestone_ledger(tmp)
        rc, out, err = _run(["--project-id", "o/r", "--no-drain", "--emit-translation-segments"])
    assert rc == 0, err
    data = json.loads(out)
    assert "Ship the API" in data["segments"] and "add the endpoint" in data["segments"]
    assert "M0" not in data["segments"] and "started" not in data["segments"]   # facts excluded


def test_apply_translation_good_localizes_text_keeps_facts() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_milestone_ledger(tmp)
        _, segout, _ = _run(["--project-id", "o/r", "--no-drain", "--emit-translation-segments"])
        segs = json.loads(segout)["segments"]
        trf = Path(tmp) / "tr.json"
        trf.write_text(json.dumps({"segments": segs, "translations": [f"✦{s}" for s in segs],
                                   "target_lang": "zh"}), encoding="utf-8")
        rc, out, err = _run(["--project-id", "o/r", "--no-drain", "--format", "text",
                             "--apply-translation", str(trf), "--lang", "zh"])
    assert rc == 0, err
    assert "✦Ship the API" in out                # producer free-text localized
    assert "In progress" in out                  # status label LOCKED (English fact)


def test_apply_translation_hostile_fact_falls_back() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_milestone_ledger(tmp)
        _, segout, _ = _run(["--project-id", "o/r", "--no-drain", "--emit-translation-segments"])
        segs = json.loads(segout)["segments"]
        badf = Path(tmp) / "bad.json"
        badf.write_text(json.dumps({"segments": segs,
                                    "translations": [f"{s} 100% done" for s in segs],
                                    "target_lang": "zh"}), encoding="utf-8")
        rc, out, err = _run(["--project-id", "o/r", "--no-drain", "--format", "text",
                             "--apply-translation", str(badf), "--lang", "zh"])
    assert rc == 0, err
    assert "100% done" not in out                # fabricated fact rejected
    assert "Ship the API" in out                 # English canonical delivered
    assert "withheld" in err                     # stderr note surfaces the fallback


def test_apply_translation_malformed_file_falls_back() -> None:
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_milestone_ledger(tmp)
        badf = Path(tmp) / "nope.json"
        badf.write_text("not json{", encoding="utf-8")
        rc, out, err = _run(["--project-id", "o/r", "--no-drain", "--format", "text",
                             "--apply-translation", str(badf), "--lang", "zh"])
    assert rc == 0, err
    assert "Ship the API" in out                 # malformed file → English canonical, no crash


def test_apply_translation_malformed_types_falls_back() -> None:
    # audit c6050ee7 gpt-f2 / gemini-f4: valid JSON with WRONG value types
    # (non-list segments, non-string target_lang) must fall back to English, not
    # raise and exit 70.
    with tempfile.TemporaryDirectory() as tmp, _data_home(tmp):
        _seed_milestone_ledger(tmp)
        badf = Path(tmp) / "types.json"
        badf.write_text('{"segments": 123, "translations": null, "target_lang": 456}',
                        encoding="utf-8")
        rc, out, err = _run(["--project-id", "o/r", "--no-drain", "--format", "text",
                             "--apply-translation", str(badf), "--lang", "zh"])
    assert rc == 0, err                          # no crash, no exit 70
    assert "Ship the API" in out                 # English canonical delivered


TESTS = [
    test_text_render_happy_path,
    test_json_render_parses,
    test_html_default_is_complete_doc,
    test_empty_ledger_is_exit_0,
    test_no_op_when_inbox_empty_creates_no_ledger,
    test_drain_on_view_surfaces_pending_inbox_event,
    test_no_drain_flag_is_strictly_read,
    test_drain_failure_renders_stored_state,
    test_drain_skipped_locked_warns,
    test_drain_skipped_locked_renders_in_band,
    test_drain_dead_lettered_renders_in_band,
    test_drain_deferred_renders_in_band,
    test_drain_exception_renders_in_band,
    test_drain_notes_in_band_json,
    test_drain_note_untrusted_text_escaped_in_band,
    test_no_drain_adds_no_inband_notes,
    test_with_repo_reality_renders_banner,
    test_default_path_has_no_repo_reality_banner,
    test_bad_format_exits_2,
    test_json_and_format_conflict_exits_2,
    test_missing_contracts_exits_3,
    test_emit_translation_segments,
    test_apply_translation_good_localizes_text_keeps_facts,
    test_apply_translation_hostile_fact_falls_back,
    test_apply_translation_malformed_file_falls_back,
    test_apply_translation_malformed_types_falls_back,
]


if __name__ == "__main__":
    for t in TESTS:
        t()
    print(f"OK: aqg_project_status self-test passed ({len(TESTS)} tests)")
