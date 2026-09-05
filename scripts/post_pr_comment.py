#!/usr/bin/env python3
"""Post or update one bounded Agent Quality Gates PR comment."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from _secret_patterns import secret_counts


EXIT_USAGE = 2
EXIT_INTERNAL = 70

MARKER_START = "<!-- aqg:quality-gates-comment:start -->"
MARKER_END = "<!-- aqg:quality-gates-comment:end -->"
REDACTED_SUMMARY_PHRASE = "Raw PR body text is intentionally excluded"
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


def skip(reason: str) -> int:
    print(f"SKIP: {reason}")
    return 0


def read_comment(path: Path, *, marker_start: str, marker_end: str) -> str:
    try:
        body = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise UsageError(f"cannot read body file {path}: {exc}") from exc
    if marker_start not in body or marker_end not in body:
        raise UsageError("body file is missing the bounded AQG sticky-comment markers")
    if REDACTED_SUMMARY_PHRASE not in body:
        raise UsageError("body file is missing the AQG redacted-summary statement")
    leaked = secret_counts(body)
    if leaked:
        raise UsageError(f"body file contains secret-like value(s): {', '.join(sorted(leaked))}")
    return body


def gh_api_json(gh_bin: str, endpoint: str, *, timeout: int) -> Any:
    try:
        proc = subprocess.run(
            [gh_bin, "api", endpoint],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UsageError(f"gh executable not found: {gh_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api timed out after {timeout}s") from exc
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no gh output"
        raise RuntimeError(f"gh api failed for {endpoint}: {detail}")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api returned invalid JSON for {endpoint}: {exc}") from exc


def gh_api_mutate(gh_bin: str, method: str, endpoint: str, payload: dict[str, str], *, timeout: int) -> None:
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        tmp_name = handle.name
    try:
        proc = subprocess.run(
            [gh_bin, "api", "--method", method, endpoint, "--input", tmp_name],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        raise UsageError(f"gh executable not found: {gh_bin}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api {method} timed out after {timeout}s") from exc
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip() or "no gh output"
        raise RuntimeError(f"gh api {method} failed for {endpoint}: {detail}")


def find_existing_comment(
    gh_bin: str,
    repo: str,
    pr_number: int,
    *,
    marker_start: str,
    marker_end: str,
    timeout: int,
    max_pages: int,
) -> int | None:
    page = 1
    newest_id: int | None = None
    while page <= max_pages:
        endpoint = f"/repos/{repo}/issues/{pr_number}/comments?per_page=100&page={page}&sort=created&direction=asc"
        comments = gh_api_json(gh_bin, endpoint, timeout=timeout)
        if type(comments) is not list:
            raise RuntimeError("issue comments response must be an array")
        for comment in comments:
            if type(comment) is not dict:
                continue
            body = comment.get("body")
            comment_id = comment.get("id")
            if type(body) is str and marker_start in body and marker_end in body and type(comment_id) is int:
                newest_id = comment_id
        if len(comments) < 100:
            return newest_id
        page += 1
    print(f"WARN: reached AQG comment search page cap ({max_pages}); using newest marker seen", file=sys.stderr)
    return newest_id


def post_or_update(
    *,
    gh_bin: str,
    repo: str,
    pr_number: int,
    body: str,
    marker_start: str,
    marker_end: str,
    timeout: int,
    max_pages: int,
    dry_run: bool,
) -> str:
    comment_id = find_existing_comment(
        gh_bin,
        repo,
        pr_number,
        marker_start=marker_start,
        marker_end=marker_end,
        timeout=timeout,
        max_pages=max_pages,
    )
    if comment_id is not None:
        if dry_run:
            return f"DRY_RUN: would update AQG comment {comment_id} on {repo}#{pr_number}"
        gh_api_mutate(
            gh_bin,
            "PATCH",
            f"/repos/{repo}/issues/comments/{comment_id}",
            {"body": body},
            timeout=timeout,
        )
        return f"OK: updated AQG comment {comment_id} on {repo}#{pr_number}"
    if dry_run:
        return f"DRY_RUN: would create AQG comment on {repo}#{pr_number}"
    gh_api_mutate(
        gh_bin,
        "POST",
        f"/repos/{repo}/issues/{pr_number}/comments",
        {"body": body},
        timeout=timeout,
    )
    return f"OK: created AQG comment on {repo}#{pr_number}"


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Post or update a single sticky AQG PR comment using the GitHub CLI."
    )
    parser.add_argument("--body-file", required=True, help="bounded redacted comment markdown from render_pr_comment.py")
    parser.add_argument("--repo", help="target repository slug, e.g. owner/repo; defaults to GITHUB_REPOSITORY")
    parser.add_argument("--pr-number", type=int, help="PR number override; bypasses event PR detection")
    parser.add_argument("--event-path", default=os.getenv("GITHUB_EVENT_PATH"), help="GitHub event JSON path")
    parser.add_argument("--event-name", default=os.getenv("GITHUB_EVENT_NAME"), help="GitHub event name")
    parser.add_argument("--marker-start", default=MARKER_START, help="sticky comment start marker")
    parser.add_argument("--marker-end", default=MARKER_END, help="sticky comment end marker")
    parser.add_argument("--gh", default="gh", help="gh executable path")
    parser.add_argument("--timeout", type=int, default=60, help="gh command timeout in seconds")
    parser.add_argument("--max-pages", type=int, default=50, help="maximum issue-comment pages to scan")
    parser.add_argument("--dry-run", action="store_true", help="validate inputs and report intended action without posting")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        event = read_event(args.event_path)
        if args.pr_number is None and args.event_name and args.event_name not in PR_EVENT_NAMES:
            return skip(f"non-PR event {args.event_name}")
        if not args.marker_start or not args.marker_end:
            raise UsageError("--marker-start and --marker-end must be non-empty")
        if args.marker_start == args.marker_end:
            raise UsageError("--marker-start and --marker-end must be different")
        if args.max_pages < 1:
            raise UsageError("--max-pages must be at least 1")

        pr_number = args.pr_number or pr_number_from_event(event)
        if pr_number is None:
            return skip("missing pull request number")

        repo = args.repo or os.getenv("GITHUB_REPOSITORY") or repo_from_event(event)
        if not repo:
            raise UsageError("target repo is required via --repo, GITHUB_REPOSITORY, or event.repository.full_name")
        if not REPO_RE.match(repo):
            raise UsageError(f"target repo must look like owner/repo: {repo}")

        body = read_comment(Path(args.body_file).expanduser(), marker_start=args.marker_start, marker_end=args.marker_end)
        message = post_or_update(
            gh_bin=args.gh,
            repo=repo,
            pr_number=pr_number,
            body=body,
            marker_start=args.marker_start,
            marker_end=args.marker_end,
            timeout=args.timeout,
            max_pages=args.max_pages,
            dry_run=args.dry_run,
        )
        print(message)
        return 0
    except UsageError as exc:
        print(f"USAGE_ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:  # noqa: BLE001 - classify unexpected posting failures
        print(f"INTERNAL_ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_INTERNAL


if __name__ == "__main__":
    raise SystemExit(main())
