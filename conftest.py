"""Repo-root pytest configuration — a report header, nothing else.

`pytest.ini` excludes the skill `self_test.py` files from collection because a
separate runner owns them (see the comment there). An excluded path produces no
`skipped`, `deselected` or `ignored` count, so without this line a bare `pytest`
looks identical whether those tests were covered or silently absent. Say so on
every run, and name the command that does gate them.
"""

from __future__ import annotations

import os

import pytest


def pytest_configure() -> None:
    """Hermeticity: a test run must never start a real managed update check.

    `sessionstart_update_check.sh` now hangs off the session-start event of every
    hook-capable host, and several suites drive those adapters as real
    subprocesses (`test_cursor_support.py`, `test_work_code_client_support.py`).
    Without this line a plain `pytest` reaches the network, writes the
    developer's real `aqg-state`, and on a fresh state root would go on to stage
    an upgrade — from a test that was only ever asserting a JSON envelope.

    Unconditional, and `setdefault` is wrong here even though it reads politer.
    The launcher treats the switch as OFF unless the value is non-empty, so an
    inherited `AQG_NO_UPDATE_CHECK=""` -- which is what a shell leaves behind
    after `export AQG_NO_UPDATE_CHECK=` or an unset-then-set -- would be kept by
    `setdefault` and disarm the whole guard silently. Nobody runs the suite in
    order to keep a preference about the update channel.

    The suites that own the update channel take it back off with the
    `live_update_channel` fixture below. The name is
    `scripts.aqg_update.run.KILL_SWITCH`, spelled out rather than imported so
    that collecting one test file does not drag in the updater -- and pinned
    equal to it by a test in `tests/behavior/test_update_run.py`, so renaming
    the constant cannot disarm this quietly.
    """
    os.environ["AQG_NO_UPDATE_CHECK"] = "1"


@pytest.fixture
def live_update_channel(monkeypatch):
    """Opt a suite out of the session-wide kill switch set above.

    For the suites that own the update channel and therefore have to watch it
    actually run. Opt-in by name rather than a per-file autouse fixture, so
    there is one definition to find and a third such suite cannot quietly grow
    its own variant:

        pytestmark = pytest.mark.usefixtures("live_update_channel")

    Taking the switch off is only safe because those suites isolate what the
    check would otherwise touch -- `AQG_ROOT`, `AQG_STATE_ROOT`, the keyring and
    the remote all point into `tmp_path`. A suite that cannot say that about
    itself must not use this.
    """
    monkeypatch.delenv("AQG_NO_UPDATE_CHECK", raising=False)


def pytest_report_header() -> str:
    return (
        "aqg: skill self_tests are excluded from collection (see pytest.ini) — "
        "gate them with `bash scripts/run_skill_self_tests.sh`"
    )
