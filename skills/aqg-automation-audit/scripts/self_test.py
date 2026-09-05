#!/usr/bin/env python3
"""Minimal self-test for inventory.py + overlap_check.py.

Uses a temp dir as fake skill_root with deterministic fixture content, so the
test does not depend on the host's ~/.claude state. Mirrors the structure of
`aqg-evidence-closeout/scripts/self_test.py` (run as a script under scripts/).
"""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

import inventory
import overlap_check

# Skip the slow `claude mcp list` subprocess for all tests that don't exercise
# the MCP redaction path. test_c1_secret_in_mcp_raw_and_stderr_redacted handles
# its own subprocess patching and temporarily unsets this flag.
os.environ["AQG_SKIP_MCP"] = "1"


def _build_fake_skill_root(root: Path) -> None:
    """Create a minimal fake ~/.claude with known overlap signals."""
    # settings.json: 1 user hook + 4 env vars (1 missing required AQG_ROOT? no, present)
    settings = {
        "enabledPlugins": {"sample-plugin@sample-mp": True},
        "env": {
            "AQG_ROOT": "/path/to/aqg",
            "SLASH_COMMAND_TOOL_CHAR_BUDGET": "20000",
        },
        "hooks": {
            "SessionStart": [{"matcher": "*", "hooks": [{"type": "command", "command": "echo hi"}]}]
        },
    }
    (root / "settings.json").write_text(json.dumps(settings), encoding="utf-8")

    # plugins/installed_plugins.json
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(json.dumps({
        "version": "1",
        "plugins": {
            "sample-plugin@sample-mp": [
                {
                    "installPath": str(plugins_dir / "cache" / "sample-mp" / "sample-plugin" / "1.0"),
                    "version": "1.0",
                    "scope": "user",
                    "installedAt": "2026-01-01T00:00:00Z",
                    "lastUpdated": "2026-01-01T00:00:00Z",
                    "gitCommitSha": "abc123",
                }
            ],
        },
    }), encoding="utf-8")

    # plugins/cache/sample-mp/sample-plugin/1.0/hooks/hooks.json — blocking + noise hooks
    plugin_root = plugins_dir / "cache" / "sample-mp" / "sample-plugin" / "1.0"
    hooks_dir = plugin_root / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    (hooks_dir / "hooks.json").write_text(json.dumps({
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [{"type": "command", "command": "block-bash.sh"}]},
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "block-edit.sh"}]},
            ],
            "PostToolUse": [
                {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "post-edit-noise.sh"}]},
            ],
            "UserPromptSubmit": [
                {"matcher": "*", "hooks": [{"type": "command", "command": "inject-banner.sh"}]},
            ],
        }
    }), encoding="utf-8")

    # plugins/cache/sample-mp/sample-plugin/1.0/skills/conflicting-skill/SKILL.md — verification keyword
    skills_dir = plugin_root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    skill_dir = skills_dir / "conflicting-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: conflicting-skill\n"
        "description: Verification routing for tasks; competes with closeout flow.\n"
        "---\n\n"
        "# Body\n", encoding="utf-8"
    )

    # plugins/cache/sample-mp/sample-plugin/1.0/skills/.disabled-by-eaf/SKILL.md — already disabled
    disabled_skill = skills_dir / "neutralized.disabled-by-eaf"
    disabled_skill.mkdir(parents=True, exist_ok=True)
    (disabled_skill / "SKILL.md").write_text(
        "---\nname: neutralized\ndescription: Already disabled.\n---\n", encoding="utf-8"
    )

    # ~/.claude/skills/ — empty user dir
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _run_inventory(skill_root: Path) -> dict:
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["inventory.py", "--skill-root", str(skill_root), "--json"]
        with redirect_stdout(out):
            rc = inventory.main()
    finally:
        sys.argv = saved
    assert rc == 0, f"inventory exit={rc}"
    return json.loads(out.getvalue())


def test_inventory_captures_five_data_types() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        inv = _run_inventory(root)
    assert inv["schema_version"] == 1
    assert inv["settings"]["env_count"] == 2
    assert "AQG_ROOT" in inv["settings"]["env_keys"]
    assert inv["plugins"]["count"] == 1
    assert inv["hooks"]["plugin_count"] == 4  # PreToolUse×2 + PostToolUse×1 + UserPromptSubmit×1
    assert inv["skills"]["counts"]["plugin"] == 1
    assert inv["skills"]["counts"]["disabled"] == 1


def test_overlap_check_flags_known_signals() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    by_severity = {}
    for f in findings:
        by_severity.setdefault(f["severity"], []).append(f)

    # 1. PreToolUse:Bash flagged blocking
    bash_blocks = [f for f in findings
                   if f["category"] == "plugin_hook"
                   and "PreToolUse" in f["item"]
                   and f.get("matcher") == "Bash"]
    assert len(bash_blocks) >= 1, "PreToolUse:Bash must flag blocking"
    assert bash_blocks[0]["severity"] == "blocking"
    assert bash_blocks[0]["overlap_with"] == "aqg-code-construction"

    # 2. PostToolUse:Edit flagged noise
    edit_noise = [f for f in findings
                  if f["category"] == "plugin_hook"
                  and "PostToolUse" in f["item"]]
    assert any(f["severity"] == "noise" for f in edit_noise), "PostToolUse must flag noise"

    # 3. UserPromptSubmit flagged noise
    ups_noise = [f for f in findings
                 if "UserPromptSubmit" in f["item"]]
    assert any(f["severity"] == "noise" for f in ups_noise), "UserPromptSubmit must flag noise"

    # 4. skill description "verification" flagged silent
    silent_skills = [f for f in findings
                     if f["category"] == "skill_description"
                     and "conflicting-skill" in f["item"]]
    assert len(silent_skills) >= 1, "verification keyword must flag silent"
    assert silent_skills[0]["severity"] == "silent"

    # 5. disabled skill surfaced as cosmetic
    disabled = [f for f in findings
                if f["category"] == "disabled_skill"
                and "neutralized" in f["item"]]
    assert len(disabled) == 1, "disabled skill must appear in findings"
    assert disabled[0]["severity"] == "cosmetic"


def test_overlap_check_flags_missing_required_env() -> None:
    """If AQG_ROOT not in env, flag blocking."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        # Strip AQG_ROOT
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["env"].pop("AQG_ROOT", None)
        sp.write_text(json.dumps(s), encoding="utf-8")
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    aqg_root_finding = [f for f in findings
                        if f["category"] == "env" and f["item"] == "AQG_ROOT"]
    assert len(aqg_root_finding) == 1
    assert aqg_root_finding[0]["severity"] == "blocking"


def test_overlap_check_flags_missing_recommended_env() -> None:
    """If a recommended env var (AQG_METRICS) is not set, flag noise (not blocking)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        # AQG_METRICS absent (fixture omits it)
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    metrics_finding = [f for f in findings
                       if f["category"] == "env" and f["item"] == "AQG_METRICS"]
    assert len(metrics_finding) == 1
    assert metrics_finding[0]["severity"] == "noise"


def test_inventory_redacts_env_values() -> None:
    """env_keys is a list; values must NEVER appear in inventory output."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        # Inject a sentinel value
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["env"]["TEST_SECRET"] = "supersecret_should_never_appear_in_output"
        sp.write_text(json.dumps(s), encoding="utf-8")
        inv = _run_inventory(root)
    text = json.dumps(inv)
    assert "supersecret_should_never_appear_in_output" not in text, \
        "env values must be redacted; sentinel leaked"
    assert "TEST_SECRET" in inv["settings"]["env_keys"]


# ---------------------------------------------------------------------------
# C1 (CRITICAL): secret leak via hook command / MCP raw / MCP stderr.
# The skill premise is "keys only, never a raw secret value" — but only the env
# field was redacted. Hook command strings, MCP raw, and MCP stderr carry tokens.
# ---------------------------------------------------------------------------

_CANARY = "ghp_FAKEFAKEFAKEFAKEFAKEFAKEFAKEFAKE0000"  # GitHub-PAT-shaped fake token


def test_c1_secret_in_hook_command_redacted() -> None:
    """A token planted in a plugin hook command must not appear in inventory JSON
    nor in overlap_check JSON (command_hint inherits the redaction)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        # Plant a canary token in a plugin hook command (a PreToolUse:Bash hook so
        # overlap_check echoes its command_hint).
        hooks_json = (root / "plugins" / "cache" / "sample-mp" / "sample-plugin"
                      / "1.0" / "hooks" / "hooks.json")
        data = json.loads(hooks_json.read_text())
        data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] = (
            f"curl -H 'Authorization: token {_CANARY}' https://api.example"
        )
        hooks_json.write_text(json.dumps(data), encoding="utf-8")
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    inv_text = json.dumps(inv)
    find_text = json.dumps(findings)
    assert _CANARY not in inv_text, "C1: canary token leaked into inventory JSON"
    assert _CANARY not in find_text, "C1: canary token leaked into overlap_check JSON"
    assert "<redacted>" in inv_text, "C1: redaction placeholder absent — was anything redacted?"


def test_c1_secret_in_user_hook_command_redacted() -> None:
    """User-level (settings.json) hook commands must also be redacted."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["hooks"]["SessionStart"][0]["hooks"][0]["command"] = (
            f"export AWS_TOKEN=AKIAIOSFODNN7EXAMPLE; echo {_CANARY}"
        )
        sp.write_text(json.dumps(s), encoding="utf-8")
        inv = _run_inventory(root)
    inv_text = json.dumps(inv)
    assert _CANARY not in inv_text, "C1: canary leaked via user hook command"
    assert "AKIAIOSFODNN7EXAMPLE" not in inv_text, "C1: AWS key leaked via user hook command"


def test_c1_secret_in_mcp_raw_and_stderr_redacted() -> None:
    """MCP `raw` (success path) and `stderr` (failure path) must be redacted at
    capture time, so the inventory JSON never carries a token. overlap_check then
    inherits the redacted text. Exercises the real _capture_mcp path by stubbing
    `shutil.which` + `subprocess.run` so no live `claude` CLI is needed."""
    from types import SimpleNamespace

    # Temporarily unset AQG_SKIP_MCP so _capture_mcp runs past the bypass
    # and we can exercise the real redaction logic with a patched subprocess.
    _saved_skip = os.environ.pop("AQG_SKIP_MCP", None)

    # Stub the module-level `shutil` / `subprocess` references inside inventory
    # (restore after) so no live `claude` CLI is needed and the real stdlib
    # modules are left untouched.
    orig_shutil, orig_subprocess = inventory.shutil, inventory.subprocess

    def _patch(run_fn):
        inventory.shutil = SimpleNamespace(which=lambda _name: "/usr/bin/claude")
        inventory.subprocess = SimpleNamespace(
            run=run_fn,
            TimeoutExpired=orig_subprocess.TimeoutExpired,
        )

    # --- success path: token in stdout line tail (-> entries[].raw) ---
    try:
        _patch(lambda *_a, **_k: SimpleNamespace(
            returncode=0,
            stdout=f"svc: stdio https://u:{_CANARY}@host - connected\n",
            stderr="",
        ))
        mcp_ok = inventory._capture_mcp(Path("/tmp"))
    finally:
        inventory.shutil, inventory.subprocess = orig_shutil, orig_subprocess
        if _saved_skip is not None:
            os.environ["AQG_SKIP_MCP"] = _saved_skip
    assert _CANARY not in json.dumps(mcp_ok), "C1: canary leaked via MCP raw (stdout)"
    # overlap_check inherits the redacted raw — the unhealthy rule must not re-expose it.
    findings = overlap_check.analyze({"mcp": mcp_ok, "hooks": {}, "skills": {}, "settings": {}})
    assert _CANARY not in json.dumps(findings), "C1: canary leaked via overlap MCP finding"

    # --- failure path: token in stderr (-> mcp.stderr) ---
    _saved_skip2 = os.environ.pop("AQG_SKIP_MCP", None)
    try:
        _patch(lambda *_a, **_k: SimpleNamespace(
            returncode=1, stdout="", stderr=f"auth failed: bearer {_CANARY}\n",
        ))
        mcp_fail = inventory._capture_mcp(Path("/tmp"))
    finally:
        inventory.shutil, inventory.subprocess = orig_shutil, orig_subprocess
        if _saved_skip2 is not None:
            os.environ["AQG_SKIP_MCP"] = _saved_skip2
    assert _CANARY not in json.dumps(mcp_fail), "C1: canary leaked via MCP stderr"


def test_c1_ordinary_command_not_over_redacted() -> None:
    """Ordinary hook commands must pass through unchanged (no over-redaction)."""
    plain = "python3 /path/to/hooks/hook.py --flag"
    assert inventory._redact(plain) == plain, \
        "C1: ordinary command was mangled by redaction"
    assert inventory._redact("uv run black --check .") == "uv run black --check ."


def test_c1_redact_helper_patterns() -> None:
    """Direct coverage of the redaction helper across required secret shapes."""
    r = inventory._redact
    assert _CANARY not in r(f"x {_CANARY} y")
    assert "AKIAIOSFODNN7EXAMPLE" not in r("aws AKIAIOSFODNN7EXAMPLE end")
    assert "sk-proj-" not in r("OPENAI=sk-proj-abcdefghijklmnopqrstuvwx") or \
        "<redacted>" in r("OPENAI=sk-proj-abcdefghijklmnopqrstuvwx")
    assert "xoxb-" not in r("slack xoxb-123456789012-abcdefghijkl") or \
        "<redacted>" in r("slack xoxb-123456789012-abcdefghijkl")
    # URL userinfo strip
    out = r("clone https://alice:hunter2@github.com/x/y.git")
    assert "hunter2" not in out and "alice" not in out
    assert "<redacted>@github.com" in out
    # generic token=... assignment
    assert "topsecretvalue123" not in r("API_KEY=topsecretvalue123 more")
    # bearer
    assert "abcdEFGHijklMNOP" not in r("-H 'Authorization: Bearer abcdEFGHijklMNOP'")


# ---------------------------------------------------------------------------
# C2: crash paths — fail-closed must cover OSError + wrong-shape JSON.
# ---------------------------------------------------------------------------


def _run_inventory_rc(skill_root: Path) -> tuple[int, str]:
    """Run inventory.main() capturing rc + stdout (no exception should escape)."""
    out = io.StringIO()
    saved = sys.argv
    try:
        sys.argv = ["inventory.py", "--skill-root", str(skill_root), "--json"]
        with redirect_stdout(out):
            rc = inventory.main()
    finally:
        sys.argv = saved
    return rc, out.getvalue()


def test_c2_settings_top_level_list_exit3() -> None:
    """settings.json with a top-level JSON list (wrong shape) → exit 3, no traceback."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        (root / "settings.json").write_text("[1, 2, 3]", encoding="utf-8")
        rc, _ = _run_inventory_rc(root)  # must NOT raise
    assert rc == 3, f"C2: top-level-list settings must exit 3, got {rc}"


def test_c2_env_as_list_exit3_or_clean() -> None:
    """`env` as a list (valid JSON, wrong shape) → no AttributeError; exit 3 or clean."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["env"] = ["AQG_ROOT", "FOO"]  # wrong: should be a dict
        sp.write_text(json.dumps(s), encoding="utf-8")
        rc, _ = _run_inventory_rc(root)  # must NOT raise AttributeError
    assert rc in (0, 3), f"C2: env-as-list must not crash; got rc={rc}"


def test_c2_non_dict_hook_entry_no_crash() -> None:
    """A non-dict hook entry must not crash (.get on a string/int)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["hooks"]["SessionStart"] = ["not-a-dict", 42]  # wrong entry shapes
        sp.write_text(json.dumps(s), encoding="utf-8")
        rc, _ = _run_inventory_rc(root)  # must NOT raise
    assert rc in (0, 3), f"C2: non-dict hook entry must not crash; got rc={rc}"


def test_c2_enabled_plugins_as_list_no_crash() -> None:
    """enabledPlugins as a list (wrong shape) must not crash inventory."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        sp = root / "settings.json"
        s = json.loads(sp.read_text())
        s["enabledPlugins"] = ["a@b", "c@d"]  # wrong: should be a dict
        sp.write_text(json.dumps(s), encoding="utf-8")
        rc, _ = _run_inventory_rc(root)
    assert rc in (0, 3), f"C2: enabledPlugins-as-list must not crash; got rc={rc}"


def test_c2_installed_plugins_entry_not_dict_no_crash() -> None:
    """installed_plugins.json plugin entries[0] non-dict must not crash."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        pj = root / "plugins" / "installed_plugins.json"
        data = json.loads(pj.read_text())
        data["plugins"]["sample-plugin@sample-mp"] = ["not-a-dict"]  # entries[0] is a str
        pj.write_text(json.dumps(data), encoding="utf-8")
        rc, _ = _run_inventory_rc(root)
    assert rc in (0, 3), f"C2: non-dict installed-plugin entry must not crash; got rc={rc}"


def test_c2_overlap_analyze_wrong_shape_no_crash() -> None:
    """overlap_check.analyze must tolerate wrong-shaped inventory sub-nodes."""
    bad = {
        "hooks": {"user": ["x"], "plugin": "nope"},  # user list-of-str, plugin a str
        "skills": {"plugin": "nope", "disabled": 5},
        "settings": {"env_keys": "AQG_ROOT"},  # str not list
        "mcp": [],  # wrong type
    }
    findings = overlap_check.analyze(bad)  # must NOT raise
    assert isinstance(findings, list)


# ---------------------------------------------------------------------------
# C3: description over-fire + truncation.
# ---------------------------------------------------------------------------


def _inv_with_plugin_skill(root: Path, name: str, description: str) -> dict:
    """Add a plugin skill with a given description and return the inventory."""
    skills_dir = (root / "plugins" / "cache" / "sample-mp" / "sample-plugin"
                  / "1.0" / "skills" / name)
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# Body\n",
        encoding="utf-8",
    )
    return _run_inventory(root)


def test_c3_bare_verification_word_not_flagged() -> None:
    """A plugin skill described 'email verification tool' must NOT be flagged
    (bare base word, per SKILL.md reserved-PHRASE rule)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        # Remove the fixture's reserved-phrase skill to isolate the bare-word case.
        import shutil as _sh
        _sh.rmtree(root / "plugins" / "cache" / "sample-mp" / "sample-plugin"
                   / "1.0" / "skills" / "conflicting-skill")
        inv = _inv_with_plugin_skill(root, "email-tool",
                                     "An email verification tool for inbox cleanup.")
        findings = overlap_check.analyze(inv)
    desc_findings = [f for f in findings
                     if f["category"] == "skill_description"
                     and "email-tool" in f.get("item", "")]
    assert desc_findings == [], \
        f"C3: bare 'verification' word over-fired: {desc_findings}"


def test_c3_reserved_phrase_past_char_200_flagged() -> None:
    """A reserved phrase ('closeout') past char 200 must still be flagged
    (full description analysed, not truncated to 200 before analysis)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        padding = "x" * 230  # push the reserved word well past char 200
        desc = f"A general tool. {padding} Helps with closeout of tasks."
        assert desc.index("closeout") > 200
        inv = _inv_with_plugin_skill(root, "late-closeout", desc)
        findings = overlap_check.analyze(inv)
    desc_findings = [f for f in findings
                     if f["category"] == "skill_description"
                     and "late-closeout" in f.get("item", "")]
    assert len(desc_findings) >= 1, \
        "C3: reserved phrase past char 200 was missed (description truncated too early)"


def test_c3_verification_routing_phrase_still_flagged() -> None:
    """The reserved PHRASE 'verification routing' must still be flagged
    (fixture's conflicting-skill keeps firing)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_fake_skill_root(root)
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    hits = [f for f in findings
            if f["category"] == "skill_description"
            and "conflicting-skill" in f.get("item", "")]
    assert len(hits) >= 1, "C3: 'verification routing' phrase must still flag"


# ---------------------------------------------------------------------------
# L1: plugin-hook liveness cross-join (state-aware filtering).
# A plugin hook is a LIVE conflict only if its plugin is enabled AND the cache
# version is the installed-current one. A disabled plugin, or a stale-cache hook
# from a superseded version, must be DOWNGRADED to cosmetic (liveness annotated),
# never reported as a live blocking/noise conflict. Fail-safe: only positive
# evidence (plugin absent from a NON-EMPTY enabledPlugins / version != KNOWN
# current) downgrades — missing data keeps the conflict (never silence a real one).
# ---------------------------------------------------------------------------


def _pretooluse_bash(cmd: str = "block-bash.sh") -> dict:
    """A hooks.json `hooks` spec with one PreToolUse:Bash hook (blocking signal)."""
    return {"PreToolUse": [{"matcher": "Bash",
                            "hooks": [{"type": "command", "command": cmd}]}]}


def _build_liveness_root(root: Path, *, enabled_plugins: dict,
                         installed: dict, cache_hooks: list) -> None:
    """Minimal fake skill_root for liveness tests (no sample-plugin noise).

    enabled_plugins : settings.json `enabledPlugins` (key `<plugin>@<marketplace>`)
    installed       : installed_plugins.json `plugins` (same key -> [{version,...}])
    cache_hooks     : list of (marketplace, plugin, version, hooks_spec) tuples
    """
    (root / "settings.json").write_text(json.dumps({
        "enabledPlugins": enabled_plugins,
        "env": {"AQG_ROOT": "/path/to/aqg"},
        "hooks": {},
    }), encoding="utf-8")
    plugins_dir = root / "plugins"
    plugins_dir.mkdir(parents=True, exist_ok=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps({"version": "1", "plugins": installed}), encoding="utf-8")
    for marketplace, plugin, version, spec in cache_hooks:
        hd = plugins_dir / "cache" / marketplace / plugin / version / "hooks"
        hd.mkdir(parents=True, exist_ok=True)
        (hd / "hooks.json").write_text(json.dumps({"hooks": spec}), encoding="utf-8")
    (root / "skills").mkdir(parents=True, exist_ok=True)


def _plugin_pre_findings(findings: list) -> list:
    """PreToolUse plugin-hook findings only (the blocking-signal subset)."""
    return [f for f in findings
            if f.get("category") == "plugin_hook" and "PreToolUse" in f.get("item", "")]


def test_liveness_helper_classifies_disabled_stale_live() -> None:
    """Direct unit coverage of _plugin_hook_liveness across all branches."""
    classify = overlap_check._plugin_hook_liveness

    def h(v: str) -> dict:
        return {"plugin": "foo", "marketplace": "mp", "version": v}

    # disabled: enabledPlugins present (non-empty) but key absent
    state, _ = classify(h("1.0"), {"other@mp": True}, {"foo@mp": "1.0"})
    assert state == "disabled"
    # disabled: key present but falsy
    state, _ = classify(h("1.0"), {"foo@mp": False}, {"foo@mp": "1.0"})
    assert state == "disabled"
    # stale: enabled but version != installed current
    state, detail = classify(h("1.0"), {"foo@mp": True}, {"foo@mp": "2.0"})
    assert state == "stale" and "1.0" in detail and "2.0" in detail
    # live: enabled + version == installed current
    state, _ = classify(h("2.0"), {"foo@mp": True}, {"foo@mp": "2.0"})
    assert state == "live"
    # fail-safe: empty enabledPlugins cannot prove disabled -> live
    state, _ = classify(h("2.0"), {}, {"foo@mp": "2.0"})
    assert state == "live"
    # fail-safe: unknown installed version cannot prove stale -> live
    state, _ = classify(h("9.9"), {"foo@mp": True}, {})
    assert state == "live"
    # disabled takes precedence over stale (don't reach version check)
    state, _ = classify(h("1.0"), {"other@mp": True}, {"foo@mp": "2.0"})
    assert state == "disabled"
    # fail-safe (audit f1): present-but-EMPTY installed version is unknown, not a
    # known-empty version — must not mark a real cache version stale
    state, _ = classify(h("1.0"), {"foo@mp": True}, {"foo@mp": ""})
    assert state == "live"
    # fail-safe (audit f1): a missing hook version cannot prove stale
    state, _ = classify({"plugin": "foo", "marketplace": "mp"}, {"foo@mp": True}, {"foo@mp": "2.0"})
    assert state == "live"
    # fail-safe (audit f2): incomplete plugin identity → live, never disabled
    state, _ = classify({"version": "1.0"}, {"other@mp": True}, {})
    assert state == "live"
    state, _ = classify({"plugin": "foo", "version": "1.0"}, {"other@mp": True}, {})
    assert state == "live"  # marketplace missing


def test_stale_version_plugin_hook_downgraded_not_blocking() -> None:
    """A superseded cache version's hook is cosmetic+stale; current is live+blocking."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"p@mp": True},
            installed={"p@mp": [{"version": "2.0"}]},
            cache_hooks=[
                ("mp", "p", "2.0", _pretooluse_bash("live.sh")),    # current -> live
                ("mp", "p", "1.0", _pretooluse_bash("stale.sh")),   # superseded -> stale
            ],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    live = [f for f in pre if "p@2.0:" in f["item"]]
    stale = [f for f in pre if "p@1.0:" in f["item"]]
    assert len(live) == 1 and live[0]["severity"] == "blocking" and live[0]["liveness"] == "live"
    assert len(stale) == 1 and stale[0]["severity"] == "cosmetic" and stale[0]["liveness"] == "stale"


def test_disabled_plugin_hook_downgraded_not_blocking() -> None:
    """A plugin absent from a non-empty enabledPlugins has its hook downgraded."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"enabled-other@mp": True},  # 'p@mp' NOT enabled
            installed={"p@mp": [{"version": "1.0"}]},
            cache_hooks=[("mp", "p", "1.0", _pretooluse_bash())],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "cosmetic"
    assert pre[0]["liveness"] == "disabled"


def test_live_plugin_hook_still_blocking() -> None:
    """Regression: an enabled + current-version hook keeps its blocking severity."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"p@mp": True},
            installed={"p@mp": [{"version": "1.0"}]},
            cache_hooks=[("mp", "p", "1.0", _pretooluse_bash())],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "blocking"
    assert pre[0]["liveness"] == "live"


def test_failsafe_missing_enabledplugins_keeps_conflict() -> None:
    """Empty enabledPlugins must NOT silence a conflict (fail-safe toward reporting)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={},  # no enable data available
            installed={"p@mp": [{"version": "1.0"}]},
            cache_hooks=[("mp", "p", "1.0", _pretooluse_bash())],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "blocking", \
        "fail-safe: missing enabledPlugins must not silence a conflict"
    assert pre[0]["liveness"] == "live"


def test_failsafe_unknown_plugin_version_keeps_conflict() -> None:
    """Unknown installed-current version must NOT mark a hook stale (fail-safe)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"p@mp": True},
            installed={},  # plugin not recorded -> current version unknown
            cache_hooks=[("mp", "p", "7.7", _pretooluse_bash())],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "blocking", \
        "fail-safe: unknown current version must not mark a hook stale"
    assert pre[0]["liveness"] == "live"


def test_failsafe_present_plugin_empty_version_keeps_conflict() -> None:
    """Audit f1 regression: an installed entry PRESENT for the plugin but with an
    empty version must read as unknown (not a known-empty version), so a real
    cache version is not falsely marked stale and silenced."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"p@mp": True},
            installed={"p@mp": [{"version": ""}]},  # present but empty version
            cache_hooks=[("mp", "p", "1.0", _pretooluse_bash())],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "blocking", \
        "fail-safe: present-but-empty installed version must not mark a hook stale"
    assert pre[0]["liveness"] == "live"


def test_failsafe_hook_missing_identity_keeps_live() -> None:
    """Audit f2 regression: a hook dict lacking plugin/marketplace identity must
    not be classified disabled (the gap is caller data, not positive evidence).
    Fed straight to analyze() since inventory always populates identity."""
    findings = overlap_check.analyze({
        "hooks": {"plugin": [
            {"hook_type": "PreToolUse", "matcher": "Bash",
             "command": "x", "version": "1.0"},  # no plugin / marketplace
        ]},
        "settings": {"enabled_plugins": {"some@mp": True}},
        "plugins": {"plugins": []},
        "skills": {},
        "mcp": {},
    })
    pre = _plugin_pre_findings(findings)
    assert len(pre) == 1
    assert pre[0]["severity"] == "blocking"
    assert pre[0]["liveness"] == "live"


def test_session_start_duplicate_ignores_stale_version() -> None:
    """Same-plugin stale+current SessionStart with identical command is NOT a
    cross-plugin duplicate (the stale version must not count as a second source)."""
    banner = {"SessionStart": [{"matcher": "*",
                                "hooks": [{"type": "command", "command": "echo same-banner-xyz"}]}]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"p@mp": True},
            installed={"p@mp": [{"version": "2.0"}]},
            cache_hooks=[
                ("mp", "p", "2.0", banner),   # live
                ("mp", "p", "1.0", banner),   # stale, identical command
            ],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    dups = [f for f in findings if "SessionStart:duplicate" in f.get("item", "")]
    assert dups == [], \
        f"stale same-plugin version must not count as a duplicate source: {dups}"


def test_session_start_duplicate_two_live_plugins_still_flagged() -> None:
    """Two DISTINCT live plugins sharing a SessionStart command still flag a
    genuine duplicate (guard against over-correcting the stale filter)."""
    banner = {"SessionStart": [{"matcher": "*",
                                "hooks": [{"type": "command", "command": "echo shared-init-banner"}]}]}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _build_liveness_root(
            root,
            enabled_plugins={"a@mp": True, "b@mp": True},
            installed={"a@mp": [{"version": "1.0"}], "b@mp": [{"version": "1.0"}]},
            cache_hooks=[
                ("mp", "a", "1.0", banner),
                ("mp", "b", "1.0", banner),
            ],
        )
        inv = _run_inventory(root)
        findings = overlap_check.analyze(inv)
    dups = [f for f in findings if "SessionStart:duplicate" in f.get("item", "")]
    assert len(dups) == 1, \
        "two distinct live plugins with same SessionStart must still flag duplicate"


_ALL_TESTS = [
    test_inventory_captures_five_data_types,
    test_overlap_check_flags_known_signals,
    test_overlap_check_flags_missing_required_env,
    test_overlap_check_flags_missing_recommended_env,
    test_inventory_redacts_env_values,
    # C1
    test_c1_secret_in_hook_command_redacted,
    test_c1_secret_in_user_hook_command_redacted,
    test_c1_secret_in_mcp_raw_and_stderr_redacted,
    test_c1_ordinary_command_not_over_redacted,
    test_c1_redact_helper_patterns,
    # C2
    test_c2_settings_top_level_list_exit3,
    test_c2_env_as_list_exit3_or_clean,
    test_c2_non_dict_hook_entry_no_crash,
    test_c2_enabled_plugins_as_list_no_crash,
    test_c2_installed_plugins_entry_not_dict_no_crash,
    test_c2_overlap_analyze_wrong_shape_no_crash,
    # C3
    test_c3_bare_verification_word_not_flagged,
    test_c3_reserved_phrase_past_char_200_flagged,
    test_c3_verification_routing_phrase_still_flagged,
    # L1: plugin-hook liveness cross-join (state-aware filtering)
    test_liveness_helper_classifies_disabled_stale_live,
    test_stale_version_plugin_hook_downgraded_not_blocking,
    test_disabled_plugin_hook_downgraded_not_blocking,
    test_live_plugin_hook_still_blocking,
    test_failsafe_missing_enabledplugins_keeps_conflict,
    test_failsafe_unknown_plugin_version_keeps_conflict,
    test_failsafe_present_plugin_empty_version_keeps_conflict,
    test_failsafe_hook_missing_identity_keeps_live,
    test_session_start_duplicate_ignores_stale_version,
    test_session_start_duplicate_two_live_plugins_still_flagged,
]


if __name__ == "__main__":
    for _t in _ALL_TESTS:
        _t()
    print(f"OK: aqg-automation-audit self-test passed ({len(_ALL_TESTS)} tests)")
