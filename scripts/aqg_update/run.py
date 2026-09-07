"""The update check that runs itself, and the record it leaves behind.

docs/UPDATE_ARCHITECTURE.md §10. This is the step that turns the machinery on.
It runs unattended at session start on every install, so its contract is mostly
about what it must **not** do:

* **It never raises and never exits non-zero.** A traceback out of a
  SessionStart hook is a broken shell, and a non-zero exit is a session
  reporting a problem the user has no way to act on. Every failure is caught,
  recorded, and swallowed.
* **It never writes to stdout.** A SessionStart hook's stdout is a JSON channel;
  plain text there is read as a malformed event and aborts a headless session.
  Anything worth saying goes to stderr, and the durable version goes to the
  record.
* **It is inert until a keyring ships.** There is no `release-trust.json` in the
  tree yet, so this code merges closed and the automatic channel opens on the
  day the Owner lands a key — not on the day this lands.

**What "automatic" covers, precisely: only a plan that is COMPLETE.** If the
planner produces anything this runner cannot carry out — a skill to route or
prune, a hook set to merge — **nothing is applied at all**, and the whole list
is recorded as `pending` for a human.

That is a correction, not a simplification. An earlier version swapped the root
and held the rest back, which leaves a host describing the OLD tree while
serving the NEW one: a hook whose script was removed upstream still has a
settings entry pointing into the root, and every AQG hook command ends in
``|| true``, so the guardrail stops running and **says nothing**. Automation
that can silently disable a guardrail is worse than no automation.

What is left automatic is still the common case. Under §1's payload classes, a
hook script's body (class 1) and a skill's content (class 2) ride the root
symlink for free — the swap *is* the whole update for them. Classes 3, 4 and 5
change what a host is configured to point at, and those wait for a person.

**Concurrency.** This does not hold the install lock while it fetches;
``transaction.apply_plan`` takes it for the apply, and a nested non-blocking
flock from the same process would simply report busy. Two racing checks are safe
for correctness — each verifies its own documents against the pinned keyring —
but they can contend on a git ref lock, which surfaces as a fetch failure and is
recorded as one rather than crashing. (The PR6a ledger claimed this ran under the
lock. It does not; that claim was wrong and is corrected here.)
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import acquire, dispatch, plan as plan_mod, stage, state, trust
    from scripts.aqg_update import transaction
    from scripts.aqg_update import hosts as hosts_mod
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import acquire, dispatch, plan as plan_mod, stage, state, trust  # type: ignore[no-redef]
    from aqg_update import transaction  # type: ignore[no-redef]
    from aqg_update import hosts as hosts_mod  # type: ignore[no-redef]

#: Set this to anything non-empty to stop the automatic channel entirely.
KILL_SWITCH = "AQG_NO_UPDATE_CHECK"

LAST_CHECK_FILENAME = "update-last-check.json"

#: How long a recorded check suppresses the next one. Sessions start many times
#: a day and a remote does not need telling every time, but the right number
#: differs by who is asking: a day of debugging wants minutes, a settled install
#: may want longer than this. It was twenty hours and hardcoded, which made
#: every adjustment a code change and a release.
#:
#: The accepted cost of one hour rather than twenty: an install whose sessions
#: are spread across a working day makes up to 24 checks a day instead of about
#: one. A check is a git ref read against the release remote, and an install
#: that wants the old rhythm back sets the variable below.
DEFAULT_CHECK_INTERVAL_SECONDS = 3600

#: Set this to a whole number of seconds to override the interval.
INTERVAL_ENV = "AQG_UPDATE_INTERVAL_SECONDS"

#: Below this, "once per interval" stops rate-limiting anything — every session
#: start becomes a network round trip, which is the cost the interval exists to
#: avoid. It is a floor rather than a rejection: someone who asks for 5 seconds
#: is asking for a short interval, and the default is the opposite of that.
MIN_CHECK_INTERVAL_SECONDS = 60

#: And a ceiling, for the reason the future-timestamp guard in `_too_soon`
#: exists. That guard stops a record dated a year ahead from turning the check
#: off forever; an unbounded interval is the same denial of service reached
#: through the other operand of the same comparison, and an audit was right
#: that defending one side and not the other is half a defence. Seven days is
#: generous — a settled install can go a week between checks — while keeping
#: "stop checking" reachable only through `AQG_NO_UPDATE_CHECK`, which is the
#: switch a person looking for one will find.
MAX_CHECK_INTERVAL_SECONDS = 7 * 24 * 3600

#: What `int()` is allowed to see. `int` on its own accepts `3_600`, `+60` and
#: non-ASCII decimal digits like `٣٦٠٠`, which is a wider grammar than
#: anything documented here — and a silently-accepted `3_600` is a worse
#: outcome than a rejected one, because it looks like it was ignored.
_INTEGER = re.compile(r"[+-]?[0-9]+")


def check_interval_seconds() -> int:
    """The interval in force, from the environment or the default.

    On the SessionStart path a raised exception is a check that silently never
    happens, so nothing in here can raise. Three cases, and the difference
    between the first two is the one worth reading twice:

    * **Not a duration at all** — unset, blank, `soon`, `1.5`, `3_600`, and
      also `0` and the negatives. These are treated as *unset* and take the
      default. In particular `0` does **not** mean "check every time": there is
      no way to ask for that, and the shortest interval obtainable is
      ``MIN_CHECK_INTERVAL_SECONDS``. To stop checking, use ``KILL_SWITCH``.
    * **A duration outside the bounds** — honoured as far as the nearest bound,
      because someone who asks for 5 seconds is asking for a short interval and
      the default would be the opposite of that. The same in the other
      direction, where the bound is also what stops the variable from being a
      silent permanent off switch.
    * **A duration within the bounds** — used as given.
    """
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not _INTEGER.fullmatch(raw):
        return DEFAULT_CHECK_INTERVAL_SECONDS
    seconds = int(raw)
    if seconds <= 0:
        return DEFAULT_CHECK_INTERVAL_SECONDS
    return min(max(seconds, MIN_CHECK_INTERVAL_SECONDS), MAX_CHECK_INTERVAL_SECONDS)


#: Outcomes. Every one of them is written to the record, including the boring
#: ones — `doctor` cannot tell "checked, nothing to do" from "never checked"
#: unless the first leaves a mark, and the second is the one worth acting on.
OUTCOMES = (
    "disabled", "no-keyring", "too-soon", "current", "applied", "pending",
    "rolled-back", "repair-required", "interrupted", "failed",
    # An update was found and deliberately not applied — the skill-side trigger
    # checks while something is reading the tree it would swap. Calling this
    # "current" told a machine with a pending update that it was up to date,
    # because `report` reads the outcome and nothing else (audit F14).
    "deferred",
)


@dataclass(frozen=True)
class CheckResult:
    outcome: str
    detail: str = ""
    pending: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise ValueError(f"unknown check outcome {self.outcome!r}")


def _atomic_write(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=".aqg-check-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:  # aqg: top-level boundary
        # Cleanup only — the exception is re-raised.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def record(
    result: CheckResult, *, state_root: Optional[Path] = None, at: Optional[float] = None
) -> Optional[Path]:
    """Leave a durable note of what happened. Best effort, never fatal.

    Failing to write the record must not turn a successful check into a failed
    one, so this swallows its own errors — but it returns the path it wrote so a
    caller that cares can tell.
    """
    try:
        root = Path(state_root) if state_root is not None else state.state_root(create=True)
        path = root / LAST_CHECK_FILENAME
        pending = list(result.pending)
        if not pending and result.outcome not in ("applied",):
            # Carried forward. The record is a single slot, so writing an empty
            # list here erased the only notice a human had that class-5 work was
            # outstanding — and its absence reads as "nothing outstanding".
            previous = read_last_check(state_root=state_root)
            if isinstance(previous, dict):
                pending = [str(x) for x in (previous.get("pending") or [])]
        _atomic_write(
            path,
            json.dumps(
                {
                    "checked_at": time.time() if at is None else at,
                    "outcome": result.outcome,
                    "detail": result.detail,
                    "pending": pending,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
        )
        return path
    except Exception:  # aqg: top-level boundary
        return None


def read_last_check(*, state_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    try:
        root = Path(state_root) if state_root is not None else state.state_root(create=False)
        raw = (root / LAST_CHECK_FILENAME).read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def report(*, state_root: Optional[Path] = None) -> List[str]:
    """What `doctor` prints. The only surface a silent system has."""
    last = read_last_check(state_root=state_root)
    if last is None:
        return [
            "update check: never run on this machine "
            "(no record; the automatic channel may not be wired, or is disabled)"
        ]
    outcome = last.get("outcome", "unknown")
    when = last.get("checked_at")
    stamp = (
        time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))
        if isinstance(when, (int, float)) else "an unknown time"
    )
    lines = [f"update check: {outcome} at {stamp}"]
    detail = last.get("detail")
    if detail:
        lines.append(f"  detail: {detail}")
    for item in last.get("pending") or []:
        lines.append(f"  pending (needs a human): {item}")
    return lines


def _too_soon(state_root: Optional[Path], now: float) -> bool:
    last = read_last_check(state_root=state_root)
    if last is None:
        return False
    when = last.get("checked_at")
    if not isinstance(when, (int, float)):
        return False
    if when > now:
        # The record is a file the user can write. Unbounded, a timestamp a year
        # ahead turns the check off permanently and silently, which is a denial
        # of service that looks exactly like a healthy install.
        return False
    return (now - when) < check_interval_seconds()


def check(
    *,
    root: Path,
    remote: str,
    channel: str,
    keyring_path: Optional[Path] = None,
    state_root: Optional[Path] = None,
    now: Optional[float] = None,
    apply: bool = True,
) -> CheckResult:
    """Run one check, record it, and return what happened.

    The order of the gates is the point, and it is: kill switch, clock, keyring,
    remote. The switch first, so a user who turned this off has nothing done at
    them — not even a file written in their state directory. The clock second,
    ahead of the keyring, because otherwise a build with no keyring would rewrite
    its record on **every session start** to say so again; once per interval is
    enough to tell `doctor` why nothing is happening.
    """
    now = time.time() if now is None else now
    if os.environ.get(KILL_SWITCH):
        # Deliberately NOT recorded: a user who turned this off should not have
        # their state directory written to on every session either.
        return CheckResult(outcome="disabled")

    if _too_soon(state_root, now):
        return CheckResult(outcome="too-soon")

    try:
        keyring = trust.load_trusted_keys(keyring_path)
    except trust.TrustError as exc:
        return _finish(CheckResult(outcome="no-keyring", detail=str(exc)), state_root, now)

    # Written BEFORE the network is touched. The record used to appear only on
    # completion, so a run that was killed mid-fetch never rate-limited — and
    # every later session started another one.
    # `interrupted`, not `failed`. This record exists the whole time a check is
    # RUNNING, so grading it as a failure made `doctor` show FAIL for the two
    # seconds a healthy check takes — and made "the record exists" stop meaning
    # "the check finished", which broke a test that had relied on it. One word
    # covers both readings honestly: a check is running now, or one stopped.
    record(CheckResult(outcome="interrupted",
                       detail="a check started and did not finish"),
           state_root=state_root, at=now)
    try:
        return _finish(
            _check_locked(
                root=Path(root), remote=remote, channel=channel,
                keyring=keyring, state_root=state_root, apply=apply,
            ),
            state_root, now,
        )
    except BaseException as exc:  # aqg: top-level boundary
        # Recorded, then swallowed by `main`. Nothing that happens inside an
        # update check may reach a user's terminal as a crash at session start.
        return _finish(
            CheckResult(
                outcome="failed", detail=f"{type(exc).__name__}: {exc}"
            ), state_root, now,
        )


def _finish(result: CheckResult, state_root: Optional[Path], now: float) -> CheckResult:
    record(result, state_root=state_root, at=now)
    return result


def _check_locked(
    *, root: Path, remote: str, channel: str, keyring, state_root, apply: bool
) -> CheckResult:
    installed = state.read_state()
    sequence = (
        installed.get("release_sequence")
        if isinstance(installed, Mapping) else None
    )
    found = acquire.available_release(
        root,
        remote=remote,
        channel=channel,
        keyring=keyring,
        installed_sequence=trust.FIRST_INSTALL if sequence is None else sequence,
    )
    if found is None:
        return CheckResult(outcome="current")
    if not apply:
        return CheckResult(
            outcome="deferred",
            detail=(
                f"{found.manifest.get('version')} is available; it will be applied "
                f"at the next session start, when nothing is reading the tree it "
                f"replaces"
            ),
        )
    return _apply(
        root=root, commit=found.commit,
        version=found.manifest.get("version"),
        installed=installed, state_root=state_root,
        channel=channel, sequence=found.release_sequence, key_id=found.key_id,
    )


def _record_installed(
    *, channel: str, version, commit: str, sequence, key_id, pending
) -> None:
    """Write what is live, in the shape `state.validate_state` demands.

    Called by the transaction after the apply commits and before the journal is
    cleared, so a crash in between still leaves a journal saying something was
    in flight. `applied_by` records HOW it arrived, because a version applied by
    the automatic channel and one a human pushed through `upgrade.sh` are not
    equally attested and nothing else distinguishes them.
    """
    state.write_state({
        "schema": 1,
        "channel": channel,
        "installed_version": str(version),
        "installed_commit": commit,
        "release_sequence": int(sequence),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "applied_by": str(key_id),
        "hosts": {},
        "pending": list(pending),
    })


def _apply(
    *, root: Path, commit: str, version, installed, state_root,
    host_reconciliation: bool = False,
    provenance: str = "verified release",
    channel: str = "stable",
    sequence: int = 0,
    key_id: str = "unsigned-manual",
) -> CheckResult:
    # Derived from where the root currently POINTS, not from a layout guessed
    # off its own path: the root is a symlink into `versions/<sha>`, so the
    # directory that holds versions is that target's parent. Computing it as
    # `root.resolve().parent / "versions"` produced `versions/versions/<sha>` —
    # caught by the end-to-end test, and by nothing before it.
    live = stage.current_target(Path(root))
    if live is None:
        return CheckResult(
            outcome="failed",
            detail=(
                f"{root} is not a symlink into a versions directory; this install "
                f"predates the managed-update layout and must be migrated by hand"
            ),
        )
    versions_dir = live.parent
    if live.name == commit:
        # Already live. Staging unconditionally turned the most ordinary thing a
        # user does — running the upgrade twice — into "a staged tree already
        # exists", reported as work needing a human.
        return CheckResult(
            outcome="current", detail=f"{version} is already the live version"
        )
    try:
        target = stage.stage_version(
            repo=Path(root), commit=commit,
            versions_dir=versions_dir, name=commit,
        )
    except stage.StageError as exc:
        # A commit-named directory is a name COLLISION, not a lock: it cannot
        # distinguish "another run holds this right now" from "a run crashed and
        # left it behind". Reporting the second as a transient conflict makes a
        # permanent condition look like a race that will clear itself.
        return CheckResult(
            outcome="pending",
            detail=str(exc),
            pending=(
                f"a staged tree for {commit[:12]} is already there, left "
                f"behind or in use: {versions_dir / commit}. Remove it if "
                f"no update is running.",
            ),
        )

    dropped: List[str] = []
    evidence = _collect_evidence(installed, dropped)
    built = plan_mod.build_plan(
        state=installed, target=target, evidence=evidence, target_commit=commit
    )
    # Two different shapes: an Action that would change a host's configuration,
    # and a Deferred the PLANNER declined to plan at all. Both mean a human has
    # to look, and both mean the root must not move underneath them.
    outstanding = tuple(
        [
            # The SUBJECT is in the line because this is what a person reads to
            # decide what to do: "route_skill" without a skill name is not an
            # instruction.
            f"{a.client_id}: {a.kind}"
            + (f" {a.subject}" if a.subject else "")
            + (f" — {a.detail}" if a.detail else "")
            for a in built.actions if a.kind in HOST_TOUCHING_KINDS
        ]
        + [f"{d.client_id}: {d.reason}" for d in built.deferred]
        + [f"{item} (adapter could not report; treated as unknown)" for item in dropped]
    )
    if outstanding and host_reconciliation:
        # Handed to the caller, not to the transaction. The dispatcher refuses a
        # route with no destination rather than guessing one, and the caller is
        # about to do that work itself by reinstalling — so the actions leave
        # the plan and stay in the report.
        built = plan_mod.Plan(
            actions=tuple(
                a for a in built.actions if a.kind not in HOST_TOUCHING_KINDS
            ),
            deferred=(),
        )
    elif outstanding:
        # NOTHING is applied. Swapping the root while holding this back would
        # leave the host describing the old tree and serving the new one, and an
        # AQG hook whose script vanished from under the root fails into `|| true`
        # — the guardrail stops running and says nothing.
        #
        # And nothing is LEFT, either. The tree was staged before the plan could
        # be judged, and leaving it meant the next attempt at the same version
        # met "a staged version already exists" — so one refused run wedged the
        # upgrade permanently. Found by running it twice, not by a test.
        stage.discard_version(repo=Path(root), target=target)
        return CheckResult(
            outcome="pending",
            detail=(
                f"{version} is staged but not applied: it changes what a host "
                f"is configured to point at"
            ),
            pending=outstanding,
        )

    result = transaction.apply_plan(
        built,
        # What is now installed, written inside the transaction. Without this the
        # anti-rollback floor came from a file nobody wrote, so `trust` compared
        # every release against FIRST_INSTALL and a correctly signed OLD one —
        # the thing a rollback attack replays — was accepted on every machine.
        record_state=lambda: _record_installed(
            channel=channel, version=version, commit=commit,
            sequence=sequence, key_id=key_id, pending=list(outstanding),
        ),
        # `absolute`, never `resolve`: the root IS the symlink being replaced,
        # so resolving it hands the swap the version tree it points at — which
        # `stage.swap_root` then refuses, correctly, as a real directory it will
        # not destroy. The end-to-end test caught this; nothing before it could.
        resources=dispatch.Resources(target=target, root=Path(root).absolute()),
        smoke=lambda: _smoke(Path(root)),
    )
    mapped = {
        "committed": "applied", "busy": "pending",
        "rolled-back": "rolled-back", "repair-required": "repair-required",
    }
    # The provenance is in the record because nothing else distinguishes a
    # version that was signature-verified from one a human applied by hand, and
    # `doctor` is the only place anyone would ever look.
    detail = result.detail
    if mapped.get(result.status) == "applied":
        detail = f"{version} ({provenance})" + (f". {detail}" if detail else "")
    return CheckResult(
        outcome=mapped.get(result.status, "failed"),
        detail=detail,
        # Handed back even on success: the caller promised to reconcile these,
        # so it needs to know what they are.
        pending=outstanding if host_reconciliation else (),
    )


#: Action kinds that change what a HOST is configured to point at — its routed
#: skills (classes 3-4) or its hook set (class 5). If a plan contains any of
#: these, this runner applies **nothing**.
#:
#: The distinction that matters is not "can I perform this action" but "does
#: skipping it leave the host inconsistent". Swapping the root while a route or
#: a hook-set change waits leaves the host describing the OLD tree and serving
#: the NEW one — and because every AQG hook command ends in `|| true`, a hook
#: whose script vanished from under the root stops running and says nothing.
#:
#: `activate_root` and `record_state` are not here: they are AQG's own
#: bookkeeping, not a host's configuration, and skipping `record_state` (which
#: the dispatcher defers anyway) leaves nothing pointing at the wrong tree.
HOST_TOUCHING_KINDS = frozenset({"route_skill", "prune_skill", "merge_hooks"})


def _collect_evidence(installed, dropped: Optional[List[str]] = None) -> Dict[str, Any]:
    """Ask every host what it currently looks like. Read-only, and forgiving.

    A host whose adapter cannot answer must not stop the update for the others:
    the planner already treats an unknown host conservatively, and losing one
    host's evidence is a smaller failure than losing the whole check. What it
    must not be is *silent* — ``dropped`` collects what could not answer, so a
    broken adapter is distinguishable from a client that is not installed.
    """
    dropped = dropped if dropped is not None else []
    # Imported HERE, not at module load. An adapter that cannot import — a
    # missing dependency, a partial install — used to raise before `main`'s
    # boundary existed, so the detached process died with a traceback into
    # /dev/null and left no record at all. `doctor` then said "never run",
    # which reads exactly like "not installed". Found in the first rehearsal.
    try:
        from scripts.aqg_update import hosts as hosts_mod
    except ModuleNotFoundError:
        # ModuleNotFoundError only. A broad `except ImportError` also swallows a
        # failure INSIDE the package — a missing third-party dependency in one
        # adapter — and then reports it as "the package is not here", which is a
        # different problem with a different fix.
        from aqg_update import hosts as hosts_mod  # type: ignore[no-redef]

    evidence: Dict[str, Any] = {}
    for client_id in hosts_mod.available_clients():
        try:
            evidence[client_id] = hosts_mod.adapter_for(client_id).verify(
                state=dict(installed) if isinstance(installed, Mapping) else None
            )
        except Exception as exc:  # aqg: top-level boundary
            # Dropped from the evidence — an absent entry is a host the planner
            # has no evidence for, which is true — but NOT dropped silently. A
            # permanently broken adapter used to be indistinguishable from a
            # client that is simply not installed, which is the same
            # indistinguishability that made a dead runner look like an
            # uninstalled one.
            dropped.append(f"{client_id}: {type(exc).__name__}: {exc}")
            continue
    return evidence


def apply_commit(
    *,
    root: Path,
    commit: str,
    version: Optional[str] = None,
    state_root: Optional[Path] = None,
    host_reconciliation: bool = False,
) -> CheckResult:
    """Apply a commit the caller already has, transactionally, without a signature.

    **This is the manual path and only the manual path.** docs §9 requires a
    signature on the *automatic* channel and explicitly allows manual invocation
    to follow `main`; what moving `upgrade.sh` onto the engine buys is not
    verification but transaction — an update that either lands or does not, with
    the previous version still on disk.

    Nothing on the session-start path calls this: `main` calls `check`, and
    `check` goes through `acquire`, which requires the pinned keyring. That is a
    design boundary rather than a control — anything that can run code can call
    anything — but it is the boundary, and a test asserts it holds.

    ``host_reconciliation`` is the one rule that differs, and only because the
    situation does. The trigger refuses a plan that changes what a host points
    at, because **nothing follows it** — the root would move and the host would
    keep describing the old tree. `upgrade.sh` reinstalls skills and refreshes
    hooks in its next three steps, which is exactly that reconciliation, in the
    same command. A caller that will do that says so, gets the swap, and gets the
    list back so it can report what it is about to fix. A caller that will not —
    `--no-hooks`, or the per-client skips — must not claim it.
    """
    if not isinstance(commit, str) or len(commit) != 40 or not all(
        c in "0123456789abcdef" for c in commit
    ):
        return CheckResult(
            outcome="failed",
            detail=f"{commit!r} is not a full commit sha; refusing to guess what to apply",
        )
    try:
        return _apply(
            root=Path(root), commit=commit, version=version or commit[:12],
            installed=state.read_state(), state_root=state_root,
            host_reconciliation=host_reconciliation,
            provenance="manual, unsigned",
        )
    except BaseException as exc:  # aqg: top-level boundary
        # Returned rather than raised, for the same reason the automatic path
        # does: the caller is a shell step that must report, not a traceback.
        return CheckResult(outcome="failed", detail=f"{type(exc).__name__}: {exc}")


def _smoke(root: Path) -> bool:
    """The cheapest check that the version just made live is usable at all.

    Through the **root**, not the staged tree. Statting a file inside the staged
    tree is true whether or not the swap happened, so it could not detect the
    one failure it exists for. Readable, not executable: `_aqg_context.sh` is
    sourced, never run.
    """
    helper = Path(root) / "scripts" / "_aqg_context.sh"
    try:
        return helper.is_file() and os.access(str(helper), os.R_OK)
    except OSError:
        return False


def main(argv: Optional[Sequence[str]] = None) -> int:
    """Always returns 0. Always writes nothing to stdout."""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        root = Path(os.environ.get("AQG_ROOT") or Path(__file__).resolve().parents[2])
        result = check(
            root=root,
            remote=os.environ.get("AQG_UPDATE_REMOTE", "origin"),
            channel=os.environ.get("AQG_UPDATE_CHANNEL", "stable"),
            apply="--check-only" not in argv,
        )
        if result.outcome not in ("disabled", "too-soon", "current"):
            print(f"[aqg update] {result.outcome}: {result.detail}", file=sys.stderr)
    except BaseException as exc:  # aqg: top-level boundary
        # The outermost boundary. `check` already catches its own failures; this
        # covers the ones before it — resolving a root, reading an environment.
        print(f"[aqg update] check could not run: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
