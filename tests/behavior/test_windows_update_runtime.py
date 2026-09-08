"""Native Windows update regressions, also exercised on POSIX CI."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from scripts.aqg_update import migrate, stage
from scripts import install_aqg_clients as installer


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    home = tmp_path / "profile with spaces"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("AQG_STATE_ROOT", str(tmp_path / "state"))


def git(root, *args):
    return subprocess.run(
        ["git", *args], cwd=root, capture_output=True, text=True,
        encoding="utf-8", check=True, timeout=30,
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.invalid")
    (root / "VERSION").write_text("1\n", encoding="utf-8")
    git(root, "add", "VERSION")
    git(root, "commit", "-qm", "first")
    return root


def test_migrated_root_remains_a_process_working_directory(checkout):
    commit = git(checkout, "rev-parse", "HEAD")
    migrate.migrate(checkout)
    assert checkout.is_dir()
    assert git(checkout, "rev-parse", "HEAD") == commit


def test_migration_preserves_an_open_skill_reader(checkout):
    with (checkout / "VERSION").open(encoding="utf-8") as reader:
        if os.name == "nt":
            with pytest.raises(migrate.MigrateError, match="Close applications"):
                migrate.migrate(checkout)
            assert not checkout.is_symlink()
        else:
            migrate.migrate(checkout)
        assert reader.read() == "1\n"
        assert (checkout / "VERSION").read_text(encoding="utf-8") == "1\n"
    migrate.migrate(checkout)
    assert checkout.is_dir()


def test_installer_migrates_when_launched_inside_the_checkout(checkout, monkeypatch):
    monkeypatch.setenv("AQG_MIGRATE", "1")
    monkeypatch.chdir(checkout)
    installer._ensure_update_layout(checkout, install_succeeded=True)
    assert checkout.is_symlink()
    assert Path.cwd().samefile(checkout)


@pytest.mark.skipif(os.name != "nt", reason="native Windows cwd restoration")
def test_interrupted_migration_preserves_diagnostic_and_recovery(checkout, monkeypatch, capsys):
    monkeypatch.setenv("AQG_MIGRATE", "1")
    commit = git(checkout, "rev-parse", "HEAD")
    target = checkout.parent / "versions" / commit
    monkeypatch.chdir(checkout)
    real_rename = os.rename

    def rename(source, destination):
        if Path(source) == checkout:
            return real_rename(source, destination)
        raise PermissionError("fixture blocks link placement and rollback")

    monkeypatch.setattr(migrate.os, "rename", rename)
    installer._ensure_update_layout(checkout, install_succeeded=True)

    output = capsys.readouterr()
    assert "cannot put the link in place" in output.err
    assert "cannot restore working directory" in output.err
    assert "automatic updates are NOT enabled" in output.err
    assert "AQG works normally" not in output.err
    assert "AQG-MIGRATION-INTERRUPTED.txt" in output.err
    assert not checkout.exists()
    assert (target / "VERSION").read_text(encoding="utf-8") == "1\n"
    assert (checkout.parent / migrate.SIGNPOST_FILENAME).is_file()
    assert Path.cwd() == checkout.parent


@pytest.mark.parametrize("action", ["--verify", "--is-installed", "--uninstall"])
def test_non_install_actions_preserve_checkout_layout(checkout, monkeypatch, action):
    monkeypatch.setenv("AQG_MIGRATE", "1")
    # Client adapters succeed; observe the wrapper's real filesystem side effect.
    monkeypatch.setattr(installer, "_execute_commands", lambda *args: ())
    assert installer.main(["--clients", "codex", "--aqg-root", str(checkout), action]) == 0
    assert not checkout.is_symlink()
    assert not (checkout.parent / "versions").exists()


def test_explicit_install_home_reaches_native_children(checkout, tmp_path):
    home = tmp_path / "other profile"
    home.mkdir()
    env = installer._execution_env(installer._collect_context(str(checkout)), None, home)
    result = subprocess.run(
        [os.sys.executable, "-E", "-s", "-c", "from pathlib import Path; print(Path.home())"],
        env=env, capture_output=True, text=True, check=True, timeout=15,
    )
    assert Path(result.stdout.strip()) == home


def test_swapped_root_and_rollback_remain_working_directories(checkout, tmp_path):
    first = git(checkout, "rev-parse", "HEAD")
    (checkout / "VERSION").write_text("2\n", encoding="utf-8")
    git(checkout, "commit", "-qam", "second")
    second = git(checkout, "rev-parse", "HEAD")
    versions = tmp_path / "versions"
    trees = [stage.stage_version(repo=checkout, commit=commit,
             versions_dir=versions, name=commit) for commit in (first, second)]
    root = tmp_path / "live 名称 with spaces"
    for tree, commit in zip((trees[0], trees[1], trees[0]), (first, second, first)):
        stage.swap_root(root=root, target=tree)
        assert root.is_dir()
        assert git(root, "rev-parse", "HEAD") == commit


def test_failed_swap_preserves_root_and_cleans_temporary_link(checkout, tmp_path, monkeypatch):
    commit = git(checkout, "rev-parse", "HEAD")
    tree = stage.stage_version(repo=checkout, commit=commit,
                               versions_dir=tmp_path / "versions", name=commit)
    root = tmp_path / "live"
    stage.swap_root(root=root, target=tree)

    def denied(*args):
        raise PermissionError("simulated sharing violation")

    monkeypatch.setattr(stage, "_replace_root_link", denied)
    with pytest.raises(stage.StageError, match="sharing violation"):
        stage.swap_root(root=root, target=tree)
    assert git(root, "rev-parse", "HEAD") == commit
    assert list(tmp_path.glob(".aqg-root-*")) == []


def test_launcher_child_resolves_profile_without_interpreter_steering(tmp_path):
    bash = shutil.which("bash")
    if bash is None and os.name == "nt":
        bash = str(Path(shutil.which("git")).resolve().parents[1] / "bin/bash.exe")
    if not bash:
        pytest.skip("Bash is required for the launcher")
    root = tmp_path / "install"
    module = root / "scripts/aqg_update/run.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "import json, os, ntpath\nfrom pathlib import Path\n"
        "print(json.dumps({'home':str(Path.home()), 'windows_home':ntpath.expanduser('~'),"
        "'steering':[k for k in ('PYTHONPATH','PYTHONHOME') if k in os.environ]}))\n",
        encoding="utf-8",
    )
    launcher = (Path(__file__).resolve().parents[2] /
                "agent-packs/claude-code/hooks/sessionstart_update_check.sh").read_text(encoding="utf-8")
    # Run the exact child environment and command synchronously so completion,
    # rather than a sleep or a file-poll deadline, is the assertion boundary.
    launcher = launcher.replace("nohup ", "").replace(
        "</dev/null >/dev/null 2>&1 &", ""
    )
    env = {**os.environ, "AQG_ROOT": str(root), "AQG_NO_UPDATE_CHECK": "",
           "PYTHONPATH": "untrusted-path", "PYTHONHOME": "untrusted-home"}
    result = subprocess.run([bash, "--noprofile", "--norc", "-c", launcher],
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.stdout.strip(), result.stderr
    child = json.loads(result.stdout)
    assert Path(child["home"]) == Path(os.environ["HOME"])
    assert child["windows_home"] == os.environ["USERPROFILE"]
    assert child["steering"] == []
