#!/usr/bin/env python3
"""WIP checkpoint — true code snapshot to a private git ref.

Upgrades the existing WIP "beacon" (scripts/wip_save.py, which only stores
metadata counts) into a true checkpoint: snapshot the current worktree changes
(including untracked) into a dangling commit, stored at refs/aqg-wip/<session>,
with **zero touch to the worktree / index / current branch / stash**.

Why store a git object instead of JSON:
- The beacon (JSON) goes into SessionStart context, so it must be secret-free →
  it can only store counts.
- The checkpoint is real code; it lives only in the local object store, never
  entering context and never going outbound (refs/aqg-wip/* is not under
  refs/heads/*, so `git push` won't push it by default) → loose locally,
  strict outbound.

Design:
- save: triggered on Stop / PreCompact. clean → skip; dirty → temp-index
  plumbing builds a tree including untracked files → commit-tree → update-ref.
  tree unchanged → dedup skip. The snapshot captures the **final current
  worktree content** (including untracked), not distinguishing staged/unstaged —
  what loss-prevention guards is "the work in your worktree right now", not the
  staging plan; re-stage as needed after recovery.
- recover: triggered on SessionStart. Lists refs/aqg-wip/*, GCs over-age refs,
  and **only prompts** recovery commands (never auto-modifies the worktree); the
  prompt text contains only counts + command templates, no raw filenames / diff
  / secret.
- Never blocks Claude Code: all paths exit 0, errors → stderr (warn-only spirit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ===== Constants =====

GIT_TIMEOUT = 5  # seconds; Stop hook fears a hang, give a big repo's `add -A` more room
REF_PREFIX = "refs/aqg-wip"

# session_id strict charset (prevent path traversal into the ref name)
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# Checkpoint commits are machine-made: fixed identity keeps them deterministic,
# privacy-clean (no user name/email leaks into the dangling commit), and lets
# commit-tree work even where git refuses to infer identity
# (e.g. user.useConfigOnly=true).
_WIP_COMMIT_IDENTITY: dict[str, str] = {
    "GIT_AUTHOR_NAME": "aqg-wip",
    "GIT_AUTHOR_EMAIL": "aqg-wip@local",
    "GIT_COMMITTER_NAME": "aqg-wip",
    "GIT_COMMITTER_EMAIL": "aqg-wip@local",
}


# ===== Result types =====


@dataclass(frozen=True)
class SaveResult:
    action: str  # created | skipped_clean | skipped_unchanged | skipped_error | error
    ref: str | None = None
    detail: str = ""


@dataclass(frozen=True)
class _BuildResult:
    commit: str | None
    status: str  # "ok" | "clean" | "error"


@dataclass(frozen=True)
class RecoverResult:
    shown: int  # checkpoints surfaced to the user
    pruned: int  # refs auto-removed (too old or already finalized)
    message: str  # human-readable prompt (empty when nothing to surface)


# ===== Subprocess helper (timeout-bounded, non-interactive) =====


def _run_git(
    repo: Path,
    args: list[str],
    *,
    input_text: str | None = None,
    extra_env: dict[str, str] | None = None,
    timeout: int = GIT_TIMEOUT,
) -> tuple[int, str]:
    """Run `git -C <repo> <args>`; never raise. Returns (returncode, stdout)."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"  # prevent git interactive prompt hang
    env["GIT_OPTIONAL_LOCKS"] = "0"  # read-only probes don't grab locks
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            input=input_text,
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


# ===== Checkpoint commit builder (temp-index plumbing) =====


def _is_dirty(repo: Path) -> bool | None:
    """True if worktree has any modified/untracked change. None = not a git repo."""
    rc, out = _run_git(repo, ["status", "--porcelain"])
    if rc != 0:
        return None
    return bool(out.strip())


def _build_checkpoint_commit(repo: Path) -> _BuildResult:
    """Snapshot worktree (incl. untracked) into a dangling commit.

    Uses a throwaway GIT_INDEX_FILE so the real index / worktree / branch /
    stash are NEVER touched. status: "clean" (nothing to save / not a repo),
    "error" (a git step failed or timed out — caller keeps any prior ref intact),
    or "ok" (commit holds the snapshot). Distinguishing clean from error means a
    large-repo timeout is no longer silently reported as "nothing to save".
    """
    if not _is_dirty(repo):
        return _BuildResult(commit=None, status="clean")  # clean / not-a-repo

    # HEAD may not exist (fresh repo, no commit): then commit-tree has no parent.
    rc, head = _run_git(repo, ["rev-parse", "--verify", "HEAD"])
    have_head = rc == 0 and bool(head)

    tmp_dir = tempfile.mkdtemp(prefix="aqg-wip-idx-")
    try:
        env = {"GIT_INDEX_FILE": str(Path(tmp_dir) / "index")}
        if have_head:
            rc, _ = _run_git(repo, ["read-tree", "HEAD"], extra_env=env)
            if rc != 0:
                return _BuildResult(commit=None, status="error")
        # stage everything (tracked mods + untracked) into the THROWAWAY index
        rc, _ = _run_git(repo, ["add", "-A"], extra_env=env)
        if rc != 0:
            return _BuildResult(commit=None, status="error")
        rc, tree = _run_git(repo, ["write-tree"], extra_env=env)
        if rc != 0 or not tree:
            return _BuildResult(commit=None, status="error")
        ct_args = ["commit-tree", tree]
        if have_head:
            ct_args += ["-p", head]
        rc, commit = _run_git(
            repo,
            ct_args,
            input_text="aqg-wip checkpoint\n",
            extra_env=_WIP_COMMIT_IDENTITY,
        )
        if rc != 0 or not commit:
            return _BuildResult(commit=None, status="error")
        return _BuildResult(commit=commit, status="ok")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _tree_of(repo: Path, rev: str) -> str | None:
    """Resolve <rev>^{tree}; None if rev/tree missing."""
    rc, out = _run_git(repo, ["rev-parse", "--verify", f"{rev}^{{tree}}"])
    if rc != 0 or not out:
        return None
    return out


# ===== Public API =====


def save(session_id: str, *, repo: Path | str) -> SaveResult:
    """Checkpoint the repo's worktree to refs/aqg-wip/<session_id>.

    - clean worktree → skipped_clean (no ref written)
    - git step failed / timed out → skipped_error (any prior ref left intact)
    - same tree as existing ref → skipped_unchanged (dedup, no churn)
    - else → created (ref points at new dangling commit)
    Never raises; callers (hooks) stay warn-only.
    """
    repo = Path(repo)
    if not SESSION_ID_RE.fullmatch(session_id):
        return SaveResult(action="error", detail="invalid session_id")

    built = _build_checkpoint_commit(repo)
    if built.status == "clean":
        return SaveResult(action="skipped_clean")
    if built.status == "error" or built.commit is None:
        # a git step failed/timed out — do NOT touch any existing ref
        return SaveResult(action="skipped_error", detail="git snapshot failed")
    commit = built.commit

    ref = f"{REF_PREFIX}/{session_id}"
    new_tree = _tree_of(repo, commit)
    old_tree = _tree_of(repo, ref)
    if new_tree is not None and new_tree == old_tree:
        return SaveResult(action="skipped_unchanged", ref=ref)

    rc, _ = _run_git(repo, ["update-ref", ref, commit])
    if rc != 0:
        return SaveResult(action="error", ref=ref, detail="update-ref failed")
    return SaveResult(action="created", ref=ref)


# ===== Recover (SessionStart prompt; never mutates the worktree) =====


def _list_wip_refs(repo: Path) -> list[tuple[str, str]]:
    """[(full_ref, session_id)] under refs/aqg-wip/, with charset-validated id.

    Skips any ref whose trailing component is not a safe session id — defense
    against a hand-crafted refs/aqg-wip/<injection> reaching SessionStart stdout.
    """
    rc, out = _run_git(repo, ["for-each-ref", "--format=%(refname)", REF_PREFIX + "/"])
    if rc != 0 or not out:
        return []
    prefix = REF_PREFIX + "/"
    refs: list[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        sid = line[len(prefix):]
        if SESSION_ID_RE.fullmatch(sid):
            refs.append((line, sid))
    return refs


def _ref_commit_epoch(repo: Path, ref: str) -> int | None:
    rc, out = _run_git(repo, ["log", "-1", "--format=%ct", ref])
    if rc != 0 or not out:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


def _changed_count_vs_head(repo: Path, ref: str) -> int:
    """Count files the checkpoint adds/changes vs HEAD.

    On an unborn repo (no HEAD) `git diff HEAD <ref>` errors — fall back to
    counting the ref's own tree so the prompt never understates new work as 0.
    """
    rc, _ = _run_git(repo, ["rev-parse", "--verify", "HEAD"])
    if rc != 0:  # unborn repo: count the ref tree itself
        rc2, out2 = _run_git(repo, ["ls-tree", "-r", "--name-only", ref])
        if rc2 != 0:
            return 0
        return sum(1 for line in out2.splitlines() if line.strip())
    rc, out = _run_git(repo, ["diff", "--name-only", "HEAD", ref])
    if rc != 0:
        return 0
    return sum(1 for line in out.splitlines() if line.strip())


def _epoch_to_iso(epoch: int | None) -> str:
    if epoch is None:
        return "?"
    try:
        return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return "?"


def _format_recover_message(entries: list[tuple[str, str, str, int]]) -> str:
    """entries = [(session_id, full_ref, saved_iso, changed_count)]. '' if empty.

    Counts + command templates only — NO raw filenames / diff / secrets (this
    text lands in SessionStart context that Claude reads).
    """
    if not entries:
        return ""
    lines = [
        "=== AQG WIP Checkpoint Recovery ===",
        f"Found {len(entries)} uncommitted checkpoint(s) in this repo:",
        "Surface these to the user — do NOT run the commands automatically.",
        "`git checkout` OVERWRITES current worktree files; restore only into a clean worktree.",
        "",
    ]
    for sid, ref, saved_iso, n in entries:
        lines.append(f"- session {sid} (saved {saved_iso})")
        lines.append(f"  changes vs HEAD: {n} file(s)")
        lines.append(f"  preview:  git diff HEAD {ref}")
        lines.append(f"  restore:  git checkout {ref} -- .")
        lines.append(f"  discard:  git update-ref -d {ref}")
    lines.append("")
    lines.append(
        "Checkpoints are local-only (never pushed). Restore into a clean worktree."
    )
    lines.append("===================================")
    return "\n".join(lines)


def recover(
    *,
    repo: Path | str,
    max_age_days: int = 7,
    now: datetime | None = None,
) -> RecoverResult:
    """Surface uncommitted checkpoints; auto-prune old / finalized; never restore.

    A ref is pruned (not shown) when it is older than max_age_days OR its tree
    already equals HEAD's tree (work was finalized into history). Everything
    else is surfaced as a prompt with restore/discard command templates.
    Never raises; SessionStart stays warn-only.
    """
    repo = Path(repo)
    refs = _list_wip_refs(repo)
    if not refs:
        return RecoverResult(shown=0, pruned=0, message="")

    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=max_age_days)).timestamp()
    head_tree = _tree_of(repo, "HEAD")

    shown: list[tuple[str, str, str, int]] = []
    pruned = 0
    for ref, sid in refs:
        epoch = _ref_commit_epoch(repo, ref)
        if epoch is not None and epoch < cutoff:
            _run_git(repo, ["update-ref", "-d", ref])
            pruned += 1
            continue
        ref_tree = _tree_of(repo, ref)
        if ref_tree is not None and ref_tree == head_tree:
            _run_git(repo, ["update-ref", "-d", ref])  # already finalized
            pruned += 1
            continue
        shown.append(
            (sid, ref, _epoch_to_iso(epoch), _changed_count_vs_head(repo, ref))
        )

    return RecoverResult(
        shown=len(shown), pruned=pruned, message=_format_recover_message(shown)
    )


# ===== Hook entry (stdin event → save / recover dispatch) =====

# token-shaped session ids must NOT become ref names (don't echo a leaked token)
_TOKEN_SHAPED_PREFIXES: tuple[str, ...] = (
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_",
    "sk-", "sk-ant-", "AKIA", "ASIA", "xoxb-", "xoxp-", "AIza",
)


def _looks_token_shaped(value: str) -> bool:
    return any(value.startswith(p) for p in _TOKEN_SHAPED_PREFIXES)


def _resolve_session_id(event: dict, *, cwd: Path) -> str:
    """event.session_id if safe + not token-shaped, else stable cwd-hash fallback."""
    raw = event.get("session_id")
    if (
        isinstance(raw, str)
        and SESSION_ID_RE.fullmatch(raw)
        and not _looks_token_shaped(raw)
    ):
        return raw
    try:
        resolved = str(cwd.resolve())
    except (OSError, RuntimeError):
        resolved = str(cwd)
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:12]


def _resolve_cwd(event: dict) -> Path:
    """Pin to the real project dir: AQG_HOOK_PROJECT_DIR > event.cwd > Path.cwd()."""
    env_cwd = os.environ.get("AQG_HOOK_PROJECT_DIR", "").strip()
    if env_cwd:
        try:
            p = Path(env_cwd).expanduser()
            if p.is_dir():
                return p
        except (OSError, ValueError):
            pass
    raw = event.get("cwd")
    if isinstance(raw, str) and raw.strip():
        try:
            p = Path(raw).expanduser()
            if p.is_dir():
                return p
        except (OSError, ValueError):
            pass
    return Path.cwd()


def _read_event(stdin_text: str | None) -> dict:
    if stdin_text is None:
        try:
            stdin_text = sys.stdin.read()
        except Exception:  # aqg: top-level boundary — stdin read must never crash hook
            return {}
    if not stdin_text or not stdin_text.strip():
        return {}
    try:
        parsed = json.loads(stdin_text)
    except (ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def main(argv: list[str] | None = None, *, stdin_text: str | None = None) -> int:
    """Hook entry. ALWAYS returns 0 (never blocks Claude Code).

    save (Stop / PreCompact): NEVER prints stdout (would inject context);
      diagnostics → stderr.
    recover (SessionStart): prints the recovery prompt to stdout for Claude.
    """
    parser = argparse.ArgumentParser(description="AQG WIP checkpoint (git-ref snapshot).")
    parser.add_argument("mode", choices=["save", "recover"])
    parser.add_argument("--max-age-days", type=int, default=7)
    args = parser.parse_args(argv)

    event = _read_event(stdin_text)
    cwd = _resolve_cwd(event)

    if args.mode == "save":
        session_id = _resolve_session_id(event, cwd=cwd)
        result = save(session_id, repo=cwd)
        # NEVER stdout on save (Stop/PreCompact stdout is injected into context)
        print(
            f"[wip_checkpoint] save: {result.action} {result.ref or ''}".rstrip(),
            file=sys.stderr,
        )
        return 0

    # recover
    result = recover(repo=cwd, max_age_days=args.max_age_days)
    if result.message:
        print(result.message)  # SessionStart stdout → Claude context (counts only)
    if result.pruned:
        print(f"[wip_checkpoint] pruned {result.pruned} ref(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
