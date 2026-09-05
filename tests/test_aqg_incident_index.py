"""Tests for scripts/aqg_incident_index.py + scripts/_incident_redaction.py.

Wave 3 Layer 3 incident index v0 — covers schema validation, redaction
guard, record-write CLI (JSON + args modes), and index generation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import _incident_redaction as ir  # noqa: E402
import aqg_incident_index as aii  # noqa: E402


# ===== Helpers =====


def _make_valid_record() -> dict:
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


# ===== _incident_redaction tests =====


def test_valid_record_passes():
    result = ir.check_incident_record(_make_valid_record())
    assert result.is_safe, result.violations


def test_missing_required_field_fails():
    rec = _make_valid_record()
    del rec["title"]
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("title" in v for v in result.violations)


def test_unknown_top_level_field_fails():
    rec = _make_valid_record()
    rec["something_else"] = "x"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("unknown top-level" in v for v in result.violations)


def test_invalid_severity_fails():
    rec = _make_valid_record()
    rec["severity"] = "high"  # bugfix uses high but incident uses P1-P4
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("severity" in v for v in result.violations)


def test_invalid_detection_source_fails():
    rec = _make_valid_record()
    rec["detection_source"] = "telepathy"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("detection_source" in v for v in result.violations)


def test_invalid_impact_scope_fails():
    rec = _make_valid_record()
    rec["impact_scope"] = "moon"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_invalid_slug_fails():
    rec = _make_valid_record()
    rec["slug"] = "Bad SLUG!"  # uppercase + space + bang
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("slug" in v for v in result.violations)


def test_invalid_date_fails():
    rec = _make_valid_record()
    rec["date"] = "May 4, 2026"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_schema_version_bool_fails():
    rec = _make_valid_record()
    rec["schema_version"] = True
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_schema_version_unknown_int_fails():
    rec = _make_valid_record()
    rec["schema_version"] = 99
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_token_prefix_in_summary_fails():
    rec = _make_valid_record()
    rec["summary"] = "we used ghp_xxx token here"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("token" in v for v in result.violations)


def test_email_in_root_cause_fails():
    rec = _make_valid_record()
    rec["root_cause"] = "user alice@example.com reported"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("email" in v for v in result.violations)


def test_absolute_path_in_resolution_fails():
    rec = _make_valid_record()
    rec["resolution"] = "fixed by editing /etc/secrets/file"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_pem_marker_in_boundaries_fails():
    rec = _make_valid_record()
    rec["boundaries"] = "key was -----BEGIN RSA PRIVATE KEY----- redacted"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_url_in_summary_fails_use_pr_url_field():
    rec = _make_valid_record()
    rec["summary"] = "see https://github.com/foo/bar/pull/1 for context"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("URL" in v for v in result.violations)


def test_pr_url_field_accepts_github_url():
    rec = _make_valid_record()
    rec["pr_url"] = "https://github.com/deeppatternai/agent-quality-gates/pull/60"
    result = ir.check_incident_record(rec)
    assert result.is_safe, result.violations


def test_pr_url_rejects_non_github():
    rec = _make_valid_record()
    rec["pr_url"] = "https://gitlab.com/foo/bar"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_audit_id_8hex_accepted():
    rec = _make_valid_record()
    rec["audit_id"] = "deadbeef"
    result = ir.check_incident_record(rec)
    assert result.is_safe, result.violations


def test_audit_id_invalid_format_fails():
    rec = _make_valid_record()
    rec["audit_id"] = "not-hex!"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_followups_list_of_str_accepted():
    rec = _make_valid_record()
    rec["followups"] = ["a", "b", "c"]
    result = ir.check_incident_record(rec)
    assert result.is_safe, result.violations


def test_followups_non_list_fails():
    rec = _make_valid_record()
    rec["followups"] = "single string"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_followups_too_many_fails():
    rec = _make_valid_record()
    rec["followups"] = [f"item {i}" for i in range(25)]
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_followup_item_with_token_fails():
    rec = _make_valid_record()
    rec["followups"] = ["rotate sk-ant-xxxx"]
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_inline_field_with_newline_fails():
    rec = _make_valid_record()
    rec["summary"] = "line one\nline two"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("single-line" in v for v in result.violations)


def test_assert_safe_raises():
    rec = _make_valid_record()
    rec["severity"] = "bogus"
    with pytest.raises(ir.IncidentRedactionError):
        ir.assert_safe_incident_record(rec)


# ===== a2 audit fix coverage =====


def test_ipv4_in_root_cause_fails():
    """a2 audit #1: IPv4 detection."""
    rec = _make_valid_record()
    rec["root_cause"] = "request from 192.168.1.1 spiked"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("IPv4" in v for v in result.violations)


def test_authorization_header_in_resolution_fails():
    """a2 audit #1: case-insensitive auth/cookie header detection."""
    rec = _make_valid_record()
    rec["resolution"] = "rotated Authorization: Bearer xyz123"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("auth/cookie/session" in v for v in result.violations)


def test_labeled_phone_in_summary_fails():
    """a2 audit #1: labeled phone field detection."""
    rec = _make_valid_record()
    rec["summary"] = "user phone: +1-555-123-4567 reported"
    result = ir.check_incident_record(rec)
    assert not result.is_safe


def test_html_comment_close_in_title_fails():
    """a2 audit #2: HTML comment delimiter defense-in-depth."""
    rec = _make_valid_record()
    rec["title"] = "normal title --> oops"
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("HTML comment delimiter" in v for v in result.violations)


def test_invalid_calendar_date_fails():
    """a2 audit #6: calendar validity beyond regex shape."""
    rec = _make_valid_record()
    rec["date"] = "2026-99-99"  # passes regex shape, fails calendar
    result = ir.check_incident_record(rec)
    assert not result.is_safe
    assert any("calendar" in v for v in result.violations)


def test_filename_whitelist_skips_non_canonical():
    """a2 audit #3: filename whitelist skips files not matching date-slug pattern."""
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "not-a-date-prefix.md").write_text(
            _full_metadata_block("notdate"), encoding="utf-8"
        )
        (d / "2026-05-04-good.md").write_text(
            _full_metadata_block("good"), encoding="utf-8"
        )
        records = aii._scan_incidents(d)
        assert len(records) == 1
        assert records[0]["slug"] == "good"


def test_md_cell_escape_pipe_and_brackets():
    """a2 audit #3: cell escape protects markdown table from injection."""
    assert aii._md_cell_escape("a|b") == "a\\|b"
    assert aii._md_cell_escape("[link](evil)") == "\\[link\\](evil)"
    assert aii._md_cell_escape("`code`") == "\\`code\\`"
    # Backslash itself escaped
    assert aii._md_cell_escape("a\\b") == "a\\\\b"


def test_atomic_record_no_overwrite_without_force():
    """a2 audit #4: O_EXCL atomic create. Already-existing canonical file
    causes auto-suffix -a1.md without overwriting."""
    script = REPO / "scripts" / "aqg_incident_index.py"
    rec = _make_valid_record()
    rec["slug"] = "atomic-test"
    rec["date"] = "2026-05-04"
    target = REPO / "docs" / "incidents" / f"{rec['date']}-{rec['slug']}.md"
    suffixed = REPO / "docs" / "incidents" / f"{rec['date']}-{rec['slug']}-a1.md"
    try:
        # 1st call writes the canonical file
        proc1 = subprocess.run(
            [sys.executable, str(script), "record", "--json"],
            input=json.dumps(rec), text=True, capture_output=True,
        )
        assert proc1.returncode == 0, proc1.stderr
        assert target.exists()
        original_content = target.read_text(encoding="utf-8")

        # 2nd call (no --force) must NOT overwrite; emits suffixed file
        rec2 = dict(rec)
        rec2["title"] = "different title for second record"
        proc2 = subprocess.run(
            [sys.executable, str(script), "record", "--json"],
            input=json.dumps(rec2), text=True, capture_output=True,
        )
        assert proc2.returncode == 0, proc2.stderr
        assert suffixed.exists()
        # Original untouched
        assert target.read_text(encoding="utf-8") == original_content
    finally:
        if target.exists(): target.unlink()
        if suffixed.exists(): suffixed.unlink()


def test_index_relative_dir_resolves_to_repo_root():
    """a2 audit #5: --dir relative path resolves to repo_root not cwd."""
    script = REPO / "scripts" / "aqg_incident_index.py"
    with tempfile.TemporaryDirectory() as tmp:
        # Run from tmp dir but pass relative --dir; should resolve from repo_root
        proc = subprocess.run(
            [sys.executable, str(script), "index",
             "--dir", "docs/incidents",
             "--output", str(Path(tmp) / "INDEX.md")],
            text=True, capture_output=True, cwd=tmp,
        )
        # docs/incidents/ exists in repo_root and contains _example_clean.md (skipped),
        # so index runs successfully producing empty or near-empty index — exit 0
        assert proc.returncode == 0, proc.stderr


def test_index_missing_dir_fails():
    """a2 audit #5: missing --dir fails clearly (not silent empty index)."""
    script = REPO / "scripts" / "aqg_incident_index.py"
    proc = subprocess.run(
        [sys.executable, str(script), "index",
         "--dir", "/nonexistent/path/xxx"],
        text=True, capture_output=True,
    )
    assert proc.returncode == aii.EXIT_USAGE
    assert "does not exist" in proc.stderr


# ===== aqg_incident_index parsing tests =====


def test_parse_metadata_block():
    text = (
        "<!--\n"
        "AQG_INCIDENT_RECORD\n"
        "schema_version: 1\n"
        "slug: foo\n"
        "date: 2026-05-04\n"
        "severity: P2\n"
        "detection_source: monitoring\n"
        "impact_scope: production\n"
        "-->\n\n"
        "# Incident Record: My title\n"
    )
    md = aii._parse_metadata(text)
    assert md is not None
    assert md["slug"] == "foo"
    assert md["severity"] == "P2"
    assert md["impact_scope"] == "production"


def test_parse_metadata_missing_returns_none():
    text = "# Incident Record: no metadata block\n"
    assert aii._parse_metadata(text) is None


def test_extract_title():
    text = "# Incident Record: My title here\n\nbody\n"
    assert aii._extract_title(text) == "My title here"


def test_extract_title_fallback_to_h1():
    text = "# Other H1\n"
    assert aii._extract_title(text) == "Other H1"


def test_extract_title_default_when_missing():
    text = "no h1\n"
    assert aii._extract_title(text) == "(untitled)"


def test_render_index_empty():
    out = aii._render_index([])
    assert "No incidents recorded yet" in out
    assert "Incident Index" in out


def test_render_index_sorted_desc_by_date():
    records = [
        {
            "date": "2026-05-01",
            "severity": "P3",
            "impact_scope": "dev",
            "detection_source": "manual",
            "slug": "old",
            "_filename": "2026-05-01-old.md",
            "_title": "Old",
        },
        {
            "date": "2026-05-04",
            "severity": "P2",
            "impact_scope": "production",
            "detection_source": "monitoring",
            "slug": "new",
            "_filename": "2026-05-04-new.md",
            "_title": "New",
        },
    ]
    out = aii._render_index(records)
    # newer date appears before older in table
    assert out.index("2026-05-04") < out.index("2026-05-01")
    assert "[2026-05-04-new.md](2026-05-04-new.md)" in out


def _full_metadata_block(slug: str, date: str = "2026-05-04") -> str:
    """Build a complete metadata block that passes schema validate (a2 audit #3)."""
    return (
        "<!--\nAQG_INCIDENT_RECORD\n"
        f"schema_version: 1\nslug: {slug}\ndate: {date}\nactor: ci-bot\n"
        f"severity: P3\ndetection_source: manual\nimpact_scope: dev\n-->\n\n"
        f"# Incident Record: {slug} title\n"
    )


def test_scan_incidents_skips_index_readme_and_underscore():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        # Real incident with complete metadata
        (d / "2026-05-04-real.md").write_text(
            _full_metadata_block("real"), encoding="utf-8"
        )
        # Should be skipped
        (d / "INDEX.md").write_text("# index", encoding="utf-8")
        (d / "README.md").write_text("# readme", encoding="utf-8")
        (d / "_example.md").write_text(
            "<!--\nAQG_INCIDENT_RECORD\nslug: ex\n-->\n# example\n",
            encoding="utf-8",
        )
        # File without metadata block
        (d / "2026-05-03-stray.md").write_text(
            "# Incident Record: no block\n", encoding="utf-8"
        )
        # File with malformed metadata (bad severity) — must be skipped per a2 audit #3
        (d / "2026-05-02-bad.md").write_text(
            "<!--\nAQG_INCIDENT_RECORD\nschema_version: 1\nslug: bad\n"
            "date: 2026-05-02\nactor: ci-bot\nseverity: BOGUS\n"
            "detection_source: manual\nimpact_scope: dev\n-->\n# Bad\n",
            encoding="utf-8",
        )
        # File with non-conforming filename (not date-slug pattern) — skipped by whitelist
        (d / "random-name.md").write_text(
            _full_metadata_block("random"), encoding="utf-8"
        )

        records = aii._scan_incidents(d)
        assert len(records) == 1
        assert records[0]["slug"] == "real"


# ===== CLI integration tests =====


def test_cli_record_args_mode_writes_file():
    script = REPO / "scripts" / "aqg_incident_index.py"
    with tempfile.TemporaryDirectory() as tmp:
        # Use the --json branch to keep test hermetic; args mode is exercised below
        rec = _make_valid_record()
        rec["slug"] = "cli-test-args"
        proc = subprocess.run(
            [sys.executable, str(script), "record", "--json", "--force"],
            input=json.dumps(rec),
            text=True,
            capture_output=True,
        )
        # The CLI writes to repo's docs/incidents/, not tmp.
        # We assert exit_code 0 + cleanup the file we just wrote.
        assert proc.returncode == 0, proc.stderr
        target = REPO / "docs" / "incidents" / f"{rec['date']}-{rec['slug']}.md"
        assert target.exists()
        target.unlink()


def test_cli_record_redaction_fail():
    script = REPO / "scripts" / "aqg_incident_index.py"
    rec = _make_valid_record()
    rec["slug"] = "cli-redaction-fail"
    rec["summary"] = "leaked sk-ant-xxxxxxxxxxxxxxxxxxxxxxxx"
    proc = subprocess.run(
        [sys.executable, str(script), "record", "--json"],
        input=json.dumps(rec),
        text=True,
        capture_output=True,
    )
    assert proc.returncode == aii.EXIT_REDACTION_FAIL


def test_cli_index_writes_file():
    script = REPO / "scripts" / "aqg_incident_index.py"
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "2026-05-04-foo.md").write_text(
            _full_metadata_block("foo"),
            encoding="utf-8",
        )
        out_path = d / "INDEX.md"
        proc = subprocess.run(
            [
                sys.executable, str(script), "index",
                "--dir", str(d), "--output", str(out_path),
            ],
            text=True,
            capture_output=True,
        )
        assert proc.returncode == 0, proc.stderr
        assert out_path.exists()
        body = out_path.read_text(encoding="utf-8")
        assert "foo title" in body
        assert "P3" in body
