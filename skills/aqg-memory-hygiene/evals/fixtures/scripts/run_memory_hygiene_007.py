#!/usr/bin/env python3
"""Hermetic superseded-pointer and future-date harness."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "skills/aqg-memory-hygiene/scripts/aqg_memory_hygiene.py"
OUT = Path.cwd() / "aqg_memory_hygiene_boundary/007-superseded-pointer"


def node(name: str, status: str, volatility: str, verified: str, extra: str = "") -> str:
    return (
        "---\n"
        f"name: {name}\ndescription: fixture\nmetadata:\n"
        f"  type: reference\n  status: {status}\n  volatility: {volatility}\n"
        f"  last_verified: {verified}\n{extra}"
        "---\nbody\n"
    )


def snapshot(root: Path) -> dict[str, str]:
    return {
        p.name: hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.glob("*.md"))
    }


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aqg-memory-007-") as tmp:
        memory = Path(tmp) / "memory"
        memory.mkdir()
        (memory / "active.md").write_text(node("active", "active", "volatile", "2026-01-01"), encoding="utf-8")
        (memory / "superseded-valid.md").write_text(
            node("old", "superseded", "volatile", "2026-01-01",
                 "  superseded_by: active\n  superseded_reason: replaced\n  superseded_date: 2026-02-01\n"),
            encoding="utf-8",
        )
        (memory / "superseded-null.md").write_text(
            node("retired", "superseded", "durable", "2026-01-01",
                 "  superseded_by: null\n  superseded_reason: retired\n  superseded_date: 2026-02-01\n"),
            encoding="utf-8",
        )
        (memory / "superseded-broken.md").write_text(
            node("broken", "superseded", "durable", "2026-01-01",
                 "  superseded_by: missing-node\n  superseded_reason: replaced\n  superseded_date: 2026-02-01\n"),
            encoding="utf-8",
        )
        (memory / "future.md").write_text(node("future", "active", "volatile", "2999-01-01"), encoding="utf-8")
        before = snapshot(memory)
        env = {**os.environ, "AQG_ROOT": str(ROOT)}
        validate = subprocess.run(
            [sys.executable, str(SCRIPT), "validate", "--memory-dir", str(memory), "--json"],
            text=True, capture_output=True, env=env, check=False,
        )
        stale = subprocess.run(
            [sys.executable, str(SCRIPT), "staleness", "--memory-dir", str(memory), "--json"],
            text=True, capture_output=True, env=env, check=False,
        )
        after = snapshot(memory)
        (OUT / "validate.json").write_text(validate.stdout, encoding="utf-8")
        (OUT / "staleness.json").write_text(stale.stdout, encoding="utf-8")
        (OUT / "validate.exit").write_text(str(validate.returncode) + "\n", encoding="utf-8")
        (OUT / "staleness.exit").write_text(str(stale.returncode) + "\n", encoding="utf-8")
        (OUT / "snapshot.before").write_text(json.dumps(before, sort_keys=True) + "\n", encoding="utf-8")
        (OUT / "snapshot.after").write_text(json.dumps(after, sort_keys=True) + "\n", encoding="utf-8")
        (OUT / "commands.log").write_text(
            f"python3 {SCRIPT} validate --memory-dir <temp> --json\n"
            f"python3 {SCRIPT} staleness --memory-dir <temp> --json\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
