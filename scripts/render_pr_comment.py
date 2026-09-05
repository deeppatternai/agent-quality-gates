#!/usr/bin/env python3
"""Render a redacted Agent Quality Gates PR comment from adapter JSON."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from _secret_patterns import secret_counts


EXIT_USAGE = 2
EXIT_INTERNAL = 70

MARKER_START = "<!-- aqg:quality-gates-comment:start -->"
MARKER_END = "<!-- aqg:quality-gates-comment:end -->"


class UsageError(ValueError):
    """Raised when input JSON or output paths are invalid."""


def load_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"input JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise UsageError(f"cannot read input JSON {path}: {exc}") from exc
    if type(payload) is not dict:
        raise UsageError("input JSON must be an object")
    return payload


def as_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def gate_rows(gates: dict[str, Any]) -> list[str]:
    rows = ["| Gate | Mode | Status | Findings |", "|---|---:|---:|---:|"]
    for name in sorted(gates):
        result = gates[name] if type(gates[name]) is dict else {}
        mode = as_text(result.get("mode"), "unknown")
        status = as_text(result.get("status"), "unknown")
        findings = result.get("findings")
        finding_count = len(findings) if type(findings) is list else 0
        rows.append(f"| `{name}` | `{mode}` | `{status}` | {finding_count} |")
    return rows


def findings_lines(gates: dict[str, Any], max_findings: int) -> list[str]:
    lines: list[str] = []
    emitted = 0
    for name in sorted(gates):
        result = gates[name] if type(gates[name]) is dict else {}
        findings = result.get("findings")
        if type(findings) is not list or not findings:
            continue
        for finding in findings:
            if emitted >= max_findings:
                lines.append(f"- Additional findings omitted from comment after limit {max_findings}. See redacted JSON artifact.")
                return lines
            if type(finding) is not dict:
                continue
            message = as_text(finding.get("message"), "finding")
            suggestion = as_text(finding.get("suggestion"), "see gate output")
            config_key = as_text(finding.get("config_key"), "unknown")
            lines.append(f"- `{name}`: {message}. Fix: {suggestion} (`{config_key}`)")
            emitted += 1
    if not lines:
        lines.append("- None.")
    return lines


def render_comment(payload: dict[str, Any], *, max_findings: int) -> str:
    ok = bool(payload.get("ok"))
    project = payload.get("project") if type(payload.get("project")) is dict else {}
    pr_body = payload.get("pr_body")
    if type(pr_body) is not dict:
        raise RuntimeError("input JSON missing pr_body metadata")
    redaction = pr_body.get("redaction")
    if type(redaction) is not dict:
        raise RuntimeError("input JSON missing pr_body.redaction metadata")
    if type(pr_body.get("sha256")) is not str or not pr_body.get("sha256"):
        raise RuntimeError("input JSON missing pr_body.sha256 metadata")
    if type(pr_body.get("bytes")) is not int:
        raise RuntimeError("input JSON missing pr_body.bytes metadata")
    if type(redaction.get("total_secret_like_matches")) is not int:
        raise RuntimeError("input JSON missing pr_body.redaction.total_secret_like_matches metadata")
    gates = payload.get("gates") if type(payload.get("gates")) is dict else {}
    blocking_failures = payload.get("blocking_failures") if type(payload.get("blocking_failures")) is list else []
    secret_like_total = redaction.get("total_secret_like_matches", 0)
    secret_like_counts = redaction.get("secret_like_match_counts") if type(redaction.get("secret_like_match_counts")) is dict else {}

    lines = [
        MARKER_START,
        "### Agent Quality Gates",
        "",
        f"**Result:** {'PASS (no blocking failures)' if ok else 'FAIL (blocking failures present)'}",
        f"**Project:** `{as_text(project.get('name'), 'unknown')}`",
        f"**Generated:** `{as_text(payload.get('generated_at'), 'unknown')}`",
        "",
        *gate_rows(gates),
        "",
    ]
    if blocking_failures:
        failures = ", ".join(f"`{as_text(item)}`" for item in blocking_failures)
        lines.extend(["**Blocking failures:** " + failures, ""])
    lines.extend(
        [
            "**Findings**",
            "",
            *findings_lines(gates, max_findings),
            "",
            "**Redacted Evidence Metadata**",
            "",
            f"- PR body sha256: `{as_text(pr_body.get('sha256'), 'unknown')}`",
            f"- PR body bytes: `{as_text(pr_body.get('bytes'), '0')}`",
            f"- Secret-like matches in PR body: `{as_text(secret_like_total, '0')}`",
        ]
    )
    if secret_like_counts:
        count_parts = ", ".join(f"{key}={value}" for key, value in sorted(secret_like_counts.items()))
        lines.append(f"- Secret-like match counts by type: `{count_parts}`")
    lines.extend(
        [
            "",
            "Raw PR body text is intentionally excluded from this comment and from the JSON artifact.",
            MARKER_END,
            "",
        ]
    )
    comment = "\n".join(lines)
    leaked = secret_counts(comment)
    if leaked:
        raise RuntimeError(f"rendered comment contains secret-like value(s): {', '.join(sorted(leaked))}")
    return comment


def write_output(path: Path | None, comment: str) -> None:
    if path is None:
        print(comment, end="")
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(comment, encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot write output markdown {path}: {exc}") from exc
    print(f"OK: wrote redacted PR comment to {path}")


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a bounded, redacted AQG PR comment from adapter JSON.")
    parser.add_argument("--input-json", required=True, help="redacted JSON from scripts/run_quality_gates.py")
    parser.add_argument("--output", help="markdown output path; omitted means stdout")
    parser.add_argument("--max-findings", type=int, default=20, help="maximum finding bullets in the comment")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        if args.max_findings < 1:
            raise UsageError("--max-findings must be at least 1")
        payload = load_payload(Path(args.input_json).expanduser())
        comment = render_comment(payload, max_findings=args.max_findings)
        write_output(Path(args.output).expanduser() if args.output else None, comment)
        return 0
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - classify unexpected renderer failures
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
