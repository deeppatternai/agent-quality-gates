"""Deterministic date-boundary tests via the pure core (injected `today`).

CLI tests use real today, so the ±1-day skew-grace boundary can only be pinned
precisely at the core level where `today` is a parameter. Locks the fix that the
local-verification pass on the real corpus surfaced (a date stamped one day ahead
of the machine's UTC clock must NOT be flagged as future).
"""

from __future__ import annotations

import datetime as dt

import _mh_core as core
from _mh_fixtures import compliant_meta, write_node


def _validate_single(tmp_path, today: dt.date) -> core.NodeResult:
    nodes = [core.parse_node(p) for p in core.iter_node_files(tmp_path)]
    idx = core.build_dir_index(nodes, core.index_files(tmp_path))
    return core.validate_node(nodes[0], today, idx)


def test_last_verified_today_passes(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="2026-01-10"))
    assert _validate_single(tmp_path, dt.date(2026, 1, 10)).ok


def test_last_verified_plus_one_within_skew_grace_passes(tmp_path):
    # +1 day: a local "today" can be one calendar day ahead of UTC — legitimate.
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="2026-01-11"))
    res = _validate_single(tmp_path, dt.date(2026, 1, 10))
    assert res.ok, f"today+1 must be within skew grace: {res.violations}"


def test_last_verified_plus_two_exceeds_grace_rejected(tmp_path):
    write_node(tmp_path, "feedback_a.md", metadata=compliant_meta(last_verified="2026-01-12"))
    res = _validate_single(tmp_path, dt.date(2026, 1, 10))
    assert not res.ok
    assert any("future" in m for m in res.violations)


def test_is_future_helper_boundary():
    today = dt.date(2026, 1, 10)
    assert core.is_future(dt.date(2026, 1, 12), today) is True
    assert core.is_future(dt.date(2026, 1, 11), today) is False  # +1 grace
    assert core.is_future(dt.date(2026, 1, 10), today) is False
    assert core.is_future(dt.date(2026, 1, 1), today) is False
