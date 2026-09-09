"""Read-only policy-path diagnostics, shared by installers, Doctor and updates.

An installed rule is host configuration. Reporting drift never rewrites it or
approves new instructions. Explicit installers own managed merges and backups.
"""
from __future__ import annotations

import os
import re
import stat
from pathlib import Path

from . import migrate

HEADING = '## Agent Quality Gates (AQG) engineering discipline'
NOTICE_PREFIX = 'rules: '
_REFERENCE = re.compile(
    r'(?P<root>(?:[A-Za-z]:[\\/]|/|~[\\/]|<AQG_ROOT>)[^\r\n`<>]*?)'
    r'[\\/]docs[\\/]policies[\\/]audit-trigger\.md'
)


def policy_root(root: Path) -> Path:
    """Preserve supplied symlinks/junctions or recover the matching default root.

    No ancestor search: an old tree or a nondefault entrance's physical target
    cannot be reassociated safely. Callers must supply that logical entrance.
    """
    if __package__.startswith('scripts.'):
        from scripts.aqg_skill_install import classify_install
    else:
        from aqg_skill_install import classify_install
    root = Path(root).expanduser().absolute()
    if classify_install(root) in {'symlink', 'junction'}:
        return root
    default = Path.home().joinpath(*migrate.MANAGED_ROOT_SUFFIX)
    if classify_install(default) == 'junction' and default.resolve() == root.resolve():
        return default
    return migrate.logical_root(root)


def _body(text: str) -> str:
    if HEADING not in text:
        return ''
    body = text.split(HEADING, 1)[1]
    # Marker-delimited installers and legacy heading-delimited rule sections.
    body = body.split('<!-- END AQG rules -->', 1)[0]
    return re.split(r'^#{1,2}\s', body, maxsplit=1, flags=re.MULTILINE)[0]


def policy_problem(text: str, root: Path | None = None) -> str | None:
    """Judge only policy references in the AQG section, never outside examples."""
    body = _body(text)
    if not body:
        return None
    if text.count(HEADING) != 1:
        return 'multiple AQG rules sections require explicit installer repair'
    lines = body.splitlines()
    if any(len(line) > 8192 for line in lines):
        return 'AQG rules contain an oversized line; inspect the policy path explicitly'
    references = [match for line in lines if 'audit-trigger.md' in line
                  for match in _REFERENCE.finditer(line)]
    if not references:
        if root is None and 'docs/policies/audit-trigger.md' in body.replace('\\', '/'):
            return None  # Rootless format check cannot resolve a relative path.
        return 'AQG rules have no usable audit-trigger policy path'
    for match in references:
        spelling = match.group('root')
        if spelling == '<AQG_ROOT>':
            return 'AQG rules still contain an unexpanded policy path'
        normalized = spelling.replace('\\', '/')
        if re.search(r'/versions/[^/]+/?$', normalized):
            return 'AQG policy is pinned to a version directory; reapply rules using the logical AQG entrance'
        if root is not None:
            expected = policy_root(root)
            actual = Path(spelling).expanduser().absolute()
            # Resolve parent aliases (/var versus /private/var), not the final
            # entrance: resolving that would make a physical pin appear current.
            actual = actual.parent.resolve() / actual.name
            expected = expected.parent.resolve() / expected.name
            if os.path.normcase(str(actual)) != os.path.normcase(str(expected)):
                return 'AQG policy points at a different checkout; reapply rules using the logical AQG entrance'
    return None


def rule_locations(home: Path | None = None):
    """Known user files from installer profiles; no project crawl or UI writes."""
    if __package__.startswith('scripts.'):
        from scripts import install_aqg_rules as resident, install_aqg_work_clients as work
        from scripts import install_aqg_qoder as qoder, install_aqg_agent_clients as agents
    else:
        import install_aqg_rules as resident
        import install_aqg_work_clients as work
        import install_aqg_qoder as qoder
        import install_aqg_agent_clients as agents
    explicit_home = home
    home = Path(home) if home is not None else Path.home()
    for client, profile in resident.CLIENTS.items():
        yield client, resident.rules_path(profile, explicit_home)
    for client, profile in work.PROFILES.items():
        if profile.rules:
            yield client, home / profile.user_dir / 'rules/aqg.md'
    for client, profile in qoder.PROFILES.items():
        if qoder._rule_supported(client, 'user'):
            yield client, home / profile['user_dir'] / 'rules/aqg.md'
    for client, profile in agents.PROFILES.items():
        if profile.user_rules_root is None or profile.user_rules_file is None:
            continue
        try:
            config = agents._user_config_root(home, profile)
            directory = agents._resolve_user_root_spec(profile.user_rules_root, home, config)
        except (OSError, ValueError, RuntimeError):
            yield client, None
            continue
        if directory is not None and profile.user_rules_file is not None:
            yield client, directory / profile.user_rules_file


def read_rule_text(path: Path) -> str:
    """Bounded regular-file reads, including readable symlink targets.

    O_NONBLOCK prevents a POSIX FIFO substitution between stat and open from
    holding the update lock. fstat validates the opened object, not its name.
    """
    if not path.is_file():
        raise ValueError('rules path is not a regular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('rules path is not a regular file')
        with os.fdopen(fd, encoding='utf-8') as handle:
            fd = None
            text = handle.read(1048577)
        if len(text) > 1048576:
            raise ValueError('inspection limit reached; AQG ownership not fully determined')
        return text
    finally:
        if fd is not None:
            os.close(fd)


def pending_rules(root: Path, *, home: Path | None = None, exclude_clients=()) -> tuple[str, ...]:
    notices = []
    try:
        for client, path in rule_locations(home):
            if client in exclude_clients:
                continue
            if path is None:
                notices.append(f'{NOTICE_PREFIX}{client}: cannot locate rules; verify host configuration explicitly')
                continue
            if not os.path.lexists(path):
                continue  # This check does not enroll clients or require absent rules.
            try:
                text = read_rule_text(path)
                problem = policy_problem(text, root)
            except (OSError, ValueError, RuntimeError):
                problem = 'cannot fully inspect rules; check file type, size, readability and encoding explicitly'
            if problem:
                notices.append(f'{NOTICE_PREFIX}{client}: {problem}')
    except (OSError, ValueError, RuntimeError, ImportError):
        notices.append(NOTICE_PREFIX + 'inspection unavailable; verify host rules explicitly')
    return tuple(notices)
