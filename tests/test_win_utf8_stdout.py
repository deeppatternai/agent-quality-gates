"""Regression: non-ASCII-emitting entry scripts must write UTF-8 to stdout even
when the interpreter's I/O encoding is a legacy locale (Windows cp936 / LANG=C).

Ported from the public mirror (deeppatternai `tests/test_win_utf8_stdout.py`,
PR #60), which fixed this before A did — A carried the same defect, unguarded and
untested, until this backport. One adaptation: the mirror anchored the end-to-end
case on Chinese text in the handoff skeleton, which is English here, so that
assertion anchors on an em dash instead (see the comment at the assertion).

Root cause (fixed): on Windows the std streams default to cp936, so a script
that `print`s Chinese produced mojibake when its output was captured/redirected
(the aqg-session-handoff `new` skeleton was the reported failure). The fix mirrors
the pre-existing guard in aqg_re_anchor.py: reconfigure stdout/stderr to UTF-8 at
the top of `main()` (errors="replace" so output never crashes), and stdin to
strict UTF-8 (so malformed piped input still surfaces). The output guard is
effectively a no-op on Linux/macOS (already UTF-8).

These tests force `PYTHONIOENCODING=cp936` on the child to reproduce the Windows
default on ANY platform, capture the child's stdout as raw BYTES, and assert the
bytes decode as UTF-8 — proving the child wrote UTF-8 regardless of the hostile
locale. Without the fix the child encodes Chinese as GBK and the utf-8 decode
raises / mismatches (RED).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# The six Chinese-emitting entry scripts that print to stdout. aqg_phase_router.py
# is intentionally excluded: it is a library module with no CLI entry — its
# Chinese strings are printed by aqg_phase_emit.py, which IS covered here.
ENTRY_SCRIPTS = [
    "skills/aqg-session-handoff/scripts/aqg_session_handoff.py",
    "skills/aqg-phase-transition/scripts/aqg_phase_emit.py",
    "skills/aqg-decision-capture/scripts/aqg_decision_capture.py",
    "skills/aqg-code-construction/scripts/aqg_construction_check.py",
    "scripts/check_evidence_closeout.py",
    "scripts/validate_audit_adjudication.py",
]

# Matches the reconfigure guard block regardless of exact whitespace/comments.
_GUARD_RE = re.compile(r"\.reconfigure\(\s*encoding=[\"']utf-8[\"']")


def _cp936_env() -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "cp936"  # simulate the Windows console default
    env.pop("PYTHONUTF8", None)        # don't let a UTF-8 override mask the bug
    return env


def test_handoff_new_emits_utf8_under_cp936(tmp_path):
    """End-to-end reproduction of the reported failure: `handoff new` under a
    cp936 stdout must still emit UTF-8 (bytes decode cleanly + Chinese present).
    Runs in a tmp cwd so it never reads/mutates the real repo's .aqg state."""
    script = REPO_ROOT / "skills/aqg-session-handoff/scripts/aqg_session_handoff.py"
    proc = subprocess.run(
        [sys.executable, str(script), "new"],
        capture_output=True,  # bytes: we assert the raw encoding ourselves
        env=_cp936_env(),
        cwd=str(tmp_path),
    )
    assert b"Traceback" not in proc.stderr, proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    # The bytes on the wire must be UTF-8 (raises UnicodeDecodeError on GBK bytes).
    text = proc.stdout.decode("utf-8")
    # This skeleton is English, so the anchor is punctuation rather than Chinese:
    # U+2014 encodes as e2 80 94 in UTF-8 but a1 aa in cp936, so a child that
    # ignored the guard still fails the decode above. Verified by removing the
    # guard: the stream then breaks at the first such byte. Asserting the char is
    # present keeps that decode meaningful — an all-ASCII skeleton would make it
    # pass no matter what the child did.
    assert "—" in text, "skeleton lost its non-ASCII anchor; this test can no longer fail"


@pytest.mark.parametrize("rel", ENTRY_SCRIPTS)
def test_entry_script_help_is_utf8_under_cp936(rel):
    """Behavioral coverage for all six scripts: each is actually EXECUTED under a
    cp936 locale and its --help output (argparse runs after the main() guard) must
    be valid UTF-8 on the wire. For scripts with Chinese in their help/description
    this fails without the guard (GBK bytes); for ASCII help it is trivially safe."""
    script = REPO_ROOT / rel
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,  # bytes
        env=_cp936_env(),
        cwd=str(REPO_ROOT),
    )
    assert b"Traceback" not in proc.stderr, proc.stderr.decode("utf-8", "replace")
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    proc.stdout.decode("utf-8")  # raises UnicodeDecodeError if the child wrote GBK


@pytest.mark.parametrize("rel", ENTRY_SCRIPTS)
def test_entry_script_has_utf8_guard(rel):
    """Structural coverage: every Chinese-emitting entry script carries the UTF-8
    std-stream guard, so a newly added Chinese branch cannot silently regress."""
    src = (REPO_ROOT / rel).read_text(encoding="utf-8")
    assert _GUARD_RE.search(src), f"{rel} missing UTF-8 std-stream reconfigure guard"
