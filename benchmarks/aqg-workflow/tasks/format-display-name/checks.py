"""Held-out deterministic checks for the profile-card feature."""
from __future__ import annotations


def run_checks(fn):
    failures = []
    valid = [
        (("Ada", "Lovelace"), "Ada Lovelace"),
        (("  Ada ", " Lovelace  "), "Ada Lovelace"),
        (("Ada", ""), "Ada"),
        (("", "Lovelace"), "Lovelace"),
    ]
    for args, expected in valid:
        try:
            actual = fn(*args)
            if actual != expected:
                failures.append(f"{args!r}: expected {expected!r}, got {actual!r}")
        except Exception as exc:  # aqg: top-level boundary — untrusted callback must fail the case
            failures.append(f"{args!r}: raised {type(exc).__name__}: {exc}")
    for args in (("", ""), (None, "Ada"), ("Ada", 7)):
        try:
            fn(*args)
            failures.append(f"{args!r}: accepted invalid name parts")
        except (TypeError, ValueError):
            pass
        except Exception as exc:  # aqg: top-level boundary — only validation errors satisfy this case
            failures.append(f"{args!r}: wrong exception {type(exc).__name__}")
    return failures
