"""Claude Code adapter — hooks merged into the user's `~/.claude/settings.json`.

The mechanics are not reimplemented here. `install_aqg_hooks.inspect_install`
already answers exactly the question `verify` asks — missing / complete / stale /
invalid — and it carries several rounds of hardening this adapter would otherwise
have to repeat: canonical (event, matcher, script) identity rather than a name
match, whitespace-normalized command comparison so a reformat is not read as
drift, and a malformed `settings.json` degrading to a reported state instead of
an exception.

So this file is a translation layer, and that is the intended shape of an
adapter: host mechanics live where the host's installer already lives, and the
adapter maps them into the dispatcher's vocabulary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts import install_aqg_hooks
except ImportError:  # invoked with scripts/ itself on sys.path
    import install_aqg_hooks  # type: ignore[no-redef]

from .base import (
    AdapterError,
    Evidence,
    HostAdapter,
    observed_routes,
    require_aqg_root,
)


class ClaudeCodeAdapter(HostAdapter):
    client_id = "claude-code"

    #: Where this host's canonical hook scripts live under the checkout.
    _HOOK_SOURCES = "agent-packs/claude-code/hooks"

    #: Where its skills are shipped from, relative to the root.
    _SKILL_SOURCES = "agent-packs/claude-code/skills"

    def __init__(
        self,
        *,
        settings_path: Optional[Path] = None,
        aqg_root: Optional[Path] = None,
        skills_dest: Optional[Path] = None,
    ) -> None:
        self._skills_dest = (
            Path(skills_dest)
            if skills_dest is not None
            else Path.home() / ".claude" / "skills"
        )
        self._settings_path = (
            Path(settings_path)
            if settings_path is not None
            else Path.home() / ".claude" / "settings.json"
        )
        # Resolved here rather than left None. `inspect_install` answers
        # "invalid" for an unresolved root, which would blame the HOST's config
        # for the adapter's own misconfiguration -- and since `adapter_for`
        # passes no arguments, every production call would report every host as
        # broken. Reuses the installer's own resolution (env, then sentinel
        # walk-up) so both agree on what the root is.
        self._aqg_root = (
            Path(aqg_root)
            if aqg_root is not None
            else install_aqg_hooks._resolve_aqg_root(None)
        )

    def _inspect(self) -> Tuple[str, str]:
        """Ask the host's own installer what state it is in. Read-only.

        Wrapped so a test can substitute a status this contract has not seen —
        the boundary where a future helper state would otherwise leak upward.
        """
        root = require_aqg_root(
            self._aqg_root, client_id=self.client_id, needs=(self._HOOK_SOURCES,)
        )
        try:
            return install_aqg_hooks.inspect_install(self._settings_path, root)
        except Exception as exc:  # aqg: top-level boundary
            raise AdapterError(
                f"{self.client_id}: cannot inspect {self._settings_path}: {exc}"
            ) from exc

    def _observed_routes(self):
        """Read the routes off disk rather than trusting the install record.

        The spelling is derived from the layout by `migrate.logical_root`, not
        read from `AQG_ROOT`: by the time an update check runs from a skill,
        that variable holds the PHYSICAL path — `_aqg_context.sh` pins it that
        way so one invocation cannot tear across a swap, which is correct for
        executing and wrong for deciding ownership.
        """
        return observed_routes(
            aqg_root=self._aqg_root,
            skills_subdir=self._SKILL_SOURCES,
            dest_root=self._skills_dest,
        )
