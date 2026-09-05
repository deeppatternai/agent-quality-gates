"""Tests for scripts/_incident_redaction.py — incident record schema + leak guard.

New (L1 three-round audit 4f0c48c0 finding #17): _incident_redaction was previously the only
redaction module with no dedicated test file and no self_test(); its IP / auth-header /
phone / markdown checks were only covered indirectly by test_aqg_incident_index.py.

Each test is annotated with the audit finding it corresponds to. Covers check_incident_record /
assert_safe_incident_record for: valid pass; rejection of each redaction class; calendar validation of date;
followups cap; markdown heading reject (#8); non-str enum does not crash (#7);
unknown key not echoed (#6); violation messages do not echo the raw value.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _incident_redaction as ir  # noqa: E402


def _make_valid() -> dict:
    return {
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


class TestSchema:
    def test_valid_passes(self):
        r = ir.check_incident_record(_make_valid())
        assert r.is_safe, r.violations

    def test_missing_required_rejected(self):
        s = _make_valid(); del s["title"]
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("title" in v for v in r.violations)

    def test_unknown_top_level_rejected(self):
        s = _make_valid(); s["something_else"] = "x"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("unknown top-level" in v for v in r.violations)

    def test_schema_version_unknown_int_rejected(self):
        s = _make_valid(); s["schema_version"] = 99
        assert not ir.check_incident_record(s).is_safe

    def test_schema_version_bool_rejected(self):
        s = _make_valid(); s["schema_version"] = True
        assert not ir.check_incident_record(s).is_safe

    def test_root_must_be_mapping(self):
        r = ir.check_incident_record(["not", "a", "mapping"])  # type: ignore[arg-type]
        assert not r.is_safe


class TestUnknownKeyNotEchoed:
    """#6: unknown top-level key name must NOT be echoed (JSON-mode caller
    controls key names → a key could itself carry a secret)."""

    def test_count_reported_not_key_text(self):
        s = _make_valid(); s["sk_secret_key_name"] = "x"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        joined = " ".join(r.violations)
        assert "sk_secret_key_name" not in joined
        assert "suppressed" in joined

    def test_multiple_unknown_keys_counted(self):
        s = _make_valid(); s["foo"] = 1; s["bar"] = 2
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("2 unknown top-level field(s)" in v for v in r.violations)


class TestEnumValidation:
    @pytest.mark.parametrize("field,bad", [
        ("severity", "high"),          # incident uses P1-P4, not low/med/high
        ("detection_source", "telepathy"),
        ("impact_scope", "moon"),
        ("marker", "hand-written"),
    ])
    def test_invalid_enum_rejected(self, field, bad):
        s = _make_valid(); s[field] = bad
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any(field in v for v in r.violations)

    @pytest.mark.parametrize("sev", ["P1", "P2", "P3", "P4"])
    def test_valid_severities(self, sev):
        s = _make_valid(); s["severity"] = sev
        assert ir.check_incident_record(s).is_safe


class TestNonStrEnumNoCrash:
    """#7: non-str (esp. unhashable) enum members must fail closed with a
    violation, NEVER raise TypeError from `value not in frozenset`."""

    @pytest.mark.parametrize("field", [
        "severity", "detection_source", "impact_scope", "marker",
    ])
    def test_unhashable_dict_returns_result(self, field):
        s = _make_valid(); s[field] = {"unhashable": "dict"}
        r = ir.check_incident_record(s)  # must not raise
        assert not r.is_safe
        assert any(field in v and "must be str" in v for v in r.violations)

    @pytest.mark.parametrize("field", [
        "severity", "detection_source", "impact_scope", "marker",
    ])
    def test_list_value_returns_result(self, field):
        s = _make_valid(); s[field] = ["a", "b"]
        r = ir.check_incident_record(s)  # must not raise
        assert not r.is_safe

    def test_int_severity_returns_result(self):
        s = _make_valid(); s["severity"] = 1
        r = ir.check_incident_record(s)  # must not raise
        assert not r.is_safe
        assert any("severity" in v and "must be str" in v for v in r.violations)


class TestSlug:
    @pytest.mark.parametrize("slug", ["test-incident", "json.parse-error", "9bug"])
    def test_valid_slugs(self, slug):
        s = _make_valid(); s["slug"] = slug
        assert ir.check_incident_record(s).is_safe, ir.check_incident_record(s).violations

    @pytest.mark.parametrize("slug", ["Bad SLUG!", ".secret", "../etc", ""])
    def test_invalid_slugs(self, slug):
        s = _make_valid(); s["slug"] = slug
        assert not ir.check_incident_record(s).is_safe


class TestDate:
    def test_valid_date_ok(self):
        s = _make_valid(); s["date"] = "2026-05-04"
        assert ir.check_incident_record(s).is_safe

    def test_bad_shape_rejected(self):
        s = _make_valid(); s["date"] = "May 4, 2026"
        assert not ir.check_incident_record(s).is_safe

    def test_invalid_calendar_date_rejected(self):
        """a2 audit #6: shape passes but calendar invalid (month/day 99)."""
        s = _make_valid(); s["date"] = "2026-99-99"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("calendar" in v for v in r.violations)

    def test_feb_30_rejected(self):
        s = _make_valid(); s["date"] = "2026-02-30"
        assert not ir.check_incident_record(s).is_safe


class TestProseRedaction:
    """Each leak class via the unified _redaction_common.scan_leaks belt."""

    @pytest.mark.parametrize("field,value,needle", [
        ("root_cause", "user alice@example.com reported", "email"),
        ("resolution", "fixed by editing /etc/secrets/file", "absolute POSIX path"),
        ("boundaries", "key was -----BEGIN RSA PRIVATE KEY----- here", "PEM"),
        ("summary", "see https://github.com/foo/bar/pull/1", "URL"),
        ("root_cause", "request from 192.168.1.1 spiked", "IPv4"),
        ("resolution", "rotated Authorization: Bearer xyz123", "auth/cookie/session"),
        ("summary", "user phone: +1-555-123-4567 called", "phone"),
        ("title", "normal title --> oops", "HTML comment delimiter"),
        ("root_cause", "long base64 " + "A" * 50, "base64"),
    ])
    def test_leak_pattern_rejected(self, field, value, needle):
        s = _make_valid(); s[field] = value
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any(needle in v for v in r.violations), r.violations

    @pytest.mark.parametrize("field", ["title", "summary", "root_cause", "boundaries"])
    def test_token_prefix_rejected(self, field):
        s = _make_valid(); s[field] = "we used ghp_FAKE_TOKEN_SHAPE_EXAMPLE here"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("token" in v for v in r.violations)


class TestInlineFields:
    """title / summary are inline (multiline=False) → newline / CR rejected."""

    @pytest.mark.parametrize("field", ["title", "summary"])
    def test_newline_rejected(self, field):
        s = _make_valid(); s[field] = "line one\nline two"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("single-line" in v for v in r.violations)

    @pytest.mark.parametrize("field", ["title", "summary"])
    def test_carriage_return_rejected(self, field):
        s = _make_valid(); s[field] = "line1\r\nbad"
        assert not ir.check_incident_record(s).is_safe


class TestMultilineMarkdownHeading:
    """#8: incident's multiline fields previously had NO markdown-heading guard;
    migrating to scan_leaks(multiline=True) supplies it. Benign multiline ok."""

    @pytest.mark.parametrize("field", ["root_cause", "resolution", "boundaries"])
    def test_heading_line_rejected(self, field):
        s = _make_valid(); s[field] = "line1\n## fake heading\nmore"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("markdown heading" in v for v in r.violations)

    def test_single_hash_heading_rejected(self):
        s = _make_valid(); s["resolution"] = "ok\n# top-level fake"
        assert not ir.check_incident_record(s).is_safe

    def test_indented_heading_rejected(self):
        s = _make_valid(); s["boundaries"] = "good\n   ## sneaky indented"
        assert not ir.check_incident_record(s).is_safe

    def test_benign_multiline_ok(self):
        s = _make_valid()
        s["root_cause"] = "first line of cause\nsecond line continues normally"
        assert ir.check_incident_record(s).is_safe, ir.check_incident_record(s).violations


class TestFollowups:
    def test_list_of_str_ok(self):
        s = _make_valid(); s["followups"] = ["a", "b", "c"]
        assert ir.check_incident_record(s).is_safe

    def test_non_list_rejected(self):
        s = _make_valid(); s["followups"] = "single string"
        assert not ir.check_incident_record(s).is_safe

    def test_too_many_rejected(self):
        s = _make_valid(); s["followups"] = [f"item {i}" for i in range(25)]
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("too many" in v for v in r.violations)

    def test_non_str_item_rejected(self):
        s = _make_valid(); s["followups"] = ["ok", 12345]
        assert not ir.check_incident_record(s).is_safe

    def test_item_with_token_rejected(self):
        s = _make_valid(); s["followups"] = ["rotate sk-ant-FAKE_xxxx"]
        assert not ir.check_incident_record(s).is_safe

    def test_item_with_newline_rejected(self):
        s = _make_valid(); s["followups"] = ["line1\n## injected"]
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert any("single-line" in v for v in r.violations)


class TestOptionalFields:
    def test_audit_id_8hex_ok(self):
        s = _make_valid(); s["audit_id"] = "4f0c48c0"
        assert ir.check_incident_record(s).is_safe, ir.check_incident_record(s).violations

    def test_audit_id_invalid_rejected(self):
        s = _make_valid(); s["audit_id"] = "not-hex!"
        assert not ir.check_incident_record(s).is_safe

    def test_pr_url_github_ok(self):
        s = _make_valid()
        s["pr_url"] = "https://github.com/deeppatternai/agent-quality-gates/pull/60"
        assert ir.check_incident_record(s).is_safe

    def test_pr_url_non_github_rejected(self):
        s = _make_valid(); s["pr_url"] = "https://gitlab.com/foo/bar"
        assert not ir.check_incident_record(s).is_safe


class TestRaiseAPI:
    def test_assert_safe_passes(self):
        ir.assert_safe_incident_record(_make_valid())

    def test_assert_safe_raises(self):
        s = _make_valid(); s["severity"] = "bogus"
        with pytest.raises(ir.IncidentRedactionError):
            ir.assert_safe_incident_record(s)


class TestNoRawValueInViolations:
    """Violation messages must not echo the raw matched value (prevents secondary leak)."""

    def test_email_not_echoed(self):
        s = _make_valid(); s["root_cause"] = "user secret@private.example hit"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert "secret@private.example" not in " ".join(r.violations)

    def test_token_not_echoed(self):
        secret = "gh" + "p_" + "S3CR3T" + "a" * 30
        s = _make_valid(); s["summary"] = f"leaked {secret}"
        r = ir.check_incident_record(s)
        assert not r.is_safe
        assert "S3CR3T" not in " ".join(r.violations)


def test_module_self_test_passes():
    """The module's own self_test() (#17) must pass when invoked directly."""
    assert ir.self_test() == 0
