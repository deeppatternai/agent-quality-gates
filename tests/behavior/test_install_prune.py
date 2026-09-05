"""CI-gated safety tests for the stale-symlink prune in the two install.sh.

`install.sh --force` (Codex) and `agent-packs/claude-code/install.sh --force`
(Claude) now prune residue — symlinks left by renamed/removed skills (incl the
0.3.0 ai-team-* rename) — so an upgrade is zero-residue without manual cleanup.

Prune is destructive (`rm -f`), so these tests pin the exact safety boundary:
it removes ONLY symlinks whose target points into THAT installer's own skills/
dir, and never touches real directories or third-party / outside-repo links.
Each installer is exercised against a throwaway --dest so nothing real is hit.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.aqg_skill_install import classify_install

REPO = Path(__file__).resolve().parents[2]

# (installer, extra_args, skills_root that this installer links into)
INSTALLERS = {
    "codex": (
        REPO / "scripts" / "install.sh",
        ["--force"],
        REPO / "skills",
    ),
    "claude": (
        REPO / "agent-packs" / "claude-code" / "install.sh",
        ["--mode", "link", "--force"],
        REPO / "agent-packs" / "claude-code" / "skills",
    ),
}


def _bash_executable() -> str:
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_bash = Path(git).resolve().parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    bash = shutil.which("bash")
    if bash:
        return bash
    pytest.skip("bash is unavailable")


def _run(installer: Path, extra_args: list[str], dest: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [_bash_executable(), str(installer), *extra_args, "--dest", str(dest)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(REPO),
    )


def test_codex_install_refreshes_hooks_by_default_and_stays_idempotent(tmp_path):
    codex_home = tmp_path / "codex-home"
    env = dict(os.environ)
    env["CODEX_HOME"] = str(codex_home)
    env["HOME"] = str(tmp_path / "home")
    env["AQG_BACKUP_DIR"] = str(tmp_path / ".aqg-central")

    proc = subprocess.run(
        [_bash_executable(), str(REPO / "scripts" / "install.sh"), "--force"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(REPO),
        env=env,
    )
    assert proc.returncode == 0, proc.stderr

    hooks_json = codex_home / "hooks.json"
    assert hooks_json.is_file()
    verify = subprocess.run(
        [
            sys.executable,
            str(REPO / "scripts" / "install_aqg_codex_hooks.py"),
            "--verify",
            "--target",
            str(hooks_json),
            "--aqg-root",
            str(REPO),
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(REPO),
        env=env,
    )
    assert verify.returncode == 0, verify.stdout + verify.stderr

    before = hooks_json.read_text(encoding="utf-8")
    rerun = subprocess.run(
        [_bash_executable(), str(REPO / "scripts" / "install.sh"), "--force"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        cwd=str(REPO),
        env=env,
    )
    assert rerun.returncode == 0, rerun.stderr
    assert hooks_json.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("variant", sorted(INSTALLERS))
def test_prune_keeps_and_removes_correctly(variant: str, tmp_path):
    installer, extra_args, skills_root = INSTALLERS[variant]
    dest = tmp_path / "skills"
    dest.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    # --- plant fixtures -------------------------------------------------------
    # 1. stale skill symlink into THIS installer's skills/ (dangling) → PRUNE
    try:
        (dest / "aqg-removed-old").symlink_to(skills_root / "aqg-removed-old")
    except OSError as exc:
        pytest.skip(f"symlink prune fixture unavailable: {exc}")
    # 2. 0.3.0 legacy ai-team-* symlink into skills/ (dangling) → PRUNE
    (dest / "ai-team-startup-preflight").symlink_to(skills_root / "ai-team-startup-preflight")
    # 3. third-party symlink to an outside, real path → KEEP
    (dest / "my-own-skill").symlink_to(outside)
    # 4. real directory (not a symlink) → KEEP, untouched
    real_dir = dest / "hand-made"
    real_dir.mkdir()
    (real_dir / "keep.txt").write_text("precious", encoding="utf-8")
    # 5. symlink into the repo but OUTSIDE skills_root → KEEP (not a skill artifact)
    (dest / "weird-link").symlink_to(REPO / "scripts")
    # 6. symlink whose TEXT is under skills_root but contains '..' so it resolves
    #    OUTSIDE — text-matches the prefix yet must be KEPT (audit 5e71dfaf f1:
    #    prune is single-segment-only, never '..'/deeper paths).
    (dest / "dotdot-escape").symlink_to(f"{skills_root}/../scripts")

    proc = _run(installer, extra_args, dest)
    assert proc.returncode == 0, f"{variant} install failed:\n{proc.stdout}\n{proc.stderr}"

    # --- pruned -------------------------------------------------------------
    assert not (dest / "aqg-removed-old").exists() and not (dest / "aqg-removed-old").is_symlink(), \
        "stale skill symlink into skills_root must be pruned"
    assert not (dest / "ai-team-startup-preflight").is_symlink(), \
        "legacy ai-team-* symlink must be pruned"

    # --- kept ---------------------------------------------------------------
    assert (dest / "my-own-skill").is_symlink(), "third-party symlink must be kept"
    assert real_dir.is_dir() and (real_dir / "keep.txt").read_text() == "precious", \
        "real directory must be untouched"
    assert (dest / "weird-link").is_symlink(), \
        "symlink into repo but outside skills_root must be kept"
    assert (dest / "dotdot-escape").is_symlink(), \
        "symlink with '..' escaping skills_root must be kept (prune is single-segment-only)"

    # --- current skills linked + resolve into the repo ----------------------
    sample = dest / "aqg-evidence-closeout"
    expected_types = {"symlink", "junction"} if os.name == "nt" else {"symlink"}
    assert classify_install(sample) in expected_types, "current skill must be linked"
    assert sample.resolve() == (skills_root / "aqg-evidence-closeout").resolve()


@pytest.mark.parametrize("variant", sorted(INSTALLERS))
def test_prune_noop_on_clean_dest(variant: str, tmp_path):
    """A dest with only current skills (or empty) loses nothing to the prune."""
    installer, extra_args, _ = INSTALLERS[variant]
    dest = tmp_path / "skills"
    dest.mkdir()
    proc = _run(installer, extra_args, dest)
    assert proc.returncode == 0, proc.stderr
    # all 12 current skills present after a clean install
    expected_types = {"symlink", "junction"} if os.name == "nt" else {"symlink"}
    linked = sorted(
        path.name for path in dest.iterdir() if classify_install(path) in expected_types
    )
    assert len(linked) >= 12, f"{variant}: expected >=12 linked skills, got {linked}"
    assert "pruned stale" not in proc.stdout, "clean dest should prune nothing"
