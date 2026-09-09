"""Read-only user-scope hook evidence for the remaining registered installers.

Render with the running, trusted installer; never import staged release code.
Inspection is read-only. A separate prepare method builds an owned edit from a
verified candidate for the transaction; it does not approve hooks or infer projects.
"""
import importlib
import json
import os
import sys
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


def preserve_interpreter(expected, actual):
    """Prove aliases only for a mismatching whole command, never foreign text."""
    import re

    def commands(value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'command' and isinstance(item, str):
                    yield item
                else:
                    yield from commands(item)
        elif isinstance(value, list):
            for item in value:
                yield from commands(item)

    try:
        document = json.loads(expected)
        actual_commands = set(commands(json.loads(actual)))
    except ValueError:
        # Kimi's owned TOML block: ignore user commands outside its delimiters.
        start, end = '# BEGIN AQG MANAGED HOOKS', '# END AQG MANAGED HOOKS'
        if actual.count(start) != 1 or actual.count(end) != 1:
            return expected
        block = actual.split(start, 1)[1].split(end, 1)[0]
        pattern = r'(?m)^(command\s*=\s*)("(?:[^"\\]|\\.)*")'
        actual_commands = {json.loads(m.group(2)) for m in re.finditer(pattern, block)}
        document = None

    spellings = None

    def adapt(command):
        nonlocal spellings
        if command in actual_commands or not any(marker in command for marker in COMMAND_MARKERS):
            return command
        if spellings is None:
            spellings = []
            raw = Path(sys.executable).absolute()
            current = raw.resolve()
            names = ('python.exe', 'python3.exe') if raw.suffix.lower() == '.exe' else ('python', 'python3')
            candidates = {raw, current}
            candidates.update(parent / name for parent in (raw.parent, current.parent) for name in names)
            try:
                size = current.stat().st_size
                proof = current.read_bytes() if 0 < size <= 32 * 1024 * 1024 else b''
                identical = []
                for path in candidates:
                    try:
                        if proof and path.stat().st_size == size and path.read_bytes() == proof:
                            identical.append(path)
                    except OSError:
                        continue
                def forms(path):
                    values = [str(path), path.as_posix()]
                    if path.drive:
                        values += [prefix + path.drive[0].lower() + path.as_posix()[2:]
                                   for prefix in ('/mnt/', '/')]
                    return values
                for source in identical:
                    for dest in identical:
                        if source != dest:
                            pairs = list(zip(forms(source), forms(dest)))
                            pairs += [(json.dumps(a)[1:-1], json.dumps(b)[1:-1]) for a, b in pairs]
                            spellings.append(sorted(set(pairs), key=lambda pair: -len(pair[0])))
            except OSError:
                pass
        for pairs in spellings:
            variant = command
            for source, dest in pairs:
                variant = variant.replace(source, dest)
            if variant in actual_commands:
                return variant
        return command

    if document is None:
        return re.sub(pattern, lambda m: m.group(1) + json.dumps(adapt(json.loads(m.group(2)))), expected)

    def visit(value):
        if isinstance(value, dict):
            return {k: adapt(v) if k == 'command' and isinstance(v, str) else visit(v)
                    for k, v in value.items()}
        if isinstance(value, list):
            return [visit(v) for v in value]
        return value
    return json.dumps(visit(document), ensure_ascii=False)


def _rows(hooks, markers=COMMAND_MARKERS):
    """Retain event, matcher and all owned command attributes; ignore foreign hooks."""
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    rows = []
    for event, blocks in hooks.items():
        if not any(marker in json.dumps(blocks) for marker in markers):
            continue
        if not isinstance(blocks, list):
            raise ValueError('hook event must be a list')
        for block in blocks:
            if not any(marker in json.dumps(block) for marker in markers):
                continue
            if not isinstance(block, dict):
                raise ValueError('hook entry must be an object')
            commands = block.get('hooks', [block])
            if not isinstance(commands, list):
                raise ValueError('nested hooks must be a list')
            for command in commands:
                if not any(marker in json.dumps(command) for marker in markers):
                    continue
                if not isinstance(command, dict) or not isinstance(command.get('command'), str):
                    raise ValueError('hook command must be a string')
                if any(marker in command['command'] for marker in markers):
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

    def prepare_hook_edit(self, target_root):
        from .reconcile import prepare
        if self._inspect()[0] in ('missing', 'not-applicable', 'invalid'):
            raise AdapterError(f'{self.client_id}: no readable installed hook configuration')
        return prepare(self, target_root)

    def hook_configuration_path(self):
        return self.canonical_config()[0]

    def _inspect(self, root=None):
        try:
            if FAMILIES[self.client_id] == 'install_aqg_qoder':
                directory = self.home / self.installer.PROFILES[self.client_id]['user_dir']
                owners, present = self.installer._read_root_owners(directory, self.client_id, 'user')
                if present and self.client_id not in owners:
                    return 'not-applicable', 'shared hooks belong to: ' + ', '.join(owners)
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
            expected = preserve_interpreter(expected, actual)
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
