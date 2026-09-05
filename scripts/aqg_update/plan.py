"""Decide what an update would change, without changing anything.

docs/UPDATE_ARCHITECTURE.md §1 and §5 phase 2. Inputs: recorded install state,
the staged target tree, its commit, and each host's `verify` evidence. Output:
an ordered list of actions, each carrying the payload class it belongs to.
Nothing here writes, so a dry run is exactly a plan that was not applied.

**The classification is load-bearing, not decoration.** Of the five payload
classes only class 5 — a change to a host's hook-set membership — mutates
host-owned configuration, which makes it the only one with a real blast radius
and the only one the transaction must be able to treat differently. There is
also a **class 0**: AQG-owned bookkeeping (activating the new root, writing
install state) which touches nothing a host owns. It is named here rather than
squeezed into the five, so a consumer switching on the class meets no surprise.

**Evidence answers a question about the tree it ran in.** `verify` compares a
host's configuration against the canonical hook set of the root it executed
against — so `complete` means "matches the version installed now", not "matches
the target". Any host whose last applied version or commit differs from the
target's therefore gets a merge regardless of how healthy it looks. Getting this
wrong meant a release that changed the hook set would install none of it.

Two absences are deliberately NOT actions:

* **Class 1 and 2 are free** — edited hook scripts and edited skill content are
  reached through the root symlink, so they go live when `activate_root` runs.
  That step IS in the plan; a plan whose correctness rests on a step it does not
  contain cannot be checked by the layer that executes it.
* **A rename has no signal of its own.** It is observable only as a prune plus a
  route, and only against a recorded previous roster — which is why
  `install-state.json` exists at all.

The planner refuses rather than guesses, with one deliberate exception: a host
that cannot be planned for is **deferred**, not made to block every other host.
Refusing the whole plan over one host would be fail-closed and disproportionate;
deferring it acts on no assumption either.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update.hosts.base import HOOK_STATUSES, Evidence
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update.hosts.base import HOOK_STATUSES, Evidence  # type: ignore[no-redef]

#: Hook states meaning "AQG should install or repair hooks here".
_HOOKS_NEED_WORK = frozenset({"missing", "stale"})

#: Hook states that are actionable-but-not-by-us: the host's configuration could
#: not be read, and merging into a file we could not parse is how a hand-edited
#: settings file gets destroyed. Deferred for a human.
_HOOKS_DEFERRED = frozenset({"invalid"})

#: Hook states requiring nothing. ``complete`` is already correct for the tree
#: the evidence was gathered against (see the version check in `build_plan`);
#: ``not-applicable`` means AQG has no verified delivery for that host, so a
#: merge would be retried forever with nothing to merge into.
_HOOKS_SETTLED = frozenset({"complete", "not-applicable"})


class PlanError(RuntimeError):
    """A refusal to plan. Always fail-closed."""


@dataclass(frozen=True)
class Action:
    """One thing the update would do, and which payload class it belongs to."""

    kind: str
    client_id: Optional[str]
    subject: Optional[str]
    payload_class: int
    detail: str


@dataclass(frozen=True)
class Deferred:
    """Something the planner deliberately did not plan, and why."""

    client_id: str
    reason: str


@dataclass(frozen=True)
class Plan:
    actions: Tuple[Action, ...] = ()
    deferred: Tuple[Deferred, ...] = ()

    @property
    def has_no_actions(self) -> bool:
        return not self.actions

    @property
    def is_complete(self) -> bool:
        """No work AND nothing set aside.

        Separate from ``has_no_actions`` because a plan that deferred every host
        has no actions and is very much not finished.
        """
        return not self.actions and not self.deferred

    @property
    def touches_host_config(self) -> bool:
        """Whether any action mutates a host-owned file (payload class 5)."""
        return any(action.payload_class == 5 for action in self.actions)


def _require_safe_skill_name(name: str, *, client_id: str) -> str:
    """Refuse a routed-skill name that is not a single bare path component.

    These names come out of `install-state.json` and are joined to a route
    directory by whatever applies a prune. A corrupted or hand-edited state file
    must not be able to aim that at `..` or an absolute path. Our own files are
    exactly what a damaged install produces, so they are untrusted input.
    """
    if (
        not name
        or name in {".", ".."}
        or os.sep in name
        or "/" in name
        or (os.altsep and os.altsep in name)
        or Path(name).name != name
    ):
        raise PlanError(
            f"{client_id}: invalid skill name {name!r} in recorded state; "
            f"expected one path component"
        )
    return name


def _target_roster(target: Path) -> Tuple[str, ...]:
    """Skill names the target tree ships, or raise.

    Raises on a missing sentinel rather than returning an empty roster: an empty
    roster is read downstream as "ships no skills", which plans a prune of every
    routed skill on every host.
    """
    target = Path(target)
    if not (target / "VERSION").is_file():
        raise PlanError(f"{target} has no VERSION sentinel; it is not a version tree")
    skills_dir = target / "skills"
    if not skills_dir.is_dir():
        raise PlanError(
            f"{target} has no skills/ directory; reading an absent roster as "
            f"'no skills' would plan a prune of every routed skill on every host"
        )
    try:
        return tuple(
            sorted(child.name for child in skills_dir.iterdir() if child.is_dir())
        )
    except OSError as exc:
        raise PlanError(f"cannot read {skills_dir}: {exc}") from exc


def _hosts_of(state: Optional[Mapping[str, Any]]) -> Mapping[str, Any]:
    if state is None:
        return {}
    hosts = state.get("hosts") or {}
    if not isinstance(hosts, Mapping):
        raise PlanError(
            f"install state 'hosts' must be a mapping, got {type(hosts).__name__}"
        )
    return hosts


def _recorded_roster(
    state: Optional[Mapping[str, Any]], client_id: str
) -> Tuple[str, ...]:
    record = _hosts_of(state).get(client_id)
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise PlanError(
            f"install state record for {client_id!r} must be a mapping, got "
            f"{type(record).__name__}"
        )
    routed = record.get("routed_skills", [])
    if not isinstance(routed, list) or any(not isinstance(x, str) for x in routed):
        raise PlanError(
            f"{client_id}: 'routed_skills' must be a list of strings, got "
            f"{type(routed).__name__}"
        )
    return tuple(sorted(_require_safe_skill_name(n, client_id=client_id) for n in routed))


def _recorded_version(
    state: Optional[Mapping[str, Any]], client_id: str
) -> Optional[str]:
    record = _hosts_of(state).get(client_id)
    if not isinstance(record, Mapping):
        return None
    version = record.get("last_applied_version")
    return version if isinstance(version, str) else None


def _version_of(target: Path) -> str:
    try:
        return (Path(target) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise PlanError(f"cannot read {target}/VERSION: {exc}") from exc


def build_plan(
    *,
    state: Optional[Mapping[str, Any]],
    target: Path,
    evidence: Mapping[str, Evidence],
    target_commit: str,
) -> Plan:
    """Return the actions an update to *target* would perform.

    ``evidence`` is what each host's ``verify`` reported, keyed by client id, and
    each entry must belong to the host it is filed under. A recorded host with no
    evidence is deferred: planning for a host nobody verified would act on an
    assumption about it, and refusing outright would block every other host.
    """
    target = Path(target)
    roster = _target_roster(target)
    target_version = _version_of(target)
    # Content can change without the version string moving, so the commit is
    # part of "is the recorded evidence about this tree at all".
    install_moved = target_version != (state or {}).get("installed_version") or (
        target_commit != (state or {}).get("installed_commit")
    )

    pending = (state or {}).get("pending") or []
    if pending:
        raise PlanError(
            f"install state carries {len(pending)} pending item(s) from a previous "
            f"update; planning over half-applied work would treat its rosters as "
            f"settled fact"
        )

    for client_id, item in evidence.items():
        if item.client_id != client_id:
            raise PlanError(
                f"evidence filed under {client_id!r} belongs to {item.client_id!r}; "
                f"refusing to plan one host from another's evidence"
            )

    recorded_hosts = set(_hosts_of(state))
    deferred = [
        Deferred(
            client_id=client_id,
            reason="no verify evidence was supplied for this recorded host",
        )
        for client_id in sorted(recorded_hosts - set(evidence))
    ]
    planned_hosts = sorted(set(evidence))

    actions = []
    # Prunes precede routes. The two subject sets are disjoint as strings
    # (`previous - roster` and `roster - previous`), so a prune can never name a
    # routed skill — the ordering matters for a CASE-ONLY rename on a
    # case-insensitive filesystem, where `Foo` and `foo` are one directory entry
    # and pruning second would delete what routing just created.
    for client_id in planned_hosts:
        previous = _recorded_roster(state, client_id)
        for name in sorted(set(previous) - set(roster)):
            actions.append(
                Action(
                    kind="prune_skill",
                    client_id=client_id,
                    subject=name,
                    payload_class=4,
                    detail=f"{name} is no longer shipped; remove its route",
                )
            )
    for client_id in planned_hosts:
        previous = _recorded_roster(state, client_id)
        for name in sorted(set(roster) - set(previous)):
            actions.append(
                Action(
                    kind="route_skill",
                    client_id=client_id,
                    subject=name,
                    payload_class=3,
                    detail=f"{name} is newly shipped; create its route",
                )
            )

    for client_id in planned_hosts:
        status = evidence[client_id].hooks_status
        if status not in HOOK_STATUSES:
            raise PlanError(
                f"{client_id}: unmodelled hook status {status!r}; this planner "
                f"handles {sorted(HOOK_STATUSES)}"
            )
        if status in _HOOKS_DEFERRED:
            deferred.append(
                Deferred(
                    client_id=client_id,
                    reason=(
                        f"hook configuration is {status}; refusing to merge into a "
                        f"config that could not be read"
                    ),
                )
            )
            continue
        if status not in _HOOKS_NEED_WORK and status not in _HOOKS_SETTLED:
            # Total by construction: a status the contract models but this
            # planner has no rule for must stop it, not fall through as "fine".
            raise PlanError(
                f"{client_id}: no planning rule for hook status {status!r}"
            )
        stale_for_target = status != "not-applicable" and (
            install_moved or _recorded_version(state, client_id) != target_version
        )
        if status in _HOOKS_NEED_WORK or stale_for_target:
            reason = (
                f"managed hook set is {status}"
                if status in _HOOKS_NEED_WORK
                else (
                    f"evidence was gathered against "
                    f"{_recorded_version(state, client_id)}, not the target "
                    f"{target_version}"
                )
            )
            actions.append(
                Action(
                    kind="merge_hooks",
                    client_id=client_id,
                    subject=None,
                    payload_class=5,
                    detail=f"{reason}; merge the canonical set",
                )
            )

    if actions or install_moved:
        # Class 0 — AQG-owned bookkeeping, no host blast radius. Activation
        # precedes recording so state never claims a version the root does not
        # yet serve.
        actions.append(
            Action(
                kind="activate_root",
                client_id=None,
                subject=None,
                payload_class=0,
                detail=f"point the root at {target}",
            )
        )
        actions.append(
            Action(
                kind="record_state",
                client_id=None,
                subject=None,
                payload_class=0,
                detail="write the new install state",
            )
        )
    return Plan(actions=tuple(actions), deferred=tuple(deferred))
