"""Doctor coverage for the three channels that carry AQG discipline to a model.

An AQG install can be 100% green on every existing doctor check and still deliver
nothing to the agent. That is not hypothetical — it happened, and it is what
started this work: one machine reported 73 PASS / 0 WARN / 0 FAIL while its
`CLAUDE.md` carried no AQG rules block at all, so the only always-resident channel
was empty and nobody could tell.

Three channels reach a model, and doctor checked none of them end to end:

1. **skills** — already covered by `check_skill_install`.
2. **rules block** — the always-resident text in `~/.claude/CLAUDE.md`,
   `~/.codex/AGENTS.md`, `.cursor/rules/aqg.mdc`. Installed by hand for Claude and
   Codex, so it is the one most likely to be missing, and nothing verified it.
3. **hook model-visibility** — a PostToolUse hook that only writes stderr is
   invisible to Claude. Definitions can be perfectly installed and still deliver
   nothing.

Plus a fourth failure mode the slice-5 audit surfaced: a rules block that IS
present but carries a **retired framing**. Updating the template does not reach
anyone who already installed it, so a stale block is silently wrong rather than
visibly absent.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import aqg_doctor as doc  # noqa: E402

BLOCK_HEADING = "## Agent Quality Gates (AQG) engineering discipline"


def test_missing_rules_block_is_reported_not_silently_green() -> None:
    """The exact failure that made a 73-PASS install deliver nothing."""
    result = doc.check_rules_block_text("claude", "# my notes\n\nnothing here\n")
    assert result.status == "WARN"
    assert "no AQG rules block" in result.detail
    assert result.fix


def test_present_and_current_block_passes() -> None:
    text = (
        f"{BLOCK_HEADING}\n\nCall `aqg-code-construction` before writing code; "
        "depth per docs/policies/audit-trigger.md.\n"
    )
    result = doc.check_rules_block_text("claude", text)
    assert result.status == "PASS", result.detail


def test_stale_framing_is_reported_even_though_the_block_is_present() -> None:
    """Present-but-outdated is the harder failure: it looks installed.

    Anyone who installed the rules template before 2026-08-11 still has the
    retired main-path/fallback framing naming `audit-self-routing.md`, a file no
    installer has ever shipped. Re-publishing the template does not reach them.
    """
    text = (
        f"{BLOCK_HEADING}\n\n"
        "- **Depth**: main path = the `audit-self-routing.md` decision tree; "
        "fallback = `aqg-phase-transition`.\n"
    )
    result = doc.check_rules_block_text("claude", text)
    assert result.status == "WARN"
    assert "stale" in result.detail.lower()
    assert "audit-self-routing" in result.detail
    assert result.fix


def test_absent_host_file_is_not_a_failure() -> None:
    """A machine with no Codex installed must not be told its Codex is broken."""
    result = doc.check_rules_block(Path("/nonexistent-host-root/AGENTS.md"), "codex")
    assert result.status == "PASS"
    assert "not installed" in result.detail.lower()


def test_hook_that_only_writes_stderr_is_reported_as_invisible() -> None:
    """Definitions installed, delivery still zero — the Claude-side defect."""
    result = doc.check_hook_model_visibility_text(
        "posttooluse_example_reminder.sh",
        'echo "[aqg] reminder: do the thing" >&2\nexit 0\n',
    )
    assert result.status == "WARN"
    assert "stderr" in result.detail.lower()
    assert result.fix


def test_hook_emitting_additional_context_passes() -> None:
    result = doc.check_hook_model_visibility_text(
        "posttooluse_example_reminder.sh",
        'say "hi"\naqg_flush_context\nexit 0\n',
    )
    assert result.status == "PASS", result.detail


def test_every_shipped_posttooluse_hook_is_model_visible() -> None:
    """Guards the real tree, not a fixture: all five must stay visible."""
    hooks = sorted((REPO / "agent-packs" / "claude-code" / "hooks").glob("posttooluse_*.sh"))
    assert len(hooks) >= 5, f"expected the five PostToolUse hooks, found {len(hooks)}"
    for hook in hooks:
        result = doc.check_hook_model_visibility_text(hook.name, hook.read_text(encoding="utf-8"))
        assert result.status == "PASS", f"{hook.name}: {result.detail}"


def test_untrusted_rules_content_cannot_break_the_one_line_contract() -> None:
    """Doctor prints one line per check; file content is untrusted input.

    Same class as GD-11 in the robustness suite — a rules file is user-editable,
    so a control character or newline in it must not escape into the report.
    """
    text = f"{BLOCK_HEADING}\n\n- Depth: `audit-self-routing.md`\x1b[31m\nsecond line\n"
    result = doc.check_rules_block_text("claude", text)
    assert "\n" not in result.detail
    assert "\x1b" not in result.detail


def test_installed_host_with_a_deleted_rules_file_is_not_reported_as_healthy(tmp_path) -> None:
    """The irony the audit caught: not-installed=PASS masked a broken install.

    `~/.cursor` exists on the author's machine while its rules file does not, and
    the first version of this check reported PASS "cursor not installed" — a false
    green on a real host, which is precisely the failure mode this slice exists to
    remove. Absence of the HOST is fine; absence of the rules file ON a present
    host is not.

    The example moved from cursor to claude when Cursor's user-scope channel was
    recognised as UI-managed (see UI_MANAGED_USER_RULES). The property under test
    is unchanged and is what matters: a present host missing a rules file AQG can
    write must not read green.
    """
    host_root = tmp_path / ".claude"
    host_root.mkdir(parents=True)
    result = doc.check_rules_block(host_root / "CLAUDE.md", "claude", host_root=host_root)
    assert result.status == "WARN", result.detail
    assert "installed" in result.detail.lower()


def test_truly_absent_host_still_passes(tmp_path) -> None:
    result = doc.check_rules_block(
        tmp_path / ".cursor" / "rules" / "aqg.mdc", "cursor", host_root=tmp_path / ".cursor"
    )
    assert result.status == "PASS"


def test_heading_without_a_block_body_is_not_a_block() -> None:
    """A stub heading delivers nothing; only its presence was being checked."""
    result = doc.check_rules_block_text("claude", f"{BLOCK_HEADING}\n")
    assert result.status == "WARN"
    assert "empty" in result.detail.lower() or "stub" in result.detail.lower()


def test_visibility_keyword_in_a_comment_does_not_count_as_delivery() -> None:
    """A hook that only *mentions* the mechanism still delivers nothing."""
    result = doc.check_hook_model_visibility_text(
        "posttooluse_example_reminder.sh",
        '# note: this hook could emit additionalContext one day\necho "reminder" >&2\nexit 0\n',
    )
    assert result.status == "WARN", result.detail


def test_doctor_verdict_agrees_with_actually_running_the_hook() -> None:
    """Cross-check the heuristic against real behaviour, not against itself.

    The shipped-hooks test calls the same function it is meant to validate, so a
    wrong heuristic would agree with itself and pass. This runs each hook and
    compares doctor's verdict to whether stdout really carries a JSON envelope.
    """
    import json
    import subprocess

    hooks_dir = REPO / "agent-packs" / "claude-code" / "hooks"
    fired = 0
    for hook in sorted(hooks_dir.glob("posttooluse_*.sh")):
        text = hook.read_text(encoding="utf-8")
        verdict = doc.check_hook_model_visibility_text(hook.name, text)
        proc = subprocess.run(
            ["bash", str(hook)],
            input=json.dumps({"tool_input": {"file_path": "/repo/src/auth_login.py"}}),
            text=True,
            capture_output=True,
            env={**__import__("os").environ, "AQG_ROOT": str(REPO)},
            check=False,
        )
        emitted = proc.stdout.strip().startswith("{")
        if not emitted and not proc.stderr.strip():
            continue  # hook did not fire for this payload; nothing to compare
        fired += 1
        assert verdict.status == ("PASS" if emitted else "WARN"), (
            f"{hook.name}: doctor says {verdict.status} but stdout envelope={emitted}"
        )
    assert fired >= 2, f"fixture exercised too few hooks ({fired})"


# --- Channel 2, source side: the templates users install FROM -------------------

# Paths are repo-relative. The two `examples/` templates are copied verbatim into
# a user's always-resident rules block; the READMEs are the project's entry
# document and were shipping the retired matrix in BOTH languages while the same
# commit banned it from the templates — a guard scoped to one surface only.
RULES_TEMPLATES = (
    "examples/aqg-claude-rules.example.md",
    "examples/aqg-codex-agents.example.md",
    "README.md",
    "README.zh-CN.md",
)

# Framings the policy retired. Each is a claim the templates used to restate, and
# each directly contradicts `docs/policies/audit-trigger.md` at HEAD.
RETIRED_FRAMINGS = (
    # Depth is keyed on stakes ALONE (policy: "Phase does not affect depth").
    # A template restating a phase-dependent matrix hands the agent a second,
    # contradictory depth authority — the exact drift that created this policy.
    ("Phase × Stakes", "phase-dependent depth matrix"),
    ("Phase x Stakes", "phase-dependent depth matrix"),
    # `aqg-phase-transition` is a timing signal, not a "fallback" behind a
    # main path — the main path (`audit-self-routing.md`) never existed.
    ("(fallback layer)", "retired main-path/fallback framing"),
)


def test_shipped_rules_templates_do_not_restate_retired_framings() -> None:
    """The templates are copied verbatim into a user's always-resident block.

    Doctor's stale-block check cannot stand in for this: it matches only
    `audit-self-routing`, so both framings below passed it while contradicting
    the policy. Verified, not assumed — the pre-fix template scored PASS against
    `check_rules_block_text`.
    """
    offenders: list[str] = []
    for name in RULES_TEMPLATES:
        path = REPO / name
        assert path.is_file(), f"shipped rules template missing: {name}"
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for needle, why in RETIRED_FRAMINGS:
                if needle in line:
                    offenders.append(f"{name}:{lineno}: {why} — {line.strip()[:70]}")
    assert not offenders, (
        "shipped rules templates restate framings the policy retired; a user who "
        "installs these gets a rulebook that contradicts "
        "docs/policies/audit-trigger.md:\n" + "\n".join(offenders)
    )


# --- Channel 3, failure mode 2: hooks installed but env-gated OFF -------------
#
# Every AQG hook command in settings.json begins with
#     if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi
# so an unset AQG_ROOT turns every one of them into a silent no-op — including the
# PreToolUse secret scan, which is a BLOCKING security gate.
#
# Doctor could not report this: resolve_aqg_root() falls back to the doctor
# script's own location when the env var is empty, so `aqg_root` is never None
# and the FAIL branch in check_claude_hooks is unreachable. The result is the
# exact green-but-broken shape this module exists to catch — doctor prints
# "PASS aqg_root" while every hook is dead.
#
# The check therefore reads the REAL environment value, never the resolved
# fallback: the fallback is what saves doctor itself and hides the hooks.

GUARDED_SETTINGS = (
    '{"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command",'
    ' "command": "if [ -z \\"${AQG_ROOT:-}\\" ]; then exit 0; fi; bash'
    ' \\"$AQG_ROOT/agent-packs/claude-code/hooks/pretooluse_secret_scan.sh\\""}]}]}}'
)


def _make_checkout(root: Path, *refs: str) -> Path:
    """A root that can actually run the commands: VERSION sentinel + the scripts."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    for ref in refs or ("agent-packs/claude-code/hooks/pretooluse_secret_scan.sh",):
        target = root / ref
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    return root


def test_env_guarded_hooks_with_unset_root_is_a_failure_not_a_pass() -> None:
    """The whole point: installed hooks + no env var = every hook silently off."""
    result = doc.check_hook_env_guard_text(GUARDED_SETTINGS, None)
    assert result.status == "FAIL", result
    assert "no-op" in result.detail or "silent" in result.detail.lower(), result


def test_empty_string_root_is_treated_as_unset() -> None:
    """`export AQG_ROOT=` passes the shell guard's -z test exactly like unset."""
    result = doc.check_hook_env_guard_text(GUARDED_SETTINGS, "   ")
    assert result.status == "FAIL", result


def test_root_pointing_at_a_nonexistent_path_also_fails(tmp_path: Path) -> None:
    """The guard only tests -z, so a stale path passes it and then breaks the bash call."""
    result = doc.check_hook_env_guard_text(GUARDED_SETTINGS, str(tmp_path / "gone"))
    assert result.status == "FAIL", result


def test_root_pointing_at_a_real_checkout_passes(tmp_path: Path) -> None:
    _make_checkout(tmp_path)
    result = doc.check_hook_env_guard_text(GUARDED_SETTINGS, str(tmp_path))
    assert result.status == "PASS", result


def test_no_env_guarded_hooks_means_nothing_to_gate() -> None:
    """A settings.json without AQG hooks must not be dragged into a FAIL."""
    result = doc.check_hook_env_guard_text('{"hooks": {}}', None)
    assert result.status == "PASS", result


def test_unparseable_settings_does_not_crash_the_doctor() -> None:
    result = doc.check_hook_env_guard_text("{ not json", None)
    assert result.status in {"PASS", "WARN"}, result


def test_env_declared_in_settings_counts_as_reachable(tmp_path: Path) -> None:
    """The repair the installer writes must satisfy the check that demands it.

    Regression for a bricking loop caught in audit aud_GkYkV_EMcdUnKQ0U: the
    installer writes env.AQG_ROOT into settings.json, but this check read only
    os.environ. A doctor run as a subprocess (which is exactly how the Decision
    Engine gates its install) never sees settings.json's env block, so the fix was
    invisible to the checker: FAIL -> gate dies -> re-run install -> writes the
    same env -> FAIL again, with no way out.

    Either source makes the hooks work — a real exported variable is inherited by
    the hook process, and settings.json's env block is injected into it by the
    host — so either must satisfy the check.
    """
    _make_checkout(tmp_path, "x.sh")
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/x.sh\\""}]}]}}' % tmp_path
    )
    result = doc.check_hook_env_guard_text(settings, None)
    assert result.status == "PASS", result


def test_a_settings_env_pointing_nowhere_still_fails(tmp_path: Path) -> None:
    """Accepting the settings.json source must not weaken the validity test."""
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/x.sh\\""}]}]}}' % (tmp_path / "gone")
    )
    assert doc.check_hook_env_guard_text(settings, None).status == "FAIL"


def test_a_version_file_alone_does_not_prove_the_hooks_can_run(tmp_path: Path) -> None:
    """PASS must mean the secret scan will actually execute, not just that a path exists.

    Audit aud_GkYkV_EMcdUnKQ0U (sol f2, blocking): a VERSION sentinel is a proxy for
    a checkout, not a test of one. A stale, partial or unrelated directory can carry
    VERSION while the `bash "$AQG_ROOT/agent-packs/.../pretooluse_secret_scan.sh"`
    the hook then runs points at nothing — doctor would report PASS while the
    BLOCKING security gate silently does not run.
    """
    (tmp_path / "VERSION").write_text("0.0.0\n", encoding="utf-8")  # sentinel, but no hooks
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/agent-packs/claude-code/hooks/'
        'pretooluse_secret_scan.sh\\""}]}]}}' % tmp_path
    )
    result = doc.check_hook_env_guard_text(settings, None)
    assert result.status == "FAIL", result
    assert "pretooluse_secret_scan.sh" in result.detail, result


def test_a_root_whose_hook_scripts_exist_passes(tmp_path: Path) -> None:
    """The positive half: a root that really can run the commands is accepted."""
    (tmp_path / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    script = tmp_path / "agent-packs" / "claude-code" / "hooks" / "pretooluse_secret_scan.sh"
    script.parent.mkdir(parents=True)
    script.write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/agent-packs/claude-code/hooks/'
        'pretooluse_secret_scan.sh\\""}]}]}}' % tmp_path
    )
    assert doc.check_hook_env_guard_text(settings, None).status == "PASS"


def test_two_sources_that_disagree_are_reported_not_silently_ranked(tmp_path: Path) -> None:
    """Which root the hook process actually receives is not knowable from here.

    Audit aud_7nsuQouSm96_9rt9 (opus + google, blocking): the first version picked
    the environment over settings.json. If the exported value is a healthy checkout
    and the settings.json one is broken — or the host prefers settings.json over the
    inherited variable — doctor validates the root the hooks do NOT use and reports
    PASS while they are dead. Neither ranking is provable from outside the host, so
    a disagreement is itself the finding.
    """
    good = _make_checkout(tmp_path / "good")
    other = tmp_path / "other"
    other.mkdir()
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/agent-packs/claude-code/hooks/'
        'pretooluse_secret_scan.sh\\""}]}]}}' % other
    )
    result = doc.check_hook_env_guard_text(settings, str(good))
    assert result.status == "FAIL", result
    assert "disagree" in result.detail.lower(), result


def test_two_sources_that_agree_still_pass(tmp_path: Path) -> None:
    root = _make_checkout(tmp_path)
    settings = (
        '{"env": {"AQG_ROOT": "%s"}, "hooks": {"PreToolUse": [{"matcher": "Bash",'
        ' "hooks": [{"type": "command", "command": "if [ -z \\"${AQG_ROOT:-}\\" ];'
        ' then exit 0; fi; bash \\"$AQG_ROOT/agent-packs/claude-code/hooks/'
        'pretooluse_secret_scan.sh\\""}]}]}}' % root
    )
    assert doc.check_hook_env_guard_text(settings, str(root)).status == "PASS"


def test_the_missing_block_fix_names_a_command_that_can_actually_fix_it(tmp_path) -> None:
    """A fix line that no script implements leaves the user to hand-edit.

    Claude Code and Codex now have an installer for this channel; before it
    existed the advice was "append the block from the template", which is what
    the AI_SETUP hand-edit procedure was for — and where its drift came from.
    """
    for host, rules_name, client in (
        ("claude", "CLAUDE.md", "claude-code"),
        ("codex", "AGENTS.md", "codex"),
    ):
        host_root = tmp_path / host
        host_root.mkdir()
        result = doc.check_rules_block(host_root / rules_name, host, host_root=host_root)
        assert result.status == "WARN"
        assert "install_aqg_rules.py" in (result.fix or ""), (
            f"{host}: fix does not name the installer that writes this channel"
        )
        assert f"--client {client}" in (result.fix or "")


def test_every_rules_block_fix_command_can_actually_write_that_file(tmp_path) -> None:
    """A fix line that provably cannot fix the thing it is attached to.

    The first version of RULES_BLOCK_FIX_COMMANDS pointed Cursor at
    `install_cursor_support.py --apply --scope user`, but that installer calls
    _install_rule only under `--scope project` and prints "Cursor User Rules are
    UI-managed; no undocumented user rule file was written". Running it would
    leave the WARN exactly where it was — the same shape of dead advice this
    channel's installer exists to remove.
    """
    cursor_fix = doc.rules_block_fix("cursor")
    assert "--scope user" not in cursor_fix, (
        "cursor fix names a command that cannot write a user-scope rules file"
    )
    assert "UI" in cursor_fix or "ui-managed" in cursor_fix.lower(), (
        "cursor fix does not say why no command can do it"
    )


def test_a_host_whose_user_channel_aqg_cannot_write_is_not_a_standing_warn(
    tmp_path,
) -> None:
    """Cursor's User Rules are UI-managed, so the WARN could never be cleared.

    aqg_client_registry states the surface honestly — '.cursor/rules/aqg.mdc for
    project scope; user rules are UI-managed' — while doctor asserted a
    user-scope file should exist. A permanent unclearable WARN is what teaches
    people to stop reading doctor, so the two are reconciled in the registry's
    favour. A block that IS there is still checked for content.
    """
    cursor_root = tmp_path / ".cursor"
    cursor_root.mkdir()

    result = doc.check_rules_block(cursor_root / "rules" / "aqg.mdc", "cursor",
                                   host_root=cursor_root)

    assert result.status == "PASS"
    assert "ui-managed" in result.detail.lower() or "UI" in result.detail
    # Claude and Codex are unaffected: theirs is writable, so its absence stays a WARN.
    for host, name in (("claude", "CLAUDE.md"), ("codex", "AGENTS.md")):
        root = tmp_path / host
        root.mkdir()
        assert doc.check_rules_block(root / name, host, host_root=root).status == "WARN"


def test_a_cursor_block_that_does_exist_is_still_checked_for_content(tmp_path) -> None:
    """Not-a-WARN when absent must not become not-checked when present."""
    cursor_root = tmp_path / ".cursor"
    rules = cursor_root / "rules"
    rules.mkdir(parents=True)
    (rules / "aqg.mdc").write_text("# not the block\n", encoding="utf-8")

    result = doc.check_rules_block(rules / "aqg.mdc", "cursor", host_root=cursor_root)

    assert result.status == "WARN"
