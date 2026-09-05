#!/usr/bin/env python3
"""Minimal example check for the AQG skill authoring guide.

Demonstrates the §1.4 execution feedback contract:
- exit 0 on success
- exit 1 on expected check failure
- exit 2 on usage error
- exit 3 on schema / config error
- exit 70 on internal error

And the §5 path portability contract:
- --repo defaults to the current git root
- --required-file optionally validates the resolved repo

This file is a *template*. New skills should copy it into
``skills/aqg-<name>/scripts/<name>_check.py`` and replace the body with
real check logic.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


EXIT_SUCCESS = 0
EXIT_CHECK_FAIL = 1
EXIT_USAGE = 2
EXIT_SCHEMA = 3
EXIT_INTERNAL = 70


def _resolve_repo(explicit: str | None) -> Path:
    """Pick the repo path: explicit --repo arg or the current git root."""
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if not path.is_dir():
            raise ValueError(f"--repo path does not exist or is not a dir: {path}")
        return path
    proc = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=os.getcwd(),
        check=False,
    )
    if proc.returncode != 0:
        raise ValueError(
            "no --repo given and current directory is not inside a git repo"
        )
    return Path(proc.stdout.strip()).resolve()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="aqg-example: no-op check that demonstrates the §1.4 contract."
    )
    p.add_argument(
        "--message",
        default="hello from aqg-example",
        help="text to echo back as the check result",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="repo path to check; defaults to git rev-parse --show-toplevel from cwd",
    )
    p.add_argument(
        "--required-file",
        dest="required_file",
        default=None,
        help="optional path (relative to --repo) that must exist for the check to pass",
    )
    p.add_argument(
        "--fail",
        action="store_true",
        help="force a check failure (exit 1) — for testing the contract",
    )
    p.add_argument(
        "--bad-config",
        dest="bad_config",
        action="store_true",
        help="simulate a schema/config error (exit 3) — for testing the contract",
    )
    p.add_argument(
        "--crash",
        action="store_true",
        help="simulate an internal exception (exit 70) — for testing the contract",
    )
    p.add_argument(
        "--json",
        action="store_true",
        help="machine-readable output instead of plain text",
    )
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    if args.crash:
        raise RuntimeError("simulated internal error from --crash")

    if args.bad_config:
        # Demonstrates §1.4 exit 3: input/schema malformed.
        result = {
            "status": "schema_error",
            "message": args.message,
            "reason": "--bad-config flag simulates malformed input",
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"[aqg-example] schema_error: {result['reason']}")
        return EXIT_SCHEMA

    repo = _resolve_repo(args.repo)
    if args.required_file:
        target = (repo / args.required_file).resolve()
        if not target.exists():
            result = {
                "status": "fail",
                "message": args.message,
                "reason": f"required file missing: {target}",
                "repo": str(repo),
            }
            if args.json:
                print(json.dumps(result, ensure_ascii=False))
            else:
                print(f"[aqg-example] fail: {result['reason']}")
            return EXIT_CHECK_FAIL

    if args.fail:
        result = {
            "status": "fail",
            "message": args.message,
            "reason": "--fail flag set",
            "repo": str(repo),
        }
        if args.json:
            print(json.dumps(result, ensure_ascii=False))
        else:
            print(f"[aqg-example] fail: {result['reason']}")
        return EXIT_CHECK_FAIL

    result = {"status": "pass", "message": args.message, "repo": str(repo)}
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(f"[aqg-example] pass: {result['message']} (repo={result['repo']})")
    return EXIT_SUCCESS


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return EXIT_USAGE if e.code != 0 else EXIT_SUCCESS
    try:
        return run(args)
    except ValueError as exc:
        # Argument or environment error caught at the boundary — surface as
        # exit 2 (usage) so the agent client can branch on it.
        print(f"[aqg-example] usage error: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # aqg: top-level boundary
        print(f"[aqg-example] internal error: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    sys.exit(main())
