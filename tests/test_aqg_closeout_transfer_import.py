"""Tests for Q7: aqg_closeout.py Transfer Test Pack 7th-line auto-import.

Per sketch a2 §8 Q7: closeout adds a 7th evidence-ledger row surfacing
threshold_met. Failure renders prominently; pass renders concisely;
missing/error renders as TODO with diagnostic.

Covered paths:
- happy path: threshold_met=true → PASS row, concise
- failure path: threshold_met=false → bold FAIL row
- not found: no candidate file → "not run this session" TODO row
- read error: file exists but unreadable
- malformed: yaml parses but top-level not mapping / threshold_met not bool
- candidate fallback: .aqg/transfer/last_run_summary.yaml found first
- candidate fallback: run_summary.yaml found when canonical missing
- explicit override via --transfer-summary
- --no-transfer-import suppresses the row entirely
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Optional

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "skills" / "aqg-evidence-closeout" / "scripts"))

import aqg_closeout as closeout  # noqa: E402


# ===== Helpers =====


def _run_main(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_closeout.py", *argv]
        with redirect_stdout(out):
            rc = closeout.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


def _write_summary(
    path: Path,
    *,
    threshold_met: bool,
    run_id: str = "test-aaaa",
    date: str = "2026-05-05",
    pass_rate: Optional[float] = None,
) -> None:
    """Write a syntactically valid summary, defaulting pass_rate consistent with threshold_met (audit d4598abf #1)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if pass_rate is None:
        pass_rate = 1.0 if threshold_met else 0.8
    path.write_text(
        f"""schema_version: 1
run_id: {run_id}
date: "{date}"
overall_pass_rate: {pass_rate}
threshold_strict: 1.0
threshold_met: {str(threshold_met).lower()}
task5_advisory: warn
tasks: []
provenance: {{}}
""",
        encoding="utf-8",
    )


# ===== Status enum tests =====


def test_import_transfer_summary_returns_not_found_for_none() -> None:
    result = closeout.import_transfer_summary(None)
    assert result["status"] == closeout.TransferImportStatus.NOT_FOUND


def test_import_transfer_summary_returns_not_found_for_missing_file(tmp_path: Path) -> None:
    result = closeout.import_transfer_summary(tmp_path / "nonexistent.yaml")
    assert result["status"] == closeout.TransferImportStatus.NOT_FOUND


def test_import_transfer_summary_returns_ok_passed(tmp_path: Path) -> None:
    summary = tmp_path / "run_summary.yaml"
    _write_summary(summary, threshold_met=True)
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.OK_PASSED
    assert result["data"]["threshold_met"] is True
    assert result["data"]["run_id"] == "test-aaaa"


def test_import_transfer_summary_returns_ok_failed(tmp_path: Path) -> None:
    summary = tmp_path / "run_summary.yaml"
    _write_summary(summary, threshold_met=False, pass_rate=0.8)
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.OK_FAILED
    assert result["data"]["threshold_met"] is False
    assert result["data"]["overall_pass_rate"] == 0.8


def test_import_transfer_summary_returns_malformed_for_non_mapping(tmp_path: Path) -> None:
    summary = tmp_path / "run_summary.yaml"
    summary.write_text("just a string\n", encoding="utf-8")
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.MALFORMED
    assert "mapping" in result["detail"]


def test_import_transfer_summary_returns_malformed_for_non_bool_threshold(tmp_path: Path) -> None:
    summary = tmp_path / "run_summary.yaml"
    summary.write_text(
        """schema_version: 1
threshold_met: yes_string_not_bool
""",
        encoding="utf-8",
    )
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.MALFORMED
    assert "threshold_met must be bool" in result["detail"]


# ===== Candidate path resolution =====


def test_resolve_picks_canonical_aqg_transfer_first(tmp_path: Path) -> None:
    canonical = tmp_path / ".aqg" / "transfer" / "last_run_summary.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("dummy", encoding="utf-8")
    fallback = tmp_path / "run_summary.yaml"
    fallback.write_text("also dummy", encoding="utf-8")
    chosen = closeout._resolve_transfer_summary_path(None, tmp_path)
    assert chosen == canonical


def test_resolve_falls_through_to_run_summary_when_canonical_missing(tmp_path: Path) -> None:
    fallback = tmp_path / "run_summary.yaml"
    fallback.write_text("dummy", encoding="utf-8")
    chosen = closeout._resolve_transfer_summary_path(None, tmp_path)
    assert chosen == fallback


def test_resolve_returns_none_when_no_candidates(tmp_path: Path) -> None:
    chosen = closeout._resolve_transfer_summary_path(None, tmp_path)
    assert chosen is None


def test_resolve_explicit_override_takes_priority(tmp_path: Path) -> None:
    canonical = tmp_path / ".aqg" / "transfer" / "last_run_summary.yaml"
    canonical.parent.mkdir(parents=True)
    canonical.write_text("dummy", encoding="utf-8")
    explicit = tmp_path / "explicit.yaml"
    chosen = closeout._resolve_transfer_summary_path(explicit, tmp_path)
    assert chosen == explicit


# ===== render_transfer_pack_evidence_row =====


def test_render_passed_row_concise(tmp_path: Path) -> None:
    summary = tmp_path / "s.yaml"
    _write_summary(summary, threshold_met=True, run_id="r1", date="2026-05-05", pass_rate=1.0)
    result = closeout.import_transfer_summary(summary)
    row = closeout.render_transfer_pack_evidence_row(result)
    assert row.startswith("| Transfer Test Pack |")
    assert "PASS" in row
    assert "**" not in row, "passed row must not be bold"
    assert "r1" in row
    assert "2026-05-05" in row


def test_render_failed_row_prominent(tmp_path: Path) -> None:
    summary = tmp_path / "s.yaml"
    _write_summary(summary, threshold_met=False, run_id="r2", pass_rate=0.8)
    result = closeout.import_transfer_summary(summary)
    row = closeout.render_transfer_pack_evidence_row(result)
    assert "**Transfer Test Pack**" in row, "failed row must be bold"
    assert "FAIL — threshold NOT MET" in row
    assert "r2" in row
    assert "0.8" in row


def test_render_not_found_row_auto_discovery() -> None:
    """D3 (Codex audit P2-c): Transfer Test Pack is opt-in — an auto-discovery
    miss (no --transfer-summary, no candidate file) omits the row entirely
    instead of polluting every closeout with a missing-pack row."""
    result = {
        "status": closeout.TransferImportStatus.NOT_FOUND,
        "data": None,
        "detail": "x",
        "was_explicit": False,
        "summary_path": None,
    }
    row = closeout.render_transfer_pack_evidence_row(result)
    assert row is None, "auto-discovery miss must omit the row, not emit a TODO"


def test_render_not_found_row_explicit_path_diagnosable() -> None:
    """audit d4598abf #3: explicit --transfer-summary missing path renders the actual path."""
    result = {
        "status": closeout.TransferImportStatus.NOT_FOUND,
        "data": None,
        "detail": "x",
        "was_explicit": True,
        "summary_path": "/typo/path.yaml",
    }
    row = closeout.render_transfer_pack_evidence_row(result)
    assert "/typo/path.yaml" in row
    assert "--transfer-summary" in row
    assert "| TODO |" in row


def test_render_inconsistent_row_treated_as_todo_not_pass() -> None:
    """audit d4598abf #1: threshold_met inconsistent with numerics → never PASS."""
    result = {
        "status": closeout.TransferImportStatus.INCONSISTENT,
        "data": {
            "run_id": "bad",
            "overall_pass_rate": 0.5,
            "threshold_strict": 1.0,
            "threshold_met": True,
        },
        "detail": "threshold_met=True contradicts overall_pass_rate=0.5 >= threshold_strict=1.0 → expected=False",
    }
    row = closeout.render_transfer_pack_evidence_row(result)
    assert "INCONSISTENT" in row
    assert "TODO" in row
    assert "PASS" not in row
    assert "do not trust threshold_met" in row


def test_render_malformed_row_surfaces_detail() -> None:
    result = {
        "status": closeout.TransferImportStatus.MALFORMED,
        "data": None,
        "detail": "yaml parse error: ScannerError",
    }
    row = closeout.render_transfer_pack_evidence_row(result)
    assert "unreadable" in row
    assert "ScannerError" in row


# ===== End-to-end via main() =====


def test_main_emits_passed_7th_row(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = repo / "run_summary.yaml"
    _write_summary(summary, threshold_met=True)
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
        ]
    )
    assert rc == 0
    assert "| Transfer Test Pack |" in output
    assert "PASS" in output


def test_main_emits_failed_7th_row(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = repo / "run_summary.yaml"
    _write_summary(summary, threshold_met=False, pass_rate=0.6)
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
        ]
    )
    assert rc == 0
    assert "**Transfer Test Pack**" in output
    assert "FAIL — threshold NOT MET" in output


def test_main_omits_transfer_row_when_no_summary(tmp_path: Path) -> None:
    """D3 (Codex P2-c): opt-in — a default closeout with no transfer summary
    omits the Transfer Test Pack row entirely (no missing-pack row pollution)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
        ]
    )
    assert rc == 0
    assert "Transfer Test Pack" not in output, "auto-discovery miss must omit the row"


def test_main_emits_explicit_path_diagnostic_when_typo(tmp_path: Path) -> None:
    """audit d4598abf #3: typo in --transfer-summary surfaces the actual path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    typo_path = tmp_path / "typo-no-such-file.yaml"
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
            "--transfer-summary",
            str(typo_path),
        ]
    )
    assert rc == 0
    assert "typo-no-such-file.yaml" in output
    assert "--transfer-summary" in output


def test_main_inconsistent_summary_renders_todo_not_pass(tmp_path: Path) -> None:
    """audit d4598abf #1: contradictory threshold_met never silently PASS."""
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = repo / "run_summary.yaml"
    summary.write_text(
        """schema_version: 1
run_id: contradictory-aaaa
date: "2026-05-05"
overall_pass_rate: 0.5
threshold_strict: 1.0
threshold_met: true
task5_advisory: warn
""",
        encoding="utf-8",
    )
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
        ]
    )
    assert rc == 0
    assert "INCONSISTENT" in output
    assert "TODO" in output
    assert "| PASS |" not in output


def test_import_summary_rejects_non_numeric_pass_rate(tmp_path: Path) -> None:
    summary = tmp_path / "s.yaml"
    summary.write_text(
        """schema_version: 1
threshold_met: true
overall_pass_rate: "not-a-number"
threshold_strict: 1.0
""",
        encoding="utf-8",
    )
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.MALFORMED
    assert "overall_pass_rate" in result["detail"]


def test_import_summary_rejects_pass_rate_out_of_range(tmp_path: Path) -> None:
    summary = tmp_path / "s.yaml"
    summary.write_text(
        """schema_version: 1
threshold_met: true
overall_pass_rate: 1.5
threshold_strict: 1.0
""",
        encoding="utf-8",
    )
    result = closeout.import_transfer_summary(summary)
    assert result["status"] == closeout.TransferImportStatus.MALFORMED
    assert "out of [0, 1]" in result["detail"]


def test_main_skips_7th_row_when_no_transfer_import_flag(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    summary = repo / "run_summary.yaml"
    _write_summary(summary, threshold_met=False)  # would normally add prominent row
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
            "--no-transfer-import",
        ]
    )
    assert rc == 0
    assert "Transfer Test Pack" not in output


def test_main_uses_explicit_transfer_summary_override(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    # Place a passing summary at default path …
    (repo / "run_summary.yaml").write_text(
        "schema_version: 1\nthreshold_met: true\n", encoding="utf-8"
    )
    # … but point --transfer-summary at a failing override
    explicit = tmp_path / "explicit.yaml"
    _write_summary(explicit, threshold_met=False, run_id="explicit-run")
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
            "--transfer-summary",
            str(explicit),
        ]
    )
    assert rc == 0
    assert "explicit-run" in output
    assert "FAIL — threshold NOT MET" in output


def test_main_canonical_path_picked_over_fallback(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    canonical = repo / ".aqg" / "transfer" / "last_run_summary.yaml"
    _write_summary(canonical, threshold_met=True, run_id="canonical-run")
    fallback = repo / "run_summary.yaml"
    _write_summary(fallback, threshold_met=False, run_id="fallback-run")
    rc, output = _run_main(
        [
            "--task",
            "test",
            "--repo",
            str(repo),
            "--no-construction-import",
        ]
    )
    assert rc == 0
    assert "canonical-run" in output
    assert "fallback-run" not in output
    assert "PASS" in output
