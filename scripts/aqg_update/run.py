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
prune, or a hook set without an automatic edit adapter — **nothing is applied at all**, and the whole list
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
change what a host is configured to point at. Owned class-5 configuration is
refreshed transactionally by opted-in adapters; other changes still wait.

**Concurrency.** A separate check lock serializes acquisition through completion
across agents. ``transaction.apply_plan`` retains the independent install lock;
manual installation and older updaters still meet that existing mutation gate.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import acquire, dispatch, plan as plan_mod, stage, state, trust
    from scripts.aqg_update import transaction, migrate, lock, rules
    from scripts.aqg_update import hosts as hosts_mod
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import acquire, dispatch, plan as plan_mod, stage, state, trust  # type: ignore[no-redef]
    from aqg_update import transaction, migrate, lock, rules  # type: ignore[no-redef]
    from aqg_update import hosts as hosts_mod  # type: ignore[no-redef]

#: Set this to anything non-empty to stop the automatic channel entirely.
KILL_SWITCH = "AQG_NO_UPDATE_CHECK"

LAST_CHECK_FILENAME = "update-last-check.json"
RECONCILIATION_LOCK_FILENAME = "update-reconciliation.lock"
RECONCILIATION_STATE_FILENAME = "update-reconciliation-state.json"

#: How long a recorded check suppresses the next one. Sessions start many times
#: a day and a remote does not need telling every time, but the right number
#: differs by who is asking: a day of debugging wants minutes, a settled install
#: may want longer than this. It was twenty hours and hardcoded, which made
#: every adjustment a code change and a release.
#:
#: The accepted cost of thirty minutes rather than one hour: an install whose
#: sessions are spread across a working day makes up to 48 checks a day. A check
#: is a git ref read against the release remote, and an install that wants a
#: longer rhythm sets the variable below.
DEFAULT_CHECK_INTERVAL_SECONDS = 1800

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

#: What `int()` is allowed to see. `int` on its own accepts `3_600` and
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
    return _interval_setting()[0]


def _interval_setting() -> Tuple[int, bool]:
    """Effective interval and whether a valid positive override was supplied."""
    raw = os.environ.get(INTERVAL_ENV, "").strip()
    if not _INTEGER.fullmatch(raw):
        return DEFAULT_CHECK_INTERVAL_SECONDS, False
    # Clamp before int(): arbitrarily long positive values must not hit the
    # interpreter's integer-string conversion limit and suppress all checks.
    digits = raw.lstrip('+-').lstrip('0')
    if raw.startswith('-') or not digits:
        return DEFAULT_CHECK_INTERVAL_SECONDS, False
    if len(digits) > len(str(MAX_CHECK_INTERVAL_SECONDS)):
        return MAX_CHECK_INTERVAL_SECONDS, True
    seconds = int(digits)
    return min(max(seconds, MIN_CHECK_INTERVAL_SECONDS), MAX_CHECK_INTERVAL_SECONDS), True


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
    "deferred", "busy", "invalid-root",
)


@dataclass(frozen=True)
class CheckResult:
    outcome: str
    detail: str = ""
    pending: Tuple[str, ...] = ()
    rules_checked: bool = False
    activation: Optional[Tuple[Path, Path]] = None

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


def _trigger_label() -> str:
    """Bounded informational provenance; never an admission/trust input."""
    source = os.environ.get('AQG_UPDATE_TRIGGER', '')
    if source == 'session-hook':
        client = os.environ.get('AQG_CLIENT', '')
        if client not in {'codex', 'claude-code', 'cursor', 'codebuddy', 'qoder', 'qoder-cli', 'workbuddy'}:
            client = 'unknown'
        return f'session-hook:{client}'
    if source in {'aqg-startup-preflight', 'aqg-code-construction', 'python-skill'}:
        return source
    return 'unknown'


def record(
    result: CheckResult, *, state_root: Optional[Path] = None, at: Optional[float] = None,
    scope: Optional[str] = None, identity=None,
) -> Optional[Path]:
    """Leave a durable note of what happened. Best effort, never fatal.

    Failing to write the record must not turn a successful check into a failed
    one, so this swallows its own errors — but it returns the path it wrote so a
    caller that cares can tell.
    """
    try:
        if scope is not None and not re.fullmatch(r'[0-9a-f]{64}', scope):
            raise ValueError('invalid update scope')
        root = Path(state_root) if state_root is not None else state.state_root(create=True)
        path = root / LAST_CHECK_FILENAME
        previous = _read_record(root / f'update-check-{scope}.json') if scope else read_last_check(state_root=state_root)
        pending = list(result.pending)
        if result.rules_checked or any(x.startswith(rules.NOTICE_PREFIX) for x in pending):
            # Release-produced pending is authoritative, as before this feature.
            # Adding diagnostic-only notices must not erase sticky approvals.
            previous_pending = (previous or {}).get("pending") or []
            retain_release = result.outcome != 'applied' and not any(
                not x.startswith(rules.NOTICE_PREFIX) for x in pending)
            carried = [str(x) for x in previous_pending if (
                not result.rules_checked if str(x).startswith(rules.NOTICE_PREFIX)
                else retain_release)]
            pending = list(dict.fromkeys(carried + pending))
        elif not pending and result.outcome not in ("applied",):
            # Carried forward. The record is a single slot, so writing an empty
            # list here erased the only notice a human had that class-5 work was
            # outstanding — and its absence reads as "nothing outstanding".
            if isinstance(previous, dict):
                pending = [str(x) for x in (previous.get("pending") or [])]
        payload = json.dumps(
                {
                    "checked_at": time.time() if at is None else at,
                    "outcome": result.outcome,
                    "detail": result.detail,
                    "trigger": _trigger_label(),
                    "pending": pending,
                    "identity": identity if identity is not None else (previous or {}).get('identity'),
                },
                indent=2,
                sort_keys=True,
            ) + "\n"
        _atomic_write(path, payload)
        if scope is not None:
            _atomic_write(root / f"update-check-{scope}.json", payload)
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
    last = read_diagnostics(state_root=state_root)
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


def _read_record(path):
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def read_diagnostics(*, state_root=None):
    """Prefer scoped evidence; legacy writers cannot erase another root's handoff."""
    root = Path(state_root) if state_root is not None else state.state_root(create=False)
    records = []
    for path in root.glob('update-check-*.json'):
        if re.fullmatch(r'update-check-[0-9a-f]{64}\.json', path.name):
            value = _read_record(path)
            if value and isinstance(value.get('checked_at'), (int, float)):
                records.append(value)
    if not records:
        return read_last_check(state_root=state_root)
    latest = max(records, key=lambda item: item['checked_at'])
    last = dict(latest)
    pending = []
    for item in records:
        identity = item.get('identity')
        label = str(identity) if isinstance(identity, list) else 'unknown scope'
        pending.extend(f'{label}: {line}' for line in item.get('pending', []) if isinstance(line, str))
        if item is not latest and item.get('outcome') in {'invalid-root', 'repair-required', 'failed'}:
            pending.append(f"{label}: {item['outcome']}: {item.get('detail', '')}")
    last['pending'] = sorted(set(pending))
    return last


def _too_soon(state_root: Optional[Path], now: float, *, scope: Optional[str] = None, discovery_scope=None) -> bool:
    if scope is None:
        last = read_last_check(state_root=state_root)
    else:
        root = Path(state_root) if state_root is not None else state.state_root(create=False)
        try:
            last = json.loads((root / f"update-check-{scope}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(last, dict):
            return False
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
    if discovery_scope and last.get('outcome') == 'current':
        root = Path(state_root) if state_root is not None else state.state_root(create=False)
        discovery = _read_record(root / f'update-check-{discovery_scope}.json') or {}
        stamp = discovery.get('checked_at')
        if discovery.get('outcome') == 'deferred' and isinstance(stamp, (int, float)) and when < stamp <= now:
            return False
    interval, overridden = _interval_setting()
    if scope is not None and not overridden and last.get("outcome") in {"failed", "invalid-root", "interrupted", "rolled-back", "repair-required"}:
        interval = min(interval, 300)
    return (now - when) < interval


def _check_identity(root: Path, remote: str, channel: str, apply: bool):
    # Resolve parent aliases, but keep the final symlink stable across updates.
    entrance = Path(root).absolute()
    return [os.path.normcase(str(entrance.parent.resolve() / entrance.name)), remote, channel, apply]


def _check_scope(root: Path, remote: str, channel: str, apply: bool) -> str:
    # Logical spelling survives a version swap. Explicit --check-only uses its
    # own discovery scope; skill nudges and SessionStart share the apply scope.
    # Agent names are NOT keys:
    # healthy agents sharing one installation should share its rate limit.
    identity = _check_identity(root, remote, channel, apply)
    return hashlib.sha256(json.dumps(identity).encode("utf-8")).hexdigest()


def check(
    *,
    root: Path,
    remote: str,
    channel: str,
    keyring_path: Optional[Path] = None,
    state_root: Optional[Path] = None,
    now: Optional[float] = None,
    apply: bool = True,
    force: bool = False,
) -> CheckResult:
    """Serialize checks; ``force`` bypasses timing only, never trust or safety."""
    if os.environ.get(KILL_SWITCH):
        return CheckResult(outcome="disabled")
    scope = identity = None
    try:
        root = Path(root).expanduser()
        root = root.absolute() if root.is_symlink() else migrate.logical_root(root)
        scope = _check_scope(root, remote, channel, apply)
        identity = _check_identity(root, remote, channel, apply)
        discovery = _check_scope(root, remote, channel, False) if apply else None
        early_root = Path(state_root) if state_root is not None else state.state_root(create=False)
        previous = _read_record(early_root / f'update-check-{scope}.json') or {}
        if (
            not force
            and previous.get("outcome") == "failed"
            and _too_soon(
                state_root,
                time.time() if now is None else now,
                scope=scope,
                discovery_scope=discovery,
            )
        ):
            return CheckResult(outcome='too-soon')
        record_root = Path(state_root) if state_root is not None else state.state_root(create=True)
        record_root.mkdir(parents=True, exist_ok=True)
        with lock.install_lock(path=record_root / "update-check.lock") as check_lock:
            if apply:
                # Manual upgrade holds this lock across its root swap, host
                # writes, and finalization. An automatic retry must not plan
                # from evidence while those files are changing underneath it.
                with lock.install_lock(
                    path=record_root / RECONCILIATION_LOCK_FILENAME
                ):
                    result = _admitted_check(
                        root=root,
                        remote=remote,
                        channel=channel,
                        keyring_path=keyring_path,
                        state_root=state_root,
                        now=now,
                        apply=apply,
                        scope=scope,
                        force=force,
                    )
            else:
                result = _admitted_check(
                    root=root,
                    remote=remote,
                    channel=channel,
                    keyring_path=keyring_path,
                    state_root=state_root,
                    now=now,
                    apply=apply,
                    scope=scope,
                    force=force,
                )
            if apply and result.outcome == "applied":
                try:
                    pair = result.activation
                    _prune_after_update(root=root, previous=pair[0] if pair else None,
                        expected=pair[1] if pair else None,
                        state_root=record_root, check_lock=check_lock)
                except BaseException:  # aqg: top-level boundary
                    pass  # Even a broken cleanup/reporting boundary cannot undo success.
            return result
    except lock.LockBusy:
        return CheckResult(outcome="busy", detail="another update check is in progress")
    except Exception as exc:  # aqg: top-level boundary
        return _finish(CheckResult(outcome="failed", detail=f"{type(exc).__name__}: {exc}"), state_root, time.time() if now is None else now, scope=scope, identity=identity)


def _prune_after_update(*, root, previous, expected, state_root, check_lock) -> None:
    """Post-success maintenance. Never changes update state or retry admission."""
    report = {"checked_at": time.time(), "outcome": "skipped", "removed": [], "skipped": []}
    try:
        journal, apply_lock = _transaction_paths(state_root)
        with lock.install_lock(path=apply_lock) as held:
            live = stage.current_target(root)
            installed = state.read_state(path=state_root / state.STATE_FILENAME)
            if (previous is None or live is None or live != expected or previous == live
                    or live.parent.resolve() != (root.parent / "versions").resolve()
                    or previous.parent != live.parent
                    or transaction.read_journal(journal) is not None
                    or not installed or installed.get("pending")):
                report["detail"] = "installation or recovery state does not permit cleanup"
            else:
                def revalidate():
                    check_lock.still_held()
                    held.still_held()
                    if stage.current_target(root) != live or journal.exists():
                        raise RuntimeError("installation changed during cleanup")
                revalidate()
                stage.prune_versions(
                    # Retain the actual before/after pair, not mtime guesses.
                    versions_dir=root.parent / "versions", keep=0, protected=(live, previous),
                    repo=live, root=root, budget_seconds=5, before_remove=revalidate,
                    progress=report,
                )
                report["outcome"] = "partial" if report["skipped"] else "complete"
    except BaseException as exc:  # aqg: top-level boundary
        report.update(outcome="deferred", detail=f"{type(exc).__name__}: {exc}")
    try:
        # Separate diagnostics, deliberately not pending / update-last-check.
        _atomic_write(state_root / "update-cleanup-last-result.json",
            json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    except BaseException:  # aqg: top-level boundary
        pass


def _admitted_check(
    *, root, remote, channel, keyring_path, state_root, now, apply, scope,
    force=False,
):
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

    identity = _check_identity(root, remote, channel, apply)
    discovery = _check_scope(root, remote, channel, False) if apply else None
    if not force and _too_soon(state_root, now, scope=scope, discovery_scope=discovery):
        return CheckResult(outcome="too-soon")

    try:
        keyring = trust.load_trusted_keys(keyring_path)
    except trust.TrustError as exc:
        return _finish(CheckResult(outcome="no-keyring", detail=str(exc)), state_root, now, scope=scope, identity=identity)

    if root.parent.name == "versions" and not root.is_symlink():
        return _finish(CheckResult(outcome="invalid-root", detail=(
            f"{root} is a fixed version directory, not the managed entrance. "
            "Reapply this host's AQG configuration using the logical installation entrance; "
            "do not migrate or rename this historical version directory."
        )), state_root, now, scope=scope, identity=identity)

    if apply:
        gated = _transaction_gate(state_root, root=root)
        if gated is not None:
            if gated.outcome == 'busy':
                return gated  # A live transaction is not a failed attempt.
            return _finish(gated, state_root, now, scope=scope, identity=identity)

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
           state_root=state_root, at=now, scope=scope, identity=identity)
    try:
        return _finish(
            _check_locked(
                root=(Path(root).absolute() if Path(root).is_symlink() else migrate.logical_root(root)),
                remote=remote, channel=channel,
                keyring=keyring, state_root=state_root, apply=apply,
            ),
            state_root, now, scope=scope,
        )
    except BaseException as exc:  # aqg: top-level boundary
        # Recorded, then swallowed by `main`. Nothing that happens inside an
        # update check may reach a user's terminal as a crash at session start.
        return _finish(
            CheckResult(
                outcome="failed", detail=f"{type(exc).__name__}: {exc}"
            ), state_root, now, scope=scope,
        )


def _finish(result: CheckResult, state_root: Optional[Path], now: float, *, scope=None, identity=None) -> CheckResult:
    record(result, state_root=state_root, at=now, scope=scope, identity=identity)
    return result


def _check_locked(
    *, root: Path, remote: str, channel: str, keyring, state_root, apply: bool
) -> CheckResult:
    result = _check_release(root=root, remote=remote, channel=channel,
                            keyring=keyring, state_root=state_root, apply=apply)
    # A current release can still have copied rules from an obsolete checkout.
    # Inspect after apply as well, so expected paths describe the live entrance.
    try:
        notices = rules.pending_rules(root)
    except (OSError, ValueError, RuntimeError, ImportError):
        # Diagnostics must not relabel a committed release as a failed update.
        return replace(result, pending=result.pending + (
            rules.NOTICE_PREFIX + 'inspection unavailable; verify host rules explicitly',))
    return replace(result, pending=result.pending + notices, rules_checked=True)


def _check_release(
    *, root: Path, remote: str, channel: str, keyring, state_root, apply: bool
) -> CheckResult:
    installed = state.read_state(path=Path(state_root) / state.STATE_FILENAME if state_root is not None else None)
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
                f"{found.manifest.get('version')} is available; the next eligible "
                f"session-start check will try to apply it, subject to update locks "
                f"and host configuration checks"
            ),
        )
    return _apply(
        root=root, commit=found.commit,
        version=found.manifest.get("version"),
        installed=installed, state_root=state_root,
        channel=channel, sequence=found.release_sequence, key_id=found.key_id,
    )


def _state_file(state_root: Optional[Path]) -> Optional[Path]:
    """The install-state path under an explicit root, directory created.

    ``state.state_root(create=True)`` creates the default root; an explicit one
    got no such treatment, so naming a root that did not exist yet failed the
    write — and a failed state write is `repair-required`, i.e. the new version
    is live but cannot refuse an older release.
    """
    if state_root is None:
        return None
    root = Path(state_root)
    root.mkdir(parents=True, exist_ok=True)
    return root / state.STATE_FILENAME


def _record_installed(
    *, channel: str, version, commit: str, sequence, key_id, pending,
    state_root: Optional[Path] = None, hosts=None,
) -> None:
    """Write what is live, in the shape `state.validate_state` demands.

    Called by the transaction after the apply commits and before the journal is
    cleared, so a crash in between still leaves a journal saying something was
    in flight. `applied_by` records HOW it arrived, because a version applied by
    the automatic channel and one a human pushed through `upgrade.sh` are not
    equally attested and nothing else distinguishes them.
    """
    # `state_root` reaches THIS write, not only the last-check file beside it.
    # It did not, so `_apply(state_root=...)` moved one of the two files it
    # names and silently wrote the more important one to the real home — which
    # is how the test suite came to overwrite a developer's own install record,
    # resetting `release_sequence` (the anti-rollback floor) to 0.
    state.write_state({
        "schema": 1,
        "channel": channel,
        "installed_version": str(version),
        "installed_commit": commit,
        "release_sequence": int(sequence),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "applied_by": str(key_id),
        "hosts": hosts if hosts is not None else {},
        "pending": list(pending),
    }, path=_state_file(state_root))


def _transaction_paths(state_root):
    storage = Path(state_root) if state_root is not None else state.state_root(create=True)
    storage.mkdir(parents=True, exist_ok=True)
    return storage / transaction.JOURNAL_FILENAME, storage / lock.LOCK_FILENAME


def prepare_reconciliation_snapshot(
    *, state_root: Optional[Path] = None
) -> Path:
    """Snapshot install state before a manual root swap.

    The caller must already hold ``RECONCILIATION_LOCK_FILENAME``. The apply
    lock makes the snapshot and transaction state mutually consistent.
    """
    journal, apply_lock = _transaction_paths(state_root)
    snapshot = journal.parent / RECONCILIATION_STATE_FILENAME
    with lock.install_lock(path=apply_lock):
        if transaction.read_journal(journal) is not None:
            raise RuntimeError(
                f"cannot snapshot reconciliation state with journal {journal} present"
            )
        installed = state.read_state(path=_state_file(state_root))
        if not isinstance(installed, Mapping):
            raise RuntimeError("cannot reconcile a managed root with no install state")
        state.write_state(dict(installed), path=snapshot)
    return snapshot


def rollback_reconciliation(
    *,
    root: Path,
    expected_commit: str,
    previous_root: Path,
    state_root: Optional[Path] = None,
) -> CheckResult:
    """Restore both root and install state after manual host work fails."""
    journal, apply_lock = _transaction_paths(state_root)
    snapshot_path = journal.parent / RECONCILIATION_STATE_FILENAME
    try:
        with lock.install_lock(path=apply_lock):
            if transaction.read_journal(journal) is not None:
                return CheckResult(
                    outcome="repair-required",
                    detail=f"cannot roll back reconciliation with journal {journal} present",
                )
            installed = state.read_state(path=_state_file(state_root))
            snapshot = state.read_state(path=snapshot_path)
            live = stage.current_target(Path(root))
            previous = Path(previous_root)
            if (
                not isinstance(installed, Mapping)
                or installed.get("installed_commit") != expected_commit
                or live is None
                or stage.version_commit(live) != expected_commit
            ):
                return CheckResult(
                    outcome="repair-required",
                    detail="live root or install state changed before reconciliation rollback",
                )
            if (
                not isinstance(snapshot, Mapping)
                or not previous.is_absolute()
                or not previous.is_dir()
                or stage.version_commit(previous)
                != snapshot.get("installed_commit")
            ):
                return CheckResult(
                    outcome="repair-required",
                    detail="previous root does not match the saved install state",
                )

            stage.swap_root(root=Path(root).absolute(), target=previous)
            try:
                state.write_state(dict(snapshot), path=_state_file(state_root))
            except (OSError, ValueError, RuntimeError) as exc:
                try:
                    stage.swap_root(root=Path(root).absolute(), target=live)
                except (OSError, ValueError, RuntimeError):
                    return CheckResult(
                        outcome="repair-required",
                        detail=(
                            "root rolled back but install state could not be "
                            f"restored: {exc}"
                        ),
                    )
                return CheckResult(
                    outcome="failed",
                    detail=(
                        "install state restore failed; the new root was "
                        f"restored: {exc}"
                    ),
                )
            try:
                snapshot_path.unlink()
            except OSError:
                pass
            return CheckResult(
                outcome="rolled-back",
                detail="previous root and install state restored",
            )
    except lock.LockBusy:
        return CheckResult(
            outcome="busy", detail="another AQG update is in progress"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return CheckResult(
            outcome="failed", detail=f"reconciliation rollback failed: {exc}"
        )


def _transaction_gate(state_root, *, root=None):
    """Read recovery state under the apply lock, before any current fast path."""
    journal, apply_lock = _transaction_paths(state_root)
    try:
        with lock.install_lock(path=apply_lock):
            if transaction.read_journal(journal) is not None:
                if root is not None and transaction.recover_hook_update(
                        journal, Path(root), state.read_state(path=journal.parent / state.STATE_FILENAME)):
                    return None
                return CheckResult(outcome='repair-required', detail=f'unresolved update journal: {journal}; repair before retrying')
    except lock.LockBusy:
        return CheckResult(outcome='busy', detail='another AQG update is in progress')
    except (OSError, ValueError, RuntimeError) as exc:
        return CheckResult(outcome='repair-required', detail=str(exc))
    return None


def _reconcile_recorded_pending(
    *, root: Path, installed, state_root, expected_commit: Optional[str] = None
):
    """Retire a stale manual-update handoff after checking live reality.

    ``pending`` is allowed to outlive the work it described because manual
    installers reconcile hosts after the root transaction. It must not outlive
    proof that the reconciliation completed: otherwise every later release is
    refused before it can make progress.
    """
    if (
        expected_commit is None
        and (not isinstance(installed, Mapping) or not installed.get("pending"))
    ):
        return installed, None

    journal, apply_lock = _transaction_paths(state_root)
    try:
        with lock.install_lock(path=apply_lock):
            state_file = _state_file(state_root)
            latest = state.read_state(path=state_file)
            if not isinstance(latest, Mapping):
                return latest, CheckResult(
                    outcome="failed",
                    detail="cannot finalize an installation with no state",
                )
            if (
                expected_commit is not None
                and latest.get("installed_commit") != expected_commit
            ):
                return latest, CheckResult(
                    outcome="failed",
                    detail="install state changed before host reconciliation was finalized",
                )
            if transaction.read_journal(journal) is not None:
                return latest, CheckResult(
                    outcome="repair-required",
                    detail=f"unresolved update journal: {journal}; repair before retrying",
                )

            live = stage.current_target(Path(root))
            if live is None:
                return latest, CheckResult(
                    outcome="repair-required",
                    detail="cannot reconcile pending work because the managed root is unavailable",
                )
            live_commit = stage.version_commit(live)
            if live_commit != latest.get("installed_commit"):
                return latest, CheckResult(
                    outcome="repair-required",
                    detail=(
                        "cannot reconcile pending work because the live commit does not "
                        "match install-state"
                    ),
                )

            if not latest.get("pending"):
                return latest, None

            dropped: List[str] = []
            evidence = _collect_evidence(latest, dropped, target_root=live)
            candidate = dict(latest)
            candidate["pending"] = []
            built = plan_mod.build_plan(
                state=candidate,
                target=live,
                evidence=evidence,
                target_commit=live_commit,
                current=live,
            )
            unresolved = tuple(
                [
                    f"{action.client_id}: {action.kind}"
                    + (f" {action.subject}" if action.subject else "")
                    + (f" - {action.detail}" if action.detail else "")
                    for action in built.actions
                    if action.kind in HOST_TOUCHING_KINDS
                ]
                + [f"{item.client_id}: {item.reason}" for item in built.deferred]
                + [
                    f"{item} (adapter could not report; treated as unknown)"
                    for item in dropped
                ]
            )
            if unresolved or not built.is_complete:
                return latest, CheckResult(
                    outcome="pending",
                    detail="previous update still has unresolved host configuration",
                    pending=(
                        unresolved
                        or tuple(str(x) for x in latest.get("pending", []))
                    ),
                )

            state.write_state(candidate, path=state_file)
            return candidate, None
    except lock.LockBusy:
        return installed, CheckResult(
            outcome="busy", detail="another AQG update is in progress"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return installed, CheckResult(
            outcome="failed", detail=f"pending reconciliation failed: {exc}"
        )


def finalize_reconciliation(
    *, root: Path, expected_commit: str, state_root: Optional[Path] = None
) -> CheckResult:
    """Verify a manual caller's completed host work and retire its handoff."""
    try:
        installed = state.read_state(path=_state_file(state_root))
        refreshed, gate = _reconcile_recorded_pending(
            root=Path(root),
            installed=installed,
            state_root=state_root,
            expected_commit=expected_commit,
        )
        if gate is not None:
            return gate
        if isinstance(refreshed, Mapping) and refreshed.get("pending"):
            return CheckResult(
                outcome="pending", detail="host reconciliation remains incomplete"
            )
        try:
            snapshot = (
                _transaction_paths(state_root)[0].parent
                / RECONCILIATION_STATE_FILENAME
            )
            snapshot.unlink()
        except OSError:
            pass
        return CheckResult(
            outcome="current", detail="host reconciliation verified and finalized"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return CheckResult(
            outcome="failed", detail=f"host reconciliation could not be finalized: {exc}"
        )


def _apply(
    *, root: Path, commit: str, version, installed, state_root,
    host_reconciliation: bool = False,
    provenance: str = "verified release",
    channel: str = "stable",
    sequence: int = 0,
    key_id: str = "unsigned-manual",
) -> CheckResult:
    baseline_state = installed
    planning_state = installed
    if isinstance(installed, Mapping) and installed.get("pending"):
        # Pending is a report from an earlier attempt, not a durable admission
        # rule. Plan the new target from fresh host evidence; the new plan will
        # still refuse any host state that is unsafe now.
        planning_state = dict(installed)
        planning_state["pending"] = []

    # Derived from where the root currently POINTS, not from a layout guessed
    # off its own path: the root is a symlink into a version directory, so the
    # directory that holds versions is that target's parent. Computing it as
    # `root.resolve().parent / "versions"` produced `versions/versions/<sha>` —
    # caught by the end-to-end test, and by nothing before it.
    gated = _transaction_gate(state_root, root=root)
    if gated is not None:
        return gated
    journal, apply_lock = _transaction_paths(state_root)
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
    if stage.version_commit(live) == commit:
        if provenance == "verified release" and sequence > (installed or {}).get("release_sequence", -1):
            def record_verified_metadata():
                # The transaction holds the install lock. Another updater may
                # have advanced the tree/state since acquisition; never regress it.
                latest = state.read_state(path=Path(state_root) / state.STATE_FILENAME if state_root is not None else None) or {}
                if sequence <= latest.get("release_sequence", -1):
                    return
                if stage.version_commit(Path(root)) != commit:
                    raise state.StateError("live revision changed before metadata commit")
                _record_installed(
                    channel=channel, version=version, commit=commit, sequence=sequence,
                    key_id=key_id, pending=latest.get("pending", []),
                    hosts=latest.get("hosts", {}), state_root=state_root,
                )
            result = transaction.apply_plan(
                plan_mod.Plan(actions=(), deferred=()),
                journal=journal, lock_path=apply_lock,
                resources=dispatch.Resources(target=live, root=Path(root).absolute()),
                record_state=record_verified_metadata, smoke=lambda: _smoke(Path(root)),
            )
            return CheckResult(
                outcome={"committed": "current", "busy": "pending", "rolled-back": "rolled-back",
                         "repair-required": "repair-required"}.get(result.status, "failed"),
                detail=result.detail,
            )
        # Already live. Staging unconditionally turned the most ordinary thing a
        # user does — running the upgrade twice — into "a staged tree already
        # exists", reported as work needing a human.
        return CheckResult(
            outcome="current", detail=f"{version} is already the live version"
        )
    name = stage.version_name(version, commit, versions_dir)
    try:
        target = stage.stage_version(
            repo=Path(root), commit=commit,
            versions_dir=versions_dir, name=name,
        )
    except stage.StageError as exc:
        return CheckResult(
            outcome="failed",
            detail=str(exc),
        )

    committed = False
    try:
        dropped: List[str] = []
        evidence = _collect_evidence(planning_state, dropped, target_root=target)
        built = plan_mod.build_plan(
            state=planning_state,
            target=target,
            evidence=evidence,
            target_commit=commit,
            current=live,
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
        # Which of those actually have to stop the swap. A host whose hook command
        # never followed the root cannot be stranded by moving it, so holding the
        # whole machine for its pending merge buys nothing and costs every other
        # host its update. See `_split_outstanding`.
        _root_relative, _pinned = _host_facts(hosts_mod.available_clients())
        blocking, deferrable = _split_outstanding(
            built.actions, root_relative=_root_relative, pinned=_pinned
        )
        hook_edits = {}
        if provenance == 'verified release' and not host_reconciliation:
            by_path = {}
            prepare_started = time.monotonic()
            for action in blocking:
                if action.kind != 'merge_hooks':
                    continue
                adapter = hosts_mod.adapter_for(action.client_id)
                try:
                    if time.monotonic() - prepare_started > 90:
                        raise RuntimeError('hook preparation time budget exceeded; retry later')
                    config_path = adapter.hook_configuration_path()
                    edit = by_path.get(config_path)
                    if edit is None:
                        edit = adapter.prepare_hook_edit(target)
                    if edit is None:
                        continue  # This host still requires its own approval.
                    previous = by_path.get(edit.path)
                    if previous is not None and (previous.before, previous.after) != (edit.before, edit.after):
                        raise ValueError('shared host renderers disagree about the configuration')
                    by_path[edit.path] = previous or edit
                    hook_edits[action.client_id] = by_path[edit.path]
                except (OSError, ValueError, RuntimeError) as exc:
                    return CheckResult(outcome='pending',
                        detail=f'{version}: hook refresh could not be prepared: {exc}', pending=outstanding)
            blocking = [a for a in blocking if not (
                a.kind == 'merge_hooks' and a.client_id in hook_edits)]
            outstanding = tuple(line for line in outstanding if not any(
                line.startswith(f'{client}: merge_hooks') for client in hook_edits))
        # A planner refusal and an unreadable adapter are not host spellings; they
        # are "nobody knows", and they block as they always did.
        must_stop = bool(blocking) or bool(built.deferred) or bool(dropped)
        recorded_pending = outstanding
        # Not under `host_reconciliation`: that path already strips every
        # host-touching action and records the report, and running both would
        # silently change what IT records. One handoff or the other, never both.
        if deferrable and not must_stop and not host_reconciliation:
            # The swap proceeds; these leave the plan and stay in the report, the
            # same handoff `host_reconciliation` already performs below.
            deferred_ids = {id(a) for a in deferrable}
            built = plan_mod.Plan(
                actions=tuple(a for a in built.actions if id(a) not in deferred_ids),
                deferred=built.deferred,
            )
            # Reported, never RECORDED. `build_plan` refuses to plan while state
            # carries pending items — rightly, because half-applied work makes a
            # roster a lie — so writing a deferral there would make the next update
            # refuse on account of the last one's human approval. Trading one stall
            # for another. This is recomputed from the host's own file every run,
            # which is the same lesson the roster fix learned: read reality, do not
            # keep a ledger nobody clears.
            outstanding = tuple(
                line for line in outstanding
                if not any(line.startswith(f"{a.client_id}: ") for a in deferrable)
            ) + tuple(
                f"{a.client_id}: still running the previous version's hooks. Its "
                f"command is pinned to the tree it was installed from, so it keeps "
                f"working; approve the new hook set in that host to move it forward"
                for a in deferrable
            )
            recorded_pending = ()
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
        elif must_stop:
            # NOTHING is applied. Swapping the root while holding this back would
            # leave the host describing the old tree and serving the new one, and an
            # AQG hook whose script vanished from under the root fails into `|| true`
            # — the guardrail stops running and says nothing.
            #
            # And nothing is LEFT, either. The tree was staged before the plan could
            # be judged, and leaving it meant the next attempt at the same version
            # met "a staged version already exists" — so one refused run wedged the
            # upgrade permanently. Found by running it twice, not by a test.
            return CheckResult(
                outcome="pending",
                detail=(
                    f"{version} was not applied: it changes what a host "
                    f"is configured to point at"
                ),
                pending=outstanding,
            )

        def check_baseline():
            latest = state.read_state(path=Path(state_root) / state.STATE_FILENAME if state_root is not None else None)
            if stage.current_target(Path(root)) != live or latest != baseline_state:
                raise transaction.TransactionError('live root or install state changed during planning; retry')
            if provenance == 'verified release' and sequence <= (latest or {}).get('release_sequence', -1):
                raise transaction.TransactionError('verified release sequence no longer advances the installed floor')

        result = transaction.apply_plan(
            built,
            journal=journal, lock_path=apply_lock,
            precondition=check_baseline,
            # What is now installed, written inside the transaction. Without this the
            # anti-rollback floor came from a file nobody wrote, so `trust` compared
            # every release against FIRST_INSTALL and a correctly signed OLD one —
            # the thing a rollback attack replays — was accepted on every machine.
            record_state=lambda: _record_installed(
                channel=channel, version=version, commit=commit,
                sequence=sequence, key_id=key_id, pending=list(recorded_pending),
                state_root=state_root,
                hosts=(planning_state or {}).get('hosts', {}),
            ),
            # `absolute`, never `resolve`: the root IS the symlink being replaced,
            # so resolving it hands the swap the version tree it points at — which
            # `stage.swap_root` then refuses, correctly, as a real directory it will
            # not destroy. The end-to-end test caught this; nothing before it could.
            resources=dispatch.Resources(target=target, root=Path(root).absolute(), hook_edits=hook_edits),
            smoke=lambda: _smoke(Path(root)),
        )
        committed = result.status == 'committed'
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
            # Deferrals are handed back even without `host_reconciliation`: the
            # update DID apply, and the one thing left is a human approval that
            # nothing else will ever mention.
            pending=outstanding if (host_reconciliation or deferrable) else (),
            activation=(live, target) if committed else None,
        )
    finally:
        # A successfully published tree may already be pinned by a reader or
        # host, even if another updater has since moved the live root again.
        if not committed:
            _discard_inactive_attempt(Path(root), target, state_root=state_root)


def _discard_inactive_attempt(root: Path, target: Path, *, state_root=None) -> None:
    """Best-effort cleanup of only the fresh tree this attempt created."""
    try:
        # Recheck under the same apply lock: a concurrent activation or an
        # unresolved transaction must retain every tree needed for recovery.
        journal, apply_lock = _transaction_paths(state_root)
        with lock.install_lock(path=apply_lock):
            if transaction.read_journal(journal) is None:
                if stage.current_target(root) != target:
                    stage.discard_version(repo=root, target=target)
    except Exception:  # aqg: top-level boundary
        pass  # A retained stage will get a distinct name on the next attempt.


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


#: The only host-touching kind a pinned host may defer. Skills live UNDER the
#: root, so a pending route follows the swap for every host regardless of how
#: that host spells its hook command; only the hook set is at issue.
_DEFERRABLE_KINDS = frozenset({"merge_hooks"})


def _physical_pins(paths):
    """A digest pins bytes; only direct physical paths also pin a generation.

    Indirect and mixed configurations block deferral. Keep their full path list
    in the adapter so pruning still protects any old physical generation they
    use. Resolving here is read-only; never replace a logical command with its
    current target and then claim the original command was stable.
    """
    try:
        return bool(paths) and all(
            Path(path).absolute() == Path(path).resolve(strict=True) for path in paths
        )
    except (OSError, RuntimeError, ValueError):
        return False


def _split_outstanding(actions, *, root_relative, pinned):
    """Which host-touching actions must stop the apply, and which need not.

    The blocking rule exists because swapping the root while a hook change
    waits leaves a host "describing the old tree and serving the new one". That
    reason is a property of how a host SPELLS its hook command, and it is false
    for a version-PINNED one: codex names an absolute path into
    ``versions/<sha>/`` plus the digest of the files at it, and refuses to run
    on a mismatch. Such a command never follows the root, so the swap cannot
    strand it — it keeps executing the tree it was written for, intact and
    integrity-checked, until a human approves the new one in Codex ``/hooks``.
    That approval is the design, not a defect; freezing every other host until
    it happens is.

    Deferral is sound only while that tree still exists, so the pins are
    checked here as well as protected in ``stage.prune_versions``: each use
    verifies the other's assumption instead of trusting it.

    Fail closed on every unknown. A host missing from either mapping, one that
    declares no pins, or one whose pins have gone — all block, because "we do
    not know" is not "it is safe".
    """
    blocking, deferrable = [], []
    for action in actions:
        if action.kind not in HOST_TOUCHING_KINDS:
            continue
        client_id = action.client_id
        pins = tuple(pinned.get(client_id, ()))
        may_defer = (
            action.kind in _DEFERRABLE_KINDS
            and root_relative.get(client_id, True) is False
            and _physical_pins(pins)
        )
        (deferrable if may_defer else blocking).append(action)
    return tuple(blocking), tuple(deferrable)


def _host_facts(client_ids):
    """`(root_relative, pinned)` for each host, asking each adapter itself."""
    root_relative, pinned = {}, {}
    for client_id in client_ids:
        try:
            adapter = hosts_mod.adapter_for(client_id)
            root_relative[client_id] = bool(adapter.hook_command_is_root_relative)
            pinned[client_id] = tuple(adapter.pinned_command_paths())
        except Exception:  # aqg: top-level boundary
            # Left out of both maps, which `_split_outstanding` reads as
            # "blocks" — an adapter that cannot answer must not buy a deferral.
            continue
    return root_relative, pinned


def _collect_evidence(
    installed,
    dropped: Optional[List[str]] = None,
    target_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Ask every host what it currently looks like. Read-only, and forgiving.

    *target_root* is the tree the update is going TO, and passing it is what
    makes a hook status mean anything: "complete" against the tree that is live
    now says nothing about whether the host's settings will still be complete
    once the root swaps. Without it the planner falls back to re-merging on any
    version change — which is every update, which is why none ever applied.

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
                state=dict(installed) if isinstance(installed, Mapping) else None,
                target_root=target_root,
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
            root=Path(root), commit=commit,
            version=version or stage._git("show", f"{commit}:VERSION", cwd=Path(root)),
            installed=state.read_state(path=Path(state_root) / state.STATE_FILENAME if state_root is not None else None), state_root=state_root,
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
            force="--force-check" in argv,
        )
        if result.outcome not in ("disabled", "too-soon", "current", "busy"):
            print(f"[aqg update] {result.outcome}: {result.detail}", file=sys.stderr)
    except BaseException as exc:  # aqg: top-level boundary
        # The outermost boundary. `check` already catches its own failures; this
        # covers the ones before it — resolving a root, reading an environment.
        print(f"[aqg update] check could not run: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
