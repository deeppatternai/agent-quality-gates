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

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple

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
    """

    client_id: str
    hooks_status: str
    hooks_detail: str
    recorded_version: Optional[str]

    def __post_init__(self) -> None:
        # On the type, not in a helper: a guard an adapter can bypass by picking
        # a different constructor is a convention, not an invariant. This is also
        # where a multi-host adapter's per-instance client id gets checked, since
        # its class cannot declare one.
        if not isinstance(self.client_id, str) or not self.client_id.strip():
            raise AdapterError(
                f"evidence carries an empty client_id: {self.client_id!r}"
            )
        if self.hooks_status not in HOOK_STATUSES:
            raise AdapterError(
                f"{self.client_id}: unrecognized hook status "
                f"{self.hooks_status!r}; this adapter contract models "
                f"{list(HOOK_STATUSES)}"
            )


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
    return root


class HostAdapter(ABC):
    """One host's mechanics behind the dispatcher's vocabulary."""

    client_id: str = ""

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
    def _inspect(self) -> Tuple[str, str]:
        """Return ``(status, detail)`` for this host's managed hooks. Read-only.

        The documented seam. ``verify`` is concrete precisely so that routing
        through the status check is a property of the base class rather than
        something each adapter re-implements identically — and so a contract
        test may substitute it without reaching past the contract.
        """

    def verify(self, *, state: Optional[Dict[str, Any]] = None) -> Evidence:
        """Report what is installed on this host. Read-only."""
        status, detail = self._inspect()
        return self._evidence(hooks_status=status, hooks_detail=detail, state=state)

    def _evidence(
        self,
        *,
        hooks_status: str,
        hooks_detail: str,
        state: Optional[Dict[str, Any]],
    ) -> Evidence:
        """Convenience constructor. The status check lives on ``Evidence``."""
        return Evidence(
            client_id=self.client_id,
            hooks_status=hooks_status,
            hooks_detail=hooks_detail,
            recorded_version=recorded_version_for(self.client_id, state),
        )
