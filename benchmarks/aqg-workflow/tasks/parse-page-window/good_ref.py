"""Disciplined reference: validates both parts of the public page window."""
from __future__ import annotations


def parse_page_window(s: str) -> tuple[int, int]:
    try:
        offset_text, limit_text = s.split(":")
        offset = int(offset_text)
        limit = int(limit_text)
    except ValueError as exc:
        raise ValueError("expected offset:limit integers") from exc
    if offset < 0 or not 1 <= limit <= 100:
        raise ValueError("invalid page window")
    return offset, limit
