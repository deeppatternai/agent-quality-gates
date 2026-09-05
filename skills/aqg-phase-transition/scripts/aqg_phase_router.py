#!/usr/bin/env python3
"""Stakes → audit depth router (with safety floor + user override).

Pure decision logic, no I/O. This is the development source; the cloud release
form (aqg_cloud/skills/phase_transition.py) mirrors it — tests guard cross-impl
consistency.

Depth is not decided here. `docs/policies/audit-trigger.md` is the single depth
authority (Owner ruling 2026-08-11): `DEPTH_BY_STAKES` below is a constant copy
of its `depth-by-stakes` marker, held in step by
test_router_depth_matches_the_policy_single_source. Phase is a TIMING signal
only — it answers "should I be asking now?", never "how deep" — so every phase
maps identically.

Per ADR `2026-05-09-phase-transition-audit-trigger-a1.md` (enum re-aligned to
audit-mcp fast/standard/deep, Owner 2026-06-04), what this module DOES own:
- Safety floor: high stakes cannot drop below deep (even if the user says "快速扫")
- User signal language → override the policy depth (within the safety floor)
- Dedup hit → forced "skip"

This docstring used to advertise a phase-dependent matrix the code had already
dropped (see DEPTH_BY_STAKES for why it went), in the exact framing
`aqg_doctor.RETIRED_RULES_MARKERS` fails an installed rules block for — the
router was flunking its own doctor in prose. Held by
tests/behavior/test_depth_authority_is_single_sourced.py.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

# ---- Matrix --------------------------------------------------------------

Phase = Literal["plan_done", "impl_done", "tests_written"]
Stakes = Literal["trivial", "moderate", "high"]
Mode = Literal["skip", "fast", "standard", "deep"]

# Per ADR §2 matrix (Owner 2026-06-04 enum re-alignment to audit-mcp fast/standard/deep).
# Legacy → new: fast→fast · single→standard · two→standard · three→deep.
# The `high` column is filled `deep` DIRECTLY (not `standard` rescued by the floor):
# floor=deep means a high-stakes matrix default below deep would contradict its own
# floor and spuriously flag safety_floor_applied on every high emit.
# Depth comes from docs/policies/audit-trigger.md, which is the single authority
# (Owner ruling 2026-08-11). Phase is a TIMING signal only: a trivial change is
# trivial at PLAN_DONE and at IMPL_DONE alike, so every phase maps identically.
#
# This used to be a phase x stakes table that routed trivial -> standard, which
# contradicted the policy's rung 1 (trivial and non-sensitive -> do not audit).
# Because the decision model takes the DEEPER of two signals, the table silently
# won, making it a second source of the over-firing this work removed. Kept as a
# constant rather than parsed at runtime so the router cannot fail when the policy
# file is unreachable; test_router_depth_matches_the_policy_single_source asserts
# the two agree, so drift is a red test rather than a silent divergence.
DEPTH_BY_STAKES: dict[Stakes, Mode] = {
    "trivial": "skip",
    "moderate": "standard",
    "high": "deep",
}

MATRIX: dict[Phase, dict[Stakes, Mode]] = {
    phase: dict(DEPTH_BY_STAKES)
    for phase in ("plan_done", "impl_done", "tests_written")
}


# Mode rank — used for "fail-safer" (max of multiple sources) + safety floor.
# New 3-tier scale (audit-mcp fast/standard/deep): skip < fast < standard < deep.
DEPTH_RANK: dict[Mode, int] = {
    "skip": 0,
    "fast": 1,
    "standard": 2,
    "deep": 3,
}

RANK_TO_MODE: dict[int, Mode] = {v: k for k, v in DEPTH_RANK.items()}

# Safety floor: high stakes never below deep (Owner 2026-06-04). High stakes is
# irreversible-class; user "快速扫" cannot drop it (explicit skip opt-out still wins).
HIGH_STAKES_FLOOR_RANK = DEPTH_RANK["deep"]


# ---- User signal parsing -------------------------------------------------

# Maps natural-language fragments (English AND Chinese) to a target Mode
# (audit-mcp fast/standard/deep). Both languages are recognized so the tool works
# for English- and Chinese-speaking users alike.
# - "skip" / "no audit" / "别审" / "我自己拍板" → skip
# - "fast" / "quick scan" / "快速扫一下" / "随便看看" → fast
# - "review it" / "cross-check" / "看看" / "审一下" / "交叉验证" → standard (dual-mainstream 2 models)
# - "deep" / "strict" / "thorough" / "before prod" / "严格审" / "深审" / "挖深" → deep (6 models)
# Legacy Chinese tier words 二审/三审/双审 are retired and deliberately NOT matched
# (the model is fast/standard/deep only). ASCII "two"/"three" fragments are also not
# matched (false-match "two files" risk + not a mode).
USER_SIGNAL_TABLE: list[tuple[str, Mode]] = [
    # Skip — most specific first (opt out of the audit). Legacy Chinese tier words
    # 二审/三审/双审 are retired and NOT recognized (only fast/standard/deep now).
    ("我自己拍板", "skip"),
    ("自己看着办", "skip"),
    ("别审", "skip"),
    ("不用审", "skip"),
    ("先别审", "skip"),
    ("直接执行", "skip"),
    ("no audit", "skip"),
    ("skip audit", "skip"),
    ("skip the audit", "skip"),
    ("don't audit", "skip"),
    ("no review", "skip"),
    ("i'll decide", "skip"),
    ("my call", "skip"),
    ("just proceed", "skip"),
    # Deep (strictest — 6 models)
    ("深审", "deep"),
    ("严格审", "deep"),
    ("严格", "deep"),
    ("挖深", "deep"),
    ("上 prod 前再过一遍", "deep"),
    ("上prod前再过一遍", "deep"),
    ("strict", "deep"),
    ("rigorous", "deep"),
    ("thorough", "deep"),
    ("deep audit", "deep"),
    ("deep review", "deep"),
    ("go deep", "deep"),
    ("before prod", "deep"),
    ("before production", "deep"),
    ("extra careful", "deep"),
    ("deep", "deep"),
    # Standard (dual-mainstream 2 models — cross-check / default-audit)
    ("交叉验证", "standard"),
    ("再确认", "standard"),
    ("cross-check", "standard"),
    ("cross check", "standard"),
    ("double-check", "standard"),
    ("double check", "standard"),
    ("review it", "standard"),
    ("take a look", "standard"),
    ("look it over", "standard"),
    ("is it right", "standard"),
    ("is it correct", "standard"),
    ("second opinion", "standard"),
    ("standard", "standard"),
    ("看看", "standard"),
    ("审一下", "standard"),
    ("对不对", "standard"),
    # Fast (last so substring search hits before "standard" wins for /audit fast)
    ("快速扫一下", "fast"),
    ("快速看", "fast"),
    ("随便看看", "fast"),
    ("trivial 看看", "fast"),
    ("/audit fast", "fast"),
    ("quick scan", "fast"),
    ("quick look", "fast"),
    ("quick check", "fast"),
    ("quick pass", "fast"),
    ("sanity check", "fast"),
    ("fast", "fast"),
]


_ASCII_FRAGMENT_RE_CACHE: dict[str, "object"] = {}


def _is_ascii(s: str) -> bool:
    try:
        s.encode("ascii")
        return True
    except UnicodeEncodeError:
        return False


def _matches_signal_fragment(fragment: str, signal_lower: str) -> bool:
    """Audit fix C1 (gpt-5.5 #3 + gemini #4 convergent):
    word-boundary regex for ASCII; substring fallback for CJK (no boundaries)."""
    import re
    fragment_lower = fragment.lower()
    if _is_ascii(fragment_lower):
        cached = _ASCII_FRAGMENT_RE_CACHE.get(fragment_lower)
        if cached is None:
            cached = re.compile(r"\b" + re.escape(fragment_lower) + r"\b")
            _ASCII_FRAGMENT_RE_CACHE[fragment_lower] = cached
        return bool(cached.search(signal_lower))
    return fragment_lower in signal_lower


# Audit fix f1 (audit c03e5465): negation tokens that, when they immediately
# precede a skip fragment, flip its meaning ("do not skip", "不要别审"). Checked
# only against the contiguous run right before the fragment (stops at
# punctuation) so a separate skip phrase like "不用审, 自己看着办" is unaffected.
_SKIP_NEGATION_RE = re.compile(r"do(es)?\s*not|n['’]?t\b|never|\bnot\b|不|勿|非|莫|甭")
_RUN_SPLIT_RE = re.compile(r"[,;。，；、!?！？\n]")


def parse_user_signal(signal: str | None) -> Mode | None:
    """Map a natural-language user signal to a Mode override.

    Audit fix C1 (audit 030b51d5): substring matching previously fired on
    "skipping" / "breakfast" / "do not skip"; now uses word-boundary regex
    for ASCII fragments, substring for CJK.

    Audit fix f1 (audit c03e5465): a skip fragment negated immediately before it
    ("do not skip audit", "不要别审") is NOT an opt-out. Fail-safer — drop the
    skip match and let the matrix + safety floor decide, never silently skip a
    high-stakes audit the user explicitly asked NOT to skip.
    """
    if not signal:
        return None
    lowered = signal.lower().strip()
    if not lowered:
        return None
    for fragment, mode in USER_SIGNAL_TABLE:
        if not _matches_signal_fragment(fragment, lowered):
            continue
        if mode == "skip":
            idx = lowered.find(fragment.lower())
            if idx > 0:
                run = _RUN_SPLIT_RE.split(lowered[:idx])[-1]
                if _SKIP_NEGATION_RE.search(run):
                    continue  # negated skip — not an opt-out
        return mode
    return None


# ---- Decision ------------------------------------------------------------

@dataclass
class RouterDecision:
    """Output of decide_mode()."""

    mode: Mode
    reason: str
    matrix_default: Mode  # what the matrix would have picked alone
    user_override_applied: bool  # True if user_signal changed the mode
    safety_floor_applied: bool  # True if high-stakes safety floor raised the mode
    # issue #282: True when a HIGH-stakes audit is being skipped via an explicit
    # user opt-out. mode stays "skip" (ADR §4 invariant 4 — opt-out wins), but
    # this flags that the caller MUST get a second explicit confirmation before
    # honoring the skip. False for moderate/trivial opt-out skips and dedup skips.
    high_stakes_skip_confirm: bool = False


def decide_mode(
    phase: Phase,
    stakes: Stakes,
    user_signal: str | None = None,
    dedup_hit: bool = False,
    explicit_override: Mode | None = None,
) -> RouterDecision:
    """Resolve recommended audit mode given phase, stakes, optional user signal.

    Order of precedence:
    1. dedup_hit → forced "skip" (separate code path; respects nothing else)
    2. explicit_override (programmatic) → applied with safety floor
    3. user_signal → parse + apply with safety floor
    4. matrix default → applied with safety floor

    Safety floor: high stakes cannot be lowered below "deep" by a lower-depth
    audit request (e.g. "快速扫"→fast). An explicit skip / no-audit opt-out still
    wins (per ADR §4 invariant 4) — the floor lifts under-depth requests, it does
    NOT force an audit the user explicitly declined. Phase-transition is a
    fail-safer mechanism, not a user-bypass tool.
    """
    matrix_default = MATRIX[phase][stakes]

    if dedup_hit:
        return RouterDecision(
            mode="skip",
            reason="dedup hit (task-id + content-hash within 5 min window)",
            matrix_default=matrix_default,
            user_override_applied=False,
            safety_floor_applied=False,
        )

    # Resolve initial pick
    if explicit_override is not None:
        chosen: Mode = explicit_override
        source = "explicit_override"
    elif user_signal is not None and (parsed := parse_user_signal(user_signal)) is not None:
        chosen = parsed
        source = f"user_signal={user_signal!r}"
    else:
        chosen = matrix_default
        source = f"matrix[{phase}][{stakes}]"

    user_override_applied = source != f"matrix[{phase}][{stakes}]"

    # Per ADR §4 invariant 4: user explicit opt-out (skip) wins — phase-transition
    # does NOT override "I don't want an audit". Safety floor only catches user
    # who *did* want some audit but at lower depth than safe (e.g. "快速扫" on
    # high stakes → bumped to deep, not skip → deep).
    if chosen == "skip" and source != f"matrix[{phase}][{stakes}]":
        # issue #282: a high-stakes opt-out skip is still honored (invariant 4),
        # but flagged so the caller re-confirms before skipping a high-stakes audit.
        needs_confirm = stakes == "high"
        reason = (
            f"user explicit opt-out (skip via {source}); "
            "phase-transition respects opt-out per ADR §4 invariant 4. "
            "Caller should record warning to log."
        )
        if needs_confirm:
            reason += (
                " HIGH-STAKES audit skipped — caller MUST obtain a second explicit "
                "user confirmation before honoring this skip (issue #282)."
            )
        return RouterDecision(
            mode="skip",
            reason=reason,
            matrix_default=matrix_default,
            user_override_applied=True,
            safety_floor_applied=False,
            high_stakes_skip_confirm=needs_confirm,
        )

    # Safety floor for high stakes (applies to non-skip downgrades only).
    safety_floor_applied = False
    if stakes == "high" and DEPTH_RANK[chosen] < HIGH_STAKES_FLOOR_RANK:
        chosen = RANK_TO_MODE[HIGH_STAKES_FLOOR_RANK]
        safety_floor_applied = True
        reason = (
            f"safety floor applied: stakes=high requires ≥deep; "
            f"raised from {source} → {chosen}"
        )
    else:
        reason = f"selected via {source}"

    return RouterDecision(
        mode=chosen,
        reason=reason,
        matrix_default=matrix_default,
        user_override_applied=user_override_applied,
        safety_floor_applied=safety_floor_applied,
    )


def merge_with_existing(
    phase_decision: RouterDecision,
    existing_mode: Mode | None,
) -> tuple[Mode, str]:
    """Combine a phase-transition decision with an existing audit decision
    (e.g. user already said "审一下" before phase fires).

    Per ADR §4 / unchanged invariants:
    - A user EXPLICIT opt-out (skip via user_signal, i.e. user_override_applied)
      is honored even against an existing audit — it is an opt-out, not a depth.
    - A dedup skip (user_override_applied False) is NOT an opt-out and yields to
      the existing audit.
    - Otherwise take the deeper (higher-rank) mode (fail-safer).

    NOTE: this fn is not yet wired into the CLI (emit / override / query); only
    tests exercise it. Kept correct so a future wiring is safe.
    """
    # gem-f1 (audit c03e5465): distinguish a user opt-out skip from a dedup skip.
    if phase_decision.mode == "skip" and phase_decision.user_override_applied:
        return "skip", f"user explicit opt-out honored over existing ({existing_mode})"
    if existing_mode is None or existing_mode == "skip":
        return phase_decision.mode, f"phase-only: {phase_decision.reason}"
    if phase_decision.mode == "skip":
        return existing_mode, f"existing audit ({existing_mode}); phase skip is dedup, not opt-out"
    if DEPTH_RANK[existing_mode] >= DEPTH_RANK[phase_decision.mode]:
        return existing_mode, f"existing audit ({existing_mode}) ≥ phase ({phase_decision.mode}); existing wins"
    return phase_decision.mode, f"phase ({phase_decision.mode}) > existing ({existing_mode}); fail-safer"
