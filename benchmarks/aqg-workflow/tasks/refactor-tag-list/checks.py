"""Held-out deterministic checks for the behavior-preserving refactor."""
from __future__ import annotations


def run_checks(fn):
    failures = []
    valid = [
        (["Python", " python ", "API"], ("python", "api")),
        (["one", "Two", "one"], ("one", "two")),
        (["  " , "A"], ("a",)),
        ([], ()),
    ]
    for value, expected in valid:
        try:
            actual = fn(value)
            if actual != expected:
                failures.append(f"{value!r}: expected {expected!r}, got {actual!r}")
        except Exception as exc:  # aqg: top-level boundary — untrusted callback must fail the case
            failures.append(f"{value!r}: raised {type(exc).__name__}: {exc}")
    for value in ("python", ["python", 3], None):
        try:
            fn(value)
            failures.append(f"{value!r}: accepted malformed tag list")
        except (TypeError, ValueError):
            pass
        except Exception as exc:  # aqg: top-level boundary — only validation errors satisfy this case
            failures.append(f"{value!r}: wrong exception {type(exc).__name__}")
    return failures
