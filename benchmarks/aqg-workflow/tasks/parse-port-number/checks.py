from __future__ import annotations

from typing import Callable


def run_checks(fn: Callable[[str], int]) -> list[str]:
    failures: list[str] = []
    for value, expected in (("1", 1), ("443", 443), ("65535", 65535)):
        try:
            got = fn(value)
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"valid {value!r} raised {exc!r}")
        else:
            if got != expected:
                failures.append(f"valid {value!r} -> {got!r}, expected {expected}")
    for value in ("0", "65536", "-1", "abc", "5.5", ""):
        try:
            got = fn(value)
        except ValueError:
            continue
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"invalid {value!r} raised {type(exc).__name__}, expected ValueError")
        else:
            failures.append(f"invalid {value!r} accepted -> {got!r}")
    return failures
