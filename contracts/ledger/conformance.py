"""IncomingEvent conformance validator — pure stdlib (no jsonschema dependency).

Shared by the AQG inbox consumer AND the EAF exporter (self-test before push),
per DesignSpec OQ-12. Mirrors incoming-event.schema.json; the golden fixtures +
these tests are the cross-check that the two stay in sync. Accumulates ALL
errors (does not short-circuit) and NEVER raises on a malformed event (every
problem is a string in the returned list).

Why hand-rolled, not jsonschema: AQG ships to others and keeps scripts
zero-third-party-dependency (DesignSpec OQ-10). The JSON Schema file remains the
machine-readable contract document; this is the runnable check both sides embed.
"""
from __future__ import annotations

import re
from datetime import datetime

from ledger.seq import is_uuid, producer_seq

# Current contract version. 1.1 = 1.0 + the milestone/decision kinds (a strict
# superset — progress/defect/handoff are byte-for-byte unchanged), so a 1.1
# consumer accepts BOTH during rollout for backward compat (DesignSpec a4 §5.6).
SCHEMA_VERSION = "1.1"
_SUPPORTED_VERSIONS = {"1.0", "1.1"}

_SOURCES = {"eaf", "aqg-hook", "manual"}
_KINDS = {"progress", "defect", "handoff", "milestone", "decision"}
# Added in 1.1 — a producer emitting one MUST declare schema_version 1.1 (the
# capability handshake; 1.0 has no milestone/decision semantics, a4 §5.6).
_V11_KINDS = {"milestone", "decision"}
# aqg-hook is low-fidelity / progress-only; only these are projected for it.
_AQG_HOOK_KINDS = {"progress", "handoff"}
_TOP_KEYS = {
    "schema_version", "event_id", "content_checksum", "project",
    "source", "source_ref", "kind", "payload", "occurred_at",
}
_REQUIRED_TOP = {
    "schema_version", "event_id", "project", "source",
    "source_ref", "kind", "payload", "occurred_at",
}
_SOURCE_REF_KEYS = {"run_id", "work_packet_id", "session_id"}
_PHASE_EVENTS = {"started", "stage_advanced", "completed", "blocked"}
_DEFECT_ACTIONS = {"opened", "status_changed", "fixed", "reopened", "closed", "wontfix"}
_SEVERITIES = {"critical", "high", "medium", "low"}
_GENERATED_BY = {"eaf-handoff", "aqg-handoff"}
_MILESTONE_ACTIONS = {"planned", "started", "completed", "cancelled"}
_DECISION_ACTIONS = {"raised", "resolved"}

_PROGRESS_KEYS = {"phase_event", "title", "detail", "pr_url", "commit_sha"}
_DEFECT_KEYS = {"defect_id", "action", "title", "severity", "fixed_commit", "verification", "regression_anchor"}
_HANDOFF_KEYS = {"manual_path", "generated_by"}
_MILESTONE_KEYS = {
    "milestone_id", "action", "title", "summary", "key_outcome", "estimated_size",
    "planned_start", "target_date", "actual_date", "actual_wallclock", "replan",
    "gate", "phase_label",
}
_DECISION_KEYS = {"decision_id", "action", "question", "options", "rationale", "blocks"}

# StoredEvent-only fields a producer must never send (AQG generates them).
_STORED_ONLY = {"recorded_at", "ledger_seq"}

# event_id = '<source>:<run_or_session_id>:<producer_seq>'. Path-segment-safe
# alphabet only ([A-Za-z0-9._:-]); paths.py percent-encodes it to an injective
# cross-platform inbox filename. First two segments are colon-free + non-empty;
# the producer_seq tail may contain ':'.
_EVENT_ID_RE = re.compile(r"^[A-Za-z0-9._-]+:[A-Za-z0-9._-]+:[A-Za-z0-9._:-]+$")

# RFC 3339 date-time, tolerant of ANY fractional-second precision (audit
# gemini-f2: py3.9 datetime.fromisoformat rejects 3-digit '.123Z' that JS/Node
# producers emit, while the JSON Schema 'format:date-time' permits it).
_RFC3339_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[Tt]\d{2}:\d{2}:\d{2}(\.\d+)?([Zz]|[+-]\d{2}:\d{2})$"
)

# Calendar date YYYY-MM-DD for milestone planned_start / target_date / actual_date
# (a4 §5.2). Range-checked like occurred_at so '2026-13-99' is rejected.
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# project is the on-disk partition key (paths.project_events_path nests it as
# subdirs), so the SAFETY invariant is: no segment may be '.'/'..'/empty and the
# value may not be absolute/backslashed/control-char-bearing, else it ESCAPES the
# ledger root on write (audit b5381a7d gpt-f1 + gemini-kf1 + o3-f1; workflow A1).
# This is a SAFETY gate, not a shape gate: project_id.py canonically emits an
# 'owner/repo' slug (>=2 segments) or 'local:<16-hex>', but conformance accepts
# any traversal-free [A-Za-z0-9._-] segment chain (>=1) so it never over-rejects a
# safe id. Mirrors incoming-event.schema.json project.pattern; paths.py adds a
# resolve()-containment backstop. BOTH cases allowed — the project FIELD may be
# mixed-case (golden fixture Example-Org/Example-Repo) though project_id.py lowercases.
_PROJECT_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*$")
_PROJECT_LOCAL_RE = re.compile(r"^local:[0-9a-f]{16}$")

# event_id length cap: an unbounded id overflows the inbox filename (ENAMETOOLONG
# on write — audit b5381a7d gemini-nf3) and feeds an unbounded int() into
# seq_sort_tuple. 200 is far above any real '<source>:<run>:<producer_seq>'.
_EVENT_ID_MAXLEN = 200


def _valid_project_id(value) -> bool:
    """True iff value is a safe, well-formed project_id (slug or local form) with
    no path-traversal / absolute / unsafe segment. Never raises."""
    if not isinstance(value, str) or not value:
        return False
    if _PROJECT_LOCAL_RE.match(value):
        return True
    if not _PROJECT_SLUG_RE.match(value):
        return False
    # the slug regex accepts '.'/'..' as a segment (both are [A-Za-z0-9._-]+);
    # reject those explicitly so an id can never carry a traversal segment.
    return not any(seg in (".", "..") for seg in value.split("/"))


def _occurred_at_in_range(value: str) -> bool:
    """Range-validate the calendar/clock fields ('2026-99-99T99:99:99Z' matches the
    shape-only RFC3339 regex; audit b5381a7d gpt-f8). datetime() is fed only the
    leading YYYY-MM-DDTHH:MM:SS, so any fractional precision + offset still pass."""
    try:
        datetime(
            int(value[0:4]), int(value[5:7]), int(value[8:10]),
            int(value[11:13]), int(value[14:16]), int(value[17:19]),
        )
    except (ValueError, IndexError):
        return False
    return True


def _in_enum(value, allowed) -> bool:
    """Membership that never raises on an unhashable/non-str value (audit gpt-f3:
    `[] in set` / `{} in set` raises TypeError). Enum values are always strings."""
    return isinstance(value, str) and value in allowed


def _is_iso8601(value) -> bool:
    return (
        isinstance(value, str)
        and bool(_RFC3339_RE.match(value))
        and _occurred_at_in_range(value)
    )


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and len(value) > 0


def _is_date(value) -> bool:
    """True iff value is a calendar date 'YYYY-MM-DD' with a real month/day
    (mirrors _is_iso8601's range check; '2026-13-99' fails). Never raises."""
    if not (isinstance(value, str) and _DATE_RE.match(value)):
        return False
    try:
        datetime(int(value[0:4]), int(value[5:7]), int(value[8:10]))
    except (ValueError, IndexError):
        return False
    return True


def validate_incoming_event(event) -> "tuple[bool, list[str]]":
    """Validate one IncomingEvent dict against the §5.1 contract.

    Returns (ok, errors). ok is True iff errors is empty. Never raises.
    """
    errors: list[str] = []
    if not isinstance(event, dict):
        return False, ["event must be a JSON object"]

    # Unknown top-level keys (additionalProperties:false). Call out StoredEvent-
    # only fields explicitly — the most likely producer mistake.
    for key in event:
        if key in _STORED_ONLY:
            errors.append(
                f"'{key}' is a StoredEvent-only field (AQG-generated at write time); "
                f"an IncomingEvent producer must not send it"
            )
        elif key not in _TOP_KEYS:
            errors.append(f"unknown top-level field '{key}'")

    for key in _REQUIRED_TOP:
        if key not in event:
            errors.append(f"missing required field '{key}'")

    sv = event.get("schema_version")
    if "schema_version" in event and not _in_enum(sv, _SUPPORTED_VERSIONS):
        errors.append(
            f"schema_version must be one of {sorted(_SUPPORTED_VERSIONS)}, got {sv!r}"
        )

    src = event.get("source")
    if "source" in event and not _in_enum(src, _SOURCES):
        errors.append(f"source must be one of {sorted(_SOURCES)}, got {src!r}")

    if "event_id" in event:
        eid = event["event_id"]
        if not _nonempty_str(eid):
            errors.append("event_id must be a non-empty string")
        elif len(eid) > _EVENT_ID_MAXLEN:
            errors.append(
                f"event_id must be at most {_EVENT_ID_MAXLEN} characters, got {len(eid)}"
            )
        elif not _EVENT_ID_RE.match(eid):
            errors.append(
                f"event_id must be '<source>:<run_or_session_id>:<producer_seq>' using only "
                f"[A-Za-z0-9._:-] (filename-safe), got {eid!r}"
            )
        elif isinstance(src, str) and eid.split(":", 1)[0] != src:
            # provenance consistency (audit gpt-f2): the event_id source-prefix
            # must match the top-level source.
            errors.append(
                f"event_id source-prefix {eid.split(':', 1)[0]!r} must match source {src!r}"
            )

    # content_checksum is ADVISORY only — AQG never recomputes/verifies it
    # (event_id is the idempotency key). Type-check only; the schema description
    # matches (audit b5381a7d gpt-f10; Owner 2026-06-01 chose advisory).
    if "content_checksum" in event and not isinstance(event["content_checksum"], str):
        errors.append("content_checksum must be a string")

    if "project" in event and not _valid_project_id(event["project"]):
        errors.append(
            "project must be a safe project_id: [A-Za-z0-9._-] segments with no "
            "'.'/'..'/empty/absolute/backslash (an 'owner/repo' slug) or "
            f"'local:<16-hex>' (got {event.get('project')!r})"
        )

    if "source_ref" in event:
        sref = event["source_ref"]
        if not isinstance(sref, dict):
            errors.append("source_ref must be an object")
        else:
            for key, val in sref.items():
                if key not in _SOURCE_REF_KEYS:
                    errors.append(
                        f"source_ref has unknown key '{key}' (allowed: {sorted(_SOURCE_REF_KEYS)})"
                    )
                elif not isinstance(val, str):
                    # schema types these as string (audit gpt-f1a / gemini-f3).
                    errors.append(f"source_ref.{key} must be a string, got {val!r}")

    if "occurred_at" in event and not _is_iso8601(event["occurred_at"]):
        errors.append(f"occurred_at must be an RFC3339 datetime, got {event.get('occurred_at')!r}")

    kind = event.get("kind")
    if "kind" in event and not _in_enum(kind, _KINDS):
        errors.append(f"kind must be one of {sorted(_KINDS)}, got {kind!r}")

    # Version×kind gate: milestone/decision are 1.1-only (a4 §5.6 capability
    # handshake). A 1.0 event carrying one is a producer that never declared the
    # new contract — reject to dead-letter rather than silently project it.
    if _in_enum(kind, _V11_KINDS) and sv == "1.0":
        errors.append(f"kind {kind!r} requires schema_version '1.1' (undefined in 1.0)")

    # milestone/decision lifecycles (4-state + terminal + re-plan / raised→resolved)
    # depend on STRICT producer_seq ordering, so the §5.1.1 UUID escape hatch does
    # NOT apply: a UUID has no monotonic order and would let a stale action reorder
    # the lifecycle (a3:T1 / acceptance ⑪). Reuse seq.is_uuid (single source).
    eid_for_seq = event.get("event_id")
    if _in_enum(kind, _V11_KINDS) and isinstance(eid_for_seq, str) and is_uuid(producer_seq(eid_for_seq)):
        errors.append(
            f"kind {kind!r} producer_seq must be a monotonic integer/compound, not a "
            f"UUID (event_id {eid_for_seq!r}): its ordered lifecycle needs the stale-event "
            f"protection a UUID disables (§5.1.1 escape hatch excluded)"
        )

    # aqg-hook is documented progress-only (low-fidelity); the projection ignores
    # its non-progress events, so accepting one would write a permanently-dead
    # event into the append-only log. Reject at ingress (audit b5381a7d gpt-f6;
    # Owner 2026-06-01 chose fail-closed). progress + handoff stay allowed.
    if src == "aqg-hook" and _in_enum(kind, _KINDS) and kind not in _AQG_HOOK_KINDS:
        errors.append(
            f"source 'aqg-hook' must not emit kind {kind!r} (aqg-hook is progress-only; "
            f"its non-progress events are ignored by the projection)"
        )

    if "payload" in event:
        payload = event["payload"]
        if not isinstance(payload, dict):
            errors.append("payload must be an object")
        elif kind == "progress":
            errors += _validate_progress(payload)
        elif kind == "defect":
            errors += _validate_defect(payload)
        elif kind == "handoff":
            errors += _validate_handoff(payload)
        elif kind == "milestone":
            errors += _validate_milestone(payload)
        elif kind == "decision":
            errors += _validate_decision(payload)
        # kind invalid/missing → already reported; skip payload-by-kind checks.

    return (len(errors) == 0), errors


def _validate_progress(p: dict) -> "list[str]":
    e: list[str] = []
    for key in p:
        if key not in _PROGRESS_KEYS:
            e.append(f"progress payload unknown key '{key}'")
    if not _in_enum(p.get("phase_event"), _PHASE_EVENTS):
        e.append(f"progress.phase_event must be one of {sorted(_PHASE_EVENTS)}, got {p.get('phase_event')!r}")
    if not _nonempty_str(p.get("title")):
        e.append("progress.title required (non-empty string)")
    for opt in ("detail", "pr_url", "commit_sha"):
        if opt in p and not isinstance(p[opt], str):
            e.append(f"progress.{opt} must be a string")
    return e


def _validate_defect(p: dict) -> "list[str]":
    e: list[str] = []
    for key in p:
        if key == "status":
            e.append(
                "defect payload must NOT contain 'status' — current status is projected "
                "by AQG from the action stream, never stored (append-only purity)"
            )
        elif key not in _DEFECT_KEYS:
            e.append(f"defect payload unknown key '{key}'")
    if not _nonempty_str(p.get("defect_id")):
        e.append("defect.defect_id required (non-empty string)")
    action = p.get("action")
    if not _in_enum(action, _DEFECT_ACTIONS):
        e.append(f"defect.action must be one of {sorted(_DEFECT_ACTIONS)}, got {action!r}")
    if action == "opened":
        if not _nonempty_str(p.get("title")):
            e.append("defect.title required on action=opened")
        if not _in_enum(p.get("severity"), _SEVERITIES):
            e.append(f"defect.severity required on action=opened (one of {sorted(_SEVERITIES)})")
    elif "severity" in p and not _in_enum(p["severity"], _SEVERITIES):
        e.append(f"defect.severity must be one of {sorted(_SEVERITIES)}, got {p['severity']!r}")
    # title (any action): non-empty when present — aligns with schema minLength:1.
    if "title" in p and not _nonempty_str(p["title"]):
        e.append("defect.title must be a non-empty string when present")
    for opt in ("fixed_commit", "verification", "regression_anchor"):
        if opt in p and not isinstance(p[opt], str):
            e.append(f"defect.{opt} must be a string")
    return e


def _validate_handoff(p: dict) -> "list[str]":
    e: list[str] = []
    for key in p:
        if key not in _HANDOFF_KEYS:
            e.append(f"handoff payload unknown key '{key}'")
    if not _nonempty_str(p.get("manual_path")):
        e.append("handoff.manual_path required (non-empty string)")
    if not _in_enum(p.get("generated_by"), _GENERATED_BY):
        e.append(f"handoff.generated_by must be one of {sorted(_GENERATED_BY)}, got {p.get('generated_by')!r}")
    return e


def _validate_milestone(p: dict) -> "list[str]":
    e: list[str] = []
    for key in p:
        if key == "status":
            e.append(
                "milestone payload must NOT contain 'status' — current status is "
                "projected by AQG from the action stream, never stored (a4 §5.2/§5.3)"
            )
        elif key not in _MILESTONE_KEYS:
            e.append(f"milestone payload unknown key '{key}'")
    if not _nonempty_str(p.get("milestone_id")):
        e.append("milestone.milestone_id required (non-empty string)")
    action = p.get("action")
    if not _in_enum(action, _MILESTONE_ACTIONS):
        e.append(f"milestone.action must be one of {sorted(_MILESTONE_ACTIONS)}, got {action!r}")
    # planned carries the business-language fields + the size estimate (a4 §5.2).
    business = ("title", "summary", "key_outcome", "estimated_size")
    if action == "planned":
        for f in business:
            if not _nonempty_str(p.get(f)):
                e.append(f"milestone.{f} required on action=planned (non-empty string)")
    else:
        for f in business:
            if f in p and not _nonempty_str(p[f]):
                e.append(f"milestone.{f} must be a non-empty string when present")
    # completed records the actual completion date (a4 §5.2).
    ad = p.get("actual_date")
    if action == "completed":
        if ad is None:
            e.append("milestone.actual_date required on action=completed (YYYY-MM-DD)")
        elif not _is_date(ad):
            e.append(f"milestone.actual_date must be a date YYYY-MM-DD, got {ad!r}")
    elif "actual_date" in p and not _is_date(ad):
        e.append(f"milestone.actual_date must be a date YYYY-MM-DD, got {ad!r}")
    # optional calendar fields — they do NOT drive progress/health (a4 §5.2 [a4:T]).
    for f in ("planned_start", "target_date"):
        if f in p and not _is_date(p[f]):
            e.append(f"milestone.{f} must be a date YYYY-MM-DD, got {p[f]!r}")
    # date invariant (a4 §5.2): enforced ONLY when both dates are present + valid.
    ps, td = p.get("planned_start"), p.get("target_date")
    if isinstance(ps, str) and isinstance(td, str) and _is_date(ps) and _is_date(td) and ps > td:
        e.append(f"milestone.planned_start ({ps}) must be <= target_date ({td})")
    if "actual_wallclock" in p and not isinstance(p["actual_wallclock"], str):
        e.append("milestone.actual_wallclock must be a string")
    if "replan" in p and not isinstance(p["replan"], bool):
        e.append("milestone.replan must be a boolean")
    for f in ("gate", "phase_label"):
        if f in p and not isinstance(p[f], str):
            e.append(f"milestone.{f} must be a string")
    return e


def _validate_decision(p: dict) -> "list[str]":
    e: list[str] = []
    for key in p:
        if key == "status":
            e.append(
                "decision payload must NOT contain 'status' — current state is "
                "projected by AQG from the action stream (a4 §9)"
            )
        elif key not in _DECISION_KEYS:
            e.append(f"decision payload unknown key '{key}'")
    if not _nonempty_str(p.get("decision_id")):
        e.append("decision.decision_id required (non-empty string)")
    action = p.get("action")
    if not _in_enum(action, _DECISION_ACTIONS):
        e.append(f"decision.action must be one of {sorted(_DECISION_ACTIONS)}, got {action!r}")
    # raised carries the question to put in front of the owner (a4 §9).
    if action == "raised":
        if not _nonempty_str(p.get("question")):
            e.append("decision.question required on action=raised (non-empty string)")
    elif "question" in p and not _nonempty_str(p["question"]):
        e.append("decision.question must be a non-empty string when present")
    if "options" in p:
        opts = p["options"]
        if not (isinstance(opts, list) and all(isinstance(o, str) for o in opts)):
            e.append("decision.options must be a list of strings")
    for f in ("rationale", "blocks"):
        if f in p and not isinstance(p[f], str):
            e.append(f"decision.{f} must be a string")
    return e
