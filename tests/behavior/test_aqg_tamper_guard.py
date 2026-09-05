"""Tests for pretooluse_aqg_tamper_guard.sh — #328 tamper-resistance slice.

Threat model: an agent working in SOME OTHER project reaches into the AQG
install ($AQG_ROOT) and edits a gate-bearing file — a hook script or the
secret-pattern / redaction bank — to NEUTER a quality/secret gate before doing
the thing the gate would have caught. This PreToolUse guard denies such a
Write/Edit/MultiEdit.

Carve-outs (the chicken-and-egg problem): editing AQG from WITHIN the AQG
checkout (cwd under $AQG_ROOT) is self-development and is allowed; so is a human
pre-set AQG_AGENT=human-opt-in.

Honest scope — in-band, PARTIAL (same class as the secret-scan canary): does
NOT stop an adaptive agent who edits THIS guard, mutates files via Bash
(mv/tee/python -c), or manipulates cwd. Full tamper-resistance needs the
read-only/signed install.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HOOK = REPO / "agent-packs" / "claude-code" / "hooks" / "pretooluse_aqg_tamper_guard.sh"

# Real protected files under the AQG checkout (REPO doubles as $AQG_ROOT here).
PROTECTED_HOOK = "agent-packs/claude-code/hooks/pretooluse_secret_scan.sh"
PROTECTED_BANK = "scripts/_secret_patterns.py"


def _run(stdin: str, aqg_root: Path | None = REPO, env: dict | None = None) -> subprocess.CompletedProcess[str]:
    full = os.environ.copy()
    full.pop("AQG_AGENT", None)  # exercise the blocking path by default
    full.pop("AQG_ROOT", None)
    full.pop("CLAUDE_PROJECT_DIR", None)
    if aqg_root is not None:
        full["AQG_ROOT"] = str(aqg_root)
    if env:
        full.update(env)
    return subprocess.run(
        ["bash", str(HOOK)],
        input=stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=full,
        check=False,
        timeout=30,
    )


def _payload(file_path: str, cwd: str, tool: str = "Edit") -> str:
    return json.dumps(
        {
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "cwd": cwd,
            "tool_input": {"file_path": file_path, "new_string": "x"},
        }
    )


# --- BLOCK: cross-project neuter of a gate-bearing file --------------------


def test_deny_edit_hook_script_from_other_project(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / PROTECTED_HOOK), cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr
    assert "tamper-guard" in r.stderr.lower() and "block" in r.stderr.lower()


def test_deny_edit_pattern_bank_from_other_project(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / PROTECTED_BANK), cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr


def test_deny_path_traversal_into_bank(tmp_path: Path) -> None:
    # `scripts/../scripts/_secret_patterns.py` must still resolve to the bank.
    sneaky = str(REPO / "scripts" / ".." / "scripts" / "_secret_patterns.py")
    r = _run(_payload(sneaky, cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr


# --- ALLOW: legitimate edits + non-targets ---------------------------------


def test_allow_self_edit_when_cwd_inside_aqg() -> None:
    # Editing AQG from within the AQG checkout itself = self-development.
    r = _run(_payload(str(REPO / PROTECTED_HOOK), cwd=str(REPO)))
    assert r.returncode == 0, r.stderr


def test_allow_self_edit_when_cwd_in_aqg_subdir() -> None:
    r = _run(_payload(str(REPO / PROTECTED_HOOK), cwd=str(REPO / "skills")))
    assert r.returncode == 0, r.stderr


def test_allow_human_opt_in_override(tmp_path: Path) -> None:
    r = _run(
        _payload(str(REPO / PROTECTED_HOOK), cwd=str(tmp_path)),
        env={"AQG_AGENT": "human-opt-in"},
    )
    assert r.returncode == 0, r.stderr


def test_allow_non_protected_file_under_aqg_root(tmp_path: Path) -> None:
    # A normal file under AQG_ROOT (not a gate-bearing file) is not this guard's
    # concern.
    r = _run(_payload(str(REPO / "README.md"), cwd=str(tmp_path)))
    assert r.returncode == 0, r.stderr


def test_allow_file_outside_aqg_root(tmp_path: Path) -> None:
    r = _run(_payload(str(tmp_path / "foo.py"), cwd=str(tmp_path)))
    assert r.returncode == 0, r.stderr


# --- Silent boundaries (consistent with all AQG hooks) ---------------------


def test_silent_when_aqg_root_unset(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / PROTECTED_HOOK), cwd=str(tmp_path)), aqg_root=None)
    assert r.returncode == 0, r.stderr


def test_silent_when_file_path_absent(tmp_path: Path) -> None:
    r = _run(
        json.dumps({"tool_name": "Edit", "cwd": str(tmp_path), "tool_input": {}})
    )
    assert r.returncode == 0, r.stderr


def test_silent_on_malformed_stdin() -> None:
    r = _run("{not json")
    assert r.returncode == 0, r.stderr


# --- audit 5d4d64d7 regression coverage --------------------------------------


def test_deny_case_variant_path_on_case_insensitive_fs(tmp_path: Path) -> None:
    # gpt-5.5 f1: realpath does NOT case-normalize; on a case-insensitive FS a
    # case-variant component (capital S in Scripts/) still points at the real bank
    # and must be denied.
    variant = str(REPO / "Scripts" / "_secret_patterns.py")
    r = _run(_payload(variant, cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr


def test_allow_sibling_prefix_dir(tmp_path: Path) -> None:
    # _within boundary (the `+ os.sep` guard): a sibling dir sharing AQG_ROOT's
    # string prefix (…/agent-quality-gates-evil) is NOT inside AQG_ROOT.
    sibling = REPO.parent / (REPO.name + "-evil")
    r = _run(_payload(str(sibling / PROTECTED_HOOK), cwd=str(tmp_path)))
    assert r.returncode == 0, r.stderr


def test_deny_when_cwd_missing_fail_secure() -> None:
    # No cwd key + CLAUDE_PROJECT_DIR unset (popped by _run): the carve-out cannot
    # fire, so a protected target falls through to deny (fail-secure).
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": str(REPO / PROTECTED_BANK), "new_string": "x"}}
    )
    r = _run(payload)
    assert r.returncode == 2, r.stderr


def test_deny_when_cwd_empty_fail_secure() -> None:
    r = _run(_payload(str(REPO / PROTECTED_BANK), cwd=""))
    assert r.returncode == 2, r.stderr


def test_allow_via_claude_project_dir_fallback() -> None:
    # No cwd key, but CLAUDE_PROJECT_DIR points inside AQG → self-dev carve-out.
    payload = json.dumps(
        {"tool_name": "Edit", "tool_input": {"file_path": str(REPO / PROTECTED_HOOK), "new_string": "x"}}
    )
    r = _run(payload, env={"CLAUDE_PROJECT_DIR": str(REPO)})
    assert r.returncode == 0, r.stderr


def test_deny_write_tool_variant(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / PROTECTED_BANK), cwd=str(tmp_path), tool="Write"))
    assert r.returncode == 2, r.stderr


def test_deny_multiedit_tool_variant(tmp_path: Path) -> None:
    # MultiEdit carries a top-level file_path (same shape memory-write-guard handles).
    r = _run(_payload(str(REPO / PROTECTED_BANK), cwd=str(tmp_path), tool="MultiEdit"))
    assert r.returncode == 2, r.stderr


def test_deny_redaction_common(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / "scripts" / "_redaction_common.py"), cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr


def test_deny_dangerous_guard(tmp_path: Path) -> None:
    r = _run(_payload(str(REPO / "scripts" / "aqg_dangerous_guard.py"), cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr


def test_deny_edit_guard_itself(tmp_path: Path) -> None:
    # The guard protects its own script (a hook under agent-packs/*/hooks/).
    r = _run(_payload(str(HOOK), cwd=str(tmp_path)))
    assert r.returncode == 2, r.stderr
