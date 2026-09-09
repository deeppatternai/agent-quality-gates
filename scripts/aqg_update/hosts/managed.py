"""Read-only user-scope hook evidence for the remaining registered installers.

Render with the running, trusted installer; never import staged release code.
Configuration reconciliation remains an explicit installer action. In particular
this adapter does not approve hooks, edit user files or infer project locations.
"""
import importlib
import json
import os
from dataclasses import replace
from pathlib import Path

from .base import AdapterError, HostAdapter
from .. import migrate


FAMILIES = {
    'cursor': 'install_cursor_support',
    **dict.fromkeys(('codebuddy', 'workbuddy-ai', 'kimi-code', 'qoderwork'), 'install_aqg_work_clients'),
    **dict.fromkeys(('qoder', 'qoder-cn', 'qoder-cli', 'qoder-cli-cn'), 'install_aqg_qoder'),
    **dict.fromkeys(('trae', 'trae-cn', 'devin'), 'install_aqg_agent_clients'),
    'pi': 'install_aqg_pi',
}
COMMAND_MARKERS = ('cursor_aqg_hook.py', 'qoder_hook_adapter.py', 'agent_client_aqg_hook.py')


def _rows(hooks):
    """Retain event, matcher and all owned command attributes; ignore foreign hooks."""
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    rows = []
    for event, blocks in hooks.items():
        if not any(marker in json.dumps(blocks) for marker in COMMAND_MARKERS):
            continue
        if not isinstance(blocks, list):
            raise ValueError('hook event must be a list')
        for block in blocks:
            if not any(marker in json.dumps(block) for marker in COMMAND_MARKERS):
                continue
            if not isinstance(block, dict):
                raise ValueError('hook entry must be an object')
            commands = block.get('hooks', [block])
            if not isinstance(commands, list):
                raise ValueError('nested hooks must be a list')
            for command in commands:
                if not any(marker in json.dumps(command) for marker in COMMAND_MARKERS):
                    continue
                if not isinstance(command, dict) or not isinstance(command.get('command'), str):
                    raise ValueError('hook command must be a string')
                if any(marker in command['command'] for marker in COMMAND_MARKERS):
                    parent = {k: v for k, v in block.items() if k != 'hooks'} if 'hooks' in block else {}
                    rows.append(json.dumps([event, parent, command], sort_keys=True))
    return sorted(rows)


class ManagedAdapter(HostAdapter):
    serves_many_hosts = True
    hook_command_is_root_relative = False

    def __init__(self, client_id, *, home=None, aqg_root=None):
        if client_id not in FAMILIES:
            raise AdapterError(f'no managed inspector for {client_id}')
        self.client_id = client_id
        self.home = Path(home) if home is not None else Path.home()
        self.root = Path(aqg_root or os.environ.get('AQG_ROOT') or Path(__file__).absolute().parents[3]).expanduser().absolute()
        if not self.root.is_symlink():
            self.root = migrate.logical_root(self.root)
        prefix = 'scripts.' if __package__.startswith('scripts.') else ''
        self.installer = importlib.import_module(prefix + FAMILIES[client_id])

    def canonical_config(self):
        """Render without writes; Qoder reads its validated shared-owner sidecar."""
        module, client, root = self.installer, self.client_id, self.root
        if client == 'cursor':
            return self.home / '.cursor/hooks.json', json.dumps({'hooks': {k: [v] for k, v in module._hook_specs(root).items()}})
        if client == 'pi':
            return self.home / '.pi/agent/extensions' / module.EXTENSION_NAME, module._render_extension(root)
        if FAMILIES[client] == 'install_aqg_work_clients':
            profile = module.PROFILES[client]
            directory = self.home / profile.user_dir
            if profile.hooks_format == 'toml':
                return directory / 'config.toml', module._toml_hook_block(root)
            return directory / 'settings.json', json.dumps({'hooks': module._json_hook_blocks(profile, root)})
        if FAMILIES[client] == 'install_aqg_qoder':
            directory = self.home / module.PROFILES[client]['user_dir']
            owners, present = module._read_root_owners(directory, client, 'user')
            effective = owners if present and client in owners else [client]
            return directory / 'settings.json', json.dumps({'hooks': module._effective_hook_specs(root, effective)})
        profile = module.PROFILES[client]
        directory = module._user_config_root(self.home, profile)
        hooks = module._hook_specs(root, profile)
        return module._settings_path(directory, profile, 'user'), json.dumps({'hooks': hooks} if client == 'devin' else hooks)

    def verify(self, *, state=None, target_root=None):
        evidence = super().verify(state=state, target_root=target_root)
        if evidence.hooks_status == 'missing' and self.client_id in (state or {}).get('hosts', {}):
            return replace(evidence, hooks_status='stale', hooks_detail='recorded AQG host has no readable managed hooks; reapply its installer using the logical entrance')
        return evidence

    def _inspect(self, root=None):
        try:
            path, expected = self.canonical_config()
            if not path.exists():
                return 'missing', f'no user-scope AQG hooks at {path}; project scopes are not inventoried'
            try:
                actual = path.read_text(encoding='utf-8')
            except (OSError, UnicodeError) as exc:
                return 'missing', f'no readable AQG ownership evidence at {path}: {exc}'
            markers = COMMAND_MARKERS + ('AQG MANAGED HOOKS', getattr(self.installer, 'EXTENSION_MARKER', 'pi_aqg_hook.py'))
            if not any(marker in actual for marker in markers):
                return 'missing', f'no managed AQG ownership in {path}'
            if self.client_id == 'pi':
                # The whole extension is owned; a marker alone proves no integrity.
                complete = actual == expected
            elif path.suffix == '.toml':
                # Compare our exact installer-owned block on Python 3.10 too.
                # This proves owned text, not validity of unrelated TOML settings.
                start, end = '# BEGIN AQG MANAGED HOOKS', '# END AQG MANAGED HOOKS'
                if actual.count(start) != 1 or actual.count(end) != 1:
                    return 'stale', f'{path}: missing or duplicated AQG TOML block; reapply installer'
                before, rest = actual.split(start, 1)
                body, after = rest.split(end, 1)
                complete = (start + body + end).strip() == expected.strip() and not any(marker in before + after for marker in COMMAND_MARKERS)
            else:
                def parse(text):
                    value = json.loads(text)
                    return value if self.client_id in ('trae', 'trae-cn') else value.get('hooks', {})
                owned = _rows(parse(actual))
                if not owned:
                    return 'missing', f'no managed AQG commands in {path}'
                complete = owned == _rows(parse(expected))
            return ('complete', f'canonical user-scope hooks at {path}') if complete else (
                'stale', f'{path}: managed hooks differ; reapply {FAMILIES[self.client_id]}.py using the logical AQG entrance and review host trust')
        except Exception as exc:  # aqg: top-level boundary
            return 'invalid', f'{self.client_id}: cannot inspect managed hooks ({type(exc).__name__}: {exc})'
