"""Nightly real-trigger runner for AQG behavior tests.

Loads triggers.yaml, samples N cases (default 5 per cost guard, sketch v3 §3.3
D-tier ~$0.135/run × 5 = $0.68/nightly), invokes ClaudeRunner against the
sanitized fixture AQG dir, runs extractor + drift + canary + decision, and
emits a JSON report.

Designed to run inside CI (PR-2b workflow) with ANTHROPIC_API_KEY env. Locally
runnable for manual smoke testing too.

Cost guard: aborts the whole nightly run when accumulated cost exceeds
--per-nightly-budget-usd (default $5). Per-run guard is enforced by ClaudeRunner.

Exit codes:
    0 — all gating cases PASS or EXCLUDED (no FAIL)
    1 — at least one BEHAVIOR_FAIL / OVER_TRIGGER / DELEGATED_TRIGGER
    2 — usage / config error (missing fixture, no api key, etc.)
    3 — nightly budget exceeded mid-run (partial results in report)
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from tests.behavior.canary import run_canary_check
from tests.behavior.decision import (
    GATING_EXCLUDED,
    GATING_FAIL,
    GATING_PASS,
    CaseStatus,
    decide_status,
)
from tests.behavior.extractor import extract_skill_calls
from tests.behavior.runner import DEFAULT_MODEL, ClaudeRunner

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_BUDGET_EXCEEDED = 3
EXIT_CANARY_FAIL = 4  # audit 0b5cc864 convergent: fail-fast on canary

DEFAULT_PER_NIGHTLY_BUDGET_USD = 5.0
DEFAULT_SAMPLE_SIZE = 5  # sketch v3 §3.3 D-tier safe small smoke
MIN_SAMPLE_SIZE = 1
MAX_SAMPLE_SIZE = 20  # full fixture cap


def _load_fixture(fixture_path: Path) -> dict[str, Any]:
    if not fixture_path.is_file():
        raise FileNotFoundError(f"fixture not found: {fixture_path}")
    return yaml.safe_load(fixture_path.read_text(encoding="utf-8"))


def _run_one_case(
    case: dict[str, Any],
    runner: ClaudeRunner,
    canary_result_passed: bool,
    per_run_budget_usd: float,
) -> dict[str, Any]:
    """Run one trigger case end-to-end: claude -p → extract → decide."""
    case_id = case.get("id", "unnamed")
    prompt = case.get("prompt", "")
    expected_skill = case.get("expected_skill")

    # Real claude -p invocation
    run_result = runner.run_case(
        prompt=prompt,
        case_id=case_id,
        per_run_budget_usd=per_run_budget_usd,
    )

    # Extract + decide
    top_level_calls, diagnostics = extract_skill_calls(run_result.jsonl_path)
    status = decide_status(
        diagnostics=diagnostics,
        top_level_calls=top_level_calls,
        expected_skill=expected_skill,
        canary_passed=canary_result_passed,
        per_run_budget_usd=per_run_budget_usd,
    )

    return {
        "case_id": case_id,
        "expected_skill": expected_skill,
        "style": case.get("style"),
        "status": status.value,
        "actual_top_level_skills": [c["skill_name"] for c in top_level_calls],
        # drift_passed / drift_reason retained as stable schema keys for any
        # downstream report parser (audit 454077e6 gemini-f1). The drift-hash
        # mechanism was retired in PR-3, so these are now constant: drift never
        # fails because nightly_runner no longer computes it.
        "drift_passed": True,
        "drift_reason": "drift mechanism retired (PR-3)",
        "run_exit_code": run_result.exit_code,
        "run_duration_s": run_result.duration_s,
        "run_timed_out": run_result.timed_out,
        "cost_usd": diagnostics.get("result_total_cost_usd"),
        "num_turns": diagnostics.get("result_num_turns"),
    }


def _empty_report(model: str, sample_size: int, rng_seed: int,
                  canary, accumulated_cost: float, budget_exceeded: bool,
                  case_results: list, extra: dict | None = None) -> dict[str, Any]:
    """Build report dict shared by happy + canary-fail + crash paths."""
    by_status: dict[str, int] = {}
    for r in case_results:
        by_status[r["status"]] = by_status.get(r["status"], 0) + 1

    pass_count = sum(by_status.get(s.value, 0) for s in GATING_PASS)
    fail_count = sum(by_status.get(s.value, 0) for s in GATING_FAIL)
    excluded_count = sum(by_status.get(s.value, 0) for s in GATING_EXCLUDED)
    gating_total = pass_count + fail_count
    pass_rate = (pass_count / gating_total) if gating_total else 0.0

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "model": model,
        "rng_seed": rng_seed,
        "sample_size": sample_size,
        "actual_sample_count": len(case_results),
        "canary_passed": canary.canary_passed if canary else None,
        "canary_reason": canary.reason if canary else None,
        "by_status": by_status,
        "gating_metrics": {
            "pass": pass_count,
            "fail": fail_count,
            "excluded": excluded_count,
            "pass_rate": pass_rate,
        },
        "accumulated_cost_usd": accumulated_cost,
        "budget_exceeded": budget_exceeded,
        "case_results": case_results,
    }
    if extra:
        report.update(extra)
    return report


def run_nightly(
    fixture_path: Path,
    fixture_aqg_dir: Path,
    canary_jsonl_path: Path,
    canary_expected_path: Path,
    sample_size: int,
    per_run_budget_usd: float,
    per_nightly_budget_usd: float,
    rng_seed: int = 42,
    model: str = DEFAULT_MODEL,
) -> tuple[dict[str, Any], int]:
    """Run nightly behavior tests; return (report, exit_code).

    Audit 0b5cc864 fixes applied:
    - Canary fail-fast: exit non-zero immediately, no Claude invocation (#convergent)
    - Per-case try/except: continue on crash, append ERROR result (gemini #4)
    - Pre-budget check: skip case when remaining < per_run (gpt-5.5 #3)
    - Input bounds validation: enforced in _parse_args + here (gpt-5.5 #6)
    """
    if not (MIN_SAMPLE_SIZE <= sample_size <= MAX_SAMPLE_SIZE):
        raise ValueError(
            f"sample_size must be {MIN_SAMPLE_SIZE}..{MAX_SAMPLE_SIZE}, got {sample_size}"
        )

    fixture = _load_fixture(fixture_path)
    cases = fixture.get("cases", [])
    if not cases:
        raise ValueError("fixture has no 'cases'")

    # Sample N (deterministic via rng_seed; for stable cross-Python sampling,
    # sort cases by id first so ordering is independent of YAML insertion)
    cases_sorted = sorted(cases, key=lambda c: c.get("id", ""))
    rng = random.Random(rng_seed)
    sampled = rng.sample(cases_sorted, min(sample_size, len(cases_sorted)))

    # Canary check (run once, must pass before any Claude invocation)
    canary = run_canary_check(canary_jsonl_path, canary_expected_path)

    # FAIL-FAST: canary failure means extractor / schema / fixture is broken.
    # Don't waste budget on cases that can't yield valid signals.
    if not canary.canary_passed:
        report = _empty_report(
            model=model, sample_size=sample_size, rng_seed=rng_seed,
            canary=canary, accumulated_cost=0.0, budget_exceeded=False,
            case_results=[],
            extra={"abort_reason": "canary check failed; Claude invocation skipped"},
        )
        return report, EXIT_CANARY_FAIL

    runner = ClaudeRunner(fixture_aqg_dir=fixture_aqg_dir, model=model)

    case_results: list[dict[str, Any]] = []
    accumulated_cost = 0.0
    budget_exceeded = False

    for case in sampled:
        # Pre-check budget: if remaining < per_run cap, can't safely run
        remaining = per_nightly_budget_usd - accumulated_cost
        if remaining < per_run_budget_usd:
            budget_exceeded = True
            break

        try:
            result = _run_one_case(
                case=case,
                runner=runner,
                canary_result_passed=canary.canary_passed,
                per_run_budget_usd=min(per_run_budget_usd, remaining),
            )
        except Exception as exc:  # noqa: BLE001 — defensive; report not lost on crash
            case_results.append({
                "case_id": case.get("id", "unnamed"),
                "expected_skill": case.get("expected_skill"),
                "style": case.get("style"),
                "status": CaseStatus.INFRA_ERROR.value,
                "actual_top_level_skills": [],
                "run_exit_code": None,
                "run_duration_s": 0.0,
                "run_timed_out": False,
                "cost_usd": 0.0,
                "num_turns": 0,
                "exception": f"{type(exc).__name__}: {exc}",
            })
            continue

        case_results.append(result)
        if result.get("cost_usd"):
            accumulated_cost += result["cost_usd"]

    report = _empty_report(
        model=model, sample_size=sample_size, rng_seed=rng_seed,
        canary=canary, accumulated_cost=accumulated_cost,
        budget_exceeded=budget_exceeded, case_results=case_results,
    )

    if budget_exceeded:
        return report, EXIT_BUDGET_EXCEEDED
    if report["gating_metrics"]["fail"] > 0:
        return report, EXIT_FAIL
    return report, EXIT_OK


def _bounded_int(min_val: int, max_val: int):
    def _inner(v: str) -> int:
        try:
            n = int(v)
        except ValueError as e:
            raise argparse.ArgumentTypeError(f"must be integer, got {v!r}") from e
        if not (min_val <= n <= max_val):
            raise argparse.ArgumentTypeError(
                f"must be in [{min_val}, {max_val}], got {n}"
            )
        return n
    return _inner


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="AQG behavior tests nightly real-trigger runner")
    p.add_argument("--fixture", type=Path,
                   default=Path("tests/behavior/fixtures/triggers.yaml"))
    p.add_argument("--fixture-aqg-dir", type=Path,
                   default=Path.cwd(),
                   help="path to sanitized AQG clone for --add-dir")
    p.add_argument("--canary-jsonl", type=Path,
                   default=Path("tests/behavior/fixtures/canary/preflight_known_skill.jsonl"))
    p.add_argument("--canary-expected", type=Path,
                   default=Path("tests/behavior/fixtures/canary/preflight_known_skill.expected.json"))
    # Bounds enforced (audit 0b5cc864 gpt-5.5 #6: prevent sample_size=0 silent pass)
    p.add_argument("--sample-size", type=_bounded_int(MIN_SAMPLE_SIZE, MAX_SAMPLE_SIZE),
                   default=DEFAULT_SAMPLE_SIZE)
    p.add_argument("--per-run-budget-usd", type=float, default=0.50)
    p.add_argument("--per-nightly-budget-usd", type=float,
                   default=DEFAULT_PER_NIGHTLY_BUDGET_USD)
    p.add_argument("--rng-seed", type=int, default=42)
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--output", type=Path, required=True,
                   help="path to write report JSON")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print(
            "ERROR: ANTHROPIC_API_KEY env var not set; cannot invoke real claude CLI",
            file=sys.stderr,
        )
        return EXIT_USAGE

    try:
        report, exit_code = run_nightly(
            fixture_path=args.fixture,
            fixture_aqg_dir=args.fixture_aqg_dir,
            canary_jsonl_path=args.canary_jsonl,
            canary_expected_path=args.canary_expected,
            sample_size=args.sample_size,
            per_run_budget_usd=args.per_run_budget_usd,
            per_nightly_budget_usd=args.per_nightly_budget_usd,
            rng_seed=args.rng_seed,
            model=args.model,
        )
    except (FileNotFoundError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return EXIT_USAGE

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(
        f"nightly run done: sample={report['actual_sample_count']} "
        f"pass={report['gating_metrics']['pass']} "
        f"fail={report['gating_metrics']['fail']} "
        f"excluded={report['gating_metrics']['excluded']} "
        f"cost=${report['accumulated_cost_usd']:.4f} "
        f"budget_exceeded={report['budget_exceeded']} "
        f"exit_code={exit_code}",
        file=sys.stderr,
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
