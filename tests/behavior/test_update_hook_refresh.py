"""Owned configuration converges without sacrificing user data or retry."""
from pathlib import Path
import json
import pytest

from scripts.aqg_update.hosts import managed

REPO = Path(__file__).resolve().parents[2]


def cursor_fixture(tmp_path):
    adapter = managed.ManagedAdapter('cursor', home=tmp_path / 'home', aqg_root=REPO)
    path, expected = adapter.canonical_config()
    data = json.loads(expected)
    data['user_setting'] = {'keep': True}
    data['hooks']['sessionStart'][0]['timeout'] = 1
    data['hooks']['sessionStart'].append({'command': 'echo user', 'timeout': 7})
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding='utf8')
    return adapter, path


def test_refresh_preserves_user_settings_and_updates_owned_fields(tmp_path):
    adapter, path = cursor_fixture(tmp_path)
    before = path.read_bytes()
    edit = adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before
    edit.apply()
    data = json.loads(path.read_bytes())
    assert data['user_setting'] == {'keep': True}
    assert {'command': 'echo user', 'timeout': 7} in data['hooks']['sessionStart']
    assert adapter.verify().hooks_status == 'complete'
    edit.restore()
    assert path.read_bytes() == before


def test_refresh_refuses_concurrent_user_edits(tmp_path):
    adapter, path = cursor_fixture(tmp_path)
    edit = adapter.prepare_hook_edit(REPO)
    path.write_text('{"user_new_value": 42}', encoding='utf8')
    with pytest.raises(Exception, match='changed'):
        edit.apply()
    assert json.loads(path.read_bytes()) == {'user_new_value': 42}


@pytest.mark.parametrize('client', sorted(managed.FAMILIES))
def test_each_managed_host_refresh_preserves_foreign_configuration(tmp_path, client):
    adapter = managed.ManagedAdapter(client, home=tmp_path, aqg_root=REPO)
    path, expected = adapter.canonical_config()
    path.parent.mkdir(parents=True)
    if client == 'pi':
        actual = expected + '\n// old AQG extension\n'
    elif path.suffix == '.toml':
        actual = '# user comment\n' + expected.replace('timeout = 30', 'timeout = 1')
    else:
        data = json.loads(expected)
        hooks = data if client in ('trae', 'trae-cn') else data['hooks']
        event = next(iter(hooks))
        block = hooks[event][0]
        if 'hooks' in block:
            block['hooks'][0]['timeout'] = 1
            block['hooks'].append({'type': 'command', 'command': 'echo keep-user'})
        else:
            block['timeout'] = 1
            hooks[event].append({'command': 'echo keep-user'})
        actual = json.dumps(data)
    path.write_text(actual, encoding='utf8')
    edit = adapter.prepare_hook_edit(REPO)
    edit.apply()
    assert adapter.verify().hooks_status == 'complete', adapter.verify().hooks_detail
    if path.suffix == '.toml':
        assert path.read_text(encoding='utf8').startswith('# user comment\n')
    elif client != 'pi':
        assert 'echo keep-user' in path.read_text(encoding='utf8')
    edit.restore()
    assert path.read_text(encoding='utf8') == actual


def test_changed_candidate_renderer_supplies_new_hook_definition(tmp_path):
    import shutil
    adapter, path = cursor_fixture(tmp_path)
    target = tmp_path / 'candidate'
    shutil.copytree(REPO / 'scripts', target / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
    renderer = target / 'scripts/install_cursor_support.py'
    original = renderer.read_text(encoding='utf8')
    modified = original.replace('"timeout": 30', '"timeout": 31')
    assert original != modified
    renderer.write_text(modified, encoding='utf8')
    edit = adapter.prepare_hook_edit(target)
    edit.apply()
    data = json.loads(path.read_bytes())
    assert any(row.get('timeout') == 31 for row in data['hooks']['preCompact'])
    assert {'command': 'echo user', 'timeout': 7} in data['hooks']['sessionStart']
    assert edit.verify()


def test_failed_smoke_restores_hooks_and_allows_next_transaction(tmp_path):
    from scripts.aqg_update import dispatch, transaction
    from scripts.aqg_update.plan import Action, Plan
    adapter, path = cursor_fixture(tmp_path)
    before = path.read_bytes()
    live = tmp_path / 'versions/old'
    target = tmp_path / 'versions/new'
    live.mkdir(parents=True)
    target.mkdir()
    (live / 'VERSION').write_text('old')
    (target / 'VERSION').write_text('new')
    root = tmp_path / 'aqg'
    root.symlink_to(live, target_is_directory=True)
    edit = adapter.prepare_hook_edit(REPO)
    actions = (Action(kind='merge_hooks', client_id='cursor', payload_class=5, subject=None, detail='refresh'),
               Action(kind='activate_root', client_id=None, payload_class=0, subject=None, detail='activate'))
    plan = Plan(actions=actions, deferred=())
    resources = dispatch.Resources(root=root, target=target, hook_edits={'cursor': edit})
    journal = tmp_path / 'journal.json'
    lock = tmp_path / 'update.lock'
    result = transaction.apply_plan(plan, resources=resources, journal=journal,
                                    lock_path=lock, smoke=lambda: False)
    assert result.status == 'rolled-back'
    assert root.resolve() == live
    assert path.read_bytes() == before
    assert not journal.exists()
    result = transaction.apply_plan(plan, resources=resources, journal=journal,
                                    lock_path=lock, smoke=lambda: True)
    assert result.status == 'committed', result.detail
    assert root.resolve() == target
    assert adapter.verify().hooks_status == 'complete'


@pytest.mark.parametrize('foreign_edit', [False, True])
def test_interrupted_hook_transaction_recovers_only_unchanged_owned_files(tmp_path, monkeypatch, foreign_edit):
    from scripts.aqg_update import hosts, stage, transaction
    adapter, path = cursor_fixture(tmp_path)
    # Crash recovery accepts only snapshots provable by the trusted renderer.
    data = json.loads(path.read_bytes())
    data['hooks']['sessionStart'][0]['timeout'] = 45
    path.write_text(json.dumps(data), encoding='utf8')
    edit = adapter.prepare_hook_edit(REPO)
    previous = tmp_path / 'versions/old'
    target = tmp_path / 'versions/new'
    for directory in (previous, target):
        directory.mkdir(parents=True)
        (directory / 'VERSION').write_text(directory.name)
    root = tmp_path / 'aqg'
    root.symlink_to(previous, target_is_directory=True)
    journal = tmp_path / 'journal.json'
    snapshot = dict(edit.backup(tmp_path), clients=['cursor'])
    transaction._write_journal(journal, dict(phase='applying', previous_root=str(previous),
        target=str(target), recoverable_hooks_only=True, hook_backups=[snapshot]))
    edit.apply()
    stage.swap_root(root=root, target=target)
    if foreign_edit:
        path.write_text('{"user_edit_during_update":true}', encoding='utf8')
    observed = path.read_bytes()
    monkeypatch.setattr(hosts, 'adapter_for', lambda client: adapter)
    monkeypatch.setattr(stage, 'version_commit', lambda directory: directory.name)
    recovered = transaction.recover_hook_update(journal, root, {'installed_commit': 'old'})
    assert recovered is (not foreign_edit)
    if foreign_edit:
        assert path.read_bytes() == observed
        assert root.resolve() == target
        assert journal.exists()
    else:
        assert path.read_bytes() == edit.before
        assert root.resolve() == previous
        assert not journal.exists()


def test_backup_failure_cannot_change_configuration_or_root(tmp_path, monkeypatch):
    from scripts.aqg_update import dispatch, transaction
    from scripts.aqg_update.plan import Action, Plan
    adapter, path = cursor_fixture(tmp_path)
    edit = adapter.prepare_hook_edit(REPO)
    monkeypatch.setattr(edit, 'backup', lambda directory: (_ for _ in ()).throw(OSError('disk full')))
    root = tmp_path / 'root'
    target = tmp_path / 'target'
    target.mkdir()
    result = transaction.apply_plan(
        Plan(actions=(Action('merge_hooks', 'cursor', None, 5, 'refresh'),)),
        resources=dispatch.Resources(root=root, target=target, hook_edits={'cursor': edit}),
        journal=tmp_path/'journal.json', lock_path=tmp_path/'update.lock')
    assert result.status == 'stale-plan'
    assert path.read_bytes() == edit.before
    assert not root.exists()
    assert not (tmp_path/'journal.json').exists()


def test_claude_refresh_preserves_foreign_command_in_same_matcher(tmp_path):
    from scripts import install_aqg_hooks
    from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter
    path = tmp_path / 'settings.json'
    hooks = install_aqg_hooks._aqg_hook_specs(REPO)
    event = next(iter(hooks))
    hooks[event][0]['hooks'].append({'type': 'command', 'command': 'echo keep-user'})
    data = {'hooks': hooks, 'env': {'AQG_ROOT': str(REPO), 'USER_SETTING': 'keep'}}
    path.write_text(json.dumps(data), encoding='utf8')
    before = path.read_bytes()
    adapter = ClaudeCodeAdapter(settings_path=path, aqg_root=REPO, skills_dest=tmp_path/'skills')
    edit = adapter.prepare_hook_edit(REPO)
    edit.apply()
    result = json.loads(path.read_bytes())
    assert result['env'] == data['env']
    assert any(h.get('command') == 'echo keep-user'
               for b in result['hooks'][event] for h in b.get('hooks', []))
    assert adapter.verify().hooks_status == 'complete'
    edit.restore()
    assert path.read_bytes() == before


def test_refresh_refuses_duplicate_configuration_keys(tmp_path):
    adapter, path = cursor_fixture(tmp_path)
    data = path.read_text(encoding='utf8')
    path.write_text('{"user_setting":false,' + data[1:], encoding='utf8')
    before = path.read_bytes()
    with pytest.raises(Exception, match='duplicate'):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


def test_external_write_equal_to_after_is_not_ours_to_undo(tmp_path):
    adapter, path = cursor_fixture(tmp_path)
    edit = adapter.prepare_hook_edit(REPO)
    path.write_bytes(edit.after)
    with pytest.raises(Exception, match='changed'):
        edit.apply()
    edit.restore()
    assert path.read_bytes() == edit.after


def test_trae_refresh_preserves_non_hook_top_level_settings(tmp_path):
    adapter = managed.ManagedAdapter('trae', home=tmp_path, aqg_root=REPO)
    path, expected = adapter.canonical_config()
    data = json.loads(expected)
    data.update(theme='dark', userOptions=[42, 'keep'])
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data), encoding='utf8')
    edit = adapter.prepare_hook_edit(REPO)
    edit.apply()
    assert json.loads(path.read_bytes())['theme'] == 'dark'
    assert json.loads(path.read_bytes())['userOptions'] == [42, 'keep']


def test_ambiguous_aqg_command_is_not_deleted_or_duplicated(tmp_path):
    adapter, path = cursor_fixture(tmp_path)
    data = json.loads(path.read_bytes())
    data['hooks']['sessionStart'][0]['command'] += ' && echo user-owned-tail'
    path.write_text(json.dumps(data), encoding='utf8')
    before = path.read_bytes()
    with pytest.raises(Exception, match='unrecognized'):
        adapter.prepare_hook_edit(REPO)
    assert path.read_bytes() == before


def test_renderer_timeout_is_a_handled_adapter_error(tmp_path, monkeypatch):
    from scripts.aqg_update.hosts import reconcile
    import subprocess
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('renderer', 30)
    monkeypatch.setattr(reconcile.subprocess, 'run', timeout)
    with pytest.raises(managed.AdapterError, match='timed out'):
        reconcile.render(REPO, REPO, tmp_path, 'cursor')


@pytest.mark.parametrize('client', ['cursor', 'qoder-cli', 'kimi-code'])
def test_foreign_alias_mentions_do_not_change_managed_command(tmp_path, monkeypatch, client):
    import sys
    executable = tmp_path / 'python.exe'
    alias = tmp_path / 'python3.exe'
    executable.write_bytes(b'identical test interpreter')
    executable.chmod(0o700)
    alias.write_bytes(executable.read_bytes())
    alias.chmod(0o700)
    monkeypatch.setattr(sys, 'executable', str(executable))
    adapter = managed.ManagedAdapter(client, home=tmp_path, aqg_root=REPO)
    path, expected = adapter.canonical_config()
    path.parent.mkdir(parents=True)
    if path.suffix == '.toml':
        actual = '# foreign mention ' + str(alias) + '\n' + expected
    else:
        data = json.loads(expected)
        data['user_command'] = str(alias)
        actual = json.dumps(data)
    path.write_text(actual, encoding='utf8')
    original_read = Path.read_bytes
    def no_binary_read(self):
        assert self not in (executable, alias), 'matching hooks must not read executables'
        return original_read(self)
    monkeypatch.setattr(Path, 'read_bytes', no_binary_read)
    assert adapter.verify().hooks_status == 'complete'


@pytest.mark.parametrize('scenario', ['old', 'new', 'unrelated', 'missing-state',
                                    'repair', 'forged-user-setting', 'forged-command', 'swap-failure'])
def test_recovery_requires_trusted_snapshot_and_known_install_state(tmp_path, monkeypatch, scenario):
    import hashlib
    from scripts.aqg_update import hosts, stage, transaction
    from scripts.aqg_update.hosts.reconcile import HookEdit
    adapter = managed.ManagedAdapter('cursor', home=tmp_path/'home', aqg_root=REPO)
    path, canonical = adapter.canonical_config()
    before = json.loads(canonical)
    before['user_config'] = 'keep'
    after = json.loads(json.dumps(before))
    after['hooks']['sessionStart'][0]['timeout'] = 45 if scenario == 'new' else 46
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(after), encoding='utf8')
    observed = path.read_bytes()
    if scenario == 'forged-user-setting':
        before['user_config'] = 'attacker-value'
    if scenario == 'forged-command':
        before['hooks']['sessionStart'][0]['command'] += ' && echo attacker'
    snapshot_bytes = json.dumps(before).encode()
    backup_dir = tmp_path/'hook-backups'
    backup_dir.mkdir()
    digest = hashlib.sha256(snapshot_bytes).hexdigest()
    backup = backup_dir/(digest+'.bak')
    backup.write_bytes(snapshot_bytes)
    previous, target = tmp_path/'versions/old', tmp_path/'versions/new'
    for directory in (previous, target):
        directory.mkdir(parents=True)
        (directory/'VERSION').write_text(directory.name)
    root = tmp_path/'entrance'
    root.symlink_to(target, target_is_directory=True)
    journal = tmp_path/'journal.json'
    record = dict(path=str(path), backup=str(backup), before_sha256=digest,
                  after_sha256=hashlib.sha256(observed).hexdigest(), clients=['cursor'])
    transaction._write_journal(journal, dict(phase='repair_required' if scenario == 'repair' else 'smoking',
        previous_root=str(previous), target=str(target), recoverable_hooks_only=True, hook_backups=[record]))
    monkeypatch.setattr(hosts, 'adapter_for', lambda client: adapter)
    monkeypatch.setattr(stage, 'version_commit', lambda path: path.name)
    if scenario == 'swap-failure':
        monkeypatch.setattr(stage, 'swap_root', lambda **kwargs: (_ for _ in ()).throw(stage.StageError('locked')))
    installed = {'installed_commit': scenario if scenario in ('new', 'unrelated') else 'old'}
    if scenario == 'missing-state':
        installed = None
    result = transaction.recover_hook_update(journal, root, installed)
    assert result == (scenario in ('old', 'new'))
    assert root.resolve() == (previous if scenario == 'old' else target)
    assert path.read_bytes() == (snapshot_bytes if scenario == 'old' else observed)
    assert journal.exists() == (scenario not in ('old', 'new'))
    if result:
        assert not backup.exists()
    if scenario == 'swap-failure':
        assert transaction.read_journal(journal)['phase'] == 'repair_required'


def test_transaction_leaves_unwritten_concurrent_config_alone(tmp_path, monkeypatch):
    from scripts.aqg_update import dispatch, transaction
    from scripts.aqg_update.plan import Action, Plan
    adapter, path = cursor_fixture(tmp_path)
    edit = adapter.prepare_hook_edit(REPO)
    original_execute = dispatch.execute
    external = b'{"user_concurrent_change": true}'
    def changed_after_backup(*args, **kwargs):
        path.write_bytes(external)
        return original_execute(*args, **kwargs)
    monkeypatch.setattr(dispatch, 'execute', changed_after_backup)
    journal = tmp_path/'journal.json'
    result = transaction.apply_plan(Plan(actions=(Action('merge_hooks', 'cursor', None, 5, 'refresh'),)),
        resources=dispatch.Resources(root=tmp_path/'root', target=tmp_path/'target', hook_edits={'cursor': edit}),
        journal=journal, lock_path=tmp_path/'lock')
    assert result.status == 'rolled-back', result.detail
    assert path.read_bytes() == external
    assert not journal.exists()
    assert not list((tmp_path/'hook-backups').glob('*.bak'))


def test_candidate_verification_failure_rolls_back_hooks(tmp_path, monkeypatch):
    from scripts.aqg_update import dispatch, transaction
    from scripts.aqg_update.plan import Action, Plan
    adapter, path = cursor_fixture(tmp_path)
    edit = adapter.prepare_hook_edit(REPO)
    monkeypatch.setattr(edit, 'verifier', lambda: False)
    result = transaction.apply_plan(Plan(actions=(Action('merge_hooks', 'cursor', None, 5, 'refresh'),)),
        resources=dispatch.Resources(root=tmp_path/'root', target=REPO, hook_edits={'cursor': edit}),
        journal=tmp_path/'journal.json', lock_path=tmp_path/'lock', smoke=lambda: True)
    assert result.status == 'rolled-back'
    assert path.read_bytes() == edit.before
