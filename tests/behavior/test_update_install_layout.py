"""What the installer produces must be what the update engine accepts.

The gap this file exists to close: `_apply` refuses any root that is not a
symlink into a versions directory —

    run.py:339  "{root} is not a symlink into a versions directory; this
                 install predates the managed-update layout and must be
                 migrated by hand"  ->  outcome "failed"

— and no install path produces that layout. `scripts/install.sh` clones into a
plain directory; decision-engine's `de-aqg-install` does the same and then calls
`install_aqg_clients.py --apply`. The conversion existed only inside a manual
`upgrade.sh --migrate` that a new user has no reason to know about.

**Why thirty-six existing tests missed it.** `test_update_run.py:260`'s
`_install()` fixture says so in its own docstring — "An install shaped the way
§5 says" — and hand-builds `versions/` plus the symlink. It tests the apply path
from a shape the installer never creates, so the mismatch between the two was
invisible from either side. These tests start where the installer actually
leaves things.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import migrate as migrate_mod

# The second suite that owns the update channel: it watches the context-helper
# nudge start a real process and watches a real apply land. Both are isolated
# into `tmp_path` -- root, state root, keyring and remote -- so it is safe to
# lift the session-wide kill switch `conftest.py` sets. The tests that assert
# the switch WORKS set it themselves in their own subprocess env.
pytestmark = pytest.mark.usefixtures("live_update_channel")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout


def _fresh_clone(tmp_path: Path, name: str = "agent-quality-gates") -> Path:
    """What a fresh install leaves behind: a plain clone, nothing else.

    Deliberately built by cloning, not by `git init` in place, because that is
    what both install paths do and the difference matters — a clone has no
    untracked files, which is what lets it be migrated at all.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "T")
    (origin / "VERSION").write_text("0.1.0\n", encoding="utf-8")
    (origin / "marker.txt").write_text("first\n", encoding="utf-8")
    # The smoke check reads `scripts/_aqg_context.sh` THROUGH the root after the
    # swap — statting the staged tree would be true whether or not the swap
    # happened. So the fixture needs one, or every apply rolls back.
    (origin / "scripts").mkdir()
    (origin / "scripts" / "_aqg_context.sh").write_text("# stub\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "first")

    root = tmp_path / name
    subprocess.run(
        ["git", "clone", "-q", str(origin), str(root)], capture_output=True, check=True
    )
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    return root


# --- R1: what the installer leaves must be the shape the engine accepts -------

def test_a_fresh_install_is_not_yet_a_layout_the_engine_accepts(tmp_path):
    """The starting condition, stated as a test so the fix cannot be claimed
    without it having been true first."""
    root = _fresh_clone(tmp_path)
    assert not root.is_symlink()
    assert not (root.parent / "versions").exists()


def test_ensuring_the_layout_converts_a_fresh_install(tmp_path):
    root = _fresh_clone(tmp_path)
    head = _git(root, "rev-parse", "HEAD").strip()

    result = migrate_mod.ensure_managed_layout(root, managed=True)

    assert result.changed is True, result.reason
    assert result.managed is True
    assert root.is_symlink()
    assert root.resolve() == (root.parent / "versions/0.1.0").resolve()
    assert _git(root, "rev-parse", "HEAD").strip() == head
    # The content came along, rather than the link pointing at an empty shell.
    assert (root / "marker.txt").read_text(encoding="utf-8") == "first\n"


def test_ensuring_the_layout_twice_is_a_no_op(tmp_path):
    """A user unsure whether the installer did it must be able to just run again."""
    root = _fresh_clone(tmp_path)
    first = migrate_mod.ensure_managed_layout(root, managed=True)
    assert first.changed is True

    second = migrate_mod.ensure_managed_layout(root, managed=True)
    assert second.changed is False
    assert second.managed is True, "an already-converted install still updates"
    assert root.is_symlink()


# --- R1 the other half: a developer's checkout is left alone ------------------

def test_a_checkout_with_untracked_files_is_left_where_it_is(tmp_path):
    """A developer's working copy almost always has untracked files; a fresh
    install has none. That existing refusal is the discriminator, so this does
    not add one."""
    root = _fresh_clone(tmp_path)
    (root / "scratch.tmp").write_text("mine\n", encoding="utf-8")

    result = migrate_mod.ensure_managed_layout(root, managed=True)

    assert result.ok is False
    assert result.managed is False
    assert "untracked" in result.reason.lower(), result.reason
    assert not root.is_symlink(), "a developer's checkout was moved"
    assert (root / "scratch.tmp").exists()


def test_a_git_worktree_is_left_where_it_is(tmp_path):
    """Moving a worktree leaves its gitdir link pointing at a path that no
    longer holds it. This repository has nine of them."""
    root = _fresh_clone(tmp_path)
    wt = tmp_path / "wt"
    _git(root, "worktree", "add", "-q", "--detach", str(wt))

    result = migrate_mod.ensure_managed_layout(wt, managed=True)

    assert result.ok is False
    assert "worktree" in result.reason.lower(), result.reason
    assert not wt.is_symlink()


def test_the_conversion_can_be_declined_outright(tmp_path, monkeypatch):
    """An opt-out that does not require making the checkout dirty on purpose."""
    root = _fresh_clone(tmp_path)
    monkeypatch.setenv("AQG_NO_MIGRATE", "1")

    result = migrate_mod.ensure_managed_layout(root, managed=True)

    assert result.changed is False
    assert result.managed is False, "declining must not read as enabled"
    assert "AQG_NO_MIGRATE" in result.reason
    assert not root.is_symlink()


def test_it_never_raises_whatever_it_is_pointed_at(tmp_path):
    """An install that cannot be converted is still a working install. Raising
    here would turn "will not auto-update" into "did not install", which is a
    worse outcome for the same underlying condition."""
    for target in (tmp_path / "does-not-exist", tmp_path / "not-a-repo"):
        (tmp_path / "not-a-repo").mkdir(exist_ok=True)
        result = migrate_mod.ensure_managed_layout(target, managed=True)
        assert result.ok is False
        assert result.reason, "a refusal with no reason is a silent failure"


# --- R3: the trigger reaches hosts that have no hooks -------------------------

def _context_helper() -> Path:
    return Path(__file__).resolve().parents[2] / "scripts/_aqg_context.sh"


def _nudge_probe(tmp_path: Path, env: dict) -> dict:
    """Source the helper against a fake root whose run.py records its own env.

    Grepping the helper for a string proved nothing: the first version of this
    check passed while an `eval`-built command line silently launched nothing at
    all. The only assertion worth making is that a process really started.
    """
    root = tmp_path / "aqg"
    (root / "scripts" / "aqg_update").mkdir(parents=True)
    (root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
    (root / "scripts" / "aqg_update" / "__init__.py").write_text("", encoding="utf-8")
    probe = tmp_path / "probe.json"
    (root / "scripts" / "aqg_update" / "run.py").write_text(
        "import os, sys, json, pathlib\n"
        "pathlib.Path(os.environ['PROBE']).write_text(json.dumps({\n"
        "  'has_HOME': 'HOME' in os.environ,\n"
        "  'HOME_empty': os.environ.get('HOME', 'x') == '',\n"
        "  'has_proxy': 'HTTPS_PROXY' in os.environ,\n"
        "  'pythonpath_dropped': 'PYTHONPATH' not in os.environ,\n"
        "  'aqg_root': os.environ.get('AQG_ROOT', ''),\n"
        "  'cwd': os.getcwd(),\n"
        "  'argv': sys.argv[1:],\n"
        "}))\n",
        encoding="utf-8",
    )
    import shutil
    shutil.copy(_context_helper(), root / "scripts" / "_aqg_context.sh")

    script = tmp_path / "caller.sh"
    script.write_text(
        "set -eu\n"
        f'export AQG_ROOT="{root}" PROBE="{probe}"\n'
        f'source "{root}/scripts/_aqg_context.sh"\n'
        "sleep 2\n",
        encoding="utf-8",
    )
    subprocess.run(["bash", str(script)], capture_output=True, text=True,
                   env={**os.environ, **env})
    # The nudge is detached, so bash exiting says nothing about whether the child
    # has run yet. A fixed sleep made this flaky — it passed in one file and
    # failed in another for no reason but load. Poll for the answer instead.
    import time
    for _ in range(40):
        if probe.is_file():
            break
        time.sleep(0.25)
    if not probe.is_file():
        return {}
    return json.loads(probe.read_text(encoding="utf-8"))


def test_the_trigger_actually_starts_a_process(tmp_path):
    """F18 and the reason it went unnoticed.

    The check this replaces asserted that the helper CONTAINED the words
    `aqg_update.run`. It did — inside an `eval` that launched nothing. A text
    assertion cannot tell a working trigger from a broken one, which is the
    same defect class as GAP 3 and it was in the test written to close GAP 3.
    """
    seen = _nudge_probe(tmp_path, {"HTTPS_PROXY": "http://proxy.example:8080",
                                   "PYTHONPATH": "/evil"})
    assert seen, "the trigger launched no process at all"
    assert seen["aqg_root"], "the child got no AQG_ROOT"
    assert seen["cwd"].endswith("aqg"), (
        f"the child did not cd into the root, so `-m` resolves elsewhere: {seen['cwd']}"
    )


def test_the_trigger_keeps_what_the_fetch_needs_and_drops_what_steers_python(tmp_path):
    """F18. `env -i` with an allowlist got both halves wrong: an unset variable
    arrived as empty (`HOME=""` is worse than none — git reads it and writes
    into the cwd), and the proxy and TLS variables were dropped, so every update
    behind a corporate proxy failed with all output discarded."""
    seen = _nudge_probe(tmp_path, {"HTTPS_PROXY": "http://proxy.example:8080",
                                   "PYTHONPATH": "/evil"})
    assert seen, "the trigger launched no process at all"
    assert seen["has_HOME"] and not seen["HOME_empty"], "HOME arrived unset or empty"
    assert seen["has_proxy"], "the proxy the fetch needs was dropped"
    assert seen["pythonpath_dropped"], (
        "PYTHONPATH reached the interpreter that verifies signatures"
    )


def test_sourcing_the_helper_does_not_disturb_the_caller(tmp_path):
    """Its contract: sourced not executed, never exits, leaves the caller's
    shell options and positional parameters intact. A trigger that breaks any
    of those breaks every skill."""
    root = _fresh_clone(tmp_path, name="aqg")
    script = tmp_path / "caller.sh"
    script.write_text(
        "set -eu\n"
        "set -- one two three\n"
        f'export AQG_ROOT="{root}"\n'
        f'source "{_context_helper()}" || echo "SOURCE-FAILED"\n'
        'echo "rc=$?"\n'
        'echo "args=$*"\n'
        'echo "opts=$-"\n',
        encoding="utf-8",
    )
    done = subprocess.run(
        ["bash", str(script)], capture_output=True, text=True,
        env={**os.environ, "AQG_NO_UPDATE_CHECK": "1"},
    )
    assert done.returncode == 0, done.stderr
    assert "SOURCE-FAILED" not in done.stdout
    assert "args=one two three" in done.stdout, done.stdout
    assert "rc=0" in done.stdout, done.stdout


def test_the_kill_switch_stops_the_trigger(tmp_path):
    """Honoured wherever the trigger lives, or switching updates off would work
    in one host and not another. Asserted by absence of a process, not by the
    variable appearing in the file."""
    seen = _nudge_probe(tmp_path, {"AQG_NO_UPDATE_CHECK": "1"})
    assert seen == {}, "the kill switch did not stop the trigger"



# --- R2: from what the installer produces, an update actually lands -----------

def test_an_install_made_the_installer_way_can_actually_be_updated(
    tmp_path, monkeypatch
):
    """The one that would have caught all of this.

    Thirty-six tests in `test_update_run.py` reach `outcome == "applied"`, and
    none of them start from an installer-shaped tree: `_install()` hand-builds
    `versions/` and the symlink, and `available_release` is stubbed. So both
    halves were proven and the seam between them was not.

    This starts where `git clone` leaves things, converts it the way the
    installer now does, and then drives a real `check()` at a real local remote
    holding a real second commit. What it asserts is the thing a user cares
    about: the file on disk changed.
    """
    from scripts.aqg_update import run as run_mod

    root = _fresh_clone(tmp_path)
    origin = tmp_path / "origin"

    # Exactly what the install path now does — no hand-built layout.
    assert migrate_mod.ensure_managed_layout(root, managed=True).changed is True
    first = _git(root, "rev-parse", "HEAD").strip()

    # A second commit upstream: the content the update must bring across.
    (origin / "marker.txt").write_text("second\n", encoding="utf-8")
    _git(origin, "commit", "-qam", "second")
    second = _git(origin, "rev-parse", "HEAD").strip()
    assert second != first

    # The planner sees this machine's real hosts, so a fixture tree routes a
    # skill to every one of them. Only the root swap is under test here.
    def only_activate(**kwargs):
        return run_mod.plan_mod.Plan(
            actions=(
                run_mod.plan_mod.Action(
                    kind="activate_root", client_id=None, subject=None,
                    payload_class=0, detail=f"activate {second[:12]}",
                ),
            ),
            deferred=(),
        )

    monkeypatch.setattr(run_mod.plan_mod, "build_plan", only_activate)

    class _Found:
        manifest = {"version": "0.14.2", "release_sequence": 2, "commit": second}
        commit = second
        key_id = "stable-2026-09"
        release_sequence = 2

    monkeypatch.setattr(run_mod.acquire, "available_release", lambda *a, **k: _Found())
    # `acquire` fetches the commit before returning it; stubbing the lookup means
    # doing that here, or staging has nothing to check out. Fetching for real
    # keeps the rest of the path — worktree, smoke test, swap — genuine.
    _git(root, "fetch", "-q", "origin", second)

    # The fixture is a key, not a keyring; `check()` wants the shipped shape.
    fixture = json.loads(
        (Path(__file__).resolve().parent / "fixtures" / "release_test_key.json")
        .read_text(encoding="utf-8")
    )
    keyring = tmp_path / "release-trust.json"
    keyring.write_text(
        json.dumps({"schema": 1, "keys": [{
            "key_id": fixture["key_id"],
            "algorithm": "rsa-pkcs1v15-sha256",
            "modulus_hex": fixture["modulus_hex"],
            "exponent": fixture["exponent"],
            "revoked": False,
        }]}),
        encoding="utf-8",
    )

    result = run_mod.check(
        root=root, remote="origin", channel="stable",
        keyring_path=keyring, state_root=tmp_path / "state",
    )

    assert result.outcome == "applied", result.detail
    assert root.resolve().name == "0.14.2"
    assert _git(root, "rev-parse", "HEAD").strip() == second, "the root still points at the old version"
    assert (root / "marker.txt").read_text(encoding="utf-8") == "second\n", (
        "the symlink moved but the content a user reads did not"
    )


def test_the_installer_itself_leaves_a_layout_the_engine_accepts(tmp_path, monkeypatch, capsys):
    """The wiring, not just the capability.

    A module that can convert the layout and no caller that does is the same
    defect wearing a different hat — and it is the one this repository keeps
    finding. Both install paths reach `install_aqg_clients.main()`, so that is
    where this is asserted.
    """
    import scripts.install_aqg_clients as inst

    # The fixture is a temp dir, not ~/.deeppattern/agent-quality-gates, so the
    # location check declines it — which is the new guard doing its job. Point
    # HOME at the fixture so the managed path is the one under test.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    managed = tmp_path / "home" / ".deeppattern" / "agent-quality-gates"
    managed.parent.mkdir(parents=True)
    root = _fresh_clone(tmp_path)
    root.rename(managed)

    inst._ensure_update_layout(managed, install_succeeded=True)

    assert managed.is_symlink(), "the installer left a shape the engine refuses"
    assert "Automatic updates: enabled" in capsys.readouterr().out


def test_the_installer_says_so_when_it_could_not(tmp_path, capsys):
    """Silence here is how this went unnoticed for months: the install looked
    fine and the update simply never happened."""
    import scripts.install_aqg_clients as inst

    root = _fresh_clone(tmp_path)
    (root / "scratch.tmp").write_text("mine\n", encoding="utf-8")
    inst._ensure_update_layout(root, install_succeeded=True)

    err = capsys.readouterr().err
    assert "NOT enabled" in err, err
    assert "upgrade.sh --migrate" in err, "the message must say how to fix it"
    assert not root.is_symlink()


def test_the_conversion_runs_after_the_commands_not_before():
    """It renames the tree this script is running from. Anything spawned during
    the window between that rename and the symlink landing would look for a path
    that does not exist, so the call site is load-bearing, not cosmetic."""
    source = (
        Path(__file__).resolve().parents[2] / "scripts/install_aqg_clients.py"
    ).read_text(encoding="utf-8")
    execute = source.index("results = _execute_commands(commands, env)")
    convert = source.index("_ensure_update_layout(args.aqg_root,")
    assert convert > execute, (
        "the layout conversion moved ahead of command execution; every "
        "subprocess after it would race the rename window"
    )


# ============================================================================
# audit aud_Y3qcP8c7jPy4xs_H — 18 findings, 4/5 voices has-serious-issues.
# Each test below names the finding it closes.
# ============================================================================

def test_declining_the_conversion_does_not_claim_updates_are_enabled(tmp_path, capsys):
    """F2/F5/F10/F13 — all four voices, independently.

    `AQG_NO_MIGRATE` returns ok=True, changed=False on an UNMIGRATED checkout,
    and the call site printed "Automatic updates: enabled" for any ok=True. So
    the one path that leaves an install unable to update announced the opposite
    — which is the silent non-updating install this whole change exists to end,
    reintroduced by the fix for it.
    """
    import scripts.install_aqg_clients as inst

    root = _fresh_clone(tmp_path)
    os.environ["AQG_NO_MIGRATE"] = "1"
    try:
        inst._ensure_update_layout(root, install_succeeded=True)
    finally:
        del os.environ["AQG_NO_MIGRATE"]

    out = capsys.readouterr()
    combined = out.out + out.err
    assert "enabled" not in out.out.lower(), (
        f"claimed updates are enabled on an unmigrated checkout: {out.out!r}"
    )
    assert "NOT enabled" in combined or "not enabled" in combined.lower()
    assert not root.is_symlink()


def test_a_failed_install_is_not_migrated(tmp_path, capsys):
    """F15 — blocking. The call site ran unconditionally after the commands,
    so a decision-engine install whose commands FAILED still had its directory
    reshaped underneath it. Their retry and repair paths then meet a symlink
    root none of their code put there."""
    import scripts.install_aqg_clients as inst

    root = _fresh_clone(tmp_path)
    inst._ensure_update_layout(root, install_succeeded=False)

    assert not root.is_symlink(), "a failed install had its layout changed anyway"


def test_only_the_managed_install_location_is_converted(tmp_path, monkeypatch):
    """F17 — the developer guard was a probability, not a check.

    "A fresh clone has no untracked files and a working copy usually does" is
    an unsourced claim about habits. A clean checkout on a feature branch has
    none either, and it would have been migrated silently and then updated away
    from that branch. The signal is now what the caller actually knows: whether
    this is the location the installer manages.
    """
    home = tmp_path / "home"
    managed = home / ".deeppattern" / "agent-quality-gates"
    managed.parent.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))

    elsewhere = _fresh_clone(tmp_path, name="my-clone")
    result = migrate_mod.ensure_managed_layout(elsewhere)
    assert result.changed is False
    assert "managed" in result.reason.lower(), result.reason
    assert not elsewhere.is_symlink()


def test_a_checkout_outside_the_managed_location_can_still_opt_in(tmp_path, monkeypatch):
    """The mirror of the above: refusing everywhere else would make the
    conversion unreachable for anyone who deliberately installs elsewhere."""
    monkeypatch.setenv("HOME", str(tmp_path / "nowhere"))
    monkeypatch.setenv("AQG_MIGRATE", "1")
    root = _fresh_clone(tmp_path, name="elsewhere")

    result = migrate_mod.ensure_managed_layout(root)

    assert result.changed is True, result.reason
    assert root.is_symlink()


def test_the_nudge_does_not_clobber_the_callers_last_background_pid(tmp_path):
    """F3/F6 — `$!` is caller state. Sourcing a helper must not overwrite the
    PID of a job the caller started, and job control must not print a line."""
    root = _fresh_clone(tmp_path, name="aqg")
    script = tmp_path / "caller.sh"
    script.write_text(
        "set -eu\n"
        "sleep 5 & mine=$!\n"
        f'export AQG_ROOT="{root}"\n'
        f'source "{_context_helper()}"\n'
        'echo "same=$([ \\"$!\\" = \\"$mine\\" ] && echo yes || echo no)"\n'
        'kill "$mine" 2>/dev/null || true\n',
        encoding="utf-8",
    )
    done = subprocess.run(["bash", str(script)], capture_output=True, text=True)
    assert "same=yes" in done.stdout, (
        f"the nudge overwrote the caller's $!: {done.stdout!r} {done.stderr!r}"
    )


def test_a_shallow_clone_survives_conversion_and_update(tmp_path):
    """F1/F9/F16 — decision-engine installs with `git clone --depth 1`, and no
    test covered that shape. One voice called it a fatal crash for every install
    made that way. Measuring it is the only way to settle the claim.

    `file://` is required: git silently ignores `--depth` for a local path.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    _git(origin, "config", "user.email", "t@example.com")
    _git(origin, "config", "user.name", "T")
    for i in range(3):
        (origin / "f.txt").write_text(f"{i}\n", encoding="utf-8")
        _git(origin, "add", "-A")
        _git(origin, "commit", "-qm", f"c{i}")

    root = tmp_path / "agent-quality-gates"
    subprocess.run(
        ["git", "clone", "-q", "--depth", "1", "--branch", "main",
         "--single-branch", f"file://{origin}", str(root)],
        capture_output=True, check=True,
    )
    assert _git(root, "rev-parse", "--is-shallow-repository").strip() == "true"
    assert _git(root, "rev-list", "--count", "HEAD").strip() == "1", "not shallow"

    assert migrate_mod.ensure_managed_layout(root, managed=True).changed is True
    assert root.is_symlink()

    # And the parts an update needs afterwards.
    (origin / "f.txt").write_text("new\n", encoding="utf-8")
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "c3")
    new = _git(origin, "rev-parse", "HEAD").strip()
    _git(root, "fetch", "-q", "--depth", "1", "origin", new)
    _git(root, "worktree", "add", "--detach", str(tmp_path / "probe"), new)
    assert (tmp_path / "probe" / "f.txt").read_text(encoding="utf-8") == "new\n"
