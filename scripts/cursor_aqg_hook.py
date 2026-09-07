#!/usr/bin/env python3
"""Cursor-native lifecycle adapter for Agent Quality Gates.

Cursor sends JSON on stdin and consumes JSON on stdout. Blocking decisions use
both native ``permission: deny`` and exit code 2. Reminder hooks return only
documented model/user-visible fields; secret values are never echoed.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable


MAX_INPUT_BYTES = 1_000_000
MAX_CONTEXT_LINES = 50
CODE_EXTENSIONS = {
    ".c", ".cc", ".clj", ".cpp", ".cs", ".dart", ".ex", ".exs", ".fs",
    ".go", ".groovy", ".h", ".hpp", ".hs", ".java", ".jl", ".js", ".jsx",
    ".kt", ".kts", ".lua", ".m", ".ml", ".mm", ".nim", ".php", ".pl",
    ".pm", ".ps1", ".py", ".r", ".rb", ".rs", ".scala", ".sh", ".sol",
    ".sql", ".svelte", ".swift", ".ts", ".tsx", ".vue", ".zig",
}
SECURITY_SIGNAL = re.compile(
    r"auth|password|secret|token|api[_-]?key|oauth|jwt|cookie|csrf|subprocess|shell\s*=\s*true|"
    r"requests\.|urlopen|innerHTML|SELECT\s+.+FROM|pickle\.load|yaml\.load",
    re.IGNORECASE,
)
COMMIT_RE = re.compile(
    r"(?:^|[\s;&|])git"
    r"(?:\s+(?:-C|-c|--git-dir|--work-tree)\s+\S+|\s+--(?:git-dir|work-tree)=\S+)*"
    r"\s+commit(?:\s|$)"
)


def _read_payload() -> dict[str, object]:
    data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    if len(data) > MAX_INPUT_BYTES:
        raise ValueError("hook input exceeds size limit")
    text = data.decode("utf-8-sig")
    value = json.loads(text) if text.strip() else {}
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def _emit(value: dict[str, object]) -> None:
    json.dump(value, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")


def _project_dir(payload: dict[str, object], *, allow_cwd_fallback: bool = True) -> Path | None:
    candidates: list[object] = [payload.get("cwd")]
    roots = payload.get("workspace_roots")
    if isinstance(roots, list) and roots:
        candidates.append(roots[0])
    candidates.append(os.environ.get("CURSOR_PROJECT_DIR"))
    if allow_cwd_fallback:
        candidates.append(os.getcwd())
    for candidate in candidates:
        if isinstance(candidate, str) and candidate.strip():
            return Path(candidate).expanduser().resolve()
    return None


def _load_secret_counts(aqg_root: Path) -> Callable[[str], dict[str, int]]:
    module_path = aqg_root / "scripts" / "_secret_patterns.py"
    spec = importlib.util.spec_from_file_location("_aqg_cursor_secret_patterns", module_path)
    if spec is None or spec.loader is None:
        raise ImportError("secret pattern bank loader unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    function = getattr(module, "secret_counts")
    if not callable(function):
        raise ImportError("secret_counts unavailable")
    return function


def _secret_chunks(payload: dict[str, object]) -> list[str]:
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return []
    if tool in {"Shell", "Bash"}:
        command = tool_input.get("command")
        return [command] if isinstance(command, str) else []
    if tool in {"Write", "Edit", "MultiEdit"}:
        chunks: list[str] = []
        for key in ("content", "new_string", "new_source"):
            value = tool_input.get(key)
            if isinstance(value, str):
                chunks.append(value)
        edits = tool_input.get("edits")
        if isinstance(edits, list):
            for edit in edits:
                if isinstance(edit, dict) and isinstance(edit.get("new_string"), str):
                    chunks.append(edit["new_string"])
        return chunks
    return []


def _deny(reason: str) -> int:
    print(reason, file=sys.stderr)
    _emit(
        {
            "permission": "deny",
            "user_message": reason,
            "agent_message": reason,
        }
    )
    return 2


def _run(args: list[str], *, cwd: Path, timeout: int, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=cwd,
        input=input_text,
        text=True,
        capture_output=True,
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _git_names(project: Path, command: str) -> list[str]:
    names: set[str] = set()
    checks = [["git", "diff", "--cached", "--name-only"]]
    if re.search(r"\s(?:-a|--all)(?:\s|$)", command) or re.search(r"\bgit\s+add\b", command):
        checks.append(["git", "diff", "--name-only", "HEAD"])
    for args in checks:
        proc = _run(args, cwd=project, timeout=10)
        if proc.returncode == 0:
            names.update(line.strip().replace("\\", "/") for line in proc.stdout.splitlines() if line.strip())
    return sorted(names)


def _validate_skill_commit(payload: dict[str, object], aqg_root: Path, project: Path) -> int | None:
    if payload.get("tool_name") not in {"Shell", "Bash"}:
        return None
    tool_input = payload.get("tool_input")
    command = tool_input.get("command", "") if isinstance(tool_input, dict) else ""
    if not isinstance(command, str) or not COMMIT_RE.search(command):
        return None
    affected = sorted(
        {name.split("/", 2)[1] for name in _git_names(project, command) if re.match(r"^skills/aqg-[^/]+/", name)}
    )
    validator = aqg_root / "scripts" / "aqg_skill_validator.py"
    for skill in affected:
        proc = _run([sys.executable, str(validator), skill], cwd=aqg_root, timeout=30)
        if proc.returncode != 0:
            return _deny(
                f"AQG skill validator blocked this commit for {skill}; fix the reported violations before committing."
            )
    return None


def _pre_tool_use(payload: dict[str, object], aqg_root: Path, project: Path) -> int:
    chunks = _secret_chunks(payload)
    if chunks:
        try:
            secret_counts = _load_secret_counts(aqg_root)
            counts = secret_counts("\n".join(chunks))
        except Exception as exc:  # aqg: top-level boundary
            if os.environ.get("AQG_AGENT") != "human-opt-in":
                return _deny(
                    f"AQG secret scan failed closed ({type(exc).__name__}); repair the AQG install or use the documented human override."
                )
            counts = {}
        if counts and os.environ.get("AQG_AGENT") != "human-opt-in":
            kinds = ", ".join(sorted(counts))
            return _deny(
                f"AQG secret scan blocked a recognized secret pattern ({kinds}). The value was not echoed; use an environment variable or secret manager."
            )
    validator_result = _validate_skill_commit(payload, aqg_root, project)
    if validator_result is not None:
        return validator_result
    # No opinion on clean calls: returning `allow` could bypass Cursor's own
    # approval/run-mode policy. AQG only denies; normal approval remains Cursor's.
    _emit({})
    return 0


def _tool_file(payload: dict[str, object]) -> tuple[str, str]:
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return "", ""
    path = tool_input.get("file_path") or tool_input.get("path")
    content = tool_input.get("content") or tool_input.get("new_string") or ""
    return (path if isinstance(path, str) else "", content if isinstance(content, str) else "")


def _is_test_path(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        any(part in normalized for part in ("/test/", "/tests/", "/spec/", "/specs/", "/__tests__/"))
        or name.startswith("test_")
        or ".test." in name
        or ".spec." in name
        or name.endswith("_test.py")
    )


def _audit_gate_reminder(path: str) -> str:
    """The audit-before-commit gate, worded per docs/policies/audit-trigger.md.

    Carries the same policy clauses as the shell hook's gate paragraph
    (agent-packs/claude-code/hooks/posttooluse_code_construction_reminder.sh);
    formatting differs by host, so the two are NOT byte-identical and no test
    claims they are. What IS mechanically asserted is that every sensitivity
    category named by the policy's gate-a-tokens marker appears in the text BOTH
    adapters actually emit — see
    test_audit_gate_sensitivity_list_does_not_drift_between_adapters.

    That guarantee was earned the hard way: the first version of the drift test
    grepped source files against a hardcoded tuple, and deleting `auth` from this
    string still passed, because the word also appears in SECURITY_SIGNAL above.

    The pointer degrades to an explicit marker rather than a fabricated path: an
    absolute-looking path that does not resolve is the same dangling-pointer
    failure this policy work exists to remove.
    """
    root = os.environ.get("AQG_ROOT") or str(Path(__file__).resolve().parent.parent)
    policy = Path(root) / "docs" / "policies" / "audit-trigger.md"
    policy_ref = str(policy) if policy.is_file() else "UNRESOLVED (set AQG_ROOT; policy not found)"
    # Same treatment the shell hook gives its path: a filename is
    # attacker-influenceable and goes straight into model context, where a
    # backtick can spill the surrounding text and a newline reads as a new
    # instruction line.
    safe_path = re.sub(r"[\r\n`]", "", path)
    return (
        f"AQG audit-before-commit gate: code edit noted ({safe_path})."
        " | SKIP if trivial / mechanical / a test\u00b7type\u00b7lint settles it / already"
        " audited \u2014 audit is the exception, not the reflex."
        " | EXCEPT auth, permissions, crypto, secrets, trust-boundary input, data model,"
        " CI/deploy, install integrity, irreversible, cross-repo: deep regardless of size."
        " The sensitivity list at the policy path is authoritative over this abbreviation"
        " and outranks SKIP."
        " | Otherwise /audit ONCE before committing (at most one per change)."
        f" | Ladder + full list: {policy_ref}"
    )


def _post_tool_use(payload: dict[str, object]) -> int:
    path, content = _tool_file(payload)
    reminders: list[str] = []
    suffix = Path(path).suffix.lower() if path else ""
    if suffix in CODE_EXTENSIONS:
        if _is_test_path(path):
            reminders.append(
                "AQG reminder: invoke aqg-test-quality-review after writing tests; assert behavior, boundaries, isolation, and avoid weakened/skipped checks."
            )
        else:
            reminders.append(
                "AQG reminder: continue through aqg-code-construction (pattern mining, behavior lock, thin slice, verification, 5-axis self-review)."
            )
            reminders.append(_audit_gate_reminder(path))
    if SECURITY_SIGNAL.search(path + "\n" + content):
        reminders.append(
            "AQG reminder: this edit has security-sensitive signals; invoke aqg-security-review and run the repository SAST check before commit."
        )
    normalized = path.replace("\\", "/")
    if re.search(r"(?:^|/)skills/aqg-[^/]+/(?:SKILL\.md|skill\.template\.json|agents/openai\.yaml)$", normalized):
        reminders.append(
            "AQG reminder: an AQG skill registration file changed; invoke aqg-skill-validator and verify cross-cutting registration."
        )
    _emit({"additional_context": "\n".join(reminders)} if reminders else {})
    return 0


def _post_tool_failure(payload: dict[str, object]) -> int:
    if payload.get("tool_name") not in {"Shell", "Bash"}:
        _emit({})
        return 0
    _emit(
        {
            "additional_context": (
                "AQG reminder: the shell command failed. Invoke aqg-systematic-debugging: "
                "record the exact symptom and command, reproduce minimally, trace the boundary, "
                "test one hypothesis at a time, then rerun the failing check plus a regression check."
            )
        }
    )
    return 0


def _wip(mode: str, payload: dict[str, object], aqg_root: Path, project: Path) -> str:
    script = aqg_root / "scripts" / "wip_checkpoint.py"
    event = dict(payload)
    event.setdefault("cwd", str(project))
    if "session_id" not in event and isinstance(payload.get("conversation_id"), str):
        event["session_id"] = payload["conversation_id"]
    env = os.environ.copy()
    env["AQG_HOOK_PROJECT_DIR"] = str(project)
    try:
        proc = subprocess.run(
            [sys.executable, str(script), mode],
            cwd=project,
            input=json.dumps(event),
            text=True,
            capture_output=True,
            errors="replace",
            timeout=15,
            env=env,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _find_bash() -> str | None:
    """The same lookup `agent_client_aqg_hook.py` and `qoder_hook_adapter.py` do.

    `shutil.which` alone finds nothing on Windows, where bash ships beside git
    rather than on PATH -- and CodeBuddy and WorkBuddy AI are hosts whose own
    installers branch on `os.name == "nt"`, so "no bash" there is a real
    configuration, not a hypothetical one.
    """
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            git_root = Path(git).resolve().parent.parent
            for candidate in (git_root / "bin" / "bash.exe", git_root / "usr" / "bin" / "bash.exe"):
                if candidate.is_file():
                    return str(candidate)
    return shutil.which("bash")


def _trigger_update_check(aqg_root: Path) -> None:
    """Start the managed update check in the background and come straight back.

    Cursor, CodeBuddy, WorkBuddy AI and Kimi Code mount ONE command per lifecycle
    event -- this adapter -- instead of naming hook scripts in their config, so
    the trigger cannot be a row in an installer's hook table the way it is on
    Claude Code. This function is the only place their session start passes
    through.

    It runs the same `sessionstart_update_check.sh` every other host runs rather
    than re-deriving the spawn here. That script's rules were paid for once
    already -- reduced environment, `nohup` and not `setsid` (util-linux, absent
    on macOS), nothing on stdout, always exit 0 -- and a second copy of them is a
    second thing to get wrong.

    NOT waited on, and the arithmetic is why. This adapter is ONE command for the
    whole event, and the rest of it already spends up to 20s on preflight and 15s
    on WIP recovery against host budgets of 45s (Cursor) and 30s (the work
    clients). A bounded wait here -- even a short one -- is spent out of a budget
    that is already tight, and being killed mid-adapter means the host gets no
    JSON at all. `Popen` with its own session costs a fork: the launcher does its
    own backgrounding, so nothing is gained by watching it do it.

    Every failure is silent, deliberately. A session must never hear about this
    -- least of all on stdout, which is the host's JSON channel.
    """
    script = aqg_root / "agent-packs" / "claude-code" / "hooks" / "sessionstart_update_check.sh"
    bash = _find_bash()
    if bash is None or not script.is_file():
        return
    env = os.environ.copy()
    env["AQG_ROOT"] = str(aqg_root)
    try:
        subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            [bash, str(script)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
    except OSError:
        return


def _session_start(payload: dict[str, object], aqg_root: Path, project: Path | None) -> int:
    # Before the workspace check below, not after: the update check needs no
    # project, and a session opened outside a repo is still a session that should
    # find out its AQG checkout is stale.
    _trigger_update_check(aqg_root)
    if project is None or not project.is_dir():
        _emit({})
        return 0
    context: list[str] = []
    preflight = aqg_root / "skills" / "aqg-startup-preflight" / "scripts" / "aqg_preflight.py"
    is_git_repo = (project / ".git").exists()
    if not is_git_repo:
        try:
            is_git_repo = _run(["git", "rev-parse", "--git-dir"], cwd=project, timeout=5).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            is_git_repo = False
    if is_git_repo:
        try:
            proc = _run([sys.executable, str(preflight), "--repo", str(project)], cwd=project, timeout=20)
            lines = (proc.stdout + proc.stderr).splitlines()
            context.extend(lines[:MAX_CONTEXT_LINES])
            if len(lines) > MAX_CONTEXT_LINES:
                context.append(f"[AQG preflight truncated: {len(lines)} lines]")
            if proc.returncode != 0:
                context.append(f"[AQG preflight exited {proc.returncode}; state unknown]")
        except (OSError, subprocess.TimeoutExpired) as exc:
            context.append(f"[AQG preflight unavailable: {type(exc).__name__}]")
    recovered = _wip("recover", payload, aqg_root, project)
    if recovered:
        context.append(recovered)
    context.append(
        "AQG startup note: this preflight covers only the current workspace; production, secrets, and Owner/admin actions remain separate authorization gates."
    )
    _emit({"additional_context": "\n".join(context)})
    return 0


def _pre_compact(payload: dict[str, object], aqg_root: Path, project: Path) -> int:
    _wip("save", payload, aqg_root, project)
    _emit(
        {
            "user_message": (
                "AQG saved the current WIP checkpoint. If the task is unfinished, use "
                "aqg-session-handoff; if it is finished, use aqg-evidence-closeout before ending."
            )
        }
    )
    return 0


def _stop(payload: dict[str, object], aqg_root: Path, project: Path) -> int:
    _wip("save", payload, aqg_root, project)
    if payload.get("loop_count", 0) == 0:
        _emit(
            {
                "followup_message": (
                    "AQG stop gate: before ending, invoke aqg-evidence-closeout and report fresh verification, durable state, untouched boundaries, and blockers. "
                    "If the task is unfinished and context must move, invoke aqg-session-handoff instead of improvising a handoff."
                )
            }
        )
    else:
        _emit({})
    return 0


def _user_prompt_submit(payload: dict[str, object]) -> int:
    prompt = payload.get("prompt") or payload.get("user_prompt") or payload.get("message")
    if not isinstance(prompt, str) or not prompt:
        _emit({})
        return 0
    if not re.search(r"hand[\s_-]?(off|over)|交接|交班|next\s+session|下个\s*session", prompt, re.IGNORECASE):
        _emit({})
        return 0
    note = (
        "AQG handoff note: the user prompt appears to request a session handoff. "
        "If they are asking you to produce a handoff for the next session, invoke "
        "aqg-session-handoff and validate the 8-section handoff instead of improvising it."
    )
    _emit(
        {
            "additional_context": note,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": note,
            },
        }
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Cursor AQG hook adapter")
    parser.add_argument(
        "event",
        choices=(
            "sessionStart",
            "preToolUse",
            "postToolUse",
            "postToolUseFailure",
            "preCompact",
            "stop",
            "userPromptSubmit",
        ),
    )
    parser.add_argument("--aqg-root", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    aqg_root = args.aqg_root.expanduser().resolve()
    try:
        payload = _read_payload()
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
        if args.event == "preToolUse":
            return _deny(f"AQG hook input was invalid ({type(exc).__name__}); blocking because this gate is fail-closed.")
        _emit({})
        return 0
    if args.event == "sessionStart":
        project = _project_dir(payload, allow_cwd_fallback=False)
        return _session_start(payload, aqg_root, project)
    project = _project_dir(payload) or Path.cwd().resolve()
    if args.event == "preToolUse":
        return _pre_tool_use(payload, aqg_root, project)
    if args.event == "postToolUse":
        return _post_tool_use(payload)
    if args.event == "postToolUseFailure":
        return _post_tool_failure(payload)
    if args.event == "preCompact":
        return _pre_compact(payload, aqg_root, project)
    if args.event == "userPromptSubmit":
        return _user_prompt_submit(payload)
    return _stop(payload, aqg_root, project)


if __name__ == "__main__":
    raise SystemExit(main())
