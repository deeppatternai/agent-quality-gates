#!/usr/bin/env python3
"""Bugfix record schema + redaction guard (Wave 1 P0 #6).

Implemented per ENGINEERING_FRAMEWORK.md §3 (Build/Adapt/Defer: bugfix record
template is a Build capability) + triple-audit (audit_id c489fdf5) 11 accepted findings.

Prevents the LLM, when auto-filling records, from leaking a secret / customer /
raw path or injecting markdown.

API:
    assert_safe_bugfix_record(record: dict) -> None  # raise BugfixRedactionError
    check_bugfix_record(record: dict) -> BugfixRedactionResult

No third-party dependencies; stdlib only.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Mapping

# L1 three-audit 4f0c48c0 root-cause A: single leak-scan codepath. All prose /
# identifier / user-key leak detection now delegates to _redaction_common so the
# bugfix guard can no longer drift from the other guards (was a false-NEGATIVE
# bypass source — findings #3/#5/#16).
from _redaction_common import scan_identifier, scan_leaks


# ===== Schema field allowlist =====

REQUIRED_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version",
    "slug",
    "date",
    "actor",
    "title",
    "affected_area",
    "severity",
    "backward_compatible",
    "symptom",
    "root_cause",
    "fix",
    "verification",
    "regression_coverage",
    "boundaries",
})
OPTIONAL_TOP_LEVEL: frozenset[str] = frozenset({
    "taxonomy",
    "regression",
    "audit_id",
    "pr_url",
    "marker",
    "files_touched",
})
ALLOWED_TOP_LEVEL: frozenset[str] = REQUIRED_TOP_LEVEL | OPTIONAL_TOP_LEVEL


# ===== Value constraints =====

ACCEPTED_SCHEMA_VERSIONS: frozenset[int] = frozenset({1})

# slug: first char [a-z0-9], then allows . _ -, total length 1-80 (o3 #6 accepted)
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")

# actor: free-form lowercase / digit / . _ - (gemini #4 + o3 #7 accepted)
ACTOR_RE = re.compile(r"^[a-z][a-z0-9._-]{0,40}$")

# date: YYYY-MM-DD
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# audit_id: 8-hex (matches de_audit return id)
AUDIT_ID_RE = re.compile(r"^[0-9a-f]{8}$")

# pr_url: must be https://github.com/... with safe trailing chars (o3-style restrict)
PR_URL_RE = re.compile(r"^https://github\.com/[A-Za-z0-9._/-]{1,200}$")

ALLOWED_SEVERITIES: frozenset[str] = frozenset({"low", "medium", "high"})
ALLOWED_BACKWARD_COMPAT: frozenset[str] = frozenset({"yes", "no", "migration needed"})
ALLOWED_TAXONOMY: frozenset[str] = frozenset({
    "configuration",
    "interface",
    "data",
    "algorithm",
    "performance",
    "security",
    "typo",
})
ALLOWED_MARKERS: frozenset[str] = frozenset({"auto-generated-by-bugfix_record", "manual"})

# Length limits
MAX_TITLE_LEN = 100
MAX_AFFECTED_AREA_LEN = 200
MAX_INLINE_LEN = 200  # symptom / regression_coverage etc. single-line
MAX_PROSE_LEN = 800   # root_cause / fix / verification / boundaries multi-line OK
MAX_SLUG_LEN = 80
MAX_FILES_TOUCHED = 30
MAX_FILE_PATH_LEN = 200

# Inline fields (NEVER newline / control char) — prevent markdown injection (gpt-5.5 #5)
INLINE_FIELDS: frozenset[str] = frozenset({
    "title",
    "affected_area",
    "symptom",
    "regression_coverage",
})
# Multi-line OK fields (but still enforce redaction)
MULTILINE_FIELDS: frozenset[str] = frozenset({
    "root_cause",
    "fix",
    "verification",
    "boundaries",
})

# Column-0 markdown block openers that corrupt a git-committed record's structure
# when an UNFENCED inline field (symptom / regression_coverage) renders them at the
# start of a line: an ATX heading (`#`..`######` then space/EOL) or a fenced-code
# opener (``` / ~~~) that swallows the following template sections. HTML comments +
# URLs are already rejected by scan_leaks; the low-impact single-line markers
# (>, -, *, |, 1.) are deliberately allowed to avoid false-positives on legitimate
# prose. (audit c200595b convergent: heading-only was incomplete.)
_MD_BLOCK_START_RE = re.compile(r"(?:#{1,6}(?:\s|$)|`{3,}|~{3,})")


def _starts_markdown_block(value: str) -> bool:
    """True if value rendered at column 0 opens a structure-corrupting markdown
    block (ATX heading or fenced-code opener). ATX-precise (`#` then space/EOL) so a
    non-heading like ``#42 reproduces`` is allowed; fail-closed on any leading
    whitespace via lstrip()."""
    return bool(_MD_BLOCK_START_RE.match(value.lstrip()))


# ===== Redaction =====
# All leak patterns (token / path / PII / URL / base64 / control-char / markdown
# heading) live in _redaction_common.scan_leaks now (root-cause A). Nothing local.


@dataclass(frozen=True)
class BugfixRedactionResult:
    is_safe: bool
    violations: tuple[str, ...] = field(default_factory=tuple)


class BugfixRedactionError(Exception):
    def __init__(self, violations: list[str]):
        self.violations = tuple(violations)
        super().__init__(self._fmt())

    def _fmt(self) -> str:
        if not self.violations:
            return "bugfix record redaction violation (no detail)"
        lines = [f"bugfix record redaction violation ({len(self.violations)} issue(s)):"]
        for v in self.violations:
            lines.append(f"  - {v}")
        return "\n".join(lines)


def _check_files_touched(value: Any, violations: list[str]) -> None:
    """files_touched: list[str] each must be project-relative, no .. / abs / unc."""
    if value is None:
        return
    if not isinstance(value, list):
        violations.append("files_touched: must be list")
        return
    if len(value) > MAX_FILES_TOUCHED:
        violations.append(f"files_touched: too many entries ({len(value)} > {MAX_FILES_TOUCHED})")
    for i, p in enumerate(value):
        prefix = f"files_touched[{i}]"
        if not isinstance(p, str):
            violations.append(f"{prefix}: must be str")
            continue
        if not p or len(p) > MAX_FILE_PATH_LEN:
            violations.append(f"{prefix}: empty or too long")
            continue
        # Reject absolute / // / trailing /
        if p.startswith("/") or p.startswith("\\"):
            violations.append(f"{prefix}: absolute path not allowed (use project-relative)")
        if p.startswith("//"):
            violations.append(f"{prefix}: UNC-style path not allowed")
        if os.path.isabs(p):
            violations.append(f"{prefix}: os.path.isabs reports absolute")
        if p.endswith("/") or p.endswith("\\"):
            violations.append(f"{prefix}: trailing slash (must be file not dir)")
        # Reject .. parent traversal
        if ".." in p.split("/") or ".." in p.split("\\"):
            violations.append(f"{prefix}: contains '..' parent traversal")
        # Reject Windows drive
        if re.match(r"^[A-Za-z]:[/\\]", p):
            violations.append(f"{prefix}: Windows drive path not allowed")
        # Restrict charset
        if not re.match(r"^[A-Za-z0-9._/\-]+$", p):
            violations.append(f"{prefix}: contains disallowed characters")
        # triple-audit 4f0c48c0 #3: a path that satisfies the charset can still BE a
        # token-shape value (e.g. "npm_<lowercase>"); run the identifier leak scan.
        scan_identifier(prefix, p, violations)


def check_bugfix_record(record: Mapping[str, Any]) -> BugfixRedactionResult:
    violations: list[str] = []

    if not isinstance(record, Mapping):
        return BugfixRedactionResult(
            is_safe=False,
            violations=(f"<root>: must be mapping, got {type(record).__name__}",),
        )

    # Unknown top-level — triple-audit 4f0c48c0 #6: do not echo key text. Under JSON
    # mode the key name is user-controlled and may itself contain a secret; only
    # report the count (parity with _metrics_redaction).
    unknown_top = sum(1 for key in record.keys() if key not in ALLOWED_TOP_LEVEL)
    if unknown_top:
        violations.append(
            f"<root>: {unknown_top} unknown top-level field(s) (not in allowlist); "
            f"key names suppressed"
        )

    # Required
    for req in REQUIRED_TOP_LEVEL:
        if req not in record:
            violations.append(f"<root>.{req}: missing required field")

    # schema_version
    sv = record.get("schema_version")
    if sv is not None:
        if not isinstance(sv, int) or isinstance(sv, bool):
            violations.append(f"schema_version: must be int, got {type(sv).__name__}")
        elif sv not in ACCEPTED_SCHEMA_VERSIONS:
            violations.append(f"schema_version: must be {sorted(ACCEPTED_SCHEMA_VERSIONS)}")

    # slug
    slug = record.get("slug")
    if slug is not None:
        if not isinstance(slug, str) or not SLUG_RE.match(slug):
            violations.append("slug: must match ^[a-z0-9][a-z0-9._-]{0,79}$")
        else:
            # triple-audit 4f0c48c0 #3: a token-shape slug (e.g. "npm_<lowercase>") fits
            # SLUG_RE but is a secret. Run the identifier leak scan on the valid slug.
            scan_identifier("slug", slug, violations)

    # actor
    actor = record.get("actor")
    if actor is not None:
        if not isinstance(actor, str) or not ACTOR_RE.match(actor):
            violations.append("actor: must match ^[a-z][a-z0-9._-]{0,40}$")
        else:
            # round-3 ebd87bef: actor is a user-controlled identifier — a
            # token-shape value fits ACTOR_RE, so run the identifier leak scan.
            scan_identifier("actor", actor, violations)

    # date
    date = record.get("date")
    if date is not None:
        if not isinstance(date, str) or not DATE_RE.match(date):
            violations.append("date: must match YYYY-MM-DD")

    # severity — triple-audit 4f0c48c0 #7: isinstance(str) guard. A non-str (list/dict)
    # in `in frozenset` raises TypeError because it is unhashable (fail-open crash);
    # fail-CLOSED switches to append.
    sev = record.get("severity")
    if sev is not None:
        if not isinstance(sev, str):
            violations.append(f"severity: must be str, got {type(sev).__name__}")
        elif sev not in ALLOWED_SEVERITIES:
            violations.append(f"severity: must be one of {sorted(ALLOWED_SEVERITIES)}")

    # backward_compatible — triple-audit 4f0c48c0 #7: same as severity, isinstance(str) guard prevents TypeError.
    bc = record.get("backward_compatible")
    if bc is not None:
        if not isinstance(bc, str):
            violations.append(f"backward_compatible: must be str, got {type(bc).__name__}")
        elif bc not in ALLOWED_BACKWARD_COMPAT:
            violations.append(f"backward_compatible: must be one of {sorted(ALLOWED_BACKWARD_COMPAT)}")

    # taxonomy (list[str])
    tax = record.get("taxonomy")
    if tax is not None:
        if not isinstance(tax, list):
            violations.append("taxonomy: must be list[str]")
        else:
            for i, t in enumerate(tax):
                if not isinstance(t, str) or t not in ALLOWED_TAXONOMY:
                    violations.append(f"taxonomy[{i}]: must be one of {sorted(ALLOWED_TAXONOMY)}")

    # regression bool
    reg = record.get("regression")
    if reg is not None and not isinstance(reg, bool):
        violations.append("regression: must be bool")

    # audit_id
    aid = record.get("audit_id")
    if aid is not None and aid != "":
        if not isinstance(aid, str) or not AUDIT_ID_RE.match(aid):
            violations.append("audit_id: must be 8-lowercase-hex if set")

    # pr_url
    pu = record.get("pr_url")
    if pu is not None and pu != "":
        if not isinstance(pu, str) or not PR_URL_RE.match(pu):
            violations.append("pr_url: must match ^https://github.com/...")

    # marker
    marker = record.get("marker")
    if marker is not None and (not isinstance(marker, str) or marker not in ALLOWED_MARKERS):
        violations.append(f"marker: must be one of {sorted(ALLOWED_MARKERS)}")

    # Post-impl dual-audit gpt-5.5 #1 (critical): required text fields are forced to
    # isinstance(str). A JSON dict/list/number in these fields would bypass redaction
    # → renders a dict repr containing a token. A missing field is already caught
    # earlier in the 'missing required' stage; here we only catch the wrong type.
    REQUIRED_STR_FIELDS = (
        "title", "affected_area", "symptom", "root_cause", "fix",
        "verification", "regression_coverage", "boundaries",
    )
    for fname in REQUIRED_STR_FIELDS:
        v = record.get(fname)
        if v is not None and not isinstance(v, str):
            violations.append(
                f"{fname}: must be str, got {type(v).__name__} (prevents dict/list from bypassing redaction)"
            )

    # Inline fields — triple-audit 4f0c48c0 #5/#16: delegate to scan_leaks(multiline=False).
    # scan_leaks now owns the newline/CR + control-char checks (the old _check_inline's
    # job), so it is no longer called separately to avoid duplicate single-line
    # violation reports.
    title = record.get("title")
    if isinstance(title, str):
        if len(title) > MAX_TITLE_LEN:
            violations.append(f"title: too long ({len(title)} > {MAX_TITLE_LEN})")
        scan_leaks("title", title, violations, multiline=False)

    aa = record.get("affected_area")
    if isinstance(aa, str):
        if len(aa) > MAX_AFFECTED_AREA_LEN:
            violations.append(f"affected_area: too long ({len(aa)} > {MAX_AFFECTED_AREA_LEN})")
        scan_leaks("affected_area", aa, violations, multiline=False)

    for fname in ("symptom", "regression_coverage"):
        v = record.get(fname)
        if isinstance(v, str):
            if len(v) > MAX_INLINE_LEN:
                violations.append(f"{fname}: too long ({len(v)} > {MAX_INLINE_LEN})")
            scan_leaks(fname, v, violations, multiline=False)
            # WB-03 (audit c200595b): symptom/regression_coverage render at column 0
            # in the git-committed record (unlike title/affected_area, which
            # interpolate mid-line, and unlike the fenced multiline fields). A
            # single-line ATX heading OR a fenced-code opener at column 0 corrupts
            # the rendered document structure; scan_leaks' heading guard is gated on
            # multiline=True and never covered fences — so guard both here.
            if _starts_markdown_block(v):
                violations.append(
                    f"{fname}: starts with a column-0 markdown block "
                    f"(heading or code fence); injection into git-committed record"
                )

    # Multi-line prose — delegate to scan_leaks(multiline=True). multiline=True already
    # includes the line-start markdown heading check (#8 parity), so the local heading
    # loop was removed.
    for fname in MULTILINE_FIELDS:
        v = record.get(fname)
        if isinstance(v, str):
            if len(v) > MAX_PROSE_LEN:
                violations.append(f"{fname}: too long ({len(v)} > {MAX_PROSE_LEN})")
            scan_leaks(fname, v, violations, multiline=True)

    # files_touched
    _check_files_touched(record.get("files_touched"), violations)

    return BugfixRedactionResult(is_safe=not violations, violations=tuple(violations))


def assert_safe_bugfix_record(record: Mapping[str, Any]) -> None:
    result = check_bugfix_record(record)
    if not result.is_safe:
        raise BugfixRedactionError(list(result.violations))


def self_test() -> int:
    valid = {
        "schema_version": 1,
        "slug": "test-bug-fix",
        "date": "2026-05-03",
        "actor": "claude",
        "title": "Test bug fix",
        "affected_area": "scripts / docs",
        "severity": "medium",
        "backward_compatible": "yes",
        "symptom": "Test failed under condition X",
        "root_cause": "Off-by-one in iteration boundary",
        "fix": "Adjusted loop bound; added regression test",
        "verification": "pytest tests/ -q -> 217 passed",
        "regression_coverage": "tests/test_x.py::TestX::test_off_by_one",
        "boundaries": "no production touched; secrets unchanged; Owner not needed",
        "taxonomy": ["algorithm"],
        "regression": False,
        "marker": "auto-generated-by-bugfix_record",
    }
    assert check_bugfix_record(valid).is_safe

    # missing required
    bad = dict(valid); del bad["slug"]
    assert not check_bugfix_record(bad).is_safe

    # unknown top-level
    bad = dict(valid); bad["leak_field"] = "anything"
    assert not check_bugfix_record(bad).is_safe

    # slug first char dot
    bad = dict(valid); bad["slug"] = ".secret"
    assert not check_bugfix_record(bad).is_safe

    # slug ..
    bad = dict(valid); bad["slug"] = "../etc"
    assert not check_bugfix_record(bad).is_safe

    # slug w/ dot OK
    ok = dict(valid); ok["slug"] = "json.parse-error"
    assert check_bugfix_record(ok).is_safe, check_bugfix_record(ok).violations

    # actor open enum
    ok = dict(valid); ok["actor"] = "gpt-5.5"
    assert check_bugfix_record(ok).is_safe

    # taxonomy multi-label
    ok = dict(valid); ok["taxonomy"] = ["algorithm", "performance"]
    assert check_bugfix_record(ok).is_safe

    # taxonomy invalid
    bad = dict(valid); bad["taxonomy"] = ["unknown_class"]
    assert not check_bugfix_record(bad).is_safe

    # inline newline
    bad = dict(valid); bad["symptom"] = "line1\n## fake heading"
    assert not check_bugfix_record(bad).is_safe

    # absolute path in prose
    bad = dict(valid); bad["fix"] = "modified /Users/example/secret.txt"
    assert not check_bugfix_record(bad).is_safe

    # email leak
    bad = dict(valid); bad["root_cause"] = "User test@example.com hit error"
    assert not check_bugfix_record(bad).is_safe

    # JWT
    bad = dict(valid); bad["fix"] = "token eyJABCDEFGHIJKLMNOP.eyJABCDEFGHIJ.signature"
    assert not check_bugfix_record(bad).is_safe

    # files_touched abs
    bad = dict(valid); bad["files_touched"] = ["/usr/bin/git"]
    assert not check_bugfix_record(bad).is_safe

    # files_touched ..
    bad = dict(valid); bad["files_touched"] = ["../etc/passwd"]
    assert not check_bugfix_record(bad).is_safe

    # files_touched OK
    ok = dict(valid); ok["files_touched"] = ["scripts/foo.py", "tests/test_foo.py"]
    assert check_bugfix_record(ok).is_safe

    # pr_url valid github
    ok = dict(valid); ok["pr_url"] = "https://github.com/deeppatternai/agent-quality-gates/pull/13"
    assert check_bugfix_record(ok).is_safe

    # pr_url non-github reject
    bad = dict(valid); bad["pr_url"] = "https://example.com/pr/1"
    assert not check_bugfix_record(bad).is_safe

    # raise API
    try:
        assert_safe_bugfix_record({"schema_version": 99})
        raise AssertionError("should have raised")
    except BugfixRedactionError:
        pass

    print("OK: _bugfix_redaction self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
