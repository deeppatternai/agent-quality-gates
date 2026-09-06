"""Repo-root pytest configuration — a report header, nothing else.

`pytest.ini` excludes the skill `self_test.py` files from collection because a
separate runner owns them (see the comment there). An excluded path produces no
`skipped`, `deselected` or `ignored` count, so without this line a bare `pytest`
looks identical whether those tests were covered or silently absent. Say so on
every run, and name the command that does gate them.
"""

from __future__ import annotations


def pytest_report_header() -> str:
    return (
        "aqg: skill self_tests are excluded from collection (see pytest.ini) — "
        "gate them with `bash scripts/run_skill_self_tests.sh`"
    )
