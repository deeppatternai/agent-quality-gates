"""Apply a plan as a recoverable transaction — the layer that owns policy.

docs/UPDATE_ARCHITECTURE.md §5. Everything below this is mechanics: the lock
knows how to exclude, the dispatcher knows how to act, the staging layer knows
how to swap. This module decides *whether*, *in what order*, and *what to do
when it goes wrong*.

**The journal exists for the case the happy path cannot exercise: the process
dies mid-apply.** Each phase is written before it is entered, never after, so a
crash between writing and acting leaves the more pessimistic record. What is on
disk afterwards has to say enough for the next run to know whether to resume,
undo, or stop and ask for a human — and a journal that is *present* is itself
the signal that something was in flight.

**What this layer can undo is narrow, and stated rather than implied.** The root
activation is invertible, because the dispatcher hands back the version it
replaced. Skill routes are not: nothing here re-derives which of them existed
before. So a failure that only touched the root is a ``rolled-back``, and a
failure after a route landed is a ``repair-required`` with the journal left in
place — reporting a half-applied roster as rolled back would be false, and the
distinction is the whole reason the two words are different.

Not here, and deliberately: acquiring and verifying a release (PR6), and writing
install state. ``record_state`` stays deferred — the state document's per-host
roster is not this slice's to compose.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import dispatch, lock, stage
    from scripts.aqg_update.plan import Plan
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import dispatch, lock, stage  # type: ignore[no-redef]
    from aqg_update.plan import Plan  # type: ignore[no-redef]

JOURNAL_FILENAME = "update-journal.json"

#: Phases a journal may record, borrowed from DE's transaction vocabulary and
#: trimmed to the ones this slice can actually reach. A phase outside this set
#: is a refusal: an unrecognized journal is not an empty one. Every name here is
#: written by some path below — a phase nothing produces is vocabulary pretending
#: to be a contract.
JOURNAL_PHASES = ("applying", "smoking", "rolling_back", "repair_required")


#: Action kinds this layer can put back. Anything applied that is NOT here makes
#: the run repair-required — an allowlist, so a vocabulary that grows fails
#: toward repair rather than toward a claim of cleanliness.
INVERTIBLE_KINDS = frozenset({"activate_root"})


class TransactionError(RuntimeError):
    """A refusal to apply. Always fail-closed."""


@dataclass(frozen=True)
class Result:
    status: str
    outcomes: Tuple[dispatch.Outcome, ...] = ()
    detail: str = ""


def journal_path() -> Path:
    """Where the journal lives — beside install state, outside the checkout."""
    from scripts.aqg_update import state  # local: avoids a cycle at import time

    return state.state_root(create=True) / JOURNAL_FILENAME


def _fsync_dir(directory: Path) -> None:
    """Make a rename or unlink durable.

    Without this the file content is synced but the directory entry that makes
    it visible is not, so a power loss can lose the journal while the symlink
    mutations it describes survive — and an absent journal reads as "nothing was
    in flight", which is the one conclusion that must never be reached wrongly.
    """
    try:
        fd = os.open(str(directory), os.O_RDONLY)
    except OSError:
        return  # not all platforms allow opening a directory
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_journal(path: Path, payload: Dict[str, Any]) -> None:
    """Atomically record a phase. Written BEFORE the phase is entered."""
    payload = {**payload, "at": datetime.now(timezone.utc).isoformat()}
    fd, tmp = tempfile.mkstemp(prefix=".aqg-journal-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except BaseException:  # aqg: top-level boundary
        # Cleanup only — the exception is re-raised. A temp journal left behind
        # would be litter beside the file the next run inspects.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_journal(path: Path) -> Optional[Dict[str, Any]]:
    """Return the in-flight journal, or ``None`` when there is none.

    Absence means nothing was in flight. **Unreadable does not**: it means
    something was, and we cannot tell what, so it raises. Reading the two alike
    is how a half-applied update gets quietly overwritten.
    """
    path = Path(path)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise TransactionError(f"cannot read the update journal at {path}: {exc}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise TransactionError(
            f"the update journal at {path} is unreadable ({exc}); a previous "
            f"update was in flight and its outcome cannot be determined"
        ) from exc
    if not isinstance(payload, dict) or payload.get("phase") not in JOURNAL_PHASES:
        raise TransactionError(
            f"the update journal at {path} records an unknown phase "
            f"{payload.get('phase') if isinstance(payload, dict) else payload!r}; "
            f"this AQG cannot interpret it"
        )
    return payload


def _clear_journal(path: Path) -> None:
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise TransactionError(f"cannot clear the update journal at {path}: {exc}") from exc
    _fsync_dir(path.parent)


def apply_plan(
    plan: Plan,
    *,
    resources: dispatch.Resources,
    journal: Optional[Path] = None,
    lock_path: Optional[Path] = None,
    smoke: Optional[Callable[[], bool]] = None,
    record_state: Optional[Callable[[], None]] = None,
    precondition: Optional[Callable[[], None]] = None,
) -> Result:
    """Take the lock, journal, apply, verify, and commit or undo.

    ``record_state`` writes what is now installed, and it runs **here** rather
    than in the dispatcher because this is the layer that holds the lock and
    knows whether the apply committed — which is what the dispatcher's deferral
    of ``record_state`` said all along, and what nothing had implemented. The
    consequence of that gap was not cosmetic: ``installed_sequence`` came from a
    file nobody wrote, so every anti-rollback comparison ran against
    ``FIRST_INSTALL`` and a correctly signed OLD release was accepted anywhere.

    Returns rather than raises for the outcomes a caller must handle routinely:
    ``busy`` when another apply holds the lock, ``rolled-back`` when the apply
    failed and the root was restored, ``repair-required`` when something landed
    that this layer cannot invert. It raises only when it will not start —
    a leftover journal, or an unreadable one.
    """
    target_journal = Path(journal) if journal is not None else journal_path()
    try:
        with lock.install_lock(path=lock_path):
            # Read UNDER the lock. Outside it, a second trigger firing during a
            # live apply reads that apply's own journal and reports a stale
            # previous run — the wrong answer, when the right one is "busy".
            existing = read_journal(target_journal)
            if existing is not None:
                raise TransactionError(
                    f"an update journal from a previous run is present at "
                    f"{target_journal} (phase {existing['phase']!r}); resolve it "
                    f"before planning another update, which would treat "
                    f"half-applied work as settled"
                )
            if precondition is not None:
                try:
                    precondition()
                except Exception as exc:  # aqg: top-level boundary
                    return Result(status='stale-plan', detail=f'plan refused before mutation: {exc}')
            return _apply_locked(plan, resources, target_journal, smoke, record_state)
    except lock.LockBusy:
        # Not an error: a second trigger firing is the expected case, and the
        # caller should exit quietly rather than report a problem.
        return Result(status="busy", detail="another AQG update is in progress")


def _apply_locked(
    plan: Plan,
    resources: dispatch.Resources,
    journal: Path,
    smoke: Optional[Callable[[], bool]],
    record_state: Optional[Callable[[], None]] = None,
) -> Result:
    previous_root = stage.current_target(Path(resources.root))
    base = {
        "target": str(resources.target),
        "previous_root": str(previous_root) if previous_root else None,
    }

    _write_journal(journal, {**base, "phase": "applying"})
    try:
        outcomes = dispatch.execute(plan, resources=resources, apply=True)
    except BaseException as exc:  # aqg: top-level boundary
        # The dispatcher attaches what it managed to apply for exactly this
        # moment. Letting the exception past without undoing would leave the
        # root on a version that had just failed.
        earned = tuple(getattr(exc, "aqg_outcomes", ()))
        return _undo(journal, base, earned, previous_root, resources,
                     reason=f"the apply raised {type(exc).__name__}: {exc}")
    failed = [o for o in outcomes if o.status == "failed"]

    if not failed:
        _write_journal(journal, {**base, "phase": "smoking"})
        if not _smoke_passed(smoke):
            return _undo(journal, base, outcomes, previous_root, resources,
                         reason="the new version did not pass its smoke check")

        # BEFORE the journal is cleared. A crash between "the root is live" and
        # "the state says so" leaves a machine running a version it has no
        # record of, which is exactly the condition that disables anti-rollback
        # — so the journal has to still be there saying something was in flight.
        if record_state is not None:
            try:
                record_state()
            except BaseException as exc:  # aqg: top-level boundary
                # NOT rolled back: the version is fine, the bookkeeping is not,
                # and undoing good code because a write failed is the worse
                # trade. But not `committed` either — an unrecorded install can
                # never refuse an older release, and nothing else would ever say
                # so.
                _write_journal(journal, {**base, "phase": "repair_required",
                                         "applied": _applied_record(outcomes)})
                return Result(
                    status="repair-required",
                    outcomes=outcomes,
                    detail=(
                        f"the new version is live but could not be recorded "
                        f"({exc}); until it is, this install cannot refuse an "
                        f"older release"
                    ),
                )

        _clear_journal(journal)
        return Result(status="committed", outcomes=outcomes)

    return _undo(journal, base, outcomes, previous_root, resources,
                 reason=failed[0].detail)


def _smoke_passed(smoke: Optional[Callable[[], bool]]) -> bool:
    """A smoke that raises is a failure, not a pass.

    The one that raises is exactly the one whose result matters most, so it must
    not reach the caller as an exception that skips the undo.
    """
    if smoke is None:
        return True
    try:
        return bool(smoke())
    except Exception:  # aqg: top-level boundary
        return False


def _applied_record(outcomes: Tuple[dispatch.Outcome, ...]) -> list:
    """What landed, in a form that survives the process.

    Without this the recovery information lives only in the in-process result —
    in a file whose entire purpose is outliving that process.
    """
    return [
        {"kind": o.action.kind, "subject": o.action.subject, "client": o.action.client_id}
        for o in outcomes
        if o.status == "applied"
    ]


def _undo(
    journal: Path,
    base: Dict[str, Any],
    outcomes: Tuple[dispatch.Outcome, ...],
    previous_root: Optional[Path],
    resources: dispatch.Resources,
    *,
    reason: str,
) -> Result:
    """Put back what can be put back, and be explicit about the rest."""
    applied = [o for o in outcomes if o.status == "applied"]
    applied_kinds = {o.action.kind for o in applied}
    record = {**base, "applied": _applied_record(outcomes)}

    # Allowlist, not denylist: anything applied that this layer cannot put back
    # makes the run repair-required, including a kind added after this was
    # written.
    uninvertible = applied_kinds - INVERTIBLE_KINDS
    restoring_root = "activate_root" in applied_kinds

    # Decided BEFORE the restore, so the pessimistic phase is on disk during the
    # step most likely to be interrupted.
    _write_journal(
        journal,
        {**record, "phase": "repair_required" if uninvertible else "rolling_back"},
    )

    if restoring_root:
        if previous_root is None:
            # A first install: there is nowhere to put the root back to, and
            # leaving it live while reporting a clean rollback is false on
            # exactly the axis this module claims to guard.
            _write_journal(journal, {**record, "phase": "repair_required"})
            return Result(
                status="repair-required",
                outcomes=outcomes,
                detail=(
                    f"{reason}; the root was activated for the first time, so "
                    f"there is no previous version to restore"
                ),
            )
        try:
            stage.swap_root(root=Path(resources.root), target=previous_root)
        except stage.StageError as exc:
            _write_journal(journal, {**record, "phase": "repair_required"})
            extra = (
                " and skill routes were already changed" if uninvertible else ""
            )
            return Result(
                status="repair-required",
                outcomes=outcomes,
                detail=f"{reason}; the root could not be restored: {exc}{extra}",
            )

    if uninvertible:
        # Reporting a half-applied roster as rolled back would be false. The
        # journal stays so the next run refuses rather than planning on top.
        return Result(
            status="repair-required",
            outcomes=outcomes,
            detail=(
                f"{reason}; {sorted(uninvertible)} were applied and this layer "
                f"does not invert them"
            ),
        )

    _clear_journal(journal)
    return Result(status="rolled-back", outcomes=outcomes, detail=reason)
