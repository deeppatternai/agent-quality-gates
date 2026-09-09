"""Adapter lookup — the dispatcher's only door into host mechanics.

The table is static and in-repository, mirroring DE's `client_hosts/registry.py`:
adapters are code, not plugins, so discovery by import scanning would buy nothing
and lose the ability to fail closed on an unknown name.

Every registry client has an adapter: dedicated Claude Code/Codex inspectors,
read-only managed hook inspectors, or explicit evidence of no verified hook
surface. Unknown ids raise rather than being mistaken for an uninstalled host.

The table maps an id to a zero-argument factory rather than to a class, because
one adapter now serves several hosts: the no-hook clients share `GenericAdapter`
and differ only by the id it is constructed with.
"""

from __future__ import annotations

from functools import partial
from typing import Callable, Dict, Iterable

from .base import AdapterError, Evidence, HostAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .generic import HOSTS_WITHOUT_HOOKS, GenericAdapter
from .managed import FAMILIES, ManagedAdapter

def build_adapter_table(
    *,
    explicit: Dict[str, Callable[[], HostAdapter]],
    generic_ids: Iterable[str],
) -> Dict[str, Callable[[], HostAdapter]]:
    """Merge dedicated adapters with the generic ones, refusing an overlap.

    A plain dict-literal merge is last-key-wins, so a client appearing in both
    would silently lose its dedicated adapter to the generic one — and the
    coverage test could not see it, because the id would still be present.
    """
    table = dict(explicit)
    for client_id in generic_ids:
        if client_id in table:
            raise AdapterError(
                f"{client_id!r} has both a dedicated adapter and a generic one; "
                f"the generic table must not shadow a dedicated adapter"
            )
        table[client_id] = partial(GenericAdapter, client_id=client_id)
    return table


_ADAPTERS: Dict[str, Callable[[], HostAdapter]] = build_adapter_table(
    explicit={
        ClaudeCodeAdapter.client_id: ClaudeCodeAdapter,
        CodexAdapter.client_id: CodexAdapter,
        **{client: partial(ManagedAdapter, client) for client in FAMILIES},
    },
    generic_ids=HOSTS_WITHOUT_HOOKS,
)


def available_clients() -> Tuple[str, ...]:
    """Client ids that can actually be driven today, sorted."""
    return tuple(sorted(_ADAPTERS))


def adapter_for(client_id: str) -> HostAdapter:
    """Return the adapter for *client_id*, or raise.

    Takes no host-specific arguments on purpose. A `settings_path` here would be
    Claude-shaped mechanics appearing in the very boundary meant to hide them,
    and would silently impose one host's constructor signature on every future
    adapter. Each adapter resolves its own location; a caller that genuinely
    needs to point one elsewhere (a test) constructs that adapter directly.
    """
    try:
        build = _ADAPTERS[client_id]
    except KeyError:
        raise AdapterError(
            f"no update adapter for client {client_id!r}; "
            f"driveable today: {list(available_clients())}"
        ) from None
    return build()


__all__ = [
    "AdapterError",
    "build_adapter_table",
    "Evidence",
    "HostAdapter",
    "adapter_for",
    "available_clients",
]
