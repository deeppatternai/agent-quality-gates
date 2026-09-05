"""Starting point given to the agent. The agent edits this file.

In a real run the agent receives task.md + this seed; checks.py / good_ref.py /
bad_ref.py are held out — they are the hidden scorer, not shown to the agent.
"""
from __future__ import annotations


def parse_positive_int(s: str) -> int:
    """Parse a user-supplied page-size query param (e.g. ?page_size=5) into an
    integer and return it."""
    return int(s)
