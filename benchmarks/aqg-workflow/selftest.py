#!/usr/bin/env python3
"""Prove the benchmark instrument before any agent run.

Ponytail discipline ("prove the instruments, no API — run first"): for each
task the good reference must pass every adversarial check, and the bad
reference (the lazy-but-plausible version) must be caught by at least one. If
either fails, the instrument is untrustworthy and no agent output should be
scored against it. Deterministic, stdlib-only, zero API cost.

Run: python3 benchmarks/aqg-workflow/selftest.py  (exit 0 == trustworthy)
"""
from __future__ import annotations

import importlib.util
import argparse
import sys
from pathlib import Path

TASKS_DIR = Path(__file__).resolve().parent / "tasks"


def _load(path: Path):
    """Import a single .py file under a unique module name."""
    name = f"_bm_{path.parent.name}_{path.stem}".replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _check_task(task_dir: Path, *, sandboxed: bool = False) -> list[str]:
    """Return instrument problems for one task ([] == trustworthy)."""
    problems: list[str] = []
    checks = _load(task_dir / "checks.py")
    good = _load(task_dir / "good_ref.py")
    bad = _load(task_dir / "bad_ref.py")
    target = task_dir.name.replace("-", "_")  # parse-positive-int -> parse_positive_int

    good_failures = checks.run_checks(getattr(good, target))
    if good_failures:
        problems.append(
            f"good_ref failed {len(good_failures)} check(s) — e.g. {good_failures[0]}"
        )

    bad_failures = checks.run_checks(getattr(bad, target))
    if not bad_failures:
        problems.append("bad_ref passed every check — the trap is not caught")

    if sandboxed:
        # The in-process checker proves task semantics.  Primary collection
        # scores through this separate JSON worker/sandbox path, so prove both
        # paths before a paid cell is allowed to start.
        import run
        good_score = run.score_file_sandboxed(task_dir / "good_ref.py", task_dir / "checks.py", target)
        if good_score.errored or not good_score.passed:
            problems.append(f"sandboxed good_ref failed: {good_score.error or good_score.failures}")
        bad_score = run.score_file_sandboxed(task_dir / "bad_ref.py", task_dir / "checks.py", target)
        if bad_score.errored:
            problems.append(f"sandboxed bad_ref harness failed: {bad_score.error}")
        elif bad_score.passed:
            problems.append("sandboxed bad_ref passed every check — the trap is not caught")

    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Prove WS-7 task instruments without an API call.")
    parser.add_argument("--sandboxed", action="store_true",
                        help="also prove every reference through the production macOS scorer")
    args = parser.parse_args(argv)
    # A directory can remain locally after a task is removed from the frozen
    # suite (for example an empty directory after a patch).  Only a directory
    # containing the visible task contract is an instrument candidate; missing
    # hidden files are still reported by `_check_task`, never silently skipped.
    task_dirs = sorted(
        d for d in TASKS_DIR.iterdir()
        if d.is_dir() and (d / "task.md").is_file() and (d / "seed.py").is_file()
    )
    if not task_dirs:
        print(f"no tasks found under {TASKS_DIR}")
        return 1

    ok = True
    for task_dir in task_dirs:
        problems = _check_task(task_dir, sandboxed=args.sandboxed)
        if problems:
            ok = False
            print(f"[BROKEN] {task_dir.name}")
            for p in problems:
                print(f"    - {p}")
        else:
            path = "in-process + sandboxed" if args.sandboxed else "in-process"
            print(f"[OK]     {task_dir.name}: {path} good_ref clean, bad_ref caught")

    if not ok:
        print("\nSELFTEST FAILED — instrument untrustworthy; do not score agent runs.")
        return 1
    print(f"\nSELFTEST PASSED — {len(task_dirs)} task instrument(s) trustworthy.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
