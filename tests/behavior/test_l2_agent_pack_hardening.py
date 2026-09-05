"""L2 pre-launch hardening regression suite for validate_agent_pack.py.

Pins audit 32e2c571 (gpt-5.5 + gemini) findings D15 + D16 (workflow PR-D). Each
bug-repro FAILS against the pre-fix scripts/validate_agent_pack.py (stash-proven).

  * D15 — the warn-only blocking-command regex `[1-9]\\b` matched only single
    digits, so `exit 10` / `exit 127` slipped through; and the `|| false` check
    was case-sensitive (missed `|| FALSE`).
  * D16 — a `$aqg_root/../...py` script ref could resolve OUTSIDE repo_root, so
    the is_file() existence check could validate an out-of-repo script.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import validate_agent_pack as vap  # noqa: E402


def _hook(tmp_path: Path, command: str) -> Path:
    p = tmp_path / "h.json"
    p.write_text(
        json.dumps(
            {"hooks": {"PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": command}]}
            ]}}
        ),
        encoding="utf-8",
    )
    return p


def _skill(tmp_path: Path, body_extra: str) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True, exist_ok=True)
    skill = repo / "skills" / "aqg-test" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(
        "---\nname: aqg-test\ndescription: Use when testing the pack validator.\n---\n"
        "Uses scripts/ resolved from CLAUDE_SKILL_DIR.\n" + body_extra + "\n",
        encoding="utf-8",
    )
    return repo, skill


# --- D15: multi-digit nonzero exit + case-insensitive || false ----------------


def test_d15_multi_digit_exit_blocked(tmp_path):
    # `|| true` present so the catch-all passes; the multi-digit `exit 10` must
    # still be caught by the blocking-command regex.
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || true; exit 10')
    with pytest.raises(vap.ValidationError, match="blocking exit"):
        vap.validate_hook(hook)


def test_d15_exit_127_blocked(tmp_path):
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || true; exit 127')
    with pytest.raises(vap.ValidationError, match="blocking exit"):
        vap.validate_hook(hook)


def test_d15_uppercase_or_false_blocked(tmp_path):
    # `exit 0` present so the catch-all passes; the `|| FALSE` must be caught
    # case-insensitively.
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || FALSE; exit 0')
    with pytest.raises(vap.ValidationError, match=r"\|\| false"):
        vap.validate_hook(hook)


def test_d15_exit_zero_warn_only_ok(tmp_path):
    # GREEN guard: a legitimate warn-only command must still pass.
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || true')
    vap.validate_hook(hook)  # no raise


def test_d15_exit_00_allowed(tmp_path):
    # `exit 00` is exit code 0 → not blocking.
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || true; exit 00')
    vap.validate_hook(hook)  # no raise


@pytest.mark.parametrize("code", ["01", "010", "100", "1"])
def test_d15_nonzero_decimal_variants_blocked(tmp_path, code):
    # strip("0") must treat leading/embedded-zero nonzero codes as blocking.
    hook = _hook(tmp_path, f'bash "$CLAUDE_PROJECT_DIR/x.sh" || true; exit {code}')
    with pytest.raises(vap.ValidationError, match="blocking exit"):
        vap.validate_hook(hook)


def test_d15_return_nonzero_blocked(tmp_path):
    hook = _hook(tmp_path, 'bash "$CLAUDE_PROJECT_DIR/x.sh" || true; return 5')
    with pytest.raises(vap.ValidationError, match="blocking exit"):
        vap.validate_hook(hook)


def test_d15_conservative_lint_flags_literal_in_text(tmp_path):
    # Documents the intentional conservative-lint policy (audit f2): a literal
    # `exit 10` even inside a quoted echo is flagged — example hooks stay clean.
    hook = _hook(tmp_path, 'echo "exit 10 in $CLAUDE_PROJECT_DIR" || true; exit 0')
    with pytest.raises(vap.ValidationError, match="blocking exit"):
        vap.validate_hook(hook)


# --- D16: script-ref path traversal -------------------------------------------


def test_d16_script_ref_traversal_rejected(tmp_path):
    repo, skill = _skill(tmp_path, "Runs $aqg_root/../evil.py")
    evil = tmp_path / "evil.py"  # OUTSIDE repo, but exists → pre-fix would accept
    evil.write_text("# evil\n", encoding="utf-8")
    with pytest.raises(vap.ValidationError, match="escapes|traversal"):
        vap.validate_skill(skill, repo_root=repo)


def test_d16_absolute_looking_ref_rejected(tmp_path):
    # `$aqg_root//etc/...py` captures `/etc/...py`; repo_root / "/etc/..." resets
    # to an absolute path that escapes the repo.
    repo, skill = _skill(tmp_path, "Runs $aqg_root//etc/evil_abs.py")
    with pytest.raises(vap.ValidationError, match="escapes|traversal"):
        vap.validate_skill(skill, repo_root=repo)


def test_d16_valid_in_repo_ref_ok(tmp_path):
    repo, skill = _skill(tmp_path, "Runs $aqg_root/scripts/real.py")
    (repo / "scripts" / "real.py").write_text("# ok\n", encoding="utf-8")
    vap.validate_skill(skill, repo_root=repo)  # no raise
