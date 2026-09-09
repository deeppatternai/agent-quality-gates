"""Execute a plan's actions, and report what happened to each.

docs/UPDATE_ARCHITECTURE.md §5 phase 5. This module owns **mechanics, not
policy**. It does not take the update lock, does not journal, and does not
decide what a failure means — those are the transaction's, and stating the
boundary here is what keeps this from growing an interface shaped around a
consumer that does not exist yet.

Two defaults carry the safety:

* **Dry by default.** ``execute`` reports without acting unless ``apply=True``.
  Calling it wrong produces a description, not a change.
* **Everything checkable is checked before anything runs.** Kind, client id,
  destination and subject are validated across the WHOLE plan in a preflight, in
  both modes, so a plan that cannot be completed is refused rather than executed
  halfway — and a dry run refuses exactly what an apply would.

Three outcomes are neither success nor failure and are named separately, because
collapsing them loses the distinction a human needs:

* ``unchanged`` — the desired end state was already true. A prune of something
  that is not ours is *not* a failure; every run would look broken.
* ``deferred`` — this layer cannot do it yet, and saying so is more honest than
  either "done" or "failed". ``merge_hooks`` needs an explicitly prepared edit, and
  ``record_state`` belongs to the transaction, which holds the lock and knows
  whether the apply committed.
* ``failed`` — it was attempted and refused. The run stops there and hands back
  exactly how far it got; whether to roll back is policy.

That last promise is unconditional, which took a correction to make true. A
refusal discovered mid-walk used to discard every outcome already earned, and an
exception type outside the caught tuple still would. The preflight removes the
first; the second now travels with the exception as ``aqg_outcomes``, because an
unexpected error genuinely should abort — but the record of what already mutated
is exactly what a transaction needs in order to decide about rollback, and
throwing it away is a separate mistake from aborting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import skills_route, stage
    from scripts.aqg_update.plan import Action, Plan
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import skills_route, stage  # type: ignore[no-redef]
    from aqg_update.plan import Action, Plan  # type: ignore[no-redef]

#: Every kind this module knows. A plan carrying anything else is a refusal.
KNOWN_KINDS = (
    "route_skill",
    "prune_skill",
    "merge_hooks",
    "activate_root",
    "record_state",
)


class DispatchError(RuntimeError):
    """A refusal to execute. Always fail-closed."""


@dataclass(frozen=True)
class Resources:
    """Everything the dispatcher is allowed to touch.

    Passed in rather than discovered, so a test and a real run differ only in
    what they hand over — and so nothing here can reach a path nobody named.
    Every path must be absolute for that to be true: a relative one resolves
    against the ambient cwd, which is precisely a path nobody named.
    """

    target: Path
    root: Path
    skills_dest: Mapping[str, Path] = field(default_factory=dict)
    hook_edits: Mapping = field(default_factory=dict)

    def __post_init__(self) -> None:
        for label, value in (("target", self.target), ("root", self.root)):
            if not Path(value).is_absolute():
                raise DispatchError(
                    f"Resources.{label} must be an absolute path, got {value}"
                )
        for client_id, value in dict(self.skills_dest).items():
            if not Path(value).is_absolute():
                raise DispatchError(
                    f"Resources.skills_dest[{client_id!r}] must be an absolute "
                    f"path, got {value}"
                )
        object.__setattr__(self, "skills_dest", MappingProxyType(dict(self.skills_dest)))
        object.__setattr__(self, "hook_edits", MappingProxyType(dict(self.hook_edits)))

    @property
    def skills_source(self) -> Path:
        return Path(self.target) / "skills"


@dataclass(frozen=True)
class Outcome:
    action: Action
    status: str
    detail: str


def _dest_for(resources: Resources, action: Action) -> Path:
    if not action.client_id:
        raise DispatchError(f"{action.kind} carries no client id")
    dest = resources.skills_dest.get(action.client_id)
    if dest is None:
        raise DispatchError(
            f"no skills destination given for {action.client_id!r}; refusing to "
            f"guess where that host keeps its skills"
        )
    return Path(dest)


def _subject_of(action: Action) -> str:
    if not action.subject:
        raise DispatchError(f"{action.kind} carries no subject to act on")
    return action.subject


def _apply_one(action: Action, resources: Resources) -> Outcome:
    if action.kind == "route_skill":
        created = skills_route.route(
            name=_subject_of(action),
            source_root=resources.skills_source,
            dest_root=_dest_for(resources, action),
        )
        return Outcome(
            action=action,
            status="applied" if created else "unchanged",
            detail=f"{action.subject} routed" if created else "route already correct",
        )

    if action.kind == "prune_skill":
        removed = skills_route.prune(
            name=_subject_of(action),
            source_root=resources.skills_source,
            dest_root=_dest_for(resources, action),
        )
        return Outcome(
            action=action,
            status="applied" if removed else "unchanged",
            detail=(
                f"{action.subject} route removed"
                if removed
                else "nothing of ours was there to remove"
            ),
        )

    if action.kind == "activate_root":
        root, target = Path(resources.root), Path(resources.target)
        if stage.current_target(root) == target:
            # `unchanged` means the same thing for every kind, or it means
            # nothing: route and prune both distinguish it.
            return Outcome(
                action=action,
                status="unchanged",
                detail=f"root already points at {target}",
            )
        previous = stage.swap_root(root=root, target=target)
        return Outcome(
            action=action,
            status="applied",
            detail=f"root now points at {target} (was {previous})",
        )

    if action.kind == "merge_hooks":
        edit = resources.hook_edits.get(action.client_id)
        if edit is not None:
            changed = edit.apply()
            return Outcome(action=action, status='applied' if changed else 'unchanged',
                           detail='AQG-owned hooks refreshed')
        return Outcome(
            action=action,
            status="deferred",
            detail=(
                f"{action.client_id}: no host adapter implements an apply verb "
                f"yet, so the managed hook set cannot be merged from here"
            ),
        )

    if action.kind == "record_state":
        return Outcome(
            action=action,
            status="deferred",
            detail=(
                "install state is written by the transaction, which holds the "
                "lock and knows whether the apply committed"
            ),
        )

    raise DispatchError(  # pragma: no cover - guarded before the call
        f"unhandled action kind {action.kind!r}"
    )


#: Kinds this layer cannot perform at all yet. Reported the same way in both
#: modes, so a dry run previews the apply rather than a rosier version of it.
_ALWAYS_DEFERRED = ("merge_hooks", "record_state")


def _preflight(plan: Plan, resources: Resources) -> None:
    """Refuse anything statically checkable, for the whole plan, before any run.

    Checked here rather than on the way past, so a refusal means *nothing ran* —
    the alternative left a host half-applied with the outcomes discarded.
    """
    for action in plan.actions:
        if action.kind not in KNOWN_KINDS:
            raise DispatchError(
                f"unhandled action kind {action.kind!r}; this dispatcher knows "
                f"{list(KNOWN_KINDS)}. A silent skip would be work not done that "
                f"nobody reports"
            )
        if action.kind in {"route_skill", "prune_skill"}:
            _subject_of(action)
            _dest_for(resources, action)


def execute(
    plan: Plan, *, resources: Resources, apply: bool = False
) -> Tuple[Outcome, ...]:
    """Walk *plan* in order. Returns one outcome per action reached.

    A preflight runs first in both modes, so a plan that could not be applied is
    refused before anything happens rather than partway through.

    With ``apply=False`` (the default) nothing is changed: each action is
    reported as ``would-apply``, except the kinds this layer can never perform,
    which report ``deferred`` in both modes so the preview matches the apply.
    With ``apply=True`` each action runs until one fails; the run then stops and
    the outcomes describe exactly how far it got. Deciding what to do about that
    — roll back, retry, record — is policy, and policy lives in the transaction.
    """
    _preflight(plan, resources)

    outcomes: list = []
    for action in plan.actions:
        if not apply:
            deferred = action.kind in _ALWAYS_DEFERRED and not (
                action.kind == 'merge_hooks' and action.client_id in resources.hook_edits)
            outcomes.append(
                Outcome(
                    action=action,
                    status="deferred" if deferred else "would-apply",
                    detail=action.detail,
                )
            )
            continue
        try:
            outcomes.append(_apply_one(action, resources))
        except (skills_route.RouteError, stage.StageError) as exc:
            outcomes.append(Outcome(action=action, status="failed", detail=str(exc)))
            break
        except BaseException as exc:  # aqg: top-level boundary
            # An unexpected type should abort — this layer met something it has
            # no model for. But the record of what already mutated is what a
            # transaction needs to decide about rollback, so it travels along.
            exc.aqg_outcomes = tuple(outcomes)  # type: ignore[attr-defined]
            raise
    return tuple(outcomes)
