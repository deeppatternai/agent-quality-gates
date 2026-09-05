"""Held-out scorer for a bounded pagination window."""
from __future__ import annotations

from typing import Callable

_VALID = [("0:25", (0, 25)), ("10:1", (10, 1)), ("500:100", (500, 100))]
_REJECT = ["-1:10", "0:0", "0:101", "0", "0:10:2", "a:10", "0:1.5", ""]


def run_checks(fn: Callable[[str], tuple[int, int]]) -> list[str]:
    failures: list[str] = []
    for value, expected in _VALID:
        try:
            got = fn(value)
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"valid {value!r} raised {exc!r}")
        else:
            if got != expected:
                failures.append(f"valid {value!r} -> {got!r}, expected {expected!r}")
    for value in _REJECT:
        try:
            got = fn(value)
        except ValueError:
            continue
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"invalid {value!r} raised {type(exc).__name__}, expected ValueError")
        else:
            failures.append(f"invalid {value!r} accepted -> {got!r}")
    return failures
