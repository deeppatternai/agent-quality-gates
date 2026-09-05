from __future__ import annotations


def refactor_tag_list(tags: list[str]) -> tuple[str, ...]:
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise TypeError("tags must be a list of strings")
    result = []
    seen = set()
    for tag in tags:
        normalized = tag.strip().lower()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return tuple(result)
