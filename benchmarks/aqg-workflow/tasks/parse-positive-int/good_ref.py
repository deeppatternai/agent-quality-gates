"""Disciplined reference: validates at the trust boundary (the AQG ladder
carve-out). selftest.py requires this to pass every check in checks.py."""
from __future__ import annotations


def parse_positive_int(s: str) -> int:
    """Parse a page-size query param into an int >= 1; raise ValueError otherwise."""
    n = int(s)  # raises ValueError on a non-integer string
    if n < 1:
        raise ValueError(f"not a positive integer: {n}")
    return n
