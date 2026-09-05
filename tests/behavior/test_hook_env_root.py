"""The installer must make AQG_ROOT reachable by the hooks it just wired.

Every managed hook command opens with

    if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi

and `install_aqg_hooks.py` deliberately does not bake the checkout path into the
command, so a checkout can be swapped without re-running install. Nothing,
however, ever set the variable: the installer only PRINTED "Ensure AQG_ROOT is
exported" as its last line, and neither AQG's nor the Decision Engine's install
script exports it. A user who misses that line — or who launches the agent from
a shell that never sourced it — gets every hook silently disabled, the
PreToolUse secret scan among them.

`settings.json` is the right home for it: the installer already owns that file
(it writes `hooks` there), and the host passes its `env` block to hook processes.
Swappability survives — point the one key at another checkout, or re-run install.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import install_aqg_hooks as ih  # noqa: E402


def test_missing_env_block_gets_one(tmp_path: Path) -> None:
    settings: dict = {"hooks": {}}
    changed = ih.ensure_env_root(settings, tmp_path)
    assert changed is not None
    assert settings["env"]["AQG_ROOT"] == str(tmp_path)


def test_existing_env_block_keeps_its_other_keys(tmp_path: Path) -> None:
    settings: dict = {"env": {"SOME_OTHER": "keep me"}}
    ih.ensure_env_root(settings, tmp_path)
    assert settings["env"]["SOME_OTHER"] == "keep me"
    assert settings["env"]["AQG_ROOT"] == str(tmp_path)


def test_same_value_is_a_no_op(tmp_path: Path) -> None:
    settings: dict = {"env": {"AQG_ROOT": str(tmp_path)}}
    assert ih.ensure_env_root(settings, tmp_path) is None


def test_a_different_checkout_is_never_clobbered(tmp_path: Path) -> None:
    """The user may be pointing at another checkout on purpose; say so, don't overwrite."""
    other = str(tmp_path / "somewhere-else")
    settings: dict = {"env": {"AQG_ROOT": other}}
    changed = ih.ensure_env_root(settings, tmp_path)
    assert settings["env"]["AQG_ROOT"] == other, "must not overwrite the user's value"
    assert changed is not None and "differs" in changed.lower(), changed


def test_blank_value_is_replaced(tmp_path: Path) -> None:
    """`AQG_ROOT: ""` fails the shell guard exactly like a missing key."""
    settings: dict = {"env": {"AQG_ROOT": "   "}}
    assert ih.ensure_env_root(settings, tmp_path) is not None
    assert settings["env"]["AQG_ROOT"] == str(tmp_path)


def test_non_dict_env_is_refused_not_crashed(tmp_path: Path) -> None:
    settings: dict = {"env": "not an object"}
    changed = ih.ensure_env_root(settings, tmp_path)
    assert settings["env"] == "not an object", "malformed env must be left alone"
    assert changed is not None and "malformed" in changed.lower(), changed


def test_env_is_written_even_when_hooks_need_no_change(tmp_path: Path, capsys) -> None:
    """The trap: cmd_apply returns early on 'already fully installed'.

    A user who installed hooks before this change has a settings.json with every
    hook present and no env block. If the env write rides only on the hook-change
    path, that user stays silently gated forever — the exact population this fix
    exists for.
    """
    target = tmp_path / "settings.json"
    aqg_root = REPO

    # First apply: writes the hooks.
    assert ih.cmd_apply(target, aqg_root) == ih.EXIT_OK
    # Simulate the pre-change population: hooks present, env absent.
    import json

    data = json.loads(target.read_text(encoding="utf-8"))
    data.pop("env", None)
    target.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    # Second apply: hooks are unchanged, so this is the early-return path.
    assert ih.cmd_apply(target, aqg_root) == ih.EXIT_OK
    after = json.loads(target.read_text(encoding="utf-8"))
    assert after.get("env", {}).get("AQG_ROOT") == str(aqg_root), (
        "hooks-unchanged path must still repair a missing env.AQG_ROOT"
    )


def test_doctors_guard_string_matches_the_one_the_installer_writes() -> None:
    """Doctor counts gated hooks by matching this literal; a drift silently kills it.

    If the installer's guard wording changes and doctor's copy does not, doctor
    counts zero gated commands, returns PASS, and the whole hook_env_guard check
    dies without a single test going red — the same hand-copied-constant drift
    this repo already guards against for the Gate A sensitivity list.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import aqg_doctor  # noqa: PLC0415

    specs = ih._aqg_hook_specs(REPO)
    commands = [
        hook["command"]
        for groups in specs.values()
        for group in groups
        for hook in group["hooks"]
    ]
    assert commands, "installer produced no hook commands"
    for command in commands:
        assert command.startswith(aqg_doctor._HOOK_ENV_GUARD), (
            "installer guard drifted from doctor's copy:\n"
            f"  installer: {command[:70]}\n"
            f"  doctor:    {aqg_doctor._HOOK_ENV_GUARD}"
        )


def test_every_guarded_command_yields_a_script_ref() -> None:
    """Silent extraction failure must be impossible, whatever the command shape.

    Audit aud_7nsuQouSm96_9rt9 (xai, blocking): the script-existence gate is only
    as strong as the regex that finds the scripts. If a future command uses
    ${AQG_ROOT}/... instead of $AQG_ROOT/..., extraction returns nothing, nothing
    is reported missing, and the check PASSes while the PreToolUse secret scan
    cannot run — a false PASS on a security gate. Counting refs against commands
    catches that regardless of which form the installer settles on.
    """
    sys.path.insert(0, str(REPO / "scripts"))
    import aqg_doctor  # noqa: PLC0415

    specs = ih._aqg_hook_specs(REPO)
    commands = [h["command"] for ev in specs.values() for g in ev for h in g["hooks"]]
    refs = aqg_doctor._hook_script_refs({"hooks": specs})
    assert refs, "extraction found no scripts at all"
    for command in commands:
        one = aqg_doctor._hook_script_refs(
            {"hooks": {"X": [{"hooks": [{"command": command}]}]}}
        )
        assert one, f"no script extracted from a managed command: {command[:90]}"


def test_the_braced_form_is_extracted_too() -> None:
    """${AQG_ROOT}/x.sh must resolve like $AQG_ROOT/x.sh, not silently to nothing."""
    sys.path.insert(0, str(REPO / "scripts"))
    import aqg_doctor  # noqa: PLC0415

    braced = {
        "hooks": {
            "PreToolUse": [
                {"hooks": [{"command": 'bash "${AQG_ROOT}/agent-packs/x.sh"'}]}
            ]
        }
    }
    assert aqg_doctor._hook_script_refs(braced) == {"agent-packs/x.sh"}


# --- uninstall must undo what install did -------------------------------------
#
# `--uninstall` stripped the hooks and left env.AQG_ROOT behind, so the obvious way
# to get back to a clean state — uninstall, reinstall — silently did not exercise
# the env write at all: the second install saw a correct value and no-opped. The
# residue is inert once no hook reads it, which is why this was first deferred; the
# real cost is that it breaks the reset path people actually use to verify an
# install.


def test_uninstall_removes_the_key_install_added(tmp_path: Path) -> None:
    target = tmp_path / "settings.json"
    assert ih.cmd_apply(target, REPO) == ih.EXIT_OK
    import json

    assert json.loads(target.read_text())["env"]["AQG_ROOT"] == str(REPO)
    assert ih.cmd_uninstall(target, REPO) == ih.EXIT_OK
    after = json.loads(target.read_text())
    assert "AQG_ROOT" not in after.get("env", {}), "uninstall left its own key behind"


def test_uninstall_keeps_a_value_pointing_somewhere_else(tmp_path: Path) -> None:
    """Mirror of install's rule: a foreign value was chosen by someone, not by us."""
    import json

    target = tmp_path / "settings.json"
    ih.cmd_apply(target, REPO)
    data = json.loads(target.read_text())
    data["env"]["AQG_ROOT"] = "/somewhere/else"
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    ih.cmd_uninstall(target, REPO)
    assert json.loads(target.read_text())["env"]["AQG_ROOT"] == "/somewhere/else"


def test_uninstall_keeps_the_users_other_env_keys(tmp_path: Path) -> None:
    import json

    target = tmp_path / "settings.json"
    ih.cmd_apply(target, REPO)
    data = json.loads(target.read_text())
    data["env"]["SOME_OTHER"] = "keep me"
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    ih.cmd_uninstall(target, REPO)
    env = json.loads(target.read_text()).get("env", {})
    assert env == {"SOME_OTHER": "keep me"}


def test_an_env_block_we_emptied_is_removed_entirely(tmp_path: Path) -> None:
    """Leaving `"env": {}` behind is residue too — restore the file's original shape."""
    import json

    target = tmp_path / "settings.json"
    ih.cmd_apply(target, REPO)
    ih.cmd_uninstall(target, REPO)
    assert "env" not in json.loads(target.read_text())


def test_env_is_cleared_even_when_no_hooks_remain(tmp_path: Path) -> None:
    """The trap cmd_apply already had: an early return that skips the repair.

    Someone who ran the old uninstall, or who removed the hooks by hand, hits
    cmd_uninstall's `no AQG hooks present (no-op)` path — and would keep the stale
    key forever.
    """
    import json

    target = tmp_path / "settings.json"
    ih.cmd_apply(target, REPO)
    data = json.loads(target.read_text())
    data.pop("hooks", None)  # hooks already gone, env still there
    target.write_text(json.dumps(data, indent=2), encoding="utf-8")
    assert ih.cmd_uninstall(target, REPO) == ih.EXIT_OK
    assert "env" not in json.loads(target.read_text())
