from __future__ import annotations

from typing import Callable


def run_checks(fn: Callable[[str], str]) -> list[str]:
    failures: list[str] = []
    for value, expected in (("Content-Type", "content-type"), ("X_A", "x_a"), ("ETag", "etag")):
        try:
            got = fn(value)
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"valid {value!r} raised {exc!r}")
        else:
            if got != expected:
                failures.append(f"valid {value!r} -> {got!r}, expected {expected!r}")
    for value in ("", "Bad Header", "X:Injected", "line\nfold", " leading"):
        try:
            got = fn(value)
        except ValueError:
            continue
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"invalid {value!r} raised {type(exc).__name__}, expected ValueError")
        else:
            failures.append(f"invalid {value!r} accepted -> {got!r}")
    return failures
