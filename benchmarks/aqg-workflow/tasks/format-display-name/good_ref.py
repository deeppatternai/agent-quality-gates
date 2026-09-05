from __future__ import annotations


def format_display_name(given: str, family: str) -> str:
    if not isinstance(given, str) or not isinstance(family, str):
        raise TypeError("name parts must be strings")
    parts = [part.strip() for part in (given, family) if part.strip()]
    if not parts:
        raise ValueError("at least one name part is required")
    return " ".join(parts)
