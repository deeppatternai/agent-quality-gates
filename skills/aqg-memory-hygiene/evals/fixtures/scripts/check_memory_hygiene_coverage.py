#!/usr/bin/env python3
from __future__ import annotations

import json
from pathlib import Path


def fail(message: str) -> int:
    print(f"FAIL: {message}")
    return 1


def main() -> int:
    root = Path.cwd() / "aqg_memory_hygiene_boundary/007-superseded-pointer"
    validate = json.loads((root / "validate.json").read_text(encoding="utf-8"))
    stale = json.loads((root / "staleness.json").read_text(encoding="utf-8"))
    violations = "\n".join(validate.get("violations", [])).lower()
    if int((root / "validate.exit").read_text().strip()) != 1 or validate.get("ok") is not False:
        return fail("invalid pointer/future date did not fail validate")
    for token in ["dangling", "future"]:
        if token not in violations:
            return fail(f"missing violation evidence for {token}: {violations}")
    if int((root / "staleness.exit").read_text().strip()) != 0:
        return fail("staleness did not retain exit 0")
    stale_files = {item.get("file") for item in stale.get("stale", [])}
    skipped_files = {item.get("file") for item in stale.get("skipped", [])}
    if "active.md" not in stale_files or "superseded-valid.md" not in skipped_files:
        return fail(f"unexpected staleness classification: {stale}")
    if (root / "snapshot.before").read_text().strip() != (root / "snapshot.after").read_text().strip():
        return fail("memory fixture changed")
    print("PASS: memory superseded-pointer coverage")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
