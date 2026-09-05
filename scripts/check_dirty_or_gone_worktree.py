#!/usr/bin/env python3
"""Gate git worktrees for dirty, detached, gone-upstream, and behind states."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 30) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip()
    except Exception as exc:  # noqa: BLE001 - gate diagnostics should be resilient
        return 999, f"{type(exc).__name__}: {exc}"


def git(repo: Path, *args: str, timeout: int = 30) -> tuple[int, str]:
    return run(["git", "-C", str(repo), *args], timeout=timeout)


def first_status_line(status: str) -> str:
    for line in status.splitlines():
        if line.startswith("## "):
            return line
    return ""


def dirty_lines(status: str) -> list[str]:
    return [line for line in status.splitlines() if line and not line.startswith("## ")]


def add_failure(report: dict[str, Any], reason: str) -> None:
    failures = report.setdefault("failures", [])
    if reason not in failures:
        failures.append(reason)


def check_repo(repo: Path, *, fetch: bool = False) -> dict[str, Any]:
    report: dict[str, Any] = {
        "path": str(repo),
        "exists": repo.exists(),
        "fetch_requested": fetch,
        "failures": [],
        "warnings": [],
    }

    if not repo.exists():
        add_failure(report, "missing repo path")
        report["ok"] = False
        return report

    rc, inside = git(repo, "rev-parse", "--is-inside-work-tree")
    if rc != 0 or inside.lower() != "true":
        add_failure(report, "not a git worktree")
        report["git_error"] = inside
        report["ok"] = False
        return report

    rc, root = git(repo, "rev-parse", "--show-toplevel")
    report["git_root"] = root if rc == 0 else None

    if fetch:
        rc, out = git(repo, "fetch", "--prune", "origin", timeout=60)
        report["fetch"] = {"ok": rc == 0, "output": out}
        if rc != 0:
            add_failure(report, "fetch failed")
    else:
        report["fetch"] = {"ok": None, "output": "not requested"}

    rc, head = git(repo, "rev-parse", "--short", "HEAD")
    report["head"] = head if rc == 0 else None

    rc, branch = git(repo, "symbolic-ref", "-q", "--short", "HEAD")
    detached = rc != 0 or not branch
    report["branch"] = None if detached else branch
    report["detached"] = detached
    if detached:
        add_failure(report, "detached HEAD")

    rc, status = git(repo, "status", "--porcelain=v1", "--branch")
    if rc != 0:
        report["status_error"] = status
        add_failure(report, "git status failed")
        report["ok"] = False
        return report

    status_line = first_status_line(status)
    changed = dirty_lines(status)
    upstream_gone = "[gone]" in status_line
    report["status_branch_line"] = status_line
    report["dirty"] = bool(changed)
    report["dirty_entries"] = changed
    report["upstream_gone"] = upstream_gone

    if changed:
        add_failure(report, "dirty worktree")
    if upstream_gone:
        add_failure(report, "upstream gone")

    rc, upstream = git(repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}")
    report["upstream"] = upstream if rc == 0 and upstream else None
    report["upstream_resolved"] = rc == 0 and bool(upstream)
    if rc != 0 or not upstream:
        report["upstream_error"] = upstream
        add_failure(report, "cannot resolve upstream")
    else:
        rc, counts = git(repo, "rev-list", "--left-right", "--count", "HEAD...@{u}")
        if rc != 0:
            report["ahead"] = None
            report["behind"] = None
            report["compare_error"] = counts
            add_failure(report, "cannot compare upstream")
        else:
            parts = counts.split()
            if len(parts) != 2:
                report["ahead"] = None
                report["behind"] = None
                report["compare_error"] = counts
                add_failure(report, "cannot compare upstream")
            else:
                ahead = int(parts[0])
                behind = int(parts[1])
                report["ahead"] = ahead
                report["behind"] = behind
                if behind > 0:
                    add_failure(report, "behind remote")

    report["ok"] = not report["failures"]
    return report


def payload_for(repos: list[Path], *, fetch: bool) -> dict[str, Any]:
    results = [check_repo(repo, fetch=fetch) for repo in repos]
    return {
        "ok": all(item["ok"] for item in results),
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "fetch_requested": fetch,
        "repos": results,
    }


def human_summary(payload: dict[str, Any]) -> str:
    lines = [f"check_dirty_or_gone_worktree: {'OK' if payload['ok'] else 'FAIL'}"]
    for repo in payload["repos"]:
        failures = repo.get("failures") or []
        status = "OK" if repo.get("ok") else "FAIL"
        lines.append(f"- {repo['path']}: {status}")
        lines.append(f"  branch: {repo.get('branch') or '<detached-or-none>'}")
        lines.append(f"  upstream: {repo.get('upstream') or '<unresolved>'}")
        lines.append(f"  ahead/behind: {repo.get('ahead', '?')}/{repo.get('behind', '?')}")
        lines.append(f"  dirty: {repo.get('dirty', '?')}")
        lines.append(f"  upstream_gone: {repo.get('upstream_gone', '?')}")
        lines.append(f"  failures: {', '.join(failures) if failures else '<none>'}")
    return "\n".join(lines)


def git_checked(args: list[str], cwd: Path | None = None) -> str:
    rc, out = run(args, cwd=cwd, timeout=60)
    if rc != 0:
        raise AssertionError(f"command failed ({rc}): {' '.join(args)}\n{out}")
    return out


def write(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def self_test() -> int:
    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)
        remote = tmp / "remote.git"
        work = tmp / "work"
        peer = tmp / "peer"

        git_checked(["git", "init", "--bare", "--initial-branch=main", str(remote)])
        git_checked(["git", "clone", str(remote), str(work)])
        git_checked(["git", "config", "user.email", "gate@example.invalid"], cwd=work)
        git_checked(["git", "config", "user.name", "Gate Test"], cwd=work)
        write(work / "README.md", "initial\n")
        git_checked(["git", "add", "README.md"], cwd=work)
        git_checked(["git", "commit", "-m", "initial"], cwd=work)
        git_checked(["git", "push", "-u", "origin", "main"], cwd=work)

        clean = check_repo(work, fetch=True)
        assert clean["ok"], clean

        write(work / "dirty.txt", "dirty\n")
        dirty = check_repo(work, fetch=False)
        assert not dirty["ok"] and "dirty worktree" in dirty["failures"], dirty
        git_checked(["git", "reset", "--hard", "HEAD"], cwd=work)
        if (work / "dirty.txt").exists():
            (work / "dirty.txt").unlink()

        git_checked(["git", "clone", str(remote), str(peer)])
        git_checked(["git", "config", "user.email", "gate@example.invalid"], cwd=peer)
        git_checked(["git", "config", "user.name", "Gate Test"], cwd=peer)
        write(peer / "peer.txt", "peer\n")
        git_checked(["git", "add", "peer.txt"], cwd=peer)
        git_checked(["git", "commit", "-m", "peer update"], cwd=peer)
        git_checked(["git", "push"], cwd=peer)
        behind = check_repo(work, fetch=True)
        assert not behind["ok"] and "behind remote" in behind["failures"], behind
        git_checked(["git", "pull", "--ff-only"], cwd=work)

        git_checked(["git", "checkout", "--detach", "HEAD"], cwd=work)
        detached = check_repo(work, fetch=False)
        assert not detached["ok"] and "detached HEAD" in detached["failures"], detached
        git_checked(["git", "checkout", "main"], cwd=work)

        git_checked(["git", "branch", "--unset-upstream"], cwd=work)
        no_upstream = check_repo(work, fetch=False)
        assert not no_upstream["ok"] and "cannot resolve upstream" in no_upstream["failures"], no_upstream
        git_checked(["git", "branch", "--set-upstream-to=origin/main", "main"], cwd=work)

        git_checked(["git", "checkout", "-b", "feature"], cwd=work)
        write(work / "feature.txt", "feature\n")
        git_checked(["git", "add", "feature.txt"], cwd=work)
        git_checked(["git", "commit", "-m", "feature"], cwd=work)
        git_checked(["git", "push", "-u", "origin", "feature"], cwd=work)
        git_checked(["git", "push", "origin", "--delete", "feature"], cwd=work)
        gone = check_repo(work, fetch=True)
        assert not gone["ok"] and "upstream gone" in gone["failures"], gone

    print("OK: check_dirty_or_gone_worktree self-test passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", action="append", help="repo path to check; may be provided multiple times")
    parser.add_argument("--fetch", action="store_true", help="run git fetch --prune origin before checking")
    parser.add_argument("--json-only", action="store_true", help="suppress human summary on stderr")
    parser.add_argument("--self-test", action="store_true", help="run offline self-test with temporary git repos")
    args = parser.parse_args(argv)

    if args.self_test:
        return self_test()

    if not args.repo:
        parser.error("at least one --repo path is required")

    payload = payload_for([Path(item).expanduser() for item in args.repo], fetch=args.fetch)
    if not args.json_only:
        print(human_summary(payload), file=sys.stderr)
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if payload["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
