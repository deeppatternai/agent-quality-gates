"""Prepare bounded AQG-owned hook edits; the transaction owns their writes.

Candidate renderers run only after the caller verified the release. They run
in a separate interpreter so target imports never contaminate the live updater.
Codex approval/digest configuration deliberately has no automatic edit adapter.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from scripts._aqg_backup import _atomic_write_bytes
from scripts import aqg_directory_links as directory_links
from .base import AdapterError

MAX_CONFIG_BYTES = 8 * 1024 * 1024

# Explicit sys.path under -I; no environment Python path and no host commands.
_RENDER = """
import json, sys
from pathlib import Path
source, root, home, client, verify_path = sys.argv[1:]
sys.path.insert(0, source)
if verify_path:
    if client == 'claude-code':
        from scripts.aqg_update.hosts.claude_code import ClaudeCodeAdapter
        adapter = ClaudeCodeAdapter(settings_path=Path(verify_path), aqg_root=Path(root))
        status = adapter.verify().hooks_status
    else:
        from scripts.aqg_update.hosts.managed import ManagedAdapter
        adapter = ManagedAdapter(client, home=Path(home), aqg_root=Path(root))
        status = adapter._inspect()[0]
    print(json.dumps({'content': status}))
    sys.exit(0)
if client == 'claude-code':
    from scripts import install_aqg_hooks as installer
    content = json.dumps({'hooks': installer._aqg_hook_specs(Path(source))})
else:
    from scripts.aqg_update.hosts.managed import ManagedAdapter
    adapter = ManagedAdapter(client, home=Path(home), aqg_root=Path(source))
    adapter.root = Path(source)
    _, content = adapter.canonical_config()
for old, new in ((source, root), (json.dumps(source)[1:-1], json.dumps(root)[1:-1])):
    content = content.replace(old, new)
print(json.dumps({'content': content}))
"""


def render(target, root, home, client, *, verify_path=''):
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith(('PYTHON', 'LD_', 'DYLD_', 'GIT_'))}
    env['AQG_NO_UPDATE_CHECK'] = '1'
    try:
        done = subprocess.run(
            [sys.executable, '-B', '-I', '-c', _RENDER,
             str(Path(target).resolve()), str(Path(root).absolute()), str(home), client, str(verify_path)],
            cwd=target, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise AdapterError(f'{client}: candidate hook renderer timed out') from exc
    if done.returncode or len(done.stdout) > MAX_CONFIG_BYTES:
        raise AdapterError(f'{client}: candidate hook renderer failed (exit {done.returncode})')
    try:
        content = json.loads(done.stdout)['content']
        if not isinstance(content, str):
            raise ValueError('content is not text')
        return content
    except (ValueError, KeyError, TypeError) as exc:
        raise AdapterError(f'{client}: invalid rendered hook configuration') from exc


def read_config(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_CONFIG_BYTES:
        raise AdapterError(f'{path}: refusing missing, linked or oversized hook configuration')
    return path.read_bytes()


@dataclass
class HookEdit:
    path: Path
    before: bytes
    after: bytes
    parent: Path
    mode: int
    written: bool = field(default=False, init=False)
    verifier: object = field(default=None, repr=False)

    def _current(self):
        if self.path.parent.resolve() != self.parent:
            raise AdapterError(f'{self.path}: configuration parent changed')
        return read_config(self.path)

    def validate(self):
        if self._current() != self.before:
            raise AdapterError(f'{self.path}: configuration changed since planning')

    def backup(self, directory):
        self.validate()
        directory = Path(directory) / 'hook-backups'
        directory.mkdir(mode=0o700, exist_ok=True)
        try:
            directory_kind = directory_links.link_kind(directory)
        except OSError as exc:
            raise AdapterError(f'cannot inspect hook backup directory: {exc}') from exc
        if directory_kind != 'directory':
            raise AdapterError(
                f'hook backup directory is {directory_kind}, not a real directory'
            )
        digest = hashlib.sha256(self.before).hexdigest()
        backup = directory / (digest + '.bak')
        if backup.exists() or backup.is_symlink():
            if read_config(backup) != self.before:
                raise AdapterError('hook backup contents changed')
        else:
            _atomic_write_bytes(backup, self.before)
        return {'path': str(self.path), 'backup': str(backup),
                'before_sha256': digest,
                'after_sha256': hashlib.sha256(self.after).hexdigest()}

    def apply(self):
        if self.written:
            if self._current() != self.after:
                raise AdapterError(f'{self.path}: configuration changed after writing')
            return False
        self.validate()
        if self.before == self.after:
            return False
        self.written = True
        try:
            _atomic_write_bytes(self.path, self.after, mode=self.mode)
        except OSError:
            if self._current() == self.before:
                self.written = False
            raise
        if self._current() != self.after:
            raise AdapterError(f'{self.path}: hook write verification failed')
        return True

    def restore(self):
        if not self.written:
            return
        current = self._current()
        if current == self.before:
            self.written = False
            return
        if current != self.after:
            raise AdapterError(f'{self.path}: concurrent configuration edit; refusing to overwrite it')
        _atomic_write_bytes(self.path, self.before, mode=self.mode)
        if self._current() != self.before:
            raise AdapterError(f'{self.path}: hook restore verification failed')
        self.written = False

    def verify(self):
        if self._current() != self.after:
            raise AdapterError(f'{self.path}: refreshed hooks changed before commit')
        if self.verifier is not None and not self.verifier():
            raise AdapterError(f'{self.path}: candidate installer did not verify refreshed hooks')
        return True


def _json_object(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise AdapterError('duplicate configuration key')
            result[key] = value
        return result
    value = json.loads(text, object_pairs_hook=pairs)
    if not isinstance(value, dict):
        raise AdapterError('hook configuration must be an object')
    return value


def _pi_commands(text):
    match = re.search(r'(?s)const AQG_COMMANDS: Record<string, string\[\]> = (\{.*?\});\r?\n\r?\n', text)
    if match is None:
        raise AdapterError('missing AQG Pi command map')
    try:
        commands = json.loads(match.group(1))
    except ValueError as exc:
        raise AdapterError('invalid AQG Pi command map') from exc
    if not isinstance(commands, dict) or not all(
            isinstance(name, str) and isinstance(argv, list)
            and all(isinstance(word, str) for word in argv)
            for name, argv in commands.items()):
        raise AdapterError('invalid AQG Pi command map')
    return commands


def _toml_commands(text):
    pattern = r'(?m)^command\s*=\s*("(?:[^"\\]|\\.)*")'
    try:
        commands = [json.loads(match.group(1)) for match in re.finditer(pattern, text)]
    except ValueError as exc:
        raise AdapterError('invalid AQG TOML command') from exc
    if not commands:
        raise AdapterError('missing AQG TOML commands')
    return commands


def merge_json(actual, expected, *, top_level, owned):
    """Remove owned commands individually, including from mixed matcher blocks."""
    result = _json_object(actual)
    new = _json_object(expected)
    old_hooks = result if top_level else result.get('hooks', {})
    new_hooks = new if top_level else new['hooks']
    if not isinstance(old_hooks, dict) or not isinstance(new_hooks, dict):
        raise AdapterError('hooks must be an object')
    kept = {}
    owned_count = 0
    for event, blocks in old_hooks.items():
        if top_level and event not in new_hooks:
            # Unrelated settings need not resemble hook events at all.
            if not any(marker in json.dumps(blocks) for marker in (
                    'cursor_aqg_hook.py', 'qoder_hook_adapter.py', 'agent_client_aqg_hook.py')):
                kept[event] = blocks
                continue
        if not isinstance(blocks, list):
            raise AdapterError('hook event must be an array')
        rest = []
        for block in blocks:
            if not isinstance(block, dict):
                raise AdapterError('hook block must be an object')
            if 'hooks' in block:
                if not isinstance(block['hooks'], list):
                    raise AdapterError('hook commands must be an array')
                commands = []
                for command in block['hooks']:
                    if owned(command):
                        owned_count += 1
                    else:
                        commands.append(command)
                if commands or not block['hooks']:
                    rest.append({**block, 'hooks': commands})
            elif owned(block):
                owned_count += 1
            else:
                rest.append(block)
        if rest or not blocks:
            kept[event] = rest
    if not owned_count:
        raise AdapterError('no owned hooks to refresh; automatic update does not install new hosts')
    for event, blocks in new_hooks.items():
        if not isinstance(blocks, list):
            raise AdapterError('rendered hook event must be an array')
        kept.setdefault(event, []).extend(copy.deepcopy(blocks))
    if top_level:
        result = kept
    else:
        result['hooks'] = kept
    return json.dumps(result, ensure_ascii=False, indent=2) + '\n'


def prepare(adapter, target):
    from .managed import COMMAND_MARKERS, preserve_interpreter
    client = adapter.client_id
    if client == 'claude-code':
        path, root, home = adapter._settings_path, adapter._aqg_root, Path.home()
        from scripts import install_aqg_hooks as installer
        canonical = json.dumps({'hooks': installer._aqg_hook_specs(root)})
        marked = lambda command: installer._command_references_aqg_script(command)
    else:
        path, canonical = adapter.canonical_config()
        root, home = adapter.root, adapter.home
        marked = lambda command: any(marker in command for marker in COMMAND_MARKERS)
    before = read_config(path)
    actual = before.decode('utf-8')
    refreshed = preserve_interpreter(render(target, root, home, client), actual)
    if client == 'pi':
        marker = adapter.installer.EXTENSION_MARKER
        if marker not in actual:
            raise AdapterError('unowned Pi extension')
        recognized = preserve_interpreter(canonical, actual)
        if _pi_commands(recognized) != _pi_commands(actual):
            raise AdapterError('unrecognized AQG Pi extension; refusing automatic replacement')
        after = refreshed
    elif path.suffix == '.toml':
        start, end = '# BEGIN AQG MANAGED HOOKS', '# END AQG MANAGED HOOKS'
        if actual.count(start) != 1 or actual.count(end) != 1:
            raise AdapterError('missing or duplicated managed TOML block')
        prefix, rest = actual.split(start, 1)
        body, suffix = rest.split(end, 1)
        if any(marker in prefix + suffix for marker in COMMAND_MARKERS):
            raise AdapterError('managed commands outside owned TOML block')
        installed = start + body + end
        recognized = preserve_interpreter(canonical, actual)
        if _toml_commands(installed) != _toml_commands(recognized):
            raise AdapterError('unrecognized AQG TOML block; refusing automatic replacement')
        after = prefix + refreshed.strip() + suffix
    else:
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
        known = set(commands(_json_object(preserve_interpreter(canonical, actual))))
        known.update(commands(_json_object(refreshed)))
        def owned(value):
            command = value.get('command') if isinstance(value, dict) else None
            if not isinstance(command, str) or not marked(command):
                return False
            if command not in known:
                raise AdapterError('unrecognized AQG command; refusing automatic replacement')
            return True
        after = merge_json(actual, refreshed, top_level=client in ('trae', 'trae-cn'), owned=owned)
    return HookEdit(Path(path), before, after.encode('utf-8'), path.parent.resolve(),
                    stat.S_IMODE(path.stat().st_mode),
                    verifier=lambda: render(target, root, home, client, verify_path=path) == 'complete')


def trusted_snapshot(adapter, before, current):
    """A journal hash is not authority to restore executable configuration.

    Only the running installer may prove the snapshot's executable fields.
    Everything outside those fields must agree with the file already on disk.
    Old definitions we cannot prove require manual recovery.
    """
    from .managed import COMMAND_MARKERS, _rows, preserve_interpreter
    actual, present = before.decode('utf8'), current.decode('utf8')
    client = adapter.client_id
    if client == 'claude-code':
        from scripts import install_aqg_hooks as installer
        expected = json.dumps({'hooks': installer._aqg_hook_specs(adapter._aqg_root)})
        markers = tuple(installer.AQG_HOOK_SCRIPTS)
    else:
        path, expected = adapter.canonical_config()
        markers = COMMAND_MARKERS
        if client == 'pi':
            return actual == preserve_interpreter(expected, actual)
        if path.suffix == '.toml':
            start, end = '# BEGIN AQG MANAGED HOOKS', '# END AQG MANAGED HOOKS'
            def split(text):
                if text.count(start) != 1 or text.count(end) != 1:
                    raise AdapterError('ambiguous managed TOML block')
                prefix, rest = text.split(start, 1)
                body, suffix = rest.split(end, 1)
                return prefix, (start + body + end).strip(), suffix
            old, now = split(actual), split(present)
            return (old[1] == preserve_interpreter(expected, actual).strip()
                    and (old[0], old[2]) == (now[0], now[2]))
    expected = preserve_interpreter(expected, actual)
    top = client in ('trae', 'trae-cn')
    def hooks(text):
        value = _json_object(text)
        return value if top else value.get('hooks', {})
    if _rows(hooks(actual), markers) != _rows(hooks(expected), markers):
        return False
    def owned(value):
        command = value.get('command', '') if isinstance(value, dict) else ''
        return isinstance(command, str) and any(marker in command for marker in markers)
    # Comparing after canonical replacement retains all unrelated settings and
    # foreign commands, including parent matcher attributes in mixed blocks.
    normalize = lambda text: _json_object(merge_json(text, expected, top_level=top, owned=owned))
    return normalize(actual) == normalize(present)
