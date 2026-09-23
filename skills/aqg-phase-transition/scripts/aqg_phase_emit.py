#!/usr/bin/env python3
"""Phase-transition CLI entry: emit / override / query.

Resolves stakes → recommended audit mode via the depth mapping owned by
`docs/policies/audit-trigger.md` + safety floor + user-signal override + 5-min
content-hash dedup. Phase is a timing signal, not a depth input. Persists state to
`<repo>/.aqg/phase-state-<safe-task>-<short-hash>.json`.

Boundary (per ADR §5): emits SIGNAL only — does NOT call the audit tool.
Caller (human / Claude session / EAF) reads `recommended_audit_mode` and acts.

Subcommands:
- emit       — read state + decide mode + write back state
- override   — explicit mode pick (e.g. user said "deep audit" / "strict review"); update state
- query      — print current state for one task

Exit codes:
  0: success — mode decided / state queried; verdict JSON printed
  1: reserved — skill surfaces blocker in output, does not raise
  2: usage error — argparse propagates exit 2 on bad args / unknown subcommand
  3: reserved — config or schema error (not currently emitted)
  70: reserved — internal error placeholder for future use
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

# Make sibling-script imports work when run as `python3 aqg_phase_emit.py`
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aqg_phase_dedup import (  # noqa: E402
    RecentAuditRecord,
    append_audit_record,
    check_dedup,
    hash_artifact,
    normalize_artifact,
)
from aqg_phase_router import (  # noqa: E402
    DEPTH_RANK,
    Mode,
    decide_mode,
    parse_user_signal,
)
from aqg_phase_state import (  # noqa: E402
    PhaseState,
    PhaseTransition,
    is_valid_transition,
    load_state,
    save_state,
)


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _read_artifact(args: argparse.Namespace) -> str:
    artifact = _read_artifact_source(args)
    # f3 (audit c03e5465): an empty / whitespace-only artifact normalizes to ""
    # (a fixed empty-hash → false dedup hit). Fail-closed unless explicitly allowed.
    if not normalize_artifact(artifact) and not getattr(args, "allow_empty_artifact", False):
        print(
            "ERROR: artifact is empty or whitespace-only (would yield a fixed "
            "empty-hash and a false dedup hit); pass --allow-empty-artifact to override",
            file=sys.stderr,
        )
        sys.exit(2)
    return artifact


def _read_artifact_source(args: argparse.Namespace) -> str:
    if args.artifact_file:
        path = Path(args.artifact_file)
        if not path.exists():
            print(f"ERROR: artifact-file not found: {path}", file=sys.stderr)
            sys.exit(2)
        return path.read_text(encoding="utf-8", errors="replace")
    if args.artifact:
        return args.artifact
    if not sys.stdin.isatty():
        return sys.stdin.read()
    print(
        "ERROR: provide --artifact-file <path> | --artifact '<text>' | pipe via stdin",
        file=sys.stderr,
    )
    sys.exit(2)


def _resolve_repo_root(args: argparse.Namespace) -> Path:
    if args.repo:
        return Path(args.repo).resolve()
    return Path.cwd()


def cmd_emit(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    artifact = _read_artifact(args)
    artifact_hash = hash_artifact(artifact)

    state = load_state(repo_root, args.task) or PhaseState(task_id=args.task)

    # Audit fix gemini #1: prior_phase comes from full state file (not the 5-min
    # dedup cache that TTL-expires within long-running tasks).
    transition_valid, transition_reason = is_valid_transition(
        state.current_phase, args.phase
    )

    # Audit fix C2 (gpt-5.5 #2 + gemini #2): pass phase + requested_mode for
    # accurate dedup match. Resolve requested mode FIRST (before dedup query).
    decision_pre = decide_mode(
        phase=args.phase,
        stakes=args.stakes,
        user_signal=args.user_signal,
        dedup_hit=False,  # provisional — dedup queried below
    )
    requested_mode = decision_pre.mode

    dedup = check_dedup(
        state.recent_audits, args.task, artifact_hash,
        phase=args.phase, requested_mode=requested_mode,
    )
    decision = decide_mode(
        phase=args.phase,
        stakes=args.stakes,
        user_signal=args.user_signal,
        dedup_hit=dedup.is_hit,
    )

    # Update state — ALWAYS append history + recent_audits (audit fix gemini #3:
    # state machine must stay in sync even when audit was skipped).
    state.current_phase = args.phase
    state.history.append(PhaseTransition(
        phase=args.phase,
        at=_now_iso(),
        content_hash=artifact_hash,
        stakes=args.stakes,
        audit_mode=decision.mode,
    ))
    state.recent_audits = append_audit_record(
        state.recent_audits,
        RecentAuditRecord(
            task_id=args.task,
            content_hash=artifact_hash,
            phase=args.phase,
            audited_at=_now_iso(),
            audit_mode=decision.mode,
        ),
    )

    save_state(repo_root, state)

    out = {
        "phase": args.phase,
        "stakes": args.stakes,
        "task_id": args.task,
        "content_hash": artifact_hash,
        "recommended_audit_mode": decision.mode,
        "reason": decision.reason,
        "matrix_default": decision.matrix_default,
        "user_override_applied": decision.user_override_applied,
        "safety_floor_applied": decision.safety_floor_applied,
        "high_stakes_skip_confirm": decision.high_stakes_skip_confirm,
        "dedup_hit": dedup.is_hit,
        "dedup_reason": dedup.reason,
        "transition_valid": transition_valid,
        "transition_reason": transition_reason,
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        _render_emit_markdown(out, decision)
    return 0


def cmd_override(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    state = load_state(repo_root, args.task)
    if state is None:
        print(f"ERROR: no state for task {args.task!r}; run `emit` first", file=sys.stderr)
        return 2

    user_mode: Mode | None = parse_user_signal(args.user_signal) if args.user_signal else None
    if args.depth:
        if args.depth not in DEPTH_RANK:
            print(f"ERROR: --depth must be one of {list(DEPTH_RANK.keys())}", file=sys.stderr)
            return 2
        user_mode = args.depth  # type: ignore[assignment]

    if user_mode is None:
        print("ERROR: provide --user-signal <text> or --depth <mode>", file=sys.stderr)
        return 2

    # Note: override does not re-evaluate matrix; it just records the user's
    # explicit choice + caps via safety floor (since stakes is part of last
    # transition — last history entry tells us the stakes).
    last = state.history[-1] if state.history else None
    stakes = last.stakes if last else "moderate"

    decision = decide_mode(
        phase=last.phase if last else "impl_done",
        stakes=stakes,  # type: ignore[arg-type]
        user_signal=None,
        dedup_hit=False,
        explicit_override=user_mode,
    )

    if last:
        last.audit_mode = decision.mode
        # f2a (audit c03e5465): keep the recent_audits dedup cache in sync with
        # the override so dedup's rank gate does not read a stale mode.
        for rec in state.recent_audits:
            if (rec.task_id == args.task
                    and rec.content_hash == last.content_hash
                    and rec.phase == last.phase):
                rec.audit_mode = decision.mode
    save_state(repo_root, state)

    out = {
        "task_id": args.task,
        "override_mode": decision.mode,
        "reason": decision.reason,
        "safety_floor_applied": decision.safety_floor_applied,
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"# Phase-transition override applied")
        print()
        print(f"- task: {args.task}")
        print(f"- requested via: signal={args.user_signal!r} or depth={args.depth!r}")
        print(f"- final mode: **{decision.mode}**")
        print(f"- reason: {decision.reason}")
        if decision.safety_floor_applied:
            print(f"- safety floor: APPLIED (stakes=high lifted to ≥deep)")
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    repo_root = _resolve_repo_root(args)
    state = load_state(repo_root, args.task)
    if state is None:
        out = {"task_id": args.task, "exists": False}
        print(json.dumps(out, indent=2) if args.json else f"task {args.task!r} has no state")
        return 0

    out = {
        "task_id": args.task,
        "exists": True,
        "current_phase": state.current_phase,
        "history_count": len(state.history),
        "recent_audits_count": len(state.recent_audits),
        "history": [h.to_dict() for h in state.history],
    }
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
    else:
        print(f"# Phase-transition state for task `{state.task_id}`")
        print()
        print(f"- current_phase: **{state.current_phase}**")
        print(f"- transitions: {len(state.history)}")
        print(f"- recent_audits (5-min window): {len(state.recent_audits)}")
        print()
        print("## History")
        for h in state.history:
            print(f"- {h.at} | phase={h.phase} | stakes={h.stakes} | "
                  f"mode={h.audit_mode} | hash={h.content_hash[:16]}...")
    return 0


def _render_emit_markdown(out: dict, decision) -> None:
    print(f"# Phase-transition: {out['phase']} × {out['stakes']}")
    print()
    print(f"- task_id: `{out['task_id']}`")
    print(f"- content_hash: `{out['content_hash'][:16]}...`")
    print(f"- transition_valid: {out['transition_valid']} ({out['transition_reason']})")
    print()
    print(f"## Recommended audit mode: **{out['recommended_audit_mode']}**")
    print()
    print(f"- reason: {out['reason']}")
    if out["dedup_hit"]:
        print(f"- DEDUP HIT — same artifact audited within 5-min window")
        print(f"  - {out['dedup_reason']}")
    if decision.safety_floor_applied:
        print(f"- safety floor APPLIED (stakes=high cannot drop below deep)")
    if decision.high_stakes_skip_confirm:
        print("- ⚠ HIGH-STAKES SKIP requires a SECOND explicit confirmation (issue #282)")
    print()
    print("## Caller next step")
    if out["recommended_audit_mode"] == "skip":
        if decision.high_stakes_skip_confirm:
            print("- ⚠ do NOT skip yet — this is a HIGH-STAKES audit opt-out.")
            print("  Get a SECOND explicit user confirmation before honoring the skip (issue #282);")
            print("  if not re-confirmed, run `/audit mode=deep` instead.")
        else:
            # Three distinct sources reach `skip`, and telling the caller the
            # wrong one is a correctness bug in a tool whose whole job is stating
            # WHY. Matrix-sourced skip became reachable when the policy became the
            # single depth authority (Owner ruling 2026-08-11): a trivial
            # non-sensitive change is not audited, which is neither a dedup hit
            # nor the user waiving anything.
            if decision.user_override_applied:
                print("- skip audit (user opted out); proceed to next phase")
            elif "dedup" in (decision.reason or ""):
                print("- skip audit (dedup hit — already emitted for this artifact); proceed to next phase")
            else:
                print("- no audit needed (policy: trivial / non-sensitive change); proceed to next phase")
    elif out["recommended_audit_mode"] == "fast":
        print("- run `/audit mode=fast`")
    elif out["recommended_audit_mode"] == "standard":
        print("- run `/audit mode=standard`")
    elif out["recommended_audit_mode"] == "deep":
        print("- run `/audit mode=deep`")
    print()
    print("> Per ADR §5 boundary: aqg-phase-transition emits SIGNAL only.")
    print("> Caller (human / Claude session / EAF) is responsible for invoking /audit.")


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aqg_phase_emit.py",
        description="Phase-transition audit-trigger router (signal-only; caller invokes /audit).",
    )
    p.add_argument("--repo", help="repo root (defaults to cwd)")
    p.add_argument("--json", action="store_true", help="emit JSON instead of markdown")
    sub = p.add_subparsers(dest="cmd", required=True)

    emit = sub.add_parser("emit", help="emit a phase-transition + decide audit mode")
    emit.add_argument("--phase", required=True,
                      choices=["plan_done", "impl_done", "tests_written"],
                      help="phase boundary just crossed")
    emit.add_argument("--stakes", required=True,
                      choices=["trivial", "moderate", "high"],
                      help="caller's stakes classification")
    emit.add_argument("--task", required=True,
                      help="task id (e.g. sprint-11a-aqg-automation-audit)")
    emit.add_argument("--artifact-file", help="path to plan/impl/tests artifact")
    emit.add_argument("--artifact", help="literal artifact text (alternative to --artifact-file)")
    emit.add_argument("--user-signal", help="natural-language user signal (e.g. 'quick scan')")
    emit.add_argument("--allow-empty-artifact", action="store_true",
                      help="allow an empty/whitespace artifact (skips the fail-closed guard)")
    emit.set_defaults(func=cmd_emit)

    override = sub.add_parser("override", help="user override on existing emit")
    override.add_argument("--task", required=True)
    override.add_argument("--user-signal", help="natural-language signal")
    override.add_argument("--depth", choices=["skip", "fast", "standard", "deep"])
    override.set_defaults(func=cmd_override)

    query = sub.add_parser("query", help="print current state of a task")
    query.add_argument("--task", required=True)
    query.set_defaults(func=cmd_query)

    return p


def main() -> int:
    # Encoding robustness: on Windows the std streams default to cp936, so Chinese
    # output would mojibake / raise UnicodeEncodeError. Force UTF-8 on output —
    # effectively a no-op on Linux/macOS (already UTF-8); errors="replace" only
    # affects the rare undecodable char, and never crashes. stdin stays strict so
    # malformed piped input surfaces instead of being silently replaced.
    # Same approach as aqg_re_anchor.py; regression: tests/test_win_utf8_stdout.py.
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass
    try:
        sys.stdin.reconfigure(encoding="utf-8")  # strict: surface bad input
    except (AttributeError, ValueError, OSError):
        pass
    args = _build_parser().parse_args()
    try:
        return args.func(args)
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: unexpected: {exc}", file=sys.stderr)
        return 70


if __name__ == "__main__":
    sys.exit(main())
