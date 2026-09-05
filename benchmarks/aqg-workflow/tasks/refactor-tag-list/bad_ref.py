from __future__ import annotations


def refactor_tag_list(tags: list[str]) -> tuple[str, ...]:
    return tuple(tag.strip().lower() for tag in tags)
