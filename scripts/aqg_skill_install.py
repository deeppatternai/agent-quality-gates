#!/usr/bin/env python3
"""Install one AQG skill and report the filesystem mode actually created.

Host-neutral: every client installer routes skill installation through here,
not just one host. Mode link (the default) creates a symlink, falling back to
a Windows junction and then to a copy; mode copy copies outright.
"""

from __future__ import annotations

import argparse
from collections import Counter
import os
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from scripts.aqg_directory_links import (
        DirectoryLinkError,
        create_junction,
        link_kind,
        read_link_target,
    )
except ModuleNotFoundError:  # direct execution with scripts/ on sys.path
    from aqg_directory_links import (  # type: ignore[no-redef]
        DirectoryLinkError,
        create_junction,
        link_kind,
        read_link_target,
    )


class InstallError(RuntimeError):
    """A skill install could not be completed without overwriting user state."""


def _is_windows_host() -> bool:
    return os.name == "nt"


def classify_install(path: Path) -> str:
    """Return symlink, junction, copied, plain_directory, missing, or other."""
    kind = link_kind(path)
    if kind == "symlink":
        return "symlink"
    if kind == "junction":
        return "junction"
    if kind == "directory":
        return "copied" if (path / ".aqg-root").is_file() else "plain_directory"
    if kind == "missing":
        return "missing"
    return kind


def _remove_existing(path: Path) -> None:
    mode = classify_install(path)
    if mode == "missing":
        return
    if mode == "symlink":
        try:
            path.unlink()
        except OSError:
            if not _is_windows_host():
                raise
            os.rmdir(path)
    elif mode == "junction":
        os.rmdir(path)
    elif mode in {"copied", "plain_directory"}:
        shutil.rmtree(path)
    elif mode == "other":
        path.unlink()
    else:
        raise InstallError(f"refusing to remove unsupported reparse point: {path}")


def remove_install(path: Path) -> None:
    """Remove one classified install entry without following its referent."""
    _remove_existing(path)


def _create_symlink(source: Path, target: Path) -> None:
    os.symlink(source, target, target_is_directory=True)


def _create_windows_junction(source: Path, target: Path) -> bool:
    if not _is_windows_host():
        return False
    try:
        create_junction(source, target)
    except DirectoryLinkError:
        return False
    return True


def create_windows_junction(source: Path, target: Path) -> bool:
    """Create a Windows directory junction without interpolating its paths."""
    return _create_windows_junction(source, target)


def _copy_skill(source: Path, target: Path, aqg_root: Path) -> None:
    shutil.copytree(source, target)
    (target / ".aqg-root").write_text(str(aqg_root.resolve()) + "\n", encoding="utf-8")


def _logical_root_of(root: Path) -> Path:
    """`aqg_update.migrate.logical_root`, or the root unchanged if unavailable.

    This installer has to keep working in a checkout with no update engine —
    and a checkout with no update engine has no version swap to survive, so the
    spelling it was given is already the stable one.
    """
    try:
        from scripts.aqg_update.migrate import logical_root
    except ImportError:  # invoked with scripts/ itself on sys.path
        try:
            from aqg_update.migrate import logical_root  # type: ignore[no-redef]
        except ImportError:
            # Loud, because the two sides are contractually required to produce
            # one string and the reader cannot even import without this module.
            # Degrading quietly here writes version-pinned links that the update
            # path will never recognise — the exact silent failure this change
            # exists to remove.
            print(
                "NOTE: the AQG update engine is not importable, so skill links "
                "are written with the path as given. In a managed install this "
                "pins them to one version and they will not receive automatic "
                "updates.",
                file=sys.stderr,
            )
            return root
    return logical_root(root)


def _link_text_for(source: Path, real: Path, aqg_root: Path | None) -> Path:
    """The spelling to write into the link.

    Ownership of a route is an exact link-text comparison, so this installer and
    `aqg_update.plan` must arrive at the same string or they will disagree about
    which links are ours — which is exactly what happened.

    The reader builds ``logical_root(root) / <subdir> / <name>`` **lexically**.
    So this prefers the lexical relationship too, and falls back to the physical
    one only when the given spellings do not share a prefix. Deriving it from
    resolved paths first looked equivalent and is not: any symlinked component
    *inside* the checkout resolves away on this side and stays on the reader's,
    and the two strings differ for a route that is perfectly correct.

    ``expanduser`` on both sides, and before anything else. ``Path.resolve()``
    does not expand ``~`` — it makes ``~/x`` into ``<cwd>/~/x`` — so an
    unexpanded tilde root sent this down the fallback path and wrote the
    physical spelling, silently restoring the stall.

    A source outside the root is left alone: there is no root to re-spell it
    against, and guessing one would write a link naming a directory the caller
    never mentioned.
    """
    if aqg_root is None:
        return _normal(real)
    root_given = _normal(Path(aqg_root).expanduser())
    source_given = _normal(Path(source).expanduser())
    try:
        rel = source_given.relative_to(root_given)
    except ValueError:
        try:
            rel = real.relative_to(Path(aqg_root).expanduser().resolve())
        except (ValueError, OSError):
            return _normal(real)
    return _logical_root_of(root_given) / rel


def _normal(path: Path) -> Path:
    """Absolute and lexically normalised, never resolved."""
    return Path(os.path.normpath(str(Path(path).expanduser().absolute())))


def _resolved_skill_path(path: Path) -> Path:
    """One resolved spelling, including Win32 extended-length path aliases."""
    value = str(path.resolve())
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def skill_link_source(source: Path) -> Path:
    """Use the managed entrance for link identity; leave other checkouts alone."""
    managed = Path.home() / ".deeppattern" / "agent-quality-gates"
    source = source.expanduser()
    return _link_text_for(source, _resolved_skill_path(source), managed)


def same_skill_source(recorded: Path, expected: Path) -> bool:
    """Compare link marker identity, including old physical release markers.

    Directional: recorded is possibly historical metadata; expected belongs to
    the current release (a source or, for ownership, an actual destination).
    This is NOT a target check: callers must also check the actual link against
    the current source. Legacy compatibility is limited to the canonical home
    installation's versions directory and the exact same skill suffix. It is
    read-only, including when retention has already removed the old release.
    """
    if not recorded.is_absolute() or not expected.is_absolute():
        return False
    try:
        if _resolved_skill_path(recorded) == _resolved_skill_path(expected):
            return True
        # Match migrate.logical_root's spelling when HOME itself is a link.
        managed = _resolved_skill_path(Path.home() / ".deeppattern") / "agent-quality-gates"
        if not managed.is_symlink():
            return False
        versions = managed.parent.resolve() / "versions"
        current = _resolved_skill_path(managed)
        if current.parent != versions:
            return False
        relative = skill_link_source(expected).relative_to(managed)
        if len(relative.parts) != 2 or relative.parts[0] != "skills":
            return False
        if not relative.name.startswith("aqg-"):
            return False
        # Resolve aliases/prefixes but reject a surviving release redirected
        # outside this installation. A removed release still resolves lexically.
        old = _resolved_skill_path(recorded).relative_to(versions)
        if len(old.parts) != 3 or Path(*old.parts[1:]) != relative:
            return False
        try:
            from scripts.aqg_update.stage import is_release_name
        except ModuleNotFoundError:
            from aqg_update.stage import is_release_name

        label = old.parts[0]
        return is_release_name(label) or (
            len(label) == 40 and all(c in "0123456789abcdef" for c in label)
        )
    except (OSError, RuntimeError, ValueError, ImportError):
        return False


@dataclass(frozen=True)
class _InstallPlan:
    source: Path
    real: Path
    link_text: Path
    target: Path
    root: Path
    requested_mode: str
    existing: str
    existing_target: Path | None
    unchanged: str | None


def _existing_target_matches(plan: _InstallPlan) -> bool:
    if plan.existing not in {"symlink", "junction"}:
        return False
    expected = plan.link_text if plan.existing == "symlink" else plan.real
    return plan.existing_target is not None and _normal(plan.existing_target) == _normal(expected)


def _prepare_install(
    source: Path,
    target: Path,
    *,
    requested_mode: str,
    force: bool,
    aqg_root: Path | None,
) -> _InstallPlan:
    try:
        real = source.resolve(strict=True)
    except OSError as exc:
        raise InstallError(f"invalid skill source: {source}: {exc}") from exc
    link_text = _link_text_for(source, real, aqg_root)
    if not real.is_dir() or not (real / "SKILL.md").is_file():
        raise InstallError(f"invalid skill source (SKILL.md required): {real}")
    target = target.absolute()
    target = target.parent.resolve(strict=False) / target.name
    if real == target or real in target.parents or target in real.parents:
        raise InstallError(f"source and target must not overlap: {real} / {target}")
    existing = classify_install(target)
    if existing == "other_reparse":
        raise InstallError(f"unsupported reparse point at target: {target}")
    existing_target = None
    if existing in {"symlink", "junction"}:
        try:
            existing_target = read_link_target(target)
        except DirectoryLinkError as exc:
            raise InstallError(f"cannot verify existing {existing}: {target}: {exc}") from exc
    root = Path(aqg_root or real.parent.parent).expanduser().resolve()
    plan = _InstallPlan(
        source=source,
        real=real,
        link_text=link_text,
        target=target,
        root=root,
        requested_mode=requested_mode,
        existing=existing,
        existing_target=existing_target,
        unchanged=None,
    )
    if not force and requested_mode == "link" and _existing_target_matches(plan):
        mode = "linked" if existing == "symlink" else "junctioned"
        return _InstallPlan(**{**plan.__dict__, "unchanged": mode})
    if existing != "missing" and not force:
        if requested_mode == "link" and existing in {"symlink", "junction"}:
            expected = link_text if existing == "symlink" else real
            assert existing_target is not None
            if same_skill_source(existing_target, expected):
                return plan
            raise InstallError(
                f"external {existing}: {target} -> {existing_target} (use --force to replace)"
            )
        display = {
            "copied": "existing copied directory",
            "plain_directory": "existing plain directory",
            "symlink": "existing symlink",
            "junction": "existing junction",
        }.get(existing, "existing file")
        raise InstallError(f"{display}: {target} (use --force to replace)")
    return plan


def _install_absent(plan: _InstallPlan) -> str:
    target = plan.target
    target.parent.mkdir(parents=True, exist_ok=True)
    if plan.requested_mode == "copy":
        try:
            _copy_skill(plan.real, target, plan.root)
        except FileExistsError as exc:
            raise InstallError(f"batch target appeared during copy: {target}") from exc
        # aqg: top-level boundary
        except BaseException:
            if classify_install(target) != "missing":
                _remove_existing(target)
            raise
        return "copied"

    try:
        _create_symlink(plan.link_text, target)
    except OSError as exc:
        if classify_install(target) != "missing":
            raise InstallError(f"batch target appeared during link creation: {target}") from exc
        created = "missing"
    else:
        created = classify_install(target)
    if created == "symlink":
        return "linked"
    if created == "junction":
        return "junctioned"
    if created != "missing":
        _remove_existing(target)
    if _is_windows_host() and _create_windows_junction(plan.real, target):
        if classify_install(target) == "junction":
            return "junctioned"
        _remove_existing(target)

    if classify_install(target) != "missing":
        raise InstallError(f"batch target appeared during junction creation: {target}")

    try:
        _copy_skill(plan.real, target, plan.root)
    except FileExistsError as exc:
        raise InstallError(f"batch target appeared during copy fallback: {target}") from exc
    # aqg: top-level boundary
    except BaseException:
        if classify_install(target) != "missing":
            _remove_existing(target)
        raise
    if plan.requested_mode == "link":
        print(
            f"NOTE: {target.name} was copied, not linked - this host will not "
            f"receive automatic skill updates for it. Directory-link creation failed.",
            file=sys.stderr,
        )
    return "copied"


def _backup_path(target: Path, index: int) -> Path:
    candidate = target.with_name(f".{target.name}.aqg-batch-backup-{os.getpid()}-{index}")
    if classify_install(candidate) != "missing":
        raise InstallError(f"batch backup path already exists: {candidate}")
    return candidate


def install_skills(
    items: list[tuple[Path, Path]],
    *,
    requested_mode: str,
    force: bool,
    aqg_root: Path | None = None,
) -> list[str]:
    """Install one host's routes as a preflighted, recoverable batch."""
    if not items:
        raise InstallError("batch must contain at least one skill")
    plans = [
        _prepare_install(
            source,
            target,
            requested_mode=requested_mode,
            force=force,
            aqg_root=aqg_root,
        )
        for source, target in items
    ]
    targets = [plan.target for plan in plans]
    if len(set(targets)) != len(targets):
        raise InstallError("batch contains duplicate target entries")

    backups: dict[Path, Path] = {}
    touched: list[_InstallPlan] = []
    results: list[str] = []
    creation_complete = False
    try:
        for index, plan in enumerate(plans):
            if plan.unchanged is not None or plan.existing == "missing":
                continue
            current = classify_install(plan.target)
            if current != plan.existing:
                raise InstallError(f"batch target changed after preflight: {plan.target}")
            if plan.existing_target is not None:
                if read_link_target(plan.target) != plan.existing_target:
                    raise InstallError(f"batch link target changed after preflight: {plan.target}")
            backup = _backup_path(plan.target, index)
            os.replace(plan.target, backup)
            backups[plan.target] = backup

        for plan in plans:
            if plan.unchanged is not None:
                results.append(plan.unchanged)
                continue
            result = _install_absent(plan)
            touched.append(plan)
            results.append(result)

        creation_complete = True
        cleanup_errors: list[str] = []
        for backup in backups.values():
            try:
                _remove_existing(backup)
            except (InstallError, OSError) as exc:
                cleanup_errors.append(f"{backup}: {exc}")
        if cleanup_errors:
            raise InstallError(
                "batch installed successfully but old-entry cleanup failed: "
                + "; ".join(cleanup_errors)
            )
        return results
    # aqg: top-level boundary
    except BaseException as exc:
        if creation_complete:
            raise
        rollback_errors: list[str] = []
        for plan in reversed(touched):
            try:
                if classify_install(plan.target) != "missing":
                    _remove_existing(plan.target)
            except (InstallError, OSError) as rollback_exc:
                rollback_errors.append(f"remove {plan.target}: {rollback_exc}")
        for target, backup in reversed(tuple(backups.items())):
            try:
                if classify_install(target) == "missing" and classify_install(backup) != "missing":
                    os.replace(backup, target)
            except (InstallError, OSError) as rollback_exc:
                rollback_errors.append(f"restore {target}: {rollback_exc}")
        if rollback_errors:
            raise InstallError(
                f"batch failed ({exc}); rollback incomplete: {'; '.join(rollback_errors)}"
            ) from exc
        raise


def install_skill(
    source: Path,
    target: Path,
    *,
    requested_mode: str,
    force: bool,
    aqg_root: Path | None = None,
) -> str:
    """Install a skill and return linked, junctioned, or copied.

    Two spellings of one directory, and which is used where is the whole point.

    ``real`` is the physical path. **Every filesystem operation uses it** —
    every check, and every read. A check that runs on one path while the work
    runs on another is not a check: it leaves a window in which the link under
    a logical path can be swapped between the two.

    ``link_text`` is written into the symlink and is used for nothing else. It
    must stay logical, because under the managed layout the physical path is
    ``versions/<sha>/...``: a link naming that stops being recognised as ours at
    the next release (`aqg_update.skills_route` compares raw link text), is
    never re-pointed, and dangles once retention drops the tree it names.
    Deriving it here rather than trusting the caller's spelling is the point —
    the reader derives the same way, and the two must agree.
    """
    return install_skills(
        [(source, target)],
        requested_mode=requested_mode,
        force=force,
        aqg_root=aqg_root,
    )[0]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source")
    parser.add_argument("--target")
    parser.add_argument("--item", nargs=2, action="append", metavar=("SOURCE", "TARGET"))
    parser.add_argument("--aqg-root", required=True)
    parser.add_argument("--mode", choices=("link", "copy"), default="link")
    parser.add_argument("--force", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.item and (args.source or args.target):
        print("ERROR: use either --item or --source/--target", file=sys.stderr)
        return 2
    if args.item:
        items = [(Path(source), Path(target)) for source, target in args.item]
    elif args.source and args.target:
        items = [(Path(args.source), Path(args.target))]
    else:
        print("ERROR: provide --item or both --source and --target", file=sys.stderr)
        return 2
    try:
        actuals = install_skills(
            items,
            requested_mode=args.mode,
            force=args.force,
            aqg_root=Path(args.aqg_root),
        )
    except (InstallError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if len(items) == 1:
        actual = actuals[0]
        source, target = items[0]
        print(f"{actual} {source.name} -> {target}")
    else:
        counts = Counter(actuals)
        detail = ", ".join(f"{mode}={counts[mode]}" for mode in sorted(counts))
        print(f"batch installed {len(items)} skills ({detail})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
