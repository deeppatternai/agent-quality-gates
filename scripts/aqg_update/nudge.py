"""Best-effort background update admission for the two Python skill CLIs.

No network, wait, state writes or cooldown latch in the foreground. The existing
signed runner owns admission, locks, retry intervals and transactional apply.
"""
from pathlib import Path
import os
import subprocess
import sys

try:
    from scripts import aqg_directory_links as directory_links
except ImportError:
    import aqg_directory_links as directory_links  # type: ignore[no-redef]


def nudge() -> None:
    """Detach a quiet updater for this managed installation, or silently skip."""
    try:
        if os.environ.get('AQG_NO_UPDATE_CHECK'):
            return
        physical = Path(__file__).resolve(strict=True).parents[2]
        root = Path.home() / '.deeppattern' / 'agent-quality-gates'
        # A development checkout must never update a separate user install,
        # even if it inherited AQG_ROOT from the invoking agent.
        if directory_links.link_kind(root) not in {"symlink", "junction"} \
                or directory_links.read_link_target(root).resolve(strict=True) != physical:
            return
        if not (physical / 'scripts/aqg_update/run.py').is_file():
            return
        # Pass the validated generation. The runner resolves it back to the
        # managed entrance only while they still match, rejecting an obsolete
        # physical version before network access if the link moved meanwhile.
        env = dict(os.environ, AQG_ROOT=str(physical))
        env['AQG_UPDATE_TRIGGER'] = {
            'aqg_preflight.py': 'aqg-startup-preflight',
            'aqg_construction_check.py': 'aqg-code-construction',
        }.get(Path(sys.argv[0]).name, 'python-skill')
        for name in (
            'PYTHONPATH', 'PYTHONHOME', 'PYTHONSTARTUP', 'PYTHONEXECUTABLE',
            'LD_PRELOAD', 'LD_LIBRARY_PATH', 'DYLD_INSERT_LIBRARIES', 'DYLD_LIBRARY_PATH',
        ):
            env.pop(name, None)
        # Git hooks can inherit the foreground project's repository/index
        # routing. Do not let that redirect the updater's explicit -C commands.
        for name in tuple(env):
            if name in {
                'GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_INDEX_FILE',
                'GIT_PREFIX', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS',
            } or name.startswith(('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')):
                env.pop(name)
        options = dict(
            cwd=str(physical), env=env, close_fds=True,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if sys.platform == 'win32':
            # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: no console window.
            # This avoided observed Git startup failures (0xC0000142) on the
            # affected host. CREATE_NO_WINDOW must not use DETACHED_PROCESS.
            options['creationflags'] = 0x08000000 | 0x00000200
        else:
            options['start_new_session'] = True
        subprocess.Popen(
            [sys.executable, '-E', '-s', '-m', 'scripts.aqg_update.run'], **options,
        )
    except Exception:  # aqg: top-level boundary
        # No timestamp or persistent flag is written here: the next invocation
        # can retry even if path resolution or process creation failed.
        return
