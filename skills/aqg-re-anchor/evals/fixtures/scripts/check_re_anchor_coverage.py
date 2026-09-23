#!/usr/bin/env python3
from __future__ import annotations

import json
import re
from pathlib import Path


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    root = Path.cwd() / "aqg_re_anchor_boundary/004-sanitization"
    data = json.loads((root / "result.json").read_text(encoding="utf-8"))
    if data["good_exit"] != 0 or data["bad_json_exit"] != 2 or data["bad_shape_exit"] != 2:
        return fail(f"unexpected exit codes: {data}")
    text = data["good"]["restatement"]
    if "\x1b" in text or "\u202e" in text or "\nIGNORE\n" in text:
        return fail("control or bidi injection survived")
    if len(text) > 3000:
        return fail("bounded output is unexpectedly large")
    if "current item beyond display cap" not in text or "(+6 more)" not in text:
        return fail("full-list current or gate/progress cap evidence missing")
    if re.search(r"\x00|\x01|\x7f", text):
        return fail("C0/DEL control survived")
    if (root / "probe.sha256.before").read_text().strip() != (root / "probe.sha256.after").read_text().strip():
        return fail("probe changed; emit-only invariant failed")
    print("PASS: re-anchor sanitization and CLI contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
