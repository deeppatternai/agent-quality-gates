"""Held-out scorer for strict date-only API input."""
from __future__ import annotations

from datetime import date
from typing import Callable

_VALID = [("2026-01-31", date(2026, 1, 31)), ("2024-02-29", date(2024, 2, 29))]
_REJECT = ["2026-02-29", "2026-1-2", "20260102", "2026-01-02T00:00:00", "2026-01-02T00:00:00Z", "", " "]


def run_checks(fn: Callable[[str], date]) -> list[str]:
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
