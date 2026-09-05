"""collect_repo_reality — opt-in VCS/GitHub "repo reality" counts (issue #245).

Reads REAL repo activity since the ledger's last-activity timestamp: M commits
(`git rev-list --count`, local) + K merged PRs (`gh pr list --search ... merged`,
network when available), extracting ONLY integer counts. Aligns with the
aqg-startup-preflight network boundary (ADR §4.2 / §4.3): opt-in, read-only,
sanitize_external, bounded timeout, fail-closed graceful degrade — every failure
becomes a note, never a crash, never a non-zero exit. VCS free-text is parsed for
numbers only and NEVER surfaced; gh failures map to a CLOSED reason-code, never raw
stderr (ADR §4.2(c) / N5).

run()/sanitize_external() mirror skills/aqg-startup-preflight/scripts/aqg_preflight.py
(pinned, reimplemented locally to avoid a cross-skill hard dependency — ADR §4.3).

Round-1 implementation-audit hardening (audit 81790517, gpt-5.5 + gemini):
- run() captures stdout/stderr SEPARATELY (a tool's stderr warnings must not corrupt
  stdout JSON/int parsing — gemini-f1) and uses stdin=DEVNULL (an interactive auth
  prompt fails fast instead of hanging to the timeout — gemini-f4).
- a missing executable is distinguished from a bad cwd, so a wrong --repo path is
  not misreported as the tool being uninstalled (gpt-f2).
- merged-PR query filters SERVER-SIDE by merge date (`--search merged:>=<date>`) so
  a >1000-PR repo is not silently undercounted by gh's created-desc --limit ordering
  (gemini-f2); a precise client-side UTC mergedAt filter still applies on top.
- shallow detection uses `git rev-parse --is-shallow-repository` (robust to worktrees
  / submodules where .git is a FILE — gemini-f3).
"""
from __future__ import annotations

import datetime as dt
import json
import re
import subprocess
from pathlib import Path

from ledger.repo_reality import RepoReality

# git is local (≤30s); gh touches the network (≤45s). Mirrors preflight values.
_GIT_TIMEOUT = 30
_GH_TIMEOUT = 45
# --limit defeats gh's default 30-row cap; combined with a server-side merge-date
# search, a returned count == limit flags a (now-accurate) truncation (ADR §4.2(a)).
_PR_LIMIT = 1000

# rc sentinels returned by run() for non-process outcomes (kept out of real git/gh
# exit-code range so classification is unambiguous).
_RC_TIMEOUT = 998
_RC_NOT_FOUND = 997   # executable missing (distinct from a bad cwd / other OSError)
_RC_ERROR = 999       # other OSError (incl. a missing/!dir cwd)

_ANSI_CONTROL_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|[\x00-\x08\x0b-\x1f\x7f]")


def sanitize_external(text: str) -> str:
    """Strip ANSI escapes + C0/C1 control chars from untrusted command output
    (preflight form). Applied ONLY to text we parse for integers / classify — the
    raw text itself is never rendered."""
    return _ANSI_CONTROL_RE.sub("", text)


def run(cmd: "list[str]", cwd: "Path | None" = None, timeout: int = 30) -> "tuple[int, str, str]":
    """Run a command → (returncode, stdout, stderr). Never raises.

    stdout and stderr are captured SEPARATELY (gemini-f1: a tool's non-fatal stderr
    warning — gh "a new release is available", git advice hints — must not corrupt
    the stdout JSON/integer payload). stdin=DEVNULL (gemini-f4) makes an interactive
    auth/credential prompt fail fast instead of blocking to the timeout. A timeout →
    (_RC_TIMEOUT, ...); a missing executable → (_RC_NOT_FOUND, ...) DISTINCT from a
    bad cwd → (_RC_ERROR, "repo-path-missing") (gpt-f2)."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return _RC_TIMEOUT, "", "timeout"
    except FileNotFoundError:
        # FileNotFoundError is raised both for a missing executable AND a missing cwd.
        # A bad --repo path must NOT be misreported as the tool being uninstalled.
        if cwd is not None and not Path(cwd).is_dir():
            return _RC_ERROR, "", "repo-path-missing"
        return _RC_NOT_FOUND, "", "executable-not-found"
    except Exception as exc:  # aqg: top-level boundary — collection must not crash the report
        return _RC_ERROR, "", type(exc).__name__


def _parse_utc(value) -> "dt.datetime | None":
    """Parse an ISO-8601 timestamp → tz-aware UTC datetime, or None on anything
    unparseable. Python 3.9's datetime.fromisoformat rejects a trailing 'Z', so
    normalize it to +00:00 first; a naive result is assumed UTC."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def _utc_date(value) -> "str | None":
    """The UTC calendar date (YYYY-MM-DD) of an ISO timestamp, for gh's day-granular
    `merged:>=` search qualifier. None if unparseable. Day-granular server-side
    filtering is a coarse prefilter only — the precise client-side UTC filter is
    authoritative, so a timezone-edge over-fetch is harmless (it is filtered out)."""
    parsed = _parse_utc(value)
    return parsed.date().isoformat() if parsed else None


def _is_shallow(repo: Path, timeout: int) -> bool:
    """True when the repo is a shallow clone — `git rev-list --count` then exits 0 but
    silently truncates, so the banner must flag it. Uses `git rev-parse
    --is-shallow-repository` (robust to worktrees / submodules where .git is a FILE,
    not a dir — gemini-f3)."""
    rc, out, _ = run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"], timeout=timeout)
    return rc == 0 and sanitize_external(out).strip().lower() == "true"


def _git_reason(rc: int, stderr_low: str) -> str:
    """Map a git failure to a CLOSED reason-code (never raw text)."""
    if rc == _RC_TIMEOUT or "timeout" in stderr_low:
        return "timeout"
    if rc == _RC_NOT_FOUND:
        return "git-not-installed"
    return "git-error"


def _gh_reason(rc: int, stderr_low: str) -> str:
    """Map a gh failure to a CLOSED reason-code. We may INSPECT the (sanitized,
    lowercased) STDERR to classify, but ONLY the code is ever surfaced — raw stderr
    never reaches a note/banner (ADR §4.2(c); round1 gpt f1 + gemini f2)."""
    if rc == _RC_TIMEOUT or "timeout" in stderr_low:
        return "timeout"
    if rc == _RC_NOT_FOUND:
        return "gh-not-installed"
    if "rate limit" in stderr_low or "rate-limit" in stderr_low or "api rate" in stderr_low:
        return "rate-limited"
    if (
        "not logged" in stderr_low
        or "authentication" in stderr_low
        or "gh auth login" in stderr_low
        or "no github token" in stderr_low
    ):
        return "not-authenticated"
    return "gh-error"


def _collect_commits(repo: Path, since: "str | None", timeout: int) -> "tuple[int | None, list[str]]":
    cmd = ["git", "-C", str(repo), "rev-list", "--count"]
    if since:
        cmd.append(f"--since={since}")
    cmd.append("HEAD")
    rc, out, err = run(cmd, timeout=timeout)

    if rc == 0:
        try:
            commits = int(sanitize_external(out).strip())
        except ValueError:
            return None, ["commits: unavailable (git-error)"]
        notes = ["(shallow clone — count may be truncated)"] if _is_shallow(repo, timeout) else []
        return commits, notes

    low = sanitize_external(err).lower()
    # empty repo: rev-list HEAD exits 128 with "does not have any commits" /
    # "unknown revision" / "ambiguous argument" — a VALID 0-commit state, NOT a
    # degraded source (ADR §4.2(c); round1 gemini f1).
    if rc == 128 and (
        "does not have any commits" in low
        or "unknown revision" in low
        or "ambiguous argument" in low
    ):
        return 0, []

    return None, [f"commits: unavailable ({_git_reason(rc, low)})"]


def _collect_prs(
    repo: Path, since: "str | None", timeout: int
) -> "tuple[int | None, bool, list[str]]":
    # Explicit `--state merged` (round2 gpt-f1): empirically `gh` returns merged PRs
    # when a --search query is given even without --state, but --state merged is the
    # documented, stable form — don't rely on "--search overrides the default
    # --state open" implicit behaviour. Server-side filter by MERGE date when `since`
    # is known (gemini-f2: a plain --limit N orders by CREATION date desc, dropping
    # old-created/recently-merged PRs on a >N-merged repo; `--search "merged:>=<date>"`
    # filters server-side by merge date). GitHub search dates are day-granular, so the
    # precise client-side UTC mergedAt filter below stays authoritative (a timezone-edge
    # over-fetch is harmless — it gets filtered out). Empirically verified against this
    # repo's merged PRs (audit 127d119f).
    cmd = ["gh", "pr", "list", "--state", "merged", "--limit", str(_PR_LIMIT), "--json", "mergedAt"]
    if since:
        since_date = _utc_date(since)
        if since_date:
            cmd += ["--search", f"merged:>={since_date}"]
    # gh resolves the repo from the working directory → run it in `repo`.
    rc, out, err = run(cmd, cwd=repo, timeout=timeout)
    if rc != 0:
        return None, False, [f"PRs: unavailable ({_gh_reason(rc, sanitize_external(err).lower())})"]

    clean = sanitize_external(out)
    try:
        data = json.loads(clean) if clean.strip() else []
    except (ValueError, json.JSONDecodeError):
        return None, False, ["PRs: unavailable (gh-error)"]
    if not isinstance(data, list):
        return None, False, ["PRs: unavailable (gh-error)"]

    truncated = len(data) >= _PR_LIMIT
    notes = [f"(PR count truncated at {_PR_LIMIT})"] if truncated else []

    if since is None:
        return len(data), truncated, notes

    since_dt = _parse_utc(since)
    if since_dt is None:
        # cannot filter without a parseable anchor → report all + a note (never crash)
        return len(data), truncated, notes + ["(PR since-filter skipped — unparseable ledger timestamp)"]

    # client-side precise-UTC filter mergedAt >= since (NOT date-only — round2 gpt f2).
    count = 0
    for item in data:
        if not isinstance(item, dict):
            continue
        merged = _parse_utc(item.get("mergedAt"))
        if merged is not None and merged >= since_dt:
            count += 1
    return count, truncated, notes


def collect_repo_reality(
    repo: Path,
    since: "str | None",
    *,
    git_timeout: int = _GIT_TIMEOUT,
    gh_timeout: int = _GH_TIMEOUT,
) -> RepoReality:
    """Collect repo-reality counts since `since` (UTC ISO, or None for all-time).
    Always returns a RepoReality (never raises): a failed source yields a None
    count + a closed-reason note, distinct from a real 0."""
    notes: "list[str]" = []
    commits, commit_notes = _collect_commits(repo, since, git_timeout)
    prs, truncated, pr_notes = _collect_prs(repo, since, gh_timeout)

    if since is None:
        notes.append("no prior ledger activity; showing all-time")
    notes.extend(commit_notes)
    notes.extend(pr_notes)

    return RepoReality(
        commits=commits,
        prs=prs,
        notes=tuple(notes),
        since=since,
        truncated=truncated,
    )
