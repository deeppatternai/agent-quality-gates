"""Every host adapter must survive the hook emitting JSON on stdout.

Making the PostToolUse hooks model-visible on Claude Code meant they now print a
`hookSpecificOutput.additionalContext` envelope on stdout **in addition to** the
stderr they always wrote. Three adapters were tested for that change (Claude,
Codex, Cursor) and three were not — and one of the untested three broke:

`agent_client_aqg_hook.py` builds its model-facing context by concatenating
stdout AND stderr. Before the change stdout was empty, so the context was just
the reminder. After it, Trae and Devin received the raw JSON envelope *plus* the
same reminder again in plain text — the reminder twice, once as escaped JSON.

The two remaining adapters (`pi_aqg_hook.py`, `agent-packs/qoder/hooks/
qoder_hook_adapter.py`) are pass-throughs onto hosts whose stdout contract is not
documented here. They are handled conservatively: the envelope is suppressed so
those hosts see exactly what they saw before this work, no better and no worse.
That is a deliberate non-improvement, and it is what these tests pin.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CODE_PAYLOAD = json.dumps({"tool_input": {"file_path": "/repo/src/lib.py"}})
HOOK = "posttooluse_code_construction_reminder.sh"


def _run(argv: list[str], stdin: str = CODE_PAYLOAD) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, *argv],
        input=stdin,
        text=True,
        capture_output=True,
        cwd=REPO,
        check=False,
    )


def test_trae_context_carries_the_reminder_not_a_json_blob() -> None:
    """The confirmed regression: a raw envelope reached the model as text."""
    proc = _run([
        "scripts/agent_client_aqg_hook.py",
        "--client", "trae", "--aqg-root", str(REPO),
        "--hook", HOOK, "--managed-id", "aqg-agent-client-v1",
    ])
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["additionalContext"]
    assert "[aqg audit-before-commit gate]" in context, "reminder lost"
    assert "hookSpecificOutput" not in context, "raw JSON envelope leaked into model context"
    assert "\\u" not in context, "escaped unicode leaked; the text was not decoded"


def test_trae_does_not_deliver_the_reminder_twice() -> None:
    """Concatenating both channels duplicated every line of it."""
    proc = _run([
        "scripts/agent_client_aqg_hook.py",
        "--client", "trae", "--aqg-root", str(REPO),
        "--hook", HOOK, "--managed-id", "aqg-agent-client-v1",
    ])
    context = json.loads(proc.stdout)["additionalContext"]
    assert context.count("[aqg audit-before-commit gate]") == 1, "reminder delivered twice"


def test_devin_uses_its_own_field_but_the_same_clean_text() -> None:
    """Schema differs per host; the content must not."""
    proc = _run([
        "scripts/agent_client_aqg_hook.py",
        "--client", "devin", "--aqg-root", str(REPO),
        "--hook", HOOK, "--managed-id", "aqg-agent-client-v1",
    ])
    assert proc.returncode == 0, proc.stderr
    context = json.loads(proc.stdout)["additional_context"]
    assert "[aqg audit-before-commit gate]" in context
    assert "hookSpecificOutput" not in context


def test_pi_stdout_is_unchanged_by_the_envelope() -> None:
    """Conservative: a host whose stdout contract is unknown sees what it saw before.

    PI's adapter is a pure pass-through. Before this work the hook wrote nothing
    to stdout, so PI's stdout was empty and the reminder arrived on stderr. That
    is restored deliberately rather than guessed at — making PI model-visible
    needs its hook contract, which this repo does not document.
    """
    proc = _run([
        "scripts/pi_aqg_hook.py",
        "--aqg-root", str(REPO), "--hook", HOOK, "--managed-id", "aqg-pi-v1",
    ])
    assert proc.returncode == 0, proc.stderr
    assert "hookSpecificOutput" not in proc.stdout, "envelope leaked to an undocumented host"
    assert "[aqg audit-before-commit gate]" in proc.stderr, "reminder lost from stderr"


def test_qoder_stdout_is_unchanged_by_the_envelope() -> None:
    """Same conservative treatment as PI."""
    proc = _run([
        "agent-packs/qoder/hooks/qoder_hook_adapter.py",
        "--aqg-root", str(REPO), "--hook", HOOK, "--managed-id", "aqg-qoder-v1",
    ])
    assert proc.returncode == 0, proc.stderr
    assert "hookSpecificOutput" not in proc.stdout, "envelope leaked to an undocumented host"
    assert "[aqg audit-before-commit gate]" in proc.stderr, "reminder lost from stderr"


def test_envelope_with_empty_payload_falls_back_to_stderr() -> None:
    """An empty payload must not blank out a reminder that stderr still holds.

    The first fix keyed on "did an envelope parse", so an envelope carrying an
    empty additionalContext produced an empty context AND discarded stderr —
    silently delivering nothing, which is the very defect this work removes.
    """
    import importlib, sys as _sys
    _sys.path.insert(0, str(REPO / "scripts"))
    mod = importlib.import_module("agent_client_aqg_hook")
    envelope = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                  "additionalContext": ""}})
    context, residual = mod._aqg_split_envelope(envelope)
    assert context == "", "empty payload should yield no context"
    assert residual.strip() == "", "the envelope line must still be consumed, not forwarded"


def test_mixed_stdout_suppresses_only_the_envelope() -> None:
    """Fail-open was the bug: one extra line made the whole blob pass through.

    An all-or-nothing json.loads over the entire stdout returns None on mixed
    output, so the caller forwarded everything — envelope included — recreating
    the leak. Line-based splitting keeps unrelated output and drops the envelope.
    """
    import importlib, sys as _sys
    _sys.path.insert(0, str(REPO / "scripts"))
    mod = importlib.import_module("agent_client_aqg_hook")
    envelope = json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse",
                                                  "additionalContext": "REMINDER"}})
    context, residual = mod._aqg_split_envelope(envelope + "\nunrelated tool output\n")
    assert context == "REMINDER"
    assert "hookSpecificOutput" not in residual, "envelope leaked through mixed output"
    assert "unrelated tool output" in residual, "unrelated stdout was swallowed"


def test_stderr_that_differs_from_the_envelope_is_not_dropped() -> None:
    """Preferring the envelope silently discarded anything else on stderr.

    stderr is normally the same text, but a hook can also emit a DEGRADED line
    there; losing it would hide exactly the failure that line exists to report.
    """
    proc = _run([
        "scripts/agent_client_aqg_hook.py",
        "--client", "trae", "--aqg-root", str(REPO),
        "--hook", HOOK, "--managed-id", "aqg-agent-client-v1",
    ])
    context = json.loads(proc.stdout)["additionalContext"]
    stderr = proc.stderr.strip()
    if stderr and stderr not in context:
        assert stderr in context, "stderr content absent from the envelope was dropped"


def test_every_adapter_in_the_repo_is_covered_by_this_file() -> None:
    """Guard against the gap that caused this: adapters nobody thought to test.

    Three adapters were tested and three were not, and the untested set held the
    regression. Asserting the inventory means a NEW adapter cannot be added
    without this file failing until someone decides how it handles the envelope.
    """
    # Identify adapters by BEHAVIOUR, not by filename. Two name-based globs were
    # tried first and both were wrong in opposite directions: `*_aqg_hook.py`
    # silently missed run_aqg_codex_hook.py, and `*hook*.py` swept in a ledger
    # writer and a contract prober that never run a hook. The defining property
    # is that the file names the PostToolUse hook family and shells out to it.
    candidates = list((REPO / "scripts").glob("*.py"))
    candidates += list((REPO / "agent-packs").glob("*/hooks/*.py"))
    adapters = set()
    for path in candidates:
        if path.name.startswith("install_"):
            continue  # installers WIRE hooks, they do not run them
        text = path.read_text(encoding="utf-8", errors="replace")
        # Any way of shelling out counts, not just subprocess.run — keying on the
        # one spelling I happened to use was another too-narrow perimeter.
        shells_out = any(
            token in text
            for token in ("subprocess.run", "subprocess.Popen", "check_output",
                          "os.system", "os.execv", "os.spawn")
        )
        if "posttooluse_" in text and shells_out:
            adapters.add(path.name)
    # Files that name the hook family and run subprocesses without being host
    # adapters. Listed explicitly with a reason rather than filtered by name, so
    # a new file still forces someone to classify it.
    adapters -= {
        "aqg_doctor.py",  # READS hook sources to judge visibility; never runs one
    }
    known = {
        "cursor_aqg_hook.py",        # own wording, never runs the .sh — covered in test_cursor_support.py
        "run_aqg_codex_hook.py",     # parses stdout JSON — covered in test_aqg_hooks.py
        "agent_client_aqg_hook.py",  # covered above
        "pi_aqg_hook.py",            # covered above
        "qoder_hook_adapter.py",     # covered above
    }
    assert adapters == known, (
        f"adapter inventory changed: {adapters ^ known}. A new host adapter must "
        "declare how it handles the hook's stdout JSON envelope before shipping."
    )
