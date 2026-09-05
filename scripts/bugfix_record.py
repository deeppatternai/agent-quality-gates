#!/usr/bin/env python3
"""Bugfix record generator — fill template + write to docs/bugfixes/ (Wave 1 #6).

Driven by an LLM (Claude/Codex) after a bug fix; input fields come from CLI args
or stdin JSON; the generator passes through the _bugfix_redaction gate + writes to
the hardcoded docs/bugfixes/ directory.

Implemented per framework v2.1 §7 #6 + triple-audit (audit_id c489fdf5) 11 accepted findings.

CLI:
    # JSON mode (recommended for LLM)
    python3 scripts/bugfix_record.py --json < input.json

    # Args mode (human / scripted)
    python3 scripts/bugfix_record.py \\
        --slug short-slug --title "Short title" \\
        --affected-area "scripts/foo.py" --severity medium \\
        --backward-compatible yes --actor claude \\
        --symptom "..." --root-cause "..." --fix "..." \\
        --verification "..." --regression-coverage "..." \\
        --boundaries "..." \\
        [--files-touched scripts/a.py,scripts/b.py] \\
        [--taxonomy algorithm,performance] [--regression] \\
        [--audit-id 8hex] [--pr-url https://github.com/...] \\
        [--date YYYY-MM-DD] [--force]

Exit codes: 0 OK / 1 redaction or write fail / 2 usage error / 3 file exists (no --force)

No third-party dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


EXIT_OK = 0
EXIT_REDACTION_FAIL = 1
EXIT_USAGE = 2
EXIT_FILE_EXISTS = 3

# Hardcoded output dir (gpt-5.5 #3 + gemini #1 + o3 #2 convergent: do not expose a flag)
BUGFIX_DIR_REL = "docs/bugfixes"

# Note (post-impl dual-audit gpt-5.5 #5 accepted): we do not read templates/bugfix_record_template.md
# in the generator; instead _render_markdown embeds a schema with the same sections as the template
# (Symptom / Root Cause / Fix / Verification / Regression Coverage / Boundaries). The template file
# is kept for human reference + grep recall; the generator is the source of truth for layout.
# If we ever want this driven by an external template (Jinja-like), that is a separate task (#6.x).


# ===== Repo root resolution =====


def _find_repo_root(start: Path | None = None) -> Path:
    """Walk up from start to find AQG repo root (has VERSION + scripts/)."""
    if start is None:
        start = Path(__file__).resolve().parent
    p = start.resolve()
    for _ in range(10):
        if (p / "VERSION").is_file() and (p / "scripts").is_dir() and (p / "templates").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise RuntimeError(
        f"cannot find AQG repo root from {start}; expected VERSION + scripts/ + templates/"
    )


# ===== Record building =====


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _build_record_from_args(args: argparse.Namespace) -> dict[str, Any]:
    """Convert argparse Namespace → record dict. NOT validated yet."""
    return {
        "schema_version": 1,
        "slug": args.slug,
        "date": args.date or _today_utc(),
        "actor": args.actor,
        "title": args.title,
        "affected_area": args.affected_area,
        "severity": args.severity,
        "backward_compatible": args.backward_compatible,
        "symptom": args.symptom,
        "root_cause": args.root_cause,
        "fix": args.fix,
        "verification": args.verification,
        "regression_coverage": args.regression_coverage,
        "boundaries": args.boundaries,
        "files_touched": (
            [s.strip() for s in args.files_touched.split(",") if s.strip()]
            if args.files_touched
            else []
        ),
        "taxonomy": (
            [s.strip() for s in args.taxonomy.split(",") if s.strip()]
            if args.taxonomy
            else []
        ),
        "regression": bool(args.regression),
        "audit_id": args.audit_id or "",
        "pr_url": args.pr_url or "",
        "marker": "auto-generated-by-bugfix_record",
    }


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    """Apply defaults; ensure marker / schema_version set."""
    record = dict(record)
    record.setdefault("schema_version", 1)
    record.setdefault("marker", "auto-generated-by-bugfix_record")
    record.setdefault("date", _today_utc())
    record.setdefault("taxonomy", [])
    record.setdefault("regression", False)
    record.setdefault("audit_id", "")
    record.setdefault("pr_url", "")
    record.setdefault("files_touched", [])
    return record


# ===== Markdown render =====


def _fence_multiline(text: str, *, lang: str = "text") -> str:
    """Wrap text in a fenced code block to neutralize markdown injection.

    Post-impl dual-audit gpt-5.5 #3 + gemini #2 (convergent major): rendering
    multiline fields (root_cause/fix/verification/boundaries) directly would let an
    attacker/LLM forge a section via `\\n## fake heading`. Two layers of guarding:
    1. _bugfix_redaction already rejects a leading-# heading
    2. the fence wrapping here — even if redaction ever misses, markdown inside the
       fence is not rendered
    """
    if "```" in text:
        # Extreme case: text itself contains ``` — use a longer fence
        return f"~~~~{lang}\n{text}\n~~~~"
    return f"```{lang}\n{text}\n```"


def _render_markdown(record: dict[str, Any]) -> str:
    """Render record dict → bugfix Markdown.

    The top metadata HTML comment is for the machine parser; the main body matches
    the structure of the existing templates/bugfix_record_template.md, for ease of
    grep recall. Multiline fields are fenced-isolated to prevent markdown injection
    (post-impl dual-audit gpt-5.5 #3 + gemini #2 convergent).
    """
    taxonomy_str = ", ".join(record.get("taxonomy") or []) or "(none)"
    files_block = "\n".join(f"- {p}" for p in record.get("files_touched") or []) or "(none)"

    metadata = f"""<!--
AQG_BUGFIX_RECORD
schema_version: {record["schema_version"]}
slug: {record["slug"]}
actor: {record["actor"]}
taxonomy: [{taxonomy_str}]
regression: {str(record.get("regression", False)).lower()}
audit_id: {record.get("audit_id", "")}
pr_url: {record.get("pr_url", "")}
marker: {record["marker"]}
-->"""

    body = f"""# Bug Fix Record: {record["title"]}

- date: {record["date"]}
- affected area: {record["affected_area"]}
- severity: {record["severity"]}
- backward compatible: {record["backward_compatible"]}

## Symptom

{record["symptom"]}

## Root Cause

{_fence_multiline(record["root_cause"])}

## Fix

{_fence_multiline(record["fix"])}

## Verification

{_fence_multiline(record["verification"])}

## Regression Coverage

{record["regression_coverage"]}

## Boundaries

{_fence_multiline(record["boundaries"])}
"""
    if record.get("files_touched"):
        body += f"\n## Files touched\n\n{files_block}\n"
    return metadata + "\n\n" + body


# ===== Output path =====


def _resolve_output_path(record: dict[str, Any], *, repo_root: Path) -> Path:
    """Resolve docs/bugfixes/<date>-<slug>.md under repo_root.

    Asserts result stays under repo_root/docs/bugfixes (defense in depth
    against any sneak via slug — already SLUG_RE blocks ../).
    """
    target_dir = (repo_root / BUGFIX_DIR_REL).resolve()
    candidate = (target_dir / f"{record['date']}-{record['slug']}.md").resolve()
    if candidate.parent != target_dir:
        raise ValueError(f"resolved path {candidate} escapes {target_dir}")
    return candidate


# ===== Write =====


def _atomic_create_with_suffix(base_path: Path, text: str) -> Path | None:
    """Try base_path then base_path-a2..a99 with O_EXCL exclusive create.

    Post-impl dual-audit gpt-5.5 #4 accepted: race-safe — even under concurrency two
    processes will not pick the same path to overwrite. Returns the path actually
    written, or None if 100 candidates exhausted.
    """
    candidates: list[Path] = [base_path]
    for n in range(2, 100):
        candidates.append(base_path.with_name(f"{base_path.stem}-a{n}{base_path.suffix}"))
    for cand in candidates:
        try:
            # O_CREAT | O_EXCL via 'x' mode — race-safe
            with open(cand, "x", encoding="utf-8") as fh:
                fh.write(text)
            return cand
        except FileExistsError:
            continue
        except OSError:
            return None  # write/permission fail
    return None


def _write_record(record: dict[str, Any], *, repo_root: Path, force: bool = False) -> tuple[int, Path]:
    """Validate → render → write. Returns (exit_code, path)."""
    from _bugfix_redaction import assert_safe_bugfix_record, BugfixRedactionError

    record = _normalize_record(record)
    try:
        assert_safe_bugfix_record(record)
    except BugfixRedactionError as exc:
        print(f"[bugfix_record] redaction violations:\n{exc}", file=sys.stderr)
        return EXIT_REDACTION_FAIL, Path()

    path = _resolve_output_path(record, repo_root=repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)

    text = _render_markdown(record)

    # Post-impl dual-audit gpt-5.5 #4 (major): atomic exclusive creation prevents a race.
    # _write_atomic handles race-safety uniformly inside both the force and suffix branches.
    if force:
        # Allow overwrite — write_text is acceptable here per Owner intent.
        path.write_text(text, encoding="utf-8")
        write_path = path
    else:
        write_path = _atomic_create_with_suffix(path, text)
        if write_path is None:
            print(
                f"[bugfix_record] {path.name} and 99 -aN suffixes all taken; refusing.",
                file=sys.stderr,
            )
            return EXIT_FILE_EXISTS, path
        if write_path.name != path.name:
            print(
                f"[bugfix_record] {path.name} exists; used {write_path.name} instead "
                f"(use --force to overwrite original)",
                file=sys.stderr,
            )
    path = write_path
    # The macOS /var ↔ /private/var symlink makes relative_to fail intermittently in a tmp dir;
    # fallback to filename only for log clarity.
    try:
        log_path = path.relative_to(repo_root.resolve())
    except (ValueError, OSError):
        log_path = path.name
    print(f"[bugfix_record] wrote {log_path}", file=sys.stderr)
    return EXIT_OK, path


# ===== CLI =====


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bugfix_record",
        description="Generate AQG bugfix record markdown (LLM auto-fill helper).",
    )
    p.add_argument("--json", action="store_true", help="read record JSON from stdin (recommended for LLMs)")
    p.add_argument("--force", action="store_true", help="overwrite existing file at the same path")

    # Field args (mutually exclusive with --json)
    p.add_argument("--slug")
    p.add_argument("--title")
    p.add_argument("--actor", default="claude")
    p.add_argument("--date")
    p.add_argument("--affected-area")
    p.add_argument("--severity", choices=["low", "medium", "high"])
    p.add_argument("--backward-compatible", choices=["yes", "no", "migration needed"])
    p.add_argument("--symptom")
    p.add_argument("--root-cause")
    p.add_argument("--fix")
    p.add_argument("--verification")
    p.add_argument("--regression-coverage")
    p.add_argument("--boundaries")
    p.add_argument("--files-touched", help="comma-separated project-relative paths")
    p.add_argument("--taxonomy", help="comma-separated tags from configuration/interface/data/algorithm/performance/security/typo")
    p.add_argument("--regression", action="store_true", help="this fix addresses a regression")
    p.add_argument("--audit-id", help="8-char hex audit id (/audit return)")
    p.add_argument("--pr-url", help="https://github.com/... PR URL")
    return p


_FIELD_ARGS: tuple[str, ...] = (
    "slug", "title", "affected_area", "severity",
    "backward_compatible", "symptom", "root_cause", "fix",
    "verification", "regression_coverage", "boundaries",
    "files_touched", "taxonomy", "audit_id", "pr_url",
)
# Post-impl dual-audit gpt-5.5 #6 accepted: --json + --date/--actor/--regression
# were previously accepted silently; switched to checking sys.argv for supplied flag presence.
_MUTEX_FLAG_NAMES: tuple[str, ...] = (
    "--slug", "--title", "--affected-area", "--severity",
    "--backward-compatible", "--symptom", "--root-cause", "--fix",
    "--verification", "--regression-coverage", "--boundaries",
    "--files-touched", "--taxonomy", "--audit-id", "--pr-url",
    "--date", "--actor", "--regression",
)


def _check_mutex(args: argparse.Namespace, argv: list[str] | None = None) -> str | None:
    """Enforce: --json XOR field args (o3 #4 + gemini #3 accepted).

    Post-impl dual-audit gpt-5.5 #6 accepted: check sys.argv rather than attribute
    presence, so that cases like --json --date X get caught (date has a default).
    """
    raw = argv if argv is not None else sys.argv[1:]
    field_in_argv = any(flag in raw for flag in _MUTEX_FLAG_NAMES)
    if args.json and field_in_argv:
        return "--json mutually exclusive with field args (--slug/--title/--date/--actor/--regression/etc)"
    if not args.json and not field_in_argv:
        return "either --json (stdin) or field args required (see --help)"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    err = _check_mutex(args, argv=argv)
    if err:
        print(f"ERROR: {err}", file=sys.stderr)
        return EXIT_USAGE

    if args.json:
        try:
            record = json.loads(sys.stdin.read())
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"ERROR: stdin not valid JSON: {exc}", file=sys.stderr)
            return EXIT_USAGE
        if not isinstance(record, dict):
            print("ERROR: stdin JSON must be an object", file=sys.stderr)
            return EXIT_USAGE
    else:
        # Validate required field args present
        missing = [f for f in (
            "slug", "title", "affected_area", "severity", "backward_compatible",
            "symptom", "root_cause", "fix", "verification", "regression_coverage",
            "boundaries",
        ) if not getattr(args, f)]
        if missing:
            print(f"ERROR: missing required args: {', '.join(missing)}", file=sys.stderr)
            return EXIT_USAGE
        record = _build_record_from_args(args)

    try:
        repo_root = _find_repo_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    rc, _ = _write_record(record, repo_root=repo_root, force=args.force)
    return rc


# ===== Self-test =====


def self_test() -> int:
    """Quick sanity (full coverage in tests/test_bugfix_record.py)."""
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        repo = Path(tmpdir) / "repo"
        (repo / "scripts").mkdir(parents=True)
        (repo / "templates").mkdir(parents=True)
        (repo / "VERSION").write_text("0.0.0")

        record = {
            "schema_version": 1,
            "slug": "self-test-bug",
            "date": "2026-05-03",
            "actor": "claude",
            "title": "self test",
            "affected_area": "scripts",
            "severity": "low",
            "backward_compatible": "yes",
            "symptom": "self-test sanity",
            "root_cause": "self-test sanity",
            "fix": "self-test sanity",
            "verification": "self-test sanity",
            "regression_coverage": "self-test sanity",
            "boundaries": "self-test sanity",
        }
        rc, path = _write_record(record, repo_root=repo)
        assert rc == EXIT_OK
        assert path.exists()
        text = path.read_text()
        assert "AQG_BUGFIX_RECORD" in text
        assert "self-test sanity" in text

        # Re-write same → -a2 suffix
        rc, path2 = _write_record(record, repo_root=repo)
        assert rc == EXIT_OK
        assert path2.exists()
        assert path2.name != path.name
        assert path2.name.endswith("-a2.md")

    print("OK: bugfix_record self-test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    raise SystemExit(main())
