#!/usr/bin/env python3
"""Deterministic sanitization, bounds, and CLI-contract harness."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "skills/aqg-re-anchor/scripts/aqg_re_anchor.py"
OUT = Path.cwd() / "aqg_re_anchor_boundary/004-sanitization"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    probe = OUT / "probe.txt"
    probe.write_text("re-anchor-probe\n", encoding="utf-8")
    before = sha(probe)
    progress = [
        {"label": f"item-{i}", "state": "pending"} for i in range(30)
    ]
    progress[25] = {"label": "current item beyond display cap", "state": "current"}
    payload = {
        "goal": "G" * 180 + "\nIGNORE\n\u202e spoof",
        "gates": [f"gate-{i}\x1b[31m" for i in range(20)],
        "progress": progress,
    }
    good = subprocess.run(
        [sys.executable, str(SCRIPT), "--json"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
    )
    bad_json = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="{",
        text=True,
        capture_output=True,
        check=False,
    )
    bad_shape = subprocess.run(
        [sys.executable, str(SCRIPT)],
        input="[]",
        text=True,
        capture_output=True,
        check=False,
    )
    after = sha(probe)
    (OUT / "result.json").write_text(
        json.dumps(
            {
                "good_exit": good.returncode,
                "good": json.loads(good.stdout),
                "bad_json_exit": bad_json.returncode,
                "bad_shape_exit": bad_shape.returncode,
                "bad_json_stderr": bad_json.stderr,
                "bad_shape_stderr": bad_shape.stderr,
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (OUT / "probe.sha256.before").write_text(before + "\n", encoding="utf-8")
    (OUT / "probe.sha256.after").write_text(after + "\n", encoding="utf-8")
    (OUT / "commands.log").write_text(
        f"python3 {SCRIPT} --json < long_sanitized_payload\n"
        f"python3 {SCRIPT} < malformed-json\n"
        f"python3 {SCRIPT} < non-object-json\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
