"""Tests for aqg-phase-transition skill — covers ADR §6 acceptance scenarios
plus 5 boundary cases.

Run from `skills/aqg-phase-transition/`:
    PYTHONPATH=scripts python3 -m pytest tests/

Acceptance scenarios (ADR §6):
1. PRD spanning 3 modules → PLAN_DONE × moderate → dual-audit
2. prod auth migration → IMPL_DONE × high → triple-audit
3. utils helper 5 unit tests → TESTS_WRITTEN × trivial → fast
4. payment-flow integration tests → TESTS_WRITTEN × high → dual-audit
5. trivial typo fix walks IMPL_DONE → fast/skip preserved (no auto-upgrade)
6. user "快速干" + plan complete → skip phase trigger, log warning

Boundary cases:
A. dedup hit within 5 min returns skip
B. content-hash whitespace normalization
C. user "别审" returns skip even on high stakes (USER WINS — phase not gate)
D. high stakes safety floor: "fast" signal → dual-audit (not fast)
E. transition validity warns on skip / regression but allows
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Resolve scripts dir relative to this test file
_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS))

# The repo-level policy-marker parser. These tests already reach the repo root to
# read the policy file; importing its parser from the same place keeps one regex
# instead of two more copies of it.
#
# APPENDED, not inserted at 0: the repo root's scripts/ holds ~40 modules and the
# skill's own scripts/ is what these tests exist to exercise. Putting the repo
# root first would let a future same-named module there shadow the skill's — the
# tests would silently test the wrong file. There is no collision today; the
# ordering is what keeps it that way.
_REPO = Path(__file__).resolve().parents[3]
sys.path.append(str(_REPO / "scripts"))

from aqg_policy_markers import depth_by_stakes  # noqa: E402

from aqg_phase_dedup import (  # noqa: E402
    RecentAuditRecord,
    check_dedup,
    hash_artifact,
    normalize_artifact,
)
from aqg_phase_router import (  # noqa: E402
    DEPTH_RANK,
    MATRIX,
    decide_mode,
    merge_with_existing,
    parse_user_signal,
)
from aqg_phase_state import (  # noqa: E402
    PhaseState,
    PhaseTransition,
    is_valid_transition,
    load_state,
    save_state,
    state_path,
)
import aqg_phase_emit as emit  # noqa: E402


# ---- ADR §6 acceptance scenarios -----------------------------------------


def test_acceptance_1_cross_module_prd_plan_moderate_standard():
    """Scenario 1: finished a refactoring PRD spanning 3 modules → PLAN_DONE × moderate → standard (two mainstream models)"""
    d = decide_mode(phase="plan_done", stakes="moderate", user_signal=None, dedup_hit=False)
    assert d.mode == "standard"
    assert d.matrix_default == "standard"
    assert not d.user_override_applied
    assert not d.safety_floor_applied


def test_acceptance_2_prod_auth_migration_impl_high_deep():
    """Scenario 2: finished implementing a prod auth migration → IMPL_DONE × high → deep (6 models)"""
    d = decide_mode(phase="impl_done", stakes="high", user_signal=None, dedup_hit=False)
    assert d.mode == "deep"
    assert d.matrix_default == "deep"


def test_acceptance_3_utils_helper_unit_test_tests_trivial_skip():
    """Scenario 3: utils helper 5 unit tests → trivial → skip.

    Was `fast`. `fast` is not a rung in docs/policies/audit-trigger.md — it is the
    opt-in cheap pass for something the ladder would otherwise skip — so routing a
    trivial change to it made the matrix prescribe an audit the policy does not.
    """
    d = decide_mode(phase="tests_written", stakes="trivial", user_signal=None, dedup_hit=False)
    assert d.mode == "skip"


def test_acceptance_4_payment_integration_test_tests_high_deep():
    """Scenario 4: finished payment-flow integration tests → TESTS_WRITTEN × high → deep (floor=deep locks the high column)"""
    d = decide_mode(phase="tests_written", stakes="high", user_signal=None, dedup_hit=False)
    assert d.mode == "deep"
    # high column fills deep directly (not lifted implicitly by the floor): matrix_default is itself deep
    assert d.matrix_default == "deep"
    assert not d.safety_floor_applied


def test_acceptance_5_trivial_typo_fix_impl_no_auto_upgrade():
    """Scenario 5: trivial typo fix (< 50 lines) IMPL_DONE → skip, and never upgraded.

    The old version asserted `standard` while its own comment recorded that the
    spec wanted "keep fast / no-audit, don't force an upgrade" — the mismatch was
    documented in the test and left in place. The policy resolves it: trivial and
    non-sensitive is not audited.
    """
    d = decide_mode(phase="impl_done", stakes="trivial", user_signal=None, dedup_hit=False)
    assert d.mode == "skip"
    assert not d.safety_floor_applied


def test_acceptance_5_trivial_typo_with_user_skip_signal():
    """Scenario 5 follow-up: user says '别审' on trivial typo → skip respected."""
    d = decide_mode(phase="impl_done", stakes="trivial",
                    user_signal="trivial 看, 我自己拍板", dedup_hit=False)
    # parse_user_signal hits "我自己拍板" → skip
    assert d.mode == "skip"


def test_acceptance_6_user_kuai_su_gan_on_plan():
    """Scenario 6: user '快速干' on PLAN_DONE moderate → fast (matrix downgrade
    via user signal). Spec phrasing '跳过 PLAN_DONE 触发 + log warning' — in
    code we map '快速干' → fast (not skip), since the user wants speed not skip.
    """
    # 快速干 ≠ 别审; user signal parsed as fast
    d = decide_mode(phase="plan_done", stakes="moderate",
                    user_signal="快速扫一下, 别拖慢我", dedup_hit=False)
    assert d.mode == "fast"
    assert d.user_override_applied


# ---- ADR §3 / §4 boundary cases ------------------------------------------


def test_boundary_A_dedup_hit_returns_skip():
    """Boundary A: dedup hit within 5 min → forced 'skip'."""
    d = decide_mode(phase="impl_done", stakes="moderate", dedup_hit=True)
    assert d.mode == "skip"
    assert "dedup hit" in d.reason.lower()


def test_boundary_B_content_hash_whitespace_normalization():
    """Boundary B: trivial whitespace diff → same hash (tolerates reformatting)."""
    a = "def foo(x):\n    return x + 1"
    b = "def  foo(x):\n\n    return x + 1\n"  # extra whitespace
    c = "def\tfoo(x):\n    return\nx + 1"  # different whitespace
    assert hash_artifact(a) == hash_artifact(b), "extra whitespace should not change hash"
    # c has 'return\nx + 1' which after normalize becomes 'return x + 1' — same!
    assert hash_artifact(a) == hash_artifact(c), "tab vs space + newline collapse should match"


def test_boundary_B_content_hash_real_change_differs():
    """Sanity: real content change → different hash."""
    a = "def foo(x): return x + 1"
    b = "def foo(x): return x + 2"  # 1 → 2
    assert hash_artifact(a) != hash_artifact(b)


def test_boundary_C_user_skip_overrides_high_stakes():
    """Boundary C: user '别审' on high stakes → skip respected.

    Per ADR §4 invariant 4: 'Phase never "overrides" the user; if the user
    says no audit, then no audit.'
    Safety floor only applies when the user said something *less safe*; an
    explicit skip is the user's decision.
    """
    d = decide_mode(phase="impl_done", stakes="high",
                    user_signal="别审, 我自己拍板", dedup_hit=False)
    # parse_user_signal returns skip; safety floor does NOT lift skip
    # (skip rank is 0 < HIGH_STAKES_FLOOR_RANK=3, but skip is user opt-out)
    # Hmm: current decide_mode applies safety floor whenever stakes=high and
    # rank < floor. So skip → would get lifted to 'two'. Verify the actual
    # behavior matches ADR invariant.
    # ADR says user explicit "别审" should be respected. Test enforces this:
    # if it currently fails, decide_mode logic needs adjustment.
    assert d.mode == "skip", (
        f"expected skip per ADR invariant 4 (user opt-out wins); got {d.mode} "
        f"with reason {d.reason!r}"
    )
    # issue #282: honoring a high-stakes opt-out skip needs a compensating
    # control. mode stays skip (invariant 4), but the router flags that the
    # caller MUST obtain a second explicit confirmation before honoring it.
    assert d.high_stakes_skip_confirm is True, (
        "high-stakes user opt-out skip must set high_stakes_skip_confirm=True "
        "so the caller re-confirms before honoring (issue #282)"
    )


def test_user_skip_moderate_stakes_no_confirm():
    """issue #282 contrast: user '别审' on MODERATE stakes → skip, but NO
    second-confirmation required (only high stakes triggers the control)."""
    d = decide_mode(phase="impl_done", stakes="moderate",
                    user_signal="别审, 我自己拍板", dedup_hit=False)
    assert d.mode == "skip"
    assert d.high_stakes_skip_confirm is False


def test_dedup_skip_high_stakes_no_confirm():
    """issue #282 contrast: a dedup-hit skip on high stakes is NOT a user
    opt-out — it must NOT demand a second confirmation (nothing was declined)."""
    d = decide_mode(phase="impl_done", stakes="high",
                    user_signal=None, dedup_hit=True)
    assert d.mode == "skip"
    assert d.high_stakes_skip_confirm is False


def test_matrix_never_returns_skip_for_high_stakes():
    """issue #282 invariant (audit 79ee4f4a claude f3), narrowed to what it protects.

    The original form forbade 'skip' anywhere in the matrix. The property actually
    at stake is narrower: a HIGH-STAKES skip must not come from the matrix, because
    only a user opt-out skip carries the #282 second-confirmation control, and a
    matrix-sourced one would bypass it.

    Owner ruling 2026-08-11 made the policy the single depth authority, and its
    rung 1 says a trivial non-sensitive change is not audited — so trivial now maps
    to 'skip' deliberately. That is safe by construction: decide_mode's opt-out
    branch is guarded by `source != matrix[...]`, so a matrix skip never masquerades
    as a user opt-out, and the high-stakes floor would lift one to deep anyway.
    """
    from aqg_phase_router import MATRIX
    for phase, by_stakes in MATRIX.items():
        assert by_stakes["high"] != "skip", f"MATRIX[{phase}][high] == skip would bypass #282 control"


def test_matrix_sourced_skip_is_not_treated_as_a_user_opt_out():
    """A trivial skip is the policy speaking, not the user waiving an audit.

    Conflating the two would mislabel the reason and, on any future stakes change,
    could route a matrix skip through the opt-out path that the #282 control guards.
    """
    d = decide_mode(phase="impl_done", stakes="trivial", user_signal=None, dedup_hit=False)
    assert d.mode == "skip"
    assert not d.user_override_applied, "matrix skip must not claim the user opted out"
    assert not d.high_stakes_skip_confirm


def test_safety_floor_high_stakes_no_confirm():
    """issue #282 (audit 79ee4f4a claude f2): a high-stakes under-depth signal that
    gets floored UP (a non-skip path) must NOT set high_stakes_skip_confirm — only an
    opt-out *skip* triggers the second-confirmation control."""
    d = decide_mode(phase="impl_done", stakes="high",
                    user_signal="快速扫一下", dedup_hit=False)
    assert d.mode == "deep"               # floored up from fast
    assert d.safety_floor_applied
    assert d.high_stakes_skip_confirm is False


def test_emit_markdown_gates_confirm_warning_on_high_stakes(capsys):
    """issue #282 (audit 79ee4f4a claude f1 / gpt-5.5 f1): the emit markdown
    second-confirmation warning renders ONLY for a high-stakes opt-out skip, not for
    a moderate-stakes skip."""
    import aqg_phase_emit

    def render(stakes):
        d = decide_mode(phase="impl_done", stakes=stakes,
                        user_signal="别审", dedup_hit=False)
        out = {
            "phase": "impl_done", "stakes": stakes, "task_id": "t",
            "content_hash": "h" * 16, "recommended_audit_mode": d.mode,
            "reason": d.reason, "matrix_default": d.matrix_default,
            "dedup_hit": False, "dedup_reason": "",
            "transition_valid": True, "transition_reason": "ok",
        }
        aqg_phase_emit._render_emit_markdown(out, d)
        return capsys.readouterr().out

    assert "SECOND explicit confirmation" in render("high")
    assert "SECOND explicit confirmation" not in render("moderate")


def test_boundary_D_safety_floor_high_stakes_fast_to_deep():
    """Boundary D: user '快速扫' on high stakes → safety floor lifts to deep.

    Per ADR §4 invariant 3 (Owner 2026-06-04 redefine): high stakes may not drop below deep.
    """
    d = decide_mode(phase="impl_done", stakes="high",
                    user_signal="快速扫一下", dedup_hit=False)
    assert d.mode == "deep"
    assert d.safety_floor_applied
    assert "safety floor" in d.reason.lower()


def test_boundary_E_transition_validity_warns_on_skip():
    """Boundary E: skip phase emits warning (is_valid=False) but doesn't block."""
    valid, reason = is_valid_transition(from_phase="plan_done", to_phase="tests_written")
    assert valid is False  # skipped impl_done
    assert "skipped phase" in reason.lower()


def test_boundary_E_transition_validity_canonical_passes():
    """Sanity: canonical PLAN → IMPL is valid."""
    valid, reason = is_valid_transition(from_phase="plan_done", to_phase="impl_done")
    assert valid is True
    assert "canonical" in reason.lower()


def test_boundary_E_transition_validity_regression_warns():
    """Sanity: going backwards (TESTS → IMPL) is regression warning."""
    valid, reason = is_valid_transition(from_phase="tests_written", to_phase="impl_done")
    assert valid is False
    assert "regression" in reason.lower()


# ---- Matrix coverage / safety floor / merge logic -----------------------


def test_matrix_full_coverage():
    """Sanity: every phase × stakes combo has a mode."""
    for phase in MATRIX:
        for stakes in ["trivial", "moderate", "high"]:
            assert MATRIX[phase][stakes] in DEPTH_RANK, \
                f"matrix[{phase}][{stakes}] missing or invalid"


def test_safety_floor_applies_when_low_signal_meets_high_stakes():
    """High stakes + 'fast' signal → safety floor → 'deep'."""
    d = decide_mode(phase="impl_done", stakes="high", user_signal="fast")
    assert d.mode == "deep"
    assert d.safety_floor_applied is True


def test_safety_floor_does_not_apply_when_signal_already_high():
    """High stakes + '严格审'(→deep) signal → 'deep' (no floor needed)."""
    d = decide_mode(phase="impl_done", stakes="high", user_signal="严格审")
    assert d.mode == "deep"
    assert d.safety_floor_applied is False


def test_merge_with_existing_takes_higher_rank():
    """Per ADR §4: existing audit + phase decision → take fail-safer."""
    phase_d = decide_mode(phase="plan_done", stakes="moderate")  # → standard
    final, reason = merge_with_existing(phase_d, existing_mode="fast")
    # phase says standard (rank 2) > existing fast (rank 1) → phase wins
    assert final == "standard"
    assert "fail-safer" in reason.lower()


def test_merge_existing_higher_than_phase_existing_wins():
    """Per ADR §4: user already said deep, phase says standard → existing wins."""
    phase_d = decide_mode(phase="plan_done", stakes="moderate")  # → standard
    final, reason = merge_with_existing(phase_d, existing_mode="deep")
    assert final == "deep"


def test_merge_existing_skip_phase_standard():
    """Edge: existing skip + phase standard → phase wins (skip is opt-out, not deeper)."""
    phase_d = decide_mode(phase="plan_done", stakes="moderate")  # → standard
    final, _ = merge_with_existing(phase_d, existing_mode="skip")
    assert final == "standard"


# ---- Dedup logic + state machine -----------------------------------------


def test_dedup_no_records_no_hit():
    result = check_dedup([], task_id="t1", content_hash="h1")
    assert result.is_hit is False


def test_dedup_same_task_same_hash_within_window_hits():
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    records = [RecentAuditRecord(
        task_id="t1",
        content_hash="h1",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="standard",
    )]
    result = check_dedup(records, task_id="t1", content_hash="h1", now=now)
    assert result.is_hit is True
    assert result.matched_record is not None


def test_dedup_same_task_different_hash_no_hit():
    """Same task different artifact → not dedup."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    records = [RecentAuditRecord(
        task_id="t1",
        content_hash="h1",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="standard",
    )]
    result = check_dedup(records, task_id="t1", content_hash="h2", now=now)
    assert result.is_hit is False


def test_dedup_different_task_same_hash_no_hit():
    """Different task same artifact → not dedup (different scope of review)."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    records = [RecentAuditRecord(
        task_id="t1",
        content_hash="h1",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=60)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="standard",
    )]
    result = check_dedup(records, task_id="t2", content_hash="h1", now=now)
    assert result.is_hit is False


def test_dedup_outside_window_no_hit():
    """6 min ago > 5 min TTL → not dedup."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 5, 9, 12, 0, 0, tzinfo=timezone.utc)
    records = [RecentAuditRecord(
        task_id="t1",
        content_hash="h1",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=360)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="standard",
    )]
    result = check_dedup(records, task_id="t1", content_hash="h1", now=now)
    assert result.is_hit is False


# ---- State persistence ---------------------------------------------------


def test_state_save_load_roundtrip(tmp_path):
    state = PhaseState(
        task_id="sprint-test",
        current_phase="impl_done",
        history=[
            PhaseTransition(
                phase="plan_done",
                at="2026-05-09T10:00:00Z",
                content_hash="abc123",
                stakes="moderate",
                audit_mode="standard",
            ),
            PhaseTransition(
                phase="impl_done",
                at="2026-05-09T11:00:00Z",
                content_hash="def456",
                stakes="moderate",
                audit_mode="standard",
            ),
        ],
    )
    p = save_state(tmp_path, state)
    assert p.exists()

    loaded = load_state(tmp_path, "sprint-test")
    assert loaded is not None
    assert loaded.task_id == "sprint-test"
    assert loaded.current_phase == "impl_done"
    assert len(loaded.history) == 2
    assert loaded.history[0].phase == "plan_done"


def test_state_load_missing_returns_none(tmp_path):
    assert load_state(tmp_path, "nonexistent-task") is None


def test_state_concurrent_tasks_isolated(tmp_path):
    """Different task_ids → different files (per-task isolation)."""
    s1 = PhaseState(task_id="task-a", current_phase="plan_done")
    s2 = PhaseState(task_id="task-b", current_phase="impl_done")
    save_state(tmp_path, s1)
    save_state(tmp_path, s2)

    loaded_a = load_state(tmp_path, "task-a")
    loaded_b = load_state(tmp_path, "task-b")
    assert loaded_a is not None and loaded_a.current_phase == "plan_done"
    assert loaded_b is not None and loaded_b.current_phase == "impl_done"


# ---- User signal parsing -------------------------------------------------


def test_parse_user_signal_skip_variants():
    assert parse_user_signal("我自己拍板") == "skip"
    assert parse_user_signal("别审") == "skip"
    assert parse_user_signal("不用审, 自己看着办") == "skip"
    assert parse_user_signal("先别审") == "skip"
    # English (added so English users are covered)
    assert parse_user_signal("no audit needed") == "skip"
    assert parse_user_signal("skip the audit") == "skip"
    assert parse_user_signal("i'll decide myself") == "skip"


def test_parse_user_signal_deep_variants():
    # Chinese (functional, kept) + English (added so English users are covered)
    assert parse_user_signal("严格审一遍") == "deep"
    assert parse_user_signal("深审一下") == "deep"
    assert parse_user_signal("上 prod 前再过一遍") == "deep"
    assert parse_user_signal("do a deep audit") == "deep"
    assert parse_user_signal("please be thorough") == "deep"
    assert parse_user_signal("strict review before prod") == "deep"


def test_parse_user_signal_standard_variants():
    # standard = dual-mainstream 2 models; "看看/审一下/交叉验证" fold into standard
    assert parse_user_signal("交叉验证下") == "standard"
    assert parse_user_signal("审一下") == "standard"
    assert parse_user_signal("review it please") == "standard"
    assert parse_user_signal("cross-check this") == "standard"
    assert parse_user_signal("can you double-check") == "standard"
    assert parse_user_signal("get a second opinion") == "standard"


def test_parse_user_signal_fast_variants():
    assert parse_user_signal("快速扫一下") == "fast"
    assert parse_user_signal("/audit fast") == "fast"
    assert parse_user_signal("just a quick scan") == "fast"
    assert parse_user_signal("quick look please") == "fast"
    assert parse_user_signal("sanity check it") == "fast"


def test_parse_user_signal_legacy_tier_words_retired():
    # 二审/三审/双审 are retired legacy tier words → no longer recognized
    assert parse_user_signal("三审") is None
    assert parse_user_signal("二审") is None
    assert parse_user_signal("双审") is None


def test_parse_user_signal_empty_or_unknown_returns_none():
    assert parse_user_signal(None) is None
    assert parse_user_signal("") is None
    assert parse_user_signal("写些不相关的话") is None


# ---- Normalize ------------------------------------------------------------


def test_normalize_artifact_basic():
    assert normalize_artifact("foo bar") == "foo bar"
    assert normalize_artifact("foo  bar") == "foo bar"  # double space → single
    assert normalize_artifact("\n\nfoo\n\nbar\n\n") == "foo bar"  # newlines → space + strip
    assert normalize_artifact("foo\tbar") == "foo bar"  # tab → space


def test_normalize_artifact_empty():
    assert normalize_artifact("") == ""
    assert normalize_artifact("   ") == ""


# ---- Batch-3 audit c03e5465 regressions ----------------------------------

import io  # noqa: E402
import json as _json  # noqa: E402
from contextlib import redirect_stdout, redirect_stderr  # noqa: E402


def _run_emit(argv: list[str]) -> tuple[int, str, str]:
    """Run aqg_phase_emit.main() capturing rc / stdout / stderr."""
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["aqg_phase_emit.py", *argv]
        with redirect_stdout(out), redirect_stderr(err):
            try:
                rc = emit.main()
            except SystemExit as exc:
                rc = int(exc.code) if exc.code is not None else 0
    finally:
        sys.argv = saved
    return rc, out.getvalue(), err.getvalue()


# f1 (CRITICAL) — a negated skip intent must NOT parse as skip / silently skip
def test_f1_negated_skip_not_parsed_as_skip():
    assert parse_user_signal("do not skip audit") != "skip"
    assert parse_user_signal("不要别审") != "skip"
    assert parse_user_signal("never skip the audit") != "skip"


def test_f1_negated_skip_high_stakes_gets_audited():
    d = decide_mode(phase="impl_done", stakes="high", user_signal="do not skip audit")
    assert DEPTH_RANK[d.mode] >= DEPTH_RANK["deep"], \
        f"high-stakes 'do not skip' must not drop below the deep safety floor; got {d.mode}"


def test_f1_bare_skip_still_respected():
    # The negation guard must not break a genuine opt-out.
    assert parse_user_signal("别审") == "skip"
    assert parse_user_signal("我自己拍板") == "skip"
    assert parse_user_signal("不用审, 自己看着办") == "skip"
    d = decide_mode(phase="impl_done", stakes="high", user_signal="别审")
    assert d.mode == "skip", "a bare user opt-out is still honored on high stakes"


# f3 — empty / whitespace-only artifact must be rejected (fail-closed)
def test_f3_empty_artifact_rejected(tmp_path):
    rc, _, err = _run_emit([
        "--repo", str(tmp_path), "emit",
        "--phase", "plan_done", "--stakes", "moderate",
        "--task", "t-empty", "--artifact", "   \n\t  ",
    ])
    assert rc == 2, f"empty artifact should exit 2, got {rc}"
    assert "empty" in err.lower()


def test_f3_allow_empty_artifact_bypass(tmp_path):
    rc, _, _ = _run_emit([
        "--repo", str(tmp_path), "--json", "emit",
        "--phase", "plan_done", "--stakes", "moderate",
        "--task", "t-allow-empty", "--artifact", "  ",
        "--allow-empty-artifact",
    ])
    assert rc == 0, f"--allow-empty-artifact should bypass the guard, got {rc}"


# gem-f1 — merge_with_existing distinguishes user opt-out skip from dedup skip
def test_gemf1_user_optout_skip_beats_existing():
    user_skip = decide_mode(phase="impl_done", stakes="moderate", user_signal="别审")
    assert user_skip.mode == "skip" and user_skip.user_override_applied
    final, _ = merge_with_existing(user_skip, existing_mode="fast")
    assert final == "skip", "user explicit opt-out must win over an existing audit"


def test_gemf1_dedup_skip_yields_to_existing():
    dedup_skip = decide_mode(phase="impl_done", stakes="moderate", dedup_hit=True)
    assert dedup_skip.mode == "skip" and not dedup_skip.user_override_applied
    final, _ = merge_with_existing(dedup_skip, existing_mode="fast")
    assert final == "fast", "a dedup skip is not an opt-out; the existing audit stands"


# f2a — cmd_override must sync the matching recent_audits record
def test_f2a_override_syncs_recent_audits(tmp_path):
    artifact = "# plan\n\nmeaningful planning content here\n"
    _run_emit([
        "--repo", str(tmp_path), "emit",
        "--phase", "plan_done", "--stakes", "trivial",
        "--task", "t-override", "--artifact", artifact,
    ])
    _run_emit([
        "--repo", str(tmp_path), "override",
        "--task", "t-override", "--depth", "deep",
    ])
    state = load_state(tmp_path, "t-override")
    assert state is not None and state.recent_audits
    last_phase = state.history[-1].phase
    matching = [r for r in state.recent_audits if r.phase == last_phase]
    assert matching, "no recent_audits record for the last phase"
    assert all(r.audit_mode == "deep" for r in matching), \
        f"recent_audits not synced to override mode: {[r.audit_mode for r in matching]}"


# f5 — state_path must not collide distinct task ids
def test_f5_state_path_no_collision(tmp_path):
    assert state_path(tmp_path, "foo/bar") != state_path(tmp_path, "foo_bar")
    long_a = "x" * 80 + "AAAA"
    long_b = "x" * 80 + "BBBB"
    assert state_path(tmp_path, long_a) != state_path(tmp_path, long_b), \
        "ids differing only past char 80 must not share a state file"


def test_f5_state_isolation_under_collision_prone_ids(tmp_path):
    save_state(tmp_path, PhaseState(task_id="foo/bar", current_phase="plan_done"))
    save_state(tmp_path, PhaseState(task_id="foo_bar", current_phase="impl_done"))
    assert load_state(tmp_path, "foo/bar").current_phase == "plan_done"
    assert load_state(tmp_path, "foo_bar").current_phase == "impl_done"


# gem-f2 — save_state normal-path regression (unique temp; no fixed .tmp leftover)
def test_gemf2_save_state_roundtrips_no_fixed_tmp(tmp_path):
    p = save_state(tmp_path, PhaseState(task_id="t-tmp", current_phase="impl_done"))
    assert p.exists()
    assert _json.loads(p.read_text(encoding="utf-8"))["task_id"] == "t-tmp"
    leftovers = [f for f in p.parent.iterdir() if f != p]
    assert not leftovers, f"temp file leaked beside the state file: {leftovers}"


# ---- Enum migration guards (audit-mode-enum-migration, 2026-06-04) ----------


def test_rank_dicts_in_lockstep():
    """router.DEPTH_RANK (new enum) and dedup._DEPTH_RANK MUST agree on every
    new-enum key — the dedup rank-gate and the safety floor depend on ONE rank
    ordering. dedup additionally carries legacy aliases (single/two/three) for
    the upgrade-window state files; router must NOT (it only ever produces new
    enum values). (Construction-ledger objection #2 + audit acffde79 f1/f2.)"""
    from aqg_phase_dedup import _DEPTH_RANK
    for mode, rank in DEPTH_RANK.items():
        assert _DEPTH_RANK.get(mode) == rank, (
            f"rank drift for {mode!r}: router={rank} dedup={_DEPTH_RANK.get(mode)}"
        )
    assert set(DEPTH_RANK) == {"skip", "fast", "standard", "deep"}, (
        f"router DEPTH_RANK must be new-enum only: {set(DEPTH_RANK)}"
    )


def test_matrix_values_are_new_enum_only():
    """No legacy mode value leaks into the matrix after the fast/standard/deep
    migration; every matrix value is a known rank."""
    legacy = {"single", "two", "three"}
    for phase, row in MATRIX.items():
        for stakes, mode in row.items():
            assert mode not in legacy, f"legacy mode {mode!r} at MATRIX[{phase}][{stakes}]"
            assert mode in DEPTH_RANK, f"unknown mode {mode!r} at MATRIX[{phase}][{stakes}]"


def test_high_column_is_deep_directly_not_floor_rescued():
    """Construction-ledger objection #1: every high-stakes cell is `deep` as the
    matrix DEFAULT (not standard rescued by the floor), so safety_floor_applied
    stays False on a plain high emit."""
    for phase in MATRIX:
        d = decide_mode(phase=phase, stakes="high", user_signal=None, dedup_hit=False)
        assert d.matrix_default == "deep", f"{phase} high default {d.matrix_default!r} != deep"
        assert d.safety_floor_applied is False, f"{phase} high spuriously flagged floor"


def test_legacy_mode_value_in_records_does_not_crash():
    """A pre-existing .aqg/phase-state record written under the OLD enum
    ('two'/'three') must not crash the dedup rank lookup after migration, AND a
    stricter new request must still bypass it. Legacy 'two' maps to its new-enum
    rank (standard=2), so a stricter deep (rank 3) request bypasses dedup.
    (Construction-ledger objection #3 + audit acffde79 f1/f2.)"""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    legacy_rec = RecentAuditRecord(
        task_id="t-legacy",
        content_hash="h-legacy",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="two",  # legacy value persisted before migration
    )
    # requested deep (rank 3) > legacy "two" (rank 2 via legacy alias) → bypass.
    res = check_dedup([legacy_rec], task_id="t-legacy", content_hash="h-legacy",
                      phase="impl_done", requested_mode="deep", now=now)
    assert res.is_hit is False


def test_legacy_two_record_dedups_equivalent_standard_request():
    """audit acffde79 f1/f2 (convergent): an OLD 'two' record (≡ new standard)
    must DEDUP an equivalent new 'standard' request during the upgrade window —
    legacy 'two' keeps rank 2, so standard (rank 2) <= recorded (rank 2) → hit.
    Without the legacy alias it would fall back to rank 0 and spuriously re-emit."""
    from datetime import datetime, timezone, timedelta
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    legacy_rec = RecentAuditRecord(
        task_id="t-legacy2",
        content_hash="h-legacy2",
        phase="impl_done",
        audited_at=(now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="two",
    )
    res = check_dedup([legacy_rec], task_id="t-legacy2", content_hash="h-legacy2",
                      phase="impl_done", requested_mode="standard", now=now)
    assert res.is_hit is True, "legacy 'two'≡standard must dedup an equivalent standard request"
    # legacy 'three' ≡ deep keeps rank 3 → a deep request dedups too.
    legacy_three = RecentAuditRecord(
        task_id="t-legacy3", content_hash="h3", phase="impl_done",
        audited_at=(now - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        audit_mode="three",
    )
    res3 = check_dedup([legacy_three], task_id="t-legacy3", content_hash="h3",
                       phase="impl_done", requested_mode="deep", now=now)
    assert res3.is_hit is True, "legacy 'three'≡deep must dedup an equivalent deep request"


def test_router_depth_matches_the_policy_single_source():
    """The router must not be a second depth authority.

    Its phase x stakes matrix used to route a trivial change to `standard`, while
    docs/policies/audit-trigger.md rung 1 says a trivial non-sensitive change is
    not audited at all. Same situation, opposite answers — and since the decision
    model takes the DEEPER of two signals, the matrix silently won. Owner ruling
    2026-08-11: the policy is the single depth authority.
    """
    expected = depth_by_stakes(_REPO)

    for phase in ("plan_done", "impl_done", "tests_written"):
        for stakes, depth in expected.items():
            d = decide_mode(phase=phase, stakes=stakes, user_signal=None, dedup_hit=False)
            assert d.mode == depth, (
                f"{phase} x {stakes}: router says {d.mode}, policy says {depth} — "
                "the router has become a second depth authority again"
            )


def test_skill_md_depth_table_matches_the_policy():
    """The convenience copy in SKILL.md must not drift either.

    The SKILL.md text claimed "the policy wins, and
    test_router_depth_matches_the_policy_single_source fails if they diverge" —
    which was false: that test compares the ROUTER to the policy and never reads
    SKILL.md. The claim shipped to users in an installed skill. Now it is true.
    """
    import re

    expected = depth_by_stakes(_REPO)

    copies = [
        _REPO / "skills" / "aqg-phase-transition" / "SKILL.md",
        _REPO / "agent-packs" / "claude-code" / "skills" / "aqg-phase-transition" / "SKILL.md",
    ]
    for path in copies:
        text = path.read_text(encoding="utf-8")
        for stakes, depth in expected.items():
            row = re.search(rf"^\|\s*{stakes}[^|]*\|\s*`([a-z]+)`", text, re.MULTILINE)
            assert row, f"{path.name}: no depth row for {stakes}"
            assert row.group(1) == depth, (
                f"{path.name}: {stakes} says {row.group(1)}, policy says {depth}"
            )
