"""Rules must keep following the installed release and report historical pins."""
import json
import os
import stat
from types import SimpleNamespace
from pathlib import Path

import pytest

from scripts import aqg_doctor, install_aqg_rules
from scripts.aqg_update import run
from scripts.aqg_update import rules


REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / '.codex'))
    monkeypatch.setenv('APPDATA', str(tmp_path / 'AppData/Roaming'))
    monkeypatch.setenv('AQG_BACKUP_DIR', str(tmp_path / 'backups'))
    return tmp_path


def rules_text(root):
    return install_aqg_rules.render_region(install_aqg_rules.CLIENTS['claude-code'], REPO).replace(str(REPO), str(root))


def test_doctor_rejects_existing_historical_policy_pin(home):
    old = home / 'versions/0.14.4'
    policy = old / 'docs/policies/audit-trigger.md'
    policy.parent.mkdir(parents=True)
    policy.write_text('old policy')
    result = aqg_doctor.check_rules_block_text('claude', rules_text(old))
    assert result.status == 'WARN'
    assert 'version' in result.detail.lower()


@pytest.mark.parametrize('old', ['/Users/Example User/versions/0.14.4', r'C:\Users\Example User\versions\abc123'])
def test_doctor_finds_pins_with_spaces_and_both_separators(old):
    assert aqg_doctor.check_rules_block_text('codex', rules_text(old)).status == 'WARN'


def test_foreign_policy_example_outside_managed_block_is_not_a_pin():
    text = rules_text(REPO) + '\nExample: /old/versions/abc/docs/policies/audit-trigger.md\n'
    assert aqg_doctor.check_rules_block_text('claude', text).status == 'PASS'


def test_current_release_check_reports_rules_and_clears_only_repaired_rules(home, monkeypatch):
    rule = home / '.claude/CLAUDE.md'
    rule.parent.mkdir()
    rule.write_text(rules_text(home / 'versions/0.14.4'), encoding='utf-8')
    original = rule.read_bytes()
    monkeypatch.setattr(run.acquire, 'available_release', lambda *a, **k: None)
    state_root = home / 'state'
    state_root.mkdir()
    args = dict(root=REPO, remote='origin', channel='stable', keyring={}, state_root=state_root, apply=True)
    result = run._check_locked(**args)
    assert result.outcome == 'current'
    assert any('rules:' in item and 'claude-code' in item for item in result.pending)
    assert rule.read_bytes() == original
    run.record(result, state_root=state_root, at=1)
    saved = run.read_last_check(state_root=state_root)
    saved['pending'].append('codex: approve changed hooks')
    (state_root / run.LAST_CHECK_FILENAME).write_text(json.dumps(saved))
    rule.write_text(rules_text(REPO), encoding='utf-8')
    repaired = run._check_locked(**args)
    run.record(repaired, state_root=state_root, at=2)
    assert run.read_last_check(state_root=state_root)['pending'] == ['codex: approve changed hooks']


def test_installer_from_physical_current_tree_writes_managed_entrance(home):
    tree = home / '.deeppattern/versions/0.14.7'
    (tree / 'examples').mkdir(parents=True)
    for name in ('aqg-claude-rules.example.md', 'aqg-codex-agents.example.md'):
        (tree / 'examples' / name).write_bytes((REPO / 'examples' / name).read_bytes())
    (tree / 'VERSION').write_text('0.14.7')
    entrance = home / '.deeppattern/agent-quality-gates'
    entrance.symlink_to(tree, target_is_directory=True)
    for client in ('claude-code', 'codex'):
        profile = install_aqg_rules.CLIENTS[client]
        path = install_aqg_rules.rules_path(profile, home)
        path.parent.mkdir(exist_ok=True)
        path.write_text('User instructions.\n\n' + rules_text(home / 'versions/old'), encoding='utf-8')
        assert install_aqg_rules.main(['--client', client, '--home', str(home), '--aqg-root', str(tree), '--apply']) == 0
        actual = path.read_text(encoding='utf-8')
        assert actual.startswith('User instructions.\n\n')
        assert str(entrance) + '/docs/policies/audit-trigger.md' in actual
        assert str(tree) + '/docs/policies/audit-trigger.md' not in actual
        assert install_aqg_rules.main(['--client', client, '--home', str(home), '--aqg-root', str(entrance), '--verify']) == 0


@pytest.mark.parametrize('outcome', ['applied', 'deferred', 'pending'])
def test_release_result_retains_rules_notice_without_blocking_update(home, monkeypatch, outcome):
    path = home / '.codex/AGENTS.md'
    path.parent.mkdir()
    path.write_text(rules_text(home / 'versions/old'))
    monkeypatch.setattr(run, '_check_release', lambda **kw: run.CheckResult(outcome=outcome, pending=('hook approval',)))
    result = run._check_locked(root=REPO, remote='origin', channel='stable', keyring={}, state_root=home, apply=True)
    assert result.outcome == outcome
    assert result.pending[0] == 'hook approval'
    assert any('rules: codex:' in item for item in result.pending)
    assert result.rules_checked


def test_all_known_user_rule_locations_report_drift_without_writes(home):
    from scripts import install_aqg_work_clients as work, install_aqg_qoder as qoder, install_aqg_agent_clients as agent
    locations = list(rules.rule_locations(home))
    native = Path(os.environ['APPDATA'])
    expected = {
        'claude-code': home / '.claude/CLAUDE.md', 'codex': home / '.codex/AGENTS.md',
        'workbuddy': home / '.workbuddy/rules/aqg.md', 'codebuddy': home / '.codebuddy/rules/aqg.md',
        'workbuddy-ai': home / '.workbuddy-ai/rules/aqg.md', 'kimi-code': home / '.kimi-code/rules/aqg.md',
        'qoderwork': home / '.qoderwork/rules/aqg.md', 'qoderwake': home / '.qoderwake/rules/aqg.md',
        'qoder-cli': home / '.qoder/rules/aqg.md', 'qoder-cli-cn': home / '.qoder-cn/rules/aqg.md',
        'zed': native / 'Zed/AGENTS.md', 'devin': native / 'devin/AGENTS.md',
    }
    assert dict(locations) == expected
    for client, path in expected.items():
        if client in install_aqg_rules.CLIENTS:
            text = install_aqg_rules.render_region(install_aqg_rules.CLIENTS[client], REPO)
        elif client in work.PROFILES:
            text = work._render_rule(work.PROFILES[client], REPO)
        elif client in qoder.PROFILES:
            text = qoder._rule_text(REPO)
        else:
            text = agent._rule_text(REPO, client)
        assert rules.policy_problem(text, REPO) is None
        path.parent.mkdir(parents=True, exist_ok=True)
        pinned = text.replace(str(REPO), str(home / 'versions/old'))
        assert rules.policy_problem(pinned, REPO)
        path.write_text(pinned, encoding='utf-8')
    before = {path: path.read_bytes() for _, path in locations}
    notices = rules.pending_rules(REPO, home=home)
    assert len(notices) == len(locations)
    for client, path in locations:
        assert any(item.startswith(f'rules: {client}:') for item in notices)
        assert path.read_bytes() == before[path]


def test_rules_inspection_respects_codex_home_and_symlinked_file(home, monkeypatch):
    custom = home / 'custom-codex'
    custom.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(custom))
    owned = home / 'owned.md'
    owned.write_text(rules_text(home / 'versions/old'))
    link = custom / 'AGENTS.md'
    link.symlink_to(owned)
    assert any('rules: codex:' in item for item in rules.pending_rules(REPO))
    assert link.is_symlink()
    owned.write_text(rules_text(REPO))
    assert not rules.pending_rules(REPO)


def test_absent_and_foreign_rules_do_not_enroll_hosts(home):
    assert not rules.pending_rules(REPO, home=home)
    path = home / '.claude/CLAUDE.md'
    path.parent.mkdir()
    path.write_text('User example /old/versions/abc/docs/policies/audit-trigger.md')
    assert not rules.pending_rules(REPO, home=home)


@pytest.mark.parametrize('content', [b'\xff\xfe', b''])
def test_unreadable_rules_are_reported_but_empty_foreign_files_are_ignored(home, content):
    path = home / '.claude/CLAUDE.md'
    path.parent.mkdir()
    path.write_bytes(content)
    assert bool(rules.pending_rules(REPO, home=home)) == bool(content)


def test_logical_paths_spaces_and_unexpanded_paths(home):
    stable = home / 'a path with spaces/entrance'
    stable.parent.mkdir()
    stable.symlink_to(REPO, target_is_directory=True)
    assert rules.policy_problem(rules_text(stable), stable) is None
    assert rules.policy_problem(rules_text('<AQG_ROOT>'), stable)
    assert rules.policy_problem(rules_text(home / 'another-checkout'), stable)


@pytest.mark.parametrize('family', ['work', 'qoder', 'agent'])
def test_other_rule_renderers_recover_the_stable_entrance(home, monkeypatch, family):
    from scripts import install_aqg_work_clients as work, install_aqg_qoder as qoder, install_aqg_agent_clients as agent
    entrance = home / '.deeppattern/agent-quality-gates'
    entrance.parent.mkdir()
    entrance.symlink_to(REPO, target_is_directory=True)
    if family == 'work':
        monkeypatch.setattr(work, 'REPO_ROOT', REPO)
        text = work._render_rule(work.PROFILES['codebuddy'])
    elif family == 'qoder':
        text = qoder._rule_text(REPO)
    else:
        text = agent._rule_text(REPO, 'zed')
    assert str(entrance) + '/docs/policies/audit-trigger.md' in text
    assert rules.policy_problem(text, entrance) is None
    assert rules.policy_problem(text.replace(str(entrance), str(home/'versions/old')), entrance)


def test_diagnostic_error_cannot_relabel_an_applied_release(home, monkeypatch):
    monkeypatch.setattr(run, '_check_release', lambda **kw: run.CheckResult(outcome='applied'))
    def broken(*a):
        raise OSError('unreadable home')
    monkeypatch.setattr(run.rules, 'pending_rules', broken)
    result = run._check_locked(root=REPO, remote='origin', channel='stable', keyring={}, state_root=home, apply=True)
    assert result.outcome == 'applied'
    assert result.pending and not result.rules_checked


def test_large_and_duplicated_rules_are_not_certified(home):
    assert rules.policy_problem(rules_text(REPO) + rules_text(home/'versions/old'), REPO)
    path = home / '.claude/CLAUDE.md'
    path.parent.mkdir()
    path.write_text('x' * 262145)
    assert not rules.pending_rules(REPO, home=home)
    path.write_text('x' * 262145 + '\n' + rules_text(home/'versions/old'))
    assert rules.pending_rules(REPO, home=home)
    path.write_text('x' * 1048577)
    assert 'cannot fully inspect' in rules.pending_rules(REPO, home=home)[0]


@pytest.mark.parametrize('outcome', ['current', 'deferred', 'pending'])
def test_inspection_failure_keeps_previous_approvals_and_rules(home, monkeypatch, outcome):
    old = ('codex: approve hooks', 'rules: claude-code: previous drift')
    run.record(run.CheckResult(outcome='pending', pending=old), state_root=home)
    monkeypatch.setattr(run, '_check_release', lambda **kw: run.CheckResult(outcome=outcome))
    def broken(*a):
        raise ImportError('fixture unavailable inspector')
    monkeypatch.setattr(run.rules, 'pending_rules', broken)
    result = run._check_locked(root=REPO, remote='origin', channel='stable', keyring={}, state_root=home, apply=True)
    run.record(result, state_root=home)
    assert set(old) <= set(run.read_last_check(state_root=home)['pending'])


@pytest.mark.parametrize('outcome,new_pending', [('applied', ()), ('pending', ('codex: new authoritative approval',))])
def test_authoritative_release_result_can_retire_resolved_approvals(home, outcome, new_pending):
    run.record(run.CheckResult(outcome='pending', pending=('codex: old approval',)), state_root=home)
    result = run.CheckResult(outcome=outcome, pending=new_pending+('rules: claude-code: drift',), rules_checked=True)
    run.record(result, state_root=home)
    assert run.read_last_check(state_root=home)['pending'] == list(result.pending)


def test_discovery_error_retains_earlier_notices_and_cannot_crash_doctor(home, monkeypatch):
    path = home/'rule.md'
    path.write_text(rules_text(home/'versions/old'))
    def locations(*a):
        yield 'fixture', path
        raise ImportError('unavailable later family')
    monkeypatch.setattr(rules, 'rule_locations', locations)
    notices = rules.pending_rules(REPO)
    assert len(notices) == 2 and 'fixture' in notices[0] and 'unavailable' in notices[1]
    # Doctor imports the same bounded helper and receives the notices, not an exception.
    assert aqg_doctor.pending_rules(REPO) == notices


def test_one_unresolvable_native_host_does_not_hide_the_next(home, monkeypatch):
    from scripts import install_aqg_agent_clients as agents
    original = agents._user_config_root
    def locate(h, profile):
        if profile.client_id == 'zed':
            raise OSError('fixture inaccessible configuration')
        return original(h, profile)
    monkeypatch.setattr(agents, '_user_config_root', locate)
    actual = dict(rules.rule_locations(home))
    assert actual['zed'] is None and actual['devin'] is not None
    assert any('zed: cannot locate' in x for x in rules.pending_rules(REPO, home=home))


def test_doctor_checks_checkout_identity_when_root_is_known():
    text = rules_text(REPO.parent/'other-checkout')
    result = aqg_doctor.check_rules_block_text('claude', text, REPO)
    assert result.status == 'WARN' and 'different checkout' in result.detail
    result = aqg_doctor.check_rules_block_text('claude', text)
    assert result.status == 'PASS' and 'not verified' in result.detail
    relative = rules.HEADING+'\n aqg-code-construction; docs/policies/audit-trigger.md'
    assert aqg_doctor.check_rules_block_text('claude', relative).status == 'PASS'
    assert aqg_doctor.check_rules_block_text('claude', relative, REPO).status == 'WARN'


def test_work_rule_writer_uses_the_supplied_custom_entrance(home):
    from scripts import install_aqg_work_clients as work
    entrance = home/'custom entrance'
    entrance.symlink_to(REPO, target_is_directory=True)
    client = home/'.codebuddy'
    client.mkdir()
    assert work.main(['--client', 'codebuddy', '--scope', 'user', '--home', str(home),
                      '--aqg-root', str(entrance), '--mode', 'link', '--apply']) == 0
    text = (client/'rules/aqg.md').read_text(encoding='utf-8')
    assert str(entrance) + '/docs/policies/audit-trigger.md' in text
    assert rules.policy_problem(text, entrance) is None


def test_special_files_are_rejected_before_open(home, monkeypatch):
    path = home/'.claude/CLAUDE.md'
    path.mkdir(parents=True)
    def forbidden(*a, **k):
        pytest.fail('must not open a non-regular rules path')
    monkeypatch.setattr(rules.os, 'open', forbidden)
    assert rules.pending_rules(REPO, home=home)


def test_opened_descriptor_is_rechecked(home, monkeypatch):
    path = home/'rule.md'
    path.write_text('fixture')
    monkeypatch.setattr(rules.os, 'fstat', lambda fd: SimpleNamespace(st_mode=stat.S_IFIFO))
    with pytest.raises(ValueError, match='not a regular'):
        rules.read_rule_text(path)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX mkfifo is unavailable on native Windows; regular-file and fstat guards run here')
def test_fifo_rule_is_reported_without_waiting_for_a_writer(home):
    path = home/'.claude/CLAUDE.md'
    path.parent.mkdir()
    os.mkfifo(path)
    assert rules.pending_rules(REPO, home=home)


@pytest.mark.skipif(os.name != 'nt', reason='NTFS junction test requires native Windows')
def test_windows_junction_entrance_round_trip(home):
    from scripts.aqg_skill_install import create_windows_junction
    entrance = home/'.deeppattern/agent-quality-gates'
    entrance.parent.mkdir()
    assert create_windows_junction(REPO, entrance)
    try:
        assert rules.policy_root(entrance) == entrance
        assert rules.policy_root(REPO) == entrance
        text = install_aqg_rules.render_region(install_aqg_rules.CLIENTS['codex'], entrance)
        assert rules.policy_problem(text, entrance) is None
    finally:
        os.rmdir(entrance)  # Remove this junction only, never its target.


def test_an_obsolete_tree_without_a_matching_entrance_is_not_silently_adopted(home):
    old = home/'versions/old'
    old.mkdir(parents=True)
    assert rules.policy_root(old) == old
    assert rules.policy_problem(rules_text(old), old)
