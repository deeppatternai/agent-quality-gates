#!/usr/bin/env python3
"""Install the always-resident AQG rules block for Claude Code and Codex.

Three channels deliver the discipline to a host: skills (invoked on demand),
hooks (fired on an event), and the rules block (resident in every prompt).
Only the third can carry the Gate A criteria, because "does this need an audit
at all" is asked BEFORE anything is read — a skill description arrives too late
to answer it.

Four adapters (agent-clients, work-clients, cursor, qoder) already write that
block for their clients. Claude Code and Codex had none: the registry declared
a ``rules_surface`` for both and ``aqg_doctor`` checked it, but nothing laid it
down, so a scripted install produced skills and hooks over an empty resident
channel and reported success.

Two deliberate differences from the four older adapters:

* **BEGIN/END markers.** They treat "marker to end of file" as managed, which
  silently owns anything the user appends afterwards. ``~/.claude/CLAUDE.md``
  and ``~/.codex/AGENTS.md`` are the user's own files, hand-edited far more
  than a generated adapter rule, so the region is delimited at both ends and
  everything outside it is left alone.
* **Refuse rather than repair a malformed region.** A file with one marker and
  not the other has been hand-edited in a way this script cannot interpret;
  guessing where the block ends risks deleting the user's prose.

Exit codes: ``0`` success, ``1`` failure. Callers that must not fail the whole
install on a rules failure absorb the non-zero themselves — see the ``||`` in
the apply command registered in ``scripts/aqg_client_registry.py``.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

try:
    from _aqg_backup import BackupSession
except ModuleNotFoundError:  # invoked as scripts.install_aqg_rules
    from scripts._aqg_backup import BackupSession


EXIT_OK = 0
EXIT_ERROR = 1

HEADING = "## Agent Quality Gates (AQG) engineering discipline"
BEGIN_MARKER = (
    "<!-- BEGIN AQG rules (managed by scripts/install_aqg_rules.py; do not edit inside) -->"
)
END_MARKER = "<!-- END AQG rules -->"
# Matched as a prefix so a future revision of the BEGIN line still finds, and
# replaces, a region an older version wrote.
BEGIN_PREFIX = "<!-- BEGIN AQG rules"
PLACEHOLDER = "<AQG_ROOT>"


class InstallError(RuntimeError):
    """A condition the caller must see, not one this script may work around."""


@dataclass(frozen=True)
class ClientProfile:
    client_id: str
    root_parts: tuple[str, ...]
    rules_name: str
    template: str
    home_env: str | None


CLIENTS: dict[str, ClientProfile] = {
    "claude-code": ClientProfile(
        client_id="claude-code",
        root_parts=(".claude",),
        rules_name="CLAUDE.md",
        template="examples/aqg-claude-rules.example.md",
        home_env=None,
    ),
    "codex": ClientProfile(
        client_id="codex",
        root_parts=(".codex",),
        rules_name="AGENTS.md",
        # Both templates carry the same block today; each client reads its own
        # so a future divergence lands where it was authored instead of being
        # silently overwritten by the other client's copy.
        template="examples/aqg-codex-agents.example.md",
        home_env="CODEX_HOME",
    ),
}


# --- paths -------------------------------------------------------------------


def _resolve_aqg_root(value: str | None) -> Path:
    """The explicit flag, else the environment, else the checkout holding this file.

    A supplied-but-invalid root is an error, not a fall-through to the next
    source: installing from a different checkout than the caller named is worse
    than stopping. The final fallback is validated the same way, because
    aqg_doctor's fix line omits ``--aqg-root`` and relies on it, and an
    unvalidated root surfaces later as a confusing missing-template read.
    """
    for candidate in (value, os.environ.get("AQG_ROOT")):
        if candidate:
            root = Path(candidate).expanduser()
            if (root / "VERSION").is_file():
                return root.absolute()
            raise InstallError(f"not an AQG checkout (no VERSION file): {root}")
    root = Path(__file__).absolute().parent.parent
    if not (root / "VERSION").is_file():
        raise InstallError(
            f"not an AQG checkout (no VERSION file): {root} — pass --aqg-root"
        )
    return root


def client_root(profile: ClientProfile, home: Path | None) -> Path:
    """The client's config directory.

    ``--home`` wins over the client's own env var so a test (or an isolated
    install) can redirect every client with one flag and not have to know which
    of them read an environment variable.
    """
    if home is not None:
        return home.joinpath(*profile.root_parts)
    if profile.home_env:
        override = os.environ.get(profile.home_env)
        if override:
            return Path(override).expanduser()
    return Path.home().joinpath(*profile.root_parts)


def rules_path(profile: ClientProfile, home: Path | None) -> Path:
    return client_root(profile, home) / profile.rules_name


# --- rendering ---------------------------------------------------------------


def render_region(profile: ClientProfile, aqg_root: Path) -> str:
    """The managed region: markers around the template's block, root resolved."""
    template = (aqg_root / profile.template).read_text(encoding="utf-8")
    start = template.find(HEADING)
    if start < 0:
        raise InstallError(f"{profile.template} has no {HEADING!r} heading to install")
    block = template[start:].rstrip()
    # A rules file is not a shell: nothing there would ever expand the
    # placeholder, so the one line that leads to the authoritative criteria
    # would lead nowhere.
    block = block.replace(PLACEHOLDER, str(aqg_root))
    return f"{BEGIN_MARKER}\n{block}\n{END_MARKER}"


def _span(text: str) -> tuple[int, int] | None:
    """Locate the managed region, or raise if the file cannot be interpreted.

    Returns ``None`` when no region is present. Anything other than exactly one
    well-ordered marker pair is a refusal, not a repair.
    """
    begins = text.count(BEGIN_PREFIX)
    ends = text.count(END_MARKER)
    if begins == 0 and ends == 0:
        return None
    if begins != 1 or ends != 1:
        raise InstallError(
            f"malformed AQG region: found {begins} begin and {ends} end markers; "
            "remove the partial region by hand, then re-run"
        )
    start = text.index(BEGIN_PREFIX)
    end = text.index(END_MARKER) + len(END_MARKER)
    if end <= start:
        raise InstallError(
            "malformed AQG region: the end marker precedes the begin marker; "
            "remove the partial region by hand, then re-run"
        )
    return start, end


def _reject_legacy_block(current: str, span: tuple[int, int] | None) -> None:
    """Refuse a block that predates this installer instead of duplicating it.

    Everyone who installed before this script has an unmarked block, pasted by
    an agent following AI_SETUP's hand-edit procedure. Appending would leave the
    file stating the discipline twice; adopting it would mean guessing where the
    user's own prose resumes, and the old block's boundary was only ever a
    heading-level heuristic. Neither is safe to do unattended.
    """
    outside = current if span is None else current[: span[0]] + current[span[1] :]
    # Anchored to a heading line: a user who *writes about* the block ("see the
    # `## Agent Quality Gates ...` section") was otherwise locked out of
    # installing, with no way forward short of rewording their own notes.
    if re.search(r"^#{1,6} *" + re.escape(HEADING.lstrip("# ")), outside, re.M):
        raise InstallError(
            "found an unmarked legacy AQG block (no BEGIN/END markers) — it was "
            "installed by hand before this script existed. Delete that section, "
            "then re-run; this script will not append a second copy or guess "
            "where the old one ends."
        )


def _installed_root(region: str) -> str | None:
    """The checkout path baked into an installed region, if it can be read back."""
    match = re.search(r"(\S+)/docs/policies/audit-trigger\.md", region)
    return match.group(1) if match else None


def compose(current: str, region: str) -> str:
    """Replace the managed region in *current*, or append it, preserving the rest."""
    span = _span(current)
    _reject_legacy_block(current, span)
    if span is None:
        prefix = current.rstrip()
        return (prefix + "\n\n" if prefix else "") + region + "\n"
    start, end = span
    return current[:start] + region + current[end:]


def strip(current: str) -> str:
    """Remove the managed region, leaving the user's own content untouched."""
    span = _span(current)
    if span is None:
        return current
    start, end = span
    remainder = current[:start].rstrip() + "\n\n" + current[end:].lstrip()
    return remainder.rstrip() + "\n" if remainder.strip() else ""


# --- writing -----------------------------------------------------------------


def _refuse_symlink(path: Path) -> None:
    if path.is_symlink():
        raise InstallError(f"refusing to write rules through symlink: {path}")


def _read(path: Path) -> str:
    """Read without newline translation — the region is not the whole file.

    ``read_text`` applies universal-newline decoding, so a CRLF file came back
    as LF and was written back as LF throughout, rewriting every line outside
    the managed region. Windows is a supported host.
    """
    _refuse_symlink(path)
    if not path.is_file():
        return ""
    with path.open("r", encoding="utf-8", newline="") as stream:
        return stream.read()


def _atomic_write(path: Path, text: str) -> None:
    """Replace *path* atomically, preserving its mode and its line endings.

    mkstemp creates at 0600 and os.replace carries that onto the target, so a
    rules file the user had made group-readable would silently narrow.
    ``newline=""`` writes the string's own newlines rather than translating.
    """
    _refuse_symlink(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.is_file() else None
    handle, temp_name = tempfile.mkstemp(prefix=".tmp-aqg-rules-", dir=str(path.parent))
    try:
        if mode is not None:
            os.chmod(temp_name, mode)
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _write_with_backup(
    profile: ClientProfile,
    path: Path,
    rendered: str,
    aqg_root: Path,
    *,
    expected: str,
) -> None:
    """Stash the user's existing file in the central store, then replace it.

    *expected* is the content this run read and based ``rendered`` on. Re-reading
    it here turns the read-modify-write into a compare-and-swap: a file another
    process (or the user's editor) changed in between is refused rather than
    silently overwritten with a version composed from stale content.
    """
    if _read(path) != expected:
        raise InstallError(
            f"{path} changed while installing — nothing was written; re-run"
        )
    with BackupSession(
        profile.client_id,
        path.parent,
        installer="install_aqg_rules.py",
        aqg_root=aqg_root,
    ) as session:
        if path.is_file():
            session.backup(path)
        _atomic_write(path, rendered)


# --- commands ----------------------------------------------------------------


def cmd_apply(profile: ClientProfile, home: Path | None, aqg_root: Path) -> int:
    path = rules_path(profile, home)
    current = _read(path)
    rendered = compose(current, render_region(profile, aqg_root))
    if rendered == current:
        print(f"[{profile.client_id}] rules block already current: {path}")
        return EXIT_OK
    _write_with_backup(profile, path, rendered, aqg_root, expected=current)
    action = "updated" if current else "created"
    print(f"[{profile.client_id}] rules block {action}: {path}")
    return EXIT_OK


def cmd_uninstall(profile: ClientProfile, home: Path | None, aqg_root: Path) -> int:
    path = rules_path(profile, home)
    if not path.is_file() and not path.is_symlink():
        print(f"[{profile.client_id}] no rules file to clean: {path}")
        return EXIT_OK
    current = _read(path)
    remainder = strip(current)
    if remainder == current:
        # A legacy unmarked block is still an AQG block. Saying "nothing to
        # remove" would tell the user their machine is clean while the
        # discipline is still resident in every prompt.
        _reject_legacy_block(current, _span(current))
        print(f"[{profile.client_id}] no managed rules block present: {path}")
        return EXIT_OK
    _write_with_backup(profile, path, remainder, aqg_root, expected=current)
    print(f"[{profile.client_id}] rules block removed: {path}")
    return EXIT_OK


def cmd_verify(profile: ClientProfile, home: Path | None, aqg_root: Path) -> int:
    path = rules_path(profile, home)
    if not path.is_file():
        print(f"[{profile.client_id}] FAIL no rules file: {path}", file=sys.stderr)
        return EXIT_ERROR
    current = _read(path)
    span = _span(current)
    if span is None:
        print(
            f"[{profile.client_id}] FAIL no managed rules block in {path}", file=sys.stderr
        )
        return EXIT_ERROR
    start, end = span
    region = current[start:end]
    expected = render_region(profile, aqg_root)
    if region != expected:
        installed_from = _installed_root(region)
        # Substitute the old root back rather than re-rendering from it: the
        # whole point of this branch is that the old checkout may be gone.
        if installed_from and region.replace(installed_from, str(aqg_root)) == expected:
            print(
                f"[{profile.client_id}] FAIL rules block was installed from a "
                f"different checkout ({installed_from}); the path inside it no "
                f"longer leads anywhere. Re-run --apply: {path}",
                file=sys.stderr,
            )
            return EXIT_ERROR
        print(
            f"[{profile.client_id}] FAIL rules block differs from the shipped template "
            f"(edited by hand, or written by an older version): {path}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    print(f"[{profile.client_id}] OK rules block current: {path}")
    return EXIT_OK


def cmd_is_installed(profile: ClientProfile, home: Path | None, aqg_root: Path) -> int:
    """Presence of a well-formed region — whether it is CURRENT is --verify's job."""
    path = rules_path(profile, home)
    if not path.is_file():
        return EXIT_ERROR
    current = _read(path)
    span = _span(current)
    if span is not None:
        return EXIT_OK
    # "Absent" and "a legacy block is resident" are different states, and only
    # the second one asks anything of the user.
    _reject_legacy_block(current, span)
    return EXIT_ERROR


# --- cli ---------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="install_aqg_rules",
        description="Install the resident AQG rules block for Claude Code or Codex.",
    )
    parser.add_argument("--client", required=True, choices=sorted(CLIENTS))
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--apply", action="store_true")
    action.add_argument("--verify", action="store_true")
    action.add_argument("--uninstall", action="store_true")
    action.add_argument("--is-installed", action="store_true")
    parser.add_argument("--aqg-root", help="AQG checkout to install from")
    parser.add_argument("--home", help="treat this directory as HOME (isolation seam)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        profile = CLIENTS[args.client]
        aqg_root = _resolve_aqg_root(args.aqg_root)
        home = Path(args.home).expanduser().resolve() if args.home else None
        if args.apply:
            return cmd_apply(profile, home, aqg_root)
        if args.uninstall:
            return cmd_uninstall(profile, home, aqg_root)
        if args.verify:
            return cmd_verify(profile, home, aqg_root)
        return cmd_is_installed(profile, home, aqg_root)
    except Exception as exc:  # aqg: top-level boundary
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
