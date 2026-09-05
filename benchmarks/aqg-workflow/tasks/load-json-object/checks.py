"""Held-out scorer for a JSON object-only boundary."""
from __future__ import annotations

from typing import Callable

_VALID = [("{}", {}), ("{\"theme\": \"dark\", \"retries\": 2}", {"theme": "dark", "retries": 2})]
_REJECT = ["[]", "null", "42", "\"text\"", "{bad}", ""]


def run_checks(fn: Callable[[str], dict]) -> list[str]:
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
