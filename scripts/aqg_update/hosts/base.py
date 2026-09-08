"""The host-adapter contract — vocabulary owned by the dispatcher.

Four verbs eventually live here (docs/UPDATE_ARCHITECTURE.md §6.2). **This slice
defines only `verify`**, deliberately: declaring `plan`, `apply` and `rollback`
before they can be implemented and tested would be scaffolding that compiles but
proves nothing, and an abstract method nobody satisfies blocks every adapter from
being instantiated. They land with their implementations.

The division of labour follows DE's `client_hosts/`: **the dispatcher owns
vocabulary and validation; an adapter owns only its host's format mechanics.**
Nothing above an adapter may learn that Claude Code keeps hooks in
`settings.json` while Codex keeps them in `hooks.json` and Kimi in
`config.toml` — that knowledge is exactly what the adapter exists to absorb.

`AdapterError` is the whole failure surface **for host and install-state
contents** — the untrusted data an adapter reads. A caller promised a refusal
must not receive a native exception instead. (A bad *argument* from calling code,
`Path(42)` say, still raises normally; that is a caller bug, not untrusted input.)
"""

from __future__ import annotations

import os

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts.aqg_update import migrate, skills_route
except ImportError:  # invoked with scripts/ itself on sys.path
    from aqg_update import migrate, skills_route  # type: ignore[no-redef]

#: What `verify` may report about a host's managed hook set.
#:
#: * ``missing`` — no managed hooks present (never installed, or uninstalled).
#: * ``complete`` — every canonical hook present, commands matching.
#: * ``stale`` — installed but drifted: a missing, extra, or changed definition.
#: * ``invalid`` — the host's config could not be read or has the wrong shape.
#: * ``not-applicable`` — AQG has no verified hook delivery for this host, so
#:   there is nothing here for it to install or compare against. This is an
#:   evidence level, not a claim that hooks are impossible: for several of these
#:   hosts the registry records only that no surface has been *verified*.
#:   Distinct from ``missing``, which means hooks are absent and SHOULD be
#:   installed — collapsing the two would send a planner to install hooks where
#:   AQG has no way to install them, every run, forever.
#:
#: ``complete`` means the canonical hook identities are all present and their
#: commands match *after the host helper's whitespace normalization* — NOT
#: byte-identical. A pure reformat is therefore complete, not stale, which is
#: the intended behaviour: reformatting is not drift.
#:
HOOK_STATUSES: Tuple[str, ...] = (
    "missing",
    "complete",
    "stale",
    "invalid",
    "not-applicable",
)


class AdapterError(RuntimeError):
    """A refusal from a host adapter. Always fail-closed."""


@dataclass(frozen=True)
class Evidence:
    """What one host looks like right now, in a host-neutral shape.

    ``recorded_version`` is what install state *claims* was last applied here;
    ``hooks_status`` is what the host's config *actually* shows. Keeping both
    is the point — the interesting case is when they disagree.

    ``routed_skills`` extends that same claim-versus-reality split to skills,
    which had only the claim side. The planner used to diff the shipped roster
    against ``state["hosts"][id]["routed_skills"]`` — a key no installer writes,
    so a freshly installed machine reported zero routes, planned one
    ``route_skill`` per shipped skill, and stalled at ``pending`` forever
    because nothing was applied and therefore nothing was ever recorded.

    Three states, and the middle one is the reason this is ``Optional``:

    * ``None``  — this host keeps no AQG skills on this machine (no destination
      directory). The planner plans no skill work for it rather than proposing
      to populate a host that is not there.
    * ``()``    — the destination exists and holds none of our routes.
    * names     — what is actually routed, read off disk.

    An adapter that cannot answer raises instead; ``run._collect_evidence``
    already turns that into a reported ``dropped`` host, so absence never has to
    double as failure.

    ``hooks_checked_against`` names the tree ``hooks_status`` describes. It
    exists because "complete" is only useful if you know what it is complete
    FOR: computed against the tree that is live now, it says nothing about
    whether the host's settings will still be complete once the root swaps —
    which is why the planner used to re-merge on every version change, and why
    no update could ever apply. ``None`` means the adapter did not say, and the
    planner falls back to that conservative merge rather than guessing.
    """

    client_id: str
    hooks_status: str
    hooks_detail: str
    recorded_version: Optional[str]
    routed_skills: Optional[Tuple[str, ...]] = None
    hooks_checked_against: Optional[str] = None

    def __post_init__(self) -> None:
        # On the type, not in a helper: a guard an adapter can bypass by picking
        # a different constructor is a convention, not an invariant. This is also
        # where a multi-host adapter's per-instance client id gets checked, since
        # its class cannot declare one.
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise AdapterError(
                f"evidence carries an empty client_id: {self.client_id!r}"
            )
        if self.routed_skills is not None:
            if not isinstance(self.routed_skills, tuple):
                raise AdapterError(
                    f"{self.client_id}: routed_skills must be None or a tuple, "
                    f"got {type(self.routed_skills).__name__} "
                    f"({self.routed_skills!r}); a bare string is iterable, so "
                    f"'nope' would become four skills named n, o, p, e"
                )
            for name in self.routed_skills:
                if not isinstance(name, str):
                    raise AdapterError(
                        f"{self.client_id}: routed_skills holds a "
                        f"{type(name).__name__}, not a name: {name!r}"
                    )
                # The full one-path-component rule, on the TYPE. These names are
                # joined to a directory that is not ours by whatever applies the
                # plan, so nothing may aim that. Enforcing it here rather than in
                # the planner is what keeps a bad name from having to be
                # *handled* downstream: a planner that refuses one host still
                # holds back the whole apply, which turns one odd directory
                # entry into an indefinite stop on a security update channel.
                # It cannot arise from `owned_routes` — a directory entry name
                # can hold no separator — so this is the boundary for an adapter
                # that builds evidence some other way.
                try:
                    skills_route._require_safe_name(name)
                except skills_route.RouteError as exc:
                    raise AdapterError(
                        f"{self.client_id}: routed_skills carries {name!r}: {exc}"
                    ) from exc
        if self.hooks_status not in HOOK_STATUSES:
            raise AdapterError(
                f"{self.client_id}: unrecognized hook status "
                f"{self.hooks_status!r}; this adapter contract models "
                f"{list(HOOK_STATUSES)}"
            )


def observed_routes(
    *, aqg_root: Optional[Path], skills_subdir: str, dest_root: Path
) -> Optional[Tuple[str, ...]]:
    """What this layer actually owns in *dest_root*, or ``None`` if not here.

    Takes the root and derives the spelling itself rather than accepting a
    ``source_root`` from the caller: ownership is an exact link-text
    comparison, so letting each adapter arrive at its own spelling is how the
    planner came to disagree with the installer about which links were ours.

    ``None`` means "there is no skills destination for this host on this
    machine". That is deliberately narrow — it is NOT "it has none of ours",
    which is ``()`` and plans a full route. A host whose directory is missing
    has nothing to maintain; one whose directory is empty has everything to.
    """
    if aqg_root is None:
        return None
    dest_root = Path(dest_root)
    if not dest_root.is_dir():
        return None
    source_root = migrate.logical_root(aqg_root) / skills_subdir
    return skills_route.owned_routes(source_root=source_root, dest_root=dest_root)


def recorded_version_for(
    client_id: str, state: Optional[Dict[str, Any]]
) -> Optional[str]:
    """Pull this host's last-applied version out of install state.

    ``None`` means "no record", which is a first run. A malformed record is NOT
    absence: read that way, the planner would re-apply a host that is in fact
    already configured, so it raises instead.
    """
    if state is None:
        return None
    if not isinstance(state, dict):
        raise AdapterError(
            f"install state must be a mapping or absent, got {type(state).__name__}"
        )
    hosts = state.get("hosts")
    if hosts is None:
        return None
    if not isinstance(hosts, dict):
        raise AdapterError(
            f"install state 'hosts' must be a mapping, got {type(hosts).__name__}"
        )
    if client_id not in hosts:
        return None
    record = hosts[client_id]
    if not isinstance(record, dict):
        raise AdapterError(
            f"install state record for {client_id!r} must be a mapping, "
            f"got {type(record).__name__}"
        )
    # Presence, not truthiness: the record EXISTS, so this host was recorded.
    # Treating a missing or null version as "never applied" would make the
    # planner re-apply a host that is in fact already configured.
    version = record.get("last_applied_version")
    if not isinstance(version, str):
        raise AdapterError(
            f"{client_id!r} record is present but its 'last_applied_version' is "
            f"{type(version).__name__}, not a string; a recorded host must carry "
            f"the version it was last applied at"
        )
    return version


def require_aqg_root(
    root: Optional[Path], *, client_id: str, needs: Iterable[str] = ()
) -> Path:
    """Return a usable AQG checkout root, or raise.

    Guarding only ``root is None`` covers one representative of "the root is
    unusable", not the condition. A stale path that still exists reaches the
    host helper, whose canonical-spec builder fails, and the helper answers
    ``invalid`` — which in this contract means *the host's config* is broken.
    An AQG-side fault must never be reported as the host's.

    ``needs`` lists paths under the root the caller cannot work without, so each
    adapter states its own requirement rather than this function guessing.
    """
    if root is None:
        raise AdapterError(
            f"{client_id}: cannot resolve the AQG root, so no canonical hook set "
            f"exists to compare against; set AQG_ROOT or pass aqg_root explicitly"
        )
    root = Path(root)
    # The same sentinel both installers use to decide a path IS a checkout.
    if not (root / "VERSION").is_file() or not (root / "scripts").is_dir():
        raise AdapterError(
            f"{client_id}: {root} is not an AQG checkout (no VERSION/scripts), so "
            f"there is no canonical hook set to compare against"
        )
    for relative in needs:
        if not (root / relative).exists():
            raise AdapterError(
                f"{client_id}: AQG checkout at {root} is missing {relative}, which "
                f"this host's canonical hook set is built from"
            )
    # Inspect command paths against the swappable entrance, even when the
    # context helper launched us with a pinned physical AQG_ROOT.
    return migrate.logical_root(root)


class HostAdapter(ABC):
    """One host's mechanics behind the dispatcher's vocabulary."""

    client_id: str = ""

    #: Does this host's hook command name the AQG root *relatively* — via
    #: ``$AQG_ROOT`` or an equivalent indirection — so that "complete against
    #: the staged target" still holds once the root symlink is swapped?
    #:
    #: The planner's target-relative rule rests on this and on nothing else,
    #: and it is a property of how each host SPELLS its command, which
    #: ``verify`` cannot observe: ``verify`` stamps the root it PASSED, never
    #: the root the adapter USED. ``generic._inspect`` takes ``root`` and
    #: ignores it, and so may a future or third-party adapter.
    #:
    #: Default False so the guarantee is opt-in and the failure direction is
    #: closed: no declaration, no provenance, and the planner keeps its
    #: conservative merge. Codex is why this is a declaration rather than an
    #: assumption — its command embeds an absolute versioned path.
    hook_command_is_root_relative: bool = False

    #: Opt-in for an adapter that serves several hosts and therefore sets
    #: ``client_id`` per instance. It is an explicit declaration rather than a
    #: silent exemption, and the guarantee it relaxes does not disappear:
    #: ``Evidence`` refuses an empty client id whichever way it was set.
    serves_many_hosts: bool = False

    def __init_subclass__(cls, **kwargs: Any) -> None:
        # At class-definition time, not first use: an adapter that forgot its id
        # would otherwise register under "" and collide with the next one.
        super().__init_subclass__(**kwargs)
        if cls.serves_many_hosts:
            return
        if not isinstance(cls.client_id, str) or not cls.client_id.strip():
            raise AdapterError(
                f"{cls.__name__} must declare a non-empty client_id"
            )

    @abstractmethod
    def _inspect(self, root: Optional[Path] = None) -> Tuple[str, str]:
        """Return ``(status, detail)`` for this host's managed hooks. Read-only.

        The documented seam. ``verify`` is concrete precisely so that routing
        through the status check is a property of the base class rather than
        something each adapter re-implements identically — and so a contract
        test may substitute it without reaching past the contract.

        *root* is the tree to compare the host's configuration against, and it
        is not always the one that is live: during an update the caller passes
        the STAGED target, because "are these hooks complete" is only an
        answerable question once you say complete for what.
        """

    def _observed_routes(self) -> Optional[Tuple[str, ...]]:
        """Which AQG skills are routed into this host right now. Read-only.

        Optional seam, unlike ``_inspect``. An adapter for a host whose skill
        layout AQG has not verified returns ``None`` and the planner leaves its
        skills alone. That is a CHANGE, not the status quo: before this, those
        hosts were planned from the recorded roster, which is empty on every
        machine, so each of them collected one `route_skill` per shipped skill
        on every check — for hosts usually not installed at all. Leaving them
        alone is better and still not maintenance; see the known gap in
        `docs/UPDATE_ARCHITECTURE.md` §8.
        """
        return None

    def pinned_command_paths(self) -> Tuple[Path, ...]:
        """Absolute paths this host's INSTALLED hook commands will execute.

        Non-empty means the host is version-pinned: its commands name a tree
        directly instead of following the AQG root, so a root swap does not
        change what they run. Two callers depend on that — the apply gate, to
        decide a pending hook change for this host cannot strand it, and
        ``prune_versions``, to refuse to delete the tree those paths live in.

        Empty by default, and empty is NOT "safe to swap": it is "this host has
        not said", which every caller must read as the conservative answer.
        """
        return ()

    def verify(
        self,
        *,
        state: Optional[Dict[str, Any]] = None,
        target_root: Optional[Path] = None,
    ) -> Evidence:
        """Report what is installed on this host. Read-only.

        *target_root* is the tree the update is going TO. Passed through
        ``verify`` rather than through ``adapter_for`` on purpose: a root is
        host-neutral, unlike a settings path, and this is already the method
        that takes what the caller knows and the adapter cannot.
        """
        # Target-relative when the target can answer, live-relative otherwise.
        # The fallback matters: an unusable target must degrade to exactly the
        # behaviour that existed before this parameter — the planner sees no
        # `hooks_checked_against` and plans its conservative merge — and must
        # NOT become an adapter failure. A raised error here lands in
        # `run._collect_evidence`'s `dropped`, which is an outstanding item,
        # which holds the whole apply: a worse stall than the one this exists
        # to remove, reached by a stricter check.
        root = target_root if self.hook_command_is_root_relative else None
        try:
            status, detail = self._inspect(root)
        except Exception as exc:  # aqg: top-level boundary
            if root is None:
                raise
            # Deliberately every exception, not just `AdapterError`. The rule
            # stated above is "an unusable target must degrade"; catching one
            # type enforces it for one type and lets every other kind of
            # unusable target become a DROPPED host — which holds the apply
            # for every host on the machine, the worse stall, reached by a
            # stricter check. `require_aqg_root` raises only `AdapterError`
            # today, so this closes the class, not an instance.
            # The TARGET could not answer — it is not a checkout this adapter
            # can read a canonical hook set out of. Ask the live tree instead
            # and record that, so the planner falls back to its conservative
            # merge. Only the target is retried away: if the live tree cannot
            # answer either, the second call raises and the host is reported
            # dropped, which is the correct outcome and the pre-existing one.
            root = None
            status, detail = self._inspect(None)
            # The discarded exception was the only evidence that the
            # signature-verified staged tree could not be read. Dropping it
            # launders a corrupt payload into an ordinary re-merge, on a
            # channel whose defining failure mode is a stall nobody could see.
            # Name what actually went wrong. A bare `except Exception`
            # that keeps only "unreadable" turns a programming error in
            # `_inspect` — a parser bug, a typo in a new adapter — into
            # an indistinguishable "conservative fallback", and the one
            # string an operator ever sees says nothing about which.
            detail = (
                f"staged target unreadable ({type(exc).__name__}: {exc}), "
                f"live-relative: {detail}"
            )
        return self._evidence(
            hooks_status=status, hooks_detail=detail, state=state,
            hooks_checked_against=root,
        )

    def _evidence(
        self,
        *,
        hooks_status: str,
        hooks_detail: str,
        state: Optional[Dict[str, Any]],
        hooks_checked_against: Optional[Path] = None,
    ) -> Evidence:
        """Convenience constructor. The status check lives on ``Evidence``."""
        return Evidence(
            client_id=self.client_id,
            hooks_status=hooks_status,
            hooks_detail=hooks_detail,
            recorded_version=recorded_version_for(self.client_id, state),
            routed_skills=self._observed_routes(),
            hooks_checked_against=(
                None if hooks_checked_against is None
                else str(_canonical_path(hooks_checked_against))
            ),
        )


def _canonical_path(path: Path) -> Path:
    """One spelling, so the planner's comparison is about trees and not text."""
    return migrate._canonical(Path(path))

