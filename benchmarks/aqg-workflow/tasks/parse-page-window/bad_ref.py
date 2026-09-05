"""Lazy reference: parses the wire shape but never limits its resource use."""
from __future__ import annotations


def parse_page_window(s: str) -> tuple[int, int]:
    left, right = s.split(":")
    return int(left), int(right)
