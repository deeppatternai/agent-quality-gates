"""Held-out scorer for upload-manifest validation.

Larger on purpose. The 2026-09 pilot found the existing tasks finish in 3 turns,
which leaves an engineering-discipline treatment nothing to act on (contract
§15.12), and it also found tasks whose every cell fails the identical checks and
therefore carry no signal at all (§15.11). This one spreads its checks across
happy path, normalization, traversal, type confusion, limits and aliasing so a
careful implementation can separate itself from a careless one.

`run_checks` is the entry point ON PURPOSE, not `run_all_checks`: the n_checks
sentinel in `recompute_r_level_saturation.n_checks_for` calls `run_checks`, and
a task that defined only the other entry point would break that measurement.
Everything reaches the solution through `fn` alone for the same reason -- the
sentinel replaces exactly that one callable.
"""
from __future__ import annotations

from typing import Callable

_TXT = "text/plain"
_MAX_FILE = 10 * 1024 * 1024
_MAX_TOTAL = 50 * 1024 * 1024
# How many max-size files land exactly on the total cap -- written as the
# relationship rather than as 5, so a later limit change cannot leave the
# "at the limit" and "over the limit" cases quietly testing something else.
_FILES_TO_FILL = _MAX_TOTAL // _MAX_FILE


def _entry(name="notes.txt", size=10, content_type=_TXT, **extra):
    return {"name": name, "size": size, "content_type": content_type, **extra}


# (label, payload, expected) -- accepted manifests and what they normalize to.
_VALID = [
    ("single entry", [_entry()], [{"name": "notes.txt", "size": 10, "content_type": _TXT}]),
    ("extra keys ignored", [_entry(owner="ada", id=7)],
     [{"name": "notes.txt", "size": 10, "content_type": _TXT}]),
    ("nested path kept", [_entry(name="reports/q3/summary.csv", content_type="text/csv")],
     [{"name": "reports/q3/summary.csv", "size": 10, "content_type": "text/csv"}]),
    ("order preserved", [_entry(name="b.txt"), _entry(name="a.txt")],
     [{"name": "b.txt", "size": 10, "content_type": _TXT},
      {"name": "a.txt", "size": 10, "content_type": _TXT}]),
    ("every allowed type", [_entry(name=f"f{i}", content_type=t) for i, t in enumerate(
        ("text/plain", "text/csv", "application/json", "image/png", "image/jpeg"))],
     [{"name": f"f{i}", "size": 10, "content_type": t} for i, t in enumerate(
         ("text/plain", "text/csv", "application/json", "image/png", "image/jpeg"))]),
    ("file at the limit", [_entry(size=_MAX_FILE)],
     [{"name": "notes.txt", "size": _MAX_FILE, "content_type": _TXT}]),
    ("total at the limit",
     [_entry(name=f"f{i}", size=_MAX_FILE) for i in range(_FILES_TO_FILL)],
     [{"name": f"f{i}", "size": _MAX_FILE, "content_type": _TXT}
      for i in range(_FILES_TO_FILL)]),
    ("leading ./ normalized", [_entry(name="./notes.txt")],
     [{"name": "notes.txt", "size": 10, "content_type": _TXT}]),
]

# (label, payload) -- manifests that must raise ValueError.
_REJECT = [
    ("not a list", {"name": "a"}),
    ("a bare string", "notes.txt"),
    ("empty manifest", []),
    ("entry not an object", ["notes.txt"]),
    ("missing name", [{"size": 10, "content_type": _TXT}]),
    ("missing size", [{"name": "a.txt", "content_type": _TXT}]),
    ("missing content_type", [{"name": "a.txt", "size": 10}]),
    ("parent traversal", [_entry(name="../secrets.txt")]),
    ("traversal mid-path", [_entry(name="reports/../../etc/passwd")]),
    ("absolute path", [_entry(name="/etc/passwd")]),
    ("backslash separator", [_entry(name="reports\\q3.txt")]),
    ("empty name", [_entry(name="")]),
    ("name is not a string", [_entry(name=123)]),
    ("NUL in name", [_entry(name="a\x00.txt")]),
    ("bare dot", [_entry(name=".")]),
    ("size zero", [_entry(size=0)]),
    ("negative size", [_entry(size=-1)]),
    ("size is a string", [_entry(size="10")]),
    ("size is a float", [_entry(size=10.5)]),
    ("size is a bool", [_entry(size=True)]),
    ("file over the limit", [_entry(size=_MAX_FILE + 1)]),
    ("total over the limit", [_entry(name=f"f{i}", size=_MAX_FILE)
                              for i in range(_FILES_TO_FILL)]
     + [_entry(name="last", size=1)]),
    ("unknown content type", [_entry(content_type="application/x-msdownload")]),
    ("content type is not a string", [_entry(content_type=None)]),
    ("duplicate name", [_entry(name="a.txt"), _entry(name="a.txt")]),
    ("duplicate after normalization", [_entry(name="a.txt"), _entry(name="./a.txt")]),
    ("one bad entry among good ones", [_entry(name="a.txt"), _entry(name="../b.txt"),
                                       _entry(name="c.txt")]),
]


def run_checks(fn: Callable[[object], list]) -> list[str]:
    failures: list[str] = []
    for label, payload, expected in _VALID:
        try:
            got = fn(payload)
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"{label}: raised {exc!r}")
        else:
            if got != expected:
                failures.append(f"{label}: returned {got!r}")
    for label, payload in _REJECT:
        try:
            got = fn(payload)
        except ValueError:
            continue
        except Exception as exc:  # aqg: top-level boundary
            failures.append(f"{label}: raised {type(exc).__name__}, expected ValueError")
        else:
            failures.append(f"{label}: accepted, returned {got!r}")
    return failures
