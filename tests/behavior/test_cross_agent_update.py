"""Shared update behavior across the registered lifecycle adapters."""
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('module_path', [
    'scripts/agent_client_aqg_hook.py', 'scripts/pi_aqg_hook.py',
    'agent-packs/qoder/hooks/qoder_hook_adapter.py', 'scripts/cursor_aqg_hook.py',
])
def test_lifecycle_adapter_preserves_the_update_entrance(tmp_path, monkeypatch, module_path):
    repo = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location('fixture_adapter', repo / module_path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    tree = tmp_path / 'versions/1.0.0'
    hooks = tree / 'agent-packs/claude-code/hooks'
    hooks.mkdir(parents=True)
    (tree / 'VERSION').write_text('1.0.0')
    (hooks / 'sessionstart_update_check.sh').write_text('# fixture')
    root = tmp_path / 'aqg'
    root.symlink_to(tree, target_is_directory=True)
    monkeypatch.setattr(adapter.sys, 'stdin', io.TextIOWrapper(io.BytesIO(json.dumps({'cwd':str(tmp_path)}).encode())))
    monkeypatch.setattr(adapter, '_find_bash', lambda: 'fixture-bash')
    seen = []
    def capture(*args, **kwargs):
        seen.append(kwargs['env']['AQG_ROOT'])
        return SimpleNamespace(returncode=0, stdout='', stderr='')
    if 'cursor' in module_path:
        monkeypatch.setattr(adapter.subprocess, 'Popen', capture)
        monkeypatch.setattr(adapter, '_session_start', lambda payload, aqg_root, project: adapter._trigger_update_check(aqg_root) or 0)
        argv = ['sessionStart', '--aqg-root', str(root)]
    else:
        monkeypatch.setattr(adapter.subprocess, 'run', capture)
        argv = ['--aqg-root', str(root), '--hook', 'sessionstart_update_check.sh', '--managed-id', adapter.MANAGED_ID]
        if 'agent_client' in module_path:
            argv += ['--client', 'trae']
    assert adapter.main(argv) == 0
    assert len(seen) == 1
    assert Path(seen[0]).absolute() == root.absolute()


def test_every_registered_client_has_explicit_update_evidence():
    from scripts.aqg_client_registry import CLIENT_REGISTRY
    from scripts.aqg_update.hosts import available_clients
    assert set(available_clients()) == set(CLIENT_REGISTRY)


@pytest.mark.parametrize('client', ['cursor', 'codebuddy', 'workbuddy-ai', 'kimi-code', 'qoder', 'qoder-cn', 'qoder-cli', 'qoder-cli-cn', 'qoderwork', 'trae', 'trae-cn', 'devin', 'pi'])
def test_other_hosts_inspect_missing_current_and_drifted_configuration(tmp_path, client):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    repo = Path(__file__).resolve().parents[2]
    adapter = ManagedAdapter(client, home=tmp_path, aqg_root=repo)
    assert adapter.verify().hooks_status == 'missing'
    path, expected = adapter.canonical_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding='utf-8')
    assert adapter.verify().hooks_status == 'complete'
    # A copied old physical command must be stale even while that tree exists.
    changed = expected.replace('--aqg-root', '--wrong-root')
    assert changed != expected
    path.write_text(changed, encoding='utf-8')
    assert adapter.verify().hooks_status == 'stale'
    path.write_text('{broken agent_client_aqg_hook.py', encoding='utf-8')
    assert adapter.verify().hooks_status in ('invalid', 'stale')


@pytest.mark.parametrize('client', ['cursor', 'codebuddy', 'workbuddy-ai', 'kimi-code', 'qoder-cli', 'qoder-cli-cn', 'qoder', 'qoder-cn', 'qoderwork', 'trae', 'trae-cn', 'devin', 'pi'])
def test_all_managed_renderers_keep_logical_paths_and_reject_physical_pins(tmp_path, client):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    repo = Path(__file__).resolve().parents[2]
    entrance = tmp_path / 'stable-entrance-marker'
    entrance.symlink_to(repo, target_is_directory=True)
    adapter = ManagedAdapter(client, home=tmp_path / 'home', aqg_root=entrance)
    path, expected = adapter.canonical_config()
    assert 'stable-entrance-marker' in expected
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(expected, encoding='utf-8')
    assert adapter.verify().hooks_status == 'complete'
    _, pinned = ManagedAdapter(client, home=tmp_path / 'home', aqg_root=repo).canonical_config()
    path.write_text(pinned, encoding='utf-8')
    assert adapter.verify().hooks_status == 'stale'


@pytest.mark.parametrize('client', ['cursor', 'codebuddy', 'trae', 'devin', 'qoder-cli'])
def test_foreign_hooks_survive_inspection_and_malformed_hooks_are_not_missing(tmp_path, client):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    adapter = ManagedAdapter(client, home=tmp_path, aqg_root=Path(__file__).resolve().parents[2])
    path, rendered = adapter.canonical_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(rendered)
    hooks = data if client == 'trae' else data['hooks']
    hooks['ForeignEvent'] = [{'command': 'echo user-owned'}]
    path.write_text(json.dumps(data), encoding='utf-8')
    before = path.read_bytes()
    assert adapter.verify().hooks_status == 'complete'
    assert path.read_bytes() == before
    hooks['ForeignEvent'] = [{'type': 'prompt', 'prompt': 'user owned'}]
    path.write_text(json.dumps(data), encoding='utf-8')
    assert adapter.verify().hooks_status == 'complete'


def _install_actual_hooks(adapter, monkeypatch, *, shared=False):
    """Invoke installer writers, never the inspector's canonical_config renderer."""
    m, client, root = adapter.installer, adapter.client_id, adapter.root
    path, _ = adapter.canonical_config()
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv('AQG_BACKUP_DIR', str(adapter.home / 'backups'))
    if client == 'cursor':
        monkeypatch.setattr(m, 'REPO_ROOT', root)
        monkeypatch.setattr(m, 'ADAPTER', root / 'scripts/cursor_aqg_hook.py')
        m._merge_hooks(path)
    elif client == 'pi':
        m._install_extension(path, adapter.home / '.pi/agent', root)
    elif m.__name__.endswith('install_aqg_qoder'):
        clients = [client, 'qoder-cli'] if shared else [client]
        for profile in clients:
            assert m.main(['--client', profile, '--home', str(adapter.home), '--aqg-root', str(root), '--mode', 'link', '--apply']) == 0
    elif m.__name__.endswith('install_aqg_work_clients'):
        m._install_hooks(path.parent, m.PROFILES[client], root)
    else:
        m._install_hooks(path, path.parent, m.PROFILES[client], 'user', root)
    return path


@pytest.mark.parametrize('client', ['cursor', 'codebuddy', 'workbuddy-ai', 'kimi-code', 'qoder', 'qoder-cn', 'qoder-cli', 'qoder-cli-cn', 'qoderwork', 'trae', 'trae-cn', 'devin', 'pi'])
def test_inspector_matches_actual_installer_output(tmp_path, monkeypatch, client):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    entrance = tmp_path / 'entrance'
    entrance.symlink_to(Path(__file__).resolve().parents[2], target_is_directory=True)
    adapter = ManagedAdapter(client, home=tmp_path/'home', aqg_root=entrance)
    path = _install_actual_hooks(adapter, monkeypatch)
    assert adapter.verify().hooks_status == 'complete', adapter.verify().hooks_detail
    owned = path.with_name(path.name + '.owned')
    path.rename(owned)
    path.symlink_to(owned)
    assert adapter.verify().hooks_status == 'complete'


def test_shared_qoder_owner_ledger_matches_real_installer(tmp_path, monkeypatch):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    adapter = ManagedAdapter('qoder', home=tmp_path, aqg_root=Path(__file__).resolve().parents[2])
    _install_actual_hooks(adapter, monkeypatch, shared=True)
    assert adapter.verify().hooks_status == 'complete'
    sibling = ManagedAdapter('qoder-cli', home=tmp_path, aqg_root=adapter.root)
    assert sibling.verify().hooks_status == 'complete'


@pytest.mark.parametrize('symlink', [False, True])
def test_unselected_foreign_config_does_not_block_a_plan(tmp_path, symlink):
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    from scripts.aqg_update.plan import build_plan
    adapter = ManagedAdapter('cursor', home=tmp_path/'home', aqg_root=tmp_path/'current')
    path, _ = adapter.canonical_config()
    path.parent.mkdir(parents=True)
    path.write_text('{malformed foreign content')
    if symlink:
        target = path.with_suffix('.owned')
        path.rename(target)
        path.symlink_to(target)
    target = tmp_path / 'target'
    (target / 'skills').mkdir(parents=True)
    (target / 'VERSION').write_text('1.0.0')
    evidence = adapter.verify()
    assert evidence.hooks_status == 'missing'
    built = build_plan(state=None, target=target, evidence={'cursor':evidence}, target_commit='abc')
    assert built.deferred == ()
    assert not built.touches_host_config
    recorded = adapter.verify(state={'hosts': {'cursor': {'last_applied_version':'1.0.0'}}})
    assert recorded.hooks_status == 'stale'
    path.write_text('{malformed cursor_aqg_hook.py')
    assert adapter.verify().hooks_status == 'invalid'


@pytest.mark.parametrize('client', ['cursor','codebuddy','kimi-code','qoder-cli','trae','pi'])
def test_managed_hook_bodies_follow_swap_but_renderer_changes_require_merge(tmp_path, client):
    from scripts.aqg_update.hosts.managed import FAMILIES
    from scripts.aqg_update.hosts.base import Evidence
    from scripts.aqg_update.plan import build_plan
    from scripts.aqg_update import transaction, dispatch
    current, target = tmp_path/'versions/old', tmp_path/'versions/new'
    for tree in (current, target):
        (tree/'skills').mkdir(parents=True)
        (tree/'VERSION').write_text('1.0.0')
        for name in [f'scripts/{FAMILIES[client]}.py', 'scripts/aqg_client_registry.py',
                     'agent-packs/claude-code/hooks/start.sh']:
            path = tree/name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# original')
    (target/'agent-packs/claude-code/hooks/start.sh').write_text('# changed body')
    (target/'scripts/cursor_aqg_hook.py').write_text('# changed adapter body')
    (target/'scripts/install_unrelated.py').write_text('# changed foreign installer')
    evidence = {client: Evidence(client, 'complete', 'actual canonical config', None)}
    def plan():
        return build_plan(state=None, current=current, target=target, evidence=evidence, target_commit='abc')
    built = plan()
    assert not built.touches_host_config
    root = tmp_path/'entrance'
    root.symlink_to(current, target_is_directory=True)
    applied = transaction.apply_plan(built, resources=dispatch.Resources(root=root,target=target),
        journal=tmp_path/'journal.json', lock_path=tmp_path/'lock', smoke=lambda:True)
    assert applied.status == 'committed'
    assert root.resolve() == target
    (target/f'scripts/{FAMILIES[client]}.py').write_text('# changed definition renderer')
    assert plan().touches_host_config
