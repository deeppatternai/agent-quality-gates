#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    root = Path.cwd() / "aqg_handoff_boundary/008-validation-edge"
    results = json.loads((root / "results.json").read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in results}
    if by_name["valid"]["exit_code"] != 0 or by_name["valid"]["valid"] is not True:
        return fail("valid control did not pass")
    for name in ["duplicate-header", "required-none", "missing-actor", "missing-audit-state", "synthetic-secret"]:
        item = by_name[name]
        if item["exit_code"] != 1 or item["valid"] is not False:
            return fail(f"{name} did not fail closed")
    if "sk-proj-" in json.dumps(by_name["synthetic-secret"]):
        return fail("secret-shaped input was echoed by the validation result")
    before = (root / "probe.sha256.before").read_text().strip()
    after = (root / "probe.sha256.after").read_text().strip()
    if before != after:
        return fail("probe changed; read-only invariant failed")
    print("PASS: handoff validation edge contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
