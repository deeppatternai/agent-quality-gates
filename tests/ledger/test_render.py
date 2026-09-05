"""Behavior tests for ProjectView rendering (text / markdown / html / json).

Focus on real, security-relevant behavior — not cosmetics:
- HTML escapes every dynamic string (untrusted: aqg-hook captures git text) → no
  unescaped <script>; a non-http pr_url is NOT emitted as a clickable href
- markdown table cells escape '|' so a stray pipe can't break the table
- the §5.6 coverage caveat appears iff has_low_fidelity
- open defects vs resolved are grouped; empty states render explicitly
- json carries the structured field values; unknown format raises
"""
from __future__ import annotations

import json

from ledger.projection import (
    DecisionEntry,
    DefectEntry,
    HandoffEntry,
    MilestoneEntry,
    ProgressEntry,
    ProjectView,
    SourceActivity,
    SourceStat,
)
from ledger.render import FORMATS, render, render_html, render_json, render_markdown, render_text


def _view(
    *,
    project_id="o/r",
    progress=(),
    defects=(),
    milestones=(),
    decisions=(),
    handoffs=(),
    sources=(SourceStat("eaf", "high", 1),),
    milestone_activity=(),
    last_activity="2026-05-29T10:00:00Z",
    has_low_fidelity=False,
    warnings=(),
):
    return ProjectView(
        project_id=project_id,
        event_count=len(progress) + len(defects) + len(handoffs),
        progress=tuple(progress),
        defects=tuple(defects),
        milestones=tuple(milestones),
        decisions=tuple(decisions),
        handoffs=tuple(handoffs),
        sources=tuple(sources),
        milestone_activity=tuple(milestone_activity),
        last_activity=last_activity,
        has_low_fidelity=has_low_fidelity,
        warnings=tuple(warnings),
    )


def _milestone(milestone_id="M0", status="planned", title="Build the thing", **over):
    base = dict(
        milestone_id=milestone_id, status=status, title=title, summary=None,
        key_outcome=None, estimated_size="~2 person-weeks; M", target_date=None,
        planned_start=None, actual_date=None, actual_wallclock=None, gate=None,
        phase_label=None, first_seq=1, last_seq=1, occurred_at="2026-05-29T10:00:00Z",
        sources=("eaf",), replanned=False, ignored_events=0,
    )
    base.update(over)
    return MilestoneEntry(**base)


def _decision(decision_id="D1", status="open", question="Ship it?", **over):
    base = dict(
        decision_id=decision_id, status=status, question=question, options=(),
        rationale=None, blocks=None, first_seq=1, last_seq=1,
        raised_at="2026-05-29T10:00:00Z", occurred_at="2026-05-29T10:00:00Z",
        sources=("manual",), ignored_events=0,
    )
    base.update(over)
    return DecisionEntry(**base)


def _defect(defect_id="D-1", status="open", severity="high", title="boom", **over):
    base = dict(
        defect_id=defect_id, status=status, severity=severity, title=title,
        first_seq=1, last_seq=1, occurred_at="2026-05-29T10:00:00Z", sources=("eaf",),
        fixed_commit=None, verification=None, regression_anchor=None, ignored_events=0,
    )
    base.update(over)
    return DefectEntry(**base)


def _progress(title="did a thing", *, ledger_seq=1, phase_event="stage_advanced",
              source="eaf", pr_url=None, commit_sha=None, detail=None):
    return ProgressEntry(
        ledger_seq=ledger_seq, phase_event=phase_event, title=title, source=source,
        occurred_at="2026-05-29T10:00:00Z", detail=detail, pr_url=pr_url, commit_sha=commit_sha,
    )


# --- dispatcher ---------------------------------------------------------------


def test_render_dispatch_matches_named_renderers():
    v = _view(progress=[_progress()])
    assert render(v, "html") == render_html(v)
    assert render(v, "markdown") == render_markdown(v)
    assert render(v, "text") == render_text(v)
    assert render(v, "json") == render_json(v)


def test_render_default_is_html():
    v = _view(progress=[_progress()])
    assert render(v) == render_html(v)


def test_render_unknown_format_raises():
    try:
        render(_view(), "pdf")
    except ValueError as exc:
        assert "pdf" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("expected ValueError for unknown format")


def test_formats_constant():
    assert FORMATS == ("html", "markdown", "text", "json")


# --- HTML escaping (security) -------------------------------------------------


def test_html_escapes_defect_title():
    v = _view(defects=[_defect(title="<script>alert(1)</script>")])
    out = render_html(v)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in out


def test_html_escapes_progress_title_and_commit():
    v = _view(progress=[_progress(title="fix <b>&</b> stuff", commit_sha="<i>x</i>")])
    out = render_html(v)
    assert "<b>&</b>" not in out
    assert "fix &lt;b&gt;&amp;&lt;/b&gt; stuff" in out
    assert "<i>x</i>" not in out


def test_html_non_http_pr_url_not_a_link():
    v = _view(progress=[_progress(pr_url="javascript:alert(1)")])
    out = render_html(v)
    assert 'href="javascript:' not in out
    assert "javascript:alert(1)" in out          # shown as escaped text, not href


def test_html_http_pr_url_is_a_link():
    v = _view(progress=[_progress(pr_url="https://example.com/pr/1")])
    out = render_html(v)
    assert '<a href="https://example.com/pr/1">PR</a>' in out


def test_html_is_a_complete_document():
    out = render_html(_view())
    assert out.startswith("<!doctype html>")
    assert out.rstrip().endswith("</html>")


# --- coverage caveat (§5.6 C5) -----------------------------------------------


def test_caveat_present_when_low_fidelity():
    v = _view(has_low_fidelity=True, sources=(SourceStat("aqg-hook", "low", 1),))
    assert "Coverage caveat" in render_html(v)
    assert "Coverage caveat" in render_markdown(v)
    assert "Coverage caveat" in render_text(v)


def test_caveat_absent_when_high_fidelity_only():
    v = _view(has_low_fidelity=False)
    assert "Coverage caveat" not in render_html(v)
    assert "Coverage caveat" not in render_markdown(v)
    assert "Coverage caveat" not in render_text(v)


# --- defect grouping + empty states ------------------------------------------


def test_open_and_resolved_defects_grouped():
    v = _view(defects=[
        _defect("OPEN-1", status="open", severity="critical"),
        _defect("DONE-1", status="closed", severity="low", title="old bug"),
    ])
    md = render_markdown(v)
    assert "## Open Defects (1)" in md
    assert "## Resolved & Closed Defects (1)" in md
    # open section lists OPEN-1 before the resolved section mentions DONE-1
    assert md.index("OPEN-1") < md.index("DONE-1")


def test_no_open_defects_message():
    v = _view(defects=[_defect("DONE-1", status="fixed")])
    assert "No open defects." in render_text(v)
    assert "_No open defects._" in render_markdown(v)


def test_no_progress_message():
    assert "no progress events" in render_text(_view()).lower()
    assert "No progress events" in render_markdown(_view())


# --- markdown table safety + link injection -----------------------------------


def test_markdown_escapes_pipe_in_defect_title():
    v = _view(defects=[_defect(title="a | b breaks tables")])
    md = render_markdown(v)
    assert "a \\| b breaks tables" in md
    assert "a | b breaks tables" not in md


def test_markdown_url_injection_rendered_as_escaped_text():
    # A URL that starts with https:// but carries markdown link delimiters must
    # NOT be emitted as a [PR](...) link, and its delimiters must be escaped so it
    # can't inject a second link (audit 33f6e5df gpt-f1).
    v = _view(progress=[_progress(pr_url="https://safe/) [x](javascript:alert(1))")])
    md = render_markdown(v)
    assert "[PR](" not in md                         # unsafe URL not linked
    assert "](javascript:alert(1))" not in md         # injection delimiters escaped
    assert "PR:" in md                                # shown as plain (escaped) text


def test_markdown_clean_http_url_is_a_link():
    v = _view(progress=[_progress(pr_url="https://example.com/pr/1")])
    assert "[PR](https://example.com/pr/1)" in render_markdown(v)


def test_markdown_escapes_raw_html_in_title():
    v = _view(progress=[_progress(title="<script>alert(1)</script>")])
    md = render_markdown(v)
    assert "<script>" not in md                       # raw HTML neutralized in markdown


def test_markdown_escapes_backtick_in_phase_event():
    # phase_event is also escaped — a backtick must not close a code span and
    # inject markdown into the docx intermediate (audit dfdcae4d f1).
    v = _view(progress=[_progress(phase_event="x`</script><b>")])
    md = render_markdown(v)
    assert "`</script>" not in md                     # backtick + raw HTML neutralized
    assert "<b>" not in md


# --- optional fields rendered in human formats (audit gpt-f4) -----------------


def test_progress_detail_rendered_all_formats():
    v = _view(progress=[_progress(detail="longer progress context")])
    assert "longer progress context" in render_text(v)
    assert "longer progress context" in render_markdown(v)
    assert "longer progress context" in render_html(v)


def test_defect_verification_and_regression_rendered_all_formats():
    # values free of markdown-special chars so the cross-format assertion is clean
    # (underscore/pipe escaping is covered by the dedicated escaping tests).
    v = _view(defects=[_defect("D-1", status="fixed",
                               verification="verified via CI", regression_anchor="tests/leak-check")])
    for renderer in (render_text, render_markdown, render_html):
        out = renderer(v)
        assert "verified via CI" in out
        assert "tests/leak-check" in out


# --- handoffs + sources -------------------------------------------------------


def test_handoffs_and_sources_rendered():
    v = _view(
        handoffs=[HandoffEntry(1, "/repo/HANDOFF.md", "eaf-handoff", "eaf", "2026-05-29T10:00:00Z")],
        sources=(SourceStat("eaf", "high", 3), SourceStat("aqg-hook", "low", 1)),
        has_low_fidelity=True,
    )
    txt = render_text(v)
    assert "/repo/HANDOFF.md" in txt
    assert "eaf: 3 events (high fidelity)" in txt
    assert "aqg-hook: 1 events (low fidelity / inferred)" in txt


# --- json ---------------------------------------------------------------------


def test_json_carries_structured_values():
    v = _view(
        defects=[_defect("D-1", status="fixed", fixed_commit="abc")],
        progress=[_progress("kickoff")],
    )
    data = json.loads(render_json(v))
    assert data["project_id"] == "o/r"
    assert data["defects"][0]["defect_id"] == "D-1"
    assert data["defects"][0]["status"] == "fixed"
    assert data["defects"][0]["fixed_commit"] == "abc"
    assert data["progress"][0]["title"] == "kickoff"


def test_json_is_deterministic():
    v = _view(progress=[_progress()])
    assert render_json(v) == render_json(v)


# --- warnings -----------------------------------------------------------------


def test_warnings_capped_in_output():
    many = tuple(f"warn {i}" for i in range(30))
    v = _view(warnings=many)
    txt = render_text(v)
    assert "Data notes (30)" in txt
    assert "...and 10 more" in txt          # 30 - cap(20)
    assert "warn 0" in txt
    assert "warn 25" not in txt             # beyond the cap


# =============================================================================
# P3a — business progress report (a4 §6.1 progressive disclosure)
# =============================================================================

NOW = "2026-06-08T00:00:00Z"


def test_no_milestone_no_decision_is_backward_compatible():
    # the business block is gated on milestones-or-decisions; a pure engineering
    # ledger (the existing tests' shape) renders byte-identically with/without `now`.
    v = _view(defects=[_defect()], progress=[_progress()])
    assert render_markdown(v) == render_markdown(v, now=NOW)
    md = render_markdown(v, now=NOW)
    assert "## Milestones" not in md
    assert "Needs your decision" not in md
    assert "Open Defects" in md             # engineering sections still top-level


def test_markdown_health_line_one_liner_green():
    v = _view(milestones=[_milestone("M0", "completed", actual_date="2026-06-01")])
    md = render_markdown(v, now=NOW)
    assert "🟢" in md
    assert "On track" in md
    # the default health line is a one-liner — the RULE matrix is NOT dumped here.
    assert "open critical defect · " not in md      # no rule enumeration in the default view


def test_markdown_health_line_red_critical_defect():
    v = _view(
        milestones=[_milestone("M0", "started")],
        defects=[_defect("D-9", status="open", severity="critical", title="boom")],
    )
    md = render_markdown(v, now=NOW)
    assert "🔴" in md
    assert "critical" in md.lower()
    assert "D-9" in md                              # the offending id surfaces in the one-liner


def test_markdown_completion_bar_proportional():
    ms = [_milestone(f"M{i}", "completed", actual_date="2026-06-01") for i in range(3)]
    ms += [_milestone(f"P{i}", "planned") for i in range(7)]   # 3/10 effective done
    md = render_markdown(_view(milestones=ms), now=NOW)
    assert "30%" in md
    assert "▓" in md and "░" in md                  # proportional bar, not all-filled


def test_completion_bar_na_no_div_by_zero():
    # effective==0 (e.g. a single cancelled among... actually all-cancelled is its own
    # fallback) — guard the N/A path directly via the completion helper output: a view
    # whose only effective milestones are 0 must never divide. Covered by the projection
    # Completion(ratio=None); here we assert all-cancelled renders WITHOUT a crash.
    ms = [_milestone("M0", "cancelled"), _milestone("M1", "cancelled")]
    md = render_markdown(_view(milestones=ms), now=NOW)        # must not raise
    assert isinstance(md, str) and md


def test_markdown_all_cancelled_note_no_table():
    # §6.5 (audit facb0e5f gpt-f2): all-cancelled → a note, NOT the milestone table
    # or an N/A completion bar.
    ms = [_milestone("M0", "cancelled"), _milestone("M1", "cancelled")]
    md = render_markdown(_view(milestones=ms), now=NOW)
    assert "cancelled — no active plan" in md       # the note
    assert "## Milestones" not in md                # table suppressed
    assert "**Completion:**" not in md              # N/A bar suppressed


def test_markdown_planned_only_is_zero_percent():
    md = render_markdown(_view(milestones=[_milestone("M0", "planned")]), now=NOW)
    assert "0%" in md
    assert "Planned" in md                          # status label


def test_markdown_milestone_table_shows_status_estimate_completed():
    v = _view(milestones=[
        _milestone("M0", "completed", title="Ship API", actual_date="2026-06-01",
                   actual_wallclock="14 minutes"),
        _milestone("M1", "started", title="Build UI", estimated_size="~3 person-weeks; L"),
    ])
    md = render_markdown(v, now=NOW)
    assert "Ship API" in md and "Build UI" in md
    assert "Done" in md and "In progress" in md     # status labels
    assert "2026-06-01" in md                       # actual completion date
    assert "14 minutes" in md                       # optional wall-clock
    assert "~3 person-weeks; L" in md               # estimate / size


def test_markdown_no_early_late_delta():
    # a4 §6 [a4:T]: a completed milestone shows the actual date, NEVER "ahead/behind N days".
    v = _view(milestones=[_milestone("M0", "completed", actual_date="2026-06-01",
                                     target_date="2026-07-01")])
    md = render_markdown(v, now=NOW)
    assert "ahead" not in md.lower()
    assert "behind" not in md.lower()
    assert "days early" not in md.lower()


def test_markdown_needs_decision_lists_open_decisions():
    v = _view(decisions=[
        _decision("D1", "open", question="Default view or folded?",
                  options=("Default", "Folded"), rationale="clarity vs clutter", blocks="M0"),
        _decision("D2", "resolved", question="already decided"),
    ])
    md = render_markdown(v, now=NOW)
    assert "Needs your decision" in md
    assert "Default view or folded?" in md
    assert "already decided" not in md              # resolved decisions are not pending
    assert "M0" in md                               # blocks which milestone
    assert "clarity vs clutter" in md               # business rationale (a4 §6.1)


def test_markdown_stale_banner():
    v = _view(
        milestones=[_milestone("M0", "planned", occurred_at="2026-05-01T10:00:00Z")],
        milestone_activity=(SourceActivity("eaf", "2026-05-01T10:00:00Z"),),
    )
    md = render_markdown(v, now=NOW)                 # 2026-06-08, >7d after 05-01
    assert "stale" in md.lower()


def test_markdown_more_details_folds_engineering_and_method():
    v = _view(
        milestones=[_milestone("M0", "started")],
        progress=[_progress("kickoff")],
        defects=[_defect("D-1")],
    )
    md = render_markdown(v, now=NOW)
    assert "More details" in md                     # progressive-disclosure folded section
    # engineering detail still present, but below the business overview
    assert md.index("## Milestones") < md.index("More details")
    assert "kickoff" in md                          # progress folded under details
    assert "threshold" in md.lower()                # health-rule + N footnote (§5.5)


def test_html_escapes_milestone_title():
    v = _view(milestones=[_milestone("M0", "started", title="<script>alert(1)</script>")])
    out = render_html(v, now=NOW)
    assert "<script>alert(1)</script>" not in out
    assert "&lt;script&gt;" in out


def test_markdown_escapes_decision_question_pipe():
    v = _view(decisions=[_decision("D1", "open", question="a | b breaks tables")])
    md = render_markdown(v, now=NOW)
    assert "a | b breaks tables" not in md          # the raw pipe is escaped
    assert "a \\| b" in md


def test_business_now_none_is_time_free():
    # with no `now`, time-based signals (stale / aged decision) are not asserted —
    # the report still renders (degraded, never raises).
    v = _view(
        milestones=[_milestone("M0", "planned", occurred_at="2025-01-01T00:00:00Z")],
        milestone_activity=(SourceActivity("eaf", "2025-01-01T00:00:00Z"),),
    )
    md = render_markdown(v)                          # now=None
    assert "## Milestones" in md
    assert "⏳" not in md                            # no time anchor → no stale BANNER fires
    assert "data older than" not in md               # (the rule footnote may mention "stale", the banner does not fire)


def test_text_and_html_render_business_block():
    v = _view(milestones=[_milestone("M0", "started", title="Build UI")])
    assert "Build UI" in render_text(v, now=NOW)
    assert "Build UI" in render_html(v, now=NOW)


def test_decisions_only_renders_no_milestone_table():
    # a decisions-only view (no milestones) shows the pending-decisions section + health, but NO completion
    # bar / milestone table (those are gated on milestones existing).
    md = render_markdown(_view(decisions=[_decision("D1", "open", question="q?")]), now=NOW)
    assert "Needs your decision" in md
    assert "## Milestones" not in md
    assert "Completion:" not in md


def test_milestone_no_title_falls_back_to_id():
    md = render_markdown(_view(milestones=[_milestone("M7", "planned", title=None)]), now=NOW)
    assert "M7" in md                               # id shown when title is absent


def test_html_no_milestone_backward_compatible():
    v = _view(defects=[_defect()], progress=[_progress()])
    assert render_html(v) == render_html(v, now=NOW)
    out = render_html(v, now=NOW)
    assert "<h2>Milestones" not in out
    assert "class=\"health" not in out              # no business block
    assert "Open Defects" in out                    # engineering view top-level (not folded)
    assert "<details>" not in out                   # no business fold in engineering mode


def test_json_carries_milestones_and_decisions():
    v = _view(
        milestones=[_milestone("M0", "completed", actual_date="2026-06-01")],
        decisions=[_decision("D1", "open", question="q")],
    )
    data = json.loads(render_json(v, now=NOW))
    assert data["milestones"][0]["milestone_id"] == "M0"
    assert data["milestones"][0]["status"] == "completed"
    assert data["decisions"][0]["decision_id"] == "D1"
    # derived business view is included for machine consumers
    assert data["business"]["completion"]["completed"] == 1
    assert data["business"]["health"]["color"] in ("red", "yellow", "green")
    assert data["business"]["coverage"]["content_state"] == "in-progress"


# --- audit facb0e5f follow-ups -------------------------------------------------


def test_health_green_text_consistent_with_open_decisions():
    # A1 (claude-f2 + gpt-f1): a FRESH open decision keeps the light green, yet the
    # pending-decisions section lists it — the green one-liner must NOT claim there are no pending decisions.
    v = _view(decisions=[_decision("D1", "open", question="q?",
                                   raised_at="2026-06-07T00:00:00Z")])   # 1 day before NOW
    md = render_markdown(v, now=NOW)
    assert "🟢" in md                                # fresh decision → green light
    assert "Needs your decision" in md              # but it IS listed in the pending-decisions section
    assert "pending decision" not in md.lower()     # green text must not deny pending decisions


def test_html_engineering_mode_has_no_business_css():
    # A4 (claude-f1 + gpt-f4): business CSS is gated → engineering-only HTML head does
    # not grow; a business view does include it.
    eng = render_html(_view(defects=[_defect()]))
    assert ".health{" not in eng                     # no business CSS in engineering mode
    biz = render_html(_view(milestones=[_milestone("M0", "started")]), now=NOW)
    assert ".health{" in biz                          # business CSS present when business


def test_markdown_multi_decision_table_is_contiguous():
    # A2 (grok-f1): a non-final decision with rationale must NOT split the markdown
    # table — data rows stay consecutive, rationale lines come AFTER the table.
    v = _view(decisions=[
        _decision("D1", "open", question="first", rationale="because A"),
        _decision("D2", "open", question="second"),
    ])
    md = render_markdown(v, now=NOW)
    lines = md.splitlines()
    hdr = next(i for i, ln in enumerate(lines) if ln.startswith("| Decision |"))
    assert lines[hdr + 1].startswith("|---")          # separator
    assert lines[hdr + 2].startswith("| D1 |")        # row 1
    assert lines[hdr + 3].startswith("| D2 |")        # row 2 — contiguous, not split
    assert "because A" in md                          # rationale still rendered
    assert md.index("| D2 |") < md.index("because A")  # ...after the rows


def test_markdown_escapes_more_untrusted_fields():
    # A5: every new producer field is escaped, not just title/question.
    v = _view(
        milestones=[_milestone("M0", "started", estimated_size="3|weeks <b>")],
        decisions=[_decision("D1", "open", question="q", options=("a|b",), blocks="M|0")],
    )
    md = render_markdown(v, now=NOW)
    assert "3|weeks" not in md                        # estimate pipe escaped
    assert "<b>" not in md                            # estimate angle-bracket escaped
    assert "a|b" not in md                            # option pipe escaped
    assert "M|0" not in md                            # blocks pipe escaped


def test_markdown_flattens_newline_in_milestone_title():
    # A5: a newline in a cell can't break the row — `_md` flattens \n to a space.
    v = _view(milestones=[_milestone("M0", "started", title="line1\nline2")])
    md = render_markdown(v, now=NOW)
    row = next(ln for ln in md.splitlines() if "line1" in ln)
    assert "line2" in row                             # both halves on the SAME line
    assert row.startswith("|") and row.endswith("|")  # still one valid cell row


# --- KPI at-a-glance scorecard (Owner 2026-06-12) ------------------------------


def test_kpi_scorecard_in_markdown():
    v = _view(
        milestones=(_milestone("M0", "completed"), _milestone("M1", "started")),
        decisions=(_decision("D1", "open"),),
        defects=(_defect("D-1", status="open"),),
    )
    md = render_markdown(v, now=NOW)
    assert "**At a glance**" in md
    assert "1/2 milestones done" in md     # 1 completed of 2 effective
    assert "1 open decision" in md
    assert "1 open defect" in md


def test_kpi_scorecard_in_text_and_html():
    v = _view(
        milestones=(_milestone("M0", "completed"),),
        decisions=(_decision("D1", "open"), _decision("D2", "open")),
    )
    txt = render_text(v, now=NOW)
    assert "At a glance:" in txt
    assert "2 open decisions" in txt
    html = render_html(v, now=NOW)
    assert 'class="kpi"' in html
    assert "2 open decisions" in html


def test_kpi_omits_milestones_chip_when_none():
    # decisions-only business view → KPI carries the decision count, no milestones chip.
    v = _view(decisions=(_decision("D1", "open"),))
    md = render_markdown(v, now=NOW)
    assert "**At a glance**" in md
    assert "1 open decision" in md
    assert "milestones done" not in md


def test_kpi_absent_in_engineering_only_view():
    # No milestones/decisions → no business report → no scorecard (gated, backward-compat).
    v = _view(defects=(_defect(),))
    md = render_markdown(v)
    assert "At a glance" not in md


def test_kpi_no_milestone_chip_when_all_cancelled():
    # audit 04e94af6 f1: all milestones cancelled → effective==0 → NO `0/total milestones
    # done` chip (mirrors the suppressed completion bar). Other chips still render.
    v = _view(milestones=(_milestone("M0", "cancelled"),), decisions=(_decision("D1", "open"),))
    md = render_markdown(v, now=NOW)
    assert "At a glance" in md
    assert "1 open decision" in md
    assert "milestones done" not in md


def test_kpi_no_decision_chip_when_none_tracked():
    # audit 04e94af6 f3: a milestones-only business view tracks no decisions → no spurious
    # `0 open decisions` chip (symmetric with the milestone / defect presence guards).
    v = _view(milestones=(_milestone("M0", "completed"),))
    md = render_markdown(v, now=NOW)
    assert "At a glance" in md
    assert "1/1 milestones done" in md
    assert "open decision" not in md
