"""ProjectView projection — a project's events.jsonl → the human-facing ledger.

The AQG READ side, counterpart to store.py's write side; NOT part of the shared
EAF producer/consumer contract. Reads a project's append-only StoredEvent log and
projects it into a ProjectView per DesignSpec §5.6 + §5.3.1:

  ① progress timeline    — kind=progress, in ledger_seq order
  ② defect projection    — kind=defect → per-defect state machine (§5.3.1)
  ③ handoff index        — kind=handoff
  ④ source distribution  — eaf / aqg-hook / manual counts + last activity
  ⑤ coverage caveat      — per-source fidelity + has_low_fidelity flag (§5.6 C5)

Replay is by ledger_seq (the consumer-authoritative order; occurred_at is
display-only — DesignSpec §5.1.1). Pure + deterministic: takes already-loaded
StoredEvent dicts (or reads a JSONL path). Defensive at the read boundary — a
malformed line / event is skipped with a warning and the projection NEVER raises
on bad data (it renders what is there, and surfaces the rest as telemetry
warnings). Pure stdlib.
"""
from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import NamedTuple

from ledger.seq import is_uuid, producer_seq, seq_sort_tuple

# --- status model (DesignSpec §5.3.1) -----------------------------------------

# action → projected status. The event stream carries only `action`; the current
# status is derived here, never stored (append-only purity).
_ACTION_STATUS = {
    "opened": "open",
    "status_changed": "fixing",
    "fixed": "fixed",
    "reopened": "open",
    "closed": "closed",
    "wontfix": "wontfix",
}
_ACTIVE = {"open", "fixing"}
_TERMINAL_HARD = {"closed", "wontfix"}   # absorbing; only `reopened` reactivates
_QUASI_TERMINAL = "fixed"                # forward-only; no silent regress to active

# Sources that DRIVE the defect state machine. aqg-hook is low-fidelity and emits
# progress only — a stray hook defect event is ignored here (§5.3.1 N7).
_DEFECT_DRIVING = {"eaf", "manual"}

_FIDELITY = {"eaf": "high", "aqg-hook": "low", "manual": "manual"}

# Ordering ranks for a deterministic, "open work first" defect list.
_STATUS_RANK = {"open": 0, "fixing": 0, "fixed": 1, "closed": 2, "wontfix": 2}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2, "low": 3}

# --- milestone / decision lifecycles (a4 §5.3 / §9) ---------------------------

# Sources that DRIVE the milestone/decision lifecycles. milestone is cross-source
# BY DESIGN (write-plan/manual emits `planned`, EAF emits execution actions —
# a4 §5.2 [a4:S]); aqg-hook is progress-only (conformance rejects its
# milestone/decision events, and the projection ignores a stray one defensively).
_LIFECYCLE_DRIVING = {"eaf", "manual"}

# action → projected status (derived here, never stored — append-only purity).
_MILESTONE_STATUS = {"planned": "planned", "started": "started",
                     "completed": "completed", "cancelled": "cancelled"}
_DECISION_STATUS = {"raised": "open", "resolved": "resolved"}
_MILESTONE_TERMINAL = {"completed", "cancelled"}
_DECISION_TERMINAL = {"resolved"}

# Action semantic ranks (a4 §5.3): the PRIMARY reduction order is the lifecycle's
# fixed action precedence, NOT cross-source producer_seq (which is incomparable
# across sources). A re-plan (planned with replan=true) is given a post-terminal
# rank so it sorts AFTER completed/cancelled and can reopen the milestone.
_MILESTONE_RANK = {"planned": 0, "started": 1, "completed": 2, "cancelled": 2}
_MILESTONE_REPLAN_RANK = 3
_DECISION_RANK = {"raised": 0, "resolved": 1}

# Business-language + calendar fields carried on a milestone (a4 §5.2). Captured
# last-write-wins across applied events; all optional at the projection layer
# (conformance enforces required-on-planned at ingress).
_MILESTONE_FIELD_KEYS = (
    "title", "summary", "key_outcome", "estimated_size", "planned_start",
    "target_date", "actual_date", "actual_wallclock", "gate", "phase_label",
)
_DECISION_FIELD_KEYS = ("question", "rationale", "blocks")

# Default staleness / pending-decision-age threshold in days (DesignSpec OQ-3).
STALE_DAYS_DEFAULT = 7


# --- view dataclasses (frozen — immutable, coding-style) ----------------------


@dataclass(frozen=True)
class ProgressEntry:
    ledger_seq: int
    phase_event: str
    title: str
    source: str
    occurred_at: str
    detail: "str | None" = None
    pr_url: "str | None" = None
    commit_sha: "str | None" = None


@dataclass(frozen=True)
class DefectEntry:
    defect_id: str
    status: str                      # open | fixing | fixed | closed | wontfix
    severity: "str | None"
    title: "str | None"
    first_seq: int                   # ledger_seq of the first applied event
    last_seq: int                    # ledger_seq of the last applied event
    occurred_at: str                 # occurred_at of the last applied event
    sources: "tuple[str, ...]"       # driving sources, sorted
    fixed_commit: "str | None" = None
    verification: "str | None" = None
    regression_anchor: "str | None" = None
    ignored_events: int = 0          # late / terminal-guarded / hook events skipped

    @property
    def is_open(self) -> bool:
        """Active work — open or in-progress (fixing). Drives the §5.6 ② list."""
        return self.status in _ACTIVE

    @property
    def is_resolved(self) -> bool:
        return self.status in _TERMINAL_HARD or self.status == _QUASI_TERMINAL


@dataclass(frozen=True)
class MilestoneEntry:
    """A reduced milestone (a4 §5.3). status is projected from the action stream by
    action semantic order across sources; business/calendar fields are last-write-
    wins across applied events. target_date is display-only (NOT progress/health)."""
    milestone_id: str
    status: str                      # planned | started | completed | cancelled
    title: "str | None"
    summary: "str | None"
    key_outcome: "str | None"
    estimated_size: "str | None"     # work-size estimate (a4 §5.2 [a4:T]); NOT wall-clock
    target_date: "str | None"        # optional calendar date; display-only
    planned_start: "str | None"
    actual_date: "str | None"        # completion date (action=completed)
    actual_wallclock: "str | None"   # optional EAF wall-clock; display-only
    gate: "str | None"
    phase_label: "str | None"
    first_seq: int                   # ledger_seq of the earliest applied event
    last_seq: int                    # ledger_seq of the latest applied event
    occurred_at: str                 # occurred_at of the latest applied event
    sources: "tuple[str, ...]"       # driving sources that applied an event, sorted
    replanned: bool = False          # a replan=true planned reopened it (a4 §5.3)
    ignored_events: int = 0          # post-terminal / non-driving events skipped

    @property
    def is_cancelled(self) -> bool:
        return self.status == "cancelled"

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    @property
    def is_effective(self) -> bool:
        """Counts toward completion (a4 §5.4) — every non-cancelled milestone."""
        return self.status != "cancelled"


@dataclass(frozen=True)
class DecisionEntry:
    """A reduced decision (a4 §9) — raised→resolved. Open (unresolved) decisions
    feed the pending-decisions list; raised_at anchors the §5.5 pending-too-long health rule."""
    decision_id: str
    status: str                      # open | resolved
    question: "str | None"
    options: "tuple[str, ...]"
    rationale: "str | None"
    blocks: "str | None"             # the milestone_id this decision blocks, if any
    first_seq: int
    last_seq: int
    raised_at: str                   # occurred_at of the raised event (age anchor)
    occurred_at: str                 # occurred_at of the latest applied event
    sources: "tuple[str, ...]"
    ignored_events: int = 0

    @property
    def is_open(self) -> bool:
        return self.status == "open"


@dataclass(frozen=True)
class HandoffEntry:
    ledger_seq: int
    manual_path: str
    generated_by: str
    source: str
    occurred_at: str


@dataclass(frozen=True)
class SourceStat:
    source: str
    fidelity: str                    # high | low | manual | unknown
    event_count: int


@dataclass(frozen=True)
class SourceActivity:
    """A driving source's latest MILESTONE activity, for per-source staleness
    (a3:T5 / §6.5 — per-source, NOT per-project max, so one active source can't
    mask another's stall). last_occurred_at is the occurred_at of that source's
    highest-ledger_seq milestone event (the authoritative-order latest)."""
    source: str
    last_occurred_at: str


@dataclass(frozen=True)
class ProjectView:
    project_id: str
    event_count: int                 # StoredEvents projected (excludes skipped lines)
    progress: "tuple[ProgressEntry, ...]"
    defects: "tuple[DefectEntry, ...]"
    handoffs: "tuple[HandoffEntry, ...]"
    sources: "tuple[SourceStat, ...]"
    last_activity: "str | None"      # occurred_at of the latest-recorded event
    has_low_fidelity: bool           # any aqg-hook events → render the §5.6 caveat
    warnings: "tuple[str, ...]"      # telemetry: skipped lines, ignored events
    # P2 additions (a4 §5.3 / §9) — appended with defaults so the prior 9-field
    # construction (render.py + its tests) stays source-compatible; render (P3)
    # consumes these later. milestone_activity carries per-source staleness anchors.
    milestones: "tuple[MilestoneEntry, ...]" = ()
    decisions: "tuple[DecisionEntry, ...]" = ()
    milestone_activity: "tuple[SourceActivity, ...]" = ()

    @property
    def open_defects(self) -> "tuple[DefectEntry, ...]":
        return tuple(d for d in self.defects if d.is_open)

    @property
    def open_decisions(self) -> "tuple[DecisionEntry, ...]":
        """Unresolved decisions — the pending-decisions list (a4 §9)."""
        return tuple(d for d in self.decisions if d.is_open)


# --- defensive load + sort (read boundary) ------------------------------------

# Fields the projection reads off every StoredEvent. A line missing any of these
# (or with the wrong type) is skipped with a warning — never crashes the report.
def _event_ok(ev) -> bool:
    return (
        isinstance(ev, dict)
        and isinstance(ev.get("event_id"), str)
        and isinstance(ev.get("ledger_seq"), int)
        and not isinstance(ev.get("ledger_seq"), bool)   # bool is an int subclass
        and isinstance(ev.get("project"), str)
        and isinstance(ev.get("source"), str)
        and isinstance(ev.get("kind"), str)
        and isinstance(ev.get("payload"), dict)
        and isinstance(ev.get("occurred_at"), str)
    )


def load_stored_events(path: "str | Path") -> "tuple[list[dict], list[str]]":
    """Read a project's events.jsonl → (events, warnings). Never raises.

    Mirrors store._existing_event_ids' defensive line-by-line read: a blank /
    non-JSON / non-object line is skipped with a warning rather than aborting the
    whole report. A missing file is an empty ledger (no warning)."""
    p = Path(path)
    events: list[dict] = []
    warnings: list[str] = []
    if not p.is_file():
        return events, warnings
    # Stream line-by-line (mirrors store._existing_event_ids) so an append-only
    # log that has grown to GBs is never fully materialized in memory — read_text
    # + splitlines() loaded the whole file (audit b5381a7d gpt-f9 + gemini-nf2).
    try:
        with p.open(encoding="utf-8") as fh:
            for lineno, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except (ValueError, json.JSONDecodeError) as exc:
                    warnings.append(f"line {lineno}: invalid JSON ({exc}); skipped")
                    continue
                if not isinstance(obj, dict):
                    warnings.append(f"line {lineno}: not a JSON object; skipped")
                    continue
                events.append(obj)
    except (OSError, UnicodeDecodeError) as exc:
        # A read/decode error surfaces the events read so far + a warning; never
        # raises (the projection renders what is there and reports the rest).
        warnings.append(f"could not read {p}: {type(exc).__name__}")
    return events, warnings


def _valid_events_sorted(events) -> "tuple[list[dict], list[str]]":
    """Filter to projectable StoredEvents and sort by ledger_seq (the authoritative
    replay order). event_id tie-breaks for determinism, though the store assigns
    ledger_seq uniquely per project."""
    valid: list[dict] = []
    warnings: list[str] = []
    for ev in events:
        if _event_ok(ev):
            valid.append(ev)
        else:
            eid = ev.get("event_id") if isinstance(ev, dict) else None
            warnings.append(f"event {eid!r}: missing/invalid required field; skipped")
    valid.sort(key=lambda e: (e["ledger_seq"], e["event_id"]))
    return valid, warnings


# --- defect state machine (§5.3.1) --------------------------------------------


def _transition(status: "str | None", action: str) -> "tuple[str | None, bool]":
    """Apply one action → (new_status, applied). `applied=False` means the action
    was guarded (state unchanged): a regression out of a terminal/quasi-terminal
    state by anything other than an explicit `reopened` (DesignSpec §5.3.1 N2)."""
    target = _ACTION_STATUS.get(action)
    if target is None:
        return status, False                      # unknown action (pre-validated away)
    if action == "reopened":
        return target, True                       # explicit reactivation, from any state
    if status in _TERMINAL_HARD:
        return status, False                      # closed/wontfix absorb everything else
    if status == _QUASI_TERMINAL and target in _ACTIVE:
        return status, False                      # fixed → no silent regress to active
    return target, True


_FIELD_KEYS = ("title", "severity", "fixed_commit", "verification", "regression_anchor")


def _project_defect(
    defect_id: str, evs: "list[dict]", uuid_warned: "set[str]"
) -> "tuple[DefectEntry | None, list[str]]":
    """Replay one defect's events (already in ledger_seq order) → DefectEntry.

    `uuid_warned` is shared across ALL defects (passed in by project_events) so the
    "source uses UUID producer_seq" notice fires once per source for the whole
    projection — a per-defect set would flood ProjectView.warnings and bury other
    notes under the cap (audit dfdcae4d gemini-f2).

    Returns (None, warnings) when no driving event applied (e.g. the defect_id was
    only ever seen in aqg-hook events, which do not drive the state machine — so it
    is not a tracked defect; §5.3.1 N7).
    """
    status: "str | None" = None
    # per-source high-water mark of producer_seq SEEN for THIS defect. Scoped per
    # (defect, source) — a global mark would let one defect's higher producer_seq
    # wrongly drop another defect's legitimate late event (same source, shared
    # monotonic counter). See DesignSpec §5.3.1 N6.
    watermark: "dict[str, tuple]" = {}
    fields: "dict[str, str]" = {}
    sources: "set[str]" = set()         # driving sources that APPLIED ≥1 event
    seen_sources: "set[str]" = set()    # driving sources that appeared (pre-filter)
    first_seq: "int | None" = None
    last_seq = 0
    occurred_at = ""
    ignored = 0
    warnings: list[str] = []

    for ev in evs:
        source = ev["source"]
        action = ev["payload"].get("action")
        eid = ev["event_id"]

        if source not in _DEFECT_DRIVING:
            ignored += 1
            warnings.append(
                f"defect {defect_id}: ignored {source} event {eid} "
                f"(only eaf/manual drive the defect state machine)"
            )
            continue
        seen_sources.add(source)   # seen even if later skipped as late/terminal-guarded

        seq_str = producer_seq(eid)
        if is_uuid(seq_str):
            # §5.1.1 escape hatch: a UUID producer_seq has no monotonic order, so
            # the watermark cannot tell stale from fresh — applying the <= check
            # would drop ~half of a producer's events. Skip stale-rejection and
            # apply in ledger_seq order. This means a UUID-driven defect lifecycle
            # has NO stale-event protection (a late 'reopened' can reactivate a
            # closed defect) — the documented §5.1.1 tradeoff, surfaced by the
            # once-per-source warning (audit 33f6e5df f3 + dfdcae4d f2).
            if source not in uuid_warned:
                warnings.append(
                    f"source {source} uses UUID producer_seq (no monotonic order) — "
                    f"watermark stale-protection disabled for its defects, applying in "
                    f"ledger_seq order (§5.1.1)"
                )
                uuid_warned.add(source)
        else:
            seq_key = seq_sort_tuple(seq_str)
            if source in watermark and seq_key <= watermark[source]:
                ignored += 1
                warnings.append(
                    f"defect {defect_id}: ignored late '{action}' from {source} "
                    f"(event {eid}: producer_seq <= watermark)"
                )
                continue
            watermark[source] = seq_key   # non-stale: advance the seen-high-water mark

        new_status, applied = _transition(status, action)
        if not applied:
            ignored += 1
            warnings.append(
                f"defect {defect_id}: ignored '{action}' from {source} "
                f"in terminal state '{status}' (only 'reopened' reactivates)"
            )
            continue

        if status is None and action != "opened":
            warnings.append(
                f"defect {defect_id}: first applied event is '{action}', not 'opened' "
                f"(event {eid}); projecting best-effort"
            )

        status = new_status
        sources.add(source)
        payload = ev["payload"]
        for key in _FIELD_KEYS:
            val = payload.get(key)
            if isinstance(val, str) and val:
                fields[key] = val
        if first_seq is None:
            first_seq = ev["ledger_seq"]
        last_seq = ev["ledger_seq"]
        occurred_at = ev["occurred_at"]

    if status is None or first_seq is None:
        return None, warnings

    # §5.3.1 N7 specifies a 'manual > eaf' tie-break for a defect touched by BOTH
    # sources, but the exact semantics (status only? fields? does manual
    # permanently override?) are under-specified and v1 has no manual writer (EAF
    # is the sole v1 writer, §8). So we apply cross-source events in ledger_seq
    # order and surface the unresolved case loudly rather than guess (audit
    # 33f6e5df gpt-f2; Owner 2026-05-30 DEFERRED the precise semantics to v2 — to
    # be decided alongside the manual writer, see DesignSpec OQ-13).
    # Keyed on SEEN sources, not applied — a manual event skipped as late/terminal
    # still means the precedence question is live (audit dfdcae4d gpt-f4).
    if "eaf" in seen_sources and "manual" in seen_sources:
        warnings.append(
            f"defect {defect_id}: has both eaf and manual events — §5.3.1 'manual > eaf' "
            f"precise semantics are deferred to v2 (DesignSpec OQ-13); v1 applies them in "
            f"ledger_seq order"
        )

    entry = DefectEntry(
        defect_id=defect_id,
        status=status,
        severity=fields.get("severity"),
        title=fields.get("title"),
        first_seq=first_seq,
        last_seq=last_seq,
        occurred_at=occurred_at,
        sources=tuple(sorted(sources)),
        fixed_commit=fields.get("fixed_commit"),
        verification=fields.get("verification"),
        regression_anchor=fields.get("regression_anchor"),
        ignored_events=ignored,
    )
    return entry, warnings


def _defect_sort_key(d: DefectEntry) -> tuple:
    """Open work first, then by severity, then oldest-first within a group."""
    return (
        _STATUS_RANK.get(d.status, 9),
        _SEVERITY_RANK.get(d.severity or "", 9),
        d.first_seq,
    )


# --- milestone / decision reduction (a4 §5.3 / §9) ----------------------------

# Shared lifecycle reducer result. status is None when no driving event applied
# (the item is not tracked); warnings always carries the per-item telemetry.
class _Fold(NamedTuple):
    status: "str | None"
    applied: "list[dict]"            # state-changing events in sorted order (last = current)
    first_seq: int
    last_seq: int
    occurred_at: str
    sources: "tuple[str, ...]"
    ignored: int
    warnings: "list[str]"


def _milestone_rank(ev: dict) -> int:
    """Sort rank for a milestone event (a4 §5.3). A replan=true planned sorts AFTER
    terminals so it can reopen; every other action keeps its semantic precedence."""
    p = ev["payload"]
    if p.get("action") == "planned" and p.get("replan") is True:
        return _MILESTONE_REPLAN_RANK
    return _MILESTONE_RANK.get(p.get("action"), 0)


def _decision_rank(ev: dict) -> int:
    return _DECISION_RANK.get(ev["payload"].get("action"), 0)


def _semantic_fold(
    label: str, item_id: str, evs: "list[dict]", *, rank_of, status_of: dict, terminal: set
) -> _Fold:
    """Reduce one lifecycle item's events by ACTION semantic order (a4 §5.3).

    Sorts by (rank_of(ev), seq_sort_tuple(producer_seq), event_id): the action
    semantic rank is PRIMARY (cross-source producer_seq is incomparable, a4:S),
    producer_seq is the within-rank tie-break (monotonic per source — conformance
    forbids a UUID producer_seq for these kinds), event_id the final deterministic
    fallback. Folds with terminal-absorb + reopen escape: once a terminal action
    applies, a later event is ignored UNLESS its rank exceeds the terminal's (only
    a milestone replan does — its post-terminal rank reopens the lifecycle).

    Non-driving (e.g. aqg-hook) and unknown-action events are skipped with a
    warning, never applied. Returns a _Fold (status None ⇒ not tracked).

    `occurred_at` / `last_seq` are taken from the LAST applied event in sorted
    order — i.e. the highest-action-rank event, which is the one that determines
    the current status (the winning terminal, or the replan that reopened it).
    A stray lower-rank event that sorts before the terminal (e.g. a non-replan
    planned that arrived after a completion) is absorbed without redefining the
    milestone's current-state timestamp (audit b0a5a67c gpt-f2).

    Cross-source caveat: when two same-action events come from DIFFERENT sources,
    their producer_seq is incomparable (a4:S), so the within-rank tie-break falls
    through to the `event_id` final key — deterministic, but NOT semantically
    "earlier/later". For two conflicting terminals this means a deterministic (not
    chronological) winner; the loser is ignored + warned (audit b0a5a67c claude-f3).

    NOTE (v1 scope): a single re-plan resetting a milestone to planned is
    supported; a milestone that RE-completes after a re-plan is a multi-epoch
    case that cross-source ordering cannot disambiguate (a4 §5.3 uses action
    order precisely because cross-source producer_seq is incomparable) — it
    projects the re-planned state and is documented as deferred past v1.
    """
    ordered = sorted(
        evs,
        key=lambda e: (rank_of(e), seq_sort_tuple(producer_seq(e["event_id"])), e["event_id"]),
    )
    status: "str | None" = None
    in_terminal = False
    terminal_rank = -1
    applied: "list[dict]" = []
    sources: "set[str]" = set()
    ignored = 0
    warnings: "list[str]" = []

    for ev in ordered:
        source = ev["source"]
        action = ev["payload"].get("action")
        eid = ev["event_id"]
        if source not in _LIFECYCLE_DRIVING:
            ignored += 1
            warnings.append(
                f"{label} {item_id}: ignored {source} event {eid} "
                f"(only eaf/manual drive {label}s)"
            )
            continue
        if action not in status_of:
            ignored += 1
            warnings.append(f"{label} {item_id}: ignored unknown action {action!r} (event {eid})")
            continue
        r = rank_of(ev)
        if in_terminal and r <= terminal_rank:
            ignored += 1
            warnings.append(
                f"{label} {item_id}: ignored '{action}' from {source} after terminal "
                f"(event {eid}: rank {r} <= terminal rank {terminal_rank})"
            )
            continue
        if in_terminal:                       # r > terminal_rank → a reopen (replan)
            in_terminal = False
        applied.append(ev)
        sources.add(source)
        status = status_of[action]
        if action in terminal:
            in_terminal = True
            terminal_rank = r

    if status is None:
        return _Fold(None, [], 0, 0, "", (), ignored, warnings)
    first_seq = min(ev["ledger_seq"] for ev in applied)
    # The status-determining event = the last applied in sorted (semantic) order =
    # highest action rank (the winning terminal, or the replan that reopened).
    determinant = applied[-1]
    return _Fold(
        status=status,
        applied=applied,
        first_seq=first_seq,
        last_seq=determinant["ledger_seq"],
        occurred_at=determinant["occurred_at"],
        sources=tuple(sorted(sources)),
        ignored=ignored,
        warnings=warnings,
    )


def _project_milestone(
    milestone_id: str, evs: "list[dict]"
) -> "tuple[MilestoneEntry | None, list[str]]":
    """Reduce one milestone's events → MilestoneEntry (a4 §5.3). Returns (None,
    warnings) when no driving event applied (e.g. only aqg-hook events)."""
    f = _semantic_fold(
        "milestone", milestone_id, evs,
        rank_of=_milestone_rank, status_of=_MILESTONE_STATUS, terminal=_MILESTONE_TERMINAL,
    )
    if f.status is None:
        return None, f.warnings
    fields: "dict[str, str]" = {}
    replanned = False
    for ev in f.applied:
        p = ev["payload"]
        if p.get("action") == "planned" and p.get("replan") is True:
            replanned = True
        for key in _MILESTONE_FIELD_KEYS:
            val = p.get(key)
            if isinstance(val, str) and val:
                fields[key] = val
    # Completion-only fields are exposed ONLY when the current status is completed —
    # a re-plan (status→planned) must not retain the voided epoch's actual_date /
    # actual_wallclock (audit b0a5a67c gemini-f1).
    is_completed = f.status == "completed"
    entry = MilestoneEntry(
        milestone_id=milestone_id,
        status=f.status,
        title=fields.get("title"),
        summary=fields.get("summary"),
        key_outcome=fields.get("key_outcome"),
        estimated_size=fields.get("estimated_size"),
        target_date=fields.get("target_date"),
        planned_start=fields.get("planned_start"),
        actual_date=fields.get("actual_date") if is_completed else None,
        actual_wallclock=fields.get("actual_wallclock") if is_completed else None,
        gate=fields.get("gate"),
        phase_label=fields.get("phase_label"),
        first_seq=f.first_seq,
        last_seq=f.last_seq,
        occurred_at=f.occurred_at,
        sources=f.sources,
        replanned=replanned,
        ignored_events=f.ignored,
    )
    return entry, f.warnings


def _project_decision(
    decision_id: str, evs: "list[dict]"
) -> "tuple[DecisionEntry | None, list[str]]":
    """Reduce one decision's events → DecisionEntry (a4 §9). raised→open,
    resolved→terminal. Returns (None, warnings) when no driving event applied."""
    f = _semantic_fold(
        "decision", decision_id, evs,
        rank_of=_decision_rank, status_of=_DECISION_STATUS, terminal=_DECISION_TERMINAL,
    )
    if f.status is None:
        return None, f.warnings
    fields: "dict[str, str]" = {}
    options: "tuple[str, ...]" = ()
    raised_at: "str | None" = None
    for ev in f.applied:
        p = ev["payload"]
        # Anchor the age to the FIRST raise (earliest by sort order) — a later
        # same-id re-raise must not reset the pending-age clock and suppress the
        # §5.5 aged-decision red signal (audit b0a5a67c gpt-f3 + deepseek-f1).
        if p.get("action") == "raised" and raised_at is None:
            raised_at = ev["occurred_at"]
        for key in _DECISION_FIELD_KEYS:
            val = p.get(key)
            if isinstance(val, str) and val:
                fields[key] = val
        opts = p.get("options")
        if isinstance(opts, list) and all(isinstance(o, str) for o in opts):
            options = tuple(opts)
    entry = DecisionEntry(
        decision_id=decision_id,
        status=f.status,
        question=fields.get("question"),
        options=options,
        rationale=fields.get("rationale"),
        blocks=fields.get("blocks"),
        first_seq=f.first_seq,
        last_seq=f.last_seq,
        raised_at=raised_at if raised_at is not None else f.occurred_at,
        occurred_at=f.occurred_at,
        sources=f.sources,
        ignored_events=f.ignored,
    )
    return entry, f.warnings


def _milestone_sort_key(m: MilestoneEntry) -> tuple:
    """Chronological by first appearance (the plan/business order)."""
    return (m.first_seq, m.milestone_id)


def _decision_sort_key(d: DecisionEntry) -> tuple:
    """Open (pending-decision) first, then by first appearance."""
    return (d.is_open is False, d.first_seq, d.decision_id)


# --- top-level projection -----------------------------------------------------


def project_events(events, *, project_id: "str | None" = None) -> ProjectView:
    """Project an iterable of StoredEvent dicts into a ProjectView (§5.6).

    project_id, when omitted, is taken from the events (the store writes one log
    per project, so they share it); falls back to '<unknown>' for an empty ledger.
    """
    valid, warnings = _valid_events_sorted(list(events))

    pid = project_id or (valid[0]["project"] if valid else "<unknown>")

    progress: list[ProgressEntry] = []
    handoffs: list[HandoffEntry] = []
    defect_evs: "dict[str, list[dict]]" = {}
    milestone_evs: "dict[str, list[dict]]" = {}
    decision_evs: "dict[str, list[dict]]" = {}
    # per-source latest milestone event (by ledger_seq = authoritative order) → its
    # occurred_at, the per-source staleness anchor (a3:T5 / §6.5). Driving sources only.
    milestone_last: "dict[str, tuple[int, str]]" = {}

    for ev in valid:
        kind = ev["kind"]
        payload = ev["payload"]
        if kind == "progress":
            entry = _progress_entry(ev, payload, warnings)
            if entry is not None:
                progress.append(entry)
        elif kind == "handoff":
            entry = _handoff_entry(ev, payload, warnings)
            if entry is not None:
                handoffs.append(entry)
        elif kind == "defect":
            did = payload.get("defect_id")
            if isinstance(did, str) and did:
                defect_evs.setdefault(did, []).append(ev)
            else:
                warnings.append(
                    f"defect event {ev['event_id']}: missing/invalid defect_id; skipped"
                )
        elif kind == "milestone":
            mid = payload.get("milestone_id")
            if isinstance(mid, str) and mid:
                milestone_evs.setdefault(mid, []).append(ev)
                src = ev["source"]
                if src in _LIFECYCLE_DRIVING:
                    prev = milestone_last.get(src)
                    if prev is None or ev["ledger_seq"] > prev[0]:
                        milestone_last[src] = (ev["ledger_seq"], ev["occurred_at"])
            else:
                warnings.append(
                    f"milestone event {ev['event_id']}: missing/invalid milestone_id; skipped"
                )
        elif kind == "decision":
            did = payload.get("decision_id")
            if isinstance(did, str) and did:
                decision_evs.setdefault(did, []).append(ev)
            else:
                warnings.append(
                    f"decision event {ev['event_id']}: missing/invalid decision_id; skipped"
                )
        # unknown kind: pre-validated away on write; ignore defensively.

    defects: list[DefectEntry] = []
    uuid_warned: "set[str]" = set()   # shared across defects → once-per-source notice
    for did, evs in defect_evs.items():
        entry, dwarn = _project_defect(did, evs, uuid_warned)
        warnings.extend(dwarn)
        if entry is not None:
            defects.append(entry)
    defects.sort(key=_defect_sort_key)

    milestones: list[MilestoneEntry] = []
    for mid, evs in milestone_evs.items():
        m_entry, mwarn = _project_milestone(mid, evs)
        warnings.extend(mwarn)
        if m_entry is not None:
            milestones.append(m_entry)
    milestones.sort(key=_milestone_sort_key)

    decisions: list[DecisionEntry] = []
    for did, evs in decision_evs.items():
        d_entry, decwarn = _project_decision(did, evs)
        warnings.extend(decwarn)
        if d_entry is not None:
            decisions.append(d_entry)
    decisions.sort(key=_decision_sort_key)

    milestone_activity = tuple(
        SourceActivity(src, milestone_last[src][1]) for src in sorted(milestone_last)
    )

    counts = Counter(ev["source"] for ev in valid)
    sources = tuple(
        SourceStat(src, _FIDELITY.get(src, "unknown"), counts[src])
        for src in sorted(counts)
    )
    last_activity = valid[-1]["occurred_at"] if valid else None

    return ProjectView(
        project_id=pid,
        event_count=len(valid),
        progress=tuple(progress),
        defects=tuple(defects),
        milestones=tuple(milestones),
        decisions=tuple(decisions),
        handoffs=tuple(handoffs),
        sources=sources,
        milestone_activity=milestone_activity,
        last_activity=last_activity,
        has_low_fidelity=counts.get("aqg-hook", 0) > 0,
        warnings=tuple(warnings),
    )


def project_log(path: "str | Path", *, project_id: "str | None" = None) -> ProjectView:
    """Convenience: load a project's events.jsonl then project it. Read-boundary
    warnings (skipped lines) are folded into the ProjectView.warnings."""
    events, load_warnings = load_stored_events(path)
    view = project_events(events, project_id=project_id)
    if load_warnings:
        view = _with_warnings(view, load_warnings)
    return view


def _with_warnings(view: ProjectView, extra: "list[str]") -> ProjectView:
    """Prepend read-boundary warnings (frozen view → return a new copy)."""
    return replace(view, warnings=tuple(extra) + view.warnings)


def _progress_entry(ev: dict, payload: dict, warnings: list) -> "ProgressEntry | None":
    title = payload.get("title")
    phase = payload.get("phase_event")
    if not (isinstance(title, str) and title and isinstance(phase, str) and phase):
        warnings.append(f"progress event {ev['event_id']}: missing title/phase_event; skipped")
        return None
    return ProgressEntry(
        ledger_seq=ev["ledger_seq"],
        phase_event=phase,
        title=title,
        source=ev["source"],
        occurred_at=ev["occurred_at"],
        detail=_opt_str(payload.get("detail")),
        pr_url=_opt_str(payload.get("pr_url")),
        commit_sha=_opt_str(payload.get("commit_sha")),
    )


def _handoff_entry(ev: dict, payload: dict, warnings: list) -> "HandoffEntry | None":
    manual_path = payload.get("manual_path")
    generated_by = payload.get("generated_by")
    if not (isinstance(manual_path, str) and manual_path):
        warnings.append(f"handoff event {ev['event_id']}: missing manual_path; skipped")
        return None
    return HandoffEntry(
        ledger_seq=ev["ledger_seq"],
        manual_path=manual_path,
        generated_by=generated_by if isinstance(generated_by, str) else "",
        source=ev["source"],
        occurred_at=ev["occurred_at"],
    )


def _opt_str(value) -> "str | None":
    return value if isinstance(value, str) and value else None


# --- derived views (a4 §5.4 completion / §5.5 health / §6.5 coverage) ----------
#
# These are PURE derivations over a ProjectView. completion() is time-free; the
# health light and coverage staleness need a reference "now", so those take it as
# an explicit argument rather than reading the wall clock — project_events stays a
# pure, time-free projection (occurred_at is display-only, §5.1.1) and the
# derivations are deterministic + unit-testable with a fixed now.


@dataclass(frozen=True)
class Completion:
    """Stage-count completion (a4 §5.4) — NOT time-based. effective = non-cancelled
    milestones; ratio = completed / effective, or None ("N/A") when effective == 0
    (no division by zero — a4 §5.4 / acceptance ⑤)."""
    completed: int
    effective: int
    total: int                       # all milestones, incl. cancelled
    ratio: "float | None"            # 0.0-1.0, or None when effective == 0

    @property
    def is_na(self) -> bool:
        return self.ratio is None

    @property
    def percent(self) -> "int | None":
        """Whole-percent completion (half-up), or None for N/A. Half-up not
        banker's round() — a progress bar reads 1/8 as 13%, not 12% (audit
        b0a5a67c qwen-f2). Render formats the display."""
        return None if self.ratio is None else int(self.ratio * 100 + 0.5)


def completion(view: ProjectView) -> Completion:
    """Completion over a view's milestones (a4 §5.4). Pure + time-free."""
    total = len(view.milestones)
    completed = sum(1 for m in view.milestones if m.is_completed)
    effective = sum(1 for m in view.milestones if m.is_effective)
    ratio = (completed / effective) if effective else None
    return Completion(completed=completed, effective=effective, total=total, ratio=ratio)


def _event_date(value) -> "date | None":
    """Calendar date from the leading YYYY-MM-DD of an RFC3339 / date string. None
    on bad input (never raises). Day-granular by design: the staleness threshold is
    days, so sub-day timezone offsets (<= 14h < 1 day) are immaterial, and this
    avoids datetime.fromisoformat's py3.9 gaps with 'Z' / 3-digit fractions
    (conformance.py:70-73)."""
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except (ValueError, IndexError):
        return None


def _to_date(now) -> "date | None":
    """Coerce a report 'now' (date / datetime / ISO-8601 string) → date. datetime
    is a date subclass, so it is checked first."""
    if isinstance(now, datetime):
        return now.date()
    if isinstance(now, date):
        return now
    if isinstance(now, str):
        return _event_date(now)
    return None


def _age_days(occurred_at, now_date: "date | None") -> "int | None":
    """Whole days from occurred_at to now. None if either side is unparseable."""
    d0 = _event_date(occurred_at)
    if d0 is None or now_date is None:
        return None
    return (now_date - d0).days


def _is_stale(occurred_at, now_date: "date | None", n: int) -> bool:
    """True iff occurred_at is more than n whole days before now (unparseable → not
    stale, so a bad timestamp never silently trips the light)."""
    age = _age_days(occurred_at, now_date)
    return age is not None and age > n


@dataclass(frozen=True)
class HealthSignal:
    # code ∈ {critical_defect, aged_decision, high_defect, blocked, stale}
    level: str                       # red | yellow
    code: str
    detail: str                      # human-facing factual detail (id / source / age)


@dataclass(frozen=True)
class HealthLight:
    """Rule-based health light (a4 §5.5). color is the worst level present. No
    target_date dependency — "behind" is expressed only as `stale` (data went quiet),
    never as an overdue calendar date (a4 §5.5 [a4:T] / acceptance ⑱)."""
    color: str                       # red | yellow | green
    signals: "tuple[HealthSignal, ...]"
    stale_days: int                  # the N threshold applied (for the report footnote)


def health_light(view: ProjectView, *, now, stale_days: int = STALE_DAYS_DEFAULT) -> HealthLight:
    """Rule-based health light (a4 §5.5). `now` (date/datetime/ISO string) anchors
    the age-based rules. Pure: same view + now ⇒ same light.

    🔴 open critical defect · open decision pending > N days
    🟡 open high defect · blocked (latest progress phase_event=blocked) · stale
       (a driving source's latest milestone > N days old)
    🟢 none of the above
    """
    now_date = _to_date(now)
    red: "list[HealthSignal]" = []
    yellow: "list[HealthSignal]" = []

    for d in view.open_defects:
        if d.severity == "critical":
            red.append(HealthSignal("red", "critical_defect", d.defect_id))
        elif d.severity == "high":
            yellow.append(HealthSignal("yellow", "high_defect", d.defect_id))

    for dec in view.open_decisions:
        age = _age_days(dec.raised_at, now_date)
        if age is not None and age > stale_days:
            red.append(HealthSignal("red", "aged_decision", f"{dec.decision_id} ({age}d)"))

    # blocked = the CURRENT state (latest progress is blocked), not ever-blocked —
    # a later stage_advanced clears it (a3:T6: signal source is progress only).
    if view.progress and view.progress[-1].phase_event == "blocked":
        yellow.append(HealthSignal("yellow", "blocked", view.progress[-1].title))

    for sa in view.milestone_activity:
        if _is_stale(sa.last_occurred_at, now_date, stale_days):
            age = _age_days(sa.last_occurred_at, now_date)
            yellow.append(HealthSignal("yellow", "stale", f"{sa.source} ({age}d)"))

    color = "red" if red else ("yellow" if yellow else "green")
    return HealthLight(color=color, signals=tuple(red) + tuple(yellow), stale_days=stale_days)


@dataclass(frozen=True)
class CoverageState:
    """Coverage content-state + per-source staleness (a4 §6.5). content_state is the
    mutually-exclusive, priority-ordered render mode; is_stale is the orthogonal
    freshness flag (per-source, NOT per-project max — a3:T5)."""
    content_state: str               # no-milestone | all-cancelled | in-progress | planned-only
    is_stale: bool
    stale_sources: "tuple[str, ...]"
    stale_days: int


def coverage_state(
    view: ProjectView, *, now, stale_days: int = STALE_DAYS_DEFAULT
) -> CoverageState:
    """Coverage content-state (time-free) + per-source staleness (needs `now`),
    a4 §6.5. The four content states are matched by priority top-down."""
    ms = view.milestones
    if not ms:
        content = "no-milestone"
    else:
        effective = [m for m in ms if m.is_effective]
        if not effective:
            content = "all-cancelled"
        elif any(m.status in ("started", "completed") for m in effective):
            content = "in-progress"
        else:
            content = "planned-only"

    now_date = _to_date(now)
    stale_sources = tuple(
        sa.source for sa in view.milestone_activity
        if _is_stale(sa.last_occurred_at, now_date, stale_days)
    )
    return CoverageState(
        content_state=content,
        is_stale=bool(stale_sources),
        stale_sources=stale_sources,
        stale_days=stale_days,
    )
