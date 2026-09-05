"""Tests for scripts/bugfix_record.py — bugfix record CLI + writer.

Each test is annotated with the accepted finding from the three-round audit
(audit_id c489fdf5) it corresponds to.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import bugfix_record as br  # noqa: E402


REPO = Path(__file__).resolve().parent.parent


def _make_valid_record() -> dict:
    return {
        "schema_version": 1,
        "slug": "test-bug",
        "date": "2026-05-03",
        "actor": "claude",
        "title": "Test Bug",
        "affected_area": "scripts",
        "severity": "medium",
        "backward_compatible": "yes",
        "symptom": "Failed condition X",
        "root_cause": "off-by-one boundary",
        "fix": "adjust loop bound",
        "verification": "pytest passed",
        "regression_coverage": "tests/test_x.py",
        "boundaries": "no prod touched",
        "taxonomy": ["algorithm"],
        "regression": False,
        "marker": "auto-generated-by-bugfix_record",
    }


def _make_fake_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    (repo / "templates").mkdir(parents=True)
    (repo / "VERSION").write_text("0.0.0")
    return repo


# ============================================================
# Markdown render
# ============================================================


class TestRender:
    def test_render_includes_all_required(self):
        text = br._render_markdown(_make_valid_record())
        assert "# Bug Fix Record: Test Bug" in text
        assert "## Symptom" in text
        assert "## Root Cause" in text
        assert "## Fix" in text
        assert "## Verification" in text
        assert "## Regression Coverage" in text
        assert "## Boundaries" in text

    def test_metadata_block_present(self):
        """gpt-5.5 #6: metadata block (schema_version + marker + audit_id) machine-readable."""
        rec = _make_valid_record(); rec["audit_id"] = "deadbeef"
        text = br._render_markdown(rec)
        assert "AQG_BUGFIX_RECORD" in text
        assert "schema_version: 1" in text
        assert "marker: auto-generated-by-bugfix_record" in text
        assert "audit_id: deadbeef" in text

    def test_files_touched_section(self):
        rec = _make_valid_record(); rec["files_touched"] = ["scripts/a.py", "tests/test_a.py"]
        text = br._render_markdown(rec)
        assert "## Files touched" in text
        assert "scripts/a.py" in text


# ============================================================
# Write path: fails if exists, -aN suffix
# ============================================================


class TestWritePath:
    def test_write_creates_file(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        rc, path = br._write_record(_make_valid_record(), repo_root=repo)
        assert rc == br.EXIT_OK
        assert path.exists()

    def test_existing_file_auto_suffix(self, tmp_path):
        """gpt-5.5 #2 + gemini #2 + o3 #1 (3-auditor convergent): no silent overwrite."""
        repo = _make_fake_repo(tmp_path)
        rc1, p1 = br._write_record(_make_valid_record(), repo_root=repo)
        rc2, p2 = br._write_record(_make_valid_record(), repo_root=repo)
        assert rc1 == rc2 == br.EXIT_OK
        assert p1 != p2
        assert p1.exists() and p2.exists()
        assert p2.name.endswith("-a2.md")

    def test_force_overwrites(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        rec1 = _make_valid_record(); rec1["title"] = "First"
        rec2 = _make_valid_record(); rec2["title"] = "Second"
        rc1, p1 = br._write_record(rec1, repo_root=repo)
        rc2, p2 = br._write_record(rec2, repo_root=repo, force=True)
        assert rc1 == rc2 == br.EXIT_OK
        assert p1 == p2
        assert "Second" in p1.read_text()

    def test_redaction_fail_returns_error(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        rec = _make_valid_record(); rec["fix"] = "modified /Users/example/secret"
        rc, _ = br._write_record(rec, repo_root=repo)
        assert rc == br.EXIT_REDACTION_FAIL

    def test_path_stays_under_bugfix_dir(self, tmp_path):
        """gpt-5.5 #3 + gemini #1 + o3 #2: must not escape docs/bugfixes/"""
        repo = _make_fake_repo(tmp_path)
        rec = _make_valid_record()
        rc, path = br._write_record(rec, repo_root=repo)
        assert rc == br.EXIT_OK
        assert (repo / "docs" / "bugfixes") in path.parents


# ============================================================
# CLI: --json XOR field args (mutex)
# ============================================================


def _run_cli(*args, stdin: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO / "scripts" / "bugfix_record.py"), *args],
        input=stdin, text=True, capture_output=True, timeout=15, check=False,
    )


class TestCLIMutex:
    """o3 #4 + gemini #3 (2-auditor): --json XOR field args."""

    def test_json_with_field_args_rejected(self):
        proc = _run_cli("--json", "--slug", "x", stdin="{}")
        assert proc.returncode == br.EXIT_USAGE
        assert "mutually exclusive" in proc.stderr

    def test_no_json_no_args_rejected(self):
        proc = _run_cli()
        assert proc.returncode == br.EXIT_USAGE


class TestCLIJsonMode:
    def test_valid_json(self, tmp_path):
        # Run from repo (uses real docs/bugfixes/) — clean up after
        rec = _make_valid_record(); rec["slug"] = "cli-json-smoke-test"
        path_in_repo = REPO / "docs" / "bugfixes" / f"{rec['date']}-{rec['slug']}.md"
        path_in_repo.unlink(missing_ok=True)
        try:
            proc = _run_cli("--json", stdin=json.dumps(rec))
            assert proc.returncode == br.EXIT_OK, proc.stderr
            assert path_in_repo.exists()
        finally:
            path_in_repo.unlink(missing_ok=True)
            # clean up -a2 and similar suffixes
            for p in (REPO / "docs" / "bugfixes").glob(f"{rec['date']}-{rec['slug']}-a*.md"):
                p.unlink()

    def test_invalid_json_stdin(self):
        proc = _run_cli("--json", stdin="{not json")
        assert proc.returncode == br.EXIT_USAGE

    def test_json_array_rejected(self):
        proc = _run_cli("--json", stdin="[1,2,3]")
        assert proc.returncode == br.EXIT_USAGE


class TestCLIArgsMode:
    def test_missing_required_fields(self):
        proc = _run_cli("--slug", "test")
        assert proc.returncode == br.EXIT_USAGE
        assert "missing required" in proc.stderr


# ============================================================
# Defaults
# ============================================================


class TestPostImplFixes:
    """Post-impl two-round audit (audit_id b8ce699b) 6 accepted findings regression."""

    def test_atomic_create_race_safe(self, tmp_path):
        """gpt-5.5 #4: atomic exclusive creation — pre-existing file correctly retries the suffix."""
        repo = _make_fake_repo(tmp_path)
        target = repo / "docs" / "bugfixes"
        target.mkdir(parents=True, exist_ok=True)
        # place a file beforehand (simulating a race-existing file)
        pre = target / "2026-05-03-test-bug.md"
        pre.write_text("pre-existing content")
        rc, p = br._write_record(_make_valid_record(), repo_root=repo)
        assert rc == br.EXIT_OK
        # the original pre file is not overwritten
        assert pre.read_text() == "pre-existing content"
        # the new path is -a2 (suffix starts at a2)
        assert p.name == "2026-05-03-test-bug-a2.md"

    def test_template_rel_constant_removed(self):
        """gpt-5.5 #5: TEMPLATE_REL removed to avoid confusion."""
        assert not hasattr(br, "TEMPLATE_REL")

    def test_multiline_fields_rendered_in_fence(self):
        """gpt-5.5 #3 + gemini #2 (convergent): multiline fields isolated in a fenced code block."""
        rec = _make_valid_record()
        rec["root_cause"] = "rc-line-A\nrc-line-B"
        rec["fix"] = "fix-line"
        rec["verification"] = "verify-line"
        rec["boundaries"] = "bound-line"
        text = br._render_markdown(rec)
        # all multiline fields should sit inside ```text ... ```
        for marker in ["rc-line-A", "fix-line", "verify-line", "bound-line"]:
            # the line preceding the marker should contain ```text
            idx = text.find(marker)
            assert idx != -1
            preceding = text[:idx]
            # the nearest fence marker
            last_fence = preceding.rfind("```")
            assert last_fence != -1
            assert "```text" in preceding[last_fence:]

    def test_render_with_internal_backticks_uses_alt_fence(self):
        """gpt-5.5 #3 corner: use ~~~~ as an alternate fence when a field itself contains ```."""
        rec = _make_valid_record()
        rec["fix"] = "code: ```inline```"
        text = br._render_markdown(rec)
        assert "~~~~text" in text  # alt fence is used


class TestCLIMutexExtra:
    """gpt-5.5 #6: --json XOR --date/--actor/--regression is also caught."""

    @pytest.mark.parametrize("extra", [
        ["--date", "2026-05-03"],
        ["--actor", "gpt-5.5"],
        ["--regression"],
    ])
    def test_json_with_default_field_args_rejected(self, extra):
        proc = _run_cli("--json", *extra, stdin="{}")
        assert proc.returncode == br.EXIT_USAGE
        assert "mutually exclusive" in proc.stderr


class TestDefaults:
    def test_date_default_today(self, tmp_path):
        from datetime import datetime, timezone
        repo = _make_fake_repo(tmp_path)
        rec = _make_valid_record(); del rec["date"]
        rc, path = br._write_record(rec, repo_root=repo)
        assert rc == br.EXIT_OK
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        assert today in path.name

    def test_marker_default(self, tmp_path):
        repo = _make_fake_repo(tmp_path)
        rec = _make_valid_record(); del rec["marker"]
        rc, path = br._write_record(rec, repo_root=repo)
        assert rc == br.EXIT_OK
        assert "marker: auto-generated-by-bugfix_record" in path.read_text()
