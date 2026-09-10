"""The two Python CLI nudges cannot interfere with foreground work."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[2]
ENTRIES = (
    'skills/aqg-startup-preflight/scripts/aqg_preflight.py',
    'skills/aqg-code-construction/scripts/aqg_construction_check.py',
)


def load_nudge():
    spec = importlib.util.spec_from_file_location('tested_nudge', ROOT / 'scripts/aqg_update/nudge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def managed(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    tree = home / '.deeppattern/versions/v1'
    (tree / 'scripts/aqg_update').mkdir(parents=True)
    (tree / 'scripts/aqg_update/run.py').touch()
    (tree / 'scripts/aqg_update/nudge.py').touch()
    root = home / '.deeppattern/agent-quality-gates'
    root.symlink_to(tree, target_is_directory=True)
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.delenv('AQG_NO_UPDATE_CHECK', raising=False)
    mod = load_nudge()
    mod.__file__ = str(tree / 'scripts/aqg_update/nudge.py')
    return mod, root, tree


@pytest.mark.parametrize('platform', ['win32', 'linux', 'darwin'])
def test_detached_launch_preserves_network_env_and_isolates_io(managed, monkeypatch, platform):
    mod, root, tree = managed
    calls = []
    monkeypatch.setattr(mod.sys, 'platform', platform)
    monkeypatch.setenv('PYTHONPATH', '/untrusted/project')
    monkeypatch.setenv('HTTPS_PROXY', 'http://proxy.invalid:8080')
    monkeypatch.setenv('SSL_CERT_FILE', '/local/ca.pem')
    for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'):
        monkeypatch.setenv(name, '/untrusted/project')
    monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **k: calls.append((a, k)))
    mod.nudge()
    assert len(calls) == 1
    args, kw = calls[0]
    assert args[0] == [sys.executable, '-E', '-s', '-m', 'scripts.aqg_update.run']
    assert Path(kw['cwd']) == tree
    assert Path(kw['env']['AQG_ROOT']) == tree
    assert 'PYTHONPATH' not in kw['env']
    assert not any(name in kw['env'] for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0', 'GIT_CONFIG_VALUE_0'))
    assert kw['env']['HTTPS_PROXY'] == 'http://proxy.invalid:8080'
    assert kw['env']['SSL_CERT_FILE'] == '/local/ca.pem'
    assert kw['env']['AQG_UPDATE_TRIGGER'] == 'python-skill'
    assert all(kw[k] == subprocess.DEVNULL for k in ('stdin', 'stdout', 'stderr'))
    assert kw['close_fds'] is True
    if platform == 'win32':
        assert kw['creationflags'] & 0x08000000  # CREATE_NO_WINDOW
        assert not kw['creationflags'] & 0x8  # incompatible DETACHED_PROCESS
        assert kw['creationflags'] & 0x200  # CREATE_NEW_PROCESS_GROUP
    else:
        assert kw['start_new_session'] is True


def test_launch_failure_does_not_latch_next_attempt(managed, monkeypatch, capsys):
    mod, root, tree = managed
    calls = []
    def launch(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise OSError('temporary process limit')
    monkeypatch.setattr(mod.subprocess, 'Popen', launch)
    mod.nudge()
    mod.nudge()
    assert len(calls) == 2
    assert capsys.readouterr() == ('', '')


@pytest.mark.parametrize('script,source', [
    ('aqg_preflight.py', 'aqg-startup-preflight'),
    ('aqg_construction_check.py', 'aqg-code-construction'),
])
def test_skill_trigger_replaces_inherited_source(managed, monkeypatch, script, source):
    mod, _, _ = managed
    calls = []
    monkeypatch.setattr(mod.sys, 'argv', [script])
    monkeypatch.setenv('AQG_UPDATE_TRIGGER', 'session-hook')
    monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **k: calls.append(k))
    mod.nudge()
    assert calls[0]['env']['AQG_UPDATE_TRIGGER'] == source


def test_link_swap_before_child_admission_cannot_redirect_the_runner(managed, monkeypatch, tmp_path):
    from scripts.aqg_update import run
    mod, root, tree = managed
    replacement = root.parent / 'versions/v2'
    replacement.mkdir()
    results = []
    monkeypatch.setattr(run.acquire, 'available_release', lambda *a, **k: pytest.fail('obsolete child must not fetch'))
    def launch(args, **kwargs):
        root.unlink()
        root.symlink_to(replacement, target_is_directory=True)
        results.append(run.check(
            root=Path(kwargs['env']['AQG_ROOT']), remote='origin', channel='stable',
            state_root=tmp_path / 'state', keyring_path=ROOT / 'scripts/aqg_update/release-trust.json',
        ))
    monkeypatch.setattr(mod.subprocess, 'Popen', launch)
    mod.nudge()
    assert len(results) == 1 and results[0].outcome == 'invalid-root'


@pytest.mark.parametrize('disabled', [True, False])
def test_disabled_or_developer_checkout_never_launches(managed, monkeypatch, tmp_path, disabled):
    mod, root, tree = managed
    if disabled:
        monkeypatch.setenv('AQG_NO_UPDATE_CHECK', '1')
    else:
        mod.__file__ = str(tmp_path / 'checkout/scripts/aqg_update/nudge.py')
        monkeypatch.setenv('AQG_ROOT', str(root))
    monkeypatch.setattr(mod.subprocess, 'Popen', lambda *a, **k: pytest.fail('unexpected updater'))
    mod.nudge()


@pytest.mark.parametrize('entry', ENTRIES)
@pytest.mark.parametrize('failure', ['import', 'launch', 'missing', 'system-exit'])
@pytest.mark.parametrize('argument,expected_exit', [('--help', 0), ('--unknown-argument', 2)])
def test_cli_output_and_exit_survive_broken_trigger(tmp_path, entry, failure, argument, expected_exit):
    dest = tmp_path / entry
    dest.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / entry, dest)
    trigger = tmp_path / 'scripts/aqg_update/nudge.py'
    trigger.parent.mkdir(parents=True)
    marker = tmp_path / 'called'
    if failure != 'missing':
        code = f'from pathlib import Path\nPath({str(marker)!r}).touch()\n'
        code += ('raise RuntimeError("broken import")\n' if failure == 'import'
                 else 'raise SystemExit(99)\n' if failure == 'system-exit'
                 else 'def nudge():\n    raise RuntimeError("broken launch")\n')
        trigger.write_text(code, encoding='utf-8')
    env = dict(os.environ, AQG_NO_UPDATE_CHECK='1', AQG_AGENT='codex')
    baseline = subprocess.run([sys.executable, str(dest), argument], env=env, capture_output=True)
    env.pop('AQG_NO_UPDATE_CHECK')
    attempt = subprocess.run([sys.executable, str(dest), argument], env=env, capture_output=True)
    assert attempt.returncode == baseline.returncode == expected_exit
    assert (attempt.stdout, attempt.stderr) == (baseline.stdout, baseline.stderr)
    if failure != 'missing':
        assert marker.exists(), 'CLI never attempted the shared trigger'


def test_native_child_survives_foreground_exit_and_cannot_hold_its_pipes(managed, tmp_path):
    mod, root, tree = managed
    shutil.copyfile(ROOT / 'scripts/aqg_update/nudge.py', tree / 'scripts/aqg_update/nudge.py')
    ready, release, done = [tmp_path / name for name in ('ready', 'release', 'done')]
    git = shutil.which('git')
    assert git, 'Git is required by the updater'
    git_env = {k: v for k, v in os.environ.items() if not k.upper().startswith('GIT_')}
    git_env.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull)
    repo = tmp_path / 'git-repo'
    subprocess.run([git, 'init', str(repo)], env=git_env, check=True, capture_output=True, timeout=10)
    (repo / 'VERSION').write_text('1.0.0\n', encoding='utf-8')
    subprocess.run([git, '-C', str(repo), 'add', 'VERSION'], env=git_env, check=True, capture_output=True, timeout=10)
    subprocess.run(
        [git, '-C', str(repo), '-c', 'user.name=AQG Test', '-c',
         'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
         'commit', '-m', 'fixture'], env=git_env, check=True, capture_output=True, timeout=10,
    )
    (tree / 'scripts/aqg_update/run.py').write_text(
        'from pathlib import Path\nimport time, sys, subprocess, json\n'
        'print("background stdout", flush=True)\nprint("background stderr", file=sys.stderr, flush=True)\n'
        f'Path({str(ready)!r}).touch()\n'
        'deadline = time.monotonic() + 15\n'
        f'while not Path({str(release)!r}).exists() and time.monotonic() < deadline:\n'
        '    time.sleep(0.02)\n'
        'print("background after foreground exit", flush=True)\n'
        'results = []\n'
        'for _ in range(12):\n'
        '    try:\n'
        f'        result = subprocess.run([{git!r}, "-C", {str(repo)!r}, "ls-tree",\n'
        '                                 "--name-only", "HEAD"], capture_output=True, timeout=5)\n'
        '    except (subprocess.TimeoutExpired, OSError) as exc:\n'
        '        results.append([type(exc).__name__, str(exc)])\n'
        '        break\n'
        '    results.append([result.returncode, result.stdout.decode(), result.stderr.decode()])\n'
        f'output = Path({str(done.with_suffix(".tmp"))!r})\n'
        'output.write_text(json.dumps(results), encoding="utf-8")\n'
        f'output.replace({str(done)!r})\n', encoding='utf-8',
    )
    home = root.parent.parent
    env = dict(git_env, HOME=str(home), USERPROFILE=str(home))
    env.pop('AQG_NO_UPDATE_CHECK', None)
    try:
        parent = subprocess.run(
            [sys.executable, '-E', '-s', '-c',
             'from scripts.aqg_update.nudge import nudge; nudge(); print("foreground done")'],
            cwd=tree, env=env, capture_output=True, timeout=5,
        )
        assert parent.returncode == 0
        assert parent.stdout.strip() == b'foreground done'
        assert parent.stderr == b''
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists()
        assert not done.exists(), 'child must still be waiting after foreground exit'
    finally:
        release.touch()
    deadline = time.monotonic() + 65  # twelve 5-second calls plus completion margin
    while not done.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert done.exists(), 'detached updater must survive parent exit'
    # Native integration coverage is environment-dependent; the parametrized
    # creationflags assertion above deterministically guards against reverting.
    assert json.loads(done.read_text(encoding='utf-8')) == [[0, 'VERSION\n', '']] * 12
