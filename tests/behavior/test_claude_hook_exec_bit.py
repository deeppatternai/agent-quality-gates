"""CI guard: every Claude-pack hook script is git-tracked as 100755 (executable).

Why this exists: PR #172 fixed two hooks (posttooluse_bash_error_debugging_reminder.sh
and posttooluse_test_quality_reminder.sh) that #149 added with git mode 100644 while
the other hooks were 100755 — a hygiene drift that no test caught. AQG hooks are
invoked via `bash "$AQG_ROOT/.../hook.sh"` (see scripts/install_aqg_hooks.py), so a
missing exec bit is NOT a runtime failure, but the inconsistency is real and recurs
whenever a new hook is added without `chmod +x`. This canary fails the moment any
tracked *.sh under the Claude hooks dir is not 100755, naming the file and the fix.

It checks the git *index/tree* mode (`git ls-files -s`), not the filesystem mode:
the committed mode is what ships to every clone and what #172 actually corrected;
filesystem mode can drift per-checkout under core.fileMode and is not the invariant.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# tests/behavior/<file> -> parents[2] == repo root
REPO = Path(__file__).resolve().parents[2]
HOOKS_DIR = "agent-packs/claude-code/hooks"
EXPECTED_MODE = "100755"


def _tracked_sh_modes() -> dict[str, str]:
    """Map each git-tracked *.sh under HOOKS_DIR to its index/tree mode.

    `git ls-files -s` emits one line per file: "<mode> <object> <stage>\\t<path>".
    """
    out = subprocess.run(
        ["git", "ls-files", "-s", "--", HOOKS_DIR],
        text=True, capture_output=True, cwd=str(REPO), check=True,
    ).stdout
    modes: dict[str, str] = {}
    for line in out.splitlines():
        meta, _, path = line.partition("\t")
        if path.endswith(".sh"):
            modes[path] = meta.split()[0]
    return modes


def test_claude_hooks_are_tracked_executable() -> None:
    modes = _tracked_sh_modes()
    assert modes, (
        f"no *.sh tracked under {HOOKS_DIR} — test wiring is broken "
        "(wrong path or repo root)"
    )
    non_exec = {p: m for p, m in sorted(modes.items()) if m != EXPECTED_MODE}
    assert not non_exec, (
        f"Claude-pack hook scripts must be git-tracked as {EXPECTED_MODE} "
        "(executable) for hygiene consistency; PR #172 fixed this class of drift. "
        "Hooks run via `bash <path>`, so this is not a runtime failure, but a new "
        "hook added without `chmod +x` regresses it. Fix each with:\n"
        "    git update-index --chmod=+x <file>\n"
        "Offending files (path: current mode):\n"
        + "\n".join(f"    {p}: {m}" for p, m in non_exec.items())
    )
