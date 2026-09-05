#!/usr/bin/env python3
"""Simulation manifest CLI validator (Wave 2 #4).

CLI:
    python3 scripts/validate_simulation_manifest.py <manifest.json>

Exit codes: 0 valid / 1 invalid / 2 usage / 2 file not found
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2

# WB-06: manifests are small configs; bound the read so a giant regular file
# can't OOM the validator (FIFOs are already rejected by main's is_file check).
_MAX_MANIFEST_BYTES = 1024 * 1024  # 1 MiB


def _load(path: Path) -> dict:
    with path.open("rb") as fh:
        raw = fh.read(_MAX_MANIFEST_BYTES + 1)
    if len(raw) > _MAX_MANIFEST_BYTES:
        raise ValueError(f"manifest exceeds {_MAX_MANIFEST_BYTES}-byte limit")
    text = raw.decode("utf-8")
    return json.loads(text, parse_constant=lambda x: (_ for _ in ()).throw(ValueError(f"non-finite JSON constant: {x}")))


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if len(args) != 1:
        print("usage: validate_simulation_manifest.py <manifest.json>", file=sys.stderr)
        return EXIT_USAGE
    path = Path(args[0])
    if not path.is_file():
        print(f"ERROR: file not found: {path}", file=sys.stderr)
        return EXIT_USAGE
    try:
        record = _load(path)
    except RecursionError:
        # WB-02: deeply nested JSON overflows the decoder's recursion limit; this
        # is NOT a ValueError/JSONDecodeError, so it slipped past as an uncaught
        # traceback. Treat it as invalid (too deeply nested) → clean EXIT_INVALID.
        print("ERROR: invalid JSON: nested too deeply", file=sys.stderr)
        return EXIT_INVALID
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"ERROR: invalid JSON: {exc}", file=sys.stderr)
        return EXIT_INVALID
    if not isinstance(record, dict):
        print("ERROR: manifest must be a JSON object", file=sys.stderr)
        return EXIT_INVALID

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _simulation_redaction import check_simulation_manifest

    result = check_simulation_manifest(record)
    if result.is_safe:
        print(f"OK: {path.name} valid")
        return EXIT_OK
    print(f"FAIL: {path.name} invalid ({len(result.violations)} violation(s))", file=sys.stderr)
    for v in result.violations:
        print(f"  - {v}", file=sys.stderr)
    return EXIT_INVALID


if __name__ == "__main__":
    raise SystemExit(main())
