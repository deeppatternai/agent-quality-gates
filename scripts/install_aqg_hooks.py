#!/usr/bin/env python3
"""AQG Hook installer — merges the managed AQG hook set into Claude Code settings.json.

Behavior:
- Default target: ~/.claude/settings.json (user-level, applies to all projects).
- Use --target=PATH to install into a project-level settings.json.
- Idempotent: re-running --apply does NOT duplicate entries (dedup by script filename).
- Backup: stashes settings.json in the central AQG backup store before any
  modification (legacy adjacent settings.json.aqg-hooks.bak files are migrated in).
- --uninstall: removes only the AQG hook entries we added (leaves other hooks alone).
- --verify: reports current install state without modifying anything.

The managed AQG hooks (key entries; AQG_HOOK_SCRIPTS is the full source of truth):
  PreToolUse(Bash) → pretooluse_bash_skill_validator.sh              [block-on-fail]
  PreToolUse(Edit|Write|MultiEdit) →
    pretooluse_memory_write_guard.sh                                 [block-on-code-signal]
  PostToolUse(Bash) → posttooluse_bash_error_debugging_reminder.sh  [warn]
  PostToolUse(Edit|Write|MultiEdit) →
    posttooluse_skill_edit_reminder.sh                               [warn]
    posttooluse_code_construction_reminder.sh                        [warn]
    posttooluse_test_quality_reminder.sh                             [warn]
    posttooluse_security_review_reminder.sh                          [warn]
  PreCompact / Stop →
    precompact_closeout_reminder.sh                                  [warn]
    wip_checkpoint_save.sh (true code snapshot → refs/aqg-wip)       [warn]
  SessionStart →
    sessionstart_preflight.sh                                        [info+autorun]
    wip_checkpoint_recover.sh (surface unrecovered checkpoints)      [warn]

All hooks silent-skip when AQG_ROOT is unset (Claude Code unaffected).

Exit codes:
    0 success
    1 generic error (target missing, parse error, etc.)
    2 usage error
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional

try:
    from _aqg_backup import BackupSession, migrate_legacy
except ModuleNotFoundError:  # imported as scripts.install_aqg_hooks
    from scripts._aqg_backup import BackupSession, migrate_legacy


EXIT_OK = 0
EXIT_GENERIC = 1
EXIT_USAGE = 2

DEFAULT_TARGET = Path.home() / ".claude" / "settings.json"
BACKUP_SUFFIX = ".aqg-hooks.bak"

# Sentinel: each AQG hook command references one of these script filenames.
# We dedupe and uninstall by exact script filename match in the command string.
AQG_HOOK_SCRIPTS = (
    "pretooluse_bash_skill_validator.sh",
    "pretooluse_memory_write_guard.sh",
    "pretooluse_secret_scan.sh",
    "pretooluse_aqg_tamper_guard.sh",
    "posttooluse_bash_error_debugging_reminder.sh",
    "posttooluse_skill_edit_reminder.sh",
    "posttooluse_code_construction_reminder.sh",
    "posttooluse_test_quality_reminder.sh",
    "posttooluse_security_review_reminder.sh",
    "precompact_closeout_reminder.sh",
    "sessionstart_preflight.sh",
    "sessionstart_update_check.sh",
    "userpromptsubmit_handoff_mandate.sh",
    "wip_checkpoint_save.sh",
    "wip_checkpoint_recover.sh",
)

# Which hook scripts BLOCK (omit the warn-only ` || true` tail) vs warn-only. The
# single explicit source of truth: runtime_cmd derives the tail from membership here,
# and test_aqg_hooks.py asserts BOTH the installer commands AND the example JSON's
# `_blocking` markers conform to it — instead of reverse-engineering blocking from the
# ` || true` tail (audit 5c7ba27f follow-up + e027d631).
#
# Blocking is a PER-SCRIPT property: a script that must block in one hook event blocks
# in ALL events it is wired to (no per-call-site override — a future per-event need
# would require a design change). To add a blocking gate: add the script to
# AQG_HOOK_SCRIPTS, add it here, and mark `"_blocking": true` on its hook entry in
# agent-packs/claude-code/hooks/settings.blocking.example.json (the test pins all three).
_AQG_BLOCKING_HOOK_SCRIPTS = frozenset({
    "pretooluse_bash_skill_validator.sh",
    "pretooluse_memory_write_guard.sh",
    "pretooluse_secret_scan.sh",
    # D3 (LOG.md 2026-07-10, supersedes the opt-in stance): the tamper-guard is now
    # default-installed + blocking. Threat model = cross-project blast-radius (an
    # agent in ANOTHER repo neutering an AQG gate file), NOT agent self-defense
    # (a shell-capable agent can always write its own env). See the hook header.
    "pretooluse_aqg_tamper_guard.sh",
})
# Deliberately a raise, NOT an `assert` (audit e027d631 gpt-5.5 #1): a security gate
# silently downgraded to warn-only — a typo'd name → membership miss → ` || true`
# appended — must fail loudly even under `python -O` / PYTHONOPTIMIZE.
if not _AQG_BLOCKING_HOOK_SCRIPTS <= set(AQG_HOOK_SCRIPTS):
    raise RuntimeError(
        "blocking hook scripts must be a subset of AQG_HOOK_SCRIPTS: "
        f"{sorted(_AQG_BLOCKING_HOOK_SCRIPTS - set(AQG_HOOK_SCRIPTS))}"
    )


def _resolve_aqg_root(arg_root: Optional[str]) -> Optional[Path]:
    """Resolve AQG repo root via arg > env > sentinel walk-up from this script."""
    if arg_root:
        p = Path(arg_root).resolve()
        if (p / "VERSION").is_file():
            return p
        return None
    env_root = os.environ.get("AQG_ROOT")
    if env_root:
        p = Path(env_root).resolve()
        if (p / "VERSION").is_file():
            return p
    # Walk up from this script.
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "VERSION").is_file() and (p / "scripts").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _aqg_hook_specs(aqg_root: Path) -> dict:
    """Build the canonical AQG hooks dict (event → list of matcher blocks).

    Each command is shaped:
        if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; bash "$AQG_ROOT/agent-packs/claude-code/hooks/<script>.sh" [extra args]

    The aqg_root parameter is intentionally NOT baked into the command string
    (we use $AQG_ROOT at runtime so users can swap checkouts without re-running
    install). aqg_root IS used at install time only for sanity-check (script files
    must exist before we wire them in).
    """
    hooks_dir = aqg_root / "agent-packs" / "claude-code" / "hooks"
    for s in AQG_HOOK_SCRIPTS:
        p = hooks_dir / s
        if not p.is_file():
            raise FileNotFoundError(f"hook script missing: {p}")

    def runtime_cmd(script: str, *, pass_project_dir_arg: bool = False) -> str:
        # Single-line shell so the JSON value stays one string (Claude Code spec).
        # `CLAUDE_PROJECT_DIR=...` forwarding is explicit (redundant runtime — env is
        # inherited — but satisfies validate_agent_pack rule and makes the command
        # self-documenting). Trailing ` || true` for warn-only hooks ensures
        # never-block; blocking gates omit it so non-zero exit propagates. Whether a
        # script blocks is its membership in _AQG_BLOCKING_HOOK_SCRIPTS (module top) —
        # the single explicit source of truth, not a per-call flag — so the test reads
        # blocking from there instead of re-parsing the ` || true` tail.
        arg = ' "${CLAUDE_PROJECT_DIR:-}"' if pass_project_dir_arg else ""
        tail = "" if script in _AQG_BLOCKING_HOOK_SCRIPTS else " || true"
        return (
            'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
            f'CLAUDE_PROJECT_DIR="${{CLAUDE_PROJECT_DIR:-}}" bash "$AQG_ROOT/agent-packs/claude-code/hooks/{script}"{arg}{tail}'
        )

    return {
        "PreToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("pretooluse_bash_skill_validator.sh")}
                ],
            },
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("pretooluse_memory_write_guard.sh")}
                ],
            },
            {
                "matcher": "Bash|Edit|Write|MultiEdit|NotebookEdit",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("pretooluse_secret_scan.sh")}
                ],
            },
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("pretooluse_aqg_tamper_guard.sh")}
                ],
            },
        ],
        "PostToolUse": [
            {
                "matcher": "Bash",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("posttooluse_bash_error_debugging_reminder.sh")},
                ],
            },
            {
                "matcher": "Bash|Edit|Write|MultiEdit",
                # Its own entry, and the ONLY reminder mounted on Bash. A Bash heredoc
                # writes files and carries no file_path, which is the defect this closes;
                # but the edit-tool entry below registers three other reminders that all
                # read file_path, so widening THAT entry would invoke them on every shell
                # call for them to exit immediately (aud_oqv59VQ00_EhRJkU opus-f6). One
                # script, one canonical matcher -- mounting it twice makes the installer
                # warn about double-fire.
                "hooks": [
                    {"type": "command", "command": runtime_cmd("posttooluse_code_construction_reminder.sh")},
                ],
            },
            {
                "matcher": "Edit|Write|MultiEdit",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("posttooluse_skill_edit_reminder.sh")},
                    {"type": "command", "command": runtime_cmd("posttooluse_test_quality_reminder.sh")},
                    {"type": "command", "command": runtime_cmd("posttooluse_security_review_reminder.sh")},
                ],
            },
        ],
        "PreCompact": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("precompact_closeout_reminder.sh")},
                    {"type": "command", "command": runtime_cmd("wip_checkpoint_save.sh", pass_project_dir_arg=True)},
                ],
            }
        ],
        "Stop": [
            {
                "matcher": "",
                # `precompact_closeout_reminder.sh` is deliberately NOT here.
                # Stop fires at the end of EVERY assistant turn, and that hook
                # emits its 14-line closeout checklist unconditionally, with no
                # dedup — so mounting it here repeated "have you answered the six
                # closeout questions?" after every reply. The six questions are a
                # COMPLETION review; asking them every turn is the same failure
                # this repo's audit-trigger work removed elsewhere: a per-turn
                # reminder drowning out the principle it is trying to convey.
                # PreCompact keeps it, which is the trigger that actually means
                # "context is about to be lost, leave a handoff".
                "hooks": [
                    {"type": "command", "command": runtime_cmd("wip_checkpoint_save.sh", pass_project_dir_arg=True)},
                ],
            }
        ],
        "SessionStart": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("sessionstart_preflight.sh", pass_project_dir_arg=True)},
                    {"type": "command", "command": runtime_cmd("wip_checkpoint_recover.sh", pass_project_dir_arg=True)},
                    # Managed update check. A trigger only: it starts a detached
                    # process and returns, so no session waits on a network round
                    # trip, and it is deliberately NOT in the blocking set — a
                    # slow remote must never be able to stop a session starting.
                    {"type": "command", "command": runtime_cmd("sessionstart_update_check.sh")},
                ],
            }
        ],
        "UserPromptSubmit": [
            {
                "matcher": "",
                "hooks": [
                    {"type": "command", "command": runtime_cmd("userpromptsubmit_handoff_mandate.sh")}
                ],
            }
        ],
    }


# The canonical AQG command embeds this path fragment. Matching the FULL fragment
# (not a bare-basename substring) avoids treating an unrelated user hook that merely
# mentions the filename as AQG-owned — which would skip it on install or, worse,
# REMOVE it on uninstall (audit 48a01f42 gpt-f5; workflow C5).
_AQG_HOOK_PATH_FRAGMENT = "agent-packs/claude-code/hooks/"


def _command_references_aqg_script(cmd: str) -> Optional[str]:
    """Return the AQG script name referenced by cmd via the canonical AQG hook
    path, or None. Requires the full path fragment, not a bare-basename substring."""
    if not isinstance(cmd, str):
        return None
    for s in AQG_HOOK_SCRIPTS:
        if _AQG_HOOK_PATH_FRAGMENT + s in cmd:
            return s
    return None


def _norm_cmd(cmd: str) -> str:
    """Whitespace-normalized command for the C6 divergence check.

    The canonical AQG command is path-INDEPENDENT (it embeds the literal
    `$AQG_ROOT`, never a resolved checkout path — see _aqg_hook_specs), so two
    installs of the SAME tool version produce byte-identical commands. Collapsing
    runs of whitespace (and trimming) means only a real token change — an old
    version's flags / `|| true` tail / args — registers as divergence, while
    cosmetic reformatting does not. This is why C6 needs no path canonicalization
    (the ledger's feared false-positive source): there is no machine path to
    normalize away."""
    return " ".join(cmd.split()) if isinstance(cmd, str) else ""


def _load_settings(target: Path) -> dict:
    if not target.exists():
        return {}
    try:
        text = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"ERROR: cannot read {target}: {exc}")
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: {target} is not valid JSON: {exc}")
    # Valid JSON but wrong shape (a list / string / number at top level) would
    # later crash settings.get(...) with AttributeError; fail with a clean message
    # instead (audit 48a01f42 gpt-f3 + gemini-f2; workflow C3).
    if not isinstance(data, dict):
        raise SystemExit(
            f"ERROR: {target} top-level must be a JSON object, got "
            f"{type(data).__name__}; refusing to operate on a malformed settings file"
        )
    return data


def _migrate_legacy_adjacent(
    target: Path, *, client: str, installer: str, aqg_root: Optional[Path]
) -> None:
    """Fold any pre-central adjacent ``<target>.aqg-hooks.bak[.N]`` into the central
    store, then delete the in-place copies (idempotent; symlinked legacy is skipped
    rather than followed). Runs only when we are about to back up — never on a
    read-only --verify."""
    pattern = target.name + BACKUP_SUFFIX + "*"
    items = [
        (p, "_legacy/" + p.name)
        for p in sorted(target.parent.glob(pattern))
        if p.is_file() and not p.is_symlink()
    ]
    if items:
        migrate_legacy(
            client, target.parent, items, scope="user", installer=installer, aqg_root=aqg_root
        )


def _backup(
    target: Path,
    *,
    client: str = "claude-code",
    installer: str = "install_aqg_hooks.py",
    aqg_root: Optional[Path] = None,
) -> Optional[Path]:
    """Back up ``target`` into the centralized store (``<base>/<client>/user/<run>/``)
    and return the backup path, or ``None`` if the target is absent.

    Shared by the Codex hook installer (``client="codex"``). Never-clobber and
    symlink refusal live in the store; each call is one self-contained run."""
    if not target.exists():
        return None
    _migrate_legacy_adjacent(target, client=client, installer=installer, aqg_root=aqg_root)
    session = BackupSession(
        client, target.parent, scope="user", installer=installer, aqg_root=aqg_root
    )
    dest = session.backup(target)
    session.close()
    session.gc()
    return dest


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text atomically (tempfile in the same dir → fsync → os.replace), so an
    interrupt / crash / concurrent run can never leave the user's settings.json
    truncated or half-written (audit 48a01f42 gpt-f1 + gemini-f1; workflow C1).
    Refuses a symlink target (no follow-symlink write). Mirrors
    install_pre_commit._atomic_write_text."""
    if path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")
    fd, tmp = tempfile.mkstemp(prefix=".tmp-aqg-settings-", suffix=".json", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _write_settings(target: Path, data: dict) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps FIRST so a serialization error never truncates the live file, then
    # write atomically (C1).
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    _atomic_write_text(target, payload)


def _merge_hooks(existing_hooks: dict, aqg_specs: dict) -> tuple[dict, list[str], list[str], int]:
    """Merge AQG hook specs into existing hooks.

    Audit gpt-5.5 #4 fix: dedup uses `(matcher, script)` tuple, not script-only.
    If a v0.8.0 script appears under a non-canonical matcher (e.g., user wired
    `Edit|Write` instead of `Edit|Write|MultiEdit`), the canonical entry is
    still installed and the stale entry is reported via `stale_warnings`.

    Audit gpt-5.5 #6 fix: returns a change count so caller can no-op (no backup,
    no write) when nothing was actually mutated. `skipped_log` is informational.

    C6: a hook present at the canonical (matcher, script) but with a divergent
    command is converged to canonical in place (AQG-owned), logged in the change
    log, and counted — so `--apply` delivers command-format upgrades to existing
    installs instead of silently skipping them.

    Returns:
        (merged_hooks, change_log, skipped_log, change_count)
        where change_count = added + updated, and change_log folds in the
        add / update / stale-matcher lines.
    """
    added_log: list[str] = []
    skipped_log: list[str] = []
    stale_warnings: list[str] = []
    updated_log: list[str] = []  # C6: AQG-owned commands converged to canonical
    added_count = 0
    updated_count = 0
    # deepcopy so we NEVER mutate the caller's loaded settings in place — a later
    # write failure must not leave the in-memory settings half-mutated (C2 — audit
    # 48a01f42 gpt-f2 + gemini-f5). Guard non-dict (C3 — defensive).
    merged = copy.deepcopy(existing_hooks) if isinstance(existing_hooks, dict) else {}

    for event, aqg_blocks in aqg_specs.items():
        raw_blocks = merged.get(event, [])
        existing_blocks = list(raw_blocks) if isinstance(raw_blocks, list) else []

        # Build map of (matcher, script) -> LIST of the wired hook DICTS (C6: we
        # keep the objects, not just the command string, so a divergent command
        # can be converged in place; list-valued so a degenerate config with
        # duplicate same-(matcher, script) hooks converges ALL of them — keeping
        # one would let --apply (no-op) and verify (warns over every entry)
        # disagree forever on the stale duplicate). Each access is shape-guarded
        # (C3): a hand-edited settings.json may carry a non-dict block, a non-list
        # hooks slot, or a non-dict hook entry.
        present: dict[tuple[str, str], list[dict]] = {}
        script_to_matchers: dict[str, list[str]] = {}
        for blk in existing_blocks:
            if not isinstance(blk, dict):
                continue
            blk_matcher = blk.get("matcher", "")
            blk_hooks = blk.get("hooks", [])
            for hook in (blk_hooks if isinstance(blk_hooks, list) else []):
                if not isinstance(hook, dict):
                    continue
                ref = _command_references_aqg_script(hook.get("command", ""))
                if ref is not None:
                    present.setdefault((blk_matcher, ref), []).append(hook)
                    script_to_matchers.setdefault(ref, []).append(blk_matcher)

        # For each v0.8.0 block, install commands missing under (matcher, script).
        for aqg_blk in aqg_blocks:
            aqg_matcher = aqg_blk.get("matcher", "")
            new_hook_entries = []
            for hook in aqg_blk.get("hooks", []) or []:
                ref = _command_references_aqg_script(hook.get("command", ""))
                key = (aqg_matcher, ref) if ref else None
                if ref and key in present:
                    # Exact (canonical matcher, script) present. C6: a true no-op
                    # ONLY if the installed command also matches canonical. A hook
                    # wired at the right matcher but carrying a stale or hand-edited
                    # command (e.g. an older version's flags) was previously reported
                    # as fully installed and skipped — so an existing install could
                    # never receive a command-format upgrade via --apply. Converge
                    # the AQG-owned hook to canonical in place (cmd_apply backs the
                    # file up first); `verify` separately WARNs without mutating.
                    # Whitespace-normalized compare: the canonical command embeds the
                    # literal $AQG_ROOT (path-independent), so same-version installs
                    # are byte-identical — only a real token change registers. Every
                    # AQG-owned hook at this (matcher, script) is converged, so a
                    # stale duplicate cannot survive (apply/verify stay in agreement).
                    canonical_command = hook.get("command", "")
                    converged = False
                    for existing_hook in present[key]:
                        if _norm_cmd(existing_hook.get("command", "")) != _norm_cmd(canonical_command):
                            existing_hook["command"] = canonical_command
                            converged = True
                    if converged:
                        updated_log.append(
                            f"updated {event}/'{aqg_matcher}' → {ref} command to canonical (was stale/hand-edited)"
                        )
                        updated_count += 1
                    else:
                        skipped_log.append(f"skip {event}/'{aqg_matcher}' — {ref} already wired at canonical matcher")
                    # #223: the canonical entry is present, but the SAME script may
                    # also linger under a non-canonical (stale) matcher. The `continue`
                    # below skips the canonical-absent stale-matcher scan further down,
                    # so without this --apply stays silent (change_count can be 0) while
                    # --verify independently reports STALE — a permanent apply/verify
                    # disagreement (#223). Emit the warning here too.
                    stale_others = sorted(
                        m for m in set(script_to_matchers.get(ref, [])) if m != aqg_matcher
                    )
                    if stale_others:
                        stale_warnings.append(
                            f"WARN: {event}/{ref} also present under non-canonical matcher(s) "
                            f"{stale_others!r} while canonical '{aqg_matcher}' is already wired; "
                            "manually remove the stale entry to avoid double-fire"
                        )
                    continue
                if ref and ref in script_to_matchers:
                    # Script present but under a different matcher → stale install.
                    other_matchers = [m for m in script_to_matchers[ref] if m != aqg_matcher]
                    if other_matchers:
                        stale_warnings.append(
                            f"WARN: {event}/{ref} present under matcher(s) {other_matchers!r} "
                            f"but canonical matcher is '{aqg_matcher}'; adding canonical entry alongside "
                            "(manually remove the stale entry if no longer needed)"
                        )
                new_hook_entries.append(hook)
                if ref:
                    present.setdefault((aqg_matcher, ref), []).append(hook)
                    script_to_matchers.setdefault(ref, []).append(aqg_matcher)

            if not new_hook_entries:
                continue

            # Try to merge into an existing block with same matcher; else append new
            # block. `merged` is a deepcopy, so mutating blk here is safe (C2).
            merged_into_existing = False
            for blk in existing_blocks:
                if isinstance(blk, dict) and blk.get("matcher", "") == aqg_matcher:
                    blk_hooks = blk.get("hooks")
                    if not isinstance(blk_hooks, list):
                        blk_hooks = []
                        blk["hooks"] = blk_hooks
                    blk_hooks.extend(new_hook_entries)
                    added_log.append(
                        f"appended {len(new_hook_entries)} hook(s) into existing {event}/'{aqg_matcher}' block"
                    )
                    added_count += len(new_hook_entries)
                    merged_into_existing = True
                    break

            if not merged_into_existing:
                existing_blocks.append({"matcher": aqg_matcher, "hooks": new_hook_entries})
                added_log.append(
                    f"created new {event}/'{aqg_matcher}' block with {len(new_hook_entries)} hook(s)"
                )
                added_count += len(new_hook_entries)

        merged[event] = existing_blocks

    # C6: updated_log (in-place command convergence) joins the change log; the
    # 4th value is the TOTAL change count (adds + updates) so cmd_apply writes +
    # surfaces the result whenever anything changed — not only on new entries.
    return merged, added_log + updated_log + stale_warnings, skipped_log, added_count + updated_count


def _strip_aqg_hooks(hooks: dict) -> tuple[dict, list[str]]:
    """Remove only v0.8.0 entries. Returns (stripped, change_log)."""
    log: list[str] = []
    stripped: dict = {}
    for event, blocks in hooks.items():
        # Preserve a malformed event value / block / hook untouched rather than
        # crashing — uninstall must never destroy what it can't parse (C3).
        if not isinstance(blocks, list):
            stripped[event] = blocks
            continue
        new_blocks = []
        for blk in blocks:
            if not isinstance(blk, dict):
                new_blocks.append(blk)
                continue
            blk_hooks = blk.get("hooks", [])
            new_hook_entries = []
            for hook in (blk_hooks if isinstance(blk_hooks, list) else []):
                if not isinstance(hook, dict):
                    new_hook_entries.append(hook)
                    continue
                ref = _command_references_aqg_script(hook.get("command", ""))
                if ref:
                    log.append(f"removed {event}/'{blk.get('matcher','')}' command referencing {ref}")
                    continue
                new_hook_entries.append(hook)
            if new_hook_entries:
                new_blk = dict(blk)
                new_blk["hooks"] = new_hook_entries
                new_blocks.append(new_blk)
            elif not blk.get("hooks"):
                new_blocks.append(blk)
        if new_blocks:
            stripped[event] = new_blocks
        # else: empty event, drop
    return stripped, log


def ensure_env_root(settings: dict, aqg_root: Path) -> str | None:
    """Make AQG_ROOT reachable by the hooks, via settings.json's ``env`` block.

    Returns a one-line change description, or ``None`` when nothing needed doing.

    Why here and not the user's shell profile: every hook command is gated on
    ``AQG_ROOT`` and the installer only ever PRINTED a reminder to export it, so a
    user who missed that line ran with every hook silently disabled — the
    PreToolUse secret scan included. ``settings.json`` is a file this installer
    already owns, and the host passes its ``env`` block to hook processes.
    Swappability (the reason the path is not baked into the command) survives:
    repoint this one key, or re-run --apply from the other checkout.

    An AQG_ROOT already pointing somewhere else is REPORTED, never overwritten —
    a user running two checkouts chose that value, and clobbering it would silently
    re-aim every hook at a checkout they did not pick.
    """
    resolved = str(aqg_root)
    env = settings.get("env")
    if env is None:
        settings["env"] = {"AQG_ROOT": resolved}
        return f"added env.AQG_ROOT={resolved}"
    if not isinstance(env, dict):
        return (
            f"malformed env block ({type(env).__name__}, expected object); left as is — "
            "hooks stay gated until it is repaired"
        )
    current = env.get("AQG_ROOT")
    if isinstance(current, str) and current.strip():
        if current == resolved:
            return None
        return (
            f"env.AQG_ROOT differs from this checkout (set: {current}); left as is — "
            "repoint it by hand if that is not deliberate"
        )
    env["AQG_ROOT"] = resolved
    return f"set env.AQG_ROOT={resolved}"


def clear_env_root(settings: dict, aqg_root: Path) -> str | None:
    """Undo what :func:`ensure_env_root` wrote. Mirror of its rules.

    Returns a one-line change description, or ``None`` when nothing needed doing.

    Uninstall used to strip the hooks and leave this key, so the obvious reset —
    uninstall, then reinstall — silently skipped the env write entirely: the second
    install saw a correct value and no-opped. The residue is inert once no hook
    reads it, but it breaks the path people actually use to verify an install.

    A value pointing somewhere ELSE is kept and reported, for the same reason
    ``ensure_env_root`` refuses to overwrite one: it was chosen by someone, not
    written by us. An ``env`` block we emptied is dropped whole — leaving ``{}``
    behind is residue too.
    """
    env = settings.get("env")
    if not isinstance(env, dict) or "AQG_ROOT" not in env:
        return None
    current = env.get("AQG_ROOT")
    resolved = str(aqg_root)
    if not isinstance(current, str) or current.strip() != resolved:
        return (
            f"kept env.AQG_ROOT (set: {current}); it does not point at this checkout, "
            "so it was not ours to remove"
        )
    del env["AQG_ROOT"]
    if not env:
        del settings["env"]
    return f"removed env.AQG_ROOT={resolved}"


def cmd_apply(target: Path, aqg_root: Path) -> int:
    try:
        aqg = _aqg_hook_specs(aqg_root)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_GENERIC

    settings = _load_settings(target)
    hooks_val = settings.get("hooks", {})
    if hooks_val and not isinstance(hooks_val, dict):
        print(
            f"ERROR: {target} 'hooks' must be a JSON object, got "
            f"{type(hooks_val).__name__}; refusing to overwrite a malformed hooks block",
            file=sys.stderr,
        )
        return EXIT_GENERIC
    existing_hooks = hooks_val or {}
    # change_log folds add + C6-update + stale-matcher lines; change_count = adds
    # + updates (an in-place command convergence is a real change even with 0 adds).
    merged, change_log, skipped_log, change_count = _merge_hooks(existing_hooks, aqg)

    # Audit gpt-5.5 #6 fix: true no-op when nothing changed (no backup, no write).
    # C6: a stale-command convergence sets change_count > 0, so it is NOT treated
    # as a no-op — the update is written + surfaced, not silently dropped here.
    if change_count == 0:
        # The env repair must NOT ride on the hook-change path: a user who installed
        # hooks before env.AQG_ROOT existed lands here every time, and would stay
        # silently gated forever (see doctor's hook_env_guard check).
        env_change = ensure_env_root(settings, aqg_root)
        if env_change is not None and "AQG_ROOT=" in env_change:
            bak = _backup(target, aqg_root=aqg_root)
            _write_settings(target, settings)
            if bak:
                print(f"OK: backup at {bak}")
            print(f"OK: {env_change} in {target}")
        elif env_change is not None:
            print(f"NOTE: {env_change}", file=sys.stderr)
        # #223: change_count counts only adds+updates, but change_log may still
        # carry stale-matcher WARN lines (same script wired at canonical AND a
        # non-canonical matcher). Surface them so --apply agrees with --verify
        # instead of printing a bare "already fully installed".
        if change_log:
            print(f"OK: AQG hooks installed in {target}; no add/update, but found stale-matcher warning(s):")
            for line in change_log:
                print(f"  - {line}")
            print("  (manually remove the stale entry, or re-run --apply after cleanup)")
        else:
            print(f"OK: AQG hooks already fully installed in {target} (no changes)")
        if skipped_log:
            for line in skipped_log:
                print(f"  - {line}")
        return EXIT_OK

    bak = _backup(target, aqg_root=aqg_root)
    settings["hooks"] = merged
    env_change = ensure_env_root(settings, aqg_root)
    _write_settings(target, settings)

    if bak:
        print(f"OK: backup at {bak}")
    print(f"OK: wrote {target}")
    if env_change is not None:
        print(f"OK: {env_change}" if "AQG_ROOT=" in env_change else f"NOTE: {env_change}")
    print(f"Changes ({change_count}):")
    for line in change_log:
        print(f"  - {line}")
    if skipped_log:
        print("Already wired (no change):")
        for line in skipped_log:
            print(f"  - {line}")
    print()
    print("Next:")
    # Only claim it when it is true: ensure_env_root REFUSES on a foreign or
    # malformed value, and printing "wired" there sends the user away from the one
    # thing still blocking every hook (audit aud_7nsuQouSm96_9rt9).
    if env_change is None or "AQG_ROOT=" in env_change:
        print("  1. AQG_ROOT is wired into settings.json env (hooks read it from there)")
    else:
        print(f"  1. ACTION NEEDED — AQG_ROOT is NOT wired: {env_change}")
        print("     Until it is, every AQG hook silently no-ops (secret scan included).")
    print("  2. Restart any running Claude Code session for hooks to load")
    print("  3. Verify: python3 scripts/install_aqg_hooks.py --verify")
    return EXIT_OK


def _write_env_only(target: Path, settings: dict, aqg_root: Path, change: str) -> None:
    """Persist an env-only cleanup on a path that would otherwise return early."""
    bak = _backup(target, aqg_root=aqg_root)
    _write_settings(target, settings)
    if bak:
        print(f"OK: backup at {bak}")
    print(f"OK: {change} in {target}")


def cmd_uninstall(target: Path, aqg_root: Path) -> int:
    settings = _load_settings(target)
    hooks_val = settings.get("hooks")
    if not hooks_val:
        # The env cleanup must NOT ride on the hooks path: someone who ran the old
        # uninstall, or removed the hooks by hand, lands here and would keep the
        # stale key forever — the same early-return trap cmd_apply had.
        change = clear_env_root(settings, aqg_root)
        if change is not None and change.startswith("removed"):
            _write_env_only(target, settings, aqg_root, change)
        elif change is not None:
            print(f"NOTE: {change}", file=sys.stderr)
        print(f"INFO: no hooks in {target} (no-op)")
        return EXIT_OK
    if not isinstance(hooks_val, dict):
        print(
            f"ERROR: {target} 'hooks' is not a JSON object ({type(hooks_val).__name__}); "
            "refusing to modify a malformed hooks block",
            file=sys.stderr,
        )
        return EXIT_GENERIC
    stripped, log = _strip_aqg_hooks(hooks_val)
    if not log:
        change = clear_env_root(settings, aqg_root)
        if change is not None and change.startswith("removed"):
            _write_env_only(target, settings, aqg_root, change)
        elif change is not None:
            print(f"NOTE: {change}", file=sys.stderr)
        print(f"OK: no AQG hooks present in {target} (no-op)")
        return EXIT_OK
    env_change = clear_env_root(settings, aqg_root)
    bak = _backup(target, aqg_root=aqg_root)
    if stripped:
        settings["hooks"] = stripped
    else:
        del settings["hooks"]
    _write_settings(target, settings)
    if bak:
        print(f"OK: backup at {bak}")
    print(f"OK: wrote {target}")
    print("Removed:")
    for line in log:
        print(f"  - {line}")
    if env_change is not None:
        print(f"  - {env_change}" if env_change.startswith("removed") else f"NOTE: {env_change}")
    return EXIT_OK


def cmd_verify(target: Path, aqg_root: Optional[Path]) -> int:
    settings = _load_settings(target)
    hooks = settings.get("hooks", {}) or {}
    if not isinstance(hooks, dict):
        hooks = {}  # read-only diagnostics: a malformed hooks block reports as none (C3)
    print(f"target: {target}")
    print(f"target_exists: {target.exists()}")
    print(f"aqg_root: {aqg_root if aqg_root else '(unresolved)'}")
    print(f"AQG_ROOT env: {os.environ.get('AQG_ROOT', '(unset; hooks will silent-skip)')}")
    print()
    found: dict[str, list[tuple[str, str, str]]] = {}  # (matcher, script, command)
    for event, blocks in hooks.items():
        for blk in blocks:
            for hook in blk.get("hooks", []) or []:
                cmd = hook.get("command", "")
                ref = _command_references_aqg_script(cmd)
                if ref:
                    found.setdefault(event, []).append((blk.get("matcher", ""), ref, cmd))

    if not found:
        print("AQG hooks: NOT INSTALLED")
        print()
        print("Run: python3 scripts/install_aqg_hooks.py --apply")
        return EXIT_OK

    # Audit gpt-5.5 #4 fix: canonical (event, matcher) check; report stale matchers.
    if aqg_root:
        try:
            canonical = _aqg_hook_specs(aqg_root)
        except FileNotFoundError:
            canonical = {}
    else:
        canonical = {}
    canonical_pairs: set[tuple[str, str, str]] = set()  # (event, matcher, script)
    # (event, matcher, script) -> canonical command. Assumes one hook per identity
    # (true for the spec — no event/matcher repeats a script); see _merge_hooks.
    canonical_cmd: dict[tuple[str, str, str], str] = {}
    for event, blocks in canonical.items():
        for blk in blocks:
            matcher = blk.get("matcher", "")
            for hook in blk.get("hooks", []) or []:
                cmd = hook.get("command", "")
                ref = _command_references_aqg_script(cmd)
                if ref:
                    canonical_pairs.add((event, matcher, ref))
                    canonical_cmd[(event, matcher, ref)] = cmd

    print("AQG hooks installed:")
    stale_count = 0
    cmd_stale_count = 0
    for event in ("PreToolUse", "PostToolUse", "PreCompact", "Stop", "SessionStart", "UserPromptSubmit"):
        entries = found.get(event, [])
        if entries:
            for matcher, script, cmd in entries:
                if canonical_pairs and (event, matcher, script) not in canonical_pairs:
                    canonical_matcher = next(
                        (m for (e, m, s) in canonical_pairs if e == event and s == script),
                        "(canonical not found)",
                    )
                    print(f"  - {event}/'{matcher}' → {script}  STALE: canonical matcher is '{canonical_matcher}'")
                    stale_count += 1
                else:
                    # C6: right matcher + right script — but is the COMMAND canonical?
                    canon = canonical_cmd.get((event, matcher, script))
                    if canon is not None and _norm_cmd(cmd) != _norm_cmd(canon):
                        print(f"  - {event}/'{matcher}' → {script}  COMMAND STALE: command differs from canonical")
                        cmd_stale_count += 1
                    else:
                        print(f"  - {event}/'{matcher}' → {script}")
        else:
            if canonical_pairs and any(e == event for (e, _, _) in canonical_pairs):
                print(f"  - {event}: (none — partial install; --apply to add missing)")

    if stale_count:
        print()
        print(f"WARN: {stale_count} stale entry/entries detected. --apply will install canonical alongside; "
              "manually remove stale to avoid double-fire.")
    if cmd_stale_count:
        print()
        print(f"WARN: {cmd_stale_count} entry/entries wired at the canonical matcher carry a command that "
              "differs from canonical (stale or hand-edited). Run --apply to update them to canonical.")
    return EXIT_OK


def inspect_install(target: Path, aqg_root: Path | None) -> tuple[str, str]:
    """Return ``(missing|complete|stale|invalid, detail)`` without printing."""
    if aqg_root is None:
        return "invalid", "AQG root is unresolved"
    try:
        settings = _load_settings(target)
    except (Exception, SystemExit) as exc:  # aqg: top-level boundary
        return "invalid", str(exc) or type(exc).__name__
    hooks = settings.get("hooks", {}) or {}
    if not isinstance(hooks, dict):
        return "invalid", f"{target} 'hooks' must be a JSON object"

    found: dict[tuple[str, str, str], str] = {}
    for event, blocks in hooks.items():
        if not isinstance(blocks, list):
            continue
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            matcher = blk.get("matcher", "")
            for hook in (blk.get("hooks") or []):
                if not isinstance(hook, dict):
                    continue
                cmd = hook.get("command", "")
                ref = _command_references_aqg_script(cmd)
                if ref:
                    found[(event, matcher, ref)] = cmd
    if not found:
        return "missing", f"no AQG Claude Code hooks in {target}"

    try:
        canonical = _aqg_hook_specs(aqg_root)
    except (OSError, FileNotFoundError) as exc:
        return "invalid", str(exc)

    canonical_cmd: dict[tuple[str, str, str], str] = {}
    for event, blocks in canonical.items():
        for blk in blocks:
            matcher = blk.get("matcher", "")
            for hook in blk.get("hooks", []) or []:
                cmd = hook.get("command", "")
                ref = _command_references_aqg_script(cmd)
                if ref:
                    canonical_cmd[(event, matcher, ref)] = cmd

    missing = sorted(set(canonical_cmd) - set(found), key=repr)
    extra = sorted(set(found) - set(canonical_cmd), key=repr)
    if missing:
        return "stale", f"partial install; missing {len(missing)} managed hook definition(s)"
    if extra:
        return "stale", f"non-canonical managed hook definition(s) present: {len(extra)}"
    stale_commands = [
        identity
        for identity, cmd in found.items()
        if _norm_cmd(cmd) != _norm_cmd(canonical_cmd[identity])
    ]
    if stale_commands:
        return "stale", f"{len(stale_commands)} managed hook command(s) differ from canonical"

    scripts = {script for _, _, script in found}
    return "complete", f"{len(scripts)} managed scripts across {len(found)} hook definitions"


def cmd_is_installed(target: Path) -> int:
    """Exit 0 iff settings.json carries >=1 MANAGED AQG hook (a script in
    AQG_HOOK_SCRIPTS) — i.e. the machine has opted into the managed hook set; else 1.
    Detection only — never writes.

    Managed AQG hooks are ALL-OR-NOTHING: `--apply` installs the full canonical set,
    `--uninstall` removes all; there is no subset interface. So "opted in" == >=1
    managed hook present, and upgrade.sh refreshes such a machine UP to the current
    canonical set (picking up newly-shipped hooks). `run_warn_only.sh` is deliberately
    NOT in AQG_HOOK_SCRIPTS, so a warn-only-only opt-in returns 1 (untouched) — this
    is what prevents the cf5adc7f mis-promotion of warn-only users to the full
    blocking set.

    Always returns a controlled exit code: `_load_settings` raises SystemExit on a
    malformed / unreadable settings file, so SystemExit is caught here too (it is a
    BaseException, NOT an Exception) and mapped to 1 — treat as not-installed so the
    caller leaves it alone, without leaking _load_settings' ERROR (audit f2)."""
    try:
        settings = _load_settings(target)
        hooks = settings.get("hooks", {})
        if not isinstance(hooks, dict):
            return EXIT_GENERIC
        for blocks in hooks.values():
            if not isinstance(blocks, list):
                continue
            for blk in blocks:
                if not isinstance(blk, dict):
                    continue
                for hook in (blk.get("hooks") or []):
                    if isinstance(hook, dict) and _command_references_aqg_script(hook.get("command", "")):
                        return EXIT_OK
        return EXIT_GENERIC
    except (Exception, SystemExit):
        # SystemExit (BaseException) covers _load_settings' raise on malformed/
        # unreadable settings; Exception covers anything else. Detection mode must
        # never propagate — unknown state → not-installed → caller leaves it alone.
        return EXIT_GENERIC


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="install_aqg_hooks",
        description="Install/uninstall/verify AQG Claude Code hooks",
    )
    parser.add_argument(
        "--target",
        default=str(DEFAULT_TARGET),
        help=f"settings.json path (default: {DEFAULT_TARGET})",
    )
    parser.add_argument(
        "--aqg-root",
        default=None,
        help="AQG checkout path (default: env AQG_ROOT or sentinel walk-up)",
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--apply", action="store_true", help="Install/merge AQG Claude Code hooks")
    g.add_argument("--uninstall", action="store_true", help="Remove AQG Claude Code hooks only")
    g.add_argument("--verify", action="store_true", help="Report install state, no changes")
    g.add_argument("--is-installed", action="store_true",
                   help="Exit 0 iff >=1 managed AQG hook present (detection only, no changes)")
    args = parser.parse_args(argv)

    target = Path(args.target).resolve()
    aqg_root = _resolve_aqg_root(args.aqg_root)

    if args.verify:
        return cmd_verify(target, aqg_root)
    if args.is_installed:
        return cmd_is_installed(target)
    if args.uninstall:
        return cmd_uninstall(target, aqg_root)
    # apply
    if aqg_root is None:
        print(
            "ERROR: cannot resolve AQG root (pass --aqg-root, set AQG_ROOT env, "
            "or run from inside the AQG checkout)",
            file=sys.stderr,
        )
        return EXIT_GENERIC
    return cmd_apply(target, aqg_root)


if __name__ == "__main__":
    raise SystemExit(main())
