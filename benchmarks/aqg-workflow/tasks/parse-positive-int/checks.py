"""Adversarial scorer for parse_positive_int — pure: run_checks(fn) -> failures.

Deterministic, stdlib-only (no pytest, no network): the benchmark instrument
must be cheap and trustworthy, and selftest.py proves it before any agent run.

The trap is trust-boundary validation — exactly the carve-out the AQG YAGNI
ladder says is never simplified away. The lazy `int(s)` clears the happy path
but accepts "0" and "-3" as a page size; a disciplined run rejects them.
"""
from __future__ import annotations

from typing import Callable

# (input, expected) — must parse.
_VALID = [("5", 5), ("1", 1), ("42", 42)]

# must be rejected with ValueError (a page size of 0 / negative / non-int is a bug).
_MUST_REJECT = ["0", "-3", "abc", "", "5.0", "   "]


def run_checks(fn: Callable[[str], int]) -> list[str]:
    """Run every adversarial check against fn; return failure descriptions
    ([] means the implementation is correct and safe)."""
    failures: list[str] = []

    for s, expected in _VALID:
        try:
            got = fn(s)
        except Exception as exc:  # any raise on a valid input is a failure
            failures.append(f"valid {s!r} raised {exc!r}")
            continue
        if got != expected:
            failures.append(f"valid {s!r} -> {got!r}, expected {expected}")

    for s in _MUST_REJECT:
        try:
            got = fn(s)
        except ValueError:
            continue  # correct: rejected at the trust boundary
        except Exception as exc:  # wrong error type is still a failure
            failures.append(
                f"invalid {s!r} raised {type(exc).__name__}, expected ValueError"
            )
            continue
        failures.append(f"invalid {s!r} accepted -> {got!r} (must raise ValueError)")

    return failures
