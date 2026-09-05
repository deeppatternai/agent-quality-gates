#!/usr/bin/env python3
"""Incident record schema + redaction guard (Wave 3 Layer 3 incident index v0).

Implements ENGINEERING_FRAMEWORK.md §2 (review-time analyzer with an
incident markdown index) + §5 (Layer 3).

Prevents the LLM, when auto-filling records, from leaking secret / customer /
raw path or injecting markdown.
Schema follows the bugfix_record pattern but with an incident-specific field set:
P1-P4 severity / detection_source / impact_scope / followups.

API:
    assert_safe_incident_record(record: dict) -> None  # raise IncidentRedactionError
    check_incident_record(record: dict) -> IncidentRedactionResult

No third-party dependencies; stdlib only (+ AQG _redaction_common).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# L1 three-audit 4f0c48c0 root-cause A: single leak-scan codepath. All prose /
# inline leak detection now delegates to _redaction_common.scan_leaks so the
# incident guard can no longer drift from the other guards (was a false-NEGATIVE
# bypass source — findings #5/#16). Migrating to scan_leaks(multiline=True) also
# fixes #8: the multiline fields (root_cause/resolution/boundaries) previously
# had NO markdown-heading-line guard; scan_leaks supplies it for free.
from _redaction_common import scan_identifier, scan_leaks


# ===== Schema field allowlist =====

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version",
    "slug",
    "date",
    "actor",
    "title",
    "severity",
    "detection_source",
    "impact_scope",
    "summary",
    "root_cause",
    "resolution",
    "followups",
    "boundaries",
})
OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "audit_id",
    "pr_url",
    "marker",
})
ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL


# ===== Value constraints =====

ACCEPTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
ACTOR_RE = re.compile(r"^[a-z][a-z0-9._-]{0,40}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
AUDIT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._/-]{1,200}$")

ALLOWED_SEVERITIES: frozenset[str] = frozenset({"P1", "P2", "P3", "P4"})
ALLOWED_DETECTION_SOURCES: frozenset[str] = frozenset({
    "monitoring",
    "user_report",
    "scheduled_check",
    "audit",
    "manual",
})
ALLOWED_IMPACT_SCOPES: frozenset[str] = frozenset({
    "production",
    "staging",
    "internal",
    "dev",
})
ALLOWED_MARKERS: frozenset[str] = frozenset({
    "auto-generated-by-aqg_incident_index",
    "manual",
})

# Length limits
MAX_TITLE_LEN = 100
MAX_INLINE_LEN = 200
MAX_PROSE_LEN = 800
MAX_SLUG_LEN = 80
MAX_FOLLOWUPS_COUNT = 20
MAX_FOLLOWUP_ITEM_LEN = 300

INLINE_FIELDS: frozenset[str] = frozenset({"title", "summary"})
MULTILINE_FIELDS: frozenset[str] = frozenset({"root_cause", "resolution", "boundaries"})


# ===== Redaction =====
# All leak patterns (token / path / PII / URL / base64 / IP / auth-header / phone
# / HTML-comment / control-char / markdown-heading) live in
# _redaction_common.scan_leaks now (root-cause A). Nothing local — this is what
# stops the incident guard drifting from bugfix/orchestration/etc.


@dataclass(frozen=True)
class IncidentRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class IncidentRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "incident record redaction violation (no detail)"
        lines = [
            f"incident record redaction violation ({len(self.violations)} issue(s)):"
        ]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _check_followups(value: Any, violations: list[str]) -> None:
    """followups: list[str] (each <= MAX_FOLLOWUP_ITEM_LEN, single-line, redacted).

    Each item is an inline field, so leak-scan with multiline=False (rejects
    newline/CR + control chars in addition to the token/path/PII/etc. belt).
    """
    if not isinstance(value, list):
        violations.append(
            f"followups: must be list, got {type(value).__name__}"
        )
        return
    if len(value) > MAX_FOLLOWUPS_COUNT:
        violations.append(
            f"followups: too many items ({len(value)} > {MAX_FOLLOWUPS_COUNT})"
        )
    for i, item in enumerate(value):
        if not isinstance(item, str):
            violations.append(
                f"followups[{i}]: must be str, got {type(item).__name__}"
            )
            continue
        if len(item) > MAX_FOLLOWUP_ITEM_LEN:
            violations.append(
                f"followups[{i}]: too long ({len(item)} > {MAX_FOLLOWUP_ITEM_LEN})"
            )
        scan_leaks(f"followups[{i}]", item, violations, multiline=False)


def check_incident_record(record: Mapping[str, Any]) -> IncidentRedactionResult:
    """Schema + redaction check; returns is_safe + violations."""
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return IncidentRedactionResult(
            is_safe=False,
            violations=(
                f"<root>: must be a mapping, got {type(record).__name__}",
            ),
        )

    # Unknown top-level keys (#6): JSON mode lets the caller control key names, so
    # a key could itself carry a secret — report only the COUNT, never echo the
    # key text. (parity with _metrics_redaction "key names suppressed".)
    unknown_top = sum(1 for key in record if key not in ALLOWED_TOP_LEVEL)
    if unknown_top:
        violations.append(
            f"<root>: {unknown_top} unknown top-level field(s) (not in allowlist); "
            f"key names suppressed to avoid leak"
        )

    # Required keys
    for key in REQUIRED_TOP_LEVEL:
        if key not in record:
            violations.append(f"missing required field: {key}")

    # schema_version (strict int, accepted set)
    sv = record.get("schema_version")
    if sv is not None:
        if not isinstance(sv, int) or isinstance(sv, bool):
            violations.append(
                f"schema_version: must be int, got {type(sv).__name__}"
            )
        elif sv not in ACCEPTED_SCHEMA_VERSIONS:
            violations.append(
                f"schema_version: {sv} not in {sorted(ACCEPTED_SCHEMA_VERSIONS)}"
            )

    # slug
    slug = record.get("slug")
    if isinstance(slug, str):
        if not SLUG_RE.match(slug):
            violations.append(f"slug: must match {SLUG_RE.pattern}")
        else:
            # (#N2) slug fits the charset but a token-shape value (npm_<lc>) still
            # passes SLUG_RE — route through scan_identifier like the other guards.
            scan_identifier("slug", slug, violations)
    elif slug is not None:
        violations.append(f"slug: must be str, got {type(slug).__name__}")

    # date — shape + actual calendar validity (a2 audit #6)
    date_v = record.get("date")
    if isinstance(date_v, str):
        if not DATE_RE.match(date_v):
            violations.append("date: must be YYYY-MM-DD")
        else:
            try:
                datetime.date.fromisoformat(date_v)
            except ValueError as exc:
                violations.append(f"date: invalid calendar date ({exc})")
    elif date_v is not None:
        violations.append(f"date: must be str, got {type(date_v).__name__}")

    # actor
    actor = record.get("actor")
    if isinstance(actor, str):
        if not ACTOR_RE.match(actor):
            violations.append(f"actor: must match {ACTOR_RE.pattern}")
        else:
            # round-3 ebd87bef: actor identifier must pass the token-shape scan.
            scan_identifier("actor", actor, violations)
    elif actor is not None:
        violations.append(f"actor: must be str, got {type(actor).__name__}")

    # severity (#7: isinstance(str) guard BEFORE `in frozenset` — a non-str /
    # unhashable value like a dict/list would otherwise raise TypeError instead
    # of failing closed with a violation).
    sev = record.get("severity")
    if sev is not None:
        if not isinstance(sev, str):
            violations.append(f"severity: must be str, got {type(sev).__name__}")
        elif sev not in ALLOWED_SEVERITIES:
            violations.append(
                f"severity: must be one of {sorted(ALLOWED_SEVERITIES)}"
            )

    # detection_source (#7 isinstance guard)
    ds = record.get("detection_source")
    if ds is not None:
        if not isinstance(ds, str):
            violations.append(
                f"detection_source: must be str, got {type(ds).__name__}"
            )
        elif ds not in ALLOWED_DETECTION_SOURCES:
            violations.append(
                f"detection_source: must be one of "
                f"{sorted(ALLOWED_DETECTION_SOURCES)}"
            )

    # impact_scope (#7 isinstance guard)
    isc = record.get("impact_scope")
    if isc is not None:
        if not isinstance(isc, str):
            violations.append(
                f"impact_scope: must be str, got {type(isc).__name__}"
            )
        elif isc not in ALLOWED_IMPACT_SCOPES:
            violations.append(
                f"impact_scope: must be one of "
                f"{sorted(ALLOWED_IMPACT_SCOPES)}"
            )

    # title (inline, max 100) — multiline=False rejects newline/CR + control chars
    title = record.get("title")
    if isinstance(title, str):
        if len(title) > MAX_TITLE_LEN:
            violations.append(f"title: too long ({len(title)} > {MAX_TITLE_LEN})")
        scan_leaks("title", title, violations, multiline=False)
    elif title is not None:
        violations.append(f"title: must be str, got {type(title).__name__}")

    # summary (inline, max MAX_INLINE_LEN)
    summary = record.get("summary")
    if isinstance(summary, str):
        if len(summary) > MAX_INLINE_LEN:
            violations.append(
                f"summary: too long ({len(summary)} > {MAX_INLINE_LEN})"
            )
        scan_leaks("summary", summary, violations, multiline=False)
    elif summary is not None:
        violations.append(f"summary: must be str, got {type(summary).__name__}")

    # root_cause / resolution / boundaries (multiline, max MAX_PROSE_LEN).
    # multiline=True allows \n \r \t but adds the markdown-heading guard (#8) the
    # local incident impl previously lacked.
    for fld in MULTILINE_FIELDS:
        v = record.get(fld)
        if isinstance(v, str):
            if len(v) > MAX_PROSE_LEN:
                violations.append(
                    f"{fld}: too long ({len(v)} > {MAX_PROSE_LEN})"
                )
            scan_leaks(fld, v, violations, multiline=True)
        elif v is not None:
            violations.append(f"{fld}: must be str, got {type(v).__name__}")

    # followups
    if "followups" in record:
        _check_followups(record["followups"], violations)

    # audit_id (optional)
    aid = record.get("audit_id")
    if aid not in (None, ""):
        if not isinstance(aid, str) or not AUDIT_ID_RE.match(aid):
            violations.append(f"audit_id: must match {AUDIT_ID_RE.pattern} or empty")

    # pr_url (optional)
    pu = record.get("pr_url")
    if pu not in (None, ""):
        if not isinstance(pu, str) or not PR_URL_RE.match(pu):
            violations.append(
                f"pr_url: must match {PR_URL_RE.pattern} or empty"
            )

    # marker (optional) (#7 isinstance guard before `in frozenset`)
    mk = record.get("marker")
    if mk is not None:
        if not isinstance(mk, str):
            violations.append(f"marker: must be str, got {type(mk).__name__}")
        elif mk not in ALLOWED_MARKERS:
            violations.append(
                f"marker: must be one of {sorted(ALLOWED_MARKERS)}"
            )

    return IncidentRedactionResult(
        is_safe=not violations, violations=tuple(violations),
    )


def assert_safe_incident_record(record: Mapping[str, Any]) -> None:
    """Raise IncidentRedactionError if record is unsafe."""
    result = check_incident_record(record)
    if not result.is_safe:
        raise IncidentRedactionError(list(result.violations))


def self_test() -> int:
    """Offline self-test — schema + every leak class + the audit 4f0c48c0 regressions.

    Parity with the other redaction guards (this was finding #17: incident was the
    only guard without a self_test()/__main__).
    """
    valid = {
        "schema_version": 1,
        "slug": "test-incident",
        "date": "2026-05-04",
        "actor": "claude",
        "title": "Test incident title",
        "severity": "P3",
        "detection_source": "manual",
        "impact_scope": "dev",
        "summary": "single line summary",
        "root_cause": "explanation of root cause",
        "resolution": "what was done",
        "followups": ["item one", "item two"],
        "boundaries": "production not touched",
        "audit_id": "",
        "pr_url": "",
        "marker": "auto-generated-by-aqg_incident_index",
    }
    assert check_incident_record(valid).is_safe, check_incident_record(valid).violations

    # missing required field
    bad = dict(valid); del bad["title"]
    assert not check_incident_record(bad).is_safe, "missing title must reject"

    # unknown top-level key — and the key name must NOT be echoed (#6)
    bad = dict(valid); bad["sk_secret_key"] = "x"
    res = check_incident_record(bad)
    assert not res.is_safe, "unknown key must reject"
    assert all("sk_secret_key" not in v for v in res.violations), "key name leaked (#6)"

    # ----- denylist leak classes (each must reject) -----
    leak_cases = [
        ("root_cause", "user alice@example.com reported"),                  # email
        ("resolution", "fixed by editing /etc/secrets/file"),               # abs path
        ("root_cause", "request from 192.168.1.1 spiked"),                  # IPv4
        ("resolution", "rotated Authorization: Bearer xyz123"),             # auth header
        ("summary", "user phone: +1-555-123-4567 reported"),               # phone (PII)
        ("summary", "we used ghp_FAKE_TOKEN_SHAPE_EXAMPLE here"),           # token prefix
        ("boundaries", "key was -----BEGIN RSA PRIVATE KEY----- redacted"), # PEM
    ]
    for fld, val in leak_cases:
        bad = dict(valid); bad[fld] = val
        assert not check_incident_record(bad).is_safe, f"{fld} leak not caught: {val!r}"

    # markdown heading line in a multiline field (#8 — incident lacked this before)
    bad = dict(valid); bad["root_cause"] = "line1\n## fake heading\nbad content"
    res = check_incident_record(bad)
    assert not res.is_safe, "markdown heading must reject"
    assert any("markdown heading" in v for v in res.violations), "md heading msg (#8)"

    # ----- #7: non-str enum members must NOT crash (TypeError), fail closed -----
    for fld in ("severity", "detection_source", "impact_scope", "marker"):
        bad = dict(valid); bad[fld] = {"unhashable": "dict"}  # unhashable → `in` TypeError
        res = check_incident_record(bad)  # must not raise
        assert not res.is_safe, f"{fld} non-str must reject"
        assert any(fld in v and "must be str" in v for v in res.violations), \
            f"{fld} should report must-be-str, got {res.violations}"

    # value never echoed in the violation message
    secret = "gh" + "p_" + "S3CR3T" + "a" * 30
    bad = dict(valid); bad["summary"] = f"leaked {secret} token"
    for v in check_incident_record(bad).violations:
        assert "S3CR3T" not in v, f"value leaked in violation: {v}"

    # raise API
    try:
        assert_safe_incident_record({"schema_version": 99})
        raise AssertionError("should have raised")
    except IncidentRedactionError:
        pass

    print("OK: _incident_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
