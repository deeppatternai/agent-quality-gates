"""The two installers carry the same hook table twice — keep them in step.

`scripts/install_aqg_hooks.py` (Claude Code, writes `~/.claude/settings.json`)
and `scripts/install_aqg_codex_hooks.py` (Codex, writes `~/.codex/hooks.json`)
each hold their own copy of which hook mounts on which lifecycle event. Nothing
compared them, and each installer's tests only ever checked its own side.

That gap has already cost once: `precompact_closeout_reminder.sh` was unmounted
from `Stop` in the Claude installer — because Stop fires after every assistant
turn and the hook emits a 14-line completion checklist unconditionally — and the
Codex installer kept firing it, because the change was made on one side and
nothing noticed. The user saw the checklist disappear in one host and persist in
the other.

The invariant is deliberately narrow: only scripts present in BOTH tables are
compared. Host-specific hooks and host-specific lifecycle events stay free to
differ, which they legitimately do.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, REPO / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _scripts_to_events(spec: dict) -> dict[str, set[str]]:
    """Map hook-script basename -> the set of events it mounts on.

    Both installers nest as {event: [{matcher, hooks: [...]}]}, but the Claude
    side stores `command` as a shell string and the Codex side may store a list,
    so the script name is recovered by scanning tokens rather than by shape.
    """
    found: dict[str, set[str]] = {}
    for event, entries in spec.items():
        for entry in entries:
            for hook in entry.get("hooks", []):
                command = hook.get("command", "")
                if isinstance(command, list):
                    command = " ".join(str(part) for part in command)
                for token in str(command).split():
                    token = token.strip('"').strip("'")
                    if token.endswith(".sh"):
                        found.setdefault(token.rsplit("/", 1)[-1], set()).add(event)
    return found


def _both_tables() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    claude = _load("aqg_claude_installer", "scripts/install_aqg_hooks.py")
    codex = _load("aqg_codex_installer", "scripts/install_aqg_codex_hooks.py")
    return (
        _scripts_to_events(claude._aqg_hook_specs(REPO)),
        _scripts_to_events(
            codex._aqg_hook_specs(REPO, python_executable=Path(sys.executable))
        ),
    )


def test_shared_hooks_mount_on_the_same_events_in_both_installers() -> None:
    claude, codex = _both_tables()
    shared = sorted(set(claude) & set(codex))
    assert len(shared) >= 10, f"only {len(shared)} shared hooks found; scan looks broken"

    drift = {
        script: {"claude": sorted(claude[script]), "codex": sorted(codex[script])}
        for script in shared
        if claude[script] != codex[script]
    }
    assert not drift, (
        "the same hook mounts on different lifecycle events in the two installers, "
        "so a change made on one side did not reach the other:\n"
        + "\n".join(
            f"  {s}: claude={v['claude']} codex={v['codex']}" for s, v in drift.items()
        )
    )


def test_the_comparison_is_not_vacuous() -> None:
    """A scan that found nothing would pass the invariant trivially."""
    claude, codex = _both_tables()
    assert len(claude) >= 12, f"claude table looks empty: {sorted(claude)}"
    assert len(codex) >= 12, f"codex table looks empty: {sorted(codex)}"
    # The hook this guard exists for must be in both tables, or the regression
    # it was written for would slip through as "not shared".
    for table, name in ((claude, "claude"), (codex, "codex")):
        assert "precompact_closeout_reminder.sh" in table, f"{name} lost the hook"
        assert "wip_checkpoint_save.sh" in table, f"{name} lost the WIP hook"
