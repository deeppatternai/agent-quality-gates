#!/usr/bin/env python3
"""Translation-segment and fact-token guard harness."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[5]
SCRIPT = ROOT / "skills/aqg-project-status/scripts/aqg_project_status.py"
OUT = Path.cwd() / "aqg_project_status_boundary/007-translation-guard"


def event(event_id: str, kind: str, payload: dict) -> dict:
    return {
        "schema_version": "1.0",
        "event_id": event_id,
        "project": "o/r",
        "source": "eaf",
        "source_ref": {"run_id": "translation-007"},
        "kind": kind,
        "payload": payload,
        "occurred_at": "2026-09-01T10:00:00Z",
        "recorded_at": "2026-09-01T10:00:00Z",
        "ledger_seq": int(event_id.rsplit("-", 1)[-1]),
    }


def run(args: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(SCRIPT), *args], text=True, capture_output=True, env=env, check=False)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="aqg-status-007-") as tmp:
        xdg = Path(tmp) / "xdg"
        events = xdg / "aqg/ledger/o/r/events.jsonl"
        events.parent.mkdir(parents=True)
        events.write_text(
            "\n".join([
                json.dumps(event("event-1", "progress", {"phase_event": "started", "title": "Ship alpha"})),
                json.dumps(event("event-2", "milestone", {
                    "milestone_id": "M-7",
                    "action": "planned",
                    "title": "Translation milestone",
                    "summary": "Review the report",
                    "key_outcome": "A stable status view",
                    "estimated_size": "small",
                })),
                json.dumps(event("event-3", "defect", {"defect_id": "D-7", "action": "opened", "title": "Needs review", "severity": "high"})),
            ]) + "\n",
            encoding="utf-8",
        )
        env = {**os.environ, "AQG_ROOT": str(ROOT), "XDG_DATA_HOME": str(xdg)}
        seg = run(["--project-id", "o/r", "--emit-translation-segments"], env)
        segments_payload = json.loads(seg.stdout)
        segments = segments_payload.get("segments", [])
        safe_file = Path(tmp) / "safe.json"
        bad_file = Path(tmp) / "bad.json"
        safe_file.write_text(json.dumps({"segments": segments, "translations": [f"[translated] {s}" for s in segments], "target_lang": "xx"}), encoding="utf-8")
        bad_file.write_text(json.dumps({"segments": segments, "translations": [f"[translated] 999 {s}" for s in segments], "target_lang": "xx"}), encoding="utf-8")
        canonical = run(["--project-id", "o/r", "--format", "markdown", "--no-drain"], env)
        safe = run(["--project-id", "o/r", "--format", "markdown", "--no-drain", "--apply-translation", str(safe_file), "--lang", "xx"], env)
        bad = run(["--project-id", "o/r", "--format", "markdown", "--no-drain", "--apply-translation", str(bad_file), "--lang", "xx"], env)
        conflict = run(["--project-id", "o/r", "--format", "json", "--json"], env)
        (OUT / "segments.json").write_text(seg.stdout, encoding="utf-8")
        (OUT / "canonical.md").write_text(canonical.stdout, encoding="utf-8")
        (OUT / "safe.md").write_text(safe.stdout, encoding="utf-8")
        (OUT / "bad.md").write_text(bad.stdout, encoding="utf-8")
        (OUT / "result.json").write_text(json.dumps({
            "segments_exit": seg.returncode,
            "safe_exit": safe.returncode,
            "bad_exit": bad.returncode,
            "bad_stderr": bad.stderr,
            "format_conflict_exit": conflict.returncode,
            "segment_count": len(segments),
            "safe_differs_from_canonical": safe.stdout != canonical.stdout,
            "bad_equals_canonical": bad.stdout == canonical.stdout,
        }, indent=2) + "\n", encoding="utf-8")
        (OUT / "commands.log").write_text(
            f"python3 {SCRIPT} --project-id o/r --emit-translation-segments\n"
            f"python3 {SCRIPT} --apply-translation <safe> --lang xx\n"
            f"python3 {SCRIPT} --apply-translation <fact-fabricating> --lang xx\n"
            f"python3 {SCRIPT} --format json --json  # expected exit 2\n",
            encoding="utf-8",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
