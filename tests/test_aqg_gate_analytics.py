"""Tests for scripts/aqg_gate_analytics.py — Wilson + 4-week aggregation per skill.

Per Q3 #10 sketch v3 §5 (PR-5a gate instrumentation).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import aqg_gate_analytics as aga  # noqa: E402


# ===========================================
# wilson_lower_bound — pure math
# ===========================================


class TestWilsonLowerBound:
    def test_zero_total_returns_zero(self):
        assert aga.wilson_lower_bound(0, 0) == 0.0

    def test_perfect_score_high_confidence(self):
        # 100/100 with z=1.96 should give Wilson lower around 0.96+
        result = aga.wilson_lower_bound(100, 100, confidence=0.95)
        assert result > 0.96
        assert result < 1.0

    def test_zero_successes_low_bound(self):
        # 0/10 → upper bound is non-zero but lower bound is 0.0
        result = aga.wilson_lower_bound(0, 10, confidence=0.95)
        assert result == 0.0

    def test_typical_value_textbook(self):
        # Classic textbook: 8/10 successes at 95% CI → Wilson lower ≈ 0.49
        result = aga.wilson_lower_bound(8, 10, confidence=0.95)
        assert 0.48 < result < 0.51

    def test_large_sample_approaches_phat(self):
        # 800/1000 → Wilson lower should be very close to 0.8 (large n)
        result = aga.wilson_lower_bound(800, 1000, confidence=0.95)
        assert 0.77 < result < 0.80

    def test_small_sample_conservative(self):
        # 4/5 → raw rate 0.8 but Wilson lower is much lower (small sample penalty)
        raw = 4 / 5
        wilson = aga.wilson_lower_bound(4, 5, confidence=0.95)
        assert wilson < raw  # Wilson is conservative for small samples
        assert wilson < 0.6  # significantly lower

    def test_invalid_successes_raises(self):
        with pytest.raises(ValueError):
            aga.wilson_lower_bound(11, 10)
        with pytest.raises(ValueError):
            aga.wilson_lower_bound(-1, 10)

    def test_invalid_confidence_raises(self):
        with pytest.raises(ValueError):
            aga.wilson_lower_bound(5, 10, confidence=0.50)

    def test_higher_confidence_lower_bound(self):
        # 99% CI gives lower bound than 95% CI for same data
        w95 = aga.wilson_lower_bound(80, 100, confidence=0.95)
        w99 = aga.wilson_lower_bound(80, 100, confidence=0.99)
        assert w99 < w95

    def test_returns_in_unit_interval(self):
        for k in [0, 1, 5, 10]:
            for n in [10, 100, 1000]:
                if k > n:
                    continue
                result = aga.wilson_lower_bound(k, n, confidence=0.95)
                assert 0.0 <= result <= 1.0


# ===========================================
# parse_iso8601 utility
# ===========================================


class TestParseISO8601:
    def test_parse_z_suffix(self):
        ts = aga._parse_iso8601("2026-05-04T12:00:00Z")
        assert ts is not None
        assert ts.tzinfo is not None

    def test_parse_offset(self):
        ts = aga._parse_iso8601("2026-05-04T12:00:00+00:00")
        assert ts is not None

    def test_parse_no_timezone_assumes_utc(self):
        ts = aga._parse_iso8601("2026-05-04T12:00:00")
        assert ts is not None
        assert ts.tzinfo == timezone.utc

    def test_invalid_returns_none(self):
        assert aga._parse_iso8601("not-a-date") is None
        assert aga._parse_iso8601("") is None


# ===========================================
# aggregate_window — read reports, group per skill
# ===========================================


def _write_report(
    dir_path: Path, name: str, generated_at: datetime, case_results: list[dict]
) -> Path:
    """Helper: write a nightly report JSON."""
    dir_path.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "generated_at": generated_at.isoformat(),
        "model": "test-model",
        "case_results": case_results,
    }
    report_path = dir_path / name
    report_path.write_text(json.dumps(report))
    return report_path


class TestAggregateWindow:
    def test_no_dir_returns_empty(self, tmp_path):
        result = aga.aggregate_window(tmp_path / "nope", days=28)
        assert result == {}

    def test_empty_dir_returns_empty(self, tmp_path):
        (tmp_path / "reports").mkdir()
        result = aga.aggregate_window(tmp_path / "reports", days=28)
        assert result == {}

    def test_single_report_in_window(self, tmp_path):
        reports = tmp_path / "reports"
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(days=1),
            [
                {"expected_skill": "aqg-startup-preflight", "status": "behavior_pass"},
                {"expected_skill": "aqg-startup-preflight", "status": "behavior_fail"},
            ],
        )
        result = aga.aggregate_window(reports, days=28, now=now)
        assert "aqg-startup-preflight" in result
        assert result["aqg-startup-preflight"]["behavior_pass"] == 1
        assert result["aqg-startup-preflight"]["behavior_fail"] == 1

    def test_outside_window_dropped(self, tmp_path):
        reports = tmp_path / "reports"
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        _write_report(
            reports,
            "nightly-old.json",
            now - timedelta(days=60),  # 60 days ago, outside 28-day window
            [{"expected_skill": "aqg-x", "status": "behavior_pass"}],
        )
        result = aga.aggregate_window(reports, days=28, now=now)
        assert result == {}

    def test_malformed_json_skipped(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "nightly-bad.json").write_text("not json")
        # Also write a good one
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        _write_report(
            reports,
            "nightly-good.json",
            now - timedelta(days=1),
            [{"expected_skill": "aqg-x", "status": "behavior_pass"}],
        )
        result = aga.aggregate_window(reports, days=28, now=now)
        assert "aqg-x" in result
        # bad one didn't crash

    def test_missing_generated_at_skipped(self, tmp_path):
        reports = tmp_path / "reports"
        reports.mkdir()
        (reports / "nightly-no-ts.json").write_text(
            json.dumps({"schema_version": 1, "case_results": []})
        )
        result = aga.aggregate_window(reports, days=28)
        assert result == {}

    def test_unknown_status_preserved_verbatim(self, tmp_path):
        # PR-5b: aggregate_window preserves unknown status keys verbatim
        # (compute_skill_metrics buckets them as "excluded")
        reports = tmp_path / "reports"
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(days=1),
            [{"expected_skill": "aqg-x", "status": "weird_new_status"}],
        )
        result = aga.aggregate_window(reports, days=28, now=now)
        assert result["aqg-x"]["weird_new_status"] == 1

    def test_multiple_skills_grouped(self, tmp_path):
        reports = tmp_path / "reports"
        now = datetime(2026, 5, 4, 12, 0, 0, tzinfo=timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(days=1),
            [
                {"expected_skill": "aqg-a", "status": "behavior_pass"},
                {"expected_skill": "aqg-a", "status": "behavior_pass"},
                {"expected_skill": "aqg-b", "status": "behavior_fail"},
                {"expected_skill": "aqg-b", "status": "infra_error"},
            ],
        )
        result = aga.aggregate_window(reports, days=28, now=now)
        assert result["aqg-a"]["behavior_pass"] == 2
        assert result["aqg-b"]["behavior_fail"] == 1
        assert result["aqg-b"]["infra_error"] == 1


# ===========================================
# compute_skill_metrics — count -> stat
# ===========================================


class TestComputeSkillMetrics:
    def test_zero_attempts_skipped(self):
        per_skill = {"aqg-x": {"behavior_pass": 0, "behavior_fail": 0, "infra_error": 5}}
        stats = aga.compute_skill_metrics(per_skill)
        assert stats == []

    def test_basic_calculation(self):
        per_skill = {"aqg-x": {"behavior_pass": 8, "behavior_fail": 2}}
        stats = aga.compute_skill_metrics(per_skill, confidence=0.95)
        assert len(stats) == 1
        s = stats[0]
        assert s.skill == "aqg-x"
        assert s.pass_count == 8
        assert s.fail_count == 2
        assert s.total_attempts == 10
        assert s.phat == 0.8
        assert 0.4 < s.wilson_lower < 0.6  # Wilson conservative

    def test_excluded_in_count_not_in_denominator(self):
        per_skill = {
            "aqg-x": {
                "behavior_pass": 5, "behavior_fail": 0,
                "infra_error": 100, "skipped": 50,
            }
        }
        stats = aga.compute_skill_metrics(per_skill)
        s = stats[0]
        assert s.total_attempts == 5  # only behavior_pass + gating fail
        assert s.excluded_count == 150
        assert s.phat == 1.0  # 5/5

    def test_sorted_by_skill_name(self):
        per_skill = {
            "aqg-zebra": {"behavior_pass": 1, "behavior_fail": 1},
            "aqg-alpha": {"behavior_pass": 1, "behavior_fail": 1},
        }
        stats = aga.compute_skill_metrics(per_skill)
        assert [s.skill for s in stats] == ["aqg-alpha", "aqg-zebra"]

    # PR-5b: gating fail covers multiple statuses (not just behavior_fail)
    def test_gating_fail_includes_drift_and_over_trigger(self):
        per_skill = {
            "aqg-x": {
                "behavior_pass": 2,
                "behavior_fail": 1,
                "drift_detected": 1,
                "over_trigger": 1,
                "delegated_trigger": 1,
            }
        }
        stats = aga.compute_skill_metrics(per_skill)
        s = stats[0]
        assert s.pass_count == 2
        assert s.fail_count == 4  # behavior_fail + drift + over + delegated
        assert s.total_attempts == 6
        assert s.excluded_count == 0


# ===========================================
# render — text + json
# ===========================================


class TestRendering:
    def test_render_text_empty(self):
        out = aga.render_text([], window_days=28, confidence=0.95, reports_count=0)
        assert "no skills with pass/fail attempts" in out

    def test_render_text_with_data(self):
        stats = [
            aga.SkillStat(
                skill="aqg-x",
                pass_count=8, fail_count=2, excluded_count=0,
                total_attempts=10, phat=0.8, wilson_lower=0.5,
            )
        ]
        out = aga.render_text(stats, window_days=28, confidence=0.95, reports_count=3)
        assert "aqg-x" in out
        assert "28-day window" in out
        assert "3 reports" in out
        assert "0.8000" in out  # phat

    def test_render_json_valid(self):
        stats = [
            aga.SkillStat(
                skill="aqg-x",
                pass_count=8, fail_count=2, excluded_count=0,
                total_attempts=10, phat=0.8, wilson_lower=0.5,
            )
        ]
        out = aga.render_json(stats, window_days=28, confidence=0.95, reports_count=3)
        parsed = json.loads(out)
        assert parsed["window_days"] == 28
        assert parsed["confidence"] == 0.95
        assert parsed["reports_count"] == 3
        assert len(parsed["skills"]) == 1
        assert parsed["skills"][0]["skill"] == "aqg-x"


# ===========================================
# main / CLI
# ===========================================


class TestCLI:
    def test_missing_dir_returns_error(self, tmp_path, capsys):
        rc = aga.main(["--reports-dir", str(tmp_path / "nope")])
        assert rc == aga.EXIT_NO_REPORTS
        captured = capsys.readouterr()
        assert "not found" in captured.err

    def test_invalid_confidence_returns_usage_error(self, tmp_path, capsys):
        (tmp_path / "reports").mkdir()
        rc = aga.main(
            ["--reports-dir", str(tmp_path / "reports"), "--confidence", "0.50"]
        )
        assert rc == aga.EXIT_USAGE
        captured = capsys.readouterr()
        assert "unsupported confidence" in captured.err

    def test_empty_dir_text_output(self, tmp_path, capsys):
        (tmp_path / "reports").mkdir()
        rc = aga.main(["--reports-dir", str(tmp_path / "reports")])
        assert rc == aga.EXIT_OK
        captured = capsys.readouterr()
        assert "no skills" in captured.out or "no skills" in captured.out.lower()

    def test_with_reports_text_output(self, tmp_path, capsys):
        reports = tmp_path / "reports"
        now = datetime.now(timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(days=1),
            [
                {"expected_skill": "aqg-x", "status": "behavior_pass"},
                {"expected_skill": "aqg-x", "status": "behavior_pass"},
            ],
        )
        rc = aga.main(["--reports-dir", str(reports)])
        assert rc == aga.EXIT_OK
        captured = capsys.readouterr()
        assert "aqg-x" in captured.out
        assert "28-day window" in captured.out
        assert "1 reports" in captured.out

    def test_with_reports_json_output(self, tmp_path, capsys):
        reports = tmp_path / "reports"
        now = datetime.now(timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(days=1),
            [{"expected_skill": "aqg-x", "status": "behavior_pass"}],
        )
        rc = aga.main(["--reports-dir", str(reports), "--format", "json"])
        assert rc == aga.EXIT_OK
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed["window_days"] == 28
        assert parsed["skills"][0]["skill"] == "aqg-x"

    # PR-5b: integration test with realistic AQG nightly report shape
    def test_realistic_nightly_report(self, tmp_path, capsys):
        """Full status matrix matching tests/behavior/decision.py CaseStatus enum."""
        reports = tmp_path / "reports"
        now = datetime.now(timezone.utc)
        _write_report(
            reports,
            "nightly-1.json",
            now - timedelta(hours=1),
            [
                {"case_id": "x-1", "expected_skill": "aqg-startup-preflight", "status": "behavior_pass"},
                {"case_id": "x-2", "expected_skill": "aqg-startup-preflight", "status": "behavior_pass"},
                {"case_id": "y-1", "expected_skill": "aqg-audit-adjudication", "status": "behavior_fail"},
                {"case_id": "y-2", "expected_skill": "aqg-audit-adjudication", "status": "behavior_fail"},
                {"case_id": "y-3", "expected_skill": "aqg-audit-adjudication", "status": "behavior_pass"},
                {"case_id": "z-1", "expected_skill": "aqg-evidence-closeout", "status": "infra_error"},
                {"case_id": "w-1", "expected_skill": "aqg-code-construction", "status": "drift_detected"},
            ],
        )
        rc = aga.main(["--reports-dir", str(reports), "--format", "json"])
        assert rc == aga.EXIT_OK
        parsed = json.loads(capsys.readouterr().out)
        skills = {s["skill"]: s for s in parsed["skills"]}
        # aqg-startup-preflight: 2/2 = 100%
        assert skills["aqg-startup-preflight"]["pass"] == 2
        assert skills["aqg-startup-preflight"]["fail"] == 0
        # aqg-audit-adjudication: 1/3 (the wrong-trigger pattern)
        assert skills["aqg-audit-adjudication"]["pass"] == 1
        assert skills["aqg-audit-adjudication"]["fail"] == 2
        # aqg-evidence-closeout: only 1 infra_error → no attempts → skipped
        assert "aqg-evidence-closeout" not in skills
        # aqg-code-construction: 0 pass + 1 drift_detected (counts as gating fail)
        assert skills["aqg-code-construction"]["pass"] == 0
        assert skills["aqg-code-construction"]["fail"] == 1
