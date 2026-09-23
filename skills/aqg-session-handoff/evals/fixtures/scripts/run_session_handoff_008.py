#!/usr/bin/env python3
"""Deterministic validation-edge harness for aqg-session-handoff."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "skills/aqg-session-handoff/scripts/aqg_session_handoff.py"
OUT = Path.cwd() / "aqg_handoff_boundary/008-validation-edge"


VALID = (
    "## 1. Goal\nShip the fixture.\n"
    "## 2. Environment\nisolated fixture; secret-free.\n"
    "## 3. Done so far\nPrepared fixture; no audit.\n"
    "## 4. Current Working State\nworktree clean\n"
    "## 5. Next\nRun the focused check.\n"
    "## 6. Discipline traps\nnone\n"
    "## 7. First step\nRun the focused check.\n"
    "## 8. Open decisions\nnone\n"
)


def run(name: str, text: str) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--json"],
        input=text,
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError:
        payload = {"json_error": proc.stdout}
    return {
        "name": name,
        "exit_code": proc.returncode,
        "valid": payload.get("valid"),
        "violations": payload.get("violations", []),
        "stderr": proc.stderr,
    }


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    probe = OUT / "probe.txt"
    probe.write_text("validation-edge-probe\n", encoding="utf-8")
    before = sha(probe)
    cases = [
        ("valid", VALID),
        ("duplicate-header", VALID + "## 1. Goal\nduplicate\n"),
        ("required-none", VALID.replace("Ship the fixture.", "none")),
        ("missing-actor", VALID.replace("## 8. Open decisions\nnone", "## 8. Open decisions\n- decide later")),
        ("missing-audit-state", VALID.replace("Prepared fixture; no audit.", "Prepared fixture.")),
        ("synthetic-secret", VALID.replace("secret-free", "key " + "sk-proj-" + "x" * 24)),
    ]
    results = [run(name, text) for name, text in cases]
    after = sha(probe)
    (OUT / "results.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    (OUT / "probe.sha256.before").write_text(before + "\n", encoding="utf-8")
    (OUT / "probe.sha256.after").write_text(after + "\n", encoding="utf-8")
    (OUT / "commands.log").write_text(
        f"python3 {SCRIPT} validate --json < validation inputs\n"
        "cases: valid duplicate-header required-none missing-actor missing-audit-state synthetic-secret\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
