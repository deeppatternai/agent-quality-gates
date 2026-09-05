#!/usr/bin/env python3
"""aqg-memory-hygiene local helper — memory-lifecycle scanner (signal-only).

Signal-only / read-only per ADR 2026-06-04-aqg-memory-hygiene-skill-a1.md: this
script READS a memory dir and emits a structured report. It NEVER writes a memory
file (acceptance #3), never calls audit-mcp, and never touches the network
(acceptance #4). Normalizing / superseding is the CALLER's action.

Two subcommands:
- `validate`  → strict schema gate over each memory-node's frontmatter (ADR §4.3);
                exit non-zero on any violation.
- `staleness` → list `volatile` memories whose `last_verified` is past the horizon
                (default 90d / env AQG_MEMORY_STALE_DAYS / --days); always exit 0.

The pure parsing / schema / staleness helpers live in `_mh_core.py` (split to keep
each file under the 800-line house limit). stdlib + PyYAML (a DECLARED AQG runtime
dependency, already installed in CI; not stdlib — wording per ADR §6 acceptance #4).

Exit codes:
  0: success — `validate` all-compliant / no memory dir / empty dir; `staleness` always
  1: validation failure — `validate` found ≥1 schema violation
  2: usage error — bad args / unknown subcommand, OR PyYAML (declared dep) unavailable
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from pathlib import Path

from _mh_core import (  # noqa: F401  (re-exported public API)
    DEFAULT_STALE_DAYS,
    ENV_STALE_DAYS,
    PYYAML_MISSING_NOTE,
    MemoryNode,
    NodeResult,
    StalenessItem,
    StalenessReport,
    ValidateReport,
    assess_staleness,
    coerce_date,
    discover_memory_dir,
    parse_node,
    resolve_threshold,
    staleness_dir,
    validate_dir,
    validate_node,
)


def _today() -> _dt.date:
    return _dt.datetime.now(_dt.timezone.utc).date()


def _now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _pyyaml_available() -> bool:
    try:
        import yaml  # noqa: F401
    except ImportError:
        return False
    return True


def _resolve_memory_dir(args: argparse.Namespace) -> Path:
    if args.memory_dir:
        return Path(args.memory_dir)
    return discover_memory_dir()


# ===== validate ================================================================


def _print_validate_text(report: ValidateReport) -> None:
    print("# AQG Memory Hygiene — validate (signal-only)")
    print()
    print(f"- generated_at: {_now_iso()}")
    print(f"- memory_dir: {report.memory_dir}")
    if not report.dir_exists:
        print("- result: no memory dir at this path (no target — not a violation)")
        return
    vios = report.violation_results
    warns = report.warning_results
    print(f"- nodes_scanned: {report.node_count} (index files excluded)")
    print(f"- violations: {sum(len(r.violations) for r in vios)} across {len(vios)} node(s)")
    print(f"- warnings: {sum(len(r.warnings) for r in warns)}")
    print()
    if vios:
        print("## Violations (strict gate — fix before commit)")
        for r in vios:
            for msg in r.violations:
                print(f"- {msg}")
        print()
    if warns:
        print("## Warnings (surface only — not a gate)")
        for r in warns:
            for msg in r.warnings:
                print(f"- {msg}")
        print()
    print("## Verdict")
    if report.ok:
        tail = " (with warnings)" if warns else ""
        print(f"- PASS: {report.node_count} node(s) schema-compliant{tail}")
    else:
        print(f"- FAIL: {len(vios)} node(s) have schema violations")
    print()
    print("## Boundary")
    print("- signal-only: this scan modified no memory file. Normalize / supersede is the caller's action.")


def _validate_json(report: ValidateReport, rc: int) -> dict:
    return {
        "command": "validate",
        "memory_dir": str(report.memory_dir),
        "dir_exists": report.dir_exists,
        "nodes_scanned": report.node_count,
        "ok": rc == 0,
        "violations": [m for r in report.results for m in r.violations],
        "warnings": [m for r in report.results for m in r.warnings],
        "exit_code": rc,
    }


def cmd_validate(args: argparse.Namespace) -> int:
    if not _pyyaml_available():
        # A missing declared dependency is an environment/usage error, NOT a
        # verdict that the memory is invalid — short-circuit instead of
        # misreporting every node as a violation.
        print("# AQG Memory Hygiene — validate: cannot run")
        print(f"- {PYYAML_MISSING_NOTE}")
        return 2
    report = validate_dir(_resolve_memory_dir(args), _today())
    rc = 0 if (not report.dir_exists or report.ok) else 1
    if args.json:
        print(json.dumps(_validate_json(report, rc), indent=2, ensure_ascii=False))
    else:
        _print_validate_text(report)
    return rc


# ===== staleness ===============================================================


def _print_staleness_text(report: StalenessReport) -> None:
    print("# AQG Memory Hygiene — staleness (signal-only)")
    print()
    print(f"- generated_at: {_now_iso()}")
    print(f"- memory_dir: {report.memory_dir}")
    print(f"- threshold_days: {report.threshold}")
    if not report.dir_exists:
        print("- result: no memory dir at this path (no target)")
        return
    print(f"- nodes_scanned: {report.scanned}")
    print(f"- stale (volatile past horizon): {len(report.stale)}")
    print(f"- skipped (superseded / malformed / missing last_verified): {len(report.skipped)}")
    print(f"- fresh (durable / volatile within horizon): {report.fresh_count}")
    print()
    if report.stale:
        print("## Volatile memories needing re-verification")
        for item in report.stale:
            print(f"- {item.path.name}: {item.reason}")
        print()
    if report.skipped:
        print("## Skipped (not gated — surfaced for transparency)")
        for p, reason in report.skipped:
            print(f"- {p.name}: {reason}")
        print()
    print("## Next safe step")
    print("- Re-confirm each listed volatile memory still holds, then bump its `last_verified` (caller action).")


def _staleness_json(report: StalenessReport) -> dict:
    return {
        "command": "staleness",
        "memory_dir": str(report.memory_dir),
        "dir_exists": report.dir_exists,
        "threshold_days": report.threshold,
        "nodes_scanned": report.scanned,
        "stale": [
            {"file": i.path.name, "age_days": i.age_days, "reason": i.reason}
            for i in report.stale
        ],
        "skipped": [{"file": p.name, "reason": reason} for p, reason in report.skipped],
        "fresh": report.fresh_count,
        "exit_code": 0,
    }


def cmd_staleness(args: argparse.Namespace) -> int:
    # No PyYAML short-circuit: without it every node simply graceful-skips
    # ("unparseable") and staleness still exits 0 — the ADR §4.4 contract.
    report = staleness_dir(_resolve_memory_dir(args), _today(), resolve_threshold(args.days))
    if args.json:
        print(json.dumps(_staleness_json(report), indent=2, ensure_ascii=False))
    else:
        _print_staleness_text(report)
    return 0  # staleness surfaces; it never gates


# ===== CLI =====================================================================


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="aqg_memory_hygiene.py",
        description=(
            "AQG memory-lifecycle scanner (signal-only). Validates memory-node "
            "frontmatter schema + surfaces stale volatile memories; never mutates "
            "memory, never calls audit-mcp."
        ),
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    vp = sub.add_parser("validate", help="strict schema gate over memory-node frontmatter (ADR §4.3)")
    vp.add_argument("--memory-dir", default=None,
                    help="memory dir (default: auto-discover the current project's dir)")
    vp.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    vp.set_defaults(func=cmd_validate)

    sp = sub.add_parser("staleness", help="list volatile memories past their last_verified horizon")
    sp.add_argument("--memory-dir", default=None,
                    help="memory dir (default: auto-discover the current project's dir)")
    sp.add_argument("--days", type=int, default=None,
                    help=f"staleness threshold in days (default {DEFAULT_STALE_DAYS} / env {ENV_STALE_DAYS})")
    sp.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    sp.set_defaults(func=cmd_staleness)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # aqg: top-level boundary
        print(f"ERROR: unexpected: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
