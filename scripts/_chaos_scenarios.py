#!/usr/bin/env python3
"""AQG self-synthetic chaos scenarios (Wave 2 #2, framework v2.1 §7 #2).

Per triple-audit (audit_id d3233f81), implemented from 13 accepted findings:
- ChaosScenario dataclass: stdin field + expect_exit_codes frozenset (gpt-5.5 #5)
- 5 scenarios v0 — chosen for ROI vs implementation cost
- doctor_python_below_min DROPPED (rejected, not implementable as shell chaos)

Scenarios protect against zhoupeng-class cross-env / fs / shell regression.
Run via `python3 scripts/aqg_chaos.py run-all` before release.

No third-party dependencies; stdlib only.
"""

from __future__ import annotations

import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional


@dataclass(frozen=True)
class ChaosScenario:
    """A single synthetic chaos scenario."""
    name: str  # snake_case slug, unique across all scenarios
    description: str  # one-line human readable
    target: str  # AQG script being chaos'd (relative path, e.g. "scripts/aqg_doctor.py")
    invoke_argv: tuple[str, ...]  # argv after the script
    invoke_cwd: str  # relative to chaos_root; "" means chaos_root itself
    expect_exit_codes: frozenset[int]  # accepted exit codes (one or more)
    expect_stdout_contains: tuple[str, ...] = ()  # all must appear if set
    expect_stderr_contains: tuple[str, ...] = ()  # all must appear if set
    timeout_seconds: int = 30
    stdin_bytes: Optional[bytes] = None  # stdin payload for the subprocess
    inherit_env_vars: tuple[str, ...] = ()  # env vars to inherit from real env
    extra_env: dict[str, str] = field(default_factory=dict)  # extra env (merged after hermetic defaults)
    setup_fn: Optional[Callable[[Path], None]] = None  # mutate chaos_root in place
    teardown_fn: Optional[Callable[[Path], None]] = None  # best-effort restore
    expect_no_leak: tuple[str, ...] = ()  # scenario-specific no-leak tokens (post-impl convergent)


# ===== Setup helpers (used by scenarios) =====


def _setup_doctor_install_command(chaos_root: Path) -> None:
    """No-op — scenario reads README directly via target."""
    pass


def _setup_corrupt_readme_python3_prefix(chaos_root: Path) -> None:
    """Strip python3 prefix from README install command (negative case for testing).

    Used by inverse test, NOT by scenario — this is for unit test only.
    """
    readme = chaos_root / "README.md"
    if not readme.is_file():
        return
    text = readme.read_text(encoding="utf-8")
    text = text.replace("python3 \"$repo/scripts/aqg_doctor.py\"", "\"$repo/scripts/aqg_doctor.py\"")
    readme.write_text(text, encoding="utf-8")


def _setup_metrics_ledger_perm_too_open(chaos_root: Path) -> None:
    """Pre-create the chaos-local ledger file with 0o644 permissions."""
    ledger = chaos_root / "chaos-ledger.jsonl"
    ledger.write_text("")
    os.chmod(ledger, 0o644)


def _setup_install_pre_commit_non_git(chaos_root: Path) -> None:
    """Create non-git target directory inside chaos_root."""
    (chaos_root / "non_git_target").mkdir(parents=True, exist_ok=True)


# ===== Scenario library (v0: 5 scenarios) =====


SCENARIOS: tuple[ChaosScenario, ...] = (
    # 1. doctor_install_command_uses_python3_prefix
    # Replaces broken doctor_no_exec_bit (3-auditor major: invoking via python3 doesn't reproduce zhoupeng).
    # Verifies README install command explicitly invokes via python3 — protecting against
    # regression to direct-exec which depends on +x bit (bugfix 0.1.6).
    ChaosScenario(
        name="doctor_install_command_uses_python3_prefix",
        description="README install command must invoke aqg_doctor.py via python3 prefix (regression guard for 0.1.6)",
        target="scripts/_chaos_introspect.py",  # introspector script created on-the-fly
        invoke_argv=("--check", "readme-install-uses-python3"),
        invoke_cwd="",
        expect_exit_codes=frozenset({0}),
        expect_stdout_contains=("OK",),
        timeout_seconds=10,
    ),

    # 2. doctor_aqg_root_unset
    # Verifies graceful failure when AQG_ROOT is empty.
    ChaosScenario(
        name="doctor_aqg_root_unset",
        description="doctor with AQG_ROOT empty should exit gracefully (not hang) with clear error",
        target="scripts/aqg_doctor.py",
        invoke_argv=("--no-cli", "--json"),
        invoke_cwd="",
        # doctor with empty AQG_ROOT produces FAIL CheckResult on env_AQG_ROOT (status=fail),
        # so script exit code is 1 (any FAIL). Post-impl gpt-5.5 #5: strict exit 1 only.
        # JSON output goes to stdout; verify it mentions AQG_ROOT (lowercase aqg_root)
        # plus the AQG_ROOT env reference.
        expect_exit_codes=frozenset({1}),
        expect_stdout_contains=("aqg_root", "AQG_ROOT"),
        timeout_seconds=15,
        extra_env={"AQG_ROOT": ""},
    ),

    # 3. wip_save_corrupt_stdin
    # Verifies wip_save handles invalid stdin JSON gracefully.
    ChaosScenario(
        name="wip_save_corrupt_stdin",
        description="wip_save with corrupt stdin JSON should exit 0 (graceful, no raise)",
        target="scripts/wip_save.py",
        invoke_argv=(),
        invoke_cwd="",
        stdin_bytes=b"{not valid json at all",
        expect_exit_codes=frozenset({0}),
        expect_stderr_contains=("JSON",),
        timeout_seconds=10,
    ),

    # 4. metrics_record_default_off_no_ledger_write
    # Verifies opt-in default — record without flag/env must NOT create ledger.
    ChaosScenario(
        name="metrics_record_default_off_no_ledger_write",
        description="metrics record without opt-in must NOT create ledger file (Q11 frozen)",
        target="scripts/aqg_metrics.py",
        invoke_argv=("record", "--tool", "audit", "--result", "pass"),
        invoke_cwd="",
        expect_exit_codes=frozenset({0}),
        expect_stderr_contains=("not enabled",),
        timeout_seconds=10,
        # Hermetic env auto-sets AQG_METRICS_PATH inside chaos_root.
        # No AQG_METRICS env, no --record-metrics flag → must no-op.
    ),

    # 5. install_pre_commit_non_git_target
    # Verifies installer rejects non-git target without writing config.
    ChaosScenario(
        name="install_pre_commit_non_git_target",
        description="install_pre_commit on non-git target must exit 2 (USAGE) not write config",
        target="scripts/install_pre_commit.py",
        invoke_argv=("--target-repo", "non_git_target", "--no-install-hooks"),
        invoke_cwd="",
        expect_exit_codes=frozenset({2}),
        expect_stderr_contains=("not inside a git",),
        timeout_seconds=10,
        setup_fn=_setup_install_pre_commit_non_git,
    ),
)


def get_scenario(name: str) -> Optional[ChaosScenario]:
    for s in SCENARIOS:
        if s.name == name:
            return s
    return None


def list_scenarios() -> tuple[ChaosScenario, ...]:
    return SCENARIOS


# ===== Inline introspector (used by scenario 1) =====
# Written to chaos_root by the runner before scenario 1 invokes it.
# Lives here so we can keep it in one source-of-truth file for the chaos test pack.

INTROSPECTOR_SOURCE = '''#!/usr/bin/env python3
"""Chaos introspector — runs simple invariant checks on the chaos_root copy of AQG.

Usage:
    python3 scripts/_chaos_introspect.py --check <name>

Checks:
    readme-install-uses-python3 — assert README install command invokes
        aqg_doctor.py via python3 prefix, not raw .py path (regression guard for 0.1.6).
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_FAIL = 1
EXIT_USAGE = 2


def check_readme_install_uses_python3() -> int:
    repo = Path(__file__).resolve().parent.parent
    readme = repo / "README.md"
    if not readme.is_file():
        print(f"FAIL README.md not found at {readme}", file=sys.stderr)
        return EXIT_FAIL
    text = readme.read_text(encoding="utf-8")
    # Find each occurrence of $repo/scripts/aqg_doctor.py and assert preceded by `python3 `
    pattern = re.compile(r'(\\S*)\\$repo/scripts/aqg_doctor\\.py')
    found = False
    for m in pattern.finditer(text):
        found = True
        prefix = m.group(1)
        # Look backwards for python3 in the same logical command — find last "python3 " before the match
        before = text[:m.start()]
        # Heuristic: the python3 prefix should appear in the same line or just preceding
        # We require either (a) prefix=="python3 " or (b) line containing match starts with `python3`
        last_newline = before.rfind("\\n")
        line_so_far = text[last_newline + 1: m.start()]
        if "python3" not in line_so_far:
            print(f"FAIL invocation of $repo/scripts/aqg_doctor.py without python3 prefix at offset {m.start()}", file=sys.stderr)
            print(f"  context: {line_so_far[:80]!r}", file=sys.stderr)
            return EXIT_FAIL
    if not found:
        # README didn't reference aqg_doctor.py at all — that's also OK in principle
        # but we expect to find it given the install instructions
        print("WARN $repo/scripts/aqg_doctor.py not referenced in README; install instructions may have changed", file=sys.stderr)
    print("OK readme install command uses python3 prefix")
    return EXIT_OK


CHECKS = {
    "readme-install-uses-python3": check_readme_install_uses_python3,
}


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "--check":
        print(f"usage: _chaos_introspect.py --check <name>; got {argv}", file=sys.stderr)
        return EXIT_USAGE
    name = argv[1]
    fn = CHECKS.get(name)
    if fn is None:
        print(f"unknown check: {name}; available: {sorted(CHECKS)}", file=sys.stderr)
        return EXIT_USAGE
    return fn()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
'''


def write_introspector(chaos_root: Path) -> Path:
    """Write the introspector script into chaos_root/scripts/."""
    target = chaos_root / "scripts" / "_chaos_introspect.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(INTROSPECTOR_SOURCE, encoding="utf-8")
    target.chmod(0o755)
    return target


def self_test() -> int:
    # All scenarios have unique names + valid target prefixes
    names = [s.name for s in SCENARIOS]
    assert len(names) == len(set(names)), f"duplicate scenario names: {names}"
    for s in SCENARIOS:
        assert s.target.startswith("scripts/"), f"target should be relative path: {s.target}"
        assert s.timeout_seconds >= 1
        assert isinstance(s.expect_exit_codes, frozenset)
        assert s.expect_exit_codes, f"{s.name}: expect_exit_codes must be non-empty"
    # introspector source compiles
    compile(INTROSPECTOR_SOURCE, "<introspector>", "exec")
    print("OK: _chaos_scenarios self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_test())
