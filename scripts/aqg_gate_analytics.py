#!/usr/bin/env python3
"""AQG gate analytics — Wilson lower bound + 4-week aggregation per skill.

Per Q3 #10 sketch v3 §5 (PR-5a): aggregate nightly behavior test reports
within a sliding window (default 4 weeks), compute Wilson score interval
lower bound for per-skill trigger-pass rate. Used to detect skill regression
trends without false-positive on small sample noise.

Wilson lower bound vs raw proportion:
- raw `phat = pass / total` is biased high for small samples
- Wilson lower bound is the lower edge of a binomial confidence interval
  → conservative estimate that "we are X% confident the true rate is at least this"
- Useful for SLO gating: "skill must hold Wilson lower bound >= 0.85 over 4 weeks"

Status categorization (denominator):
- "pass" → success (correct skill triggered + drift OK) — counts as success
- "fail" → failure (wrong skill or drift) — counts as failure
- "infra_error" / "excluded" / "budget_exceeded" → DROPPED from denominator
  (these are CI infra noise, not real signals)

Exit codes:
  0 OK
  1 reports dir missing
  2 usage error
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


EXIT_OK = 0
EXIT_NO_REPORTS = 1
EXIT_USAGE = 2

# Confidence -> two-sided z-score (standard normal critical values)
# Common levels per textbook stats; default 0.95 for 1.96 z-score.
_Z_BY_CONFIDENCE: dict[float, float] = {
    0.80: 1.2816,
    0.85: 1.4395,
    0.90: 1.6449,
    0.95: 1.96,
    0.975: 2.2414,
    0.99: 2.5758,
}

# PR-5b fix: status names must match tests/behavior/decision.py CaseStatus enum.
# AQG nightly reports use behavior_* prefix; pass/fail raw was a guess that didn't
# match real schema. Source: tests/behavior/decision.py GATING_PASS / GATING_FAIL.
#
# Statuses that count as success (Wilson numerator)
_SUCCESS_STATUSES: frozenset[str] = frozenset({"behavior_pass"})

# Statuses that count as gating failure (counted in denominator)
_GATING_FAIL_STATUSES: frozenset[str] = frozenset(
    {"behavior_fail", "over_trigger", "delegated_trigger", "drift_detected"}
)

# All statuses that count toward Wilson denominator (real attempts: pass + gating fail)
_DENOMINATOR_STATUSES: frozenset[str] = _SUCCESS_STATUSES | _GATING_FAIL_STATUSES


@dataclass(frozen=True)
class SkillStat:
    skill: str
    pass_count: int        # behavior_pass count
    fail_count: int        # sum of gating fail statuses
    excluded_count: int    # infra_error + parse_error + skipped + ...
    total_attempts: int    # pass + fail (Wilson denominator)
    phat: float            # raw pass rate (pass / attempts)
    wilson_lower: float    # Wilson lower bound at requested confidence

    def as_dict(self) -> dict:
        return {
            "skill": self.skill,
            "pass": self.pass_count,
            "fail": self.fail_count,
            "excluded": self.excluded_count,
            "total_attempts": self.total_attempts,
            "phat": round(self.phat, 4),
            "wilson_lower": round(self.wilson_lower, 4),
        }


def wilson_lower_bound(
    successes: int, total: int, confidence: float = 0.95
) -> float:
    """Compute the Wilson score interval lower bound.

    Returns a value in [0.0, 1.0]. Returns 0.0 if total <= 0 (no data).
    Raises ValueError if successes is out of [0, total] range or unsupported confidence.
    """
    if total <= 0:
        return 0.0
    if successes < 0 or successes > total:
        raise ValueError(
            f"successes must be in [0, total]; got successes={successes}, total={total}"
        )
    if confidence not in _Z_BY_CONFIDENCE:
        raise ValueError(
            f"unsupported confidence {confidence}; choose from {sorted(_Z_BY_CONFIDENCE.keys())}"
        )
    z = _Z_BY_CONFIDENCE[confidence]
    phat = successes / total
    z_sq = z * z
    denominator = 1.0 + z_sq / total
    center = (phat + z_sq / (2.0 * total)) / denominator
    margin = (
        z * math.sqrt((phat * (1.0 - phat) / total) + (z_sq / (4.0 * total * total)))
    ) / denominator
    return max(0.0, center - margin)


def _parse_iso8601(timestamp: object) -> Optional[datetime]:
    """Parse ISO 8601 with trailing Z; return None on failure or non-str input.

    The explicit isinstance guard makes the choke-point's type-tolerance explicit
    (audit 72e0206e convergent): a report whose `generated_at` is a non-string
    JSON value (null / number / list / dict) is skipped rather than relied upon to
    raise AttributeError inside the parser. Behaviour is unchanged today (the
    except below already absorbed those), but the boundary is now self-documenting
    and robust to future refactors of this helper.
    """
    if not isinstance(timestamp, str):
        return None
    try:
        ts = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _window_cutoff(now: datetime, days: int) -> datetime:
    """Return ``now - days``, clamped to the minimum aware datetime on overflow.

    An absurdly large --window-days (negative is rejected at the CLI, DR-13)
    would overflow timedelta/datetime arithmetic (OverflowError, DR-12); clamping
    to datetime.min means "look back that far" degrades to "include all history"
    instead of crashing. The try wraps the whole expression so both the
    ``timedelta(days=...)`` construction and the subtraction are covered.
    """
    try:
        return now - timedelta(days=days)
    except OverflowError:
        return datetime.min.replace(tzinfo=timezone.utc)


def aggregate_window(
    reports_dir: Path, days: int, now: Optional[datetime] = None
) -> dict[str, dict[str, int]]:
    """Aggregate per-skill case status counts within sliding window.

    Returns: {skill_name: {status_name: count}}. Status names are taken
    verbatim from the nightly report (no normalization), so any unknown
    status enum values are preserved for compute_skill_metrics to bucket.
    Reports outside the window or malformed are skipped silently — including
    non-dict top-level JSON (DR-10), non-list case_results, non-dict cases, and
    non-string skill/status values (DR-11), all of which are tolerated rather
    than aborting the whole aggregation.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = _window_cutoff(now, days)
    per_skill: dict[str, dict[str, int]] = {}
    if not reports_dir.is_dir():
        return per_skill
    for report_file in sorted(reports_dir.glob("nightly-*.json")):
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue  # DR-10: top-level must be an object, else data.get crashes
        gen_at = _parse_iso8601(data.get("generated_at", ""))
        if gen_at is None or gen_at < cutoff:
            continue
        case_results = data.get("case_results", [])
        if not isinstance(case_results, list):
            continue  # malformed case_results → no usable cases
        for case in case_results:
            if not isinstance(case, dict):
                continue  # DR-11 sibling: case must be an object
            skill = case.get("expected_skill")
            if not isinstance(skill, str) or not skill:
                skill = "unknown"  # DR-11: non-str/empty/missing → unhashable-safe
            status = case.get("status")
            if not isinstance(status, str) or not status:
                status = "unknown"
            entry = per_skill.setdefault(skill, {})
            entry[status] = entry.get(status, 0) + 1
    return per_skill


def compute_skill_metrics(
    per_skill: dict[str, dict[str, int]], confidence: float = 0.95
) -> list[SkillStat]:
    """Convert per-skill counts into SkillStat list with Wilson bounds.

    Sums success / gating-fail / other status counts using AQG CaseStatus
    enum (PR-5b fix). Skills with zero attempts (no pass + no gating-fail)
    are skipped.
    """
    stats: list[SkillStat] = []
    for skill, counts in sorted(per_skill.items()):
        pass_n = sum(counts.get(s, 0) for s in _SUCCESS_STATUSES)
        fail_n = sum(counts.get(s, 0) for s in _GATING_FAIL_STATUSES)
        excluded_n = sum(
            v for k, v in counts.items() if k not in _DENOMINATOR_STATUSES
        )
        attempts = pass_n + fail_n
        if attempts == 0:
            continue
        phat = pass_n / attempts
        wilson = wilson_lower_bound(pass_n, attempts, confidence=confidence)
        stats.append(
            SkillStat(
                skill=skill,
                pass_count=pass_n,
                fail_count=fail_n,
                excluded_count=excluded_n,
                total_attempts=attempts,
                phat=phat,
                wilson_lower=wilson,
            )
        )
    return stats


def render_text(
    stats: list[SkillStat], window_days: int, confidence: float, reports_count: int
) -> str:
    """Render text-format summary for terminal display."""
    lines = [
        f"AQG Gate Analytics — {window_days}-day window, {reports_count} reports, "
        f"confidence={confidence}",
        "",
    ]
    if not stats:
        lines.append("(no skills with pass/fail attempts in window; check reports dir)")
        return "\n".join(lines)
    header = f"{'Skill':<35} {'Pass':>6} {'Fail':>6} {'Total':>6} {'Phat':>8} {'Wilson':>10}"
    lines.append(header)
    lines.append("-" * len(header))
    for s in stats:
        lines.append(
            f"{s.skill:<35} {s.pass_count:>6} {s.fail_count:>6} "
            f"{s.total_attempts:>6} {s.phat:>8.4f} {s.wilson_lower:>10.4f}"
        )
    return "\n".join(lines) + "\n"


def render_json(
    stats: list[SkillStat], window_days: int, confidence: float, reports_count: int
) -> str:
    """Render JSON summary (suitable for piping to other tools)."""
    return json.dumps(
        {
            "window_days": window_days,
            "confidence": confidence,
            "reports_count": reports_count,
            "skills": [s.as_dict() for s in stats],
        },
        indent=2,
        ensure_ascii=False,
    )


def _count_in_window_reports(reports_dir: Path, days: int, now: datetime) -> int:
    """Count nightly-*.json reports whose generated_at is within the window."""
    cutoff = _window_cutoff(now, days)
    count = 0
    if not reports_dir.is_dir():
        return 0
    for report_file in reports_dir.glob("nightly-*.json"):
        try:
            data = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(data, dict):
            continue  # DR-10 sibling: non-dict top-level → data.get would crash
        gen_at = _parse_iso8601(data.get("generated_at", ""))
        if gen_at is None or gen_at < cutoff:
            continue
        count += 1
    return count


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="aqg_gate_analytics",
        description="Aggregate AQG nightly reports + compute Wilson lower bound per skill",
    )
    parser.add_argument(
        "--reports-dir", default="reports", help="Nightly reports directory"
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=28,
        help="Sliding window size in days (default 28 = 4 weeks)",
    )
    parser.add_argument(
        "--confidence",
        type=float,
        default=0.95,
        help=f"Wilson confidence level (one of {sorted(_Z_BY_CONFIDENCE.keys())}; default 0.95)",
    )
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )
    args = parser.parse_args(argv)

    if args.confidence not in _Z_BY_CONFIDENCE:
        print(
            f"ERROR: unsupported confidence {args.confidence}; "
            f"choose from {sorted(_Z_BY_CONFIDENCE.keys())}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    # DR-13: a negative window puts cutoff in the FUTURE, silently dropping every
    # report and exiting 0 with "(no skills)" — masking real data as "no data".
    # Reject it as a usage error instead of degrading silently.
    if args.window_days < 0:
        print(
            f"ERROR: --window-days must be >= 0; got {args.window_days}",
            file=sys.stderr,
        )
        return EXIT_USAGE

    reports_dir = Path(args.reports_dir).resolve()
    if not reports_dir.is_dir():
        print(f"ERROR: reports directory not found: {reports_dir}", file=sys.stderr)
        return EXIT_NO_REPORTS

    now = datetime.now(timezone.utc)
    per_skill = aggregate_window(reports_dir, args.window_days, now=now)
    reports_count = _count_in_window_reports(reports_dir, args.window_days, now)
    stats = compute_skill_metrics(per_skill, confidence=args.confidence)

    if args.format == "json":
        print(render_json(stats, args.window_days, args.confidence, reports_count))
    else:
        print(render_text(stats, args.window_days, args.confidence, reports_count))
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
