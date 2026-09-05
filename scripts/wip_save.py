#!/usr/bin/env python3
"""WIP snapshot saver — Claude Code PreCompact hook handler (Wave 1 P0 #7).

Triggered by Claude Code on PreCompact event. Reads JSON event from **stdin**,
collects non-PII WIP snapshot, writes atomically to `~/.aqg/wip/<session_id>.json`.

NEVER prints to stdout (would be injected into compaction context). All
diagnostics → stderr.

Implemented per the triple-audit's 14 accepted findings (audit_id 656913cb):
- session_id strictly sanitized to prevent path traversal (gpt-5.5 #1)
- git_branch is hashed, never raw (gpt-5.5 #3 + o3 #1 convergent)
- all subprocesses use timeout=2s + GIT_TERMINAL_PROMPT=0 (gpt-5.5 #4 + gemini #4)
- never calls jq (gpt-5.5 #2 + gemini #6 + o3 #4 convergent)
- gh pr list skipped by default (network too slow, risks hanging Claude Code)
- cwd_sha256_first8 prevents cross-project bleed (gemini #2)
- fallback session_id `sha256(cwd)[:12]` carries no timestamp (gemini #5)
- never prints to stdout (o3 #5)
- snapshot is gated through _wip_redaction.assert_safe_wip_snapshot
"""

from __future__ import annotations

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


# ===== Constants =====

SCHEMA_VERSION = 1
ALL_SUBPROCESS_TIMEOUT = 2  # seconds; PreCompact must not hang

DEFAULT_BRANCHES: frozenset[str] = frozenset({"main", "master", "develop", "trunk"})

SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ===== Path resolver =====


def _default_wip_dir() -> Path:
    """Single default WIP dir resolver, shared by save + recover (gpt-5.5 #6 accepted).

    Prefers $XDG_DATA_HOME/aqg/wip; falls back to ~/.aqg/wip.
    """
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg and xdg.strip():
        return Path(xdg).expanduser() / "aqg" / "wip"
    return Path.home() / ".aqg" / "wip"


# ===== Hash helpers =====


def _sha256_first8(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# ===== Session ID resolution =====


def _resolve_session_id(event: dict, *, cwd: Path) -> str:
    """Use event.session_id if valid AND not token-shaped, else stable fallback.

    Triple-audit gpt-5.5 #1 + gemini #5 accepted: regex validation prevents path
    traversal; the fallback carries no timestamp so repeated PreCompact runs in the
    same session still overwrite a single file.
    Post-impl dual-audit gpt-5.5 #2 + gemini #2 accepted: reject token-shape; use
    cwd.resolve() to prevent symlinks from fragmenting session_id.
    """
    raw = event.get("session_id")
    if (
        isinstance(raw, str)
        and SESSION_ID_RE.match(raw)
        and not _looks_token_shaped(raw)
    ):
        return raw
    # Fallback: stable hash of resolved cwd (12 chars from sha256 hex)
    try:
        resolved = str(cwd.resolve())
    except (OSError, RuntimeError):
        resolved = str(cwd)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


_TOKEN_SHAPED_PREFIXES: tuple[str, ...] = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "sk-", "sk-ant-", "AKIA", "ASIA", "xoxb-", "xoxp-", "AIza",
)


def _looks_token_shaped(value: str) -> bool:
    return any(value.startswith(p) for p in _TOKEN_SHAPED_PREFIXES)


# ===== Subprocess helpers (all timeout-bounded) =====


def _safe_run(cmd: list[str], *, cwd: Path, timeout: int = ALL_SUBPROCESS_TIMEOUT) -> tuple[int, str]:
    """Run subprocess with hard timeout + non-interactive env. Never raise."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # prevent git prompt hang
    # Post-impl dual-audit gpt-5.5 #3 accepted: GIT_OPTIONAL_LOCKS=0 is what
    # disables it ("1" is the default = enable optional locks); 0 lets
    # read-only probes avoid grabbing the lock.
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
        return proc.returncode, (proc.stdout or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return 127, ""


# ===== Git status collectors =====


def _collect_cwd_status(cwd: Path, *, probe_cli: bool = True) -> dict[str, Any]:
    """Collect cwd / git status. NEVER returns raw branch name or paths.

    A None on any field means "not probed / unknown"; safe defaults.
    """
    out: dict[str, Any] = {
        "exists": cwd.exists(),
        "is_directory": cwd.is_dir(),
        "git_branch_sha256_first8": None,
        "git_is_default_branch": None,
        "git_branch_status": "unknown",
        "git_dirty": None,
        "git_ahead_count": None,
        "git_behind_count": None,
        "modified_files_count": None,
        "untracked_files_count": None,
    }
    if not cwd.is_dir():
        out["git_branch_status"] = "missing"
        return out
    if not probe_cli:
        return out

    # git rev-parse --abbrev-ref HEAD
    rc, stdout = _safe_run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
    if rc != 0:
        # not a git repo or git missing
        out["git_branch_status"] = "missing"
        return out
    branch = stdout.strip()
    if branch == "HEAD":
        out["git_branch_status"] = "detached"
    else:
        out["git_branch_status"] = "present"
        # NEVER store raw branch name (triple-audit gpt-5.5 #3 + o3 #1)
        out["git_branch_sha256_first8"] = _sha256_first8(branch)
        out["git_is_default_branch"] = branch in DEFAULT_BRANCHES

    # git status --porcelain (get modified/untracked count in one pass)
    rc, stdout = _safe_run(["git", "status", "--porcelain"], cwd=cwd)
    if rc == 0:
        modified = 0
        untracked = 0
        for line in stdout.splitlines():
            if line.startswith("?? "):
                untracked += 1
            elif line.strip():
                modified += 1
        out["git_dirty"] = (modified + untracked) > 0
        out["modified_files_count"] = modified
        out["untracked_files_count"] = untracked

    # git rev-list --count @{u}.. and ..@{u} (ahead/behind)
    # use --left-right to get both at once; fails when no upstream, graceful
    rc, stdout = _safe_run(
        ["git", "rev-list", "--left-right", "--count", "HEAD...@{u}"], cwd=cwd
    )
    if rc == 0:
        parts = stdout.split()
        if len(parts) == 2:
            try:
                out["git_ahead_count"] = int(parts[0])
                out["git_behind_count"] = int(parts[1])
            except ValueError:
                pass

    return out


def _collect_open_pr_count(cwd: Path, *, probe_cli: bool = False) -> int | None:
    """Count open PRs via `gh pr list --json number --limit 100`.

    Triple-audit gemini #4 accepted: probe_cli=False by default — network calls are
    too slow and easily hang PreCompact. Only runs when the caller explicitly opts in.
    NEVER uses jq (triple-audit gpt-5.5 #2 + gemini #6 + o3 #4 convergent: stdlib only).
    """
    if not probe_cli:
        return None
    rc, stdout = _safe_run(
        ["gh", "pr", "list", "--json", "number", "--limit", "100"], cwd=cwd
    )
    if rc != 0:
        return None
    try:
        data = json.loads(stdout)
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(data, list):
        return None
    return len(data)


# ===== Todo state =====


def _collect_todo_state(event: dict) -> dict[str, int] | None:
    """Parse event.todo (if hook payload includes TodoWrite snapshot).

    Whether Claude Code's current hook payload includes todo is unclear; gracefully
    returns None when absent.
    Schema (inferred; return None if it does not match):
        event.todo = {"items": [{"status": "in_progress" | ...}, ...]}
    """
    todo = event.get("todo")
    if not isinstance(todo, dict):
        return None
    items = todo.get("items")
    if not isinstance(items, list):
        return None
    counts = {
        "items_count": len(items),
        "in_progress_count": 0,
        "completed_count": 0,
        "pending_count": 0,
    }
    for item in items:
        if not isinstance(item, dict):
            continue
        status = item.get("status")
        if status == "in_progress":
            counts["in_progress_count"] += 1
        elif status == "completed":
            counts["completed_count"] += 1
        elif status == "pending":
            counts["pending_count"] += 1
    return counts


# ===== Snapshot composer =====


def _build_snapshot(
    *,
    session_id: str,
    cwd: Path,
    event: dict,
    probe_cli: bool = True,
    probe_network: bool = False,
) -> dict[str, Any]:
    """Compose WIP snapshot dict per schema. NO raw paths/secrets/branch names."""
    return {
        "schema_version": SCHEMA_VERSION,
        "session_id": session_id,
        "saved_at_iso": datetime.now(timezone.utc).isoformat(),
        "trigger": "PreCompact",
        "cwd_sha256_first8": _sha256_first8(str(cwd.resolve())),
        "cwd_status": _collect_cwd_status(cwd, probe_cli=probe_cli),
        "recent_open_pr_count": _collect_open_pr_count(cwd, probe_cli=probe_network),
        "todo_state": _collect_todo_state(event),
        "marker": "auto-saved-precompact",
    }


# ===== Atomic write =====


def _resolve_safe_path(session_id: str, *, wip_dir: Path) -> Path:
    """Resolve and assert path stays under wip_dir (defense in depth).

    Triple-audit gpt-5.5 #1 accepted: regex already prevents path traversal; this
    adds a realpath check as a backstop layer.
    """
    candidate = (wip_dir / f"{session_id}.json").resolve()
    wip_resolved = wip_dir.resolve()
    if wip_resolved not in candidate.parents and candidate.parent != wip_resolved:
        raise ValueError(
            f"resolved path {candidate} escapes wip_dir {wip_resolved}"
        )
    return candidate


def _write_snapshot_atomic(snapshot: dict, *, wip_dir: Path) -> Path:
    """mkdir -p wip_dir; write to tempfile; rename to final path."""
    wip_dir.mkdir(parents=True, exist_ok=True)
    final_path = _resolve_safe_path(snapshot["session_id"], wip_dir=wip_dir)
    # tempfile in same dir to keep rename atomic on POSIX
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-wip-", suffix=".json", dir=str(wip_dir))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(snapshot, fh, ensure_ascii=False, indent=2, sort_keys=True)
        os.replace(tmp_path, final_path)
    except Exception:
        # cleanup tempfile if rename failed
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return final_path


# ===== Main entry =====


def main(stdin_text: str | None = None, *, wip_dir: Path | None = None) -> int:
    """PreCompact entry. NEVER prints to stdout (compaction injection guard).

    Always exit 0 to not block Claude Code; errors → stderr only.
    """
    # 1. Read event from stdin
    if stdin_text is None:
        try:
            stdin_text = sys.stdin.read()
        except Exception as exc:  # noqa: BLE001
            print(f"[wip_save] stdin read failed: {exc}", file=sys.stderr)
            return 0

    event: dict[str, Any] = {}
    if stdin_text and stdin_text.strip():
        try:
            event = json.loads(stdin_text)
            if not isinstance(event, dict):
                event = {}
        except (ValueError, json.JSONDecodeError) as exc:
            print(f"[wip_save] stdin not JSON, proceeding with empty event: {exc}", file=sys.stderr)

    # 2. cwd resolution priority:
    #    a) AQG_HOOK_PROJECT_DIR env (set by bash wrapper from CLAUDE_PROJECT_DIR)
    #    b) event.cwd (if Claude Code includes it in stdin payload)
    #    c) Path.cwd() fallback
    cwd = Path.cwd()
    env_cwd = os.environ.get("AQG_HOOK_PROJECT_DIR", "").strip()
    if env_cwd:
        try:
            cwd_candidate = Path(env_cwd).expanduser()
            if cwd_candidate.is_dir():
                cwd = cwd_candidate
        except (OSError, ValueError):
            pass
    elif "cwd" in event and isinstance(event["cwd"], str):
        try:
            cwd_candidate = Path(event["cwd"]).expanduser()
            if cwd_candidate.is_dir():
                cwd = cwd_candidate
        except (OSError, ValueError):
            pass

    # 3. Resolve session id (sanitize / fallback)
    session_id = _resolve_session_id(event, cwd=cwd)

    # 4. Compose snapshot
    try:
        snapshot = _build_snapshot(session_id=session_id, cwd=cwd, event=event)
    except Exception as exc:  # noqa: BLE001
        print(f"[wip_save] snapshot build failed: {exc}", file=sys.stderr)
        return 0

    # 5. Validate schema (belt-and-suspenders gatekeeping)
    try:
        from _wip_redaction import assert_safe_wip_snapshot
    except ImportError as exc:
        print(f"[wip_save] _wip_redaction import failed: {exc}", file=sys.stderr)
        return 0
    try:
        assert_safe_wip_snapshot(snapshot)
    except Exception as exc:  # noqa: BLE001
        print(f"[wip_save] snapshot schema validation failed: {exc}", file=sys.stderr)
        return 0

    # 6. Write atomically
    target_dir = wip_dir or _default_wip_dir()
    try:
        path = _write_snapshot_atomic(snapshot, wip_dir=target_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"[wip_save] write failed: {exc}", file=sys.stderr)
        return 0

    # 7. NEVER print to stdout (compaction injection guard, o3 #5)
    # Post-impl dual-audit gpt-5.5 #5 accepted: log must not contain absolute
    # path (includes username); use ~/relative form.
    print(f"[wip_save] saved {_redact_log_path(path)}", file=sys.stderr)
    return 0


def _redact_log_path(path: Path) -> str:
    """Replace $HOME prefix with ~ for log readability without leaking username."""
    try:
        home = Path.home()
        rel = path.resolve().relative_to(home.resolve())
        return f"~/{rel}"
    except (ValueError, OSError):
        return path.name  # fallback: filename only


if __name__ == "__main__":
    raise SystemExit(main())
