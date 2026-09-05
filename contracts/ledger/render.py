"""Render a ProjectView into a human-facing report (DesignSpec §5.6 + §5.8).

Formats: html (default — the §5.8 progress-report default), markdown (the docx /
translation intermediate), text (terminal), json (the structured view). All
output is CANONICAL ENGLISH (§5.7): translation to a requested language is a
presentation-layer step done by the rendering LLM in the aqg-project-status
skill, NOT here — this module stays deterministic and English-only.

Security: every dynamic string (progress/defect titles, details, commit text,
paths) is untrusted — aqg-hook captures free text from git/commit messages. The
HTML renderer html-escapes ALL of it and only emits a clickable <a> for http(s)
URLs (never javascript:/data: hrefs). The markdown renderer escapes table-cell
pipes/newlines so a stray '|' can't break the table. Pure stdlib.
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import asdict

from ledger.projection import (
    ProjectView,
    completion,
    coverage_state,
    health_light,
)
from ledger.repo_reality import RepoReality, reconciliation_lines

FORMATS = ("html", "markdown", "text", "json")

_STATUS_LABEL = {
    "open": "OPEN",
    "fixing": "IN PROGRESS",
    "fixed": "FIXED",
    "closed": "CLOSED",
    "wontfix": "WON'T FIX",
}
_FIDELITY_NOTE = {
    "high": "high fidelity",
    "low": "low fidelity / inferred",
    "manual": "human-entered",
    "unknown": "unknown fidelity",
}

# §5.6 C5: stop low-fidelity hook data making the ledger look more complete than
# it is. Shown only when has_low_fidelity.
_CAVEAT = (
    "Coverage caveat: this ledger mixes sources of differing fidelity. EAF records "
    "are high-fidelity (emitted by the engineering agent itself); AQG-hook records "
    "are low-fidelity, inferred from git / PR / session signals and may be "
    "incomplete or approximate; manual records are human-entered. The absence of an "
    "event does not mean the work did not happen."
)

_MAX_WARNINGS = 20   # cap the data-notes list; report the remainder as a count


def render(view: ProjectView, fmt: str = "html",
           repo_reality: "RepoReality | None" = None, *, now=None) -> str:
    """Render `view` in `fmt` (one of FORMATS; default html). Raises ValueError on
    an unknown format so the CLI fails fast rather than emitting nothing.

    `repo_reality` is an OPTIONAL, SEPARATE presentation parameter (issue #245):
    when given, an in-band reconciliation banner (counts + our notes only, never
    VCS free-text) is rendered above the defect section. It is NEVER a field of
    `view` and never feeds `_defect_groups(view)` — the typed-separation invariant
    (ADR §4.2): the deterministic projection is unperturbed by repo-reality.

    `now` (date / datetime / ISO string) is the report-generation time the
    business report's time-based signals (health aged-decision / staleness) read
    via the P2 derivations. It is injected (not read from the wall clock) so the
    renderer stays deterministic; when None the business block degrades to the
    time-free signals only (never raises). The business block (a4 §6.1) renders
    only when the view carries milestones or decisions — a pure engineering ledger
    is byte-for-byte unchanged."""
    if fmt == "html":
        return render_html(view, repo_reality, now=now)
    if fmt == "markdown":
        return render_markdown(view, repo_reality, now=now)
    if fmt == "text":
        return render_text(view, repo_reality, now=now)
    if fmt == "json":
        return render_json(view, repo_reality, now=now)
    raise ValueError(f"unknown format {fmt!r}; expected one of {FORMATS}")


# --- helpers ------------------------------------------------------------------


def _status_label(status: str) -> str:
    return _STATUS_LABEL.get(status, status.upper())


def _defect_groups(view: ProjectView):
    """(open_active, resolved) — active = open/fixing; resolved = fixed/closed/wontfix.
    Both keep the projection's open-work-first ordering."""
    active = [d for d in view.defects if d.is_open]
    resolved = [d for d in view.defects if not d.is_open]
    return active, resolved


def _is_http_url(value) -> bool:
    return isinstance(value, str) and (value.startswith("http://") or value.startswith("https://"))


def _defect_extras(d):
    """(label, value) pairs for a defect's verification / regression anchor. Each
    renderer escapes `value` for its own format (audit 33f6e5df gpt-f4)."""
    pairs = []
    if d.verification:
        pairs.append(("verified", d.verification))
    if d.regression_anchor:
        pairs.append(("regression", d.regression_anchor))
    return pairs


def _h(value) -> str:
    """html-escape (incl. quotes) — never trust dynamic text in HTML output."""
    return html.escape("" if value is None else str(value), quote=True)


# Structural punctuation that, unescaped, could inject a markdown link/image,
# emphasis, code span, raw HTML, table cell, or strikethrough. CommonMark renders
# a backslash-escaped ASCII punctuation char as the literal char, so escaping is
# safe and round-trips through the docx pipeline.
_MD_ESCAPE = re.compile(r"([\\`*_\[\]()<>&|~])")


def _md(value) -> str:
    """Escape an untrusted string for safe markdown inline display, and flatten
    newlines (a newline would break a list item / table row). Applied to ALL
    dynamic strings in markdown output (audit 33f6e5df gpt-f1)."""
    text = "" if value is None else str(value)
    text = text.replace("\r", " ").replace("\n", " ")
    return _MD_ESCAPE.sub(r"\\\1", text)


# A URL safe to embed as a markdown link destination `[..](url)`: http(s) and
# free of anything that could close the destination or inject (parens, brackets,
# quotes, backtick, pipe, backslash, whitespace, control chars). Anything else is
# rendered as escaped plain text instead of a clickable link (audit 33f6e5df gpt-f1).
_URL_UNSAFE = set("()<>[]{}\"'`|\\ ")


def _is_clean_url(value) -> bool:
    if not _is_http_url(value):
        return False
    return not any((c in _URL_UNSAFE) or (ord(c) < 0x20) for c in value)


# --- business progress report (a4 §6.1 — progressive disclosure) --------------
#
# Rendered ONLY when the view carries milestones or decisions; a pure engineering
# ledger renders exactly as before (backward compat). Default view = health light
# one-liner + completion bar + milestone stage table + pending-decisions; engineering detail
# and the data/method footnote fold into "More details". Every producer-supplied
# string (milestone/decision free text) is untrusted and escaped per format, the
# same stance as progress/defect text.

_HEALTH_EMOJI = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
_HEALTH_LABEL = {"red": "At risk", "yellow": "Needs attention", "green": "On track"}
_MILESTONE_STATUS_LABEL = {
    "planned": "Planned", "started": "In progress", "completed": "Done",
    "cancelled": "Cancelled",
}
_COMPLETION_BAR_WIDTH = 18


def _has_business(view: ProjectView) -> bool:
    return bool(view.milestones or view.decisions)


def _s(n: int) -> str:
    return "" if n == 1 else "s"


def _health_summary(light, esc=lambda x: x) -> str:
    """One-line, count-based health reason (a4 §6.1: NO rule-matrix dump in the
    default view — the rule + threshold live in the folded data/method footnote).
    Untrusted detail tokens (ids) are escaped via `esc`; the template text is
    literal so its punctuation is never mangled."""
    if light.color == "green":
        # NOT "no pending decisions" — a FRESH (non-aged) open decision keeps the
        # light green yet is still listed under pending-decisions; "overdue items" is the true
        # claim (only aged decisions / stale data are health signals) — audit
        # facb0e5f claude-f2 + gpt-f1.
        return "no critical issues or overdue items."
    grouped: "dict[str, list[str]]" = {}
    for sig in light.signals:
        grouped.setdefault(sig.code, []).append(esc(sig.detail))
    phrases: list[str] = []
    for code in ("critical_defect", "aged_decision", "high_defect", "blocked", "stale"):
        details = grouped.get(code)
        if not details:
            continue
        n = len(details)
        joined = ", ".join(details)
        if code == "critical_defect":
            phrases.append(f"{n} open critical defect{_s(n)} ({joined})")
        elif code == "high_defect":
            phrases.append(f"{n} open high-severity defect{_s(n)} ({joined})")
        elif code == "aged_decision":
            phrases.append(f"{n} decision{_s(n)} pending too long ({joined})")
        elif code == "blocked":
            phrases.append(f"work blocked ({joined})")
        elif code == "stale":
            phrases.append(f"{n} source{_s(n)} with stale data ({joined})")
    return "; ".join(phrases) + "."


def _kpi_chips(view: ProjectView) -> list:
    """At-a-glance scorecard chips (Owner 2026-06-12) — the counts a reader wants up
    front, so the business overview leads with them instead of burying them in the
    sections below. Each value is a FACT computed from the view (the §4.2 translation
    fact-guard protects the digits); there is NO producer free-text here, so the chips
    are never translation segments. Returns pre-formatted English strings (the same
    label convention as the section headers)."""
    chips: list = []
    if view.milestones:
        comp = completion(view)
        # Only when there are EFFECTIVE (non-cancelled) milestones — mirror the completion
        # bar's all-cancelled suppression so the scorecard never asserts `0/total done`
        # against cancelled work (audit 04e94af6 convergent f1).
        if comp.effective:
            chips.append(f"{comp.completed}/{comp.effective} milestones done")
    # Guard on presence like the milestone / defect chips — a business overview can render
    # with zero decisions (milestones-only); don't imply decision tracking that isn't there
    # with a spurious `0 open decisions` (audit 04e94af6 claude f3).
    if view.decisions:
        n_dec = len(view.open_decisions)
        chips.append(f"{n_dec} open decision{_s(n_dec)}")
    if view.defects:
        n_open = sum(1 for d in view.defects if d.is_open)
        chips.append(f"{n_open} open defect{_s(n_open)}")
    return chips


def _completion_cells(comp) -> "tuple[str, str]":
    """(bar, label) for the completion line. effective==0 → ('', 'N/A …') — the bar
    is empty and the label is N/A (no division, a4 §5.4). percent is the P2 half-up
    value; the bar fills proportionally."""
    if comp.percent is None:
        return ("", "N/A (no effective milestones)")
    filled = round(comp.percent / 100 * _COMPLETION_BAR_WIDTH)
    bar = "▓" * filled + "░" * (_COMPLETION_BAR_WIDTH - filled)
    return (bar, f"{comp.percent}% ({comp.completed} of {comp.effective})")


def _milestone_target(m) -> str:
    """The 'Estimate / target' cell (a4 §6 [a4:T]): the work-size estimate, or the
    optional target_date (flagged human-baseline), or '—'. NEVER an ahead/behind
    delta — the milestone shows facts, not a schedule judgement."""
    if m.estimated_size:
        return m.estimated_size
    if m.target_date:
        return f"{m.target_date} (target, human-baseline)"
    return "—"


def _milestone_completed_cell(m) -> str:
    """The 'Completed' cell: actual date (+ optional wall-clock) for a done
    milestone; '—' otherwise. No comparison to target (a4 §6 [a4:T])."""
    if m.status != "completed" or not m.actual_date:
        return "—"
    if m.actual_wallclock:
        return f"{m.actual_date} ({m.actual_wallclock})"
    return m.actual_date


def _health_rule_text(stale_days: int) -> str:
    """The folded data/method footnote rule text (a4 §5.5 / §6.1): the light's
    rule + threshold N. Static, no untrusted input."""
    return (
        f"Health light rule (threshold N = {stale_days} days) — 🔴 open critical "
        f"defect or a decision pending >{stale_days} days; 🟡 open high-severity "
        f"defect, blocked work, or data >{stale_days} days stale; 🟢 none of these. "
        f"A stalled milestone surfaces as stale data, not as an overdue date."
    )


# --- text ---------------------------------------------------------------------


def render_text(view: ProjectView, repo_reality: "RepoReality | None" = None,
                *, now=None) -> str:
    out: list[str] = []
    out.append(f"Project Ledger — {view.project_id}")
    out.append(f"{view.event_count} events | last activity: {view.last_activity or 'n/a'}")
    if _has_business(view):
        out += _business_text(view, now)
        out.append("")
        out.append("== More details ==")
        out += _data_method_text(view, now)
    out += _eng_text(view, repo_reality)
    return "\n".join(out) + "\n"


def _business_text(view: ProjectView, now) -> list:
    """Default business overview (a4 §6.1): health one-liner + completion bar +
    milestone stage table + pending-decisions. Plain text has no MARKUP-injection surface, so
    no html/md escaping is applied — producer free-text is emitted raw, the SAME
    stance as the existing progress/defect text rendering (a terminal control-byte
    sanitizer, if ever wanted, is a separate cross-cutting change across all text
    output, not P3 — audit facb0e5f claude-f4 + gpt-f3)."""
    out: list[str] = []
    light = health_light(view, now=now)
    cov = coverage_state(view, now=now)
    out.append("")
    out.append("== Status ==")
    out.append(f"  {_HEALTH_EMOJI[light.color]} {_HEALTH_LABEL[light.color]} — "
               f"{_health_summary(light)}")
    chips = _kpi_chips(view)
    if chips:
        out.append(f"  At a glance: {' · '.join(chips)}")
    if cov.is_stale:
        out.append(f"  [stale] data >{cov.stale_days}d old from: {', '.join(cov.stale_sources)}")
    if view.milestones and cov.content_state == "all-cancelled":
        # §6.5: all-cancelled falls back to the activity overview + a note — NOT the
        # milestone table or an N/A completion bar (audit facb0e5f gpt-f2).
        out.append("")
        out.append(f"  All {len(view.milestones)} milestone(s) cancelled — no active plan.")
    elif view.milestones:
        bar, label = _completion_cells(completion(view))
        out.append("")
        out.append(f"  Completion: {bar} {label}".rstrip())
        out.append("")
        out.append(f"== Milestones ({len(view.milestones)}) ==")
        for m in view.milestones:
            out.append(f"  [{_MILESTONE_STATUS_LABEL.get(m.status, m.status)}] "
                       f"{m.milestone_id}: {m.title or '(no title)'}")
            out.append(f"      estimate/target: {_milestone_target(m)} | "
                       f"completed: {_milestone_completed_cell(m)}")
    opens = view.open_decisions
    if opens:
        out.append("")
        out.append(f"== Needs your decision ({len(opens)}) ==")
        for d in opens:
            out.append(f"  {d.decision_id}: {d.question or '(no question)'}")
            if d.options:
                out.append(f"      options: {', '.join(d.options)}")
            if d.rationale:
                out.append(f"      rationale: {d.rationale}")
            if d.blocks:
                out.append(f"      blocks: {d.blocks}")
    return out


def _data_method_text(view: ProjectView, now) -> list:
    out: list[str] = [""]
    out.append(f"  {_health_rule_text(coverage_state(view, now=now).stale_days)}")
    if view.milestone_activity:
        out.append("  Per-source freshness (latest milestone):")
        for sa in view.milestone_activity:
            out.append(f"    {sa.source}: {sa.last_occurred_at}")
    return out


def _eng_text(view: ProjectView, repo_reality: "RepoReality | None") -> list:
    """The engineering ledger sections — top-level in engineering mode, folded
    under 'More details' in business mode."""
    out: list[str] = []
    if repo_reality is not None:
        out.append("")
        out.append("== Repo Reality (reconciliation) ==")
        for line in reconciliation_lines(repo_reality, view.event_count):
            out.append(f"  {line}")
    if view.has_low_fidelity:
        out.append("")
        out.append(f"[!] {_CAVEAT}")

    active, resolved = _defect_groups(view)
    out.append("")
    out.append(f"== Open Defects ({len(active)}) ==")
    if active:
        for d in active:
            out.append(f"  [{(d.severity or '?').upper()}] {d.defect_id}: {d.title or '(no title)'}"
                       f" — {_status_label(d.status)} ({', '.join(d.sources)})")
    else:
        out.append("  No open defects.")

    out.append("")
    out.append(f"== Progress Timeline ({len(view.progress)}) ==")
    for p in view.progress:
        line = f"  #{p.ledger_seq} {p.phase_event}: {p.title} ({p.source})"
        if p.pr_url:
            line += f" [{p.pr_url}]"
        if p.commit_sha:
            line += f" @{p.commit_sha}"
        out.append(line)
        if p.detail:
            out.append(f"      {p.detail}")
    if not view.progress:
        out.append("  (no progress events)")

    if resolved:
        out.append("")
        out.append(f"== Resolved & Closed Defects ({len(resolved)}) ==")
        for d in resolved:
            out.append(f"  {d.defect_id}: {d.title or '(no title)'} — {_status_label(d.status)}"
                       + (f" (fix: {d.fixed_commit})" if d.fixed_commit else ""))
            extras = _defect_extras(d)
            if extras:
                out.append(f"      {' | '.join(f'{k}: {v}' for k, v in extras)}")

    if view.handoffs:
        out.append("")
        out.append(f"== Delivery Handbooks ({len(view.handoffs)}) ==")
        for hd in view.handoffs:
            out.append(f"  {hd.manual_path} ({hd.generated_by})")

    out.append("")
    out.append("== Sources ==")
    for s in view.sources:
        out.append(f"  {s.source}: {s.event_count} events ({_FIDELITY_NOTE.get(s.fidelity, s.fidelity)})")

    out += _text_warnings(view)
    return out


def _text_warnings(view: ProjectView) -> list:
    if not view.warnings:
        return []
    out = ["", f"== Data notes ({len(view.warnings)}) =="]
    for w in view.warnings[:_MAX_WARNINGS]:
        out.append(f"  - {w}")
    extra = len(view.warnings) - _MAX_WARNINGS
    if extra > 0:
        out.append(f"  ...and {extra} more")
    return out


# --- markdown -----------------------------------------------------------------


def render_markdown(view: ProjectView, repo_reality: "RepoReality | None" = None,
                    *, now=None) -> str:
    out: list[str] = []
    out.append(f"# Project Ledger — {_md(view.project_id)}")
    out.append("")
    out.append(f"_{view.event_count} events · last activity: {_md(view.last_activity or 'n/a')}_")
    if _has_business(view):
        out += _business_md(view, now)
        out.append("")
        out.append("## More details")
        out += _data_method_md(view, now)
    out += _eng_md(view, repo_reality)
    return "\n".join(out) + "\n"


def _business_md(view: ProjectView, now) -> list:
    """Default business overview in markdown (a4 §6.1). All producer free text is
    escaped via `_md` (untrusted — same stance as progress/defect text)."""
    out: list[str] = []
    light = health_light(view, now=now)
    cov = coverage_state(view, now=now)
    out.append("")
    out.append("## Status")
    out.append("")
    out.append(f"{_HEALTH_EMOJI[light.color]} **{_HEALTH_LABEL[light.color]}** — "
               f"{_health_summary(light, _md)}")
    chips = _kpi_chips(view)
    if chips:
        out.append("")
        out.append("**At a glance** · " + " · ".join(chips))
    if cov.is_stale:
        out.append("")
        out.append(f"> ⏳ **Stale:** data older than {cov.stale_days} days from "
                   f"{_md(', '.join(cov.stale_sources))}.")
    if view.milestones and cov.content_state == "all-cancelled":
        out.append("")
        out.append(f"_All {len(view.milestones)} milestone(s) cancelled — no active plan._")
    elif view.milestones:
        bar, label = _completion_cells(completion(view))
        out.append("")
        out.append(f"**Completion:** {bar} {label}".rstrip())
        out.append("")
        out.append(f"## Milestones ({len(view.milestones)})")
        out.append("")
        out.append("| Milestone | Status | Estimate / target | Completed |")
        out.append("|---|---|---|---|")
        for m in view.milestones:
            out.append(
                f"| {_md(m.title or m.milestone_id)} "
                f"| {_md(_MILESTONE_STATUS_LABEL.get(m.status, m.status))} "
                f"| {_md(_milestone_target(m))} | {_md(_milestone_completed_cell(m))} |"
            )
    opens = view.open_decisions
    if opens:
        out.append("")
        out.append(f"## Needs your decision ({len(opens)})")
        out.append("")
        out.append("| Decision | Question | Options | Blocks |")
        out.append("|---|---|---|---|")
        for d in opens:
            options = ", ".join(d.options) if d.options else "—"
            out.append(
                f"| {_md(d.decision_id)} | {_md(d.question or '(no question)')} "
                f"| {_md(options)} | {_md(d.blocks or '—')} |"
            )
        # rationale lines go AFTER the whole table — a markdown table block must be
        # contiguous, so a non-final decision's rationale must not split the rows
        # (audit facb0e5f grok-f1).
        for d in opens:
            if d.rationale:
                out.append("")
                out.append(f"_{_md(d.decision_id)} rationale:_ {_md(d.rationale)}")
    return out


def _data_method_md(view: ProjectView, now) -> list:
    out: list[str] = [""]
    out.append(f"📎 {_md(_health_rule_text(coverage_state(view, now=now).stale_days))}")
    if view.milestone_activity:
        out.append("")
        out.append("_Per-source freshness (latest milestone):_")
        for sa in view.milestone_activity:
            out.append(f"- **{_md(sa.source)}**: {_md(sa.last_occurred_at)}")
    return out


def _eng_md(view: ProjectView, repo_reality: "RepoReality | None") -> list:
    """Engineering ledger sections in markdown — top-level (engineering mode) or
    folded under 'More details' (business mode)."""
    out: list[str] = []
    if repo_reality is not None:
        lines = reconciliation_lines(repo_reality, view.event_count)
        out.append("")
        out.append(f"> 📊 **Repo reality:** {_md(lines[0])}")
        for line in lines[1:]:
            out.append(f"> {_md(line)}")
    if view.has_low_fidelity:
        out.append("")
        out.append(f"> ⚠️ **{_CAVEAT}**")

    active, resolved = _defect_groups(view)
    out.append("")
    out.append(f"## Open Defects ({len(active)})")
    out.append("")
    if active:
        out.append("| Severity | Defect | Summary | Status | Sources |")
        out.append("|---|---|---|---|---|")
        for d in active:
            out.append(f"| {_md((d.severity or '?').upper())} | {_md(d.defect_id)} "
                       f"| {_md(d.title or '(no title)')} | {_md(_status_label(d.status))} "
                       f"| {_md(', '.join(d.sources))} |")
    else:
        out.append("_No open defects._")

    out.append("")
    out.append(f"## Progress Timeline ({len(view.progress)})")
    out.append("")
    if view.progress:
        for p in view.progress:
            # phase_event is escaped too — the projection accepts any non-empty
            # string for it, so a stray backtick must not close a code span and
            # inject markdown into the docx intermediate (audit dfdcae4d f1).
            line = f"- **#{p.ledger_seq}** {_md(p.phase_event)} — {_md(p.title)} _({_md(p.source)})_"
            if p.pr_url:
                line += (f" — [PR]({p.pr_url})" if _is_clean_url(p.pr_url)
                         else f" — PR: {_md(p.pr_url)}")
            if p.commit_sha:
                line += f" commit {_md(p.commit_sha)}"
            out.append(line)
            if p.detail:
                out.append(f"  - {_md(p.detail)}")
    else:
        out.append("_No progress events._")

    if resolved:
        out.append("")
        out.append(f"## Resolved & Closed Defects ({len(resolved)})")
        out.append("")
        for d in resolved:
            fix = f" (fix: {_md(d.fixed_commit)})" if d.fixed_commit else ""
            out.append(f"- {_md(d.defect_id)}: {_md(d.title or '(no title)')} "
                       f"— **{_md(_status_label(d.status))}**{fix}")
            extras = _defect_extras(d)
            if extras:
                out.append(f"  - {' · '.join(f'{k}: {_md(v)}' for k, v in extras)}")

    if view.handoffs:
        out.append("")
        out.append(f"## Delivery Handbooks ({len(view.handoffs)})")
        out.append("")
        for hd in view.handoffs:
            out.append(f"- {_md(hd.manual_path)} ({_md(hd.generated_by)})")

    out.append("")
    out.append("## Sources")
    out.append("")
    for s in view.sources:
        out.append(f"- **{_md(s.source)}**: {s.event_count} events "
                   f"({_FIDELITY_NOTE.get(s.fidelity, s.fidelity)})")

    if view.warnings:
        out.append("")
        out.append(f"## Data notes ({len(view.warnings)})")
        out.append("")
        for w in view.warnings[:_MAX_WARNINGS]:
            out.append(f"- {_md(w)}")
        extra = len(view.warnings) - _MAX_WARNINGS
        if extra > 0:
            out.append(f"- _...and {extra} more_")

    return out


# --- html ---------------------------------------------------------------------

_HTML_CSS = (
    "body{font:15px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;max-width:880px;"
    "margin:2rem auto;padding:0 1rem;color:#1a1a1a}"
    "h1{font-size:1.6rem}h2{font-size:1.2rem;border-bottom:1px solid #ddd;padding-bottom:.2rem;margin-top:1.8rem}"
    ".meta{color:#666}.caveat{background:#fff8e1;border:1px solid #ffd54f;border-radius:6px;padding:.7rem 1rem;margin:1rem 0}"
    "table{border-collapse:collapse;width:100%}th,td{border:1px solid #ddd;padding:.35rem .5rem;text-align:left;vertical-align:top}"
    "th{background:#f5f5f5}.sev-critical{color:#b71c1c;font-weight:700}.sev-high{color:#e65100;font-weight:600}"
    "ul{padding-left:1.2rem}.src-low{color:#8d6e63}details{margin-top:1.5rem;color:#777}code{background:#f3f3f3;padding:0 .25rem;border-radius:3px}"
    ".reconcile{background:#e3f2fd;border:1px solid #90caf9;border-radius:6px;padding:.7rem 1rem;margin:1rem 0}"
)

# Business-report-only CSS, appended to _HTML_CSS ONLY when the view has a business
# block — so a pure engineering ledger's HTML is byte-for-byte unchanged (the head
# CSS does not grow). Audit facb0e5f claude-f1 + gpt-f4.
_HTML_BUSINESS_CSS = (
    ".health{padding:.7rem 1rem;border-radius:6px;margin:1rem 0;font-weight:600}"
    ".health-green{background:#e8f5e9;border:1px solid #a5d6a7}.health-yellow{background:#fff8e1;border:1px solid #ffd54f}"
    ".health-red{background:#ffebee;border:1px solid #ef9a9a}.bar{letter-spacing:1px;font-weight:400}"
)


def render_html(view: ProjectView, repo_reality: "RepoReality | None" = None,
                *, now=None) -> str:
    business = _has_business(view)
    css = _HTML_CSS + (_HTML_BUSINESS_CSS if business else "")
    parts: list[str] = []
    parts.append("<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">")
    parts.append(f"<title>Project Ledger — {_h(view.project_id)}</title>")
    parts.append(f"<style>{css}</style></head><body>")
    parts.append(f"<h1>Project Ledger — {_h(view.project_id)}</h1>")
    parts.append(f"<p class=\"meta\">{view.event_count} events · last activity: "
                 f"{_h(view.last_activity or 'n/a')}</p>")
    if business:
        parts += _business_html(view, now)
        parts.append("<details><summary>More details — engineering ledger &amp; data</summary>")
        parts += _data_method_html(view, now)
        parts += _eng_html(view, repo_reality)
        parts.append("</details>")
    else:
        parts += _eng_html(view, repo_reality)
    parts.append("</body></html>")
    return "".join(parts)


def _business_html(view: ProjectView, now) -> list:
    """Default business overview in HTML (a4 §6.1). Every producer string is
    html-escaped (`_h`) — untrusted, same stance as defect/progress text."""
    parts: list[str] = []
    light = health_light(view, now=now)
    cov = coverage_state(view, now=now)
    parts.append(
        f"<div class=\"health health-{_h(light.color)}\">{_HEALTH_EMOJI[light.color]} "
        f"<strong>{_h(_HEALTH_LABEL[light.color])}</strong> — {_health_summary(light, _h)}</div>"
    )
    chips = _kpi_chips(view)
    if chips:
        parts.append(
            "<div class=\"kpi\">At a glance: "
            + " · ".join(f"<span>{_h(c)}</span>" for c in chips)
            + "</div>"
        )
    if cov.is_stale:
        parts.append(f"<div class=\"caveat\">⏳ Stale: data older than {cov.stale_days} days "
                     f"from {_h(', '.join(cov.stale_sources))}.</div>")
    if view.milestones and cov.content_state == "all-cancelled":
        # §6.5: all-cancelled → note + activity overview, not the milestone table
        # or an N/A completion bar (audit facb0e5f gpt-f2).
        parts.append(f"<p><em>All {len(view.milestones)} milestone(s) cancelled — "
                     f"no active plan.</em></p>")
    elif view.milestones:
        bar, label = _completion_cells(completion(view))
        parts.append(f"<p><strong>Completion:</strong> <code class=\"bar\">{_h(bar)}</code> "
                     f"{_h(label)}</p>")
        parts.append(f"<h2>Milestones ({len(view.milestones)})</h2>")
        parts.append("<table><tr><th>Milestone</th><th>Status</th>"
                     "<th>Estimate / target</th><th>Completed</th></tr>")
        for m in view.milestones:
            parts.append(
                f"<tr><td>{_h(m.title or m.milestone_id)}</td>"
                f"<td>{_h(_MILESTONE_STATUS_LABEL.get(m.status, m.status))}</td>"
                f"<td>{_h(_milestone_target(m))}</td>"
                f"<td>{_h(_milestone_completed_cell(m))}</td></tr>"
            )
        parts.append("</table>")
    opens = view.open_decisions
    if opens:
        parts.append(f"<h2>Needs your decision ({len(opens)})</h2>")
        parts.append("<table><tr><th>Decision</th><th>Question</th>"
                     "<th>Options</th><th>Blocks</th></tr>")
        for d in opens:
            options = ", ".join(d.options) if d.options else "—"
            rationale = (f"<br><span class=\"meta\">{_h(d.rationale)}</span>"
                         if d.rationale else "")
            parts.append(
                f"<tr><td><code>{_h(d.decision_id)}</code></td>"
                f"<td>{_h(d.question or '(no question)')}{rationale}</td>"
                f"<td>{_h(options)}</td><td>{_h(d.blocks or '—')}</td></tr>"
            )
        parts.append("</table>")
    return parts


def _data_method_html(view: ProjectView, now) -> list:
    parts: list[str] = [
        f"<p class=\"meta\">📎 {_h(_health_rule_text(coverage_state(view, now=now).stale_days))}</p>"
    ]
    if view.milestone_activity:
        freshness = "; ".join(
            f"{_h(sa.source)}: {_h(sa.last_occurred_at)}" for sa in view.milestone_activity
        )
        parts.append(f"<p class=\"meta\">Per-source freshness (latest milestone): {freshness}</p>")
    return parts


def _eng_html(view: ProjectView, repo_reality: "RepoReality | None") -> list:
    """Engineering ledger sections in HTML — top-level (engineering mode) or folded
    under 'More details' (business mode)."""
    active, resolved = _defect_groups(view)
    parts: list[str] = []
    if repo_reality is not None:
        inner = "<br>".join(_h(line) for line in reconciliation_lines(repo_reality, view.event_count))
        parts.append(f"<div class=\"reconcile\">📊 {inner}</div>")
    if view.has_low_fidelity:
        parts.append(f"<div class=\"caveat\">⚠️ {_h(_CAVEAT)}</div>")

    parts.append(f"<h2>Open Defects ({len(active)})</h2>")
    if active:
        parts.append("<table><tr><th>Severity</th><th>Defect</th><th>Summary</th>"
                     "<th>Status</th><th>Sources</th></tr>")
        for d in active:
            sev = (d.severity or "?").lower()
            parts.append(
                f"<tr><td class=\"sev-{_h(sev)}\">{_h((d.severity or '?').upper())}</td>"
                f"<td><code>{_h(d.defect_id)}</code></td><td>{_h(d.title or '(no title)')}</td>"
                f"<td>{_h(_status_label(d.status))}</td><td>{_h(', '.join(d.sources))}</td></tr>"
            )
        parts.append("</table>")
    else:
        parts.append("<p><em>No open defects.</em></p>")

    parts.append(f"<h2>Progress Timeline ({len(view.progress)})</h2>")
    if view.progress:
        parts.append("<ul>")
        for p in view.progress:
            extra = ""
            if p.pr_url:
                extra += (f" — <a href=\"{_h(p.pr_url)}\">PR</a>" if _is_http_url(p.pr_url)
                          else f" — PR: {_h(p.pr_url)}")
            if p.commit_sha:
                extra += f" <code>{_h(p.commit_sha)}</code>"
            detail = f"<br><span class=\"meta\">{_h(p.detail)}</span>" if p.detail else ""
            parts.append(f"<li><strong>#{p.ledger_seq}</strong> <code>{_h(p.phase_event)}</code> — "
                         f"{_h(p.title)} <span class=\"meta\">({_h(p.source)})</span>{extra}{detail}</li>")
        parts.append("</ul>")
    else:
        parts.append("<p><em>No progress events.</em></p>")

    if resolved:
        parts.append(f"<h2>Resolved &amp; Closed Defects ({len(resolved)})</h2><ul>")
        for d in resolved:
            fix = f" (fix: <code>{_h(d.fixed_commit)}</code>)" if d.fixed_commit else ""
            extras = _defect_extras(d)
            extra_html = (
                "<br><span class=\"meta\">"
                + " · ".join(f"{k}: {_h(v)}" for k, v in extras)
                + "</span>"
            ) if extras else ""
            parts.append(f"<li><code>{_h(d.defect_id)}</code>: {_h(d.title or '(no title)')} — "
                         f"<strong>{_h(_status_label(d.status))}</strong>{fix}{extra_html}</li>")
        parts.append("</ul>")

    if view.handoffs:
        parts.append(f"<h2>Delivery Handbooks ({len(view.handoffs)})</h2><ul>")
        for hd in view.handoffs:
            parts.append(f"<li><code>{_h(hd.manual_path)}</code> ({_h(hd.generated_by)})</li>")
        parts.append("</ul>")

    parts.append("<h2>Sources</h2><ul>")
    for s in view.sources:
        cls = " class=\"src-low\"" if s.fidelity == "low" else ""
        parts.append(f"<li{cls}><strong>{_h(s.source)}</strong>: {s.event_count} events "
                     f"({_h(_FIDELITY_NOTE.get(s.fidelity, s.fidelity))})</li>")
    parts.append("</ul>")

    if view.warnings:
        parts.append(f"<details><summary>Data notes ({len(view.warnings)})</summary><ul>")
        for w in view.warnings[:_MAX_WARNINGS]:
            parts.append(f"<li>{_h(w)}</li>")
        extra = len(view.warnings) - _MAX_WARNINGS
        if extra > 0:
            parts.append(f"<li><em>...and {extra} more</em></li>")
        parts.append("</ul></details>")

    return parts


# --- json ---------------------------------------------------------------------


def render_json(view: ProjectView, repo_reality: "RepoReality | None" = None,
                *, now=None) -> str:
    """The structured ProjectView (a derived view — events.jsonl stays the
    canonical source, §5.8). Dataclass field order preserved; deterministic.

    With `repo_reality` (issue #245), a top-level `repo_reality` key is appended
    ALONGSIDE the view fields — the view dict itself is unchanged, so the default
    (no-flag) JSON shape is byte-for-byte backward compatible.

    When the view carries milestones or decisions, a `business` key adds the
    derived completion / health / coverage views (the same numbers the text/html
    business report shows) for machine consumers — additive, so a no-business view
    is byte-for-byte unchanged."""
    payload = asdict(view)
    if _has_business(view):
        payload = {**payload, "business": _business_json(view, now)}
    if repo_reality is not None:
        payload = {**payload, "repo_reality": asdict(repo_reality)}
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _business_json(view: ProjectView, now) -> dict:
    """Derived business view (completion / health / coverage) for JSON consumers."""
    comp = completion(view)
    light = health_light(view, now=now)
    cov = coverage_state(view, now=now)
    return {
        "completion": asdict(comp),
        "health": asdict(light),
        "coverage": asdict(cov),
    }
