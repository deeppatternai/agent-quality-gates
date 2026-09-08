"""Codex adapter — hooks in `$CODEX_HOME/hooks.json`.

The second host, and the one that makes the boundary in `base.py` a claim worth
testing: Codex stores a different file, in a different shape, at a location
resolved from a different environment variable, and its hook entries carry a
Windows command variant Claude Code's do not. None of that reaches the
dispatcher — `verify` returns the same `Evidence` either way.

Like the Claude adapter, this wraps the host installer's own
`inspect_install` rather than reimplementing it: that helper already checks
interpreter runnability and command-signature drift, which are Codex-specific
notions of "stale" that do not exist for Claude Code and should not be invented
twice.

The two adapters are similar enough to tempt a shared base class. Deliberately
not extracted yet — two samples do not show where the seam belongs, and the
differences that exist (a differently-named config argument, a default
location resolved from a different environment variable, a different notion of
drift) are exactly the kind that make a premature base class leak. The shared *contract test* is the abstraction that
matters, and it exists.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Tuple

try:  # invoked as a package (tests, `python3 -m`)
    from scripts import install_aqg_codex_hooks
except ImportError:  # invoked with scripts/ itself on sys.path
    import install_aqg_codex_hooks  # type: ignore[no-redef]

from .base import (
    AdapterError,
    HostAdapter,
    observed_routes,
    require_aqg_root,
)


class CodexAdapter(HostAdapter):
    client_id = "codex"

    #: Codex runs the shared hook scripts through its own runner, so it needs
    #: the same sources the Claude pack ships.
    _HOOK_SOURCES = "agent-packs/claude-code/hooks"

    #: The inspector compares command paths and pinned digests against the live
    #: root. It cannot yet compare staged bytes while rendering logical paths.
    #: The planner's byte-equality proof covers unchanged inputs; changed inputs
    #: still require reconciliation. Only physically pinned commands can defer
    #: that reconciliation while another version becomes live.
    hook_command_is_root_relative = False

    #: Codex takes the repo-root `skills/` wrappers, not the Claude pack's.
    _SKILL_SOURCES = "skills"

    def __init__(
        self,
        *,
        hooks_path: Optional[Path] = None,
        aqg_root: Optional[Path] = None,
        skills_dest: Optional[Path] = None,
    ) -> None:
        # Same call-time reasoning as `_default_target`: CODEX_HOME is read now,
        # not at import, so a session that exports it gets its own directory.
        self._skills_dest = (
            Path(skills_dest)
            if skills_dest is not None
            else install_aqg_codex_hooks._default_target().parent / "skills"
        )
        # `_default_target` reads CODEX_HOME at call time, so the location is
        # resolved when the adapter is built rather than when this module is
        # imported — a session that exports CODEX_HOME still gets its own file.
        self._hooks_path = (
            Path(hooks_path)
            if hooks_path is not None
            else install_aqg_codex_hooks._default_target()
        )
        # Resolved here for the same reason as the Claude adapter: an
        # unresolved root is the adapter's own misconfiguration, and must not be
        # reported as the host's config being invalid.
        self._aqg_root = (
            Path(aqg_root)
            if aqg_root is not None
            else install_aqg_codex_hooks._resolve_aqg_root(None)
        )

    def pinned_command_paths(self) -> Tuple[Path, ...]:
        """Report every owned path for pruning; the apply gate checks stability."""
        try:
            return install_aqg_codex_hooks.owned_command_paths(self._hooks_path)
        except Exception:  # aqg: top-level boundary
            # Reporting nothing is the conservative answer here: the gate then
            # blocks and the pruner then protects nothing extra, which is the
            # behaviour that existed before this method.
            return ()

    def _inspect(self, root: Optional[Path] = None) -> Tuple[str, str]:
        """Ask Codex's own installer what state its hooks file is in. Read-only."""
        # The caller's tree when it named one — during an update that is the
        # STAGED target, because whether these hooks are complete is a question
        # about the tree that is about to be live, not the one that is.
        root = require_aqg_root(
            root if root is not None else self._aqg_root,
            client_id=self.client_id, needs=(self._HOOK_SOURCES,)
        )
        try:
            return install_aqg_codex_hooks.inspect_install(self._hooks_path, root)
        except Exception as exc:  # aqg: top-level boundary
            raise AdapterError(
                f"{self.client_id}: cannot inspect {self._hooks_path}: {exc}"
            ) from exc

    def _observed_routes(self):
        """See `ClaudeCodeAdapter._observed_routes` — same reasoning, different
        source directory: Codex is routed the repo-root `skills/` wrappers."""
        return observed_routes(
            aqg_root=self._aqg_root,
            skills_subdir=self._SKILL_SOURCES,
            dest_root=self._skills_dest,
        )
