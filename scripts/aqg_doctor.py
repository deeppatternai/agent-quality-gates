#!/usr/bin/env python3
"""AQG Doctor — one-shot diagnostic for a local installation.

Why this is needed: once AQG is installed across several machines (dev, CI,
co-worker sync), the common breakage is "AQG_ROOT unset / symlink broken /
Codex and Claude Code skills out of sync". The doctor gathers these common
checks in one place and prints an actionable fix on failure.

Exit codes:
    0 — all checks PASS (or only WARN)
    1 — at least one FAIL
    2 — usage error

Usage:
    python3 scripts/aqg_doctor.py            # full check
    python3 scripts/aqg_doctor.py --json     # JSON output (machine-readable)
    python3 scripts/aqg_doctor.py --no-cli   # skip git/gh CLI probing

No third-party dependencies — stdlib only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

try:
    from aqg_skill_install import classify_install
except ModuleNotFoundError:  # package import, e.g. `python -m scripts.aqg_doctor`
    from scripts.aqg_skill_install import classify_install


EXIT_USAGE = 2

# Kept in sync with install.sh / Codex install.sh
# Issue #114 fix: aqg-phase-transition (Sprint 11, PR #103) and aqg-multi-review
# (Sprint 11.5, PR #104) added to source `skills/` were not registered in either
# install array — surfaced when CLAUDE.md / README claim "10 skills" but installer
# only links 8. Claude wrappers under agent-packs/claude-code/skills/ added in
# same fix PR.
CLAUDE_SKILL_NAMES = (
    "aqg-startup-preflight",
    "aqg-code-construction",
    "aqg-systematic-debugging",
    "aqg-audit-adjudication",
    "aqg-evidence-closeout",
    "aqg-skill-validator",
    "aqg-security-review",
    "aqg-automation-audit",
    "aqg-phase-transition",
    "aqg-multi-review",
    "aqg-test-quality-review",
    "aqg-re-anchor",
    "aqg-project-status",
    "aqg-memory-hygiene",
    "aqg-session-handoff",
    "aqg-decision-capture",
)

CODEX_SKILL_NAMES = (
    "aqg-startup-preflight",
    "aqg-code-construction",
    "aqg-systematic-debugging",
    "aqg-audit-adjudication",
    "aqg-evidence-closeout",
    "aqg-skill-validator",
    "aqg-security-review",
    "aqg-automation-audit",
    "aqg-phase-transition",
    "aqg-multi-review",
    "aqg-test-quality-review",
    "aqg-re-anchor",
    "aqg-project-status",
    "aqg-memory-hygiene",
    "aqg-session-handoff",
    "aqg-decision-capture",
)

CRITICAL_SCRIPTS = (
    "scripts/run_quality_gates.py",
    "scripts/validate_audit_adjudication.py",
    "scripts/check_dirty_or_gone_worktree.py",
    "scripts/check_evidence_closeout.py",
    "scripts/_secret_patterns.py",
    "scripts/_surface_redaction.py",  # Wave 1-0: surface fingerprint redaction guardrail
    "scripts/validate_handoff_manifest.py",  # Wave 1-0: handoff manifest v0 validator
    "scripts/_session_fingerprint.py",  # Wave 1: doctor --mode session collector
    "scripts/_wip_redaction.py",  # Wave 1 #7: WIP snapshot allowlist guard
    "scripts/wip_save.py",  # Wave 1 #7: PreCompact hook handler
    "scripts/wip_recover.py",  # Wave 1 #7: SessionStart hook handler
    "scripts/wip_checkpoint.py",  # wip-checkpoint: true code snapshot to refs/aqg-wip (Stop/PreCompact save + SessionStart recover)
    "scripts/_bugfix_redaction.py",  # Wave 1 #6: bugfix record allowlist guard
    "scripts/bugfix_record.py",  # Wave 1 #6: bugfix record CLI + writer
    "scripts/install_pre_commit.py",  # Wave 1 #1: pre-commit hook installer
    "scripts/_metrics_redaction.py",  # Wave 2 #3: metrics ledger record guard
    "scripts/aqg_metrics.py",  # Wave 2 #3: metrics ledger CLI
    "scripts/_chaos_scenarios.py",  # Wave 2 #2: chaos scenario library
    "scripts/aqg_chaos.py",  # Wave 2 #2: chaos runner
    "scripts/_simulation_redaction.py",  # Wave 2 #4: simulation manifest schema
    "scripts/validate_simulation_manifest.py",  # Wave 2 #4: simulation CLI validator
    "scripts/_orchestration_redaction.py",  # Wave 2 #5: orchestration manifest schema
    "scripts/validate_orchestration_manifest.py",  # Wave 2 #5: orchestration CLI validator
    "scripts/_aqg_context.sh",  # Wave 3 PR-3b1: shared AQG_ROOT resolver helper
    "scripts/_incident_redaction.py",  # Wave 3 Layer 3: incident record schema + redaction
    "scripts/aqg_incident_index.py",  # Wave 3 Layer 3: incident record CLI + INDEX.md generator
    "scripts/_skill_template_schema.py",  # Wave 4+ machine-template B-1: sidecar schema
    "scripts/aqg_skill_validator.py",  # Wave 4+ machine-template B-1: skill validator CLI
    "scripts/aqg_skill_gen.py",  # Wave 4+ machine-template B-2: skill skeleton generator
    "scripts/install_aqg_codex_hooks.py",  # Codex lifecycle hook installer
    "scripts/run_aqg_codex_hook.py",  # Codex-to-AQG hook schema adapter
    "scripts/aqg_skill_install.py",  # Cross-platform Codex skill link/copy installer
)

# Verified to run on 3.9: every AQG script uses `from __future__ import annotations`
# so PEP 585/604 type annotations are evaluated lazily. 3.8 and earlier do not
# support the PEP 604 `X | None` form even with `from __future__ import annotations`.
MIN_PYTHON = (3, 9)

# Character cap for the single-line sentinel files doctor reads (VERSION / .aqg-root).
# Normal content is a version number / single path (far below this cap); the bounded
# read prevents a corrupted / tampered oversized file from triggering MemoryError and
# crashing the diagnostic tool itself (dual-audit b1a90943: MemoryError is not an
# OSError subclass, so a full read_text() would bypass the except guard).
_MAX_PROBE_CHARS = 8192


@dataclass(frozen=True)
class CheckResult:
    """Result of a single check.

    status: PASS / WARN / FAIL
    name: check identifier (short)
    detail: one-line diagnostic detail
    fix: optional fix hint (only meaningful for WARN/FAIL)
    """

    status: str
    name: str
    detail: str
    fix: str | None = None


def run_cli(cmd: list[str], timeout: int = 10) -> tuple[int, str]:
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    except subprocess.TimeoutExpired:
        return 124, f"{cmd[0]}: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - doctor must not crash on cli quirks
        return 1, f"{type(exc).__name__}: {exc}"


def _python_version_string(version: tuple[int, ...]) -> str:
    """Format a Python version tuple as a short string, e.g. (3, 9) → '3.9'."""
    return ".".join(str(x) for x in version)


def check_python_version() -> CheckResult:
    actual = sys.version_info[:3]
    actual_str = _python_version_string(actual)
    min_str = _python_version_string(MIN_PYTHON)
    if actual >= MIN_PYTHON:
        return CheckResult(
            "PASS",
            "python_version",
            f"Python {actual_str} (>= {min_str})",
        )
    # 0.1.4 finding #9: the fix hint references MIN_PYTHON via f-string so a hardcoded
    # version number does not drift out of sync when MIN_PYTHON is bumped.
    return CheckResult(
        "FAIL",
        "python_version",
        f"Python {actual_str} < required {min_str}",
        fix=f"install Python {min_str}+ (AQG uses modern type hints with postponed annotations)",
    )


# Minimum PyYAML version declared in the repo-root requirements.txt; keep in sync.
_PYYAML_MIN = (6, 0)
# Fix hint must run from the repo root (relative -r path); audit fc86aa91 f2.
_PYYAML_FIX = "pip install -r requirements.txt  (run from the AQG repo root; declares pyyaml>=6.0)"


def _version_tuple(version: str) -> tuple[int, ...]:
    """Parse '6.0.3'-style version strings to a comparable int tuple; stop at the
    first non-numeric segment ('5.4.1.dev0' → (5, 4, 1)). stdlib only — no packaging
    dependency. Returns () when nothing numeric parses (treated as 'unknown → skip')."""
    parts: list[int] = []
    for token in version.split("."):
        digits = ""
        for ch in token:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts)


def check_pyyaml() -> CheckResult:
    """PyYAML runtime-dependency health check.

    PyYAML parses AQG's YAML metadata — triggers.yaml fixtures, agents/openai.yaml
    (Codex picker display_name), SKILL.md frontmatter — for aqg_skill_validator.py
    and several skill scripts. It is declared in the repo-root requirements.txt and
    is NOT in the Python stdlib. The installed skill FILES do not need it, so a miss
    is WARN (not FAIL): it is only hard-required when running the validator / a
    skill's YAML-parsing path (validator's triggers-count check hard-fails without
    it; other checks degrade to a regex fallback). Also WARNs when the installed
    version is older than the manifest minimum (audit fc86aa91 f1).
    """
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return CheckResult(
            "WARN",
            "pyyaml",
            "PyYAML not installed — aqg_skill_validator triggers-count + several "
            "skills' YAML parsing degrade or fail",
            fix=_PYYAML_FIX,
        )
    version = getattr(yaml, "__version__", "")
    parsed = _version_tuple(version)
    # Compare major.minor only; pad to 2 so (6,) is not treated as < (6, 0).
    norm = (parsed + (0, 0))[:2] if parsed else ()
    if norm and norm < _PYYAML_MIN:
        return CheckResult(
            "WARN",
            "pyyaml",
            f"PyYAML {version} is older than the declared minimum "
            f"({'.'.join(map(str, _PYYAML_MIN))}) in requirements.txt",
            fix=_PYYAML_FIX,
        )
    return CheckResult(
        "PASS",
        "pyyaml",
        f"PyYAML {version or 'unknown'}",
    )


def resolve_aqg_root() -> tuple[Path | None, str]:
    """Resolve in priority order: AQG_ROOT > the repo the doctor script lives in.

    Hook verification requires the same stable entrance spelling used at install
    time; physical-path equivalence must not hide a version-pinned definition.

    The source field is kept short ("AQG_ROOT env" / "AQG_ROOT env (empty)" /
    "AQG_ROOT env (not a directory)" / "derived from doctor location") to avoid
    stuttering in check_aqg_root output (finding #8).
    """
    env_root = os.environ.get("AQG_ROOT")
    if env_root is not None:
        stripped = env_root.strip()
        if not stripped:
            return None, "AQG_ROOT env (empty)"
        path = Path(stripped).expanduser()
        if path.is_dir():
            # Hook definitions retain this entrance so managed upgrades can move it.
            return path.absolute(), "AQG_ROOT env"
        return path, "AQG_ROOT env (not a directory)"
    # Match the installer's logical root even when AQG_ROOT is unset.
    candidate = Path(__file__).absolute().parents[1]
    if not (candidate / "VERSION").is_file():
        # Retain discovery when only the doctor script is linked outside its repo.
        candidate = Path(__file__).resolve().parents[1]
    if (candidate / "VERSION").is_file():
        return candidate, f"derived from doctor location ({candidate})"
    return None, "unresolved"


def _sanitize_echo(text: str, *, limit: int = 200) -> str:
    """Collapse untrusted file content into a single line safe to echo into
    CheckResult.detail (GD-11).

    VERSION / .aqg-root content is subject to corruption / tampering; a bare echo
    would let a multi-line / control-character payload break the "one line per
    check" output contract. Whitespace (including the Unicode line separators
    NEL/LS/PS) is collapsed to a single space, control / format / surrogate
    characters (Unicode "C*" categories) are removed, and overlong content is
    truncated.
    """
    kept: list[str] = []
    for ch in text:
        if ch.isspace():
            kept.append(" ")
        elif unicodedata.category(ch).startswith("C"):
            continue
        else:
            kept.append(ch)
    collapsed = " ".join("".join(kept).split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…(truncated)"
    return collapsed


def check_aqg_root(root: Path | None, source: str) -> list[CheckResult]:
    if root is None:
        return [
            CheckResult(
                "FAIL",
                "aqg_root",
                f"cannot resolve AQG root ({source})",
                fix="export AQG_ROOT=/path/to/agent-quality-gates checkout",
            )
        ]
    if not root.is_dir():
        # 0.1.4 finding #8: detail no longer stutters by repeating "not a directory".
        # source already carries the reason "AQG_ROOT env (not a directory)", so here
        # we only print the path.
        return [
            CheckResult(
                "FAIL",
                "aqg_root",
                f"{root} ({source})",
                fix="unset AQG_ROOT or set it to a real agent-quality-gates checkout",
            )
        ]
    results = [CheckResult("PASS", "aqg_root", f"{root} ({source})")]

    version_file = root / "VERSION"
    if version_file.is_file():
        try:
            with version_file.open(encoding="utf-8") as fh:
                version = fh.read(_MAX_PROBE_CHARS).strip()
        except (OSError, UnicodeDecodeError) as exc:
            # GD-08: doctor must not crash on the corrupted VERSION it is meant to diagnose; degrade to FAIL.
            results.append(
                CheckResult(
                    "FAIL",
                    "version",
                    f"VERSION unreadable: {type(exc).__name__}",
                    fix="re-clone the AQG repository or check checkout integrity",
                )
            )
        else:
            results.append(CheckResult("PASS", "version", f"VERSION={_sanitize_echo(version)}"))
    else:
        results.append(
            CheckResult(
                "FAIL",
                "version",
                f"VERSION file missing at {version_file}",
                fix="re-clone the AQG repository or check checkout integrity",
            )
        )

    for rel in CRITICAL_SCRIPTS:
        path = root / rel
        if path.is_file():
            results.append(CheckResult("PASS", f"script:{rel}", "present"))
        else:
            results.append(
                CheckResult(
                    "FAIL",
                    f"script:{rel}",
                    f"missing at {path}",
                    fix="re-clone the AQG repository or pin a different ref",
                )
            )
    return results


def check_skill_install(
    *,
    label: str,
    target_dir: Path,
    expected_names: tuple[str, ...],
    aqg_root: Path | None,
) -> list[CheckResult]:
    """Generic skill-install check: each skill should be a directory (copy) or a
    symlink (link) that ultimately resolves inside the repo."""
    if not target_dir.is_dir():
        return [
            CheckResult(
                "WARN",
                f"{label}_root",
                f"{target_dir} does not exist (skills not installed for this agent)",
                fix=f"run installer to populate {target_dir}",
            )
        ]
    # Containment compares physical paths; hook rendering keeps the entrance.
    try:
        resolved_root = aqg_root.resolve() if aqg_root is not None else None
    except (OSError, RuntimeError) as exc:
        return [
            CheckResult(
                "FAIL",
                f"{label}_root",
                f"cannot resolve AQG_ROOT: {type(exc).__name__}",
                fix="verify AQG_ROOT points to a resolvable checkout",
            )
        ]
    results = [CheckResult("PASS", f"{label}_root", str(target_dir))]
    for name in expected_names:
        skill_path = target_dir / name
        if not skill_path.exists():
            results.append(
                CheckResult(
                    "FAIL",
                    f"{label}:{name}",
                    f"missing at {skill_path}",
                    fix="re-run the installer with --force",
                )
            )
            continue
        install_type = classify_install(skill_path)
        if install_type in {"symlink", "junction"}:
            try:
                resolved = skill_path.resolve(strict=True)
            except OSError as exc:
                results.append(
                    CheckResult(
                        "FAIL",
                        f"{label}:{name}",
                        f"broken {install_type}: {exc}",
                        fix="re-run the installer with --force to restore link mode",
                    )
                )
                continue
            if resolved_root is None or resolved_root not in resolved.parents and resolved_root != resolved:
                results.append(
                    CheckResult(
                        "WARN",
                        f"{label}:{name}",
                        f"{install_type} resolves to {resolved} (outside AQG_ROOT={aqg_root})",
                        fix="verify AQG_ROOT matches the actual checkout that owns these skills",
                    )
                )
            elif not (resolved / "SKILL.md").is_file():
                # GD-10 sibling gap (dual-audit b1a90943): a symlink target missing
                # SKILL.md is equally a broken install and must not count as a healthy
                # PASS — symmetric with the copy-mode SKILL.md gate below.
                results.append(
                    CheckResult(
                        "FAIL",
                        f"{label}:{name}",
                        f"{install_type} target missing SKILL.md: {resolved}",
                        fix="re-run the installer with --force",
                    )
                )
            else:
                results.append(
                    CheckResult("PASS", f"{label}:{name}", f"{install_type} -> {resolved}")
                )
        elif install_type in {"copied", "plain_directory"}:
            # Directory installs must contain SKILL.md to count as usable (GD-10:
            # an empty dir / missing SKILL.md was previously treated as PASS, i.e.
            # reporting a broken install as healthy).
            if not (skill_path / "SKILL.md").is_file():
                results.append(
                    CheckResult(
                        "FAIL",
                        f"{label}:{name}",
                        f"{install_type.replace('_', ' ')} but SKILL.md missing at {skill_path}",
                        fix="re-run the installer with --force",
                    )
                )
                continue
            aqg_root_pointer = skill_path / ".aqg-root"
            if install_type == "copied":
                try:
                    with aqg_root_pointer.open(encoding="utf-8") as fh:
                        pointed = fh.read(_MAX_PROBE_CHARS).strip()
                except (OSError, UnicodeDecodeError) as exc:
                    # GD-09: a corrupted / tampered pointer should not crash doctor; degrade to WARN.
                    results.append(
                        CheckResult(
                            "WARN",
                            f"{label}:{name}",
                            f"copied directory, cannot read .aqg-root: {type(exc).__name__}",
                            fix="re-run the installer to refresh .aqg-root",
                        )
                    )
                    continue
                if pointed and Path(pointed).is_dir():
                    results.append(
                        CheckResult(
                            "PASS",
                            f"{label}:{name}",
                            f"copied directory, .aqg-root -> {_sanitize_echo(pointed)}",
                        )
                    )
                else:
                    results.append(
                        CheckResult(
                            "WARN",
                            f"{label}:{name}",
                            f"copied directory, .aqg-root points to invalid path: {_sanitize_echo(pointed)}",
                            fix="re-run the installer to refresh .aqg-root",
                        )
                    )
            else:
                results.append(
                    CheckResult(
                        "WARN",
                        f"{label}:{name}",
                        "unmarked plain directory (unmanaged or legacy copy; no installer provenance)",
                        fix="re-run the installer with --force to restore link mode (symlink/junction), or use --copy --force to migrate a legacy copy",
                    )
                )
        else:
            results.append(
                CheckResult(
                    "FAIL",
                    f"{label}:{name}",
                    f"unexpected file type at {skill_path}",
                    fix="remove the file and re-run the installer",
                )
            )
    return results


def check_codex_hooks(target: Path, aqg_root: Path | None) -> CheckResult:
    """Check the managed Codex hook set without changing ``hooks.json``."""
    if aqg_root is None:
        return CheckResult(
            "FAIL",
            "codex_hooks",
            "cannot verify hooks because AQG root is unresolved",
            fix="set AQG_ROOT to the trusted AQG checkout",
        )
    expanded = target.expanduser()
    if expanded.is_symlink() and not expanded.exists():
        return CheckResult(
            "FAIL",
            "codex_hooks",
            f"broken symlink: {expanded}",
            fix="remove the dangling hooks.json symlink and re-run scripts/install_aqg_codex_hooks.py --apply",
        )
    try:
        from install_aqg_codex_hooks import inspect_install

        status, detail = inspect_install(expanded, aqg_root, Path(sys.executable))
    except Exception as exc:  # aqg: top-level boundary
        return CheckResult(
            "FAIL",
            "codex_hooks",
            f"verification failed: {type(exc).__name__}",
            fix="run scripts/install_aqg_codex_hooks.py --verify",
        )
    if status == "complete":
        return CheckResult(
            "PASS",
            "codex_hooks",
            f"on-disk definitions verified ({detail}); this does not prove runtime discovery or /hooks trust",
        )
    if status == "missing":
        return CheckResult(
            "WARN",
            "codex_hooks",
            detail,
            fix="run scripts/install_aqg_codex_hooks.py --apply, restart Codex, then trust via /hooks",
        )
    return CheckResult(
        "FAIL",
        "codex_hooks",
        f"{status}: {detail}",
        fix="review the backup boundary, then re-run scripts/install_aqg_codex_hooks.py --apply",
    )


def check_claude_hooks(target: Path, aqg_root: Path | None) -> CheckResult:
    """Check the managed Claude Code hook set without changing ``settings.json``."""
    if aqg_root is None:
        return CheckResult(
            "FAIL",
            "claude_hooks",
            "cannot verify hooks because AQG root is unresolved",
            fix="set AQG_ROOT to the trusted AQG checkout",
        )
    expanded = target.expanduser()
    if expanded.is_symlink() and not expanded.exists():
        return CheckResult(
            "FAIL",
            "claude_hooks",
            f"broken symlink: {expanded}",
            fix="remove the dangling settings.json symlink and re-run scripts/install_aqg_hooks.py --apply",
        )
    try:
        from install_aqg_hooks import inspect_install

        status, detail = inspect_install(expanded, aqg_root)
    except Exception as exc:  # aqg: top-level boundary
        return CheckResult(
            "FAIL",
            "claude_hooks",
            f"verification failed: {type(exc).__name__}",
            fix="run scripts/install_aqg_hooks.py --verify",
        )
    if status == "complete":
        return CheckResult(
            "PASS",
            "claude_hooks",
            f"on-disk definitions verified ({detail}); this does not prove runtime discovery",
        )
    if status == "missing":
        return CheckResult(
            "WARN",
            "claude_hooks",
            detail,
            fix="run scripts/install_aqg_hooks.py --apply, then restart Claude Code",
        )
    return CheckResult(
        "FAIL",
        "claude_hooks",
        f"{status}: {detail}",
        fix="review the backup boundary, then re-run scripts/install_aqg_hooks.py --apply",
    )


def _has_any_expected_skill(target_dir: Path, expected_names: tuple[str, ...]) -> bool:
    return any((target_dir / name).exists() or os.path.lexists(target_dir / name) for name in expected_names)


RULES_BLOCK_HEADING = "## Agent Quality Gates (AQG) engineering discipline"

# Framings a rules block may still carry after being retired. Publishing a new
# template does not reach anyone who already installed the old one, so a
# present-but-outdated block is a silent wrong answer rather than a visible gap —
# strictly harder to notice than a missing one.
RETIRED_RULES_MARKERS: tuple[tuple[str, str], ...] = (
    (
        "audit-self-routing",
        "retired main-path/fallback depth framing (Owner ruling 2026-08-11); "
        "the file it names was never shipped by any installer, so the depth "
        "guidance it promises is unreachable",
    ),
    (
        "Phase × Stakes",
        "retired phase-dependent depth matrix; depth is keyed on stakes alone "
        "(docs/policies/audit-trigger.md: 'Phase does not affect depth'), so this "
        "block hands the agent a second, contradictory depth authority",
    ),
    (
        "### Audit orchestration",
        "superseded long rules block (~190 lines of per-skill usage). The host "
        "already loads those skill descriptions as its index, so an installed copy "
        "pays that cost twice per session — and the Codex-only install paths it "
        "states are false on every other client that received the same text",
    ),
    (
        "(fallback layer)",
        "retired main-path/fallback framing for aqg-phase-transition; it supplies "
        "timing, not depth, and the main path it implies was never shipped",
    ),
)


# Which installer writes each host's resident block. Cursor's adapter predates
# the Claude/Codex one; both are named here so a WARN carries a command the
# reader can run rather than "append the block from the template", which is a
# hand-edit procedure and was where its own drift came from.
RULES_BLOCK_FIX_COMMANDS: dict[str, str] = {
    "claude": "python3 scripts/install_aqg_rules.py --client claude-code --apply",
    "codex": "python3 scripts/install_aqg_rules.py --client codex --apply",
}

# Cursor is deliberately absent above: install_cursor_support.py writes
# .cursor/rules/aqg.mdc only under --scope project, and under --scope user it
# prints "Cursor User Rules are UI-managed; no undocumented user rule file was
# written". No command can fix a missing user-scope Cursor block, so naming one
# would be the same dead advice this channel's installer exists to remove.
# Hosts whose USER-scope rules channel AQG cannot write, so its absence is a fact
# about the product rather than a defect on this machine. aqg_client_registry
# already states Cursor's surface this way ('.cursor/rules/aqg.mdc for project
# scope; user rules are UI-managed'); doctor asserting the opposite produced a
# WARN that no action could clear, which is what teaches people to stop reading
# doctor at all. A block that IS present is still checked for content.
UI_MANAGED_USER_RULES: dict[str, str] = {
    "cursor": (
        "Cursor User Rules are UI-managed, so no AQG installer may write this "
        "file; paste the block there by hand, or install a PROJECT block with "
        "scripts/install_cursor_support.py --apply --scope project"
    ),
}


RULES_BLOCK_MANUAL_FIX: dict[str, str] = {
    "cursor": (
        "paste the block from examples/aqg-codex-agents.example.md into Cursor's "
        "User Rules (UI-managed; no installer may write it). For a PROJECT block, "
        "run python3 scripts/install_cursor_support.py --apply --scope project "
        "--project-root <path>"
    ),
}


def rules_block_fix(host: str) -> str:
    """The command that installs *host*'s block, or generic advice if it has none."""
    command = RULES_BLOCK_FIX_COMMANDS.get(host)
    if command is not None:
        return f"run {command} (from the AQG checkout)"
    manual = RULES_BLOCK_MANUAL_FIX.get(host)
    if manual is not None:
        return manual
    return (
        "append the block from the current examples/aqg-*-rules template — "
        f"no installer writes {host}'s rules file"
    )


def check_rules_block_text(host: str, text: str) -> CheckResult:
    """Channel 2: the always-resident rules block, judged from its content.

    Split from the file lookup so the judgement is testable without a fixture
    tree, and so a caller can check a block it already has in hand.
    """
    name = f"rules_block:{host}"
    if RULES_BLOCK_HEADING not in text:
        return CheckResult(
            "WARN",
            name,
            "no AQG rules block — the always-resident channel is empty, so nothing "
            "tells the agent the discipline exists",
            fix=rules_block_fix(host),
        )
    # A heading with nothing under it delivers exactly as much as no heading at
    # all, and the first version of this check called it PASS. Every real template
    # names the skills it governs, so requiring at least one is a content signal
    # rather than a line count, which a stub could pad past.
    body = text.split(RULES_BLOCK_HEADING, 1)[1]
    if "aqg-" not in body:
        return CheckResult(
            "WARN",
            name,
            "AQG rules block is a stub: the heading is present but its body names "
            "no aqg-* skill, so it tells the agent nothing",
            fix=(
                "replace it with the full block from the current "
                "examples/aqg-*-rules template"
            ),
        )
    for marker, why in RETIRED_RULES_MARKERS:
        if marker in text:
            offending = next(
                (line for line in text.splitlines() if marker in line), marker
            )
            return CheckResult(
                "WARN",
                name,
                f"stale rules block: still references {marker} — {why}; "
                f"first hit: {_sanitize_echo(offending, limit=90)}",
                fix=(
                    "re-sync the block from the current examples/aqg-*-rules template; "
                    "editing the template does not update an already-installed copy"
                ),
            )
    return CheckResult("PASS", name, "AQG rules block present and current")


def check_rules_block(
    path: Path, host: str, host_root: Path | None = None
) -> CheckResult:
    """Channel 2 for one host.

    An ABSENT host is not a problem; a PRESENT host whose rules file is missing
    very much is. The first version conflated the two and reported PASS "not
    installed" for a host that was installed with its rules file deleted — a false
    green on a real machine, which is the exact failure mode this check exists to
    remove.
    """
    name = f"rules_block:{host}"
    expanded = path.expanduser()
    if not expanded.is_file():
        root = (host_root or expanded.parent).expanduser()
        if root.exists():
            ui_managed = UI_MANAGED_USER_RULES.get(host)
            if ui_managed is not None:
                return CheckResult("PASS", name, f"{host}: {ui_managed}")
            return CheckResult(
                "WARN",
                name,
                f"{host} is installed ({root.name} exists) but has no AQG rules "
                f"block: {expanded.name} is missing, so its always-resident "
                "channel is empty",
                fix=rules_block_fix(host),
            )
        return CheckResult(
            "PASS", name, f"{host} not installed on this host (no {root.name})"
        )
    try:
        text = expanded.read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:  # corrupt / non-UTF-8 / unreadable
        return CheckResult(
            "WARN",
            name,
            f"cannot read {expanded.name}: {type(exc).__name__}",
            fix="repair or re-create the rules file, then re-run doctor",
        )
    return check_rules_block_text(host, text)


def check_hook_model_visibility_text(script_name: str, text: str) -> CheckResult:
    """Channel 3: does a PostToolUse hook actually reach the model?

    Claude Code does not forward a hook's stderr to the model when the hook exits
    0, so a reminder written only to stderr is terminal-only there while Codex and
    Cursor receive theirs. Definitions can be perfectly installed and deliver
    nothing — which no existing check would notice.
    """
    name = f"hook_visible:{script_name}"
    # Strip comments first: a hook that only MENTIONS the mechanism in a TODO
    # delivers nothing, and the first version of this check counted the mention.
    code = "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )
    if "additionalContext" in code or "aqg_flush_context" in code:
        return CheckResult("PASS", name, "emits model-visible context")
    if ">&2" not in code:
        return CheckResult("PASS", name, "emits no reminder text; nothing to deliver")
    return CheckResult(
        "WARN",
        name,
        "writes reminders to stderr only — on exit 0 Claude Code does not forward "
        "stderr to the model, so this hook is invisible there",
        fix=(
            "emit hookSpecificOutput.additionalContext on stdout as well; see "
            "posttooluse_code_construction_reminder.sh for the shape"
        ),
    )


def check_hook_model_visibility(hooks_dir: Path) -> list[CheckResult]:
    """Channel 3 across every shipped PostToolUse hook."""
    expanded = hooks_dir.expanduser()
    if not expanded.is_dir():
        return [
            CheckResult(
                "WARN",
                "hook_visible",
                f"hook directory not found: {expanded}",
                fix="set AQG_ROOT to the trusted AQG checkout",
            )
        ]
    results: list[CheckResult] = []
    for hook in sorted(expanded.glob("posttooluse_*.sh")):
        try:
            text = hook.read_text(encoding="utf-8")
        except (OSError, ValueError) as exc:  # corrupt / non-UTF-8
            results.append(
                CheckResult(
                    "WARN",
                    f"hook_visible:{hook.name}",
                    f"cannot read: {type(exc).__name__}",
                    fix="restore the hook from the AQG checkout",
                )
            )
            continue
        results.append(check_hook_model_visibility_text(hook.name, text))
    return results


# The literal guard every managed hook command opens with. install_aqg_hooks.py
# deliberately does NOT bake the checkout path into the command (so a user can
# swap checkouts without re-running install) — the cost of that choice is that an
# unset AQG_ROOT turns every hook into a silent no-op, including the PreToolUse
# secret scan, which is a BLOCKING security gate.
_HOOK_ENV_GUARD = 'if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi'


# Both spellings a command may use for the root. Matching only the bare form would
# make the script-existence gate fail SILENTLY if a command ever switched to the
# braced one: nothing extracted, nothing reported missing, PASS on a security gate
# that cannot run (audit aud_7nsuQouSm96_9rt9). test_every_guarded_command_yields_a
# _script_ref keeps that from going unnoticed whatever the installer settles on.
_HOOK_SCRIPT_REF = re.compile(r'\$(?:AQG_ROOT|\{AQG_ROOT\})/([^"\s;|&)]+)')


def _hook_script_refs(settings: object) -> set[str]:
    """Every ``$AQG_ROOT``-relative script path the managed hook commands invoke."""
    refs: set[str] = set()
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for groups in (hooks or {}).values():
        for group in groups or ():
            for hook in (group or {}).get("hooks", ()) or ():
                command = str((hook or {}).get("command", ""))
                refs.update(_HOOK_SCRIPT_REF.findall(command))
    return refs


def check_hook_env_guard_text(settings_text: str, env_value: str | None) -> CheckResult:
    """Channel 3, failure mode 2: hooks installed but gated OFF by the environment.

    Never uses ``resolve_aqg_root()``'s result. That resolver falls back to the
    doctor script's own location when the variable is empty, which is what keeps
    doctor itself working — and is exactly what hides this failure: ``aqg_root``
    is then never ``None``, so the FAIL branch in ``check_claude_hooks`` is
    unreachable and doctor prints "PASS aqg_root" while every hook is dead.

    TWO sources make the hooks work, and either satisfies this check: a real
    exported variable (inherited by the hook process) or ``settings.json``'s own
    ``env`` block (injected into it by the host). Reading only ``os.environ``
    bricked the install — the installer writes the repair into settings.json, but
    doctor runs as a subprocess that never sees that block, so the fix was
    invisible to the checker it exists to satisfy: FAIL, gate dies, re-run
    install, same FAIL, no way out (audit aud_GkYkV_EMcdUnKQ0U).

    The shell guard only tests ``-z``, so a stale path passes it and fails later
    at the ``bash "$AQG_ROOT/..."`` call; both are reported here as one failure.
    """
    name = "hook_env_guard"
    try:
        settings = json.loads(settings_text)
    except (ValueError, TypeError):
        return CheckResult(
            "WARN",
            name,
            "settings.json is not valid JSON; cannot tell whether hooks are gated",
            fix="repair settings.json, then re-run this check",
        )
    guarded = 0
    hooks = settings.get("hooks") if isinstance(settings, dict) else None
    for groups in (hooks or {}).values():
        for group in groups or ():
            for hook in (group or {}).get("hooks", ()) or ():
                if _HOOK_ENV_GUARD in str((hook or {}).get("command", "")):
                    guarded += 1
    if not guarded:
        return CheckResult("PASS", name, "no AQG_ROOT-gated hooks installed; nothing to gate")

    settings_env = settings.get("env") if isinstance(settings, dict) else None
    declared = settings_env.get("AQG_ROOT") if isinstance(settings_env, dict) else None
    exported = (env_value or "").strip()
    from_settings = declared.strip() if isinstance(declared, str) else ""
    # Which of the two the hook process actually receives is a host detail not
    # observable from here, so a disagreement cannot be ranked — picking one would
    # mean validating a root the hooks may not use and reporting PASS while they are
    # dead (audit aud_7nsuQouSm96_9rt9). The disagreement IS the finding.
    if exported and from_settings and exported != from_settings:
        return CheckResult(
            "FAIL",
            name,
            "the environment and settings.json's env block disagree about AQG_ROOT "
            f"(environment: {_sanitize_echo(exported, limit=80)}; settings.json: "
            f"{_sanitize_echo(from_settings, limit=80)}) — which one the hook process "
            "receives is not observable here, so neither can be verified for it",
            fix="make the two agree (or drop one), then restart the session",
        )
    stripped = exported or from_settings
    if not stripped:
        return CheckResult(
            "FAIL",
            name,
            f"{guarded} hook command(s) are gated on AQG_ROOT, but neither the environment "
            "nor settings.json's env block supplies it — every one of them silently "
            "no-ops, including the PreToolUse secret scan",
            fix=(
                'set it where the host passes it to hooks: add {"env": {"AQG_ROOT": '
                '"/path/to/checkout"}} to settings.json, then restart the session'
            ),
        )
    root = Path(stripped).expanduser()
    if not (root / "VERSION").is_file():
        return CheckResult(
            "FAIL",
            name,
            f"{guarded} hook command(s) are gated on AQG_ROOT, but it does not point at an "
            f"AQG checkout (no VERSION sentinel under {_sanitize_echo(str(root), limit=120)}) — "
            "the guard passes and the hooks then fail on the bash call",
            fix="point AQG_ROOT at the checkout that owns these hooks, then restart the session",
        )
    # A VERSION sentinel proves a path, not a working checkout. PASS here has to mean
    # "the secret scan will actually run", so resolve each command's own script and
    # require it to exist: a stale or partial tree carries VERSION while the bash call
    # the hook makes points at nothing (audit aud_GkYkV_EMcdUnKQ0U, blocking).
    missing = sorted(
        {
            ref
            for ref in _hook_script_refs(settings)
            if not (root / ref).is_file()
        }
    )
    if missing:
        shown = ", ".join(_sanitize_echo(m, limit=80) for m in missing[:3])
        more = f" (+{len(missing) - 3} more)" if len(missing) > 3 else ""
        return CheckResult(
            "FAIL",
            name,
            f"AQG_ROOT resolves, but {len(missing)} hook script(s) it must run are missing "
            f"under {_sanitize_echo(str(root), limit=90)}: {shown}{more} — the commands would "
            "fail on the bash call, so the gates they carry do not run",
            fix="re-run scripts/install_aqg_hooks.py --apply from the checkout AQG_ROOT names",
        )
    source = "environment" if (env_value or "").strip() else "settings.json env"
    return CheckResult(
        "PASS",
        name,
        f"{guarded} gated hook command(s); AQG_ROOT resolves via {source} and every "
        "referenced hook script exists",
    )


UPDATE_KILL_SWITCH = "AQG_NO_UPDATE_CHECK"


def check_update_channel() -> list[CheckResult]:
    """Report what the automatic update check has been doing.

    The automatic path is silent by design — it runs detached at session start
    and writes nothing to any terminal — so this is the only place a user can
    find out whether it ran, what it decided, and what it is holding back for
    them. A silent system with no surface is one nobody can debug after the fact.
    """
    try:
        from aqg_update import run as update_run
    except ImportError:
        try:
            from scripts.aqg_update import run as update_run  # type: ignore[no-redef]
        except ImportError as exc:
            return [
                CheckResult("WARN", "update-channel",
                            f"managed update module not importable: {exc}",
                            "reinstall AQG, or ignore if this build predates managed updates")
            ]
    last = update_run.read_diagnostics()
    if last is None:
        return [
            CheckResult("WARN", "update-channel",
                        "no update check has ever run on this machine",
                        "check that AQG_ROOT is exported and the SessionStart hooks are installed")
        ]
    outcome = str(last.get("outcome", "unknown"))
    detail = str(last.get("detail") or "")
    # Status AND remedy per outcome. One remedy string reused for every
    # non-PASS state told a user with a disabled channel to go read the
    # architecture document, which is not what they need to know.
    grades = {
        "applied": ("PASS", ""),
        "current": ("PASS", ""),
        "too-soon": ("PASS", ""),
        "deferred": ("WARN", "a check-only trigger found a release; a later session-start check can apply it if locks and host checks permit"),
        "invalid-root": ("FAIL", "reapply the affected host's AQG hooks and rules using the logical installation entrance, not a versions directory"),
        "disabled": ("WARN", f"unset {UPDATE_KILL_SWITCH} to re-enable automatic updates"),
        "no-keyring": ("WARN", "this build ships no pinned release key; managed "
                               "updates stay off until one does"),
        "pending": ("WARN", "a release is verified and staged but changes host "
                            "configuration, so it was not applied; see the "
                            "update-pending lines below"),
        "rolled-back": ("WARN", "an update was undone; the previous version is live"),
        "interrupted": ("WARN", "a check is running now, or one was stopped before "
                                "it finished; look again in a moment"),
        "repair-required": ("FAIL", "an update stopped part-way and could not be "
                                    "undone; see docs/UPDATE_ARCHITECTURE.md 5"),
    }
    status, fix = grades.get(outcome, ("FAIL", "see docs/UPDATE_ARCHITECTURE.md 10"))
    results = [
        CheckResult(status, "update-channel", f"last check: {outcome}. {detail}".strip(), fix)
    ]
    for item in last.get("pending") or []:
        results.append(
            CheckResult("WARN", "update-pending", str(item),
                        "this change needs a human: it alters a host's own configuration")
        )
    return results


def check_external_cli(name: str, *, required: bool) -> CheckResult:
    if not shutil.which(name):
        if required:
            return CheckResult(
                "FAIL",
                f"cli:{name}",
                f"{name} not found in PATH",
                fix=f"install {name} (required for AQG)",
            )
        return CheckResult(
            "WARN",
            f"cli:{name}",
            f"{name} not found in PATH",
            fix=f"install {name} for full AQG GitHub PR adapter functionality",
        )
    rc, out = run_cli([name, "--version"])
    if rc == 0:
        first_line = out.splitlines()[0] if out else "(no version output)"
        return CheckResult("PASS", f"cli:{name}", first_line)
    return CheckResult(
        "WARN",
        f"cli:{name}",
        f"{name} found but --version exit {rc}: {out[:100]}",
        fix=None,
    )


def _redact_path_in_detail(detail: str) -> str:
    """Replace any path prefix matching the user's $HOME / cwd with a placeholder
    marker.

    triple-audit gpt-5.5 #2 accepted: install-mode CheckResult.detail contains
    local absolute paths (claude_root / aqg_root / skill resolution paths, etc.);
    when the whole session-mode output is treated downstream as a redacted
    artifact, those paths would bypass the surface fingerprint guard. This helper
    runs one redaction pass before session-mode output.

    Strategy: replace only the longest prefix match (home > cwd), preserving the
    detail text + the file-name suffix so it stays readable for a human / log
    diff. If a single detail contains both the home and cwd substrings, both are
    replaced.
    """
    if not detail:
        return detail
    home = str(Path.home())
    cwd = str(Path.cwd())
    out = detail
    # replace the longest prefix first (avoids a miss when home is a subset of cwd)
    for prefix, marker in sorted(
        ((home, "<HOME>"), (cwd, "<CWD>")),
        key=lambda x: len(x[0]),
        reverse=True,
    ):
        if prefix and prefix in out:
            out = out.replace(prefix, marker)
    return out


def emit_text(
    results: list[CheckResult],
    *,
    fingerprint: dict | None = None,
    mode: str = "install",
) -> None:
    redact_install = mode == "session"
    statuses = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for result in results:
        statuses[result.status] = statuses.get(result.status, 0) + 1
        marker = {"PASS": "+", "WARN": "!", "FAIL": "x"}.get(result.status, "?")
        detail = _redact_path_in_detail(result.detail) if redact_install else result.detail
        print(f"  [{marker}] {result.status:4s} {result.name}: {detail}")
        if result.fix and result.status != "PASS":
            fix = _redact_path_in_detail(result.fix) if redact_install else result.fix
            print(f"        fix: {fix}")
    if fingerprint is not None:
        print()
        print("## Session fingerprint")
        for surface_name in sorted(fingerprint.keys()):
            value = fingerprint[surface_name]
            print(f"  {surface_name}: {_format_fingerprint_value(value)}")
    print()
    print(f"Summary: PASS={statuses['PASS']} WARN={statuses['WARN']} FAIL={statuses['FAIL']}")


def _format_fingerprint_value(value: object) -> str:
    """Text rendering of a single surface value. dict → 'k=v k=v'; scalar → repr-like."""
    if isinstance(value, dict):
        if not value:
            return "{}"
        return " ".join(f"{k}={value[k]}" for k in sorted(value.keys()))
    return str(value)


def emit_json(
    results: list[CheckResult],
    *,
    fingerprint: dict | None = None,
    mode: str = "install",
) -> None:
    redact_install = mode == "session"
    payload: dict[str, object] = {
        "ok": all(r.status != "FAIL" for r in results),
        "mode": mode,
        "results": [
            {
                "status": r.status,
                "name": r.name,
                "detail": _redact_path_in_detail(r.detail) if redact_install else r.detail,
                "fix": (
                    _redact_path_in_detail(r.fix)
                    if (redact_install and r.fix is not None)
                    else r.fix
                ),
            }
            for r in results
        ],
    }
    if fingerprint is not None:
        payload["session_fingerprint"] = fingerprint
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


SUPPORTED_MODES: tuple[str, ...] = ("install", "session", "commit", "push", "pr")
WAVE1_IMPLEMENTED_MODES: frozenset[str] = frozenset({"install", "session"})


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Diagnose local AQG installation.")
    parser.add_argument("--json", action="store_true", help="emit JSON instead of text")
    parser.add_argument("--no-cli", action="store_true", help="skip git/gh probing")
    parser.add_argument(
        "--mode",
        default="install",
        choices=SUPPORTED_MODES,
        help=(
            "diagnostic mode: install (default, current behavior); session "
            "(install + redacted desktop client surface fingerprint per "
            "framework v2.1 §6/§12); commit/push/pr (Wave 2-3, not yet "
            "implemented; will exit %d)" % EXIT_USAGE
        ),
    )
    parser.add_argument(
        "--claude-skills-dir",
        default=str(Path.home() / ".claude" / "skills"),
        help="Claude Code skills install root (default: ~/.claude/skills)",
    )
    parser.add_argument(
        "--codex-skills-dir",
        default=str(Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "skills"),
        help="Codex skills install root (default: $CODEX_HOME/skills)",
    )
    parser.add_argument(
        "--codex-hooks-file",
        default=None,
        help="Codex hooks file (default: $CODEX_HOME/hooks.json)",
    )
    parser.add_argument(
        "--claude-settings-file",
        default=None,
        help="Claude Code settings file (default: ~/.claude/settings.json)",
    )
    return parser.parse_args(argv)


def _run_install_mode(args: argparse.Namespace) -> tuple[list[CheckResult], Path | None]:
    """Run install-mode checks; return (results, resolved aqg_root).

    This is extracted into a helper so session mode can reuse all of install's
    checks and then layer the fingerprint on top, without resolving aqg_root a
    second time (guards against env drift). triple-audit o3 #1 accepted: the first
    implementation step refactors the existing inline code into a helper, then
    calls it.
    """
    aqg_root, source = resolve_aqg_root()
    results: list[CheckResult] = []
    results.append(check_python_version())
    results.append(check_pyyaml())
    results.extend(check_aqg_root(aqg_root, source))
    results.extend(
        check_skill_install(
            label="claude_skill",
            target_dir=Path(args.claude_skills_dir).expanduser(),
            expected_names=CLAUDE_SKILL_NAMES,
            aqg_root=aqg_root,
        )
    )
    claude_settings_file = getattr(args, "claude_settings_file", None)
    claude_settings_explicit = claude_settings_file is not None
    if claude_settings_file is None:
        claude_settings_file = str(Path.home() / ".claude" / "settings.json")
    claude_settings_path = Path(claude_settings_file).expanduser()
    claude_skills_path = Path(args.claude_skills_dir).expanduser()
    if (
        claude_settings_explicit
        or os.path.lexists(claude_settings_path)
        or _has_any_expected_skill(claude_skills_path, CLAUDE_SKILL_NAMES)
    ):
        results.append(check_claude_hooks(claude_settings_path, aqg_root))
        # Definitions being present is not the same as the hooks being able to RUN:
        # every managed command is gated on AQG_ROOT, and nothing else notices when
        # that variable is missing (see check_hook_env_guard_text).
        try:
            settings_text = claude_settings_path.read_text(encoding="utf-8")
        except (OSError, ValueError):
            settings_text = ""
        results.append(
            check_hook_env_guard_text(settings_text, os.environ.get("AQG_ROOT"))
        )
    codex_hooks_file = getattr(args, "codex_hooks_file", None)
    codex_hooks_explicit = codex_hooks_file is not None
    if codex_hooks_file is None:
        codex_hooks_file = str(
            Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
            / "hooks.json"
        )
    codex_hooks_path = Path(codex_hooks_file).expanduser()
    codex_skills_path = Path(args.codex_skills_dir).expanduser()
    if (
        codex_hooks_explicit
        or os.path.lexists(codex_hooks_path)
        or _has_any_expected_skill(codex_skills_path, CODEX_SKILL_NAMES)
    ):
        results.append(check_codex_hooks(codex_hooks_path, aqg_root))
    results.extend(
        check_skill_install(
            label="codex_skill",
            target_dir=Path(args.codex_skills_dir).expanduser(),
            expected_names=CODEX_SKILL_NAMES,
            aqg_root=aqg_root,
        )
    )
    # Channels 2 and 3 — the two that carry discipline to a MODEL. Skills being
    # installed proves neither: a machine once reported 73 PASS / 0 WARN with an
    # empty rules block and stderr-only hooks, delivering nothing while looking
    # perfectly healthy. A host that is not installed reports PASS, so this stays
    # quiet on machines that only use one agent.
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    # The host root is what proves the host is INSTALLED. It is the rules file's
    # own directory for Claude and Codex, but Cursor nests its rules one level
    # deeper, so ~/.cursor is the marker rather than ~/.cursor/rules.
    host_roots = {
        "claude": Path.home() / ".claude",
        "codex": codex_home,
        "cursor": Path.home() / ".cursor",
    }
    for host, rules_path in (
        ("claude", Path.home() / ".claude" / "CLAUDE.md"),
        ("codex", codex_home / "AGENTS.md"),
        ("cursor", Path.home() / ".cursor" / "rules" / "aqg.mdc"),
    ):
        results.append(check_rules_block(rules_path, host, host_root=host_roots[host]))
    if aqg_root is not None:
        results.extend(
            check_hook_model_visibility(
                aqg_root / "agent-packs" / "claude-code" / "hooks"
            )
        )
    if not args.no_cli:
        results.append(check_external_cli("git", required=True))
        results.append(check_external_cli("gh", required=False))
    return results, aqg_root


def _run_session_fingerprint(
    args: argparse.Namespace, aqg_root: Path | None
) -> tuple[list[CheckResult], dict | None]:
    """Collect + redact desktop client surface fingerprint.

    Returns (extra check results, fingerprint dict or None).
    The fingerprint is returned only when it passes the _surface_redaction gate;
    on a violation the fingerprint is None and a FAIL CheckResult is added (its
    detail is sanitized and contains no raw value).
    """
    # deferred import: only session mode needs these two modules
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    try:
        from _session_fingerprint import collect_session_fingerprint
        from _surface_redaction import check_fingerprint
    except ImportError as exc:
        return (
            [
                CheckResult(
                    "FAIL",
                    "session_fingerprint",
                    f"cannot import session-fingerprint deps: {exc}",
                    fix="re-clone AQG checkout (scripts/_session_fingerprint.py + scripts/_surface_redaction.py both required)",
                )
            ],
            None,
        )

    fingerprint = collect_session_fingerprint(
        aqg_root=aqg_root,
        claude_skills_dir=Path(args.claude_skills_dir).expanduser(),
        codex_skills_dir=Path(args.codex_skills_dir).expanduser(),
        probe_cli=not args.no_cli,
    )
    redaction = check_fingerprint(fingerprint)
    if redaction.is_safe:
        return (
            [
                CheckResult(
                    "PASS",
                    "session_fingerprint_redaction",
                    f"{len(fingerprint)} surfaces, no violations",
                )
            ],
            fingerprint,
        )
    # triple-audit gpt-5.5 #1 accepted: do not emit the raw violation string directly.
    # Only surface "N violation(s) in <field-list>" without the value.
    fields = sorted({_extract_field_from_violation(v) for v in redaction.violations})
    return (
        [
            CheckResult(
                "FAIL",
                "session_fingerprint_redaction",
                f"{len(redaction.violations)} violation(s) in: {', '.join(fields)}",
                fix="audit collect_session_fingerprint helpers; ensure no raw value falls into allowlisted leaf fields",
            )
        ],
        None,
    )


def _extract_field_from_violation(violation: str) -> str:
    """Extract the field path from a _surface_redaction violation string.

    violation format: "<full_path>: <reason>", e.g. "claude_md.host: scheme not allowed".
    Returns the "<full_path>" part; if there is no ':', returns the string as-is.
    triple-audit gpt-5.5 #1 accepted: ensures the doctor output never contains a
    raw value snippet.
    """
    if ":" in violation:
        return violation.split(":", 1)[0].strip()
    return violation.strip()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    if args.mode not in WAVE1_IMPLEMENTED_MODES:
        print(
            f"--mode {args.mode}: not implemented (session mode is available; "
            f"other modes extended as needed); see docs/ENGINEERING_FRAMEWORK.md §5",
            file=sys.stderr,
        )
        return EXIT_USAGE

    results, aqg_root = _run_install_mode(args)

    fingerprint: dict | None = None
    if args.mode == "session":
        fp_results, fingerprint = _run_session_fingerprint(args, aqg_root)
        results.extend(fp_results)

    if args.json:
        emit_json(results, fingerprint=fingerprint, mode=args.mode)
    else:
        emit_text(results, fingerprint=fingerprint, mode=args.mode)

    return 0 if all(r.status != "FAIL" for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
