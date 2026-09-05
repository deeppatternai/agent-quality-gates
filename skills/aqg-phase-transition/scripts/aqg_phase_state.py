#!/usr/bin/env python3
"""Phase state machine + .aqg/phase-state.json persistence.

Per ADR §3 ambiguity 2 (Owner's ruling) + audit c03e5465 (f4/f5): a per-task file
`<repo>/.aqg/phase-state-<safe>-<digest>.json` keyed by task_id for concurrent
tasks (the 8-char digest of the raw task_id disambiguates ids that sanitize to
the same safe string).

State stored:
- task_id (str)
- current_phase (one of plan_done / impl_done / tests_written / done; or None for fresh task)
- history: list of {phase, at, content_hash} (transitions)
- recent_audits: list of RecentAuditRecord (5-min sliding cache for dedup)

Transition order (preferred): plan_done → impl_done → tests_written → done.
Out-of-order is a WARNING (not block) — fail-loud per ADR §"How to apply".
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

# Make sibling-script imports work when run as `python3 aqg_phase_state.py`
# from the scripts/ directory (same trick mirrored in aqg_phase_emit.py).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from aqg_phase_dedup import RecentAuditRecord  # noqa: E402


Phase = Literal["plan_done", "impl_done", "tests_written", "done"]
PHASE_ORDER: list[Phase] = ["plan_done", "impl_done", "tests_written", "done"]


@dataclass
class PhaseTransition:
    """One row in PhaseState.history."""

    phase: Phase
    at: str  # ISO 8601 UTC
    content_hash: str
    stakes: str  # trivial / moderate / high
    audit_mode: str  # the mode that was emitted as recommendation (or actually audited)

    def to_dict(self) -> dict:
        return {
            "phase": self.phase,
            "at": self.at,
            "content_hash": self.content_hash,
            "stakes": self.stakes,
            "audit_mode": self.audit_mode,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseTransition":
        return cls(
            phase=d["phase"],
            at=d["at"],
            content_hash=d["content_hash"],
            stakes=d.get("stakes", "moderate"),
            audit_mode=d.get("audit_mode", "standard"),
        )


@dataclass
class PhaseState:
    """One phase state machine instance for one task_id."""

    task_id: str
    current_phase: Phase | None = None
    history: list[PhaseTransition] = field(default_factory=list)
    recent_audits: list[RecentAuditRecord] = field(default_factory=list)
    schema_version: int = 1

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "current_phase": self.current_phase,
            "history": [h.to_dict() for h in self.history],
            "recent_audits": [r.to_dict() for r in self.recent_audits],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PhaseState":
        return cls(
            schema_version=d.get("schema_version", 1),
            task_id=d["task_id"],
            current_phase=d.get("current_phase"),
            history=[PhaseTransition.from_dict(h) for h in d.get("history", [])],
            recent_audits=[RecentAuditRecord.from_dict(r) for r in d.get("recent_audits", [])],
        )


# ---- Transition validity --------------------------------------------------

def is_valid_transition(from_phase: Phase | None, to_phase: Phase) -> tuple[bool, str]:
    """Per ADR: out-of-order transitions are WARNINGS, not blocks. Returns
    (is_valid, reason). is_valid=True means the transition is in canonical
    order; False means it's a skip / regression / repeat (still allowed).
    """
    if from_phase is None:
        # First emission for this task — any phase is OK; canonical is plan_done first
        if to_phase == "plan_done":
            return True, "fresh task, plan_done first (canonical)"
        return False, f"fresh task started at {to_phase!r} (canonical is plan_done first)"
    if from_phase == to_phase:
        return False, f"already at {to_phase!r}; re-emit (allowed; dedup may still apply)"
    try:
        from_idx = PHASE_ORDER.index(from_phase)
        to_idx = PHASE_ORDER.index(to_phase)
    except ValueError:
        return False, f"unknown phase value(s): from={from_phase} to={to_phase}"
    if to_idx == from_idx + 1:
        return True, f"canonical: {from_phase} → {to_phase}"
    if to_idx > from_idx + 1:
        skipped = PHASE_ORDER[from_idx + 1:to_idx]
        return False, f"skipped phase(s) {skipped}: {from_phase} → {to_phase}"
    return False, f"regression: {from_phase} → {to_phase} (going backwards)"


# ---- File I/O -------------------------------------------------------------

STATE_FILENAME = "phase-state.json"
STATE_DIR = ".aqg"


def state_path(repo_root: Path, task_id: str) -> Path:
    """Per-task state file: <repo>/.aqg/phase-state-<safe>-<digest>.json.

    Why per-task: ADR §3.2 ambiguity 2 — concurrent tasks supported via
    task_id. File-per-task avoids RW races on a single shared JSON.

    f5 (audit c03e5465): append an 8-char sha256 of the RAW task_id so distinct
    ids that sanitize/truncate to the same `safe` string never share a file
    (e.g. "foo/bar" vs "foo_bar", or ids differing only past char 80).
    """
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_id)[:80] or "default"
    digest = hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:8]
    return repo_root / STATE_DIR / f"phase-state-{safe}-{digest}.json"


def load_state(repo_root: Path, task_id: str) -> PhaseState | None:
    """Read state file; return None if missing / unreadable."""
    p = state_path(repo_root, task_id)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or data.get("task_id") != task_id:
        return None
    return PhaseState.from_dict(data)


def save_state(repo_root: Path, state: PhaseState) -> Path:
    """Atomic-write state file. Creates `.aqg/` dir if missing.

    gem-f2 (audit c03e5465): use a unique temp filename (pid + random token) so
    a concurrent same-task write cannot clobber another's temp file; the final
    os.replace stays atomic.
    """
    p = state_path(repo_root, state.task_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.parent / f"{p.name}.tmp.{os.getpid()}.{secrets.token_hex(4)}"
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(state.to_dict(), f, indent=2, ensure_ascii=False)
    tmp.replace(p)
    return p
