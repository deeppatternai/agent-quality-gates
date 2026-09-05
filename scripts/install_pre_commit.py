#!/usr/bin/env python3
"""AQG pre-commit hook installer (Wave 1 #1, framework v2.1 §7 #1).

Adapt pre-commit framework + Gitleaks for git lifecycle gatekeeping. NOT a bundle —
pre-commit / Gitleaks are third-party; AQG provides the installer + config template + dogfood.

Implemented per triple-audit (audit_id 7ebd63e7) 15 accepted findings:
- NOT pip install by default (3-auditor convergent critical: PEP 668 + supply-chain trust);
  pre-commit missing → print install instructions + exit 4; --allow-pip enables it explicitly
- Always use sys.executable -m pre_commit (PATH safe; 2-auditor convergent)
- _safe_run wrapper: timeout + GIT_TERMINAL_PROMPT=0 + PIP_NO_INPUT=1 (2-auditor convergent)
- git rev-parse --is-inside-work-tree instead of .git/ stat (gpt-5.5 #3)
- byte-identical config treated as OK and continue (gpt-5.5 #7 idempotent)
- pin pre-commit's own version (o3 #1)
- distinguish exit codes 0/1/2/3/4/5 (o3 #4)
- --ci-mode alias for --skip-pip (o3 #8)
- document the Go dep for Gitleaks in the README (gpt-5.5 #4 + gemini #3)

No third-party library dependency, stdlib only.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional


# ===== Exit codes (o3 #4 accepted: distinguish cases) =====
EXIT_OK = 0
EXIT_GENERIC_FAIL = 1
EXIT_USAGE = 2
EXIT_FILE_EXISTS = 3
EXIT_PIP_FAIL = 4
EXIT_HOOK_INSTALL_FAIL = 5

# Default pinned pre-commit version (o3 #1 accepted)
DEFAULT_PRE_COMMIT_VERSION = "4.0.1"

# Subprocess timeouts (gpt-5.5 #6 + o3 #5 convergent)
TIMEOUT_PIP_S = 60
TIMEOUT_PRE_COMMIT_S = 30
TIMEOUT_GIT_S = 5

# Template paths (relative to AQG repo root)
TEMPLATE_CONFIG_REL = "templates/pre-commit-config.aqg.example.yaml"
TEMPLATE_GITLEAKS_TOML_REL = "templates/.gitleaks.aqg.example.toml"


# ===== AQG repo root resolution =====


def _find_aqg_root() -> Path:
    """Walk up from this script to find AQG repo root."""
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "VERSION").is_file() and (p / "templates").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise RuntimeError(
        f"cannot find AQG repo root from {Path(__file__).resolve()}"
    )


# ===== Subprocess helper (gpt-5.5 #6 + o3 #5 convergent) =====


def _safe_run(
    cmd: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout: int = TIMEOUT_PRE_COMMIT_S,
    extra_env: Optional[dict] = None,
) -> tuple[int, str, str]:
    """Run subprocess with timeout + non-interactive env. Never raise."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["PIP_NO_INPUT"] = "1"
    env["PRE_COMMIT_COLOR"] = "never"
    env["TERM"] = "dumb"
    if extra_env:
        env.update(extra_env)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            env=env,
        )
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        return 127, "", f"{type(exc).__name__}: {exc}"


# ===== Git repo validation (gpt-5.5 #3 accepted) =====


def _resolve_git_toplevel(target: Path) -> Optional[Path]:
    """Use `git rev-parse` to validate + get repo top-level dir.

    gpt-5.5 #3 accepted: a .git/ stat is not a valid test (worktree / submodule
    have a .git file); use rev-parse and reject bare repos.
    """
    rc, out, _ = _safe_run(
        ["git", "-C", str(target), "rev-parse", "--is-inside-work-tree"],
        timeout=TIMEOUT_GIT_S,
    )
    if rc != 0 or out.strip() != "true":
        return None
    rc, top, _ = _safe_run(
        ["git", "-C", str(target), "rev-parse", "--show-toplevel"],
        timeout=TIMEOUT_GIT_S,
    )
    if rc != 0 or not top.strip():
        return None
    return Path(top.strip()).resolve()


# ===== pre-commit detection / install =====


def _resolve_pre_commit_cmd() -> Optional[list[str]]:
    """Return argv prefix to invoke pre-commit, or None if unavailable.

    Post-impl dual-audit gpt-5.5 #2 (major): a pipx-installed pre-commit cannot use
    sys.executable -m pre_commit (pipx installs into an isolated venv, the current
    Python cannot find the module). Fallback to absolute pre-commit binary via shutil.which.
    """
    rc, _, _ = _safe_run(
        [sys.executable, "-m", "pre_commit", "--version"],
        timeout=TIMEOUT_PRE_COMMIT_S,
    )
    if rc == 0:
        return [sys.executable, "-m", "pre_commit"]
    binary = shutil.which("pre-commit")
    if binary:
        rc, _, _ = _safe_run([binary, "--version"], timeout=TIMEOUT_PRE_COMMIT_S)
        if rc == 0:
            return [binary]
    return None


def _check_pre_commit_available() -> bool:
    """True if pre-commit can be invoked (either via -m or via shutil.which)."""
    return _resolve_pre_commit_cmd() is not None


def _is_in_virtualenv() -> bool:
    """Detect active virtualenv (post-impl dual-audit gemini #2: pip --user fails in venv)."""
    return getattr(sys, "real_prefix", None) is not None or sys.prefix != getattr(
        sys, "base_prefix", sys.prefix
    )


def _install_pre_commit_via_pip(version: str) -> tuple[int, str]:
    """`sys.executable -m pip install pre-commit==<version>` (with optional --user).

    Returns (returncode, stderr). Called only when the caller explicitly passes --allow-pip
    (3-auditor convergent critical: do not install supply-chain code by default).

    Post-impl dual-audit gemini #2 (major): inside a venv, pip --user fails with
    "Can not perform a --user install"; detect venv → omit --user.
    """
    cmd = [sys.executable, "-m", "pip", "install", "--no-input"]
    if not _is_in_virtualenv():
        cmd.append("--user")
    cmd.append(f"pre-commit=={version}")
    rc, _, err = _safe_run(cmd, timeout=TIMEOUT_PIP_S)
    return rc, err


# ===== Config write (idempotent) =====


def _read_template(aqg_root: Path) -> str:
    template = aqg_root / TEMPLATE_CONFIG_REL
    if not template.is_file():
        raise RuntimeError(f"AQG template missing: {template}")
    return template.read_text(encoding="utf-8")


def _atomic_write_text(path: Path, text: str) -> None:
    """Write text to path atomically via tempfile + os.replace.

    Post-impl dual-audit gpt-5.5 #4 + gemini #3 (2-auditor): write_text is not atomic;
    an interruption / race leaves a partial file. Use tempfile.mkstemp in the same dir +
    os.replace to guarantee rename atomicity. Refuse a symlink target to prevent
    follow-symlink attacks.
    """
    import tempfile
    if path.is_symlink():
        raise ValueError(f"refusing to write through symlink: {path}")
    fd, tmp = tempfile.mkstemp(
        prefix=".tmp-pre-commit-", suffix=".yaml", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _copy_config(
    target_top: Path, *, aqg_root: Path, force: bool = False
) -> tuple[int, Path, str]:
    """Write `.pre-commit-config.yaml` to target. Return (status, path, info).

    gpt-5.5 #7 + gemini #2 accepted (idempotent): an existing byte-identical file is
    treated as OK and continues (status EXIT_OK); differing content + no --force →
    status EXIT_FILE_EXISTS.
    Post-impl dual-audit gpt-5.5 #4 + gemini #3: write goes through _atomic_write_text
    to prevent corruption.
    """
    template_text = _read_template(aqg_root)
    target = target_top / ".pre-commit-config.yaml"
    if target.exists():
        # exists() alone is not enough: a directory or a non-UTF-8 file at this
        # path makes read_text() raise IsADirectoryError / UnicodeDecodeError out
        # of main() as an uncaught traceback (audit 48a01f42 gpt-f4 + gemini-f3;
        # workflow C4). Guard with is_file() + a decode try/except.
        if not target.is_file():
            return (
                EXIT_FILE_EXISTS,
                target,
                f"{target} exists but is not a regular file (directory / special); "
                f"resolve it manually",
            )
        try:
            existing: Optional[str] = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            if not force:
                return (
                    EXIT_FILE_EXISTS,
                    target,
                    f"{target} exists but is not readable as UTF-8 text "
                    f"({type(exc).__name__}); use --force to overwrite or resolve manually",
                )
            existing = None  # --force + undecodable → treat as differing, overwrite
        else:
            if existing == template_text:
                return EXIT_OK, target, "config already present (byte-identical)"
            if not force:
                return (
                    EXIT_FILE_EXISTS,
                    target,
                    f"{target} exists with different content; "
                    f"use --force to overwrite, --skip-config to keep yours, "
                    f"or merge manually (see README)",
                )
        # force overwrite (differing content, or undecodable existing file)
    _atomic_write_text(target, template_text)
    return EXIT_OK, target, "config written"


# ===== pre-commit install hooks (Gitleaks bootstrap) =====


def _run_pre_commit_install(target_top: Path) -> tuple[int, str]:
    """`pre-commit install --install-hooks` (gpt-5.5 #4 accepted).

    Bootstraps hook environments (including the Gitleaks golang hook that triggers a Go build).
    Missing Go → fail with a stderr hint; the caller warns and does not install it for the user.

    Post-impl dual-audit gpt-5.5 #2: use _resolve_pre_commit_cmd to get the argv prefix,
    supporting the pipx-installed binary fallback (when the sys.executable -m path is unavailable).
    """
    pc_cmd = _resolve_pre_commit_cmd()
    if pc_cmd is None:
        return 127, "pre-commit not available (neither python -m pre_commit nor 'pre-commit' on PATH)"
    rc, _, err = _safe_run(
        pc_cmd + ["install", "--install-hooks"],
        cwd=target_top,
        timeout=180,
    )
    return rc, err


# ===== CLI =====


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="install_pre_commit",
        description="Install AQG-recommended pre-commit hook into a target git repo.",
    )
    p.add_argument(
        "--target-repo",
        default=".",
        help="target git repo (default: cwd; resolved via git rev-parse --show-toplevel)",
    )
    p.add_argument(
        "--allow-pip",
        action="store_true",
        help=(
            "permit `python -m pip install --user pre-commit` if missing "
            "(default: NOT installed; print instructions and exit 4)"
        ),
    )
    p.add_argument(
        "--pre-commit-version",
        default=DEFAULT_PRE_COMMIT_VERSION,
        help=f"pin pre-commit version when installing via --allow-pip (default: {DEFAULT_PRE_COMMIT_VERSION})",
    )
    p.add_argument(
        "--no-install-hooks",
        action="store_true",
        help="copy config only; skip `pre-commit install --install-hooks`",
    )
    p.add_argument(
        "--config-only",
        action="store_true",
        help="alias for --no-install-hooks",
    )
    p.add_argument(
        "--skip-config",
        action="store_true",
        help=(
            "skip writing .pre-commit-config.yaml; only run hook install "
            "(use when target repo already has a custom config you want to keep). "
            "Post-impl dual-audit gpt-5.5 #3 + gemini #1 convergent."
        ),
    )
    p.add_argument(
        "--skip-pip",
        action="store_true",
        help="(deprecated alias for --ci-mode) assume pre-commit binary present",
    )
    p.add_argument(
        "--ci-mode",
        action="store_true",
        help="CI mode: assume pre-commit pre-installed (e.g., via image); skip pip even if --allow-pip",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing .pre-commit-config.yaml that differs from template",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="print actions without executing pip install / hook install / config write",
    )
    return p


def _print_install_instructions() -> None:
    """3-auditor convergent critical: do not pip install by default; missing → print guidance + exit 4."""
    print(
        "[install_pre_commit] pre-commit not found in current Python environment.\n"
        "                     Install one of:\n"
        "                       (recommended)  pipx install pre-commit\n"
        f"                       (alternative)  python3 -m pip install --user pre-commit=={DEFAULT_PRE_COMMIT_VERSION}\n"
        "                     Then re-run this installer.\n"
        "                     Or pass --allow-pip to let us install it for you (uses pip --user).",
        file=sys.stderr,
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    no_hooks = args.no_install_hooks or args.config_only
    ci_mode = args.ci_mode or args.skip_pip

    try:
        aqg_root = _find_aqg_root()
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    # === 1. Resolve target git toplevel (gpt-5.5 #3) ===
    target_input = Path(args.target_repo).expanduser().resolve()
    target_top = _resolve_git_toplevel(target_input)
    if target_top is None:
        print(
            f"ERROR: {target_input} is not inside a git working tree "
            f"(or git not available / bare repo)",
            file=sys.stderr,
        )
        return EXIT_USAGE

    if args.dry_run:
        print(f"[dry-run] would write .pre-commit-config.yaml to {target_top}", file=sys.stderr)
        if not no_hooks:
            print(f"[dry-run] would run pre_commit install --install-hooks in {target_top}", file=sys.stderr)
        return EXIT_OK

    # === 2. Detect pre-commit (PATH-safe via sys.executable -m pre_commit) ===
    if not _check_pre_commit_available():
        if not args.allow_pip or ci_mode:
            _print_install_instructions()
            return EXIT_PIP_FAIL
        # explicit --allow-pip: install via sys.executable -m pip
        print(
            f"[install_pre_commit] installing pre-commit=={args.pre_commit_version} via pip --user",
            file=sys.stderr,
        )
        rc, err = _install_pre_commit_via_pip(args.pre_commit_version)
        if rc != 0:
            print(
                f"[install_pre_commit] pip install failed (rc={rc}); "
                f"stderr (truncated): {err[:300]}",
                file=sys.stderr,
            )
            return EXIT_PIP_FAIL
        # re-check after install
        if not _check_pre_commit_available():
            print(
                "[install_pre_commit] pip install reported success but "
                "`python -m pre_commit --version` still fails; check Python env",
                file=sys.stderr,
            )
            return EXIT_PIP_FAIL

    # === 3. Copy config (idempotent byte-identical OK) — unless --skip-config ===
    if args.skip_config:
        print(
            "[install_pre_commit] --skip-config: keeping existing config; only running hook install",
            file=sys.stderr,
        )
    else:
        rc, config_path, info = _copy_config(target_top, aqg_root=aqg_root, force=args.force)
        print(f"[install_pre_commit] {info}: {config_path.name}", file=sys.stderr)
        if rc != EXIT_OK:
            return rc

    # === 4. Install hooks (bootstrap Gitleaks Go build) ===
    if no_hooks:
        print("[install_pre_commit] skipping `pre-commit install --install-hooks` per flag", file=sys.stderr)
        return EXIT_OK

    print(
        "[install_pre_commit] running `pre-commit install --install-hooks` "
        "(Gitleaks bootstrap may need Go toolchain — see README)",
        file=sys.stderr,
    )
    rc, err = _run_pre_commit_install(target_top)
    if rc != 0:
        print(
            f"[install_pre_commit] hook install failed (rc={rc}); "
            f"stderr (truncated): {err[:500]}\n"
            f"  Common cause: Gitleaks hook needs Go compiler. Install Go and re-run.",
            file=sys.stderr,
        )
        return EXIT_HOOK_INSTALL_FAIL

    print(f"[install_pre_commit] OK: pre-commit installed in {target_top}", file=sys.stderr)
    return EXIT_OK


# ===== Self-test =====


def self_test() -> int:
    """Sanity self-test (full coverage in tests/test_install_pre_commit.py)."""
    # AQG root resolution
    root = _find_aqg_root()
    assert (root / "VERSION").is_file()

    # Template exists + readable
    text = _read_template(root)
    assert "gitleaks" in text.lower(), "template should reference gitleaks"
    assert "AQG" in text or "aqg" in text.lower(), "template should mark AQG provenance"

    # _safe_run helper sets non-interactive env (post-impl dual-audit gemini #4:
    # use sys.executable -c so it does not depend on bash, runs on Alpine/Windows too)
    rc, out, _ = _safe_run(
        [sys.executable, "-c", "import os; print(os.environ.get('GIT_TERMINAL_PROMPT', 'unset'))"],
        timeout=5,
    )
    assert rc == 0
    assert out == "0", f"GIT_TERMINAL_PROMPT not set: {out!r}"

    # _safe_run handles missing command
    rc, out, err = _safe_run(["this_command_does_not_exist_xyz"], timeout=2)
    assert rc == 127

    print("OK: install_pre_commit self-test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    raise SystemExit(main())
