"""L2 pre-launch hardening regression suite for the AQG installers.

Each test pins one finding from audit 48a01f42 (gpt-5.5 + gemini) + the Claude
review (workflow PR-C). Failing-before / passing-after — every bug-repro test
FAILS against the pre-fix scripts/install_aqg_hooks.py / install_pre_commit.py.
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import install_aqg_hooks as h  # noqa: E402
import install_pre_commit as pc  # noqa: E402


# --- C1: settings.json must be written atomically (no follow-symlink write) ----

def test_c1_atomic_write_refuses_symlink(tmp_path):
    real = tmp_path / "real.json"
    real.write_text("{}", encoding="utf-8")
    link = tmp_path / "link.json"
    link.symlink_to(real)
    with pytest.raises(ValueError, match="symlink"):
        h._atomic_write_text(link, '{"x": 1}')


def test_c1_write_settings_roundtrip_no_tmp_residue(tmp_path):
    target = tmp_path / "settings.json"
    h._write_settings(target, {"hooks": {"PreToolUse": []}})
    import json
    assert json.loads(target.read_text())["hooks"] == {"PreToolUse": []}
    assert list(tmp_path.glob(".tmp-*")) == []  # atomic temp cleaned up


# --- C2: _merge_hooks must not mutate the caller's existing settings in place ---

def test_c2_merge_hooks_does_not_mutate_input(tmp_path):
    existing = {
        "PreToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "echo user-hook"}]}
        ]
    }
    snapshot = copy.deepcopy(existing)
    aqg = h._aqg_hook_specs(REPO)
    merged, _added, _skipped, count = h._merge_hooks(existing, aqg)
    assert count > 0  # the AQG Bash validator is appended into the existing block
    assert existing == snapshot, "caller's settings dict was mutated in place"
    # the merged result DID get the new entry (sanity)
    assert merged != existing


# --- C3: valid-JSON-but-wrong-shape settings fail cleanly, never crash ---------

def test_c3_load_settings_rejects_non_object_toplevel(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text("[1, 2, 3]", encoding="utf-8")  # valid JSON, wrong shape
    with pytest.raises(SystemExit):
        h._load_settings(p)


def test_c3_apply_rejects_non_dict_hooks(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text('{"hooks": [1, 2]}', encoding="utf-8")  # hooks must be an object
    rc = h.cmd_apply(p, REPO)
    assert rc == h.EXIT_GENERIC  # clean error, not AttributeError traceback


def test_c3_merge_hooks_survives_malformed_nested(tmp_path):
    # a non-dict block / non-list hooks slot must be skipped, not crash.
    existing = {"PreToolUse": ["not-a-dict", {"matcher": "Bash", "hooks": "not-a-list"}]}
    merged, _a, _s, _c = h._merge_hooks(existing, h._aqg_hook_specs(REPO))
    assert isinstance(merged, dict)  # did not raise


# --- C4: _copy_config must not crash on a directory / binary target ------------

def test_c4_copy_config_directory_target_no_crash(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").mkdir()  # a DIRECTORY at the config path
    rc, _path, _info = pc._copy_config(tmp_path, aqg_root=REPO, force=False)
    assert rc == pc.EXIT_FILE_EXISTS  # clean status, not IsADirectoryError


def test_c4_copy_config_binary_target_no_crash(tmp_path):
    (tmp_path / ".pre-commit-config.yaml").write_bytes(b"\xff\xfe\x00 not utf8")
    rc, _path, _info = pc._copy_config(tmp_path, aqg_root=REPO, force=False)
    assert rc == pc.EXIT_FILE_EXISTS  # clean status, not UnicodeDecodeError


# --- C5: AQG-hook ownership must match the canonical PATH, not a bare basename --

def test_c5_bare_basename_not_treated_as_aqg_hook():
    # a user hook that merely mentions the filename must NOT be claimed by AQG
    # (else it is skipped on install / removed on uninstall).
    assert h._command_references_aqg_script("echo pretooluse_bash_skill_validator.sh") is None


def test_c5_canonical_command_still_matched():
    canonical = ('if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; '
                 'bash "$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh"')
    assert h._command_references_aqg_script(canonical) == "sessionstart_preflight.sh"


# --- C10: backup must never clobber a prior backup (sub-second collisions) ------

def test_c10_backup_never_clobbers(tmp_path, monkeypatch):
    monkeypatch.setenv("AQG_BACKUP_DIR", str(tmp_path / "central"))
    monkeypatch.delenv("AQG_BACKUP_KEEP", raising=False)
    target = tmp_path / "settings.json"
    target.write_text("{}", encoding="utf-8")
    made = set()
    for _ in range(4):
        bak = h._backup(target)
        assert bak is not None
        made.add(str(bak))
    # 4 calls in the same second must yield 4 DISTINCT central backups — the
    # never-clobber .N guarantee now lives in the shared store, not adjacent files.
    assert len(made) == 4, f"backups collided: {made}"
    assert len(list((tmp_path / "central").rglob("settings.json"))) == 4


# --- C6: a stale/divergent command at the canonical matcher is surfaced --------
# Previously, a hook wired at the right (event, matcher) with the right script was
# reported as fully installed (verify) / a true no-op (merge) even when its COMMAND
# string was stale (an old version's flags) or hand-edited. The fix compares the
# whitespace-normalized command against canonical. No path normalization is needed:
# the canonical command embeds the literal `$AQG_ROOT`, so same-version installs are
# byte-identical and only a real token change registers as divergence.

def _canonical_entry(event: str = "PostToolUse", matcher: str = "Bash") -> tuple[str, str]:
    """(canonical command, script) for one AQG hook pulled from the live specs."""
    specs = h._aqg_hook_specs(REPO)
    for blk in specs[event]:
        if blk.get("matcher", "") == matcher:
            cmd = blk["hooks"][0]["command"]
            return cmd, h._command_references_aqg_script(cmd)
    raise AssertionError(f"no canonical entry for {event}/{matcher!r}")


def _settings_with(event: str, matcher: str, command: str) -> dict:
    return {"hooks": {event: [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]}}


def test_c6_verify_flags_stale_command(tmp_path, capsys):
    canon, _script = _canonical_entry()
    stale = canon + " --obsolete-flag"  # right matcher + right script, stale command
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(_settings_with("PostToolUse", "Bash", stale)), encoding="utf-8")
    h.cmd_verify(p, REPO)
    assert "COMMAND STALE" in capsys.readouterr().out  # pre-fix: reported as cleanly installed


def test_c6_verify_canonical_command_not_flagged(tmp_path, capsys):
    canon, _script = _canonical_entry()
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(_settings_with("PostToolUse", "Bash", canon)), encoding="utf-8")
    h.cmd_verify(p, REPO)
    assert "COMMAND STALE" not in capsys.readouterr().out


def test_c6_verify_whitespace_only_diff_not_flagged(tmp_path, capsys):
    # FP guard (the ledger's worry): cosmetic whitespace must NOT register as drift.
    canon, _script = _canonical_entry()
    spaced = "  ".join(canon.split(" "))  # double every separator — cosmetic only
    assert spaced != canon
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(_settings_with("PostToolUse", "Bash", spaced)), encoding="utf-8")
    h.cmd_verify(p, REPO)
    assert "COMMAND STALE" not in capsys.readouterr().out


def test_c6_merge_converges_stale_command_in_place():
    # C6 (post-audit): a stale AQG-owned command at the canonical matcher is
    # converged to canonical IN PLACE — not skipped, not duplicated — so --apply
    # delivers command-format upgrades to existing installs.
    canon, script = _canonical_entry("PostToolUse", "Bash")
    stale = canon + " --obsolete-flag"
    existing = {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": stale}]}]}
    snapshot = copy.deepcopy(existing)
    merged, log, _skipped, count = h._merge_hooks(existing, h._aqg_hook_specs(REPO))
    assert any("updated" in m and script in m for m in log)  # pre-fix: silently skipped (no update line)
    bash_cmds = [hk["command"] for b in merged["PostToolUse"]
                 if b.get("matcher") == "Bash" for hk in b.get("hooks", [])]
    refs = [h._command_references_aqg_script(c) for c in bash_cmds]
    assert refs.count(script) == 1                       # converged in place, not duplicated
    assert canon in bash_cmds and stale not in bash_cmds  # command content IS replaced (gemini f3)
    assert count >= 1                                     # the update counts as a change → cmd_apply writes
    assert existing == snapshot                           # C2: caller's input dict not mutated


def test_c6_merge_canonical_command_is_noop():
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    existing = {"PostToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": canon}]}]}
    merged, log, skipped, _count = h._merge_hooks(existing, h._aqg_hook_specs(REPO))
    assert not any("updated" in m for m in log)  # canonical command → no false convergence
    assert any("already wired" in s for s in skipped)  # recognized as installed


def test_c6_apply_converges_stale_command_when_otherwise_fully_installed(tmp_path, capsys):
    # gpt-5.5 verify-round (f1): with everything else canonical, a single stale
    # command makes adds == 0 — pre-fix --apply early-returned "already fully
    # installed" and dropped the change. Now it must converge + surface + write.
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    for blk in settings["hooks"]["PostToolUse"]:
        if blk.get("matcher") == "Bash":
            blk["hooks"][0]["command"] = canon + " --obsolete-flag"
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    rc = h.cmd_apply(p, REPO)
    assert rc == h.EXIT_OK
    out = capsys.readouterr().out
    assert "already fully installed" not in out  # pre-fix: FALSE no-op (the bug)
    assert "updated" in out                       # convergence surfaced to the user
    written = json.loads(p.read_text(encoding="utf-8"))
    cmds = [hk["command"] for b in written["hooks"]["PostToolUse"]
            if b.get("matcher") == "Bash" for hk in b.get("hooks", [])]
    assert canon in cmds and (canon + " --obsolete-flag") not in cmds  # canonical written


@pytest.mark.parametrize("order", ["stale_first", "stale_last", "both_stale"])
def test_c6_merge_converges_all_duplicate_same_key_hooks(order):
    # Round-2 verify (convergent gpt-5.5 + gemini): a degenerate config with TWO
    # hooks for the same (matcher, script) must converge EVERY stale duplicate.
    # Pre-fix, the present map kept only one hook per key, so a stale duplicate
    # (esp. when the canonical one is last) survived — leaving --apply (no-op) and
    # verify (warns over all entries) in permanent disagreement.
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    stale = canon + " --obsolete-flag"
    pair = {"stale_first": [stale, canon],
            "stale_last": [canon, stale],
            "both_stale": [stale, stale]}[order]
    existing = {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": c}]} for c in pair
    ]}
    merged, _log, _skipped, _count = h._merge_hooks(existing, h._aqg_hook_specs(REPO))
    bash_cmds = [hk["command"] for b in merged["PostToolUse"]
                 if b.get("matcher") == "Bash" for hk in b.get("hooks", [])]
    assert stale not in bash_cmds, order            # every stale duplicate converged
    assert bash_cmds and all(c == canon for c in bash_cmds), order  # all now canonical


# --- #223: stale-matcher warning must fire even when canonical is ALSO present --
# (apply/verify disagreement — pre-existing, surfaced by the C6 round-3 audit)
#
# Markers are PATH-INDEPENDENT on purpose: pytest's tmp_path embeds the test
# function name, so a test named "...stale..." leaks the literal "stale" into
# cmd_apply's printed target path and falsely satisfies `"stale" in out`. We assert
# on the warning phrasing ("non-canonical matcher" / "STALE:") instead.

def test_223_merge_emits_warning_when_canonical_also_present():
    # Same script wired at BOTH the canonical matcher (clean command) AND a
    # non-canonical matcher. Pre-fix, the canonical-present `continue` in
    # _merge_hooks skipped the stale-matcher scan, so NO warning was emitted —
    # cmd_apply stayed silent while cmd_verify reported STALE (#223). `log` is the
    # returned change_log (no filesystem path), so a plain substring check is safe.
    canon, script = _canonical_entry("PostToolUse", "Bash")
    existing = {"PostToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": canon}]},
        {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": canon}]},
    ]}
    _merged, log, _skipped, _count = h._merge_hooks(existing, h._aqg_hook_specs(REPO))
    assert any("non-canonical matcher" in m and script in m for m in log), (
        "stale-matcher warning must be emitted even when canonical is also present"
    )


def test_223_apply_surfaces_warning_when_fully_installed(tmp_path, capsys):
    # Full canonical install (change_count == 0) PLUS the same script under a
    # non-canonical matcher. Pre-fix, cmd_apply early-returned "already fully
    # installed" and never surfaced the duplicate; cmd_verify reported STALE (#223).
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, script = _canonical_entry("PostToolUse", "Bash")
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": canon}]}
    )
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    rc = h.cmd_apply(p, REPO)
    out = capsys.readouterr().out
    assert rc == h.EXIT_OK
    assert "non-canonical matcher" in out, "cmd_apply must surface the duplicate entry"
    assert script in out


def test_223_apply_and_verify_agree_on_duplicate(tmp_path, capsys):
    # Core invariant (#223): apply and verify must AGREE on a duplicate entry.
    # verify uses "STALE:" (uppercase), apply uses "non-canonical matcher" — both
    # path-independent, so the tmp dir name cannot decide the outcome.
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": canon}]}
    )
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    h.cmd_verify(p, REPO)
    verify_flags = "STALE:" in capsys.readouterr().out
    h.cmd_apply(p, REPO)
    apply_flags = "non-canonical matcher" in capsys.readouterr().out
    assert verify_flags, "verify should flag the duplicate (sanity)"
    assert apply_flags == verify_flags, "apply and verify must agree on stale state (#223)"


# --- #223 edge cases (audit 78558574 f1/f2: multi-matcher / same-matcher dup / C6) --

def test_223_apply_surfaces_multiple_non_canonical_matchers(tmp_path, capsys):
    # Same script under canonical + TWO non-canonical matchers — apply lists both.
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": canon}]}
    )
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Edit", "hooks": [{"type": "command", "command": canon}]}
    )
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    h.cmd_apply(p, REPO)
    out = capsys.readouterr().out
    assert "non-canonical matcher" in out
    assert "Bash|Edit" in out and "'Edit'" in out  # both stale matchers listed in the repr


def test_223_apply_flags_duplicate_under_same_matcher(tmp_path, capsys):
    # f1 (audit): two entries of the same script under ONE non-canonical matcher.
    # apply reports the matcher once (set-deduped), verify lists per-entry — the
    # invariant that matters is that BOTH still flag stale (agree), not cardinality.
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Bash|Edit", "hooks": [
            {"type": "command", "command": canon},
            {"type": "command", "command": canon},
        ]}
    )
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    h.cmd_verify(p, REPO)
    verify_flags = "STALE:" in capsys.readouterr().out
    h.cmd_apply(p, REPO)
    apply_flags = "non-canonical matcher" in capsys.readouterr().out
    assert verify_flags and apply_flags, "both must flag stale even for a same-matcher duplicate"


def test_223_apply_converges_canonical_and_flags_duplicate(tmp_path, capsys):
    # f2 (audit): C6 command-convergence (change_count > 0 path) AND a stale
    # duplicate together — apply must BOTH converge the canonical command and
    # surface the non-canonical-matcher warning in the same run.
    settings = {"hooks": copy.deepcopy(h._aqg_hook_specs(REPO))}
    canon, _script = _canonical_entry("PostToolUse", "Bash")
    for blk in settings["hooks"]["PostToolUse"]:
        if blk.get("matcher") == "Bash":
            blk["hooks"][0]["command"] = canon + " --obsolete-flag"
    settings["hooks"]["PostToolUse"].append(
        {"matcher": "Bash|Edit", "hooks": [{"type": "command", "command": canon}]}
    )
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(settings), encoding="utf-8")
    rc = h.cmd_apply(p, REPO)
    out = capsys.readouterr().out
    assert rc == h.EXIT_OK
    assert "updated" in out                    # C6 convergence surfaced (change_count > 0 path)
    assert "non-canonical matcher" in out      # stale warning ALSO surfaced in the same run
