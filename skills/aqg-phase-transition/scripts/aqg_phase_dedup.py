#!/usr/bin/env python3
"""Content-hash + 5-min TTL dedup for phase-transition audit triggers.

Per ADR §3 ambiguity 4 + 6 (Owner's final call) + audit c03e5465 (f4 doc-sync):
- content-hash = sha256 of normalize_whitespace(artifact)
- key = (task_id, content_hash, phase) — phase is part of the key so distinct
  sequential transitions with the same artifact do NOT dedup
- requested-mode rank gate: a later request for a STRICTER mode than the recorded
  one bypasses dedup (a deeper audit is never suppressed)
- TTL = 300 seconds (5 min) sliding window
- a record stores an EMITTED RECOMMENDATION, not a confirmed audit (the caller
  owns running de_audit per the ADR §5 signal-only boundary)

Pure logic, no file I/O — caller passes existing audit list, we filter +
return updated list.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


# ---- Content hash ---------------------------------------------------------

_WHITESPACE_RE = re.compile(r"\s+")


def normalize_artifact(artifact: str) -> str:
    """Whitespace-normalize then sha256.

    Per ADR §3 ambiguity 4: collapse all runs of whitespace (incl. newlines /
    tabs) to a single space; strip leading/trailing whitespace. Preserves all
    non-whitespace characters verbatim.

    This means trivial reformatting (extra blank lines, indentation diffs,
    different newline conventions) yields the same hash → 5-min dedup
    correctly catches "I tweaked formatting and re-submitted".
    """
    if not artifact:
        return ""
    return _WHITESPACE_RE.sub(" ", artifact).strip()


def hash_artifact(artifact: str) -> str:
    """Normalize whitespace then sha256 hex digest (full 64 chars)."""
    normalized = normalize_artifact(artifact)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---- Audit record + dedup --------------------------------------------------

DEDUP_TTL_SECONDS = 300  # 5 min


@dataclass
class RecentAuditRecord:
    """One row in `.aqg/phase-state.json`'s `recent_audits` list."""

    task_id: str
    content_hash: str
    phase: str  # plan_done / impl_done / tests_written
    audited_at: str  # ISO 8601 UTC
    audit_mode: str  # the mode the caller actually ran (or "skip" if skipped)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "content_hash": self.content_hash,
            "phase": self.phase,
            "audited_at": self.audited_at,
            "audit_mode": self.audit_mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RecentAuditRecord":
        return cls(
            task_id=d["task_id"],
            content_hash=d["content_hash"],
            phase=d["phase"],
            audited_at=d["audited_at"],
            audit_mode=d["audit_mode"],
        )


@dataclass
class DedupResult:
    """Output of check_dedup()."""

    is_hit: bool
    matched_record: RecentAuditRecord | None = None
    reason: str = ""


def _parse_iso(ts: str) -> datetime | None:
    """Parse ISO 8601 timestamp; return None on bad format."""
    try:
        # Python 3.12+ fromisoformat handles the typical 'Z' / '+00:00' both
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def filter_expired(
    records: list[RecentAuditRecord],
    now: datetime | None = None,
    ttl_seconds: int = DEDUP_TTL_SECONDS,
) -> list[RecentAuditRecord]:
    """Drop records older than TTL. Pure (returns new list)."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(seconds=ttl_seconds)
    return [r for r in records if (parsed := _parse_iso(r.audited_at)) is not None and parsed >= cutoff]


# New-enum ranks MUST match aqg_phase_router.DEPTH_RANK (test asserts the new-enum
# subset is identical). Legacy aliases (single/two/three) are added HERE only — not
# in router — so a `.aqg/phase-state` record written under the OLD enum during the
# upgrade window still dedups at its new-enum-equivalent rank (single/two→standard,
# three→deep) instead of falling back to rank 0 (which would spuriously bypass the
# 5-min dedup for an equivalent new request). New 3-tier: skip < fast < standard < deep.
_DEPTH_RANK = {
    "skip": 0, "fast": 1, "standard": 2, "deep": 3,
    "single": 2, "two": 2, "three": 3,  # legacy upgrade-window aliases (read-only)
}


def check_dedup(
    records: list[RecentAuditRecord],
    task_id: str,
    content_hash: str,
    phase: str | None = None,
    requested_mode: str | None = None,
    now: datetime | None = None,
    ttl_seconds: int = DEDUP_TTL_SECONDS,
) -> DedupResult:
    """Audit fixes (audit 030b51d5):
    - gemini #2: include `phase` in match key (distinct sequential transitions
      with same artifact must NOT dedup).
    - gpt-5.5 #2: requested_mode rank must be ≤ recorded audit_mode rank for
      dedup to qualify. Stricter user request bypasses dedup.

    Backward-compat: phase / requested_mode optional — when None, fall back
    to legacy task+hash matching.
    """
    now = now or datetime.now(timezone.utc)
    fresh = filter_expired(records, now=now, ttl_seconds=ttl_seconds)
    requested_rank = _DEPTH_RANK.get(requested_mode or "", 0) if requested_mode else 0
    for r in fresh:
        if r.task_id != task_id or r.content_hash != content_hash:
            continue
        if phase is not None and r.phase != phase:
            continue
        if requested_mode is not None:
            prev_rank = _DEPTH_RANK.get(r.audit_mode, 0)
            if prev_rank < requested_rank:
                continue
        return DedupResult(
            is_hit=True,
            matched_record=r,
            reason=(
                f"matched task={task_id!r} + content_hash={content_hash[:16]}... "
                f"+ phase={phase!r} audited at {r.audited_at} as mode={r.audit_mode!r}"
            ),
        )
    return DedupResult(is_hit=False, reason="no in-window match")


def append_audit_record(
    records: list[RecentAuditRecord],
    new_record: RecentAuditRecord,
    now: datetime | None = None,
    ttl_seconds: int = DEDUP_TTL_SECONDS,
    cap: int = 64,
) -> list[RecentAuditRecord]:
    """Add a new record + drop expired. Caps at most `cap` to prevent unbounded
    growth in long-running tasks.
    """
    fresh = filter_expired(records, now=now, ttl_seconds=ttl_seconds)
    fresh.append(new_record)
    if len(fresh) > cap:
        fresh = fresh[-cap:]
    return fresh
