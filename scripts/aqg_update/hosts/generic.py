"""Adapter for hosts with no verified lifecycle-hook surface.

For six of the twenty registered clients AQG has no verified way to deliver a
hook — the registry records that as ``hook_delivery="none"``, and this adapter is
what lets the update layer say so in the same vocabulary as everyone else.
Without it a dispatcher looping the registry would hit `AdapterError` for each of
them, which is correct today ("no adapter yet") but wrong once we know the real
answer is "there is nothing here for AQG to install".

That distinction is the whole point. ``missing`` means *hooks are absent and
should be installed*; ``not-applicable`` means *AQG has no verified way to
install them here*. Collapsing them would send a planner to install hooks it has
no mechanism for, every single run, forever.

Note the second phrasing carefully: for several of these hosts the registry
records that no surface has been **verified**, which is an evidence level, not a
claim that the host could never take hooks. Promoting the one into the other
would quietly close the door on hosts that merely have not been investigated.

This is the first adapter to serve more than one client, so its ``client_id`` is
per-instance rather than per-class. The base contract admits that explicitly
(``serves_many_hosts``) instead of letting an empty class attribute slip through:
the guarantee that no evidence carries an empty id moves to ``Evidence``, where
it still fires.

Skills routing is NOT handled here. These hosts do receive skills — that is why
they are in the registry at all — but routing them is the planner's job in a
later slice, and this adapter reports only on hooks.
"""

from __future__ import annotations

from typing import Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts import aqg_client_registry
except ImportError:  # invoked with scripts/ itself on sys.path
    import aqg_client_registry  # type: ignore[no-redef]

from .base import AdapterError, HostAdapter

#: Derived from the registry rather than restated, so the two cannot drift.
HOSTS_WITHOUT_HOOKS: Tuple[str, ...] = tuple(
    sorted(
        client_id
        for client_id, spec in aqg_client_registry.CLIENT_REGISTRY.items()
        if spec.hook_delivery == "none"
    )
)


def _require_no_hook_host(client_id: str) -> None:
    """Raise unless *client_id* is a registered host with no hook delivery."""
    spec = aqg_client_registry.CLIENT_REGISTRY.get(client_id)
    if spec is None:
        raise AdapterError(
            f"{client_id!r} is not a registered AQG client; "
            f"the generic adapter serves {list(HOSTS_WITHOUT_HOOKS)}"
        )
    if spec.hook_delivery != "none":
        # Reporting `not-applicable` for a host that DOES take hooks would make
        # the planner skip real hook work on it, silently and forever.
        raise AdapterError(
            f"{client_id!r} delivers hooks by {spec.hook_delivery!r}, so the "
            f"generic no-hook adapter must not be used for it"
        )


class GenericAdapter(HostAdapter):
    """One implementation for every host whose hook delivery is ``none``."""

    serves_many_hosts = True

    def __init__(self, *, client_id: str) -> None:
        _require_no_hook_host(client_id)
        self.client_id = client_id

    def _inspect(self, root: Optional[Path] = None) -> Tuple[str, str]:
        # Re-checked here, not only in the constructor: `client_id` is a public
        # attribute, so a validated instance can be reassigned afterwards. A
        # guard that runs once on a mutable field protects the constructor, not
        # the invariant.
        _require_no_hook_host(self.client_id)
        return (
            "not-applicable",
            f"AQG has no verified lifecycle-hook delivery for "
            f"{self.client_id}; it installs no hooks here",
        )
