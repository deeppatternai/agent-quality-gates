"""Tests for scripts/_bugfix_redaction.py — bugfix record schema + leak guard.

Each test is annotated with the accepted finding it corresponds to from the three-round audit (audit_id c489fdf5).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import _bugfix_redaction as br  # noqa: E402


def _make_valid() -> dict:
    return {
        "schema_version": 1,
        "slug": "test-fix",
        "date": "2026-05-03",
        "actor": "claude",
        "title": "Test Fix",
        "affected_area": "scripts",
        "severity": "medium",
        "backward_compatible": "yes",
        "symptom": "Failed under condition X",
        "root_cause": "Off-by-one in loop boundary",
        "fix": "Adjusted loop bound; added regression test",
        "verification": "pytest tests/ -q -> all passed",
        "regression_coverage": "tests/test_x.py::test_off_by_one",
        "boundaries": "no production touched; secrets unchanged",
        "taxonomy": ["algorithm"],
        "regression": False,
        "marker": "auto-generated-by-bugfix_record",
    }


class TestSchema:
    def test_valid_passes(self):
        assert br.check_bugfix_record(_make_valid()).is_safe

    def test_unknown_top_level_rejected(self):
        s = _make_valid(); s["leak_field"] = "x"
        assert not br.check_bugfix_record(s).is_safe

    def test_missing_required(self):
        s = _make_valid(); del s["slug"]
        assert not br.check_bugfix_record(s).is_safe

    def test_schema_version_unknown(self):
        s = _make_valid(); s["schema_version"] = 99
        assert not br.check_bugfix_record(s).is_safe


class TestSlug:
    """o3 #6: the slug regex allows a dot, but the first character is not a dot."""

    @pytest.mark.parametrize("slug", [
        "test-fix",
        "json.parse-error",
        "v0.2.4-fix",
        "fix_typo",
        "abc",
        "9bug",
    ])
    def test_valid_slugs(self, slug):
        s = _make_valid(); s["slug"] = slug
        assert br.check_bugfix_record(s).is_safe, br.check_bugfix_record(s).violations

    @pytest.mark.parametrize("slug", [
        ".secret",
        "..parent",
        "../etc",
        "../../etc",
        "Test-Fix",  # uppercase
        "fix with space",
        "x" * 81,
        "",
    ])
    def test_invalid_slugs(self, slug):
        s = _make_valid(); s["slug"] = slug
        assert not br.check_bugfix_record(s).is_safe


class TestActor:
    """gemini #4 + o3 #7: actor accepts a model name."""

    @pytest.mark.parametrize("actor", [
        "claude", "codex", "gpt-5.5", "gemini", "o3", "human", "ci-bot", "llama-v4",
    ])
    def test_valid_actors(self, actor):
        s = _make_valid(); s["actor"] = actor
        assert br.check_bugfix_record(s).is_safe

    @pytest.mark.parametrize("actor", [
        "Claude",  # uppercase
        "5gpt",    # leading digit
        "",
        "x" * 50,
    ])
    def test_invalid_actors(self, actor):
        s = _make_valid(); s["actor"] = actor
        assert not br.check_bugfix_record(s).is_safe


class TestTaxonomy:
    """gpt-5.5 #7 + gemini #5 + o3 #5: taxonomy multi-label list."""

    def test_empty_list_ok(self):
        s = _make_valid(); s["taxonomy"] = []
        assert br.check_bugfix_record(s).is_safe

    def test_multi_label_ok(self):
        s = _make_valid(); s["taxonomy"] = ["algorithm", "performance", "security"]
        assert br.check_bugfix_record(s).is_safe

    def test_invalid_label_rejected(self):
        s = _make_valid(); s["taxonomy"] = ["unknown_class"]
        assert not br.check_bugfix_record(s).is_safe

    def test_must_be_list(self):
        s = _make_valid(); s["taxonomy"] = "algorithm"
        assert not br.check_bugfix_record(s).is_safe


class TestInlineFieldsNoNewline:
    """gpt-5.5 #5: inline fields reject newlines (markdown injection guard)."""

    @pytest.mark.parametrize("field", [
        "title", "affected_area", "symptom", "regression_coverage",
    ])
    def test_newline_in_inline_rejected(self, field):
        s = _make_valid(); s[field] = "line1\n## fake heading"
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("single-line" in v for v in result.violations)

    @pytest.mark.parametrize("field", [
        "title", "affected_area", "symptom", "regression_coverage",
    ])
    def test_carriage_return_rejected(self, field):
        s = _make_valid(); s[field] = "line1\r\nbad"
        assert not br.check_bugfix_record(s).is_safe


class TestProseRedaction:
    """gpt-5.5 #4 + o3 #3: prose fields reject abs path / URL / email / token / JWT / PEM."""

    @pytest.mark.parametrize("payload", [
        ("fix", "modified /Users/example/secret.txt"),
        ("root_cause", "User test@example.com hit error"),
        ("fix", "see https://internal.private/log"),
        ("fix", "token eyJABCDEFGHIJKLMNOP.eyJABCDEFGHIJ.signature"),
        ("fix", "key -----BEGIN RSA PRIVATE KEY-----"),
        ("fix", "C:\\Users\\admin\\secret.log"),
        ("fix", "share \\\\server\\path"),
        ("root_cause", "ghp_FAKE_TOKEN_SHAPE_EXAMPLE"),
        ("fix", "long base64 " + "A" * 50),
    ])
    def test_leak_pattern_rejected(self, payload):
        field, value = payload
        s = _make_valid(); s[field] = value
        assert not br.check_bugfix_record(s).is_safe


class TestFilesTouched:
    """gpt-5.5 #3 + gemini #1 + o3 #2: files_touched path traversal."""

    def test_relative_paths_ok(self):
        s = _make_valid(); s["files_touched"] = ["scripts/foo.py", "tests/test_foo.py"]
        assert br.check_bugfix_record(s).is_safe

    @pytest.mark.parametrize("path", [
        "/usr/bin/git",
        "//unc/path",
        "../etc/passwd",
        "C:\\Windows\\sys",
        "scripts/",  # trailing slash
        "scripts/../etc",
        "scripts/foo bar.py",  # space disallowed
    ])
    def test_unsafe_path_rejected(self, path):
        s = _make_valid(); s["files_touched"] = [path]
        assert not br.check_bugfix_record(s).is_safe

    def test_too_many_files_rejected(self):
        s = _make_valid(); s["files_touched"] = [f"f{i}.py" for i in range(50)]
        assert not br.check_bugfix_record(s).is_safe


class TestPrUrl:
    def test_github_pr_url_ok(self):
        s = _make_valid(); s["pr_url"] = "https://github.com/deeppatternai/agent-quality-gates/pull/13"
        assert br.check_bugfix_record(s).is_safe

    def test_non_github_rejected(self):
        s = _make_valid(); s["pr_url"] = "https://example.com/pr/1"
        assert not br.check_bugfix_record(s).is_safe

    def test_empty_ok(self):
        s = _make_valid(); s["pr_url"] = ""
        assert br.check_bugfix_record(s).is_safe


class TestAuditId:
    def test_valid_8hex_ok(self):
        s = _make_valid(); s["audit_id"] = "c489fdf5"
        assert br.check_bugfix_record(s).is_safe

    def test_uppercase_rejected(self):
        s = _make_valid(); s["audit_id"] = "C489FDF5"
        assert not br.check_bugfix_record(s).is_safe

    def test_wrong_length_rejected(self):
        s = _make_valid(); s["audit_id"] = "c489fdf"
        assert not br.check_bugfix_record(s).is_safe


class TestRaiseAPI:
    def test_assert_safe_passes(self):
        br.assert_safe_bugfix_record(_make_valid())

    def test_assert_safe_raises(self):
        with pytest.raises(br.BugfixRedactionError):
            br.assert_safe_bugfix_record({"schema_version": 99})


class TestPostImplFixes:
    """Post-impl two-round audit (audit_id b8ce699b) regression tests for 6 accepted findings."""

    def test_non_string_text_field_rejected(self):
        """gpt-5.5 #1 (critical): a JSON dict/list in fields like fix bypasses redaction."""
        s = _make_valid()
        s["fix"] = {"token": "ghp_LEAK_xxx"}
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("must be str" in v for v in result.violations)

    def test_non_string_list_in_root_cause_rejected(self):
        s = _make_valid()
        s["root_cause"] = ["item1", "ghp_token_in_list"]
        assert not br.check_bugfix_record(s).is_safe

    def test_non_string_int_in_symptom_rejected(self):
        s = _make_valid()
        s["symptom"] = 12345
        assert not br.check_bugfix_record(s).is_safe

    @pytest.mark.parametrize("payload", [
        ("root_cause", "we replaced token sk-ant-FAKE_xxx in code"),
        ("fix", "before failing on ghp_FAKE_TOKEN_SHAPE we used"),
        ("verification", "no AKIA prefix anywhere... wait AKIAIOSFODNN7EXAMPLE"),
        ("boundaries", "audit catch xoxb-fake-slack mid-prose"),
    ])
    def test_token_mid_string_caught(self, payload):
        """gpt-5.5 #2 + gemini #1 (convergent critical): a token embedded in prose is also caught."""
        field, value = payload
        s = _make_valid(); s[field] = value
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("token-shape prefix" in v for v in result.violations)

    def test_markdown_heading_line_in_multiline_rejected(self):
        """gpt-5.5 #3 + gemini #2 (convergent major): multiline fields reject a leading ^# heading."""
        s = _make_valid()
        s["root_cause"] = "line1\n## fake heading\nbad content"
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("markdown heading" in v for v in result.violations)

    def test_indented_heading_also_rejected(self):
        s = _make_valid()
        s["fix"] = "good\n   ## sneaky indented"
        assert not br.check_bugfix_record(s).is_safe

    def test_single_hash_heading_rejected(self):
        s = _make_valid()
        s["verification"] = "ok\n# top-level fake"
        assert not br.check_bugfix_record(s).is_safe


class TestNoRawValueInViolations:
    """The violation description itself must not echo the raw value (prevents secondary leak)."""

    def test_email_not_echoed_in_violation_msg(self):
        s = _make_valid(); s["root_cause"] = "user secret@private.example hit"
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        all_msgs = " ".join(result.violations)
        assert "secret@private.example" not in all_msgs


class TestThreeAuditL1Fixes:
    """Three-round audit (audit_id 4f0c48c0) L1 redaction-engine false-negative bypass fixes.

    Root cause A: the bugfix guard delegates to the single _redaction_common leak-scan codepath.
    Each test corresponds to a finding in the adjudication table.
    """

    def test_token_shape_slug_rejected(self):
        """#3: a token-shape slug can pass SLUG_RE (npm_<lowercase>) but is a secret -> reject."""
        s = _make_valid(); s["slug"] = "npm_" + "a" * 34
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("token-shape" in v for v in result.violations)

    def test_punctuation_prefixed_abs_path_rejected(self):
        """#4/#5: an absolute path immediately preceded by punctuation (a quote) — the old (?:^|\\s) boundary missed it; the helper now uses [^\\w./-] to catch it."""
        s = _make_valid(); s["fix"] = 'edited file="/etc/shadow" x'
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        assert any("absolute POSIX path" in v for v in result.violations)

    def test_non_string_severity_no_crash_and_unsafe(self):
        """#7: when severity is a list, `not in frozenset` would raise TypeError; the isinstance guard fails closed."""
        s = _make_valid(); s["severity"] = ["high"]
        result = br.check_bugfix_record(s)  # must not raise TypeError
        assert result.is_safe is False
        assert any("severity: must be str" in v for v in result.violations)

    def test_non_string_backward_compatible_no_crash(self):
        """#7: backward_compatible is guarded the same way (a dict is not hashable)."""
        s = _make_valid(); s["backward_compatible"] = {"x": 1}
        result = br.check_bugfix_record(s)  # must not raise TypeError
        assert result.is_safe is False
        assert any("backward_compatible: must be str" in v for v in result.violations)

    def test_unknown_key_text_not_echoed(self):
        """#6: an unknown top-level key name (user-controlled, may contain a secret) must not appear in violation messages."""
        leak_key = "ghp_" + "x" * 36  # token-shape key name
        s = _make_valid(); s[leak_key] = "anything"
        result = br.check_bugfix_record(s)
        assert not result.is_safe
        all_msgs = " ".join(result.violations)
        assert leak_key not in all_msgs
        assert any("key names suppressed" in v for v in result.violations)
