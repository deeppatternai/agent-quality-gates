#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    root = Path.cwd() / "aqg_skill_validator_boundary/003-contract-strict"
    data = json.loads((root / "result.json").read_text(encoding="utf-8"))
    normal = data["normal"]
    strict = data["strict"]
    if normal["is_valid"] is not False:
        return fail("missing entry-script contract was not a normal violation")
    if strict["computed_exit_code"] != 1:
        return fail("strict result did not fail")
    if not strict["warnings"]:
        return fail("strict case did not preserve advisory warning evidence")
    joined = json.dumps(normal).lower()
    if "exit-code" not in joined and "0/1/2/3/70" not in joined:
        return fail("exit-code contract evidence is missing")
    if data["skill_hash_before"] != data["skill_hash_after"]:
        return fail("validator changed checked fixture")
    print("PASS: skill-validator contract and strict coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
