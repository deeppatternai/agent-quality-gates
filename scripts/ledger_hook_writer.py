#!/usr/bin/env python3
"""AQG Ledger v2 — non-EAF hook capture writer (SessionStart anchor + Stop capture).

Implements the ledger v2 hook-writer design
(R1 audit 3514996c + R2 audit cbf33b6b, both folded).

A deterministic Claude Code hook that captures the work which does NOT go through
EAF: `SessionStart` records a capture anchor {baseline_sha, session_start_time};
`Stop` captures the git commits made in the session window and emits ONE
low-fidelity `progress` event per commit into the ledger inbox (the same inbox
the EAF exporter writes to). The single-consumer store drains it (already shipped).

Design invariants (sketch):
- NEVER fail Claude Code: any error / missing git / missing contract → silent
  exit 0. Best-effort capture, never a gate.
- No LLM, no network: pure git subprocess + local file write, bounded work.
- progress ONLY: never writes defect/handoff, never drives the defect state
  machine (DesignSpec §5.3.1 N7).
- Time-anchored range (R2 B/C/D): commits with committer-date >= session anchor,
  reachable from HEAD, `--no-merges` — regardless of author email or sha-ancestry.
- event_id = aqg-hook:<sha256(project_id)[:16]>:<committer_unix_ts>.<commit_sha>
  → committer_ts gives chronological within-batch order (R2 G2), sha gives
  uniqueness; both immutable per commit → cross-session idempotency (R1 f1).

Reuses the wip_save deterministic-hook discipline (session_id sanitize, _safe_run,
atomic write, never-fail) and the ledger contract write API
(ledger.paths.write_incoming_event + ledger.project_id.project_id_from_repo).
Pure stdlib.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


# ===== Make the neutral contract package importable (contracts/ledger/*) =====
# scripts/ -> repo root -> contracts/ . Guarded: if the package is absent at hook
# runtime, the lazy import in _emit_events fails and main() silent-exits 0.
_CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "contracts"
if _CONTRACTS_DIR.is_dir() and str(_CONTRACTS_DIR) not in sys.path:
    sys.path.insert(0, str(_CONTRACTS_DIR))


# ===== Constants (reuse wip_save idioms verbatim — scripts/wip_save.py) =====

SCHEMA_VERSION = "1.0"          # IncomingEvent contract version (conformance.py)
SOURCE = "aqg-hook"
GIT_TIMEOUT = 5                 # seconds; Stop blocks turn-end, keep it small
PROJECT_TOKEN_HEX = 16          # sha256(project_id)[:16] = 64-bit (R2 E)
TITLE_MAX = 200                 # first-line cap (sketch §6 capture-time bounds)
DETAIL_MAX = 4000               # body cap

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_TOKEN_SHAPED_PREFIXES: tuple[str, ...] = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "sk-", "sk-ant-", "AKIA", "ASIA", "xoxb-", "xoxp-", "AIza",
)

# git log field separator. Records are NUL-delimited via `git log -z`: a NUL byte
# is IMPOSSIBLE in a git commit message, so records never corrupt even on hostile
# message bytes (impl-audit 81f5f866 B). Fields use \x1f and are split with
# maxsplit=3 so an embedded \x1f in the body is absorbed into the body, never
# dropping the commit. The committer timestamp + sha fields are clean by construction.
_FIELD_SEP = "\x1f"
_NUL = "\x00"
_GIT_LOG_FORMAT = _FIELD_SEP.join(("%H", "%ct", "%s", "%b"))
_SINCE_BUFFER_S = 2   # query git slightly earlier than the anchor, then filter in code

# Capture-time sanitize: strip ANSI CSI + C0 control chars (keep \t and \n) + DEL.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")   # keeps \x09 (\t) and \x0a (\n)


# ===== Pure helpers (no I/O, no contract import — unit-testable) =====


def project_token(project_id: str) -> str:
    """sha256(project_id)[:16] — 64-bit, hex/filename-safe (R2 E). project_id may
    contain '/' (owner/repo); hashing makes the token slash/colon-free so it fits
    the event_id middle-segment alphabet (conformance.py _EVENT_ID_RE)."""
    return hashlib.sha256(project_id.encode("utf-8")).hexdigest()[:PROJECT_TOKEN_HEX]


def build_event_id(token: str, committer_unix_ts: str, commit_sha: str) -> str:
    """aqg-hook:<token>:<committer_unix_ts>.<commit_sha> (sketch §3 / R2 f1+G2).

    producer_seq (the 3rd ':'-segment) is '<ts>.<sha>': the leading numeric ts
    makes the store batch sort chronological (seq.py segments numeric-first), the
    sha guarantees uniqueness; both immutable → identical across sessions."""
    return f"{SOURCE}:{token}:{committer_unix_ts}.{commit_sha}"


def sanitize_text(text: str, *, max_len: int, single_line: bool) -> str:
    """Bound the blast radius of a pathological commit message before it reaches
    store/render (sketch §6). Strips ANSI + control chars; first-line + length
    cap. NOT an injection defense (that is the render-prompt's job, §6.1) — only
    surface reduction."""
    if not isinstance(text, str):
        return ""
    if single_line:
        # subject is one line; defend against embedded newlines anyway.
        text = text.splitlines()[0] if text.splitlines() else ""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    if single_line:
        text = text.strip()
    else:
        text = text.strip("\n").rstrip()
    if len(text) > max_len:
        text = text[:max_len]
    return text


def _redact_secrets(text: str) -> str:
    """Drop a field to a placeholder if it carries a real SECRET (API key / token /
    JWT / PEM), so a secret pasted into a commit message never lands unredacted in
    the ledger — which is projected + rendered (audit bbf4ca5d gpt-f3; extends the
    L1 redaction engine to the ledger-hook emitter, the one producer L1 did not
    cover). Uses the secret scanner ONLY — paths/emails are legitimate in commit
    text and are NOT redacted. Best-effort + never-fail: if the scanner is
    unavailable at hook runtime, return the text unchanged (capture is best-effort
    and the ledger is machine-local)."""
    if not text:
        return text
    try:
        from _secret_patterns import secret_counts

        if sum(secret_counts(text).values()) > 0:
            return "[redacted: contained a secret]"
    except Exception:  # aqg: top-level boundary — scanner missing → degrade, never fail
        return text
    return text


class CommitRec:
    """One parsed commit. Plain data."""

    __slots__ = ("sha", "committer_unix_ts", "subject", "body")

    def __init__(self, sha: str, committer_unix_ts: str, subject: str, body: str) -> None:
        self.sha = sha
        self.committer_unix_ts = committer_unix_ts
        self.subject = subject
        self.body = body


def parse_git_log(output: str) -> list[CommitRec]:
    """Parse the NUL-record-delimited _GIT_LOG_FORMAT stream → CommitRecs (oldest→
    newest via --reverse). Robust to hostile commit bytes (impl-audit 81f5f866 B):
    records split on NUL (impossible in a commit message); fields split with
    maxsplit=3 so an embedded \\x1f in the body is absorbed, never dropping a commit.
    A record without a hex sha / int ts is skipped (never raises)."""
    out: list[CommitRec] = []
    for rec in output.split(_NUL):
        if not rec.strip():
            continue
        fields = rec.split(_FIELD_SEP, 3)
        if len(fields) != 4:
            continue
        sha, ct, subject, body = fields
        sha = sha.strip()
        ct = ct.strip()
        # ct must be a bounded ASCII integer: str.isdigit() also accepts unicode
        # digits and arbitrarily large values, and a committer-date like
        # `--date=@99999999999999` then makes datetime.fromtimestamp(int(ct)) raise
        # OverflowError in build_event, crashing the whole list comprehension and
        # SILENTLY DROPPING the entire capture batch (audit bbf4ca5d gpt-f1 +
        # gemini-f2; workflow B1). 11 digits covers well past year 5000; skip only
        # the bad commit.
        if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha) or not re.fullmatch(r"[0-9]{1,11}", ct):
            continue
        out.append(CommitRec(sha=sha, committer_unix_ts=ct, subject=subject, body=body))
    return out


def build_event(*, project_id: str, session_id: str, commit: CommitRec) -> dict[str, Any]:
    """Map one commit → IncomingEvent dict (sketch §3). The store assigns
    ledger_seq + recorded_at on append; occurred_at is the committer date
    (display-only — projection orders by ledger_seq)."""
    token = project_token(project_id)
    occurred_at = datetime.fromtimestamp(
        int(commit.committer_unix_ts), tz=timezone.utc
    ).isoformat()
    payload: dict[str, Any] = {
        "phase_event": "stage_advanced",
        "title": _redact_secrets(sanitize_text(commit.subject, max_len=TITLE_MAX, single_line=True)),
        "commit_sha": commit.sha,
    }
    detail = _redact_secrets(sanitize_text(commit.body, max_len=DETAIL_MAX, single_line=False))
    if detail:
        payload["detail"] = detail
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": build_event_id(token, commit.committer_unix_ts, commit.sha),
        "project": project_id,
        "source": SOURCE,
        "source_ref": {"session_id": session_id},
        "kind": "progress",
        "payload": payload,
        "occurred_at": occurred_at,
    }


# ===== PR-creation capture (v2.1) — observe `gh pr create`, network-free =====

# `gh pr create` invocation, whitespace-tolerant (cd x && gh pr create …). The grep
# pre-filter in the wrapper is the cheap fast-reject; this is the rigorous match.
_GH_PR_CREATE_RE = re.compile(r"\bgh\s+pr\s+create\b")

# A PR URL of strict shape https://<host>[:port]/<owner>/<repo>/pull/<digits>. The
# strict structure bounds injection; the host (port-stripped) is the host-match key.
# The trailing (?![0-9A-Za-z]) rejects a malformed suffix like /pull/12abc → not #12
# (impl-audit b86ee8f3 D). The optional :port keeps ported GHE parseable (C).
_PR_URL_RE = re.compile(
    r"https://([A-Za-z0-9.\-]+(?::\d+)?)/([A-Za-z0-9._-]+)/([A-Za-z0-9._-]+)/pull/(\d+)(?![0-9A-Za-z])"
)


def match_gh_pr_create(command: str) -> bool:
    """True when the command invokes `gh pr create` (whitespace-tolerant). `--web` is
    NOT excluded here (impl-audit b86ee8f3 E): browser mode prints no `/pull/<N>` URL,
    so the URL-in-stdout gate skips it anyway, and a quoted `--web` in a title no
    longer suppresses a real capture."""
    return isinstance(command, str) and bool(_GH_PR_CREATE_RE.search(command))


def _url_host(match) -> str:
    """Lowercased, port-stripped host from a _PR_URL_RE match (host-only comparison
    vs origin_host, which is also port-stripped — impl-audit b86ee8f3 C)."""
    return match.group(1).lower().split(":")[0]


def parse_pr_url(stdout: str) -> "tuple[str, str, str, str, str] | None":
    """First PR URL in stdout → (url, host, owner, repo, number); None if absent.
    host is lowercased + port-stripped. NOTE: run_pr_capture does NOT use this (it
    iterates all matches to find the first host-matching one — impl-audit b86ee8f3 B);
    this stays a convenience for tests."""
    if not isinstance(stdout, str):
        return None
    m = _PR_URL_RE.search(stdout)
    if not m:
        return None
    return (m.group(0), _url_host(m), m.group(2), m.group(3), m.group(4))


def _remote_host(url: str) -> "str | None":
    """Host of a git remote URL (url-form https/ssh or scp-form git@host:path),
    lowercased; None if unparseable. Mirrors project_id.normalize_remote_url's
    url/scp split but extracts the host, not the path."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = url.strip()
    if "://" in url:
        netloc = urlparse(url).netloc          # may be user@host:port
        host = netloc.split("@")[-1].split(":")[0]
    elif ":" in url:
        host = url.split(":", 1)[0].split("@")[-1]   # scp-like git@host:path
    else:
        return None
    return host.lower() or None


def build_pr_event(*, project_id: str, session_id: str, pr_url: str, pr_number: str) -> dict[str, Any]:
    """Map an observed PR creation → IncomingEvent (sketch §3). title is GENERATED
    (no untrusted free text); occurred_at = capture time (display-only)."""
    token = project_token(project_id)
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"{SOURCE}:{token}:pr.{pr_number}",
        "project": project_id,
        "source": SOURCE,
        "source_ref": {"session_id": session_id},
        "kind": "progress",
        "payload": {
            "phase_event": "stage_advanced",
            "title": f"Opened PR #{pr_number}",
            "pr_url": pr_url,
        },
        "occurred_at": _now_iso(),
    }


# ===== session_id (reuse wip_save sanitize + fallback) =====


def _looks_token_shaped(value: str) -> bool:
    return any(value.startswith(p) for p in _TOKEN_SHAPED_PREFIXES)


def _cwd_token(cwd: Path) -> str:
    """Stable 12-hex token for a resolved cwd — identifies the project checkout, so
    the state file can be keyed per-(session, project) (impl-audit 81f5f866 D)."""
    try:
        resolved = str(cwd.resolve())
    except (OSError, RuntimeError):
        resolved = str(cwd)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def resolve_session_id(event: dict, *, cwd: Path) -> str:
    """event.session_id if valid AND not token-shaped, else _cwd_token(cwd)
    (wip_save discipline: regex guards path-traversal; stable fallback keeps one
    session → one state file)."""
    raw = event.get("session_id")
    if isinstance(raw, str) and SESSION_ID_RE.match(raw) and not _looks_token_shaped(raw):
        return raw
    return _cwd_token(cwd)


# ===== git subprocess (reuse wip_save _safe_run) =====


def _safe_run(cmd: list[str], *, cwd: Path, timeout: int = GIT_TIMEOUT) -> tuple[int, str]:
    """Run a subprocess with a hard timeout + non-interactive env. Never raises."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), text=True, errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, check=False, env=env,
        )
        return proc.returncode, (proc.stdout or "")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 127, ""


def git_head(cwd: Path) -> "str | None":
    """Current HEAD sha, or None (not a git repo / unborn / detached-no-commit)."""
    rc, out = _safe_run(["git", "rev-parse", "HEAD"], cwd=cwd)
    if rc != 0:
        return None
    sha = out.strip()
    return sha if re.fullmatch(r"[0-9a-fA-F]{7,64}", sha) else None


def _anchor_unix(since_iso: str) -> "int | None":
    """Parse the stored anchor ISO → unix seconds, or None if unparseable."""
    if not isinstance(since_iso, str):
        return None
    s = since_iso.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except (ValueError, TypeError, OverflowError, OSError):
        # OverflowError/OSError: .timestamp() on an extreme-year datetime (e.g. a
        # corrupted anchor state file) is out of the platform range — degrade to
        # None (git's own --since parse) rather than crash the capture (audit
        # bbf4ca5d gemini-f2; workflow B8).
        return None


def git_commits_since(cwd: Path, since_iso: str) -> list[CommitRec]:
    """Commits reachable from HEAD with committer-date >= the anchor, `--no-merges`,
    oldest→newest. The time window IS the session-membership signal (R2 B/C/D): no
    author filter, no sha-ancestry guard. Bounded by --since (fast, no network).

    The boundary is made DETERMINISTIC in code (impl-audit 81f5f866 C): git's
    `--since` is fuzzily "after" and version-dependent, so we query a couple of
    seconds EARLIER and then keep only commits with committer_ts >= anchor_ts. A
    same-second-as-anchor commit is therefore captured, not silently lost."""
    anchor_ts = _anchor_unix(since_iso)
    if anchor_ts is not None:
        query_since = datetime.fromtimestamp(
            anchor_ts - _SINCE_BUFFER_S, tz=timezone.utc
        ).isoformat()
    else:
        query_since = since_iso  # degraded: rely on git's own parse, no code filter
    rc, out = _safe_run(
        [
            "git", "log", "--no-merges", "--reverse", "-z",
            f"--since={query_since}", f"--pretty=format:{_GIT_LOG_FORMAT}", "HEAD",
        ],
        cwd=cwd,
    )
    if rc != 0:
        return []
    commits = parse_git_log(out)
    if anchor_ts is not None:
        commits = [c for c in commits if int(c.committer_unix_ts) >= anchor_ts]
    return commits


# ===== state file (anchor) — atomic, path-traversal-guarded =====


def _state_path(session_token: str, *, state_dir: Path) -> Path:
    """Resolve <state_dir>/<session_token>.json, asserting containment (defense in
    depth atop the SESSION_ID_RE sanitize — wip_save._resolve_safe_path pattern)."""
    candidate = (state_dir / f"{session_token}.json").resolve()
    base = state_dir.resolve()
    if base not in candidate.parents and candidate.parent != base:
        raise ValueError(f"state path {candidate} escapes {base}")
    return candidate


def load_state(session_token: str, *, state_dir: Path) -> dict[str, Any]:
    """Load the per-session anchor state, or {} if absent/unreadable (never raises)."""
    try:
        path = _state_path(session_token, state_dir=state_dir)
    except ValueError:
        return {}
    if not path.is_file():
        return {}
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
        return obj if isinstance(obj, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_state(session_token: str, state: dict, *, state_dir: Path) -> None:
    """Atomically write the anchor state (tmp → replace). Best-effort: logs + returns
    on any I/O error (never raises — the state is an optimization, not correctness)."""
    try:
        state_dir.mkdir(parents=True, exist_ok=True)
        final = _state_path(session_token, state_dir=state_dir)
        fd, tmp = tempfile.mkstemp(prefix=".tmp-hookstate-", suffix=".json", dir=str(state_dir))
    except (OSError, ValueError) as exc:
        print(f"[ledger_hook] state setup failed: {exc}", file=sys.stderr)
        return
    ok = False
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, final)
        ok = True
    except OSError as exc:
        print(f"[ledger_hook] state write failed: {exc}", file=sys.stderr)
    finally:
        if not ok:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ===== emit =====


def _emit_events(commit_events, *, inbox_dir=None) -> set:
    """Write each (sha, IncomingEvent) into the ledger inbox via the contract API.
    Per-event failure is logged + skipped (never-fail) — so a transient write error
    must NOT mark the commit emitted (impl-audit 81f5f866 A). Returns the SET of shas
    SUCCESSFULLY written; the caller advances optimization state only for those. The
    lazy import means a missing contract package → caught by main → silent exit 0.
    `inbox_dir` overrides the default inbox (tests)."""
    from ledger.paths import write_incoming_event

    ok_shas: set = set()
    for sha, event in commit_events:
        try:
            write_incoming_event(event, inbox_dir=inbox_dir)
            ok_shas.add(sha)
        except Exception as exc:  # aqg: top-level boundary — never fail the hook
            print(f"[ledger_hook] emit skipped {event.get('event_id')!r}: {exc}", file=sys.stderr)
    return ok_shas


# ===== orchestration =====


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def run_anchor(*, session_token: str, cwd: Path, state_dir: Path, now_iso: str) -> dict:
    """SessionStart: record {session_start_time, baseline_sha} ONCE. Does NOT
    overwrite an existing anchor (preserve it across resume/compact SessionStart)."""
    state = load_state(session_token, state_dir=state_dir)
    if state.get("session_start_time"):
        return state  # anchor already set this session — keep it
    state = {
        "session_start_time": now_iso,
        "baseline_sha": git_head(cwd),
        "emitted_shas": [],
    }
    save_state(session_token, state, state_dir=state_dir)
    return state


def run_capture(
    *, project_id: str, session_id: str, session_token: str, cwd: Path,
    state_dir: Path, now_iso: str, inbox_dir: "Path | None" = None,
) -> tuple[int, list[dict]]:
    """Stop: capture commits in the session window → emit progress events.
    Returns (emitted_count, events) — events returned for testability."""
    head = git_head(cwd)
    if head is None:
        return 0, []  # not a git repo / unborn HEAD

    state = load_state(session_token, state_dir=state_dir)
    since = state.get("session_start_time")
    if not since:
        # No anchor (SessionStart never ran AND no prior state): set anchor=now and
        # emit nothing this turn (bounded first-turn gap, sketch §4.1 step 2). The
        # ONLY emit-nothing degraded case.
        save_state(
            session_token,
            {"session_start_time": now_iso, "baseline_sha": head, "emitted_shas": []},
            state_dir=state_dir,
        )
        return 0, []

    if state.get("baseline_sha") == head:
        return 0, []  # fast path: HEAD unchanged since last capture

    emitted_shas = state.get("emitted_shas")
    seen = set(emitted_shas) if isinstance(emitted_shas, list) else set()
    commits = [c for c in git_commits_since(cwd, since) if c.sha not in seen]

    commit_events = [
        (c.sha, build_event(project_id=project_id, session_id=session_id, commit=c))
        for c in commits
    ]
    ok_shas = _emit_events(commit_events, inbox_dir=inbox_dir)

    # Advance optimization state CONSERVATIVELY (impl-audit 81f5f866 A): mark only
    # SUCCESSFULLY-written commits as emitted, and advance baseline_sha to head ONLY
    # when every candidate emitted. On a partial failure, baseline stays put so the
    # next Stop re-scans the window and retries the failed commit (which is absent
    # from emitted_shas); store dedup drops the ones that did get written.
    new_state = dict(state)
    new_state["emitted_shas"] = sorted(seen | ok_shas)
    if len(ok_shas) == len(commits):
        new_state["baseline_sha"] = head
    save_state(session_token, new_state, state_dir=state_dir)
    return len(ok_shas), [ev for _, ev in commit_events]


def origin_host(cwd: Path) -> "str | None":
    """Host of the repo's origin remote (local `git config` read — no network), or
    None if there is no resolvable origin. Used for the §3 host-match."""
    rc, out = _safe_run(["git", "config", "--get", "remote.origin.url"], cwd=cwd)
    if rc != 0:
        return None
    return _remote_host(out.strip())


def _pr_from_git_operation(
    command: str, git_operation: "dict | None", origin_host_str: str
) -> "tuple[str, str] | None":
    """Structured path (preferred): Claude Code pre-parses the gh stdout into
    `tool_response.gitOperation.pr` {number:int, url, action} (BashOutput,
    sdk-tools.d.ts:2464; transcript-confirmed 2026-05-30). Returns (pr_url, pr_number)
    for a CREATED pr — else None (caller falls back to stdout). Gates, in order:
    - the command is a `gh pr create` (mirrors the stdout path; audit bbcb14d8 gpt-f1);
    - action == "created" (v2.1 scope; a lifecycle verb merged/closed/ready/draft → None,
      and merge is uncapturable anyway — empirical doc §4);
    - number is a positive int (gitOperation-validity gate);
    - url is a WHOLE-VALUE PR url (fullmatch, not substring) whose host matches origin
      (the anti-phishing gate shared with the stdout path; audit bbcb14d8 gpt-f2);
    - the structured number AGREES with its own url's number (else malformed → fall back).
    pr_url + pr_number both come from the one validated match → self-consistent."""
    if not match_gh_pr_create(command) or not isinstance(git_operation, dict):
        return None
    pr = git_operation.get("pr")
    if not isinstance(pr, dict) or pr.get("action") != "created":
        return None
    number, url = pr.get("number"), pr.get("url")
    if not isinstance(url, str) or not isinstance(number, int) or number <= 0:
        return None  # number int>0 is the gitOperation-validity gate
    m = _PR_URL_RE.fullmatch(url)
    if not m or _url_host(m) != origin_host_str:
        return None  # not a whole-value PR url, or non-origin host (phishing) → reject
    if m.group(4) != str(number):
        return None  # structured number disagrees with its own url → malformed → fall back
    return (m.group(0), m.group(4))


def _pr_from_stdout(
    command: str, stdout: str, origin_host_str: str
) -> "tuple[str, str] | None":
    """Fallback path (legacy v2.1): parse the FIRST origin-host PR URL from the stdout
    of a `gh pr create`. Byte-for-byte the pre-upgrade behavior — an earlier non-origin
    URL (or a forged look-alike) must not shadow the real PR (impl-audit b86ee8f3 B);
    a look-alike host never matches → no phishing link. Returns (pr_url, pr_number)."""
    if not match_gh_pr_create(command) or not isinstance(stdout, str):
        return None
    for m in _PR_URL_RE.finditer(stdout):
        if _url_host(m) == origin_host_str:
            return (m.group(0), m.group(4))
    return None


def run_pr_capture(
    *, command: str, stdout: str, git_operation: "dict | None", session_id: str, cwd: Path,
    inbox_dir: "Path | None" = None,
) -> tuple[int, list[dict]]:
    """PostToolUse(Bash): observe a `gh pr create` → emit one PR-creation progress event.
    gitOperation-first (Claude Code's structured pre-parse), stdout-fallback (legacy URL
    parse) — progressive enhancement, never regresses. BOTH paths require the command to
    be a `gh pr create` AND the PR url host to equal the origin remote host (the
    anti-phishing gate; a look-alike host or a no-origin repo yields no event).

    No is_error gate: BashOutput exposes no exit-code field to gate on (empirical doc §6).
    A failed `gh pr create` is normally not captured (no created gitOperation, no PR url),
    EXCEPT the PR-already-exists case (gh exits nonzero yet prints the existing PR's url):
    the stdout fallback then re-emits that pr.N — harmless, because the event_id pr.N is
    idempotently deduped by the store (same PR already captured at creation), an accepted
    low-fidelity tradeoff matching pre-upgrade v2.1 (audit bbcb14d8 gemini-f1). Lifecycle
    is out of scope (empirical doc §4)."""
    o_host = origin_host(cwd)
    if not o_host:
        return 0, []  # no resolvable origin host → PR-capture disabled (host-match)
    parsed = _pr_from_git_operation(command, git_operation, o_host)
    if parsed is None:
        parsed = _pr_from_stdout(command, stdout, o_host)
    if parsed is None:
        return 0, []
    pr_url, pr_number = parsed
    from ledger.project_id import project_id_from_repo
    project_id = project_id_from_repo(str(cwd))
    event = build_pr_event(
        project_id=project_id, session_id=session_id, pr_url=pr_url, pr_number=pr_number
    )
    ok = _emit_events([(f"pr.{pr_number}", event)], inbox_dir=inbox_dir)
    return len(ok), [event]


def _resolve_cwd(event: dict) -> Path:
    """AQG_HOOK_PROJECT_DIR env (set by the wrapper from CLAUDE_PROJECT_DIR) →
    event.cwd → Path.cwd() (wip_save resolution order).

    The two fallbacks are INDEPENDENT: a set-but-invalid env dir (non-existent /
    stale) must still fall through to event.cwd, not jump straight to Path.cwd().
    The previous `elif env_cwd` chained the event.cwd branch to env_cwd being
    falsy, breaking the documented chain (audit bbf4ca5d gpt-f4 + gemini-f3;
    workflow B6)."""
    env_cwd = os.environ.get("AQG_HOOK_PROJECT_DIR", "").strip()
    if env_cwd:
        try:
            cand = Path(env_cwd).expanduser()
            if cand.is_dir():
                return cand
        except (OSError, ValueError):
            pass
    ev_cwd = event.get("cwd")
    if isinstance(ev_cwd, str):
        try:
            cand = Path(ev_cwd).expanduser()
            if cand.is_dir():
                return cand
        except (OSError, ValueError):
            pass
    return Path.cwd()


def main(argv: "list[str] | None" = None, *, stdin_text: "str | None" = None) -> int:
    """Hook entry. ALWAYS returns 0 (never fail Claude Code); diagnostics → stderr."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--mode", choices=("anchor", "capture", "pr-capture"), required=True)
    parser.add_argument("--state-dir", default=None)  # test override
    try:
        args, _ = parser.parse_known_args(argv)
    except SystemExit:
        return 0  # bad/missing --mode → never fail the hook

    # 1. stdin event (session_id, maybe cwd) — graceful on absent/non-JSON.
    if stdin_text is None:
        try:
            stdin_text = sys.stdin.read()
        except Exception as exc:  # aqg: top-level boundary
            print(f"[ledger_hook] stdin read failed: {exc}", file=sys.stderr)
            return 0
    event: dict[str, Any] = {}
    if stdin_text and stdin_text.strip():
        try:
            parsed = json.loads(stdin_text)
            if isinstance(parsed, dict):
                event = parsed
        except (ValueError, json.JSONDecodeError):
            pass  # proceed with empty event

    try:
        cwd = _resolve_cwd(event)
        session_id = resolve_session_id(event, cwd=cwd)

        if args.mode == "pr-capture":
            # PostToolUse(Bash): STATELESS — observe `gh pr create` from the payload.
            # gitOperation-first (tool_response.gitOperation.pr, Claude Code's structured
            # pre-parse of stdout), stdout-fallback (tool_response.stdout URL). No is_error
            # gate — BashOutput has no exit-code field (empirical doc §6); see run_pr_capture
            # for the PR-already-exists low-fidelity note (audit bbcb14d8 gemini-f1).
            ti = event.get("tool_input") or {}
            tr = event.get("tool_response") or {}
            command = ti.get("command", "") if isinstance(ti, dict) else ""
            stdout = tr.get("stdout", "") if isinstance(tr, dict) else ""
            git_operation = tr.get("gitOperation") if isinstance(tr, dict) else None
            count, _ = run_pr_capture(
                command=command, stdout=stdout, git_operation=git_operation,
                session_id=session_id, cwd=cwd,
            )
            if count:
                print("[ledger_hook] captured PR creation", file=sys.stderr)
            return 0

        # anchor / capture: per-(session, project) state. A reused session_id across
        # project dirs must not cross-contaminate (impl-audit 81f5f866 D) — key by
        # both (filename-safe, so the _state_path containment check still holds).
        session_token = f"{session_id}.{_cwd_token(cwd)}"
        if args.state_dir is not None:
            state_dir = Path(args.state_dir)
        else:
            from ledger.paths import ledger_hook_state_dir
            state_dir = ledger_hook_state_dir()

        now_iso = _now_iso()
        if args.mode == "anchor":
            run_anchor(session_token=session_token, cwd=cwd, state_dir=state_dir, now_iso=now_iso)
        else:  # capture
            from ledger.project_id import project_id_from_repo
            project_id = project_id_from_repo(str(cwd))
            count, _ = run_capture(
                project_id=project_id, session_id=session_id, session_token=session_token,
                cwd=cwd, state_dir=state_dir, now_iso=now_iso,
            )
            if count:
                print(f"[ledger_hook] captured {count} commit(s)", file=sys.stderr)
    except Exception as exc:  # aqg: top-level boundary — never fail Claude Code
        print(f"[ledger_hook] {args.mode} failed: {exc}", file=sys.stderr)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
