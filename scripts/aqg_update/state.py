"""The machine-side record of what AQG installed — `install-state.json`.

Every later update decision is a diff against this file (docs/UPDATE_ARCHITECTURE.md
§4): whether a skill was added, removed or *renamed* (a rename is only ever
observable as delete+add against a recorded previous state), which hosts are
behind, and what is still pending. Before this file existed the answer was
re-derived by probing, which is why a rename and an unrelated stale symlink were
indistinguishable.

Two properties are load-bearing:

* **Fail-closed on every ambiguity.** A partially-readable state is worse than no
  state: read as "nothing installed", it makes the planner re-route every skill
  and prune links it does not own. Every unreadable shape therefore raises
  ``StateError`` rather than degrading — malformed, truncated, wrongly-typed,
  unknown-schema, non-UTF-8, pathologically nested, or reached through a broken
  symlink. Only a genuinely absent file returns ``None``, the normal first-run
  case, so callers can tell "never installed" from "damaged".

  The corollary is that ``StateError`` is the module's *whole* failure surface.
  A native ``TypeError`` or ``OSError`` escaping to the caller is a bug, not an
  edge case: an external panel found five such leaks in the first draft
  (``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``; ``[] in
  frozenset()`` raises ``TypeError``; ``json.dumps`` and ``mkdir`` were
  untranslated), and each one crashed a caller that had been promised a refusal.
* **The file lives OUTSIDE ``AQG_ROOT``.** The atomic swap in §5.1 replaces
  ``AQG_ROOT`` wholesale, so state kept inside it would vanish or revert to an
  older version at exactly the moment a rollback needs it. Every path goes
  through ``_refuse_inside_aqg_root`` — the default root *and* an explicit
  ``path=`` — because an invariant enforced on one branch is a property of that
  branch, not of the module.

Writes reuse ``_aqg_backup._atomic_write_bytes`` rather than reimplementing the
temp-file/fsync/``fchmod``-on-fd/``os.replace`` dance: that helper already
carries a reviewed TOCTOU fix, and a second copy would have to be hardened
again, separately, forever.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

try:  # invoked as a package (tests, `python3 -m`)
    from scripts._aqg_backup import BackupError, _atomic_write_bytes
except ImportError:  # invoked with scripts/ itself on sys.path
    from _aqg_backup import BackupError, _atomic_write_bytes  # type: ignore[no-redef]


STATE_SCHEMA = 1
STATE_FILENAME = "install-state.json"

#: `stable` follows signed tags and is the only channel an automatic apply may
#: use; `edge` follows main and is manual-only (§9). An unrecognized value is
#: refused rather than defaulted, because this field gates signature checking.
CHANNELS = frozenset({"stable", "edge"})

#: Schemas this version can read. A newer one is refused because we cannot know
#: its meaning; an *older unknown* one is refused for the same reason, so the set
#: is the check rather than a `> STATE_SCHEMA` comparison — when schema 2 lands,
#: 1 stays here and gains a migration rather than being locked out.
_KNOWN_SCHEMAS = frozenset({1})

#: Required keys that must additionally be strings. They are compared and
#: rendered downstream (a version against a tag, a commit against `git`), so a
#: wrongly-typed one validates here and fails somewhere far away instead.
_STRING_KEYS = (
    "installed_version",
    "installed_commit",
    "installed_at",
    "applied_by",
)

_REQUIRED_KEYS = (
    "schema",
    "channel",
    "installed_version",
    "installed_commit",
    "release_sequence",
    "installed_at",
    "applied_by",
    "hosts",
    "pending",
)


def _require_int(payload: Dict[str, Any], key: str) -> int:
    """Return an integer field, refusing ``bool`` — ``True`` is an ``int`` in
    Python, and accepting it would make ``release_sequence`` compare as 1."""
    value = payload[key]
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateError(
            f"install state {key!r} must be an integer, got {type(value).__name__}"
        )
    return value


class StateError(RuntimeError):
    """A refusal to read or write install state. Always fail-closed."""


def _resolve(path: Path, label: str) -> Path:
    """Fully resolve *path*, refusing rather than degrading.

    The previous fallback to ``absolute()`` was fail-OPEN: it does not follow
    symlinks, so a root that really is inside ``AQG_ROOT`` could pass the
    containment check whenever ``resolve()`` failed (a symlink loop, say).
    """
    try:
        return path.resolve()
    except OSError as exc:
        raise StateError(f"cannot resolve {label} {path}: {exc}") from exc


def _refuse_inside_aqg_root(candidate: Path, label: str) -> None:
    """Enforce the one invariant this module exists to protect.

    Applied to EVERY path, default or caller-supplied: an invariant that holds
    only on the default branch is a property of that branch, not of the module.
    Silent when ``AQG_ROOT`` is unset — the containment question is unanswerable
    then, and the default root is a sibling of the checkout by construction.
    """
    aqg_root = os.environ.get("AQG_ROOT")
    if not aqg_root:
        return
    resolved = _resolve(candidate, label)
    resolved_aqg = _resolve(Path(aqg_root), "AQG_ROOT")
    if resolved == resolved_aqg or resolved.is_relative_to(resolved_aqg):
        raise StateError(
            f"refusing {label} inside AQG_ROOT: {candidate} — the version swap "
            f"replaces {aqg_root} wholesale, which would take the state with it"
        )


def state_root(*, create: bool = True) -> Path:
    """Resolve the directory holding ``install-state.json``.

    Precedence: ``AQG_STATE_ROOT`` (which must be absolute) >
    ``~/.deeppattern/aqg-state``. Mirrors ``_aqg_backup.backup_base``'s
    env-override shape.

    Raises ``StateError`` when the root would sit inside ``AQG_ROOT``, is
    reached through a symlink, is relative, or cannot be created.
    """
    override = os.environ.get("AQG_STATE_ROOT")
    if override:
        root = Path(override)
        if not root.is_absolute():
            # A relative override resolves against the caller's CWD, so a
            # SessionStart hook and upgrade.sh would read different files —
            # each reporting the other's install as absent.
            raise StateError(
                f"AQG_STATE_ROOT must be an absolute path, got {override!r}"
            )
    else:
        root = Path.home() / ".deeppattern" / "aqg-state"

    _refuse_inside_aqg_root(root, "state root")

    # Checked in both modes: a read through a redirected root is exactly as
    # wrong as a write through one.
    if root.is_symlink():
        raise StateError(f"refusing to use state root through symlink: {root}")

    if create:
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"cannot create state root {root}: {exc}") from exc
        try:  # best-effort: Windows has no meaningful POSIX mode
            os.chmod(root, 0o700)
        except OSError:
            pass
    return root


def state_path(*, create: bool = True) -> Path:
    """Absolute path of the state file (its directory created when asked)."""
    return state_root(create=create) / STATE_FILENAME


def _checked_path(path: Optional[Path], *, create: bool) -> Path:
    """Resolve the target for a read or write, enforcing the location invariant.

    Both the default root and an explicit ``path=`` go through here, so no call
    shape can bypass the containment check.
    """
    if path is None:
        return state_path(create=create)
    target = Path(path)
    _refuse_inside_aqg_root(target, "state file")
    return target


def validate_state(payload: Any) -> Dict[str, Any]:
    """Return *payload* if it is a well-formed state document, else raise.

    Type checks are part of the contract, not defensive noise:
    ``release_sequence`` is the anti-rollback comparison, so a string there
    silently disables the guard.
    """
    if not isinstance(payload, dict):
        raise StateError(
            f"install state must be a JSON object, got {type(payload).__name__}"
        )

    if "schema" not in payload:
        raise StateError("install state is missing required keys: schema")
    schema = _require_int(payload, "schema")
    if schema not in _KNOWN_SCHEMAS:
        raise StateError(
            f"install state schema {schema} is not one this AQG understands "
            f"({sorted(_KNOWN_SCHEMAS)}); upgrade AQG, or remove the state file "
            f"to re-record it"
        )

    missing = [key for key in _REQUIRED_KEYS if key not in payload]
    if missing:
        raise StateError(f"install state is missing required keys: {', '.join(missing)}")

    channel = payload["channel"]
    # isinstance BEFORE membership: `[] in frozenset(...)` raises TypeError,
    # which would crash the caller instead of refusing the document.
    if not isinstance(channel, str) or channel not in CHANNELS:
        raise StateError(
            f"install state has an unknown channel {channel!r}; "
            f"expected one of {sorted(CHANNELS)}"
        )

    _require_int(payload, "release_sequence")

    for key in _STRING_KEYS:
        if not isinstance(payload[key], str):
            raise StateError(
                f"install state {key!r} must be a string, "
                f"got {type(payload[key]).__name__}"
            )

    if not isinstance(payload["hosts"], dict):
        raise StateError(
            f"install state 'hosts' must be a mapping keyed by client id, "
            f"got {type(payload['hosts']).__name__}"
        )
    if not isinstance(payload["pending"], list):
        raise StateError(
            f"install state 'pending' must be a list, "
            f"got {type(payload['pending']).__name__}"
        )
    return payload


def read_state(*, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Read install state, or ``None`` when this machine has none recorded.

    ``None`` means "never installed by the updater" — a normal first run.
    Anything else that cannot be read with confidence raises ``StateError``.
    """
    target = _checked_path(path, create=False)
    try:
        raw = target.read_bytes()
    except FileNotFoundError:
        # A dangling symlink also raises FileNotFoundError. Reporting that as
        # absence is the worst possible answer: the planner would conclude
        # "nothing installed" and re-route or prune every host.
        if target.is_symlink():
            raise StateError(
                f"install state at {target} is a broken symlink — refusing to "
                f"report a damaged install as an absent one"
            ) from None
        return None
    except OSError as exc:
        raise StateError(f"cannot read install state at {target}: {exc}") from exc

    # Decoding happens inside this try because UnicodeDecodeError is a
    # ValueError, not an OSError — corrupt bytes used to escape uncaught.
    # RecursionError comes from a pathologically nested document.
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError) as exc:
        raise StateError(
            f"install state at {target} is not a readable JSON document: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    return validate_state(payload)


def write_state(payload: Dict[str, Any], *, path: Optional[Path] = None) -> Path:
    """Validate then atomically write install state; return the path written.

    Validation happens BEFORE the write so an invalid document can never be
    persisted — a bad file on disk would strand every later read in fail-closed
    with no way out but manual deletion.
    """
    validate_state(payload)
    target = _checked_path(path, create=True)
    # `hosts` values are only checked to be inside a mapping, so a value json
    # cannot encode survives validation; serialize BEFORE touching the target so
    # a TypeError here cannot leave a half-written file.
    try:
        data = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    except (TypeError, ValueError) as exc:
        raise StateError(f"install state is not JSON-serializable: {exc}") from exc
    try:
        _atomic_write_bytes(target, data.encode("utf-8"), mode=0o600)
    except (BackupError, OSError) as exc:
        raise StateError(f"cannot write install state at {target}: {exc}") from exc
    return target
