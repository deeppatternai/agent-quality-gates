#!/usr/bin/env python3
"""AQG self-synthetic chaos runner (Wave 2 #2, framework v2.1 §7 #2).

Implemented per the triple-audit (audit_id d3233f81) 13 accepted findings:
- Hermetic sandbox by default (3-auditor critical: HOME / XDG / AQG_* / PATH all
  isolated; minimal PATH /usr/bin:/bin; GIT_CEILING_DIRECTORIES prevents git from
  traversing into the real repo)
- Process group kill on timeout (3-auditor major: start_new_session + os.killpg)
- _vetted_copy: shutil.copytree(symlinks=True) + post-copy walk asserts no escape
- _safe_rmtree: onerror chmod retry + ignore_errors fallback
- _chaos_redact: replace real-checkout-path / real-HOME with placeholders; cap each
  stream at 4KB
- automatically prepend real-checkout-path + real-HOME to each scenario expect_no_leak

CLI:
    python3 scripts/aqg_chaos.py list
    python3 scripts/aqg_chaos.py run-all [--limit N] [--filter PAT]
    python3 scripts/aqg_chaos.py run <scenario_name>
    python3 scripts/aqg_chaos.py run-all --json

Exit codes: 0 all pass / 1 any fail / 2 usage / 3 setup fail (chaos itself broken)

No third-party dependencies, stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2
EXIT_SETUP_FAIL = 3

# Cap each stream at 4 KiB before reporting (privacy + size)
MAX_STREAM_BYTES = 4096
# Min PATH (POSIX defaults; scenarios needing git/gh add via inherit_env_vars)
HERMETIC_PATH = "/usr/bin:/bin"


# ===== AQG repo root =====


def _find_aqg_root() -> Path:
    p = Path(__file__).resolve().parent
    for _ in range(8):
        if (p / "VERSION").is_file() and (p / "scripts").is_dir():
            return p
        if p.parent == p:
            break
        p = p.parent
    raise RuntimeError(f"cannot find AQG repo root from {Path(__file__).resolve()}")


# ===== Vetted copy (gpt-5.5 #4 + o3 #6 accepted) =====


_COPY_SKIP_DIRS: frozenset[str] = frozenset({
    ".git",  # Don't copy real .git — chaos shouldn't operate on our git history
    ".github",  # Workflows not needed for chaos runs
    ".claude",  # Worktree internals
    ".venv",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
})


def _vetted_copy(src: Path, dst: Path) -> None:
    """Copy src tree to dst. Skip .git/.github/etc; preserve other symlinks;
    reject any that resolve outside src; reject hardlinks.

    Post-impl dual-audit gpt-5.5 #2: escape symlinks raise RuntimeError (not silent
    unlink) so runner can mark scenario error; gpt-5.5 #1 + gemini #5 (convergent):
    hardlink check (st_nlink > 1) added.
    """
    src_resolved = src.resolve()
    if dst.exists():
        raise RuntimeError(f"_vetted_copy: dst already exists: {dst}")

    # Pre-copy walk: reject hardlinks in src (post-impl gpt-5.5 #1 + gemini #5 convergent).
    # shutil.copytree turns hardlinks into independent regular files, so post-walk
    # st_nlink check on dst always shows 1; we MUST check src before copy.
    for root, dirs, files in os.walk(str(src_resolved), followlinks=False):
        # skip ignored subtrees to avoid false alerts in __pycache__ / .git etc
        dirs[:] = [d for d in dirs if d not in _COPY_SKIP_DIRS]
        for fname in files:
            full = os.path.join(root, fname)
            try:
                st = os.lstat(full)
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode) and st.st_nlink > 1:
                rel = os.path.relpath(full, str(src_resolved))
                raise RuntimeError(
                    f"_vetted_copy: hardlink detected in src at {rel} "
                    f"(st_nlink={st.st_nlink}); chaos copy must be regular files only"
                )

    def ignore(_dir: str, names: list[str]) -> list[str]:
        return [n for n in names if n in _COPY_SKIP_DIRS]

    shutil.copytree(
        str(src_resolved),
        str(dst),
        symlinks=True,
        dirs_exist_ok=False,
        ignore=ignore,
    )

    # Post-copy walk: validate symlinks + hardlinks
    dst_resolved = dst.resolve()
    for path in dst.rglob("*"):
        if path.is_symlink():
            try:
                target = path.resolve(strict=False)
            except (OSError, RuntimeError):
                target = None
            if target is None:
                # broken symlink to nowhere — safe to keep, target unresolvable
                continue
            try:
                target.relative_to(dst_resolved)
            except ValueError:
                # Post-impl gpt-5.5 #2: escape → raise so runner reports error
                # (silent unlink hides mutated copy from misclassified scenario fails)
                raise RuntimeError(
                    f"_vetted_copy: symlink {path.name} escapes dst (target outside chaos_root)"
                )


# ===== Safe rmtree (gemini #3 + o3 #5 accepted) =====


def _safe_chmod_no_follow(target: str, mode: int) -> None:
    """chmod that NEVER follows symlinks (post-impl gemini #1 CRITICAL).

    Without this, chaos teardown can mutate real outside files (e.g.,
    ~/.bashrc) if a scenario inadvertently creates a symlink to them.
    """
    if os.path.islink(target):
        return  # NEVER chmod through symlinks
    try:
        os.chmod(target, mode)
    except OSError:
        pass


def _safe_rmtree(path: Path) -> None:
    """rmtree with chmod retry on perm error, then ignore_errors fallback.

    Pre-pass: walk and chmod every dir/file to 0o700 so subsequent rmtree
    has full access. NEVER chmod through symlinks (post-impl gemini #1 critical).
    """
    if not path.exists():
        return

    # Pre-pass chmod everything writable (skip symlinks)
    try:
        for root, dirs, files in os.walk(str(path), topdown=False):
            for name in files + dirs:
                full = os.path.join(root, name)
                _safe_chmod_no_follow(full, 0o700)
        _safe_chmod_no_follow(str(path), 0o700)
    except OSError:
        pass

    # Post-impl gemini #3 (major): onerror must not swallow OSError, or the outer
    # ignore_errors won't trigger. Raise here so shutil sees the failure and the
    # outer fallback runs.
    def onerror(func, target, exc_info):
        # Try chmod target + parent (skip symlinks)
        _safe_chmod_no_follow(target, 0o700)
        if not os.path.islink(target):
            _safe_chmod_no_follow(os.path.dirname(target), 0o700)
        try:
            func(target)
        except OSError:
            raise  # let shutil propagate so outer fallback runs

    try:
        shutil.rmtree(str(path), onerror=onerror)
    except OSError:
        # Final fallback: best-effort with ignore
        shutil.rmtree(str(path), ignore_errors=True)


# ===== Hermetic env (3-auditor CRITICAL accepted) =====


def _build_hermetic_env(
    chaos_root: Path,
    *,
    inherit: tuple[str, ...] = (),
    extra: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Construct env with all global paths isolated to chaos_root.

    Default inherit: nothing. Scenarios can opt-in specific vars via inherit list.
    extra is merged last (scenario can override hermetic defaults if needed,
    but should rarely do so).
    """
    fake_home = chaos_root / "home"
    fake_xdg_data = chaos_root / "xdg-data"
    fake_xdg_config = chaos_root / "xdg-config"
    fake_xdg_cache = chaos_root / "xdg-cache"
    for d in (fake_home, fake_xdg_data, fake_xdg_config, fake_xdg_cache):
        d.mkdir(parents=True, exist_ok=True, mode=0o700)

    base: dict[str, str] = {
        "HOME": str(fake_home),
        "XDG_DATA_HOME": str(fake_xdg_data),
        "XDG_CONFIG_HOME": str(fake_xdg_config),
        "XDG_CACHE_HOME": str(fake_xdg_cache),
        "AQG_ROOT": str(chaos_root),
        "AQG_METRICS_PATH": str(chaos_root / "chaos-ledger.jsonl"),
        # GIT_CEILING_DIRECTORIES (gemini #4 accepted): prevent git traversal to real repo
        "GIT_CEILING_DIRECTORIES": str(chaos_root.parent),
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "PATH": HERMETIC_PATH,
        "TERM": "dumb",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    # Inherit specific vars from real env (e.g., scenario needs gh on PATH)
    real_env = os.environ
    for var in inherit:
        if var in real_env:
            base[var] = real_env[var]
    if extra:
        base.update(extra)
    return base


# ===== Output redaction (gpt-5.5 #7 + o3 #7 accepted) =====


def _build_no_leak_set(scenario_no_leak: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Auto-prepend real checkout path + real HOME to each scenario's no-leak set.

    Post-impl gemini #2 (major): sort by length desc so longer paths replaced
    first (e.g., HOME=/Users/joe + AQG=/Users/joe/proj → must replace AQG first
    or HOME replacement makes AQG no longer match).
    """
    leak_set: list[str] = []
    # DR-19: Path.home() raises RuntimeError when HOME is unset AND the uid has
    # no passwd entry (minimal containers / CI). Don't let leak-set setup abort
    # the whole redaction path; just skip the home token if it can't resolve.
    try:
        real_home = str(Path.home())
    except (RuntimeError, OSError):
        real_home = ""
    if real_home and real_home != "/":
        leak_set.append(real_home)
    try:
        real_root = str(_find_aqg_root())
        if real_root and real_root != "/" and real_root not in leak_set:
            leak_set.append(real_root)
    except RuntimeError:
        pass
    leak_set.extend(scenario_no_leak)
    # Sort by length desc; longer paths replaced first
    return tuple(sorted({p for p in leak_set if p}, key=len, reverse=True))


def _chaos_redact(text: str, *, real_paths: tuple[str, ...]) -> str:
    """Replace real paths with placeholders; cap to MAX_STREAM_BYTES.

    Caller should pass real_paths from _build_no_leak_set (already sorted desc).
    """
    out = text
    for p in real_paths:
        if p:
            out = out.replace(p, f"<REDACTED-PATH:{len(p)}>")
    # DR-17: the cap is measured in BYTES, so it must also slice in bytes.
    # Slicing out[:MAX_STREAM_BYTES] counts CHARACTERS, so multibyte (e.g. CJK)
    # output blew past the 4 KiB cap (5000 CJK chars = 15 KiB → 12 KiB "capped").
    # errors="ignore" drops a partial trailing multibyte char so the result is
    # always valid UTF-8 within the cap.
    encoded = out.encode("utf-8")
    if len(encoded) > MAX_STREAM_BYTES:
        out = encoded[:MAX_STREAM_BYTES].decode("utf-8", errors="ignore")
        out = out + "\n... (truncated)"
    return out


def _check_no_leak(text: str, *, real_paths: tuple[str, ...]) -> list[str]:
    """Return list of real-path strings found in text (empty = clean)."""
    found = []
    for p in real_paths:
        if p and p in text:
            found.append(p)
    return found


# ===== Scenario result =====


@dataclass(frozen=True)
class ScenarioResult:
    name: str
    status: str  # "pass" / "fail" / "error"
    exit_code: Optional[int]
    duration_ms: int
    failure_reasons: tuple[str, ...]  # human-readable; empty if pass
    stdout_redacted: str
    stderr_redacted: str


# ===== Runner core =====


def _run_scenario(scenario, aqg_root: Path) -> ScenarioResult:
    import time
    from _chaos_scenarios import write_introspector

    start_ns = time.monotonic_ns()
    chaos_root: Optional[Path] = None
    failure_reasons: list[str] = []
    exit_code: Optional[int] = None
    stdout_redacted = ""
    stderr_redacted = ""

    try:
        # 1. Create isolated tmpdir
        chaos_root_str = tempfile.mkdtemp(prefix="aqg-chaos-")
        chaos_root = Path(chaos_root_str).resolve()

        # 2. Copy AQG (vetted)
        chaos_aqg = chaos_root / "aqg"
        try:
            _vetted_copy(aqg_root, chaos_aqg)
        except (RuntimeError, OSError) as exc:
            failure_reasons.append(f"setup: copy AQG failed: {exc}")
            return ScenarioResult(
                name=scenario.name,
                status="error",
                exit_code=None,
                duration_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
                failure_reasons=tuple(failure_reasons),
                stdout_redacted="",
                stderr_redacted="",
            )

        # 3. Write introspector for scenario 1
        write_introspector(chaos_aqg)

        # 4. Run scenario setup
        if scenario.setup_fn is not None:
            try:
                scenario.setup_fn(chaos_aqg)
            except Exception as exc:  # noqa: BLE001
                failure_reasons.append(f"setup_fn: {type(exc).__name__}: {exc}")
                return ScenarioResult(
                    name=scenario.name,
                    status="error",
                    exit_code=None,
                    duration_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
                    failure_reasons=tuple(failure_reasons),
                    stdout_redacted="",
                    stderr_redacted="",
                )

        # 5. Build hermetic env
        env = _build_hermetic_env(
            chaos_aqg,  # AQG_ROOT points to chaos_aqg
            inherit=scenario.inherit_env_vars,
            extra=scenario.extra_env,
        )

        # 6. Build invocation
        target_path = chaos_aqg / scenario.target
        invoke_cwd = chaos_aqg / scenario.invoke_cwd if scenario.invoke_cwd else chaos_aqg
        cmd = [sys.executable, str(target_path), *scenario.invoke_argv]

        # 7. Run subprocess in new session for grandchild kill
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(invoke_cwd),
                env=env,
                stdin=subprocess.PIPE if scenario.stdin_bytes is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,  # new process group for killpg
            )
        except (FileNotFoundError, OSError) as exc:
            failure_reasons.append(f"subprocess.Popen failed: {exc}")
            return ScenarioResult(
                name=scenario.name,
                status="error",
                exit_code=None,
                duration_ms=int((time.monotonic_ns() - start_ns) / 1_000_000),
                failure_reasons=tuple(failure_reasons),
                stdout_redacted="",
                stderr_redacted="",
            )

        try:
            stdout_b, stderr_b = proc.communicate(
                input=scenario.stdin_bytes,
                timeout=scenario.timeout_seconds,
            )
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            # Kill entire process group (prevent grandchild leak, 3-auditor major)
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
            try:
                stdout_b, stderr_b = proc.communicate(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                stdout_b, stderr_b = b"", b""
            failure_reasons.append(f"timeout after {scenario.timeout_seconds}s; killed process group")
            exit_code = None
        except OSError as exc:
            failure_reasons.append(f"communicate failed: {exc}")
            stdout_b, stderr_b = b"", b""
            exit_code = None

        # 8. Decode + redact + check no-leak (incl. scenario.expect_no_leak; post-impl convergent)
        stdout_text = (stdout_b or b"").decode("utf-8", errors="replace")
        stderr_text = (stderr_b or b"").decode("utf-8", errors="replace")
        leak_set = _build_no_leak_set(scenario.expect_no_leak)  # real HOME + AQG + scenario tokens
        leaks = _check_no_leak(stdout_text + stderr_text, real_paths=leak_set)
        if leaks:
            failure_reasons.append(f"no-leak violation: {len(leaks)} real-path(s) found in output")
        stdout_redacted = _chaos_redact(stdout_text, real_paths=leak_set)
        stderr_redacted = _chaos_redact(stderr_text, real_paths=leak_set)

        # 9. Check exit code
        if exit_code is not None and exit_code not in scenario.expect_exit_codes:
            failure_reasons.append(
                f"exit_code={exit_code}, expected one of {sorted(scenario.expect_exit_codes)}"
            )

        # 10. Check stdout/stderr contains
        for needle in scenario.expect_stdout_contains:
            if needle not in stdout_text:
                failure_reasons.append(f"stdout missing expected substring: {needle!r}")
        for needle in scenario.expect_stderr_contains:
            if needle not in stderr_text:
                failure_reasons.append(f"stderr missing expected substring: {needle!r}")

        # 11. Run teardown (best-effort)
        if scenario.teardown_fn is not None:
            try:
                scenario.teardown_fn(chaos_aqg)
            except Exception:  # noqa: BLE001
                pass

    finally:
        # 12. Cleanup chaos_root (always)
        if chaos_root is not None:
            _safe_rmtree(chaos_root)

    duration_ms = int((time.monotonic_ns() - start_ns) / 1_000_000)
    status = "pass" if not failure_reasons else "fail"
    return ScenarioResult(
        name=scenario.name,
        status=status,
        exit_code=exit_code,
        duration_ms=duration_ms,
        failure_reasons=tuple(failure_reasons),
        stdout_redacted=stdout_redacted,
        stderr_redacted=stderr_redacted,
    )


# ===== CLI =====


def cmd_list(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _chaos_scenarios import list_scenarios
    for s in list_scenarios():
        print(f"{s.name}: {s.description}")
    return EXIT_OK


def cmd_run_all(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _chaos_scenarios import list_scenarios
    aqg_root = _find_aqg_root()
    scenarios = list(list_scenarios())
    if args.filter:
        scenarios = [s for s in scenarios if args.filter in s.name]
    if args.limit and args.limit > 0:
        scenarios = scenarios[: args.limit]
    if not scenarios:
        print(f"[aqg_chaos] no scenarios match filter={args.filter!r}", file=sys.stderr)
        return EXIT_USAGE

    results: list[ScenarioResult] = []
    for s in scenarios:
        print(f"[aqg_chaos] running {s.name}...", file=sys.stderr)
        r = _run_scenario(s, aqg_root)
        results.append(r)
        marker = {"pass": "✓", "fail": "✗", "error": "!"}.get(r.status, "?")
        print(f"  [{marker}] {r.name} ({r.duration_ms}ms) exit={r.exit_code}", file=sys.stderr)
        if r.failure_reasons:
            for reason in r.failure_reasons:
                print(f"      - {reason}", file=sys.stderr)

    if args.json:
        payload = {
            "scenarios": [
                {
                    "name": r.name,
                    "status": r.status,
                    "exit_code": r.exit_code,
                    "duration_ms": r.duration_ms,
                    "failure_reasons": list(r.failure_reasons),
                    "stdout_redacted": r.stdout_redacted,
                    "stderr_redacted": r.stderr_redacted,
                }
                for r in results
            ],
            "summary": {
                "total": len(results),
                "pass": sum(1 for r in results if r.status == "pass"),
                "fail": sum(1 for r in results if r.status == "fail"),
                "error": sum(1 for r in results if r.status == "error"),
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))

    setup_errors = sum(1 for r in results if r.status == "error")
    fails = sum(1 for r in results if r.status == "fail")
    print(
        f"[aqg_chaos] summary: {len(results) - setup_errors - fails}/{len(results)} pass "
        f"(fails={fails}, setup_errors={setup_errors})",
        file=sys.stderr,
    )
    # Post-impl gpt-5.5 #4 (major): distinguish setup errors (chaos infra broken)
    # from scenario fails (chaos detected real regression).
    if setup_errors > 0:
        return EXIT_SETUP_FAIL
    return EXIT_OK if fails == 0 else EXIT_FAIL


def cmd_run(args) -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from _chaos_scenarios import get_scenario
    aqg_root = _find_aqg_root()
    scenario = get_scenario(args.name)
    if scenario is None:
        print(f"unknown scenario: {args.name}", file=sys.stderr)
        return EXIT_USAGE
    r = _run_scenario(scenario, aqg_root)
    marker = {"pass": "✓", "fail": "✗", "error": "!"}.get(r.status, "?")
    print(f"[{marker}] {r.name} ({r.duration_ms}ms) exit={r.exit_code}", file=sys.stderr)
    for reason in r.failure_reasons:
        print(f"  - {reason}", file=sys.stderr)
    if args.verbose:
        if r.stdout_redacted:
            print("--- stdout (redacted) ---", file=sys.stderr)
            print(r.stdout_redacted, file=sys.stderr)
        if r.stderr_redacted:
            print("--- stderr (redacted) ---", file=sys.stderr)
            print(r.stderr_redacted, file=sys.stderr)
    # Post-impl gpt-5.5 #4: distinguish setup error from scenario fail
    if r.status == "error":
        return EXIT_SETUP_FAIL
    return EXIT_OK if r.status == "pass" else EXIT_FAIL


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="aqg_chaos",
        description="AQG self-synthetic chaos runner (release-time regression guard)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="list available scenarios")
    sp = sub.add_parser("run-all", help="run all scenarios")
    sp.add_argument("--filter", help="only run scenarios with this substring in name")
    sp.add_argument("--limit", type=int, default=0, help="run only first N (0=all)")
    sp.add_argument("--json", action="store_true", help="emit JSON summary")
    sp = sub.add_parser("run", help="run a single scenario by name")
    sp.add_argument("name")
    sp.add_argument("--verbose", "-v", action="store_true", help="print redacted stdout/stderr")
    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    handlers = {"list": cmd_list, "run-all": cmd_run_all, "run": cmd_run}
    return handlers[args.cmd](args)


def self_test() -> int:
    """Quick sanity (full coverage in tests/test_aqg_chaos.py)."""
    # _find_aqg_root works
    root = _find_aqg_root()
    assert (root / "VERSION").is_file()
    # _build_hermetic_env produces all required keys
    with tempfile.TemporaryDirectory(prefix="aqg-chaos-self-") as td:
        env = _build_hermetic_env(Path(td))
        assert env["AQG_ROOT"] == td
        assert env["PATH"] == HERMETIC_PATH
        assert "GIT_CEILING_DIRECTORIES" in env
    # _chaos_redact replaces paths
    redacted = _chaos_redact(f"hello {Path.home()} world", real_paths=(str(Path.home()),))
    assert str(Path.home()) not in redacted
    print("OK: aqg_chaos self-test passed")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    raise SystemExit(main())
