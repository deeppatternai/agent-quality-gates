"""Starting point given to the agent. The hidden scorer is not shown."""
from __future__ import annotations


def _legacy_lower(tag: str) -> str:
    return tag.lower()


def refactor_tag_list(tags: list[str]) -> tuple[str, ...]:
    """Legacy tag normalization that needs safe contract hardening."""
    return tuple(_legacy_lower(tag) for tag in tags)
