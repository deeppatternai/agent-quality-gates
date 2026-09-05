"""Held-out deterministic checks for the versioned API contract."""
from __future__ import annotations


def run_checks(fn):
    failures = []
    valid = [
        ({"id": "u1", "name": "Ada"}, {"id": "u1", "profile": {"display_name": "Ada"}}),
        ({"id": "u2", "profile": {"display_name": "Grace"}}, {"id": "u2", "profile": {"display_name": "Grace"}}),
    ]
    for value, expected in valid:
        try:
            actual = fn(value)
            if actual != expected:
                failures.append(f"{value!r}: expected {expected!r}, got {actual!r}")
        except Exception as exc:  # aqg: top-level boundary — untrusted callback must fail the case
            failures.append(f"{value!r}: raised {type(exc).__name__}: {exc}")
    invalid = [
        {"id": "", "name": "Ada"},
        {"id": "u1", "name": 3},
        {"id": "u1", "profile": {}},
        {"id": "u1", "name": "Ada", "admin": True},
        {"name": "Ada"},
        [],
    ]
    for value in invalid:
        try:
            fn(value)
            failures.append(f"{value!r}: accepted malformed API record")
        except (TypeError, ValueError):
            pass
        except Exception as exc:  # aqg: top-level boundary — only validation errors satisfy this case
            failures.append(f"{value!r}: wrong exception {type(exc).__name__}")
    return failures
