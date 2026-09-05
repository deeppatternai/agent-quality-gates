"""Behavior tests for the §4.2 injection-proof translation pipeline.

The security contract: an UNTRUSTED translation (the LLM is treated as hostile)
may localize producer free-text but must NEVER fabricate or tamper with a FACT
(number / % / date / status keyword). Any such attempt → reject → English
canonical. md/html metachars in a translated value are sanitized by render.
Pure stdlib; the translator is an injected callable (the calling session, here a
fake).
"""
from __future__ import annotations

import json

from ledger.projection import (
    DecisionEntry,
    MilestoneEntry,
    ProjectView,
    SourceStat,
)
from ledger.render import render
from ledger.translate import (
    TranslatedReport,
    apply_segments,
    collect_segments,
    translate_report,
)

NOW = "2026-06-08T00:00:00Z"


def _ms(milestone_id="M0", status="completed", **over):
    base = dict(
        milestone_id=milestone_id, status=status, title="Ship the API", summary=None,
        key_outcome=None, estimated_size=None, target_date=None, planned_start=None,
        actual_date="2026-06-01", actual_wallclock=None, gate=None, phase_label=None,
        first_seq=1, last_seq=1, occurred_at="2026-05-29T10:00:00Z", sources=("eaf",),
        replanned=False, ignored_events=0,
    )
    base.update(over)
    return MilestoneEntry(**base)


def _dec(decision_id="D1", status="open", **over):
    base = dict(
        decision_id=decision_id, status=status, question="Postgres or SQLite?", options=(),
        rationale=None, blocks=None, first_seq=1, last_seq=1,
        raised_at="2026-06-07T00:00:00Z", occurred_at="2026-06-07T00:00:00Z",
        sources=("manual",), ignored_events=0,
    )
    base.update(over)
    return DecisionEntry(**base)


def _view(milestones=(), decisions=()):
    return ProjectView(
        project_id="o/r", event_count=1, progress=(), defects=(), handoffs=(),
        sources=(SourceStat("eaf", "high", 1),), last_activity="2026-06-01T10:00:00Z",
        has_low_fidelity=False, warnings=(), milestones=tuple(milestones),
        decisions=tuple(decisions),
    )


def _wrap(prefix):
    """A fake translator that prepends a marker — a faithful localization that adds
    no facts (passes the prose scan)."""
    return lambda segs, lang: [f"{prefix}{s}" for s in segs]


# --- segment collection (the "dict" is producer free-text ONLY) ---------------


def test_collect_segments_only_producer_free_text():
    v = _view(
        milestones=[_ms(title="Ship the API", summary="do it", key_outcome="users call it",
                        estimated_size="about two weeks")],
        decisions=[_dec(question="pg or sqlite?", rationale="scale", options=("pg", "sqlite"))],
    )
    segs = collect_segments(v)
    for s in ("Ship the API", "do it", "users call it", "pg or sqlite?", "scale", "pg", "sqlite"):
        assert s in segs
    # locked facts/ids/dates AND the number-bearing estimate fields are NOT collected
    # (the LLM never sees them) — audit c6050ee7: keep number-heavy fields out of the dict.
    for locked in ("M0", "D1", "completed", "2026-06-01", "open", "about two weeks"):
        assert locked not in segs


def test_apply_segments_is_immutable():
    v = _view(milestones=[_ms(title="Ship the API")])
    v2 = apply_segments(v, {"Ship the API": "✦Ship the API"})
    assert v.milestones[0].title == "Ship the API"        # original untouched
    assert v2.milestones[0].title == "✦Ship the API"


# --- happy path: producer text localized, facts locked ------------------------


def test_translate_happy_path_translates_text_keeps_facts():
    v = _view(milestones=[_ms(status="completed", title="Ship the API", actual_date="2026-06-01")])
    r = translate_report(v, "markdown", target_lang="zh", translator=_wrap("✦"), now=NOW)
    assert isinstance(r, TranslatedReport)
    assert r.translated is True and r.fell_back is False
    assert "✦Ship the API" in r.report          # producer free-text localized
    assert "Done" in r.report                    # status label LOCKED (English chrome)
    assert "2026-06-01" in r.report              # date LOCKED


def test_translate_locked_facts_byte_identical():
    v = _view(milestones=[_ms(status="completed", title="Ship the API",
                              actual_date="2026-06-01", actual_wallclock="14 minutes")])
    en = json.loads(render(v, "json", now=NOW))
    tr = json.loads(translate_report(v, "json", target_lang="zh",
                                     translator=_wrap("✦"), now=NOW).report)
    m_en, m_tr = en["milestones"][0], tr["milestones"][0]
    assert m_tr["status"] == m_en["status"]               # locked
    assert m_tr["actual_date"] == m_en["actual_date"]     # locked
    assert m_tr["title"] != m_en["title"]                 # translated
    assert tr["business"] == en["business"]               # derived facts identical


# --- the security core: a hostile translation cannot fabricate facts ----------


def test_translate_rejects_injected_percent():
    v = _view(milestones=[_ms(title="Ship the API")])
    bad = lambda segs, lang: [f"{s} — 100% done" for s in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True and r.translated is False
    assert r.violations
    assert "100% done" not in r.report           # delivered English canonical, no injected fact


def test_translate_rejects_changed_number():
    v = _view(milestones=[_ms(title="Ship 3 services")])
    bad = lambda segs, lang: [s.replace("3", "9") for s in segs]   # 3 → 9 tamper
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True
    assert "9 services" not in r.report
    assert "Ship 3 services" in r.report         # original number preserved


def test_translate_rejects_fullwidth_digit_bypass():
    # a fullwidth "１００％" must not slip past the ASCII regex — NFKC normalizes it.
    v = _view(milestones=[_ms(title="Ship the API")])
    bad = lambda segs, lang: [f"{s}（１００％）" for s in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True


def test_translate_rejects_injected_status_keyword():
    v = _view(milestones=[_ms(status="started", title="Build UI")])
    bad = lambda segs, lang: [f"{s} (completed)" for s in segs]    # no digits, but a status word
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True
    assert "(completed)" not in r.report         # the injected status claim never reaches output
    assert "Build UI" in r.report                # English canonical delivered


def test_translate_rejects_injected_date():
    v = _view(milestones=[_ms(title="Ship the API")])
    bad = lambda segs, lang: [f"{s} due 2099-12-31" for s in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True
    assert "2099-12-31" not in r.report


def test_translate_preserves_legit_numbers_in_producer_text():
    # a number that WAS in the original prose may stay (a faithful localization);
    # the milestone TITLE is the displayed translatable cell.
    v = _view(milestones=[_ms(title="Ship 3 services v2")])
    keep = lambda segs, lang: [f"✦{s}" for s in segs]   # wraps, keeps the digits
    r = translate_report(v, "markdown", target_lang="zh", translator=keep, now=NOW)
    assert r.translated is True and r.fell_back is False
    assert "✦Ship 3 services v2" in r.report             # digits 3, 2 preserved → multiset equal


# --- md/html sanitization is INHERITED from render ----------------------------


def test_translate_html_escapes_translated_markup():
    # a digit-free markup injection passes the fact scan but render escapes it.
    v = _view(milestones=[_ms(title="Ship the API")])
    inj = lambda segs, lang: ["<b>x</b>" for _ in segs]
    r = translate_report(v, "html", target_lang="zh", translator=inj, now=NOW)
    assert r.translated is True                  # no fact tokens → scan passes
    assert "<b>x</b>" not in r.report            # ...but render html-escapes it
    assert "&lt;b&gt;" in r.report


def test_translate_markdown_escapes_translated_pipe():
    v = _view(decisions=[_dec(question="a or b")])
    inj = lambda segs, lang: ["x | y breaks table" for _ in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=inj, now=NOW)
    assert "x | y breaks table" not in r.report
    assert "x \\| y" in r.report                 # md pipe escaped


# --- fail-closed: a misbehaving translator never poisons / crashes ------------


def test_translate_shape_mismatch_falls_back():
    v = _view(milestones=[_ms(title="Ship the API")])
    bad = lambda segs, lang: list(segs) + ["extra"]          # wrong count
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True


def test_translate_non_string_falls_back():
    v = _view(milestones=[_ms(title="Ship the API")])
    bad = lambda segs, lang: [None for _ in segs]            # non-str
    r = translate_report(v, "markdown", target_lang="zh", translator=bad, now=NOW)
    assert r.fell_back is True


def test_translate_translator_raises_falls_back():
    v = _view(milestones=[_ms(title="Ship the API")])

    def boom(segs, lang):
        raise RuntimeError("llm unavailable")

    r = translate_report(v, "markdown", target_lang="zh", translator=boom, now=NOW)
    assert r.fell_back is True
    assert "Ship the API" in r.report            # English canonical still delivered


# --- no-op paths --------------------------------------------------------------


def test_translate_no_segments_is_canonical():
    v = _view()   # no milestones / decisions → nothing to translate
    r = translate_report(v, "markdown", target_lang="zh", translator=_wrap("✦"), now=NOW)
    assert r.translated is False and r.fell_back is False
    assert r.report == render(v, "markdown", now=NOW)


def test_translate_same_language_is_noop():
    v = _view(milestones=[_ms(title="Ship the API")])
    r = translate_report(v, "markdown", target_lang="en", translator=_wrap("✦"), now=NOW)
    assert r.translated is False and r.fell_back is False
    assert "✦" not in r.report
    assert r.report == render(v, "markdown", now=NOW)


# --- audit c6050ee7 hardening: adversarial bypass regressions -----------------


def test_translate_rejects_sign_inversion():
    # gemini-f3 / qwen-f2: '-100' → '100' polarity flip must be caught.
    v = _view(milestones=[_ms(title="Net change -100")])
    flip = lambda segs, lang: [s.replace("-100", "100") for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=flip, now=NOW).fell_back is True


def test_translate_rejects_number_removal():
    # gemini-f1 / grok-f3: dropping an original number (not just adding) must be caught.
    v = _view(milestones=[_ms(title="adds 3 endpoints")])
    drop = lambda segs, lang: [s.replace("3 ", "") for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=drop, now=NOW).fell_back is True


def test_translate_thousands_separator_not_false_positive():
    # gemini-f5: a faithful locale fold '1,000' ↔ '1000' must NOT spuriously reject.
    v = _view(milestones=[_ms(title="serves 1,000 users")])
    fold = lambda segs, lang: [s.replace("1,000", "1000") for s in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=fold, now=NOW)
    assert r.translated is True and r.fell_back is False
    # but tampering a thousands-number IS caught
    tamper = lambda segs, lang: [s.replace("1,000", "9,000") for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=tamper, now=NOW).fell_back is True


def test_translate_rejects_zero_width_split_keyword():
    # claude-f1 / deepseek-f1: a ZWSP-split 'comple​ted' renders as "completed"
    # but must be stripped + caught.
    v = _view(milestones=[_ms(status="started", title="Build UI")])
    zw = lambda segs, lang: [f"{s} comple​ted" for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=zw, now=NOW).fell_back is True


def test_translate_rejects_zero_width_split_digit():
    v = _view(milestones=[_ms(title="Build UI")])
    zw = lambda segs, lang: [f"{s} 1​00%" for s in segs]   # ZWSP-split "100%"
    assert translate_report(v, "markdown", target_lang="zh", translator=zw, now=NOW).fell_back is True


def test_translate_keyword_word_boundary_handles_negation():
    # qwen-f1: "unblocked" in the source is NOT the keyword "blocked" (word boundary),
    # so a faithful wrap passes; a hostile STANDALONE "blocked" is caught.
    v = _view(milestones=[_ms(status="started", title="unblocked the queue")])
    ok = lambda segs, lang: [f"✦{s}" for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=ok, now=NOW).translated is True
    inj = lambda segs, lang: [f"{s} now blocked" for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=inj, now=NOW).fell_back is True


def test_translate_rejects_non_list_container():
    # claude-f4 / gpt-f3: a bare str (→ chars) or dict (→ keys) must fail closed, not
    # slip past list() coercion as a "success".
    v = _view(milestones=[_ms(title="Ship the API")])
    as_str = lambda segs, lang: "".join(segs)
    assert translate_report(v, "markdown", target_lang="zh", translator=as_str, now=NOW).fell_back is True
    as_dict = lambda segs, lang: {s: f"✦{s}" for s in segs}
    assert translate_report(v, "markdown", target_lang="zh", translator=as_dict, now=NOW).fell_back is True


def test_translate_markdown_leading_control_char_is_inert():
    # claude-f3: a translated title attempting a markdown heading/quote is placed in a
    # TABLE cell (mid-line) and newline-flattened by _md → it cannot restructure the doc.
    v = _view(milestones=[_ms(title="Build UI")])
    inj = lambda segs, lang: ["# Heading\n> quote" for _ in segs]
    r = translate_report(v, "markdown", target_lang="zh", translator=inj, now=NOW)
    assert r.translated is True                          # no fact tokens → scan passes
    row = next(ln for ln in r.report.splitlines() if "Heading" in ln)
    assert row.startswith("|") and "quote" in row        # one table row — no new block


# --- audit 02944902 round-2: second-order bypass regressions ------------------


def test_translate_rejects_thousands_regroup_merge():
    # gpt-f1 / gemini-f2 / grok-f1 / qwen-f3: the restricted thousands strip must NOT
    # merge "1,2,3" → "123"; regrouping comma-separated numbers is caught.
    v = _view(milestones=[_ms(title="phases 1,2,3 ship")])
    merge = lambda segs, lang: [s.replace("1,2,3", "123") for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=merge, now=NOW).fell_back is True


def test_translate_rejects_cjk_adjacent_keyword():
    # claude-f3 / gpt-f3 / gemini-f3: an English keyword glued to CJK text (no space)
    # must still be caught — \b fails here, the ASCII-letter lookaround does not.
    v = _view(milestones=[_ms(status="started", title="Build UI")])
    inj = lambda segs, lang: [f"{s}任务completed完成" for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=inj, now=NOW).fell_back is True


def test_translate_rejects_superscript_spoof():
    # gemini-f1: NFKC folds '10⁵' → '105'; stripping the "No" superscript first makes
    # the spoof change the multiset → caught.
    v = _view(milestones=[_ms(title="uses 105 items")])
    spoof = lambda segs, lang: [s.replace("105", "10⁵") for s in segs]
    assert translate_report(v, "markdown", target_lang="zh", translator=spoof, now=NOW).fell_back is True


def test_translate_rejects_combining_grapheme_joiner_split():
    # claude-f2 / gpt-f2: U+034F (CGJ, NOT category Cf) must be stripped so it cannot
    # split an injected keyword while rendering nothing.
    v = _view(milestones=[_ms(status="started", title="Build UI")])
    cgj = lambda segs, lang: [f"{s} comple͏ted" for s in segs]   # U+034F inside "completed"
    assert translate_report(v, "markdown", target_lang="zh", translator=cgj, now=NOW).fell_back is True


def test_translate_report_is_pure_stdlib():
    import inspect

    import ledger.translate as t

    src = inspect.getsource(t)
    for banned in ("import requests", "import openai", "import anthropic", "import httpx"):
        assert banned not in src


# --- KPI scorecard × §4.2 (audit 04e94af6: the at-a-glance line is renderer chrome) ---


def test_collect_segments_excludes_kpi_chrome():
    # The KPI counts/labels are renderer chrome (computed from the view), never producer
    # free-text — they must NOT enter the translatable segment dict (the LLM never sees
    # chrome). collect_segments reads view fields, so the rendered KPI strings can't appear.
    v = _view(milestones=[_ms("M0", status="completed")], decisions=[_dec("D1", status="open")])
    segs = collect_segments(v)
    for chrome in ("At a glance", "milestones done", "open decision"):
        assert chrome not in segs


def test_translate_keeps_kpi_scorecard_as_locked_chrome():
    # The at-a-glance scorecard survives translation intact: English labels stay (like the
    # "Done" status chrome) and its counts are locked facts — a faithful translator localizes
    # only producer prose. 1 effective milestone + 1 open decision → "1/1 · 1 open decision".
    v = _view(
        milestones=[_ms("M0", status="completed", title="Ship the API")],
        decisions=[_dec("D1", status="open", question="pg or sqlite?")],
    )
    r = translate_report(v, "markdown", target_lang="zh", translator=_wrap("✦"), now=NOW)
    assert r.fell_back is False
    assert "✦pg or sqlite?" in r.report          # producer free-text localized
    assert "At a glance" in r.report             # KPI chrome stays English
    assert "1/1 milestones done" in r.report     # counts locked
    assert "1 open decision" in r.report
