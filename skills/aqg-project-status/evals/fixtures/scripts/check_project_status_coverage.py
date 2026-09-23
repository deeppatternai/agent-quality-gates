#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    root = Path.cwd() / "aqg_project_status_boundary/007-translation-guard"
    segments = json.loads((root / "segments.json").read_text(encoding="utf-8"))
    result = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if int(result["segments_exit"]) != 0 or int(result["safe_exit"]) != 0 or int(result["bad_exit"]) != 0:
        return fail(f"translation command failed: {result}")
    if result["segment_count"] < 1 or result["bad_equals_canonical"] is not True:
        return fail("fact-token guard did not fail closed")
    if result["safe_differs_from_canonical"] is not True:
        return fail("safe translation did not apply")
    if int(result["format_conflict_exit"]) != 2:
        return fail("format/json conflict did not return usage exit 2")
    if any(token in json.dumps(segments) for token in ["D-7", "event_count", "high"]):
        return fail("translation segments exposed structural facts")
    print("PASS: project-status translation and format contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
