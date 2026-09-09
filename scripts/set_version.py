#!/usr/bin/env python3
"""Sync current-release markers from VERSION using only the standard library.

    python scripts/set_version.py               # sync to VERSION
    python scripts/set_version.py 0.15.0        # set VERSION and sync
    python scripts/set_version.py --check       # read-only drift check

Only VERSION and the current-version lines in README.md / README.zh-CN.md are
managed. Historical notes, schema/tool/dependency versions, tags and signed
release manifests are independent and must not be rewritten here.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_NUMBER = r"(?:0|[1-9][0-9]*)"
_PRE_ID = rf"(?:{_NUMBER}|[0-9]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    rf"{_NUMBER}\.{_NUMBER}\.{_NUMBER}"
    rf"(?:-{_PRE_ID}(?:\.{_PRE_ID})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
)
# Explicit allowlist: exactly one marker per file, anchored to its semantic label.
_MARKERS = {
    "README.md": re.compile(r"(?m)^(Current version: `)[^`\r\n]*(`)"),
    "README.zh-CN.md": re.compile(r"(?m)^(当前版本：`)[^`\r\n]*(`)"),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", nargs="?", help="SemVer, optionally prefixed with v")
    parser.add_argument("--check", action="store_true", help="check against VERSION; never write")
    args = parser.parse_args(argv)
    if args.check and args.version is not None:
        parser.error("--check cannot be combined with a version; it checks VERSION")

    try:
        version_path = ROOT / "VERSION"
        original = version_path.read_bytes()
        version = (args.version.removeprefix("v") if args.version is not None
                   else original.decode("utf-8").strip())
        if not _SEMVER.fullmatch(version):
            raise ValueError("invalid version: expected SemVer X.Y.Z with optional prerelease/build")

        # Validate every target before any writes. Preserve all other bytes,
        # including CRLF in the READMEs. A missing/duplicate marker is an error.
        changes = []
        canonical = (version + "\n").encode("utf-8")
        if original != canonical:
            changes.append((version_path, canonical))
        for name, pattern in _MARKERS.items():
            path = ROOT / name
            before = path.read_bytes()
            after, count = pattern.subn(
                lambda match: match[1] + version + match[2], before.decode("utf-8")
            )
            if count != 1:
                raise ValueError(f"{name}: expected exactly one current-version marker, found {count}")
            updated = after.encode("utf-8")
            if updated != before:
                changes.append((path, updated))

        if args.check and changes:
            for path, _ in changes:
                reason = ("not normalized (expected version plus one LF newline)"
                          if path == version_path else f"out of sync with VERSION ({version})")
                print(f"{path.name}: {reason}", file=sys.stderr)
            print("Run: python scripts/set_version.py", file=sys.stderr)
            return 1
        for path, updated in changes:
            path.write_bytes(updated)
            print(f"updated {path.name} -> {version}")
        print(f"version: all markers agree at {version}")
        return 0
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"version sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
