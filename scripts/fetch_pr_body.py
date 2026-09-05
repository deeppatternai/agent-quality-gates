#!/usr/bin/env python3
"""Fetch a GitHub PR body into a local temporary markdown file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from _secret_patterns import secret_counts


EXIT_USAGE = 2
EXIT_INTERNAL = 70

PR_EVENT_NAMES = {"pull_request", "pull_request_target"}
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class UsageError(ValueError):
    """Raised when CLI inputs or GitHub event metadata are invalid."""


def read_event(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    event_path = Path(path).expanduser()
    if not event_path.exists():
        raise UsageError(f"event path does not exist: {event_path}")
    try:
        payload = json.loads(event_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise UsageError(f"event JSON is invalid at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    except OSError as exc:
        raise UsageError(f"cannot read event path {event_path}: {exc}") from exc
    if type(payload) is not dict:
        raise UsageError("event JSON must be an object")
    return payload


def as_pr_number(value: Any) -> int | None:
    if type(value) is int and value > 0:
        return value
    if type(value) is str and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def pr_number_from_event(event: dict[str, Any]) -> int | None:
    pull_request = event.get("pull_request")
    if type(pull_request) is dict:
        number = as_pr_number(pull_request.get("number"))
        if number:
            return number
    return as_pr_number(event.get("number"))


def repo_from_event(event: dict[str, Any]) -> str | None:
    repo = event.get("repository")
    if type(repo) is dict and type(repo.get("full_name")) is str:
        return repo["full_name"]
    return None


def append_github_output(path: str | None, values: dict[str, str]) -> None:
    if not path:
        return
    output = Path(path).expanduser()
    try:
        with output.open("a", encoding="utf-8") as handle:
            for key, value in values.items():
                sanitized = value.replace("\r", " ").replace("\n", " ")
                handle.write(f"{key}={sanitized}\n")
    except OSError as exc:
        raise UsageError(f"cannot write GitHub output file {output}: {exc}") from exc


def append_step_summary(path: str | None, lines: list[str]) -> None:
    if not path:
        return
    summary = Path(path).expanduser()
    try:
        with summary.open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines).rstrip() + "\n")
    except OSError as exc:
        raise UsageError(f"cannot write GitHub step summary {summary}: {exc}") from exc


def skip(reason: str, *, github_output: str | None, summary: str | None) -> int:
    append_github_output(
        github_output,
        {
            "skipped": "true",
            "skip_reason": reason,
            "pr_number": "",
            "pr_body_file": "",
            "pr_body_sha256": "",
            "pr_body_bytes": "0",
        },
    )
    append_step_summary(summary, ["### Agent Quality Gates", "", f"Skipped: {reason}"])
    print(f"SKIP: {reason}")
    return 0


def fetch_pr_body(*, gh_bin: str, repo: str, pr_number: int, timeout: int) -> str:
    try:
        proc = subprocess.run(
            [gh_bin, "pr", "view", str(pr_number), "--repo", repo, "--json", "body"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UsageError(f"gh executable not found: {gh_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh pr view timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no gh output"
        raise RuntimeError(f"gh pr view failed for {repo}#{pr_number}: {detail}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr view returned invalid JSON: {exc}") from exc
    if type(payload) is not dict:
        raise RuntimeError("gh pr view JSON must be an object")
    body = payload.get("body")
    if body is None:
        return ""
    if type(body) is not str:
        raise RuntimeError("gh pr view body field must be a string or null")
    return body


def write_body(path: Path, body: str) -> tuple[str, int, dict[str, int]]:
    """Write the PR body to disk and pre-scan for secret-like content.

    Returns (sha256, byte_count, secret_counts). secret_counts is used to:
    - warn the caller via stderr
    - highlight in the GitHub Actions step summary
    - let a downstream caller decide whether to abort (the current fetch
      stage does not abort, it only warns)

    Note: redaction is not done here -- the hash must stay stable based on the
    raw bytes, and downstream run_quality_gates.py likewise stores only counts,
    not the raw text. Redaction is left for the user to handle.
    """
    raw = body.encode("utf-8")
    counts = secret_counts(body)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    except OSError as exc:
        raise UsageError(f"cannot write PR body file {path}: {exc}") from exc
    return hashlib.sha256(raw).hexdigest(), len(raw), counts


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fetch a GitHub pull request body into a local temp markdown file. "
            "Non-PR GitHub events exit 0 with skipped=true."
        )
    )
    parser.add_argument("--output", required=True, help="local markdown file path for the fetched PR body")
    parser.add_argument("--repo", help="target repository slug, e.g. owner/repo; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--pr-number", type=int, help="PR number override; bypasses event PR detection")
    parser.add_argument("--event-path", default=os.getenv("GITHUB_EVENT_PATH"), help="GitHub event JSON path")
    parser.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME"), help="GitHub event name")
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT"), help="GitHub Actions output file")
    parser.add_argument("--summary", default=os.getenv("GITHUB_STEP_SUMMARY"), help="GitHub job summary file")
    parser.add_argument("--gh", default="gh", help="gh executable path")
    parser.add_argument("--timeout", type=int, default=60, help="gh command timeout in seconds")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        event = read_event(args.event_path)
        if args.pr_number is None and args.event_name and args.event_name not in PR_EVENT_NAMES:
            return skip(
                f"non-PR event {args.event_name}",
                github_output=args.github_output,
                summary=args.summary,
            )

        pr_number = args.pr_number or pr_number_from_event(event)
        if pr_number is None:
            return skip("missing pull request number", github_output=args.github_output, summary=args.summary)

        repo = args.repo or os.getenv("GITHUB_REPOSITORY") or repo_from_event(event)
        if not repo:
            raise UsageError("target repo is required via --repo, GITHUB_REPOSITORY, or event.repository.full_name")
        if not REPO_RE.match(repo):
            raise UsageError(f"target repo must look like owner/repo: {repo}")

        output = Path(args.output).expanduser()
        try:
            body = fetch_pr_body(gh_bin=args.gh, repo=repo, pr_number=pr_number, timeout=args.timeout)
        except RuntimeError as exc:
            # The PR body feeds a WARN-ONLY quality gate. A transient gh failure
            # (GraphQL 401 / network / timeout / unexpected gh output) must DEGRADE to a
            # VISIBLE skip — not crash the job into a hard-red check (Owner 2026-06-10: a
            # flapping GitHub GraphQL 401 turned the warn-only gate red). Config errors
            # (gh missing, bad repo) stay UsageError below; only NON-RuntimeError bugs
            # still hit the catch-all INTERNAL_ERROR (a RuntimeError-shaped bug inside
            # fetch_pr_body would visibly skip with its message in skip_reason).
            #
            # Collapse whitespace + cap the gh detail before it reaches skip_reason ->
            # GITHUB_OUTPUT / step summary (audit a786236c, convergent): defence-in-depth
            # vs Actions-output injection. append_github_output() already strips newlines,
            # but append_step_summary() does not, so neutralise newlines at the source.
            detail = " ".join(str(exc).split())[:200]
            return skip(
                f"could not fetch PR body for {repo}#{pr_number}: {detail}",
                github_output=args.github_output,
                summary=args.summary,
            )
        digest, byte_count, secret_hits = write_body(output, body)
        secret_total = sum(secret_hits.values())
        append_github_output(
            args.github_output,
            {
                "skipped": "false",
                "skip_reason": "",
                "pr_number": str(pr_number),
                "pr_body_file": str(output),
                "pr_body_sha256": digest,
                "pr_body_bytes": str(byte_count),
                "secret_like_total": str(secret_total),
            },
        )
        summary_lines = [
            "### Agent Quality Gates",
            "",
            f"Fetched PR body for `{repo}#{pr_number}` into a local temporary file.",
            f"- bytes: {byte_count}",
            f"- sha256: `{digest}`",
            f"- secret-like matches: {secret_total}",
        ]
        if secret_hits:
            # Only output types and counts, not the raw text -- consistent with the redacted JSON output policy
            count_parts = ", ".join(f"{k}={v}" for k, v in sorted(secret_hits.items()))
            summary_lines.append(f"- secret-like match counts by type: `{count_parts}`")
            print(
                f"WARN: PR body contains {secret_total} secret-like match(es): "
                f"{count_parts}; downstream artifacts hash + count only, but the "
                f"local PR body file at {output} still contains raw text. "
                f"Treat as sensitive and rotate any exposed credentials.",
                file=sys.stderr,
            )
        append_step_summary(args.summary, summary_lines)
        print(f"OK: fetched PR body for {repo}#{pr_number}; bytes={byte_count}; sha256={digest}; secret_like={secret_total}")
        return 0
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - adapter failures should be classified
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
