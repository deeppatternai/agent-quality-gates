"""Starting point given to the agent. The hidden scorer is not shown."""
from __future__ import annotations


def parse_page_window(s: str) -> tuple[int, int]:
    """Parse an offset:limit page window."""
    offset, limit = s.split(":", 1)
    return int(offset), int(limit)
