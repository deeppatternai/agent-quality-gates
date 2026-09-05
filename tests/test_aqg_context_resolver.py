"""Pytest cover for scripts/_aqg_context.sh + the bash self-test runner.

These tests run the helper through bash subprocesses (the helper must be
sourced, not executed) and assert against stdout / exit code so the
behavior table from sketch a2 §4 is enforced from CI.

Each test isolates env via env=... so cross-contamination cannot happen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HELPER = REPO_ROOT / "scripts" / "_aqg_context.sh"


def _source_and_print(env: dict[str, str], snippet: str = "echo $aqg_root") -> tuple[int, str, str]:
    """Source the helper in a clean bash, then run the snippet."""
    cmd = ["bash", "-c", f"source '{HELPER}' && {snippet}"]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr


def _source_observe_rc(env: dict[str, str]) -> tuple[int, str]:
    """Source the helper but tolerate failure; return rc + stderr."""
    cmd = ["bash", "-c", f"source '{HELPER}' 2>&1; echo rc=$?"]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    return proc.returncode, proc.stdout.strip()


def test_helper_file_exists():
    assert HELPER.is_file(), f"helper missing at {HELPER}"


def test_source_env_valid_path():
    """§4 row 1 source variant: AQG_ROOT set to valid checkout → success."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_REQUIRE_ENV": "1",
        "AQG_ROOT": str(REPO_ROOT),
    }
    rc, stdout, _ = _source_and_print(env)
    assert rc == 0
    assert stdout == str(REPO_ROOT)


def test_source_env_missing_returns_not_exits():
    """§4 row 3 source variant: AQG_ROOT unset → return 1 (caller survives)."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_REQUIRE_ENV": "1",
    }
    _, observe = _source_observe_rc(env)
    assert "rc=1" in observe, observe
    assert "source skill requires AQG_ROOT" in observe, observe


def test_source_env_invalid_path():
    """§4 row 2 source variant: AQG_ROOT set but path invalid → return 1."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_REQUIRE_ENV": "1",
        "AQG_ROOT": "/nonexistent/aqg/path",
    }
    _, observe = _source_observe_rc(env)
    assert "rc=1" in observe
    assert "AQG_ROOT points to invalid path" in observe


def test_wrapper_env_valid():
    """§4 row 1 wrapper variant: same as source — env set → use it."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    rc, stdout, _ = _source_and_print(env)
    assert rc == 0
    assert stdout == str(REPO_ROOT)


def test_wrapper_default_install_fallback():
    """§4 row 5 wrapper variant: default install path exists → use it."""
    fake_home = Path(tempfile.mkdtemp())
    try:
        install = fake_home / ".local" / "share" / "aqg" / "agent-quality-gates"
        install.mkdir(parents=True)
        (install / "VERSION").write_text("0.0.0-test")

        env = {"HOME": str(fake_home), "PATH": os.environ["PATH"]}
        rc, stdout, _ = _source_and_print(env)
        assert rc == 0
        # Physical path since the generation-pinning change: on macOS the temp
        # dir is itself reached through a symlink, so the raw and resolved forms
        # differ. Comparing against `.resolve()` is the new contract, not a
        # loosened assertion.
        assert Path(stdout) == install.resolve()
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


def test_wrapper_no_resolution():
    """§4 row 6: nothing set, nothing exists → return 1."""
    fake_home = Path(tempfile.mkdtemp())
    try:
        env = {"HOME": str(fake_home), "PATH": os.environ["PATH"]}
        _, observe = _source_observe_rc(env)
        assert "rc=1" in observe
        assert "cannot resolve AQG root" in observe
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


def test_wrapper_sentinel_resolution():
    """§4 row 3 wrapper variant: CLAUDE_SKILL_DIR/.aqg-root sentinel → use content."""
    fake_home = Path(tempfile.mkdtemp())
    try:
        skill_dir = fake_home / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (skill_dir / ".aqg-root").write_text(str(REPO_ROOT))

        env = {
            "HOME": str(fake_home),
            "PATH": os.environ["PATH"],
            "CLAUDE_SKILL_DIR": str(skill_dir),
        }
        rc, stdout, _ = _source_and_print(env)
        assert rc == 0
        assert stdout == str(REPO_ROOT)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


def test_wrapper_sentinel_invalid_falls_through_to_walkup():
    """sentinel pointing to invalid path falls through to walk-up."""
    fake_home = Path(tempfile.mkdtemp())
    try:
        # Build a fake checkout layout: <fake>/proj/(VERSION) +
        # <fake>/proj/agent-packs/claude-code/skills/foo/SKILL.md.
        # CLAUDE_SKILL_DIR walks up 4 levels back to <fake>/proj.
        # Use resolve() because helper uses `pwd -P` which dereferences
        # macOS /var → /private/var symlinks.
        proj = (fake_home / "proj").resolve()
        skill_dir = proj / "agent-packs" / "claude-code" / "skills" / "foo"
        skill_dir.mkdir(parents=True)
        (proj / "VERSION").write_text("0.0.0-test")
        (skill_dir / ".aqg-root").write_text("/nonexistent/path")  # invalid sentinel

        env = {
            "HOME": str(fake_home),
            "PATH": os.environ["PATH"],
            "CLAUDE_SKILL_DIR": str(skill_dir),
        }
        rc, stdout, _ = _source_and_print(env)
        assert rc == 0
        assert stdout == str(proj)
    finally:
        shutil.rmtree(fake_home, ignore_errors=True)


def test_direct_execution_refused():
    """Helper must be SOURCED, not EXECUTED."""
    proc = subprocess.run(
        ["bash", str(HELPER)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.returncode == 2
    assert "must be sourced" in proc.stderr


def test_no_scratch_leak():
    """Scratch vars (_aqgctx_*) must not leak into caller."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    rc, stdout, _ = _source_and_print(
        env,
        snippet='echo "args=$1,$2 leaked=${_aqgctx_candidate:-none}"',
    )
    assert rc == 0
    assert "leaked=none" in stdout


def test_caller_set_options_preserved():
    """Helper must not enable set -e / set -u in the caller shell."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    # Caller has neither -e nor -u set; after sourcing, $- should not contain
    # 'e' or 'u' that we did not enable ourselves.
    cmd = [
        "bash", "-c",
        f"before=$-; source '{HELPER}' && echo before=$before after=$-",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.returncode == 0
    line = proc.stdout.strip()
    # Parse "before=XXX after=YYY"
    parts = dict(p.split("=") for p in line.split())
    assert parts["before"] == parts["after"], (
        f"shell options changed: before={parts['before']!r} after={parts['after']!r}"
    )


def test_idempotent_sourcing():
    """Sourcing twice in same shell must produce same aqg_root, no error."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    cmd = [
        "bash", "-c",
        f"source '{HELPER}' && first=$aqg_root && source '{HELPER}' && "
        f"echo \"first=$first second=$aqg_root\"",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.returncode == 0
    assert f"first={REPO_ROOT} second={REPO_ROOT}" in proc.stdout


def test_no_function_leak_on_failure(tmp_path):
    """Audit #1: failure path must unset _aqgctx_resolve before returning."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "AQG_REQUIRE_ENV": "1",  # forces failure (no AQG_ROOT)
    }
    cmd = [
        "bash", "-c",
        f"source '{HELPER}' 2>/dev/null; "
        "if declare -F _aqgctx_resolve >/dev/null 2>&1; then "
        "  echo LEAKED; "
        "else "
        "  echo CLEAN; "
        "fi",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.stdout.strip() == "CLEAN", (
        f"_aqgctx_resolve leaked into caller after failed source.\n"
        f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
    )


def test_no_function_leak_on_success():
    """Audit #1: success path must also unset _aqgctx_resolve."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    cmd = [
        "bash", "-c",
        f"source '{HELPER}' && "
        "if declare -F _aqgctx_resolve >/dev/null 2>&1; then "
        "  echo LEAKED; "
        "else "
        "  echo CLEAN; "
        "fi",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.stdout.strip() == "CLEAN"


def test_aqg_root_exported_to_child_process():
    """Audit #2: child processes must see exported aqg_root."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": str(REPO_ROOT),
    }
    cmd = [
        "bash", "-c",
        f"source '{HELPER}' && "
        # Child process should inherit the exported aqg_root via env.
        "bash -c 'echo child=\"$aqg_root\"'",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert proc.returncode == 0
    assert f"child={REPO_ROOT}" in proc.stdout


def test_stale_aqg_root_does_not_bypass_require_env(tmp_path):
    """Audit #3: cached aqg_root must NOT bypass AQG_REQUIRE_ENV=1.

    A shell that already has aqg_root=/tmp/stale must still hard-fail
    when sourced with AQG_REQUIRE_ENV=1 and no AQG_ROOT env var.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "AQG_REQUIRE_ENV": "1",
    }
    cmd = [
        "bash", "-c",
        f"aqg_root=/tmp/stale-from-prior-source; "
        f"source '{HELPER}' 2>&1; echo rc=$?",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert "rc=1" in proc.stdout, f"stale aqg_root bypassed AQG_REQUIRE_ENV: {proc.stdout!r}"
    assert "source skill requires AQG_ROOT" in proc.stdout


def test_stale_aqg_root_does_not_bypass_invalid_env(tmp_path):
    """Audit #3: cached aqg_root must NOT mask invalid AQG_ROOT env."""
    env = {
        "HOME": os.environ.get("HOME", ""),
        "PATH": os.environ["PATH"],
        "AQG_ROOT": "/nonexistent/aqg",  # invalid — should hard-fail
    }
    cmd = [
        "bash", "-c",
        f"aqg_root='{REPO_ROOT}'; source '{HELPER}' 2>&1; echo rc=$?",
    ]
    proc = subprocess.run(
        cmd, env=env, text=True,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=10, check=False,
    )
    assert "rc=1" in proc.stdout, f"cached aqg_root masked invalid env: {proc.stdout!r}"
    assert "AQG_ROOT points to invalid path" in proc.stdout


def test_sentinel_path_with_whitespace(tmp_path):
    """Audit #4: sentinel content with embedded space must resolve correctly."""
    # Create a checkout in a directory with a space.
    proj = (tmp_path / "aqg root with space").resolve()
    skill_dir = proj / "agent-packs" / "claude-code" / "skills" / "foo"
    skill_dir.mkdir(parents=True)
    (proj / "VERSION").write_text("0.0.0-test")
    # Sentinel content includes the embedded space — old `tr -d '[:space:]'`
    # would strip the space and fail; new IFS= read -r preserves it.
    (skill_dir / ".aqg-root").write_text(str(proj))

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    env = {
        "HOME": str(fake_home),
        "PATH": os.environ["PATH"],
        "CLAUDE_SKILL_DIR": str(skill_dir),
    }
    rc, stdout, _ = _source_and_print(env)
    assert rc == 0
    assert stdout == str(proj), f"sentinel with whitespace lost: {stdout!r}"


def test_bash_self_test_runner_passes():
    """The companion bash self-test must pass too (cross-check)."""
    runner = REPO_ROOT / "tests" / "test_aqg_context_resolver.sh"
    proc = subprocess.run(
        ["bash", str(runner)],
        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        timeout=30, check=False,
    )
    assert proc.returncode == 0, f"bash self-test failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
    assert "12/12 PASS" in proc.stdout


# --- generation pinning (docs/UPDATE_ARCHITECTURE.md §5.2) ---------------------
#
# The managed update swaps the root symlink to a new version tree while sessions
# are running. A skill run that resolved the root before the swap and then
# invokes another file under it afterwards would span two generations — one
# logical operation reading half of each. Exporting the PHYSICAL path pins a run
# to the tree it started on.
#
# Scope, stated because it is narrower than it sounds: this pins a SKILL RUN.
# Hooks reference $AQG_ROOT directly in their command strings and never source
# this helper, so each hook invocation resolves the link independently. That is
# fine — a hook is one short-lived file execution, not a multi-step operation —
# but it is not pinned by this change.


def _versioned_install(base: Path) -> tuple[Path, Path, Path]:
    """A root symlink pointing at one of two version trees."""
    versions = base / "versions"
    first = versions / "v1"
    second = versions / "v2"
    for tree, tag in ((first, "v1"), (second, "v2")):
        tree.mkdir(parents=True)
        (tree / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        (tree / "marker").write_text(tag, encoding="utf-8")
    root = base / "agent-quality-gates"
    root.symlink_to(first, target_is_directory=True)
    return root, first, second


def test_a_symlinked_root_resolves_to_the_version_tree():
    with tempfile.TemporaryDirectory() as tmp:
        root, first, _ = _versioned_install(Path(tmp))
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": str(root)}
        rc, out, err = _source_and_print(env)
        assert rc == 0, err
        assert Path(out.strip()) == first.resolve()


def test_a_resolved_run_is_not_moved_by_a_swap_underneath_it():
    """The property this change exists for: one logical operation stays on one
    tree even if the root is re-pointed while it runs."""
    with tempfile.TemporaryDirectory() as tmp:
        root, first, second = _versioned_install(Path(tmp))
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": str(root)}
        # `mv` onto a symlink-to-directory follows it and moves INTO the tree,
        # so the swap is done the way it actually happens: remove and relink.
        # Reading THROUGH the pinned path on both sides. Comparing the variable
        # alone would only prove the variable did not change; the property is
        # that a file access still reaches the tree the run started on.
        swap = (
            f'echo "$(cat "$aqg_root/marker")"; '
            f"rm -f '{root}' && ln -s '{second}' '{root}'; "
            f'echo "$(cat "$aqg_root/marker")"; echo $aqg_root'
        )
        rc, out, err = _source_and_print(env, snippet=swap)
        assert rc == 0, err
        lines = out.strip().splitlines()
        assert lines[0] == lines[1] == "v1", lines
        assert Path(lines[2]) == first.resolve()
        # ...and the swap really did happen, so the test is not vacuous.
        assert Path(os.path.realpath(root)) == second.resolve()


def test_a_plain_directory_root_is_unchanged():
    """The common case today: no symlink anywhere. Must behave exactly as before."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "checkout"
        root.mkdir()
        (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": str(root)}
        rc, out, err = _source_and_print(env)
        assert rc == 0, err
        assert Path(out.strip()) == root.resolve()


def test_a_root_with_a_trailing_slash_is_normalized():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "checkout"
        root.mkdir()
        (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": f"{root}/"}
        rc, out, err = _source_and_print(env)
        assert rc == 0, err
        assert out.strip() == str(root.resolve())


def test_a_relative_root_becomes_absolute():
    """A relative AQG_ROOT would otherwise be re-resolved against whatever
    directory each child process happens to run in."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "checkout"
        root.mkdir()
        (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": "checkout"}
        cmd = ["bash", "-c", f"cd '{tmp}' && source '{HELPER}' && echo $aqg_root"]
        proc = subprocess.run(
            cmd, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert Path(proc.stdout.strip()) == root.resolve()


def test_the_default_install_path_is_also_physical():
    """Rows other than the env row must pin too, or the property depends on how
    the root happened to be discovered."""
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        real = Path(tmp) / "real-checkout"
        real.mkdir()
        (real / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        (home / ".deeppattern").mkdir(parents=True)
        (home / ".deeppattern" / "agent-quality-gates").symlink_to(
            real, target_is_directory=True
        )
        env = {"PATH": os.environ["PATH"], "HOME": str(home)}
        rc, out, err = _source_and_print(env)
        assert rc == 0, err
        assert Path(out.strip()) == real.resolve()


def test_an_unreadable_root_still_fails_the_same_way():
    """Resolution must not turn a clear refusal into a confusing one."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {
            "PATH": os.environ["PATH"],
            "HOME": tmp,
            "AQG_ROOT": str(Path(tmp) / "nope"),
        }
        rc, _out, err = _source_and_print(env)
        assert rc == 1
        assert "AQG_ROOT points to invalid path" in err


def test_a_hostile_cdpath_cannot_redirect_resolution():
    """`cd` consults CDPATH for a relative operand and lands somewhere else —
    and then prints the directory it chose, corrupting the substitution too."""
    with tempfile.TemporaryDirectory() as tmp:
        real = Path(tmp) / "real" / "checkout"
        decoy = Path(tmp) / "decoy" / "checkout"
        for tree, tag in ((real, "real"), (decoy, "decoy")):
            tree.mkdir(parents=True)
            (tree / "VERSION").write_text(tag, encoding="utf-8")
        env = {
            "PATH": os.environ["PATH"],
            "HOME": tmp,
            "AQG_ROOT": "checkout",
            "CDPATH": str(decoy.parent),
        }
        cmd = [
            "bash",
            "-c",
            f"cd '{real.parent}' && source '{HELPER}' && echo $aqg_root",
        ]
        proc = subprocess.run(
            cmd, env=env, text=True, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, timeout=10, check=False,
        )
        assert proc.returncode == 0, proc.stderr
        assert Path(proc.stdout.strip()) == real.resolve()


def test_a_root_directory_named_like_an_option_still_resolves():
    """Without `--`, `cd` reads a leading hyphen as options and a previously
    working install becomes a hard failure."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "-dashed"
        root.mkdir()
        (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": str(root)}
        rc, out, err = _source_and_print(env)
        assert rc == 0, err
        assert Path(out.strip()) == root.resolve()


def test_a_root_whose_name_ends_in_a_newline_is_refused_not_truncated():
    """Command substitution strips trailing newlines, so such a path would
    resolve to a silently truncated one. Refusing is the honest answer."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "checkout\n"
        try:
            root.mkdir()
        except OSError:  # pragma: no cover - filesystem forbids it
            pytest.skip("filesystem rejects a newline in a directory name")
        (root / "VERSION").write_text("0.15.0\n", encoding="utf-8")
        env = {"PATH": os.environ["PATH"], "HOME": tmp, "AQG_ROOT": str(root)}
        rc, _out, err = _source_and_print(env)
        assert rc == 1
        assert "AQG_ROOT" in err
