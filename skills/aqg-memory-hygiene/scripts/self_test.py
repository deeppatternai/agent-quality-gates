#!/usr/bin/env python3
"""Minimal self-test for aqg_memory_hygiene.py.

Verifies the helper imports cleanly, `validate` passes a compliant dir (index
file excluded) and fails a node missing a required field, and `staleness` always
exits 0 while surfacing a stale volatile node. Exercises the exit-code contract
(0 happy path / 1 validation failure). stdlib only.
"""

from __future__ import annotations

import io
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import aqg_memory_hygiene as mh

_COMPLIANT = (
    "---\nname: feedback_x\ndescription: x\nmetadata:\n"
    "  type: feedback\n  status: active\n  volatility: durable\n"
    "  last_verified: 2020-01-01\n---\n\nbody\n"
)
_MISSING_FIELD = (
    "---\nname: y\ndescription: x\nmetadata:\n"
    "  type: feedback\n  volatility: durable\n  last_verified: 2020-01-01\n---\n\nbody\n"
)
_VOLATILE_STALE = (
    "---\nname: ref\ndescription: x\nmetadata:\n"
    "  type: reference\n  status: active\n  volatility: volatile\n"
    "  last_verified: 2020-01-01\n---\n\nbody\n"
)


def _run(argv: list[str]) -> tuple[int, str]:
    out = io.StringIO()
    with redirect_stdout(out):
        rc = mh.main(argv)
    return rc, out.getvalue()


def _mkdir_with(name: str, body: str, *, with_index: bool = False) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / name).write_text(body, encoding="utf-8")
    if with_index:
        (d / "MEMORY.md").write_text("# index\n", encoding="utf-8")
    return d


def test_validate_compliant_dir_exit_zero() -> None:
    d = _mkdir_with("feedback_x.md", _COMPLIANT, with_index=True)
    rc, text = _run(["validate", "--memory-dir", str(d)])
    assert rc == 0, f"validate returned {rc}: {text[:200]}"
    assert "PASS" in text


def test_validate_missing_field_exit_one() -> None:
    d = _mkdir_with("feedback_y.md", _MISSING_FIELD)
    rc, _ = _run(["validate", "--memory-dir", str(d)])
    assert rc == 1, f"missing required field should exit 1, got {rc}"


def test_staleness_always_exit_zero() -> None:
    d = _mkdir_with("ref.md", _VOLATILE_STALE)
    rc, text = _run(["staleness", "--memory-dir", str(d)])
    assert rc == 0, f"staleness must always exit 0, got {rc}"
    assert "ref.md" in text


if __name__ == "__main__":
    test_validate_compliant_dir_exit_zero()
    test_validate_missing_field_exit_one()
    test_staleness_always_exit_zero()
    print("OK: aqg_memory_hygiene self-test passed")
