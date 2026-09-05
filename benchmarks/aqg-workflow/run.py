#!/usr/bin/env python3
"""run.py — AQG-workflow benchmark runner.

Produces the numbers the MVP instrument (#320) + feasibility probe (#321/#326)
set up: does an agent running the AQG workflow produce measurably fewer defects
than the same agent without it — and than a few lines of CLAUDE.md rules?

The original three arms (FEASIBILITY.md → "What run.py must do"):
  - baseline        no AQG, isolated out  (--setting-sources project --strict-mcp-config)
  - claude-md-lite  isolated + a few lines of YAGNI/validation rules injected
  - aqg-full        AQG skills available + the AQG CLAUDE.md rules injected
                    (AQG is a SKILL, not an always-on plugin → must inject, not just expose)

WS-7 CARRIED a fourth active control built on a third-party plugin. It was
REMOVED 2026-08-23 on Owner instruction: this study does not benchmark AQG
against other people's plugins. See amendment A5, whose
`retired_third_party_plugin` record resolves the hashes the earlier attempts'
ledgers still carry.

Per cell (one task × one arm × one run): a fresh temp copy of the task seed is
edited by a headless `claude -p` run; the produced file is scored by the task's
held-out adversarial `checks.run_checks`; workflow adherence + cost/turns are
read from the stream-json. The `baseline` (and `claude-md-lite`) cells are
hard-checked to see ZERO `aqg-*` skills — the contamination bug ponytail caught
in its own benchmark (a SessionStart hook firing on every arm).

Design: all scoring / parsing / isolation / command-building / budget logic is
pure and unit-tested with fixtures (test_run.py) — zero API cost. Only `run_cell`
shells out to `claude`. Spending is OFF by default: a plain run prints the plan +
projected cost and stops; pass `--execute` to actually call the API.

Run (no spend):   python3 benchmarks/aqg-workflow/run.py            # dry-run is the default
Run (spends $$):  python3 benchmarks/aqg-workflow/run.py --execute --runs 1
Prove instrument: python3 benchmarks/aqg-workflow/selftest.py
"""
from __future__ import annotations

import argparse
import atexit
import fnmatch
import hashlib
import importlib.util
import itertools
import json
import math
import os
import platform
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, replace as dataclass_replace
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

HERE = Path(__file__).resolve().parent
TASKS_DIR = HERE / "tasks"
PROTOCOL_FILE = HERE / "protocol.json"
DISCLOSED_POST_OUTCOME_CLASSIFICATION = "disclosed_post_outcome_replication"
# These two strings carry what a reader needs in order to judge selection risk,
# so they track the STRONGEST prior observation, not the first one. The
# predecessor promotion changed that: what was observed is no longer a single
# frozen `inconclusive` decision over thirteen cells but a four-arm pass/fail
# summary over 242 cells of this identical matrix. Leaving the wording calibrated
# to the weaker observation would understate the disclosure in the one field a
# published result is required to quote.
# THREE now, not two: the 2026-08-21 attempt scored 254 cells and its four-arm
# summary was read the same way. Counting it is not bookkeeping — the number of
# prior observations is the whole basis on which this study is classified as a
# disclosed post-outcome replication rather than a preregistration, and leaving
# it at two after a third attempt would understate the disclosure in the one
# sentence a reader checks it by (audit 26d0a1c2, five voices of five found the
# promotion had left this class of text behind).
DISCLOSED_POST_OUTCOME_CLAIM_LIMIT = (
    "Three prior aborted outcomes were observed before this protocol: a thirteen-cell "
    "validity-failed attempt whose frozen analyzer decision was inconclusive, a "
    "242-cell attempt of this identical matrix whose four-arm pass/fail summary was read, "
    "and a 254-cell attempt of it whose summary was read the same way. "
    "This is a disclosed post-outcome replication, not an outcome-blind preregistration; "
    "it can only support the fixed-suite replication claim stated in reporting.claim_rule."
)
# The rule is frozen in BOTH places and `load_protocol` refuses unless they match
# exactly, so widening what must be disclosed cannot be done in the protocol alone.
# EXTENDED 2026-08-24 from two observations to four: followup-4's 182-cell three-arm
# summary (the Owner confirmed the run was on an interactive terminal, so it was on
# screen) and the six-cell probe of one task-arm cell run the same day.
DISCLOSED_POST_OUTCOME_CLAIM_RULE = (
    "For this disclosed post-outcome replication, report the new fixed-suite result as supported "
    "only when the AQG-minus-lite difference is at most -10 percentage points and the exact "
    "stratified test passes; report harmful only when the difference is at least +10 percentage "
    "points and that test passes; otherwise report inconclusive. It is not outcome-blind "
    "preregistration evidence and does not establish a population effect of at least 10pp. The "
    "published report must state that pass/fail summaries of FOUR earlier observations at this same "
    "matrix were made before this protocol was frozen -- four-arm summaries of 254 cells on "
    "2026-08-21 and 242 cells on 2026-08-17, the THREE-ARM summary of 182 cells on 2026-08-24 (182 "
    "cells attempted: 60 pass and 121 fail, which are the 181 SCORED cells, plus 1 infrastructure "
    "failure that is not scored; recorded as this study's predecessor after the Owner confirmed the "
    "run was on an interactive terminal, so the summary was on screen), and the six-cell 2026-08-24 "
    "probe of migrate-api-version x baseline (0 pass, 6 fail, mean_fail 2.83), which is six cells "
    "(run_idx 0-5) of one task-arm of this very matrix and is therefore outcome data about one arm "
    "on one task -- that disclosing only some of them does not satisfy this rule, and that TWO "
    "THINGS WERE CHANGED AFTER those observations and neither is a threshold, a primary contrast or "
    "a test: amendment A5 removed the fourth arm on 2026-08-23, after the two four-arm summaries "
    "were read, and this claim rule itself was widened on 2026-08-24 to enumerate the third and "
    "fourth observations. A prior wording of this sentence asserted that nothing had changed after "
    "the observations, which was false on both counts and was refused by three voices of five "
    "(audit 1d8685a3). No task, threshold, primary contrast or test has been changed after any "
    "observation."
)
REPO_ROOT = HERE.parent.parent  # benchmarks/aqg-workflow -> repo root
# aqg-full injects the AQG CLAUDE.md rules (mirrors the Owner setup that MANDATES
# the skills, not just exposes them). This example file is that rules source.
AQG_RULES_FILE = REPO_ROOT / "examples" / "aqg-claude-rules.example.md"
AQG_SKILLS_DIR = REPO_ROOT / "agent-packs" / "claude-code" / "skills"

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
# Per-cell cost estimate (USD), Haiku, isolated — FEASIBILITY.md ("~$0.05 a cell").
PER_CELL_COST_USD = 0.05
# Tools the agent needs: edit the stub (Edit/Write/Read). They are allowlisted
# without `--dangerously-skip-permissions` (forbidden by house rules).
# No shell tool: the benchmark runs hundreds of unattended model turns. `Skill`
# remains available so both plugin treatments are actually invokable. Claude's
# current-directory/`--add-dir` file-access model is recorded and then checked
# against every declared file-tool path; giving an arbitrary model turn Bash
# would instead expose the operator's network and filesystem. Every arm gets
# this same fixed surface.
DEFAULT_ALLOWED_TOOLS = "Edit Write Read Skill"

# Bytecode, dependency and cache directories are execution artifacts, not part
# of a frozen source instrument.  Both the task and plugin provenance hashes
# use this one list so that running selftests cannot alter a source hash.
_FROZEN_SOURCE_IGNORED_PARTS = frozenset({
    ".git", ".in_use", "__pycache__", "node_modules", ".DS_Store", ".idea", ".vscode",
})
# Claude Code 2.1+ convention-loads `hooks/hooks.json` from a plugin even when
# the manifest only declares skills and commands. The benchmark is explicitly
# no-hook, so the ECC active-control source definition omits that surface.
_PLUGIN_IGNORED_PARTS = _FROZEN_SOURCE_IGNORED_PARTS | frozenset({"hooks"})

# aqg-* signal token: skill slugs are lowercase-kebab (aqg-code-construction, …).
_AQG_TOKEN_RE = re.compile(r"/?(aqg-[a-z0-9]+(?:-[a-z0-9]+)*)", re.IGNORECASE)
# The last alternative is the wording Claude Code actually emits, measured on the
# collection host 2026-08-19 by the stage-2 probe:
#   <tool_use_error>File is in a directory that is denied by your permission
#   settings.</tool_use_error>
# The first three wanted "permission denied" ADJACENT, so a real denial scored as
# unprovable and would have invalidated the study on every correctly-blocked
# read. Widened against the measured template, not from memory — three earlier
# paid probes each measured something else (a 256KB size limit, then an anchor
# defect), and widening to either of those would have scored ordinary tool
# errors as proven denials across all 800 cells.
#
# TWO matchers, because the two kinds of wording need opposite defences
# (audit d73430e0, converged).
#
# `_MEASURED_DENIAL_RE` is the sentence Claude Code actually emits, measured on
# the collection host 2026-08-19 by the stage-2 probe on BOTH of its arms:
#   <tool_use_error>File is in a directory that is denied by your permission
#   settings.</tool_use_error>
# It is anchored to the CLI's own envelope and to the whole leading sentence.
# A bare phrase would be forgeable: an ENOENT echoes the model's requested path,
# so a read of `/tmp/denied by your permission/x` would otherwise be scored a
# proven denial. Requiring the full sentence directly after `<tool_use_error>`
# means the stock `File does not exist: {path}` template cannot produce a match
# however the path is spelled. The trailing noun stays free, so a CLI saying
# "permission rules" instead of "permission settings" still matches.
#
# `_LEGACY_DENIAL_RE` holds the three OS-level phrasings. They cannot be
# envelope-anchored — they appear inside errors of many shapes — so they are
# defended the other way, by removing model-authored text before matching (see
# `_explicit_permission_denial`).
#
# NOT widened from memory. Three earlier paid probes each measured something
# else — a 256KB size limit, then a rule-anchor defect — and widening to either
# would have scored ordinary tool failures as proven denials across 800 cells.
#
# KNOWN RESIDUAL, deliberately not covered: a path refused merely because it was
# never GRANTED (rather than named by a deny rule) may use different wording.
# Both probe arms exercised the deny-rule channel, so that wording is unmeasured.
# Its failure mode is fail-closed — such a cell invalidates rather than passing —
# and the fix is to measure it, not to guess (audit d73430e0, 4 of 5 voices).
_MEASURED_DENIAL_RE = re.compile(
    r"<tool_use_error>\s*file\s+is\s+in\s+a\s+directory\s+that\s+is\s+denied"
    r"\s+by\s+your\s+permission\b",
    re.IGNORECASE,
)
_LEGACY_DENIAL_RE = re.compile(
    r"\b(?:permission|access) denied\b|\boperation not permitted\b|\bnot authorized\b",
    re.IGNORECASE,
)
# Model-authored strings shorter than this are not stripped. A string that could
# forge any of these phrasings is necessarily long, while a short one — a
# declared path of "e" — would otherwise delete letters out of a GENUINE denial
# and turn it into a false negative (audit d73430e0, Voice 3).
_STRIPPABLE_MIN_CHARS = 8
# A sentinel that satisfies neither `\s` nor `\b`, so removing a fragment can
# never JOIN its neighbours into a match that was not there.
_STRIP_SENTINEL = "\x00"

_HOST_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.-])(?:~|/)[^\s'\"`]+")
# init-event fields that enumerate the model's capability surface. Used to decide
# whether an init event is rich enough to TRUST an "isolated" verdict — a real
# baseline always lists at least `tools`, so an init with none of these is an
# unexpected schema we must not read as "clean" (audit 9aad54d9 gpt-5.5 f3).
_CAPABILITY_FIELDS = ("slash_commands", "tools", "mcp_servers", "skills", "agents", "commands")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_CONTAMINATED = 3  # an isolation hard-check failed (baseline saw aqg-*, or aqg-full saw none)


# --------------------------------------------------------------------------- #
# Scoring — import the agent-edited solution and run the held-out checks.
# --------------------------------------------------------------------------- #
def _load_callable(py_file: Path, attr: str) -> Callable[..., Any]:
    """Import a single .py file (fresh, not via sys.modules) and return `attr`.

    Mirrors selftest.py's loader. Raises on import failure / missing attr — the
    caller turns that into an 'errored' cell rather than crashing the run."""
    mod_name = f"_bm_{py_file.parent.name}_{py_file.stem}".replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(mod_name, py_file)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot create import spec for {py_file}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return getattr(mod, attr)


@dataclass(frozen=True)
class ScoreResult:
    """Outcome of scoring one solution file against a task's checks."""
    func_name: str
    failures: tuple[str, ...]
    errored: bool          # solution didn't import, or the function is missing/raised
    error: str | None      # the import/scoring error message, if errored
    harness_failed: bool = False  # scorer infrastructure, not model-written solution code
    # Every check this scorer ran, as (check_id, ok).  Empty when the task's
    # checks.py has not been converted to the per-check contract — an empty
    # tuple therefore means "not reported", NOT "no checks ran", and callers
    # must not read it as evidence of anything (2026-08-28: the binary endpoint
    # hid per-check variation in 13 of 24 (task, arm) groups, which is why this
    # field exists at all).
    checks: tuple[tuple[str, bool], ...] = ()

    @property
    def n_failures(self) -> int:
        return len(self.failures)

    @property
    def reports_checks(self) -> bool:
        """True when this task's scorer reported per-check outcomes.

        Distinct from `bool(self.checks)`: a scorer that ran zero checks would
        be an instrument bug, and conflating it with an unconverted task would
        hide that bug behind a legacy code path."""
        return bool(self.checks)

    @property
    def passed(self) -> bool:
        return not self.errored and not self.failures


def score_file(solution_file: Path, checks_file: Path, func_name: str) -> ScoreResult:
    """Score an agent-edited solution with the task's adversarial scorer.

    A solution that won't import (syntax error, lingering NotImplementedError) or
    whose target function is missing is the WORST outcome — `errored`, not a
    crash. `checks.run_checks` already turns per-input exceptions into failures."""
    # Loaded ONCE. Two path-based loads would execute checks.py twice, so any
    # module-level side effect would run twice per scored solution.
    module = _load_scorer_module(checks_file)
    run_all = scorer_entry_point(module, checks_file)
    run_checks = getattr(module, "run_checks", None)
    if run_all is None and not callable(run_checks):
        # Neither entry point: an instrument bug, and it must surface loudly.
        raise AttributeError(f"{checks_file} defines neither run_all_checks nor run_checks")
    try:
        fn = _load_callable(solution_file, func_name)
    except Exception as exc:
        return ScoreResult(func_name, (), True, f"import failed: {type(exc).__name__}: {exc}")
    if run_all is not None:
        try:
            raw = run_all(fn)
        except Exception as exc:
            return ScoreResult(
                func_name, (), True, f"run_all_checks raised {type(exc).__name__}: {exc}")
        # Contract violations are the INSTRUMENT's fault, so they must not be
        # recorded as a failing model cell (audit 39542cac, four voices of
        # four): an earlier revision let them land as `errored` with the
        # message "run_checks raised", which blames the solution for a scorer
        # bug and is indistinguishable in the ledger from a real failure.
        try:
            reported = normalize_check_report(raw)
        except ValueError as exc:
            return ScoreResult(
                func_name, (), True, f"per-check contract violation: {exc}", True)
        return ScoreResult(
            func_name, tuple(d for _, ok, d in reported if not ok), False, None,
            checks=tuple((cid, ok) for cid, ok, _ in reported),
        )
    try:
        failures = run_checks(fn)
    except Exception as exc:
        return ScoreResult(func_name, (), True, f"run_checks raised {type(exc).__name__}: {exc}")
    return ScoreResult(func_name, tuple(failures), False, None)


def _load_scorer_module(py_file: Path) -> Any:
    """Import a scorer module once (fresh, not via sys.modules)."""
    mod_name = f"_bm_{py_file.parent.name}_{py_file.stem}".replace("-", "_").replace(".", "_")
    spec = importlib.util.spec_from_file_location(mod_name, py_file)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise ImportError(f"cannot create import spec for {py_file}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PER_CHECK_CONTRACT_FLAG = "PER_CHECK_CONTRACT"


def scorer_entry_point(module: Any, source: Path) -> Callable[..., Any] | None:
    """Return `run_all_checks`, or None for a task still on the legacy path.

    A missing `run_all_checks` is normally just an unconverted task. But a
    converter who mistypes the name — `run_all_check` — would get exactly the
    same silent fallback to the binary endpoint that this whole contract exists
    to remove, and nothing downstream could ever tell the two apart (audit
    39542cac). So a module that DECLARES the contract must deliver it: set
    `PER_CHECK_CONTRACT = True` in a converted checks.py and a typo becomes a
    loud AttributeError instead of a silent revert.
    """
    entry = getattr(module, "run_all_checks", None)
    declared = bool(getattr(module, PER_CHECK_CONTRACT_FLAG, False))
    if declared and not callable(entry):
        raise AttributeError(
            f"{source} declares {PER_CHECK_CONTRACT_FLAG} but has no callable run_all_checks")
    if entry is not None and not callable(entry):
        raise AttributeError(f"{source} has a non-callable run_all_checks")
    return entry


def _optional_callable(py_file: Path, attr: str) -> Callable[..., Any] | None:
    """`_load_callable` but returns None when the module lacks `attr`."""
    try:
        return _load_callable(py_file, attr)
    except AttributeError:
        return None


def normalize_check_report(reported: Any) -> tuple[tuple[str, bool, str], ...]:
    """Validate a per-check report into (check_id, ok, detail) triples.

    Fail closed on every malformed shape. This runs on trusted scorer output,
    but a silently-dropped or duplicated check id would corrupt the per-check
    endpoint in a way no downstream consumer could detect — the ids are what
    the whole endpoint is keyed on."""
    out: list[tuple[str, bool, str]] = []
    seen: set[str] = set()
    for item in reported:
        if not isinstance(item, (tuple, list)) or len(item) != 3:
            raise ValueError("per-check report entry must be (check_id, ok, detail)")
        cid, ok, detail = item
        if not isinstance(cid, str) or not cid:
            raise ValueError("per-check report needs a non-empty string check id")
        if not isinstance(ok, bool):
            raise ValueError(f"per-check report for {cid!r} needs a bool outcome")
        if cid in seen:
            raise ValueError(f"per-check report repeats check id {cid!r}")
        seen.add(cid)
        out.append((cid, ok, "" if detail is None else str(detail)))
    if not out:
        raise ValueError("per-check report is empty")
    return tuple(out)


_SCORE_SANDBOX = Path("/usr/bin/sandbox-exec")
_SCORE_SCRIPT = HERE / "score_subprocess.py"

# This is a future-protocol capability, not a migration of the consumed WS-7
# evidence.  Its configuration names a dedicated macOS account and the exact
# roots the authenticated CLI needs; neither raw values nor paths are written
# to a publishable ledger.
DEDICATED_BENCHMARK_PROOF_DIR = Path("/var/db/aqg-ws7")
MODEL_CHILD_SANDBOX = Path("/usr/bin/sandbox-exec")
MODEL_ISOLATION_ENFORCEMENT = "macos_sandbox_exec"
MODEL_ISOLATION_NETWORK_POLICY = "model_service_egress_not_restricted"
_MODEL_SYSTEM_READ_ROOTS = (
    Path("/System"), Path("/usr"), Path("/bin"), Path("/sbin"),
    Path("/Library/Apple"), Path("/private/etc"), Path("/dev"),
)
_MODEL_RUNTIME_PARENT_ROOTS = (
    Path("/Applications"), Path("/Library"), Path("/opt"), Path("/usr/local"),
)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _refuse_unowned_directory(path: Path) -> None:
    """Refuse a recursive-delete target this account does not exclusively own.

    `lstat`, never `stat`: judging a path by what it POINTS AT is how a planted
    symlink passes, because its target is usually something this account does
    own at 0o700 — most of a home directory qualifies.

    The symlink is then refused BY TYPE rather than by its mode. Measured, not
    assumed: on macOS `lstat` reports a symlink as 0o755, so `& 0o022` is 0 and
    a permission test alone would accept one. (0o777 is the Linux value; a
    guard written against it silently does nothing here.)

    An absent path is accepted — the caller creates it 0o700 — so this says
    "if something is already there, it must be ours alone", not "it must exist".
    """
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise UsageError(f"CLI scratch directory could not be inspected: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise UsageError("CLI scratch directory must not be a symlink")
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o022:
        raise UsageError("CLI scratch directory must not be a shared directory this run does not own")


def _sandbox_string(path: Path) -> str:
    """Escape one resolved path for a sandbox-exec profile literal."""
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def _profile_path_clause(action: str, path: Path) -> str:
    resolved = path.resolve()
    predicate = "subpath" if resolved.is_dir() else "literal"
    return f'({action} ({predicate} "{_sandbox_string(resolved)}"))'


def per_account_cli_scratch_dir() -> Path:
    """The scratch the CLI hardcodes, creates before its first event, and dies without.

    Named rather than inlined so the value every guard below is calibrated
    against is one testable thing. Two different drifts are fatal in opposite
    directions: a shallower path turns the per-cell reset into a recursive
    delete of shared system state, and any other path silently stops matching
    the directory the CLI actually creates — the "no init event" abort that
    already cost one real run at 3/800 cells.
    """
    return Path("/private/tmp") / f"claude-{os.getuid()}"


@dataclass(frozen=True)
class ModelIsolationCapability:
    """Ephemeral, account-bound inputs for one isolated primary workspace."""
    workspace_root: Path
    dedicated_home: Path
    auth_roots: tuple[Path, ...]
    runtime_roots: tuple[Path, ...]
    cli_executable: Path | None = None
    plugin_roots: tuple[Path, ...] = ()
    cli_scratch_dir: Path | None = None

    def require_scratch_dir(self) -> Path:
        """Validate the one filesystem path the profile grants outside the cell.

        This is path identity only, and says nothing about lifetime: it
        guarantees that `_model_child_profile`'s grant and
        `reset_cli_scratch_dir`'s delete name the same directory, not that a
        grant is ever accompanied by a reset. The second guarantee is that
        `wrap_model_child_argv` does both in one call and is the only
        production path to a profile — a caller reaching past it into
        `_model_child_profile`, as the tests do, gets no reset.

        There is deliberately no fallback. A capability that never declared a
        scratch fails closed here, because inferring one would hand a write
        grant, and a recursive delete, to whatever CLI scratch the invoking
        account happens to own — which on a developer machine is the live one.
        """
        if self.cli_scratch_dir is None:
            raise UsageError("model isolation requires a declared CLI scratch directory")
        scratch = self.cli_scratch_dir
        if not scratch.is_absolute():
            raise UsageError("CLI scratch directory must be an absolute path")
        # Refuse a symlink at the declared path BEFORE resolving anything. This
        # order is the guard: `Path.resolve()` follows a final-component
        # symlink, so checking the resolved value can never see one, and the
        # caller's recursive delete would follow it to an arbitrary target. The
        # declared parent is world-writable with the sticky bit, so that target
        # is attacker-chosen, and the delete runs once per cell.
        if scratch.is_symlink():
            raise UsageError("CLI scratch directory must not be a symlink")
        # Only now resolve. On macOS `/var` and `/tmp` are symlinks, so an
        # unresolved value silently passes every containment check below and
        # makes the sandbox clause disagree with every other path in the
        # profile. Resolving is safe *because* the final component was just
        # proven not to be a symlink: what comes back is the same directory,
        # spelled canonically.
        resolved_scratch = scratch.resolve()
        if resolved_scratch == Path("/"):
            raise UsageError("CLI scratch directory must not be the filesystem root")
        # Own the delete target. The containment loops below are a deny-list of
        # THIS run's protected roots, and the highest-consequence
        # mis-declaration slips straight past them: nothing protected lives
        # under `/private/tmp` — the workspace is a `/var/folders` mkdtemp and
        # every other root is under `/Users` — so a declaration that drifted to
        # `/private/tmp` itself would pass every clause, hand `file*` on all of
        # it to the child, and rmtree shared system state once per cell.
        # Enumerating what must survive a recursive delete cannot work; requiring
        # the target to be ours does. An absent scratch is safe by construction:
        # `reset_cli_scratch_dir` creates it 0o700 below.
        #
        # BOTH spellings, because each catches what the other cannot. The
        # declared path catches a symlink planted in the window after the
        # `is_symlink` check above, which no single check before the delete can
        # cover. The resolved path catches a declaration that only NORMALISES
        # onto a shared root — `/private/tmp/claude-x/..` lstats as ENOENT and
        # would sail through a declared-only check, then resolve to
        # `/private/tmp` and be deleted.
        _refuse_unowned_directory(scratch)
        _refuse_unowned_directory(resolved_scratch)
        # A scratch that CONTAINS any of these takes it down with the delete.
        containing = (
            self.workspace_root, self.dedicated_home,
            *self.auth_roots, *self.runtime_roots, *self.plugin_roots,
        )
        # A scratch INSIDE these deletes part of a tree that must stay intact.
        # `workspace_root` is excluded on purpose: it is the runner's own
        # ephemeral per-run directory, so a scratch beneath it is legitimate.
        inside = (
            self.dedicated_home, *self.auth_roots, *self.runtime_roots, *self.plugin_roots,
        )
        for other in containing:
            resolved = other.resolve()
            if resolved_scratch == resolved or _is_under(resolved, resolved_scratch):
                raise UsageError("CLI scratch directory must not contain a protected isolation root")
        for other in inside:
            if _is_under(resolved_scratch, other.resolve()):
                raise UsageError("CLI scratch directory must not sit inside a protected isolation root")
        # The DECLARED path, deliberately — every check above ran on the
        # resolved value, but the delete must not. `shutil.rmtree` refuses a
        # symlinked root, and that refusal is live only while the root it is
        # handed is still a symlink; give it the resolved value and it sees an
        # already-followed real directory with nothing left to refuse. That is
        # the only cover for the window between the `is_symlink` check above
        # and the delete itself, which matters because on a fresh account the
        # scratch does not exist yet, so the name is unclaimed and the sticky
        # bit on the parent protects nothing. Callers that need a canonical
        # string for the sandbox profile resolve it themselves.
        return scratch

    def with_plugin_roots(self, roots: Sequence[Path]) -> ModelIsolationCapability:
        """Add only frozen, symlink-free plugin snapshots as read-only inputs."""
        validated: list[Path] = []
        for root in roots:
            resolved = root.resolve()
            if root.is_symlink() or not resolved.is_dir():
                raise UsageError("model isolation plugin root is invalid")
            try:
                if any(path.is_symlink() for path in resolved.rglob("*")):
                    raise UsageError("model isolation plugin root contains a symbolic link")
            except OSError as exc:
                raise UsageError("model isolation plugin root cannot be inspected") from exc
            if resolved not in validated:
                validated.append(resolved)
        if not validated:
            raise UsageError("model isolation requires frozen plugin roots before primary reservation")
        # `replace` rather than a positional rebuild: a field added to this
        # dataclass must not be silently dropped by the derived per-arm
        # capability, which is what the child actually runs under.
        return dataclass_replace(self, plugin_roots=tuple(sorted(validated, key=str)))

    def public_receipt(self) -> dict[str, str]:
        """Return publishable enforcement facts without paths or account identity."""
        return {
            "phase": "capability_ready",
            "enforcement": MODEL_ISOLATION_ENFORCEMENT,
            "network_policy": MODEL_ISOLATION_NETWORK_POLICY,
            "profile_schema": "default_deny_v1",
            # Default-deny is not the whole truth and a receipt that says only
            # that overstates the isolation. Two states outside the cell are
            # reachable, and they differ: one is emptied before every child,
            # the other is not scoped by this profile at all.
            "shared_state_outside_cell": (
                "per-account CLI scratch, emptied before every child; "
                "account keychain, not isolated per cell"
            ),
        }


@dataclass(frozen=True)
class DedicatedAccountProof:
    """Root-owned, non-secret platform configuration for one benchmark account."""
    auth_roots: tuple[Path, ...]
    runtime_roots: tuple[Path, ...]


def model_isolation_for_arm(
    capability: ModelIsolationCapability,
    arm_name: str,
    *,
    aqg_plugin_root: Path,
) -> ModelIsolationCapability:
    """Grant a model child only the frozen plugin snapshot for its own arm."""
    if arm_name == "aqg-full":
        return capability.with_plugin_roots((aqg_plugin_root,))
    if arm_name in {"baseline", "claude-md-lite"}:
        return capability
    raise UsageError(f"unknown model-isolation arm: {arm_name}")


def _declared_roots(
    value: str | None,
    *,
    label: str,
    allowed_parent_roots: Sequence[Path] | None = None,
    required_parent: Path | None = None,
    require_cli_auth_state: bool = False,
) -> tuple[Path, ...]:
    if not value:
        raise UsageError(f"{label} declaration is required for model isolation")
    required_resolved = required_parent.resolve() if required_parent is not None else None
    allowed_resolved = (
        tuple(parent.resolve() for parent in allowed_parent_roots)
        if allowed_parent_roots is not None else None
    )
    roots: list[Path] = []
    for rendered in value.split(os.pathsep):
        raw = Path(rendered)
        if not rendered or not raw.is_absolute():
            raise UsageError(f"{label} must contain only absolute existing paths")
        resolved = raw.resolve()
        if resolved == Path("/") or not resolved.exists():
            raise UsageError(f"{label} must contain only absolute existing paths")
        if required_resolved is not None and (
                resolved == required_resolved or not _is_under(resolved, required_resolved)):
            raise UsageError(f"{label} must stay below the dedicated benchmark home")
        if allowed_resolved is not None and not any(
                resolved != parent and _is_under(resolved, parent) for parent in allowed_resolved):
            raise UsageError(f"{label} contains an unapproved runtime path")
        if require_cli_auth_state and required_resolved is not None:
            relative = resolved.relative_to(required_resolved)
            if not any("claude" in part.casefold() for part in relative.parts):
                raise UsageError(f"{label} must name explicit Claude authentication state")
        if resolved not in roots:
            roots.append(resolved)
    if not roots:
        raise UsageError(f"{label} declaration is required for model isolation")
    return tuple(sorted(roots, key=str))


def _proof_path_values(value: Any, *, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(item, str) and item for item in value):
        raise UsageError(f"dedicated benchmark proof has invalid {label}")
    if any(os.pathsep in item for item in value):
        raise UsageError(f"dedicated benchmark proof has invalid {label}")
    return tuple(value)


def _load_dedicated_account_proof(
    account: str,
    home: Path,
    *,
    proof_path: Path | None = None,
    runtime_parent_roots: Sequence[Path] = _MODEL_RUNTIME_PARENT_ROOTS,
) -> DedicatedAccountProof:
    """Load only a root-owned platform declaration, never account credentials."""
    path = proof_path if proof_path is not None else DEDICATED_BENCHMARK_PROOF_DIR / f"{account}.json"
    try:
        metadata = os.lstat(path)
    except OSError as exc:
        raise UsageError("dedicated benchmark account proof is required") from exc
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0
            or metadata.st_mode & 0o022 or metadata.st_size > 8192):
        raise UsageError("dedicated benchmark account proof is invalid")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError("dedicated benchmark account proof is invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise UsageError("dedicated benchmark account proof is invalid")
    if payload.get("account") != account or payload.get("home") != str(home):
        raise UsageError("dedicated benchmark account proof does not match this process")
    auth_roots = _declared_roots(
        os.pathsep.join(_proof_path_values(payload.get("auth_roots"), label="auth_roots")),
        label="model authentication roots", required_parent=home, require_cli_auth_state=True,
    )
    runtime_roots = _declared_roots(
        os.pathsep.join(_proof_path_values(payload.get("runtime_roots"), label="runtime_roots")),
        label="model runtime roots", allowed_parent_roots=runtime_parent_roots,
    )
    return DedicatedAccountProof(auth_roots, runtime_roots)


def _current_account_identity() -> tuple[str, Path]:
    try:
        account = pwd.getpwuid(os.getuid())
    except (KeyError, OSError) as exc:
        raise UsageError("cannot verify the dedicated benchmark account identity") from exc
    return account.pw_name, Path(account.pw_dir).resolve()


def preflight_model_isolation_capability(
    workspace_root: Path,
    *,
    parent_env: dict[str, str] | None = None,
) -> ModelIsolationCapability:
    """Validate OS isolation inputs before a primary can reserve an attempt.

    This only builds a capability; it does not launch the CLI, inspect an auth
    file, or claim that network egress is blocked.  A future protocol binds the
    redaction-safe receipt after a dedicated-account platform check.
    """
    if sys.platform != "darwin" or not MODEL_CHILD_SANDBOX.is_file():
        raise UsageError("model isolation requires macOS sandbox-exec")
    source = os.environ if parent_env is None else parent_env
    try:
        child_env = clean_child_env(source)
    except UsageError as exc:
        raise UsageError("dedicated benchmark account environment is incomplete") from exc
    declared_home = Path(child_env["HOME"])
    if not declared_home.is_absolute():
        raise UsageError("dedicated benchmark account home is invalid")
    declared_home = declared_home.resolve()
    actual_account, actual_home = _current_account_identity()
    if (not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", actual_account)
            or actual_home != declared_home or declared_home != Path("/Users") / actual_account):
        raise UsageError("dedicated benchmark account identity does not match this process")
    root = workspace_root.resolve()
    if not root.is_dir() or _is_under(root, Path("/Users")):
        raise UsageError("model isolation workspace must be a private non-home directory")
    proof = _load_dedicated_account_proof(actual_account, declared_home)
    auth_roots = proof.auth_roots
    runtime_roots = proof.runtime_roots
    claude = shutil.which("claude", path=child_env["PATH"])
    if claude is None or not Path(claude).resolve().is_file():
        raise UsageError("model isolation requires a resolved Claude runtime")
    if not any(_is_under(Path(claude).resolve(), runtime_root) for runtime_root in runtime_roots):
        raise UsageError("model runtime roots do not cover the Claude executable")
    return ModelIsolationCapability(
        root, declared_home, auth_roots, runtime_roots, Path(claude).resolve(),
        # Declared here, once, by the only production constructor: the profile
        # grants exactly this and `reset_cli_scratch_dir` empties exactly this.
        # The account is already pinned to this process above, so its uid is the
        # uid the child will run as.
        cli_scratch_dir=per_account_cli_scratch_dir(),
    )


def _model_child_profile(
    capability: ModelIsolationCapability, cell_dir: Path, *, read_root: Path | None = None,
) -> str:
    """Return a default-deny filesystem profile for one model-child cwd."""
    cell = cell_dir.resolve()
    workspace_root = capability.workspace_root.resolve()
    if not cell.is_dir() or not _is_under(cell, workspace_root):
        raise UsageError("model isolation cell must stay below its private workspace root")
    readable = (read_root if read_root is not None else cell).resolve()
    if (not readable.is_dir() or not _is_under(readable, workspace_root)
            or not _is_under(cell, readable)):
        raise UsageError("model isolation read root must contain its private cell")
    read_roots = tuple(sorted(
        {*(_MODEL_SYSTEM_READ_ROOTS), *capability.runtime_roots, *capability.plugin_roots}, key=str,
    ))
    # The platform profile supplies the macOS process/bootstrap primitives a
    # dynamically linked CLI needs. It is NOT filesystem-neutral: system.sb
    # carries its own `allow file-read*` clauses, so "default-deny plus the
    # explicit clauses below" understates what a child can read. The clauses
    # below are what this runner ADDS; the deny rules below cover the
    # readable namespaces a model has no reason to touch, so an attempt there is
    # a provable denial rather than a silent success.
    clauses = [
        "(version 1)", "(deny default)", '(import "system.sb")',
        "(allow process*)", "(allow network*)",
    ]
    clauses.extend(_profile_path_clause("allow file-read*", root) for root in read_roots)
    clauses.extend(_profile_path_clause("allow file-read*", root) for root in capability.auth_roots)
    # The CLI keeps its account credential in the macOS keychain, not in a file
    # below an auth root, so a filesystem-only grant leaves a signed-in account
    # reporting `loggedIn: false` and the primary can never reserve an attempt.
    # Reaching it needs securityd *and* the credential store; each alone still
    # fails (measured by hand, see the ADR ablation table — test_run.py locks
    # the filesystem shape, not that ablation).
    #
    # What the filesystem clause bounds is direct file I/O: the child can open
    # this one store, read-only, and no sibling. It does NOT bound what
    # securityd will serve over the mach channel granted on the next line —
    # that boundary is untested (ADR, "Open question"). Treat the child as able
    # to reach anything in this account's keychain.
    clauses.append('(allow mach-lookup (global-name "com.apple.SecurityServer"))')
    # The CLI creates `/tmp/claude-<uid>` for its own settings scratch and dies
    # if it cannot — before emitting a single stream event, which the runner can
    # only report as "no init event". It ignores TMPDIR, so the path itself has
    # to be granted.
    #
    # The one writable FILESYSTEM path outside the cell (the keychain above is
    # reachable too, by a channel this profile cannot scope). It is per-account,
    # so every cell of a run shares it, and `workspace_verdict` does NOT close
    # that hole: it inspects the model's Read/Edit/Write tool events, while the
    # writes this path exists for are the CLI's own and never appear as tool
    # uses. Every production profile is built through `wrap_model_child_argv`,
    # which empties the directory in that same call, so no production child —
    # cell or preflight probe — starts on a dirty one. This function does not
    # itself verify that a reset happened; tests build profiles directly.
    #
    # `subpath`, not `literal`: that same reset pre-creates the root, so what
    # the child needs granted is everything it writes *inside* it; a literal
    # would grant the directory and deny its contents. Requiring the declared
    # path here — not an inferred one — is what stops a profile from granting
    # write access to a directory no reset will clean.
    # Resolved HERE, not by the accessor: sandbox-exec matches canonical paths,
    # while the accessor owes the delete an unresolved one. This resolve is NOT
    # risk-free — if a symlink ever reached it, this is the line that would name
    # the target in a `file*` grant. What keeps it honest is that the accessor
    # refuses a symlink twice, the second time by `lstat` on the declared path
    # so the refusal does not depend on when the link appeared, and in
    # production the only mutable component is the leaf: the parent is
    # `/private/tmp`, which is root-owned and sticky.
    clauses.append(
        f'(allow file* (subpath "{_sandbox_string(capability.require_scratch_dir().resolve())}"))'
    )
    clauses.append(_profile_path_clause(
        "allow file-read*",
        capability.dedicated_home / "Library" / "Keychains" / "login.keychain-db",
    ))
    clauses.append(_profile_path_clause("allow file-read*", readable))
    clauses.append(_profile_path_clause("allow file-write*", cell))
    return " ".join(clauses)


@dataclass
class ModelChildInvocation:
    """Ephemeral `sandbox-exec` argv plus its private profile-file lifecycle."""
    argv: list[str]
    profile_path: Path

    def cleanup(self) -> None:
        try:
            self.profile_path.unlink()
        except FileNotFoundError:
            pass


def reset_cli_scratch_dir(capability: ModelIsolationCapability) -> None:
    """Empty the CLI's account-wide scratch so one cell cannot seed the next.

    The profile must grant this path — the CLI dies before its first stream
    event without it — and the path is per-account, not per-cell. The writes it
    exists for are the CLI's own, so they never reach `workspace_verdict`, which
    only sees the model's file-tool events. Emptying the directory before every
    child is therefore the only thing standing between a shared grant and a
    cross-cell channel no gate in this runner can observe.

    Two limits, both deliberate. First, this deletes a directory shared by the
    whole ACCOUNT, not just by this run. The runner drives cells serially in one
    process, so its own children never overlap — but every other `claude` the
    account starts uses the same path, including an interactive session or an
    editor integration, and this reset will recursively delete that session's
    live state, once per cell, for hours. The recreate check below is not mutual
    exclusion: it sees only the narrow window between this delete and its own
    mkdir, so a collider that arrives after the mkdir passes silently. Run the
    primary from an otherwise idle account (see MODEL_ISOLATION_RUNBOOK.md).

    Second, it empties the scratch, not the account: the keychain the profile
    also grants remains shared across cells, and whether a child can write it is
    still open (see the model-child keychain decision record).
    """
    # Validated, and DECLARED rather than resolved (see `require_scratch_dir`):
    # the accessor refused a symlink it could see, and handing rmtree the
    # unresolved path keeps rmtree's own refusal live for one it could not.
    scratch = capability.require_scratch_dir()
    try:
        shutil.rmtree(scratch)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # `/private/tmp` is world-writable with the sticky bit, so a foreign
        # entry can make this unremovable. Say which errno, rather than leaving
        # an operator to rediscover it.
        raise UsageError(f"CLI scratch directory could not be emptied: {exc}") from exc
    try:
        scratch.mkdir(mode=0o700, parents=True)
    except FileExistsError as exc:
        # Something recreated it between the delete and here. On a per-account
        # path that is any other `claude` this account is running, not just a
        # second benchmark, and this child can no longer claim an empty scratch.
        raise UsageError("CLI scratch directory was recreated during the reset") from exc


def wrap_model_child_argv(
    argv: Sequence[str], *, capability: ModelIsolationCapability, cell_dir: Path,
    read_root: Path | None = None,
) -> ModelChildInvocation:
    """Create an argv with a mode-0600 profile outside the model-readable cell."""
    if not argv or argv[0] != "claude":
        raise UsageError("model isolation only wraps the Claude executable")
    executable = capability.cli_executable
    if executable is None or not executable.is_absolute() or not executable.is_file():
        raise UsageError("model isolation requires a verified absolute Claude executable")
    executable = executable.resolve()
    if not any(_is_under(executable, root) for root in capability.runtime_roots):
        raise UsageError("verified Claude executable is outside the model runtime roots")
    profile_dir = capability.workspace_root.resolve() / ".model-isolation-profiles"
    if profile_dir.is_symlink():
        raise UsageError("model isolation profile directory is invalid")
    try:
        profile_dir.mkdir(mode=0o700, exist_ok=True)
        profile_dir.chmod(0o700)
        fd, rendered = tempfile.mkstemp(prefix="profile-", suffix=".sb", dir=profile_dir, text=True)
        profile_path = Path(rendered)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(_model_child_profile(capability, cell_dir, read_root=read_root))
    except OSError as exc:
        raise UsageError("cannot prepare model isolation profile") from exc
    # Emptied HERE, in the same call that grants it: the profile just written
    # hands the child `file*` on a per-account directory, so every wrapped
    # child — not just a measured cell — must find it empty. Pairing these at
    # the callers instead is what round 2 got wrong, and three of this
    # function's four production callers never reset; ordering alone kept that
    # benign. Deliberately the LAST step: this is the only irreversible thing
    # the function does, so every validating failure path above it leaves the
    # account's scratch untouched.
    reset_cli_scratch_dir(capability)
    return ModelChildInvocation(
        [str(MODEL_CHILD_SANDBOX), "-f", str(profile_path), str(executable), *argv[1:]], profile_path,
    )


def _model_preflight_probe_dir(
    capability: ModelIsolationCapability, *, requested_cwd: Path | None = None,
) -> Path:
    """Create the empty nested cell used by all pre-reservation CLI probes."""
    root = capability.workspace_root.resolve()
    if requested_cwd is not None and requested_cwd.resolve() != root:
        raise UsageError("model isolation preflight cwd must be its private workspace root")
    probe = root / ".model-isolation-probe"
    if probe.is_symlink():
        raise UsageError("model isolation preflight probe directory is invalid")
    try:
        probe.mkdir(mode=0o700, exist_ok=True)
        probe.chmod(0o700)
    except OSError as exc:
        raise UsageError("cannot prepare model isolation preflight probe") from exc
    return probe


def _kill_process_group(pid: int) -> None:
    try:
        os.killpg(pid, 9)
    except ProcessLookupError:
        pass


def _score_profile(*, protected_write_roots: tuple[Path, ...] = ()) -> str:
    """macOS profile for untrusted generated code: no user files and no network.

    The scorer runs from an unguessable, private temporary directory outside
    the checkout. `sandbox-exec` is deprecated and cannot safely authenticate
    the model client itself, so this profile is deliberately limited to the
    post-turn code scorer; it blocks the sensitive user-home and all network
    access while preserving the system Python runtime it needs.
    """
    protected_writes = "".join(
        f' (deny file-write* (subpath "{_sandbox_string(root.resolve())}"))'
        for root in protected_write_roots
    )
    return (
        "(version 1) "
        "(allow default) "
        "(deny network*) "
        "(deny file-read* (subpath \"/Users\")) "
        "(deny file-write* (subpath \"/Users\")) "
        "(deny file-write* (subpath \"/Library\")) "
        "(deny file-write* (subpath \"/usr/local\")) "
        "(deny file-write* (subpath \"/opt\")) "
        "(deny file-write* (subpath \"/private/etc\")) "
        "(deny file* (subpath \"/Volumes\"))"
        f"{protected_writes}"
    )


def score_file_sandboxed(
    solution_file: Path,
    checks_file: Path,
    func_name: str,
    *,
    timeout: int = 10,
    protected_write_roots: tuple[Path, ...] = (),
) -> ScoreResult:
    """Score generated code in a fail-closed process after copying hidden checks.

    Tests may use `score_file` directly for trusted fixture references. Real
    benchmark cells call this function only after the agent has finished, so
    the hidden scorer does not exist in the editable directory during the model
    turn.
    """
    if sys.platform != "darwin" or not _SCORE_SANDBOX.is_file():
        return ScoreResult(func_name, (), True, "macOS sandbox-exec is required for scoring", True)
    if not _SCORE_SCRIPT.is_file():
        return ScoreResult(func_name, (), True, "trusted scorer harness is missing", True)
    if solution_file.is_symlink():
        return ScoreResult(func_name, (), True, "model solution symlink is forbidden")
    if not solution_file.is_file():
        return ScoreResult(func_name, (), True, "model solution file is missing")
    # Never place held-out checks in the model cell. A unique scoring directory
    # also prevents later cells from reading a sibling's copied scorer.
    scoring_dir = Path(tempfile.mkdtemp(prefix="benchscore-"))
    worker_dir = scoring_dir / "worker"
    checks_dir = scoring_dir / "trusted-checks"
    worker_dir.mkdir()
    checks_dir.mkdir()
    copied_solution = worker_dir / "solution.py"
    copied_checks = checks_dir / "checks.py"
    copied_runner = checks_dir / "score_subprocess.py"
    # Freeze one immutable copy before invoking any hidden check.  Each checker
    # call starts a fresh worker, so using the model's mutable workspace file
    # would otherwise let an early call change a later one.
    shutil.copy2(solution_file, copied_solution)
    copied_solution.chmod(0o400)
    shutil.copy2(checks_file, copied_checks)
    shutil.copy2(_SCORE_SCRIPT, copied_runner)
    argv = [
        str(_SCORE_SANDBOX), "-p", _score_profile(
            protected_write_roots=(*protected_write_roots, worker_dir),
        ), sys.executable, "-I",
        str(copied_runner), str(copied_solution), str(copied_checks), func_name,
    ]
    try:
        process = subprocess.Popen(
            argv,
            cwd=str(checks_dir),
            env={"PATH": os.defpath, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb"},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_process_group(process.pid)
            process.communicate()
            return ScoreResult(func_name, (), True, f"sandboxed scorer timed out after {timeout}s", True)
        if process.returncode != 0:
            return ScoreResult(func_name, (), True, "sandboxed scorer failed", True)
        data = json.loads(stdout.strip())
        if not isinstance(data, dict):
            raise ValueError("scorer output is not an object")
        if "report" in data:
            # Converted task: the subprocess transports the report RAW and this
            # is the only place it is validated or derived from, so the two
            # scoring paths cannot disagree about the same solution.
            reported = normalize_check_report(data["report"])
            return ScoreResult(
                func_name,
                tuple(d for _, ok, d in reported if not ok),
                bool(data.get("errored")),
                data.get("error"),
                bool(data.get("harness_failed")),
                checks=tuple((cid, ok) for cid, ok, _ in reported),
            )
        if not isinstance(data.get("failures"), list):
            raise ValueError("missing failures")
        return ScoreResult(
            func_name,
            tuple(str(item) for item in data["failures"]),
            bool(data.get("errored")),
            data.get("error"),
            bool(data.get("harness_failed")),
        )
    except OSError:
        return ScoreResult(func_name, (), True, "sandboxed scorer could not start", True)
    except (json.JSONDecodeError, ValueError, AttributeError):
        return ScoreResult(func_name, (), True, "invalid sandboxed scorer output", True)
    finally:
        # ``communicate`` reaps a normal scorer.  Kill only a still-live group:
        # signalling the already-reaped leader can hit a recycled PGID.
        if "process" in locals() and process.poll() is None:
            _kill_process_group(process.pid)
            process.communicate()
        shutil.rmtree(scoring_dir, ignore_errors=True)


# --------------------------------------------------------------------------- #
# stream-json parsing — metrics + tool/skill adherence from a `claude -p` run.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class RunMetrics:
    """Everything read from one cell's stream-json output."""
    is_error: bool
    result_subtype: str | None
    cost_usd: float | None
    num_turns: int | None
    duration_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cache_creation_tokens: int | None
    cache_read_tokens: int | None
    tool_uses: tuple[str, ...]      # ordered tool_use event names
    skill_uses: tuple[str, ...]     # `skill` arg of every Skill tool_use
    file_tool_paths: tuple[tuple[str, str], ...]  # (tool, declared path), file tools only
    # Aligned with ``file_tool_paths``: True iff the matching tool_result
    # explicitly proves a permission/access denial.  ``None`` means the stream
    # did not prove that the shared deny, rather than (for example) a missing
    # file, blocked the escaping path.
    file_tool_denied: tuple[bool | None, ...]
    # Aligned the same way, and the reason `file_tool_denied` alone is not
    # enough to judge an escape: that flag is False BOTH when the operation
    # succeeded and when it was refused in wording the denial regexes do not
    # cover — a residual `_MEASURED_DENIAL_RE` declares in its own comment.
    # Contamination and a parser gap are opposite conclusions, and the aborting
    # cell of 2026-08-23 recorded nothing that could tell them apart.
    file_tool_errored: tuple[bool | None, ...]
    # How many FILE-TOOL OCCURRENCES had a tool_use_id the stream reused, so no
    # result could be attributed to them (issue #609). Those occurrences carry
    # None in both tuples above: the correlation is by id, and a reused id makes
    # "which result describes this use" unanswerable. A COUNT, not the ids —
    # they are opaque CLI tokens, and the ledger's rule is class, not content.
    #
    # Occurrences, aligned with the tuples above, so one Read declaring two
    # paths contributes two. And FILE-tool only: an id reused purely among, say,
    # two Bash uses corrupts nothing `workspace_check` reads, so it is detected
    # (the predicate counts every tool) but not counted here. That means this
    # number answers "did a reused id reach the gate", not the broader "does the
    # CLI ever reuse ids at all"; #609 asks the second and this field does not
    # close it.
    ambiguous_tool_use_correlations: int
    init_event: dict[str, Any] | None
    has_result: bool                # a terminal result event was seen
    truncated: bool                 # hit max turns / error result
    parse_errors: int               # malformed stream lines (logged, never silently dropped)


def _walk_tool_uses(content: Any) -> Iterator[dict[str, Any]]:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                yield block


def _walk_tool_results(content: Any) -> Iterator[dict[str, Any]]:
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "tool_result":
                yield block


def _text_values(value: Any) -> Iterator[str]:
    """Yield untrusted tool-result text only while parsing ephemeral raw data."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, list):
        for item in value:
            yield from _text_values(item)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _text_values(item)


def denial_wording_recognised(text: str) -> bool:
    """Does this text carry a denial wording the runner recognises?

    Diagnostic only, for the stage-2 probe's report. It deliberately does NOT
    apply the model-authored-text strip: the probe is asking "would the runner
    know this wording", not "is this a proven denial".
    """
    return bool(_MEASURED_DENIAL_RE.search(text) or _LEGACY_DENIAL_RE.search(text))


def _without_model_text(text: str, declared: Sequence[str]) -> str:
    """Remove model-authored strings, case-insensitively and longest-first.

    Case-insensitively because the matchers are IGNORECASE and a plain
    `str.replace` is not, so a differently-cased echo would survive.
    Longest-first so a short fragment cannot pre-empt a longer one.
    """
    fragments = sorted(
        {value for value in declared
         if isinstance(value, str) and len(value) >= _STRIPPABLE_MIN_CHARS},
        key=len, reverse=True)
    for fragment in fragments:
        pattern = re.compile(re.escape(fragment), re.IGNORECASE)
        text = pattern.sub(_STRIP_SENTINEL, text)
    return text


def _explicit_permission_denial(result: dict[str, Any],
                                declared: Sequence[str] = ()) -> bool:
    """Distinguish a proven permission block from any other file-tool error.

    `declared` is what the MODEL authored in the tool input — the requested
    path(s) and, for Edit, the search/replace strings. Tool errors echo those
    back, so without removing them a model could read
    `/tmp/permission denied/x`, collect the ENOENT that quotes it, and have its
    out-of-boundary attempt scored as a proven denial and TOLERATED — laundering
    a real escape attempt into a clean cell. Five of five voices raised this in
    audit d7caadfb; five more found the first strip incomplete in d73430e0.

    Defence in depth: the measured CLI sentence is additionally anchored to its
    own envelope, so it cannot be assembled out of an echoed path even if the
    strip misses; the legacy OS phrasings cannot be anchored and rely on the
    strip alone.
    """
    if result.get("is_error") is not True:
        return False
    for text in _text_values(result.get("content")):
        clean = _without_model_text(text, declared)
        if _MEASURED_DENIAL_RE.search(clean) or _LEGACY_DENIAL_RE.search(clean):
            return True
    return False


def _file_paths(value: Any) -> list[str]:
    """Extract declared path values from one file-tool input structurally."""
    paths: list[str] = []
    if isinstance(value, (list, tuple)):
        for item in value:
            paths.extend(_file_paths(item))
        return paths
    if not isinstance(value, dict):
        return paths
    for key, item in value.items():
        if key.lower() in {"file_path", "path", "filename", "directory"} and isinstance(item, str):
            paths.append(item)
        elif isinstance(item, (dict, list, tuple)):
            paths.extend(_file_paths(item))
    return paths


def parse_stream_json(lines: list[str]) -> RunMetrics:
    """Parse `--output-format stream-json --verbose` output (one JSON obj/line).

    Robust to blank/malformed lines (counted in `parse_errors`, not dropped
    silently) and to a missing result event (`has_result=False`)."""
    tool_uses: list[str] = []
    skill_uses: list[str] = []
    file_tool_uses: list[tuple[str, str, str | None]] = []
    tool_result_denials: dict[str, bool] = {}
    tool_result_errored: dict[str, bool | None] = {}
    # Counted over EVERY tool use, not only file tools: the result dicts are
    # keyed by id regardless of tool kind, so a Bash use sharing an id with a
    # Read use corrupts the Read's correlation just as surely as two Reads do.
    tool_use_id_counts: dict[str, int] = {}
    tool_result_id_counts: dict[str, int] = {}
    declared_by_use_id: dict[str, list[str]] = {}
    init_event: dict[str, Any] | None = None
    parse_errors = 0
    is_error = False
    result_subtype: str | None = None
    cost_usd = num_turns = duration_ms = None
    in_tok = out_tok = cc_tok = cr_tok = None
    has_result = False

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            parse_errors += 1
            continue
        if not isinstance(obj, dict):
            parse_errors += 1
            continue
        etype = obj.get("type")
        if etype == "system" and obj.get("subtype") == "init":
            init_event = obj
        elif etype == "assistant":
            msg = obj.get("message")
            if not isinstance(msg, dict):
                parse_errors += 1
                continue
            for tu in _walk_tool_uses(msg.get("content")):
                name = tu.get("name") or ""
                tool_uses.append(name)
                use_id = tu.get("id")
                if isinstance(use_id, str):
                    tool_use_id_counts[use_id] = tool_use_id_counts.get(use_id, 0) + 1
                tool_input = tu.get("input")
                if tool_input is not None and not isinstance(tool_input, dict):
                    parse_errors += 1
                    continue
                if name in {"Read", "Edit", "Write"}:
                    paths = _file_paths(tool_input)
                    tool_use_id = tu.get("id") if isinstance(tu.get("id"), str) else None
                    file_tool_uses.extend((name, path, tool_use_id) for path in paths)
                    if tool_use_id is not None:
                        # Paths AND the other model-authored strings an Edit
                        # error can quote back (audit d73430e0: `old_string` was
                        # an open forgery channel because it is not a path).
                        authored = list(paths) + [
                            value for key, value in (tool_input or {}).items()
                            if key in {"old_string", "new_string", "content"}
                            and isinstance(value, str)]
                        declared_by_use_id.setdefault(tool_use_id, []).extend(authored)
                    if not paths:
                        # A known file tool without a path is an unknown stream
                        # schema, never evidence that it stayed in the cell.
                        file_tool_uses.append((name, "", tool_use_id))
                if name == "Skill":
                    skill = (tool_input or {}).get("skill")
                    if isinstance(skill, str) and skill:
                        skill_uses.append(skill)
        elif etype == "user":
            msg = obj.get("message")
            if not isinstance(msg, dict):
                parse_errors += 1
                continue
            for result in _walk_tool_results(msg.get("content")):
                tool_use_id = result.get("tool_use_id")
                if isinstance(tool_use_id, str):
                    tool_result_id_counts[tool_use_id] = (
                        tool_result_id_counts.get(tool_use_id, 0) + 1)
                    tool_result_denials[tool_use_id] = _explicit_permission_denial(
                        result, declared_by_use_id.get(tool_use_id, ()))
                    raw_error = result.get("is_error")
                    tool_result_errored[tool_use_id] = (
                        raw_error if isinstance(raw_error, bool) else None)
        elif etype == "result":
            has_result = True
            result_subtype = obj.get("subtype")
            is_error = bool(obj.get("is_error"))
            cost_usd = obj.get("total_cost_usd")
            num_turns = obj.get("num_turns")
            duration_ms = obj.get("duration_ms")
            usage = obj.get("usage") or {}
            if not isinstance(usage, dict):
                parse_errors += 1
                usage = {}
            in_tok = usage.get("input_tokens")
            out_tok = usage.get("output_tokens")
            cc_tok = usage.get("cache_creation_input_tokens")
            cr_tok = usage.get("cache_read_input_tokens")

    truncated = bool(result_subtype and result_subtype != "success")

    def _ambiguous(use_id: str) -> bool:
        """Did the stream reuse this id, in either direction?

        Two uses sharing an id means one result must stand for both; two results
        sharing an id means one use has two answers. Either way the pairing is
        not recoverable, and the previous behaviour — last write wins — resolved
        it to whichever line was parsed last. In the ordering where an escaping
        operation SUCCEEDED and a later one was provably denied, that handed the
        escape a proven denial and `workspace_check` tolerated it: a fail-open
        on the check that protects cross-cell isolation (issue #609).

        Returning True here makes both `file_tool_denied` and
        `file_tool_errored` None for every occurrence of the id, which is the
        fail-closed direction: `workspace_check` tolerates an out-of-boundary
        operation only on True.

        Assumes each stream block is walked exactly once — `_walk_tool_uses`
        and `_walk_tool_results` iterate one parsed line's content, and each
        line is parsed once. If a stream ever re-emitted a message envelope
        (a partial followed by a final, say), its blocks would count twice and
        this would poison an id nothing actually reused. That direction is safe
        but not free: the cell fails closed and the matrix aborts, so it would
        show up as an abort with `ambiguous_tool_use_correlations` non-zero
        rather than as a silent wrong answer.
        """
        return (tool_use_id_counts.get(use_id, 0) > 1
                or tool_result_id_counts.get(use_id, 0) > 1)

    return RunMetrics(
        is_error=is_error,
        result_subtype=result_subtype,
        cost_usd=cost_usd,
        num_turns=num_turns,
        duration_ms=duration_ms,
        input_tokens=in_tok,
        output_tokens=out_tok,
        cache_creation_tokens=cc_tok,
        cache_read_tokens=cr_tok,
        tool_uses=tuple(tool_uses),
        skill_uses=tuple(skill_uses),
        file_tool_paths=tuple((tool, path) for tool, path, _ in file_tool_uses),
        file_tool_denied=tuple(
            tool_result_denials.get(tool_use_id)
            if tool_use_id is not None and not _ambiguous(tool_use_id) else None
            for _, _, tool_use_id in file_tool_uses
        ),
        file_tool_errored=tuple(
            tool_result_errored.get(tool_use_id)
            if tool_use_id is not None and not _ambiguous(tool_use_id) else None
            for _, _, tool_use_id in file_tool_uses
        ),
        ambiguous_tool_use_correlations=sum(
            1 for _, _, tool_use_id in file_tool_uses
            if tool_use_id is not None and _ambiguous(tool_use_id)
        ),
        init_event=init_event,
        has_result=has_result,
        truncated=truncated,
        parse_errors=parse_errors,
    )


# --------------------------------------------------------------------------- #
# Isolation hard-check — the ponytail contamination guard.
# --------------------------------------------------------------------------- #
def _aqg_tokens(candidates: set[str]) -> set[str]:
    signals: set[str] = set()
    for candidate in candidates:
        for component in _capability_components(candidate):
            match = _AQG_TOKEN_RE.fullmatch(component)
            if match:
                signals.add(match.group(1).lower())
    return signals


def aqg_capability_signals(metrics: RunMetrics) -> set[str]:
    """AQG tokens the CLI reports as loaded in the init capability surface."""
    if metrics.init_event is None:
        return set()
    candidates = set().union(*(
        _capability_tokens(metrics.init_event.get(field)) for field in _CAPABILITY_FIELDS
    ))
    installed_skills = (
        {
            path.name.casefold() for path in AQG_SKILLS_DIR.iterdir()
            if path.is_dir() and path.name.casefold().startswith("aqg-")
        }
        if AQG_SKILLS_DIR.is_dir() else set()
    )
    return _aqg_tokens(candidates) & installed_skills


def aqg_skill_signals(metrics: RunMetrics) -> set[str]:
    """Every distinct `aqg-*` signal the cell could see — schema-agnostic.

    Availability: any `aqg-*` token in the init event's CAPABILITY collections
    (`slash_commands` / `skills` / `tools` / `mcp_servers` / `agents`, confirmed
    present by the #327 live smoke). Only those fields are scanned — NOT scalar
    fields (cwd, …) nor path-bearing lists like `memory_paths`, whose sandbox
    paths can contain an 'aqg-' substring (e.g. the cell dir `…__aqg-full__r0`)
    and would false-trip the isolation check. Usage: aqg-* skill invocations +
    aqg-* in any tool name."""
    candidates: set[str] = set(metrics.skill_uses) | set(metrics.tool_uses)
    if metrics.init_event is not None:
        for field in _CAPABILITY_FIELDS:
            candidates |= _capability_tokens(metrics.init_event.get(field))
    return _aqg_tokens(candidates)


def _init_has_capability_fields(init_event: dict[str, Any]) -> bool:
    """True iff the init event lists at least one non-empty capability collection.
    A real run always lists `tools`; an init with none is an unrecognized schema we
    must not trust as 'isolated' (fail-closed, audit 9aad54d9 gpt-5.5 f3)."""
    return any(
        isinstance(init_event.get(f), (list, dict)) and init_event.get(f)
        for f in _CAPABILITY_FIELDS
    )


def _capability_tokens(value: Any) -> set[str]:
    """Collect only schema-level capability labels, never nested metadata.

    Capability surfaces sometimes contain objects with paths, descriptions, or
    source locations.  Those values are not capability names and must never
    enter the redaction-safe ledger or analyzer output.
    """
    if isinstance(value, str):
        return {value}
    if isinstance(value, list):
        return set().union(*(_capability_tokens(item) for item in value)) if value else set()
    if isinstance(value, dict):
        # A descriptor object has a narrow label field; its other keys (for
        # example ``source`` / ``path``) are metadata, not capabilities.  A
        # mapping without descriptor fields can express a capability by key
        # (``{"tdd-workflow": true}``).
        name_keys = ("name", "id", "skill", "command")
        tokens = set() if any(key in value for key in name_keys) else {str(key) for key in value}
        for key in name_keys:
            item = value.get(key)
            if isinstance(item, str):
                tokens.add(item)
        return tokens
    return set()


def _capability_components(value: str) -> set[str]:
    """Return exact plugin-namespace components without substring matching."""
    cleaned = value.strip().lstrip("/")
    return {component.casefold() for component in cleaned.split(":") if component}


@dataclass(frozen=True)
class IsolationVerdict:
    ok: bool
    signals: tuple[str, ...]
    detail: str
    unavailable: bool = False


@dataclass(frozen=True)
class WorkspaceVerdict:
    ok: bool
    paths: tuple[str, ...]
    detail: str
    denied_out_of_boundary_attempts: int = 0
    # One coarse class per out-of-boundary event, never the path. This is the
    # only durable record of WHAT escaped: the raw stream is blanked whenever an
    # out-of-boundary event occurs, which is why the 2026-08-17 abort could not
    # be diagnosed afterwards.
    out_of_boundary_classes: tuple[str, ...] = ()
    # Tolerated on an INDEPENDENT POSITIVE FACT rather than on a denial the
    # stream proved: the tool errored AND the runner, which is unsandboxed and
    # holds the resolved path, could see nothing occupied that name. Counted
    # apart from `denied_out_of_boundary_attempts` because the two tolerances
    # rest on different evidence and a reader of the ledger has to be able to
    # tell which one kept a cell.
    absent_out_of_boundary_attempts: int = 0


@dataclass(frozen=True)
class MemoryVerdict:
    ok: bool
    detail: str
    unavailable: bool = False


# macOS mounts the whole data volume here and reaches it by firmlink, so every
# user-visible root has a second, equally real spelling underneath this prefix.
_DATA_VOLUME_FIRMLINK = "/System/Volumes/Data"

# The CLI's own scratch, `claude-<uid>` directly under a tmp root. It is the ONE
# filesystem path outside the cell that `_model_child_profile` grants (`file*`,
# because the CLI dies without it), so it is the one ephemeral path whose
# CONTENTS a model Read can actually reach. Matched by SHAPE rather than by
# `per_account_cli_scratch_dir()`, so the class names exactly the set the
# `//tmp/claude-*/**` deny rules name — one definition of "the scratch", not two
# that can drift apart.
_CLI_SCRATCH_LEAF_RE = re.compile(r"claude-[^/]+")


def _out_of_boundary_class(
    tool: str, resolved: Path, *, cell_root: Path | None = None,
    cell_parent: Path | None = None,
) -> str:
    """Name what an out-of-boundary path could carry, for the durable ledger.

    The class goes in the durable ledger INSTEAD
    of the path: an out-of-boundary path may contain host data, but knowing only
    that "something escaped" is what left the 2026-08-17 abort undiagnosable —
    its raw stream is blanked by design, so the ledger is the only place a
    recurrence can be understood from.

    This records WHICH kind of path escaped, so a recurrence is diagnosable. It
    changes nothing about what is allowed: an out-of-boundary event that the
    stream proves was denied is still counted and tolerated, and one that it
    cannot prove was denied still invalidates the study.
    """
    # No namespace is tolerated. A prefix test cannot express "arm-invariant and
    # private-data-free": `/System/Volumes/Data` is the firmlink mount of the
    # entire data volume, so `/System/Volumes/Data/Users/...` is a lexical
    # descendant of `/System`; and `/usr` contains `/usr/local`, which this file
    # declares a runtime parent root and whose granted contents differ by arm.
    # Provable denial is the mechanism instead: these namespaces are now in
    # CELL_PERMISSION_DENY_RULES, so an attempt there is a counted denial under
    # the pre-existing rule and no validity gate has to be relaxed at all.
    #
    # That same firmlink is why classification cannot test the plain spellings
    # alone. A firmlink is not a symlink, so `Path.resolve()` leaves
    # `/System/Volumes/Data/Users/...` untouched — it would fall through to
    # `other` and report "anywhere else" for the one case this class exists to
    # name. Both spellings of each root are therefore checked.
    def _any_under(*roots: str) -> bool:
        return any(
            _is_under(resolved, Path(prefix + root))
            for root in roots
            for prefix in ("", _DATA_VOLUME_FIRMLINK)
        )

    if _any_under("/Users", "/home"):
        return "user-home"
    # The CLI scratch is split out of `ephemeral-shared` because the two behave
    # oppositely under the sandbox, and one class for both is what made the
    # 2026-08-21 abort still ambiguous after it was classified. A read of the
    # scratch SUCCEEDS — the profile grants it — while every other ephemeral
    # path is refused, and a refusal is provable and therefore tolerated
    # (followup-3 r4, 2026-08-21, CLI 2.1.238: an `ephemeral-shared` attempt with
    # denied_out_of_boundary_file_attempts=1, scored and kept). So a future
    # `cli-scratch` record means a deny rule did not fire, and a future
    # `ephemeral-shared` one means something reached a path the sandbox refused
    # without the stream proving it. Those need different fixes; the old single
    # class could not ask for either. The path is still never retained.
    if _cli_scratch_class(resolved):
        return "cli-scratch"
    # THE RUN'S OWN TREE IS SPLIT OUT OF `ephemeral-shared` for the same reason
    # `cli-scratch` was on 2026-08-21: one class for paths that need different
    # fixes is what left that abort ambiguous after it had been classified. The
    # 2026-08-31 abort (211/660, `merge-allowed-fields x claude-md-lite r10`)
    # repeated it — `ephemeral-shared` was all the ledger could say, and the raw
    # stream that could have disambiguated is blanked by design a few lines into
    # the caller, so the cause was unrecoverable for the third time.
    #
    # The tree (see the `cell_parent` / `cell_root` / `cell_dir` construction):
    #
    #     run root      <tmp>/benchcell-AAAA        shared by every cell
    #      cell parent    benchcell-BBBB            THIS cell only, one child
    #       cell root       <task>__<arm>__...      the read boundary
    #        cell dir         workspace             the write boundary
    #
    # These three need genuinely different fixes, which is the whole point:
    #
    #   `own-cell-parent`  one level up, into a directory whose only child is
    #                      this cell's own root. Carries NO other cell's data,
    #                      so a recurrence here is the case the rejected
    #                      2026-08-21 design sketch identified as removable by
    #                      raising the read boundary to the parent (PR #591,
    #                      audit 55dafb28) — a direction, still unverified.
    #   `sibling-cell`     another cell's tree. This is the contamination the
    #                      hard check exists for and it must keep aborting.
    #   `run-root`         the shared root itself, i.e. an attempt to enumerate.
    #                      Note the model has no Bash/LS/Glob, so this is
    #                      expected to be unreachable; a record here would
    #                      falsify that assumption, which is worth knowing.
    #
    # Classification only. NOTHING about what is tolerated changes: each of
    # these is still out of boundary, still counted, and still invalidates
    # unless the pre-existing denied/absent rules apply. The path is still
    # never retained — only which of these names it fell under.
    # `cell_parent` is PASSED, never derived from `cell_root.parent`. The first
    # version derived it, and CI caught what that costs: a caller whose read
    # root sits directly under a shared tmp root (the absent-arm probe's
    # `/private/tmp/cell`) made `/private/tmp` itself look like this cell's
    # private parent, so a genuinely shared location would have been labelled
    # `own-cell-parent` -- the one class that asserts "carries no other cell's
    # data". Backwards, and in the direction that misleads. The structure is
    # known at the ONE place that builds it, so it is passed from there and the
    # split is simply skipped when a caller cannot vouch for it.
    if cell_root is not None and cell_parent is not None and _is_under(
            cell_root, cell_parent):
        run_root = cell_parent.parent
        # Both spellings, for the same firmlink reason as the fixed roots above:
        # `Path.resolve()` leaves `/System/Volumes/Data/private/tmp/...` alone,
        # so a single-spelling test would drop these back into the generic class
        # and re-create exactly the ambiguity this split exists to remove.
        def _under_either(root: Path) -> bool:
            return any(_is_under(resolved, Path(prefix + str(root)))
                       for prefix in ("", _DATA_VOLUME_FIRMLINK))

        def _is_exactly(root: Path) -> bool:
            return any(resolved == Path(prefix + str(root))
                       for prefix in ("", _DATA_VOLUME_FIRMLINK))

        # Order matters: the cell's own parent is inside the run root, so the
        # narrower test has to run first or every cell-local escape would be
        # reported as a sibling.
        if _under_either(cell_parent):
            return "own-cell-parent"
        if _is_exactly(run_root):
            return "run-root"
        if _under_either(run_root):
            return "sibling-cell"
    # The system-granted /private/var files live here, as does any part of the
    # run tree that the split above could not name (it is skipped when the
    # caller has no cell root to compare against).
    if _any_under("/private/tmp", "/tmp", "/private/var", "/var"):
        return "ephemeral-shared"
    return "other"


def _cli_scratch_class(resolved: Path) -> bool:
    """True when `resolved` is the CLI scratch directory or something inside it.

    Both tmp spellings and both firmlink spellings, for the reason
    `_out_of_boundary_class` checks both everywhere else: a firmlink is not a
    symlink, so `Path.resolve()` leaves `/System/Volumes/Data/private/tmp/...`
    exactly as the model declared it.
    """
    for root in ("/private/tmp", "/tmp"):
        for prefix in ("", _DATA_VOLUME_FIRMLINK):
            base = Path(prefix + root)
            if not _is_under(resolved, base):
                continue
            parts = resolved.relative_to(base).parts
            if parts and _CLI_SCRATCH_LEAF_RE.fullmatch(parts[0]):
                return True
    return False


def target_is_absent_from_the_runner(path: Path) -> bool:
    """Can the runner see that nothing occupies this name?

    Called on the RESOLVED path, which is the safer of the two spellings: a
    symlink pointing at a real file resolves to that file and is seen present,
    where judging the declared name would wave it through as "just a link". A
    dangling link resolves to a target that is not there, and a read through it
    returns nothing — so absence is the right answer for it.

    `lstat` rather than `stat` as the conservative primitive: it answers about
    the name itself and never follows one more link. Stated as a preference and
    not as a behavioural claim — after `resolve()` the two agree on every case
    reachable here, and a mutation swapping them changed no test. The one shape
    that would have separated them, a symlink loop, never reaches this function:
    `resolve()` raises on it and the caller invalidates.

    NOTE this is a different question from the probe's pre-spend check, which
    asks whether a name is free to PLANT into; there a broken link is an
    occupant. Here the question is whether anything could have been read.

    Any error other than "not there" answers False: an unreadable parent, a
    permission problem, a path that cannot be stat-ed at all are all cases where
    the runner does NOT have the positive fact, and the fail-closed reading of
    "I could not look" is "I did not see absence".
    """
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return False


def assert_absent_target_tolerance_premises(protocol: dict[str, Any]) -> None:
    """Refuse a primary run whose tool set breaks the absent-target tolerance.

    The tolerance in `workspace_check` is safe only while the model cannot
    delete a file it has read: otherwise an errored read carrying bytes could be
    followed by a delete, and the check-time absence would buy tolerance for a
    real leak (audit c9206b21, four voices of four). The current tool set —
    Edit, Write, Read, Skill — has no delete. This makes that a CHECKED premise
    rather than an assumed one, so widening the tool set fails loudly here
    instead of silently weakening containment.
    """
    allowed = tuple((protocol.get("matrix") or {}).get("allowed_tools") or ())
    permitted = {"Edit", "Write", "Read", "Skill"}
    extra = sorted(set(allowed) - permitted)
    if extra:
        raise UsageError(
            "the absent-target tolerance assumes the model cannot delete a file it read, "
            f"and the tool set now includes {', '.join(extra)}; re-derive the tolerance "
            "before running, or narrow the tools")


def workspace_check(
    metrics: RunMetrics, read_root: Path, *, write_root: Path | None = None,
    cell_parent: Path | None = None,
) -> WorkspaceVerdict:
    """Fail closed on every declared file operation outside the workspace."""
    read_boundary = read_root.resolve()
    write_boundary = (write_root if write_root is not None else read_root).resolve()
    paths: list[str] = []
    denied_out_of_boundary_attempts = 0
    absent_out_of_boundary_attempts = 0
    out_of_boundary_classes: list[str] = []
    fatal: str | None = None
    def _invalid(detail: str) -> WorkspaceVerdict:
        """Fail this cell while keeping whatever classes were already seen.

        Every failing exit has to carry them. An earlier event's class is the
        only durable trace of what escaped — the raw stream is blanked on any
        out-of-boundary event — so returning a bare verdict from a LATER,
        unrelated malformation would silently discard the one record that makes
        a recurrence diagnosable.
        """
        return WorkspaceVerdict(
            False, (), detail, denied_out_of_boundary_attempts,
            out_of_boundary_classes=tuple(out_of_boundary_classes),
            absent_out_of_boundary_attempts=absent_out_of_boundary_attempts,
        )

    for index, (tool, declared) in enumerate(metrics.file_tool_paths):
        if not declared:
            return _invalid(f"{tool} tool use omitted a file path")
        # pathlib does not expand ``~``; treating it as a relative child would
        # turn a home-directory shorthand into a false in-cell pass.
        if declared.startswith("~"):
            return _invalid(f"{tool} path uses home shorthand")
        candidate = Path(declared)
        try:
            resolved = (candidate.resolve() if candidate.is_absolute()
                        else (write_boundary / candidate).resolve())
        except (OSError, RuntimeError) as exc:
            # `Path.resolve` raises RuntimeError — NOT OSError — on a symlink
            # loop, and nothing caught it: a model-declared looping path would
            # have taken the runner down with a traceback in the middle of a
            # paid matrix instead of producing a verdict. Found while mutation
            # testing the absent-target rule. A path that cannot be resolved
            # cannot be shown to stay inside the boundary, so it invalidates.
            return _invalid(f"{tool} path could not be resolved ({type(exc).__name__})")
        boundary = read_boundary if tool == "Read" else write_boundary
        try:
            relative = resolved.relative_to(boundary)
        except ValueError:
            klass = _out_of_boundary_class(
                tool, resolved, cell_root=read_boundary,
                cell_parent=(cell_parent.resolve() if cell_parent is not None else None))
            out_of_boundary_classes.append(klass)
            denied = metrics.file_tool_denied[index]
            if denied is True:
                # The shared, identical permission deny prevented this attempt.
                # Preserve only a count, never the host path, and reserve a hard
                # invalidation for a successful or unproven external operation.
                denied_out_of_boundary_attempts += 1
                continue
            # THE ABSENT-TARGET TOLERANCE (design §5; Owner 2026-08-26).
            #
            # NOT a wording rule. Audit e8de7a82 established that a permission
            # layer can answer a blocked path with an existence-shaped message,
            # which is why existence-shaped wording is routed to the unmatched
            # branch and must STAY there; widening the denial patterns is
            # forbidden and this does not do it. What this adds is an
            # independent positive fact from a different observer: the runner is
            # unsandboxed and holds the resolved path, so it can look.
            #
            # BOTH conditions, and the second is what the bare sketch in §5 was
            # missing. Absence alone is not enough: `workspace_check` runs after
            # the model process, the class this study keeps dying on is
            # `ephemeral-shared` — sibling cell directories, which get torn down
            # — so a file read SUCCESSFULLY and then removed would look absent
            # here and buy tolerance for real contamination. Requiring the tool
            # to have ERRORED closes that: a successful read means content
            # reached the model whatever the filesystem says afterwards, and an
            # errored read of a name nothing occupies read nothing at all.
            #
            # `errored is True` on purpose: None means the stream never said,
            # which is not the same as "it errored" and must not be tolerated.
            #
            # WHAT THIS RULE DEPENDS ON, and it is outside this function. Four
            # voices of four (audit c9206b21) found the same hole: `is_error` and
            # the result content are independent in the parser, so an error
            # carrying file bytes would be tolerated if the name were also absent
            # at check time. That leak needs the target to have EXISTED when the
            # model read it and to be GONE by now, and three facts of this
            # study's configuration are what close that window:
            #
            #   1. cells run serially — there is no concurrency primitive in
            #      this module, so no sibling cell is torn down while a model is
            #      running;
            #   2. a cell's own teardown runs AFTER its own `workspace_check`,
            #      not before;
            #   3. the model's tool set is Edit/Write/Read/Skill — no Bash, no
            #      delete. It cannot remove a file it just read.
            #
            # `assert_absent_target_tolerance_premises` checks (3) against the
            # protocol before a primary run, so the premise fails loudly if the
            # tool set ever widens. (1) and (2) are pinned by tests. THE RESIDUAL
            # that remains, and it is disclosed rather than argued away: a
            # process outside this study removing the target mid-cell. Nothing
            # here detects that.
            if (metrics.file_tool_errored[index] is True
                    and target_is_absent_from_the_runner(resolved)):
                absent_out_of_boundary_attempts += 1
                continue
            # This verdict goes in the durable ledger.  Do not preserve the
            # declared or resolved escape path: an out-of-workspace model tool
            # use may contain private host data.  The generic invalidation
            # class, tool kind, redacted raw stream, and failed workspace flag
            # are sufficient for fail-closed analysis.
            #
            # WHICH of the three it was is not private, so it is stated — as an
            # OBSERVATION. Each phrase reports what the stream carried and stops
            # there; what it licenses is here, not in the ledger:
            #   no error          -> the operation was not refused. A refusal
            #                        always errors, so this is real contamination
            #                        and invalidating is right.
            #   an unmatched error-> could be a refusal worded outside the two
            #                        denial patterns (the residual
            #                        `_MEASURED_DENIAL_RE` declares about
            #                        itself), in which case a parser gap threw a
            #                        study away — but it could equally be an
            #                        ENOENT, a size limit or a bad tool input,
            #                        so the phrase does NOT say "refusal".
            #   no result / no flag -> neither, and must not be reported as either.
            #
            # WHAT THIS STILL DOES NOT GIVE A READER, stated because an earlier
            # draft of this comment called the three-way split "the whole
            # difference between diagnoses" and three voices of five refused
            # that (audit 02e0b706): the error's TEXT. On the middle branch a
            # reader learns `_MEASURED_DENIAL_RE` may have a gap but not what to
            # add to it, and cannot rule out an ordinary ENOENT. Carrying a
            # path-scrubbed snippet would close that, and would put arbitrary
            # error bytes in a result-PR-adjacent ledger whose rule is
            # class-not-path.
            #
            # THE OWNER DECIDED ON 2026-08-25: NO. The ledger keeps the class
            # and this three-way outcome and never the error text, so the middle
            # branch stays undifferentiated BY DESIGN, not by omission. What
            # that costs is worth stating plainly, because a later reader will
            # want the text and should find the decision instead of a gap: the
            # 2026-08-25 absent-path probe (issue #611) measured that an
            # out-of-boundary read of a name nothing occupies lands on exactly
            # this middle branch, so the branch is known to be reachable and its
            # cause will not be recoverable from the ledger of a future abort.
            # The first-order split — real contamination versus an error whose
            # wording went unmatched — is NOT what was given up; that is the
            # three-way outcome below and it stays. See docs/decisions/LOG.md.
            # `test_the_ledger_never_carries_the_error_text` holds the line.
            #
            # The leading sentence is unchanged, so the frozen 2026-08-17 and
            # 2026-08-21 rows still read the same; a bare sentence with no
            # suffix means the outcome was not captured, which is not the same
            # as any of the three.
            errored = metrics.file_tool_errored[index]
            if errored is None:
                outcome = "the stream does not record whether it errored"
            elif errored:
                outcome = "it errored, and the wording matched no denial pattern"
            else:
                outcome = "the operation returned no error"
            # Remember it and KEEP SCANNING. Returning here made the surviving
            # count "denials proven before the first fatal escape", while the
            # passing path counts them all — two different quantities, which is
            # the same flaw that made the A4 comparison unsound to begin with
            # (audit 02e0b706, Voice 1 f3). Only the FIRST fatal escape is
            # described; every later one still contributes its class.
            if fatal is None:
                fatal = f"{tool} path escapes workspace boundary; {outcome}"
            continue
        # The durable ledger is result-PR-adjacent evidence.  Keep only the
        # cell-relative path; raw host paths remain local to the raw stream.
        paths.append(relative.as_posix())
    if fatal is not None:
        return _invalid(fatal)
    detail = "all declared file-tool paths stay in workspace boundary"
    if out_of_boundary_classes:
        detail = "out-of-boundary file attempts were recorded by class without path retention"
    return WorkspaceVerdict(
        True, tuple(paths), detail, denied_out_of_boundary_attempts,
        out_of_boundary_classes=tuple(out_of_boundary_classes),
        absent_out_of_boundary_attempts=absent_out_of_boundary_attempts,
    )


def memory_check(metrics: RunMetrics, cell_root: Path) -> MemoryVerdict:
    """Reject any declared memory source outside the private cell boundary.

    ``memory_paths`` is deliberately not part of the capability token scan:
    paths can contain an ``aqg-`` arm label.  It is a separate privacy and
    treatment-isolation check and never records the path in the durable ledger.
    """
    if metrics.init_event is None:
        return MemoryVerdict(False, "cannot verify memory isolation: no init event", unavailable=True)
    value = metrics.init_event.get("memory_paths")
    if value is None:
        return MemoryVerdict(True, "no memory paths declared")
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        return MemoryVerdict(False, "memory path evidence is malformed")
    boundary = cell_root.resolve()
    for declared in value:
        if declared.startswith("~"):
            return MemoryVerdict(False, "memory path uses home shorthand")
        candidate = Path(declared)
        resolved = candidate.resolve() if candidate.is_absolute() else (boundary / candidate).resolve()
        try:
            resolved.relative_to(boundary)
        except ValueError:
            return MemoryVerdict(False, "memory path escapes private cell boundary")
    return MemoryVerdict(True, "declared memory paths stay in private cell boundary")


def isolation_check(metrics: RunMetrics, arm: "Arm") -> IsolationVerdict:
    """Enforce each arm's AQG-visibility contract, failing LOUD on uncertainty.

    - require_no_aqg arm (baseline / claude-md-lite): must see 0 aqg-* signals;
      a missing init event means we CANNOT prove isolation → NOT ok (never assume
      isolated — that is exactly the ponytail contamination bug).
    - require_aqg_present arm (aqg-full): must see >=1 aqg-* signal, else AQG was
      never actually loaded and the arm is measuring nothing."""
    signals = aqg_skill_signals(metrics)
    capability_signals = aqg_capability_signals(metrics)
    sig_t = tuple(sorted(signals))
    if arm.require_no_aqg:
        if metrics.init_event is None:
            return IsolationVerdict(
                False, sig_t, "cannot verify isolation: no init event (won't assume clean)", unavailable=True,
            )
        if not _init_has_capability_fields(metrics.init_event):
            return IsolationVerdict(
                False, sig_t,
                "cannot verify isolation: init event lists no capability collection (won't assume clean)")
        if signals:
            return IsolationVerdict(False, sig_t, f"CONTAMINATED: {len(signals)} aqg-* signal(s) visible: {', '.join(sig_t)}")
    if arm.require_aqg_present:
        if metrics.init_event is None:
            return IsolationVerdict(False, sig_t, "cannot verify AQG presence: no init event", unavailable=True)
        if not _init_has_capability_fields(metrics.init_event) or not capability_signals:
            return IsolationVerdict(False, sig_t, "MISCONFIGURED: aqg-full saw 0 aqg-* signals (AQG not loaded)")
        detail = f"AQG present: {len(capability_signals)} init capability signal(s)"
    else:
        detail = "clean: 0 aqg-* signals" if arm.require_no_aqg else "n/a (arm has no AQG-visibility contract)"
    capability_components: set[str] = set()
    if arm.expected_skill_tokens or arm.forbidden_capability_components:
        if metrics.init_event is None:
            return IsolationVerdict(
                False, sig_t, "cannot verify expected third-party capability: no init event", unavailable=True,
            )
        capabilities = set().union(*(
            _capability_tokens(metrics.init_event.get(field)) for field in _CAPABILITY_FIELDS
        ))
        capability_components = set().union(*(
            _capability_components(token) for token in capabilities
        ))
    if arm.forbidden_capability_components:
        leaked = [token for token in arm.forbidden_capability_components
                  if token.casefold() in capability_components]
        if leaked:
            return IsolationVerdict(
                False, sig_t, f"CONTAMINATED: forbidden capability visible: {', '.join(leaked)}",
            )
    if arm.expected_skill_tokens:
        missing = [token for token in arm.expected_skill_tokens
                   if token.casefold() not in capability_components]
        if missing:
            return IsolationVerdict(False, sig_t, f"MISCONFIGURED: missing expected capability: {', '.join(missing)}")
        detail += f"; expected capability present: {', '.join(arm.expected_skill_tokens)}"
    return IsolationVerdict(True, sig_t, detail)


# --------------------------------------------------------------------------- #
# Arms + command building.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Arm:
    name: str
    extra_args: tuple[str, ...] = ()
    append_system_prompt: str | None = None
    require_no_aqg: bool = False
    require_aqg_present: bool = False
    expected_skill_tokens: tuple[str, ...] = ()
    forbidden_capability_components: tuple[str, ...] = ()
    description: str = ""


# claude-md-lite's "few lines" — the YAGNI/validation rules distilled to the one
# carve-out the parse-positive-int task measures (trust-boundary validation),
# without any AQG skills. Answers "isn't AQG just a few lines of CLAUDE.md?".
CLAUDE_MD_LITE_RULES = (
    "Engineering discipline for every change:\n"
    "- YAGNI: build exactly what the ticket needs, no speculative abstraction.\n"
    "- Never simplify away validation at a trust boundary: reject invalid or "
    "out-of-range user input explicitly (raise) instead of passing it through.\n"
    "- Handle the edge cases the ticket implies even when it does not spell them out."
)

_ISOLATION_FLAGS = ("--setting-sources", "project", "--strict-mcp-config")
# These deny rules are a shared execution boundary, not a treatment.  They are
# INTENDED to prevent the model's file tools from reading the authenticated
# user's home and common host-private roots even if a model ignores the
# ticket's workspace rule. What was OBSERVED, once, on 2026-08-19: a `//`-
# anchored read of a /System path came back denied, and an in-cell sentinel came
# back denied. That is not the same as efficacy — see A1 `verified_property` for
# why those observations cannot separate the re-spelling from binary drift, and
# A3 `limitations` for the home class, which is unmeasured (audit cb1ae3ce Voice
# 1 f5 caught this sentence claiming prevention; audit 9d92d22c Voice 4 f4
# caught the replacement still claiming measured efficacy).
# Every filesystem-root rule uses `//`, not `/`.
#
# MEASURED 2026-08-18 on the collection host: with these rules spelled `/X/**`,
# the model READ /System/Library/CoreServices/SystemVersion.plist and the stream
# carried no denial (is_error false, file_tool_denied false, 657 chars of file
# content). That is the whole of what was observed.
#
# INFERRED from the vendor documentation, not measured: that the cause is the
# anchor. Claude Code anchors a SINGLE leading slash at the SETTINGS SOURCE, not
# the filesystem root (https://code.claude.com/docs/en/permissions): "A pattern
# like /Users/alice/file isn't an absolute path ... Use //Users/alice/file for
# absolute paths." Under that rule these 33 resolved somewhere under the cell and
# could not match the host roots they name.
#
# HISTORICAL (superseded 2026-08-20). This paragraph read: the measurement does
# not exclude that the `--settings <JSON string>` payload is not applied at all,
# so the next probe reads a `~/**` path as a POSITIVE CONTROL. Both halves are
# now overtaken. The 2026-08-19 in-cell delivery sentinel closed the
# payload-delivery question attributably, and the Owner ruling of 2026-08-20
# cancelled the `~/**` arm: four probe designs for it were refuted as
# unattributable against the real home. See amendment A1 `verification_required`
# for the formal amendment and its reasons (audit cb1ae3ce, Voice 1 f5).
#
# /var, /private/var and the CLI scratch under /tmp ARE denied as of amendment
# A4 (2026-08-22); see `_EPHEMERAL_READABLE_SUFFIXES` below for what that costs
# and why it is safe. What is still NOT denied is /tmp and /private/tmp
# generally, because the cell tree itself lives there.
#
# This paragraph previously read "Deliberately NOT denied: /var, /tmp and
# /private/** generally. The cell workspace is a mkdtemp under /var/folders".
# It is rewritten rather than annotated because BOTH halves became false, and
# the second half is why `cell_tree_root` exists: an inherited TMPDIR really can
# put the workspace under /var/folders, which is the configuration that
# sentence described. `make_cell_tree` pins it instead of inheriting it.
# /private/etc is listed because it is the real directory /etc symlinks to, and
# neither spelling covers the other.
#
# The model's tool surface is Edit/Write/Read/Skill (protocol matrix
# `allowed_tools`) — no Grep, Glob or Bash — so these three tool names are the
# whole file-touching surface, not a subset of it.
#
# `test_every_filesystem_root_deny_rule_uses_the_double_slash_anchor` guards the
# spelling. It cannot guard the behaviour: that needs the live probe.
# Credential-bearing locations under a macOS home, restated in `//` — the ONE
# anchor form this study has actually exercised.
#
# WHAT THIS CHANGE DOES AND DOES NOT CLAIM. Every rule below is anchored at
# `//Users/*/`, so no rule here can name a path outside `/Users`. That claim
# holds unconditionally, and it is what rules out the misfire audit 440195f7
# V4 f1 raised — a rule landing on the model's own cell. The cell cannot be
# under `/Users` at all: the model-isolation preflight refuses a workspace root
# under `/Users` outright (the "must be a private non-home directory" check
# above), so this is an ENFORCED precondition, not an accident of the default
# TMPDIR (audit cb1ae3ce, Voice 1 f8).
#
# What is NOT claimed — audit cb1ae3ce, 5 voices of 5: that "nothing can be
# newly denied". Under the documented gitignore semantics every rule below is
# already inside the footprint of `Read/Edit(//Users/**)` and this is a pure
# restatement. But the enumeration exists precisely for the world where those
# semantics do NOT hold, and in that world `//Users/**` never reached the dotted
# paths, so these rules do newly deny them — which is the point of the
# restatement, not a hazard. Both arguments cannot be load-bearing at once, so
# only the anchor-prefix one is. Whether any rule here FIRES is unmeasured; see
# amendment A3, which holds the primary gate shut until the enlarged payload is
# shown to be accepted.
#
# The containing footprint is `//Users/**`, NOT `~/**`: these patterns match
# every home under /Users, not only the running account's (Voice 5 f5). `~/**`
# is retained unchanged alongside them.
#
# WHY NOT `~`: the three `~/**` rules are the one class here with no evidence
# of ever firing. They were not part of the `//` re-spelling (they are
# home-relative, not filesystem-root), and four successive probe designs aimed
# at measuring one were refuted as unattributable against the real home — the
# sandbox's own `(deny default)` and the CLI's out-of-workspace gate
# over-determine any denial there (audits eff06574, b5aec617, ae288efd, and
# 440195f7 f1). Owner ruling 2026-08-20: stop paying to measure the tilde;
# express home protection in the anchor that was measured. Two voices of panel
# 440195f7 (V1 f4, V4 f1) independently proposed this exact form. The `~/**`
# catch-all is RETAINED — these narrow within it, they do not replace it.
#
# WHY BOTH A COVER AND AN ENUMERATION: `.*` and `.*/**` dominate the named list
# under the documented gitignore semantics ("`*` matches within a single path
# segment and can appear at any position", https://code.claude.com/docs/en/permissions).
# The enumeration is the hedge against one specific failure: a matcher whose
# `*` refuses to match a leading dot — the same dotted-component doubt that
# motivated this work. If `.*` behaves, the enumeration is redundant; if it
# does not, the enumeration is the protection. Neither is measured, and this
# comment does not claim either fires: see protocol amendment A3. The
# enumeration hedges ONLY that failure mode: it still needs `*` to match the
# account segment, and 26 of its rules still need `/**`, so it is not a
# wildcard-free layer (audit 9d92d22c, Voice 1 f5). It also reaches only
# names sitting directly under a home, not `~/projects/.env` (Voice 1 f6).
#
# Directory names are denied BOTH as the object and as a subtree (`X` and
# `X/**`): under gitignore semantics `X/**` matches what is inside `X`, not `X`
# itself (audit cb1ae3ce, Voice 3 f2). Read-on-a-directory is not a listing
# operation in this tool surface, so this closes a theoretical gap rather than
# a demonstrated one, at two strings per name.
#
# WHY NO `Write(...)` TWIN: "Claude Code checks file permissions against
# Edit(path) and Read(path) rules only. If you write a path rule for Write ...
# Claude Code accepts the rule but never consults it, and warns at startup"
# (same page, v2.1.210+; installed binary 2.1.237). The pre-existing Write
# rules above are inert for the same reason but are NOT removed here: a Read
# deny also blocks the Edit and Write tools on the same path (v2.1.228+), so
# they are decoration rather than a hole, and churning them would perturb the
# exact payload the 2026-08-19 probe measured. Recorded as a limitation in A3.
_HOME_CREDENTIAL_DIRS = (
    ".ssh", ".aws", ".gnupg", ".docker", ".kube", ".config", ".claude",
    ".codex", ".azure", ".terraform.d", ".local", ".cargo", ".gem",
)
_HOME_CREDENTIAL_FILES = (
    ".netrc", ".git-credentials", ".npmrc", ".pypirc", ".gitconfig",
    ".vault-token", ".bash_history", ".zsh_history",
    # Shell startup files are the commonest home location for an exported API
    # key, and were missing while the history files were present (audit
    # cb1ae3ce, Voice 1 f7). They go in BEFORE the A3 acceptance probe runs, or
    # the probe measures a payload the study will not use.
    ".zshrc", ".bashrc", ".bash_profile", ".zprofile", ".profile",
    ".pgpass", ".my.cnf",
)
# `Library/**` is the macOS store that is NOT dotted and so needs no
# dot-traversal at all: Keychain databases, browser cookie stores, per-app
# token caches (audit 440195f7, V1 f5).
_HOME_COVERS = ("Library", "Library/**", ".*", ".*/**")


# TWO namespaces the model-child profile leaves READABLE outside the cell while
# the deny set never named them. Found on 2026-08-22 by reading the two profile
# sources against the deny set, after the SECOND identical abort:
#
#   1. `/private/tmp/claude-<uid>` — the CLI's own scratch. `_model_child_profile`
#      grants it `file*` because the CLI dies without it, so a model Read there
#      SUCCEEDS with nothing to deny it. It is per-account, so every cell of a
#      run shares one directory.
#   2. `/private/var/**` — the imported `system.sb` grants `file-read*` on
#      `db/timezone`, `db/DarwinDirectory/local/recordStore.data`,
#      `db/eligibilityd/eligibility.plist` and `db/CVMS`. (`/tmp` and `/var`
#      themselves get only `file-read-metadata file-test-existence` there, so
#      their CONTENTS were never reachable.)
#
# These two are the paths KNOWN to be reachable and unnamed. They are NOT
# established to be the only ones, and an earlier draft of this comment said
# they were — on the strength of followup-3 r4 (2026-08-21, CLI 2.1.238), which
# recorded an `ephemeral-shared` attempt with
# denied_out_of_boundary_file_attempts=1 and was scored and kept. Four voices of
# four refused that inference (audit 864cf832). r4 shows that SOME ephemeral
# out-of-boundary attempt produced a denial this runner recognises; it does not
# say which path, or which layer denied it — the seatbelt, ordinary filesystem
# permissions, or the CLI's own out-of-workspace gate. And r13 of the same task
# and arm, in the same run, is the counterexample: same class, denial NOT
# proven. That is exactly the shape of an unrecognised refusal wording, which
# the `_MEASURED_DENIAL_RE` block above already records as a known residual for
# paths refused because they were never GRANTED.
#
# So these rules close two paths that are certainly reachable. Whether anything
# else in the ephemeral roots can produce an unproven denial is unmeasured, and
# A4's probe (b) is what measures it.
#
# WHY NOT DENY THE WHOLE ROOT. On the collection account TMPDIR is unset, so
# `tempfile.mkdtemp(prefix="benchcell-")` lands in `/private/tmp` (measured
# 2026-08-22 on that account: `gettempdir` -> `/tmp`, resolved
# `/private/tmp/benchcell-...`). `//private/tmp/**` would deny each cell its own
# workspace. These two rules cannot: `claude-*` does not match `benchcell-*`,
# and no cell is under /var at all. `test_no_added_ephemeral_rule_can_match_a_cell_path`
# pins that against a real cell tree rather than against this paragraph.
#
# WHY THIS DOES NOT BREAK THE CLI. A deny rule governs the model's Read/Edit
# tools; the CLI's own file I/O is governed by the seatbelt profile, which still
# grants the scratch. That layering is not an inference: 254 cells ran on
# 2026-08-21 with `Read(//usr/**)` and `Read(//System/**)` in the payload while
# the CLI read /usr/lib and /System on every one of them.
#
# Read+Edit only, no Write twin — the same vendor rule that shaped
# `_home_credential_denies`: a Write-spelled path rule is accepted, never
# consulted, and warns at startup, while a Read deny already blocks Edit and
# Write on the same path (v2.1.228+).
#
# The firmlink spellings are a HEDGE, not new reach: under the documented `**`
# semantics `Read(//System/**)` already dominates every
# `/System/Volumes/Data/...` path. They are here because those same semantics
# are what the entire deny set rests on, and audit 55dafb28 (Voices 3 and 5)
# caught an earlier sketch of this fix omitting them altogether. Whether either
# spelling FIRES is unmeasured until amendment A4's probe; this comment does not
# claim it does.
# The scratch appears BOTH as the directory object and as its subtree, for the
# reason `_HOME_COVERS` carries `Library` and `Library/**`: under the documented
# gitignore semantics `X/**` matches what is INSIDE X, not X itself (audit
# cb1ae3ce, Voice 3 f2). The profile grants `file*` on the directory, so a Read
# of the bare path is the one case the sandbox would not refuse — and without
# the object form no rule named it, while `_cli_scratch_class` already
# classified it (audit 864cf832, Voices 1 and 3, convergent).
_EPHEMERAL_READABLE_SUFFIXES = (
    "private/tmp/claude-*", "private/tmp/claude-*/**",
    "tmp/claude-*", "tmp/claude-*/**",
    "private/var/**", "var/**",
)


def _ephemeral_readable_denies() -> tuple[str, ...]:
    """Return the ephemeral rules, deterministically ordered (the payload is frozen)."""
    return tuple(
        f"{tool}(//{prefix}{suffix})"
        for suffix in _EPHEMERAL_READABLE_SUFFIXES
        for prefix in ("", "System/Volumes/Data/")
        for tool in ("Read", "Edit")
    )


def _home_credential_denies() -> tuple[str, ...]:
    """Return the home rules, deterministically ordered (the payload is frozen)."""
    suffixes = [part for name in _HOME_CREDENTIAL_DIRS
                for part in (name, f"{name}/**")]
    suffixes += list(_HOME_CREDENTIAL_FILES) + list(_HOME_COVERS)
    return tuple(f"{tool}(//Users/*/{suffix})"
                 for suffix in suffixes for tool in ("Read", "Edit"))


CELL_PERMISSION_DENY_RULES = (
    "Read(~/**)", "Edit(~/**)", "Write(~/**)",
    "Read(//Users/**)", "Edit(//Users/**)", "Write(//Users/**)",
    "Read(//etc/**)", "Edit(//etc/**)", "Write(//etc/**)",
    "Read(//private/etc/**)", "Edit(//private/etc/**)", "Write(//private/etc/**)",
    "Read(//Library/**)", "Edit(//Library/**)", "Write(//Library/**)",
    "Read(//opt/**)", "Edit(//opt/**)", "Write(//opt/**)",
    "Read(//Volumes/**)", "Edit(//Volumes/**)", "Write(//Volumes/**)",
    # The profile grants these to every arm (`_MODEL_SYSTEM_READ_ROOTS` plus
    # system.sb's own read clauses) while the deny set never mentioned them, so
    # an attempt here would have succeeded with no denial the stream could prove.
    # That is a gap found by reading the profile against the deny set, NOT a
    # path the 2026-08-17 abort is known to have taken: that stream is blanked
    # and its ledger kept no path, so which namespace it reached is unknowable
    # and is not claimed here or anywhere else in this contract.
    # Denying them makes such an attempt a provable, counted denial;
    # a denial the stream cannot prove still invalidates, as before.
    "Read(//System/**)", "Edit(//System/**)", "Write(//System/**)",
    "Read(//usr/**)", "Edit(//usr/**)", "Write(//usr/**)",
    "Read(//bin/**)", "Edit(//bin/**)", "Write(//bin/**)",
    "Read(//sbin/**)", "Edit(//sbin/**)", "Write(//sbin/**)",
    "Read(//dev/**)", "Edit(//dev/**)", "Write(//dev/**)",
    *_ephemeral_readable_denies(),
    *_home_credential_denies(),
)


# Where the cell tree is created. PINNED, not inherited.
#
# `tempfile.mkdtemp(prefix="benchcell-")` follows the PARENT's TMPDIR, which on
# macOS is normally `/var/folders/...`. Since A4 denies `//private/var/**`, an
# inherited location would deny each cell its own `seed.py` — and that failure
# does NOT abort: the path is INSIDE the workspace boundary, so `workspace_check`
# passes and the matrix completes as 600 cells whose model could not read its
# own task. Silent invalid data, on the one authorized attempt, is worse than
# the abort this amendment exists to remove (audit 864cf832, three voices of
# four, convergent).
#
# The collection account was measured with TMPDIR unset on 2026-08-22, but one
# session's environment is not a guarantee about the next one's, and this file's
# own older comment asserted the workspace was "a mkdtemp under /var/folders" —
# documentary evidence that the other configuration has been real here.
CELL_TREE_ROOT = Path("/private/tmp")


def make_cell_tree(prefix: str = "benchcell-") -> Path:
    """Create the run's cell-tree root under `CELL_TREE_ROOT` when it exists.

    The fallback to the platform temporary directory is for hosts where that
    root does not exist — Linux CI and unit tests, where no paid cell can run:
    `preflight_primary_environment` requires darwin, and
    `require_cell_tree_outside_the_deny_set` refuses any root a frozen rule could
    match. The pin is the ordinary path; the refusal is the enforcement.
    """
    if CELL_TREE_ROOT.is_dir():
        return Path(tempfile.mkdtemp(prefix=prefix, dir=str(CELL_TREE_ROOT)))
    return Path(tempfile.mkdtemp(prefix=prefix))


def deny_rule_could_match(rule: str, path: Path) -> bool:
    """True when `rule` could match `path` under a matcher LOOSER than the CLI's.

    `fnmatch`'s `*` crosses `/`, and `**` is collapsed to it, so anything the
    real gitignore-style matcher would catch is caught here too. The asymmetry is
    deliberate: this decides whether to REFUSE, so over-matching costs a false
    alarm while under-matching costs the study.

    Home-relative rules are skipped — they cannot name a filesystem root, and a
    workspace under a home is refused by `preflight_model_isolation_capability`
    before this is reached.
    """
    if not rule.endswith(")") or "(" not in rule:
        return False
    pattern = rule.split("(", 1)[1][:-1]
    if not pattern.startswith("//"):
        return False
    return fnmatch.fnmatch(str(path).lstrip("/"), pattern[2:].replace("**", "*"))


def require_cell_tree_outside_the_deny_set(workspace_root: Path) -> None:
    """Refuse a cell tree that any frozen deny rule could reach.

    The one runtime check standing between "TMPDIR happened to be set" and 800
    cells that cannot read their own seed file. It probes a REPRESENTATIVE cell
    path rather than the rules' prose, so a later rule added without thinking
    about the workspace is caught by the same mechanism.
    """
    probe = (workspace_root.resolve() / "benchcell-probe"
             / "task__arm__neutral__r0" / "workspace" / "seed.py")
    offenders = [rule for rule in CELL_PERMISSION_DENY_RULES
                 if deny_rule_could_match(rule, probe)]
    if offenders:
        raise UsageError(
            "the cell tree sits where the frozen deny rules can reach it "
            f"({len(offenders)} rules, first {offenders[0]}); a cell would be denied "
            "its own workspace and the matrix would complete as invalid data rather "
            "than abort. Pin the tree under a root no rule names")


def deny_payload_json(rules: Sequence[str]) -> str:
    """Serialize ANY deny payload in the exact shape a cell sends.

    Split out so a fingerprint can be recomputed from a payload a committed
    report RECORDS, not only from the live tuple. Without it the only way to
    check a historical report's own arithmetic was to compare it with today's
    rules, which stops working the moment the payload is amended — and the
    provenance assertions were simply dropped when A4 amended it (audit
    864cf832, Voice 4 f5).
    """
    return json.dumps({"permissions": {"deny": list(rules)}}, sort_keys=True)


def deny_payload_fingerprint(rules: Sequence[str]) -> str:
    """Digest of an arbitrary recorded deny payload, in the shape a cell sends."""
    return hashlib.sha256(deny_payload_json(rules).encode("utf-8")).hexdigest()


def cell_permission_settings_json(extra_denies: Sequence[str] = ()) -> str:
    """Return the frozen, arm-neutral CLI settings payload for file denies.

    `extra_denies` exists for ONE caller: the stage-2 diagnostic probe, which
    needs a rule that can only have come from this payload in order to tell
    "the deny rules are mis-spelled" from "the payload never reaches the CLI".
    It defaults to empty, so every paid cell emits the frozen payload byte for
    byte — `test_the_paid_payload_is_unchanged_without_extra_denies` pins that.
    """
    return deny_payload_json(list(CELL_PERMISSION_DENY_RULES) + list(extra_denies))


def diagnostic_permission_settings_json(deny_rules: Sequence[str]) -> str:
    """Return a payload that is EXACTLY `deny_rules` and shares nothing with the paid one.

    Two of the three authorized probes cannot use the production payload. The
    tilde probe has to drop the `~/**` catch-all — with it, the control file is
    denied along with the target and the arm measures nothing. The anchor probe
    has to carry the OLD single-slash `/System/**` spelling WITHOUT the
    `//System/**` rule, which would deny its target no matter what the rule
    under test does (audit 9d92d22c, Voice 5 f3 killed an earlier design that
    missed exactly this).

    It is a REPLACEMENT, deliberately, and never a subtraction from
    `CELL_PERMISSION_DENY_RULES`. A parameter that could remove one production
    rule would be a way to weaken the shared execution boundary a rule at a
    time; a diagnostic payload is exactly what it is handed and cannot be
    mistaken for the frozen one. A cell built this way proves nothing about the
    production payload, which is what the acceptance probe is for.
    """
    rules = list(deny_rules)
    if not rules:
        raise UsageError("a diagnostic payload with no deny rule measures nothing")
    return json.dumps({"permissions": {"deny": rules}}, sort_keys=True)


# The primary preflight checks the complete argv surface, including the shared
# prompt-delivery channel. A CLI upgrade must fail before the one authorized
# paid attempt rather than discover an unsupported treatment flag mid-matrix.
PRIMARY_REQUIRED_FLAGS = (
    "--tools", "--allowedTools", "--max-budget-usd", "--plugin-dir", "--add-dir",
    "--setting-sources", "--strict-mcp-config", "--settings", "--no-session-persistence",
    "--append-system-prompt", "--output-format", "--verbose", "--model",
)


# The heading the installable rule block starts at. Everything BEFORE it is
# addressed to a human operator ("`cat` the block below into your Claude Code
# global rules file", a bash one-liner, a PowerShell note, and how to scope it
# to one project) -- 656 of the 16511 injected characters, on 2026-09-02.
#
# WHY THIS EXISTS. The 2026-09-01 pilot measured the aqg-full arm invoking the
# Skill tool 0 times in 120 cells while `aqg_signals` listed all 16 skills in
# 119 of them, and the model never once mentioned an AQG concept. The delivery
# channel is fine (`--append-system-prompt` is emitted; verified by dry run) and
# so is the tool surface (`tools=['Edit','Read','Skill','Write']` in the init
# event). What the model receives is a document explaining to somebody else how
# to INSTALL a rule block -- so the hypothesis is that the treatment administers
# the wrapper rather than the payload.
#
# THAT IS A HYPOTHESIS, NOT A FINDING, and this flag exists to test it rather
# than to act on it. It is opt-in, it is refused for `--primary`, and the frozen
# payload is unchanged byte for byte without it: the protocol pins the sha256 of
# the whole file, and every run to date (followup-5 included) administered the
# preamble, so a run without it is NOT comparable to them.
AQG_RULES_BODY_HEADING = "## Agent Quality Gates (AQG) engineering discipline"


def aqg_rules_body(text: str) -> str:
    """Return the installable rule block, dropping the operator-facing preamble.

    The heading is required to appear EXACTLY once. Splitting on the `---` rule
    would work today and rot the first time a second horizontal rule is added;
    a heading that must be unique fails loudly instead of silently returning a
    different, shorter treatment than the caller asked for.
    """
    count = text.count(AQG_RULES_BODY_HEADING)
    if count != 1:
        raise UsageError(
            f"the AQG rules body heading appears {count} times, expected exactly "
            f"once -- refusing to guess where the installable block starts")
    return text[text.index(AQG_RULES_BODY_HEADING):]


def build_arms(
    aqg_rules_file: Path = AQG_RULES_FILE,
    aqg_plugin_dir: Path | None = None,
    *,
    rules_body_only: bool = False,
) -> dict[str, Arm]:
    """Build the three isolated arms of the matrix.

    A fourth arm ran a third-party plugin as an active control until 2026-08-23,
    when the Owner ruled that this study compares AQG only against Claude's and
    Codex's official tooling, not against other people's plugins (amendment A5).
    The parameter that admitted it is gone rather than defaulted to None: an
    optional arm nothing constructs is an invitation to reconstruct it.
    """
    # Read the AQG rules exactly once while constructing the treatment.  Primary
    # cells must administer a frozen string, never a mutable checkout file.
    aqg_rules = aqg_rules_file.read_text(encoding="utf-8")
    if rules_body_only:
        aqg_rules = aqg_rules_body(aqg_rules)
    arms = {
        "baseline": Arm(
            name="baseline",
            extra_args=_ISOLATION_FLAGS,
            require_no_aqg=True,
            description="no AQG, isolated out (the contamination control)",
        ),
        "claude-md-lite": Arm(
            name="claude-md-lite",
            extra_args=_ISOLATION_FLAGS,
            append_system_prompt=CLAUDE_MD_LITE_RULES,
            require_no_aqg=True,
            description="isolated baseline + a few lines of YAGNI/validation rules",
        ),
        "aqg-full": Arm(
            name="aqg-full",
            extra_args=(_ISOLATION_FLAGS + (("--plugin-dir", str(aqg_plugin_dir))
                                            if aqg_plugin_dir is not None else ())),
            append_system_prompt=aqg_rules,
            require_aqg_present=True,
            description=("explicit AQG plugin + isolated settings + AQG rules" if aqg_plugin_dir is not None
                         else "AQG skills available + the AQG rules injected (mirrors Owner setup)"),
        ),
    }
    return arms


def build_argv(
    arm: Arm,
    prompt: str,
    *,
    model: str = DEFAULT_MODEL,
    allowed_tools: str = DEFAULT_ALLOWED_TOOLS,
    max_budget_usd: float | None = None,
    cell_dir: Path | None = None,
    extra_denies: Sequence[str] = (),
    diagnostic_denies: Sequence[str] | None = None,
) -> list[str]:
    """The exact `claude -p` argv for one cell of `arm`.

    Every flag was re-checked present in `claude --help` on the installed binary
    (2.1.237) on 2026-08-20. The version is recorded as a dated OBSERVATION, not
    a pin: the host CLI has moved twice during this study (2.1.207 -> 2.1.234 ->
    2.1.237), and prose that names a version goes stale silently. What actually
    binds is `require_claude_help`, which re-checks every PRIMARY_REQUIRED_FLAG
    on the installed binary before a paid cell can start."""
    argv = [
        "claude", "-p", prompt,
        "--output-format", "stream-json", "--verbose",
        "--no-session-persistence",
        "--model", model,
    ]
    argv += list(arm.extra_args)
    if diagnostic_denies is not None:
        # An Owner-authorized diagnostic cell, never a paid one:
        # `test_no_paid_call_site_passes_a_diagnostic_payload` asserts the source.
        if extra_denies:
            raise UsageError(
                "a diagnostic payload replaces the frozen rules; appending to it as "
                "well confuses two different probes")
        argv += ["--settings", diagnostic_permission_settings_json(diagnostic_denies)]
    else:
        argv += ["--settings", cell_permission_settings_json(extra_denies)]
    if max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{max_budget_usd:.2f}"]
    if cell_dir is not None:
        argv += ["--add-dir", str(cell_dir)]
    # System-prompt injection: BOTH treatment arms use the SAME flag
    # (--append-system-prompt) so the delivery channel can't confound the ablation
    # (audit 9aad54d9 f3/grok-f4). Both contents are frozen strings assembled
    # before the matrix starts. Identical emitted flag either way.
    injected = arm.append_system_prompt
    if injected is not None:
        argv += ["--append-system-prompt", injected]
    # Both flags pin the tool surface: `--tools` makes Bash unavailable and
    # `--allowedTools` grants non-interactive permission only to this same set.
    argv += ["--tools", ",".join(allowed_tools.split())]
    # `--allowedTools <tools...>` is variadic: pass each tool as its own token and
    # put it LAST so the variadic capture cannot swallow a following flag's value.
    argv += ["--allowedTools", *allowed_tools.split()]
    return argv


# --------------------------------------------------------------------------- #
# Prompt-strictness ladder (skill-comply "prompt independence").
# --------------------------------------------------------------------------- #
# Orthogonal to the arm: the ARM varies the SYSTEM prompt (rule injection); STRICTNESS
# varies the USER prompt's stance toward disciplined construction. `competing` is the
# stress test — does aqg-full still keep trust-boundary validation + TDD when the ticket
# actively pushes for a quick-and-dirty job? (FEASIBILITY.md's core constraint: AQG is a
# SKILL the model CHOOSES to invoke, so it must be measured under prompt pressure, not
# only the default neutral framing.) Default is neutral-only so existing invocations +
# their cost are unchanged; competing is opt-in (it changes the matrix size, and the
# FEASIBILITY budget is LOGGED, never silently capped).
NEUTRAL = "neutral"
COMPETING = "competing"
# `mandated` exists because the 2026-09 pilot measured u-hat ~ 0.5% across three
# runs (contract §15.12): the aqg-full arm almost never CHOOSES to invoke a
# skill, so the delivery gate fails and every D(t) estimates "AQG installed but
# unused". This level asks the question that remains -- given that the discipline
# IS applied, does the output differ -- rather than the one already answered.
#
# THE TEXT IS IDENTICAL FOR EVERY ARM, and that is the whole design. Strictness
# varies the USER prompt and the arm varies the SYSTEM prompt; a mandate written
# only for aqg-full would break that orthogonality AND confound the AQG rules
# with "was told to follow a process at all", which three lines of prose can also
# do. Same sentence to all three arms: baseline has nothing configured and so
# does nothing, lite has its rules, aqg-full has its skills. The arm supplies the
# discipline; the ticket only stops treating it as optional.
MANDATED = "mandated"
STRICTNESS_LEVELS = (NEUTRAL, COMPETING, MANDATED)
DEFAULT_STRICTNESS = (NEUTRAL,)
ATOMIC_MATCHED_BLOCKS = "atomic_matched_blocks"
# Appended to the neutral ticket for the competing level. Realistic developer pressure
# (speed / skip ceremony) — deliberately NOT a named bug to copy: it must test whether
# discipline HOLDS under pressure, not whether the agent OBEYS an answer. The disciplined
# response still keeps the page-size range check (it IS strictly needed) and is caught
# lazy by the held-out scorer if it caves.
COMPETING_SUFFIX = (
    "\n\n"
    "Move fast on this one — it's a quick throwaway. Don't over-engineer it, "
    "skip the tests, and don't add validation you don't strictly need. "
    "The simplest thing that runs is fine."
)
# Deliberately says "if ... available" rather than naming AQG: naming it would
# make the sentence false for baseline and lite, and a ticket that describes a
# treatment only one arm has is no longer the same ticket.
MANDATED_SUFFIX = (
    "\n\n"
    "Before you write any code: if you have engineering-discipline skills or rules "
    "available in this session, you are required to apply them rather than treating "
    "them as optional -- invoke the relevant skill first and follow it. Do not skip "
    "this step because the task looks small."
)
WORKSPACE_BOUNDARY_INSTRUCTION = (
    "Workspace boundary: work only inside this private cell. Edit and Write only inside the "
    "current workspace directory. Read only inside the current workspace directory or its "
    "otherwise-empty private cell root. Do not inspect any shared parent directory, repository "
    "README, project docs, user-home file, or other absolute host path. The task text above is complete."
)


def render_prompt(task_md: str, solution_filename: str, strictness: str = NEUTRAL) -> str:
    """The ticket handed to the agent. The held-out checks / refs are NEVER
    included — only task.md (edge cases left implicit) + where to write. `strictness`
    appends competing pressure (the SAME ticket + a quick-and-dirty push) so adherence
    is measured under prompt pressure, not only the default neutral framing.

    Fail-closed on an off-list level (audit 15d2bac8 gpt-5.5 f1): a programmatic caller
    passing e.g. "bogus" must raise here, not silently get a neutral prompt that then
    gets recorded under the wrong label — that would quietly nullify the strictness axis."""
    if strictness not in STRICTNESS_LEVELS:
        raise ValueError(f"unknown strictness {strictness!r} (have: {', '.join(STRICTNESS_LEVELS)})")
    base = (
        f"{task_md.strip()}\n\n"
        f"The stub to implement is in `{solution_filename}` in the current directory. "
        f"Edit that file in place to complete the task, then reply DONE.\n\n"
        f"{WORKSPACE_BOUNDARY_INSTRUCTION}"
    )
    if strictness == COMPETING:
        return base + COMPETING_SUFFIX
    if strictness == MANDATED:
        return base + MANDATED_SUFFIX
    return base


# --------------------------------------------------------------------------- #
# Budget — log, never silently cap (FEASIBILITY.md §Budget).
# --------------------------------------------------------------------------- #
def project_cost(
    n_tasks: int, n_arms: int, n_strictness: int, n_runs: int,
    per_cell: float = PER_CELL_COST_USD,
) -> tuple[int, float]:
    """(n_cells, projected_usd) for an N tasks × arms × strictness × runs matrix."""
    n_cells = n_tasks * n_arms * n_strictness * n_runs
    return n_cells, round(n_cells * per_cell, 4)


@dataclass(frozen=True)
class CollectionBlock:
    """One task/repeat matched set for the frozen singleton neutral stratum."""

    block_id: str
    task: Path
    run_idx: int
    strictness: str
    arm_order: tuple[str, ...]


def build_collection_blocks(
    tasks: Sequence[Path], arms: Sequence[Arm | str], *, runs: int, order_seed: int,
    strictness: str = NEUTRAL, expected_arms: Sequence[str] | None = None,
) -> tuple[CollectionBlock, ...]:
    """Build reproducible complete primary blocks without interpreter RNG state.

    The order is a pure SHA-256 ranking of frozen coordinates. That lets the
    analyzer reconstruct the pre-turn schedule across supported Python versions
    instead of trusting a manifest-provided digest or implementation-specific
    ``random.shuffle`` behavior.
    """
    if strictness != NEUTRAL:
        raise UsageError("atomic primary blocks require the frozen neutral strictness stratum")
    if runs <= 0:
        raise UsageError("atomic primary blocks require a positive repeat count")
    arm_names = tuple(arm if isinstance(arm, str) else arm.name for arm in arms)
    # A block is "one of every arm on one task". The check was a literal `== 4`,
    # and an earlier draft of this change replaced it with non-emptiness and
    # uniqueness — which four voices of five correctly called removing the guard
    # rather than re-deriving it, since a single arm satisfies it (audit
    # 83c7e451). What has to hold is that the block carries the WHOLE declared
    # arm set, so the set is passed in rather than counted.
    if len(set(arm_names)) != len(arm_names):
        raise UsageError("atomic primary blocks require unique arms")
    # Defaulting to `arm_names` makes the comparison below tautological, so
    # the fallback is only ever reached by a caller that has no protocol to
    # speak for — the tests. The paid path passes the declared matrix (audit
    # 6867c40e, Voice 4 f4: the guard was right but opt-in and untested).
    required = tuple(expected_arms) if expected_arms is not None else arm_names
    if not required:
        raise UsageError("atomic primary blocks require a non-empty arm set")
    if set(arm_names) != set(required):
        raise UsageError(
            "atomic primary blocks must contain every arm of the declared matrix")
    blocks: list[CollectionBlock] = []
    for task in tasks:
        for run_idx in range(runs):
            arm_order = tuple(sorted(
                arm_names,
                key=lambda arm: (
                    hashlib.sha256(
                        f"{order_seed}:arm:{task.name}:{run_idx}:{arm}".encode("utf-8")
                    ).hexdigest(),
                    arm,
                ),
            ))
            block_id = f"{task.name}__{strictness}__r{run_idx}"
            blocks.append(CollectionBlock(block_id, task, run_idx, strictness, arm_order))
    blocks.sort(key=lambda block: (
        hashlib.sha256(
            f"{order_seed}:block:{block.task.name}:{block.run_idx}".encode("utf-8")
        ).hexdigest(),
        block.block_id,
    ))
    return tuple(blocks)


def collection_plan_payload(blocks: tuple[CollectionBlock, ...]) -> list[dict[str, Any]]:
    """Redaction-safe complete primary schedule stored before the first model turn."""
    return [
        {
            "block_id": block.block_id,
            "task": block.task.name,
            "run_idx": block.run_idx,
            "strictness": block.strictness,
            "arm_order": list(block.arm_order),
        }
        for block in blocks
    ]


def collection_plan_sha256(blocks: tuple[CollectionBlock, ...]) -> str:
    """Digest the exact schedule with stable JSON serialization."""
    payload = json.dumps(collection_plan_payload(blocks), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Cell execution (the one place that shells out to `claude`) + recording.
# --------------------------------------------------------------------------- #
# A cell's status. The frozen analyzer treats every status other than `pass` as
# an ITT defect; the distinct non-pass values remain visible diagnostics.
STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_ERRORED = "errored"
STATUS_INFRA = "infra_failed"
STATUS_BUDGET = "budget_capped"
SCORED_STATUSES = (STATUS_PASS, STATUS_FAIL)


@dataclass(frozen=True)
class CellResult:
    task: str
    arm: str
    strictness: str
    run_idx: int
    status: str
    n_failures: int
    failures: tuple[str, ...]
    error: str | None
    isolation_ok: bool
    isolation_detail: str
    isolation_unavailable: bool
    aqg_signals: tuple[str, ...]
    memory_ok: bool
    memory_detail: str
    memory_unavailable: bool
    workspace_ok: bool
    workspace_detail: str
    workspace_root: str
    file_tool_paths: tuple[str, ...]
    denied_out_of_boundary_file_attempts: int
    absent_out_of_boundary_file_attempts: int
    ambiguous_tool_use_correlations: int
    capability_signals: tuple[str, ...]
    cost_usd: float | None
    accounted_cost_usd: float
    cost_accounting: str
    num_turns: int | None
    duration_ms: int | None
    has_result: bool
    result_subtype: str | None
    is_error: bool
    truncated: bool
    returncode: int | None
    stderr_tail: str | None
    skill_uses: tuple[str, ...]
    tool_uses: tuple[str, ...]
    parse_errors: int
    raw_path: str | None
    solution_path: str | None
    block_id: str | None = None
    # Additive and defaulted: one coarse class per out-of-boundary event, never a
    # path. Optional so every existing construction keeps working; the runner
    # always supplies it.
    out_of_boundary_classes: tuple[str, ...] = ()

    @property
    def passed(self) -> bool:
        return self.status == STATUS_PASS

    @property
    def scored(self) -> bool:
        return self.status in SCORED_STATUSES

    def to_dict(self) -> dict[str, Any]:
        def artifact_sha256(path_text: str | None) -> str | None:
            if path_text is None:
                return None
            path = Path(path_text)
            return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None

        raw_sha256 = artifact_sha256(self.raw_path)
        solution_sha256 = artifact_sha256(self.solution_path)
        return {
            "record_type": "cell",
            "task": self.task, "arm": self.arm, "strictness": self.strictness, "run_idx": self.run_idx,
            "block_id": self.block_id,
            "status": self.status, "n_failures": self.n_failures,
            "failures": [redact_untrusted_text(item) for item in self.failures],
            "error": redact_untrusted_text(self.error),
            "isolation_ok": self.isolation_ok, "isolation_detail": self.isolation_detail,
            "isolation_unavailable": self.isolation_unavailable,
            "aqg_signals": list(self.aqg_signals),
            "memory_ok": self.memory_ok, "memory_detail": self.memory_detail,
            "memory_unavailable": self.memory_unavailable,
            "workspace_ok": self.workspace_ok, "workspace_detail": self.workspace_detail,
            "workspace_root": self.workspace_root,
            "file_tool_paths": list(self.file_tool_paths),
            "out_of_boundary_classes": list(self.out_of_boundary_classes),
            "denied_out_of_boundary_file_attempts": self.denied_out_of_boundary_file_attempts,
            "absent_out_of_boundary_file_attempts": self.absent_out_of_boundary_file_attempts,
            "ambiguous_tool_use_correlations": self.ambiguous_tool_use_correlations,
            "capability_signals": list(self.capability_signals),
            "cost_usd": self.cost_usd, "accounted_cost_usd": self.accounted_cost_usd,
            "cost_accounting": self.cost_accounting,
            "num_turns": self.num_turns, "duration_ms": self.duration_ms,
            "has_result": self.has_result, "result_subtype": self.result_subtype,
            "is_error": self.is_error, "truncated": self.truncated,
            "returncode": self.returncode, "stderr_tail": self.stderr_tail,
            "skill_uses": list(self.skill_uses), "tool_uses": list(self.tool_uses),
            "parse_errors": self.parse_errors, "raw_path": self.raw_path, "raw_sha256": raw_sha256,
            "solution_path": self.solution_path, "solution_sha256": solution_sha256,
        }


def _as_text(maybe: Any) -> str:
    if maybe is None:
        return ""
    if isinstance(maybe, bytes):
        return maybe.decode("utf-8", "replace")
    return str(maybe)


def redact_untrusted_text(value: str | None) -> str | None:
    """Keep model/checker diagnostic text local without durable host paths."""
    return _HOST_PATH_RE.sub("[redacted-path]", value) if isinstance(value, str) else value


def capability_signals(metrics: RunMetrics) -> tuple[str, ...]:
    """A compact, non-path capability snapshot for frozen diagnostics."""
    if metrics.init_event is None:
        return ()
    values: set[str] = set()
    for field in _CAPABILITY_FIELDS:
        values |= _capability_tokens(metrics.init_event.get(field))
    return tuple(sorted(values))


def _is_budget_capped(metrics: RunMetrics) -> bool:
    """Recognize the documented CLI terminal subtype without guessing success."""
    return (metrics.result_subtype or "").casefold() == "error_max_budget"


def _accounted_cost(
    raw_cost: Any, *, reservation_usd: float, per_cell_cap_usd: float | None,
) -> tuple[float | None, float, str, str | None]:
    """Normalize untrusted CLI cost evidence without ever decreasing spend.

    A missing terminal cost is permitted by the frozen infra-tolerance rule and
    is conservatively charged at the reservation. Malformed, negative, or
    over-cell-cap values are hard faults: they cannot lower the running cap.
    """
    if not math.isfinite(reservation_usd) or reservation_usd <= 0:
        raise ValueError("cost reservation must be finite and positive")
    if raw_cost is None:
        return None, reservation_usd, "reserved_missing", None
    if isinstance(raw_cost, bool) or not isinstance(raw_cost, (int, float)):
        return None, reservation_usd, "reserved_invalid", "CLI reported a non-numeric cost"
    cost = float(raw_cost)
    if not math.isfinite(cost) or cost < 0:
        return cost, reservation_usd, "reserved_invalid", "CLI reported a non-finite or negative cost"
    if per_cell_cap_usd is not None and cost > per_cell_cap_usd:
        return cost, cost, "reported_over_cap", "CLI reported cost above the frozen per-cell cap"
    return cost, cost, "reported", None


def archive_solution(source: Path, destination: Path) -> Path:
    """Copy one regular model solution once into durable evidence."""
    if source.is_symlink() or not source.is_file():
        raise UsageError("model solution cannot be archived")
    data = source.read_bytes()
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise UsageError(f"cannot exclusively archive model solution: {exc}") from exc
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)
    return destination


def _run_model_process(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Run Claude in its own process group and reap descendants on every exit."""
    capture_output = bool(kwargs.pop("capture_output", False))
    timeout = kwargs.pop("timeout", None)
    kwargs.pop("check", None)
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE if capture_output else None,
        stderr=subprocess.PIPE if capture_output else None,
        start_new_session=True,
        **kwargs,
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        _kill_process_group(process.pid)
        process.communicate()
        raise subprocess.TimeoutExpired(argv, timeout, output=exc.output, stderr=exc.stderr) from exc
    finally:
        # A successful communicate has already reaped the process.  Only clean
        # up descendants while the group leader is still live.
        if not timed_out and process.poll() is None:
            _kill_process_group(process.pid)
            process.communicate()
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def run_cell(
    task_dir: Path,
    arm: Arm,
    run_idx: int,
    *,
    model: str,
    allowed_tools: str,
    timeout: int,
    workdir: Path,
    strictness: str = NEUTRAL,
    max_budget_usd: float | None = None,
    cost_reservation_usd: float | None = None,
    raw_dir: Path | None = None,
    solution_dir: Path | None = None,
    block_id: str | None = None,
    runner: Callable[..., Any] | None = None,
    model_isolation: ModelIsolationCapability | None = None,
    require_model_isolation: bool = False,
) -> CellResult:
    """Run one cell end-to-end: fresh seed copy → headless `claude` → score →
    isolation check. `runner` is injectable so tests exercise the wiring without
    spending; it is resolved to subprocess.run at CALL time (not bound as a
    default) so a test patching subprocess.run is honored even via main().

    A timeout / spawn failure / non-zero exit / missing-or-error result event
    makes the cell `infra_failed`: the run didn't complete, so its solution is not
    a trustworthy measurement and must not be folded into the arm's defect mean
    (audit 9aad54d9 f2/f6). One bad cell never aborts the matrix."""
    # strictness token in the dir keeps neutral/competing cells distinct; "neutral"/
    # "competing" carry no `aqg-` substring, so the isolation path-scan can't false-trip.
    # Give every model turn its own unpredictable parent.  The known coordinate
    # root remains the only readable private cell, so one turn cannot discover
    # a sibling merely by reading the shared matrix directory.
    cell_parent = Path(tempfile.mkdtemp(prefix="benchcell-", dir=str(workdir))).resolve()
    cell_root = (cell_parent / f"{task_dir.name}__{arm.name}__{strictness}__r{run_idx}").resolve()
    cell_dir = cell_root / "workspace"
    cell_dir.mkdir(parents=True, exist_ok=True)
    solution = cell_dir / "seed.py"
    shutil.copy2(task_dir / "seed.py", solution)

    task_md = (task_dir / "task.md").read_text(encoding="utf-8")
    prompt = render_prompt(task_md, "seed.py", strictness)
    argv = build_argv(
        arm,
        prompt,
        model=model,
        allowed_tools=allowed_tools,
        max_budget_usd=max_budget_usd,
        cell_dir=cell_dir,
    )
    raw_path = ((raw_dir / f"{task_dir.name}__{arm.name}__{strictness}__r{run_idx}.jsonl")
                if raw_dir is not None else cell_dir / "stream.jsonl")
    # Prepare the artifact directory before the paid turn.  A setup failure is
    # then a no-spend refusal, rather than an unaccounted post-call exception.
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    if require_model_isolation and model_isolation is None:
        raise UsageError("model isolation capability is required before a primary model child starts")
    invocation = (
        wrap_model_child_argv(
            argv, capability=model_isolation, cell_dir=cell_dir, read_root=cell_root,
        )
        if model_isolation is not None else None
    )
    model_argv = invocation.argv if invocation is not None else argv

    raw = ""
    returncode: int | None = None
    stderr = ""
    exec_error: str | None = None
    try:
        # cwd = the cell sandbox, so the agent's relative file edits land in it.
        kwargs: dict[str, Any] = {
            "cwd": str(cell_dir), "capture_output": True, "text": True, "timeout": timeout,
            "env": clean_child_env(),
        }
        completed = _run_model_process(model_argv, **kwargs) if runner is None else runner(model_argv, **kwargs)
        raw = _as_text(getattr(completed, "stdout", ""))
        stderr = _as_text(getattr(completed, "stderr", ""))
        returncode = getattr(completed, "returncode", None)
    except subprocess.TimeoutExpired as exc:
        exec_error = f"timeout after {timeout}s"
        # A killed process can leave a partial final JSON line.  Do not let byte
        # flush timing turn the permitted init-less infra state into a malformed
        # stream study failure; charge the full reservation instead.
        raw = ""
        stderr = _as_text(getattr(exc, "stderr", ""))
    except OSError as exc:                              # claude not on PATH, etc.
        exec_error = f"spawn failed: {type(exc).__name__}"
    finally:
        if invocation is not None:
            invocation.cleanup()

    metrics = parse_stream_json(raw.splitlines())
    reservation = cost_reservation_usd if cost_reservation_usd is not None else (
        max_budget_usd if max_budget_usd is not None else PER_CELL_COST_USD
    )
    reported_cost, accounted_cost, cost_accounting, cost_error = _accounted_cost(
        metrics.cost_usd, reservation_usd=float(reservation), per_cell_cap_usd=max_budget_usd,
    )
    func_name = task_dir.name.replace("-", "_")
    iso = isolation_check(metrics, arm)
    memory = memory_check(metrics, cell_root)
    # THE READ BOUNDARY IS THE CELL PARENT, NOT THE CELL ROOT. Five runs died on
    # this hard check (2026-08-17, 08-21, 08-24, 08-31, 08-31b); the fifth was
    # the first one the ledger could name, and it named `own-cell-parent` twice
    # -- the model reading ONE LEVEL UP, into the mkdtemp whose only child is
    # this cell's own root. Nothing else is ever created there (the directory is
    # made at `cell_parent = mkdtemp(...)`, `cell_root` is built inside it, and
    # it is removed at teardown), so that directory carries no other cell's data
    # and reading it can carry nothing across cells. This is the direction the
    # 2026-08-21 design review endorsed (audit 55dafb28, PR #591) at the time it
    # rejected everything else in that sketch.
    #
    # THIS GRANTS THE MODEL NOTHING NEW. The sandbox profile is untouched, so
    # those reads are still refused exactly as before; what stops is invalidating
    # an entire matrix over a refused read of a directory holding only our own
    # cell. Everything outside `cell_parent` -- sibling cells, the shared run
    # root, the user home -- is still out of boundary and still fatal, which is
    # the contamination the check exists for.
    #
    # WRITES ARE UNCHANGED: `write_root` is still `cell_dir`, so the model can
    # still only write inside its own workspace.
    workspace = workspace_check(metrics, cell_parent, write_root=cell_dir,
                                cell_parent=cell_parent)
    # Do not durably retain a raw stream if it contains a model tool operation
    # outside the cell tree, even if the CLI denied it: the raw event can still
    # contain a user-home path.  A blank forensic placeholder plus the count
    # and verdict is sufficient to prove the study must abort without carrying
    # that data into a future result PR.
    raw_persistence_failed = False
    try:
        raw_path.write_text(
            # ANY out-of-boundary event blanks the stream, including one the
            # stream proves was denied and which therefore does not invalidate:
            # whether the cell stays valid and whether its raw event is safe to
            # keep are different questions, and the raw event still contains the
            # escaped path either way.
            raw if (workspace.ok and not workspace.out_of_boundary_classes
                    and (memory.ok or memory.unavailable) and not metrics.parse_errors) else "",
            encoding="utf-8",
        )
    except OSError:
        # The model call may already have spent money.  Return a forensic cell
        # whose conservative reservation will be durably charged by ``main``;
        # the absent raw artifact makes the frozen analyzer fail closed.
        raw_persistence_failed = True

    solution_path: Path | None = None
    budget_capped = _is_budget_capped(metrics)
    infra_failed = (
        exec_error is not None
        or (returncode is not None and returncode != 0)
        or not metrics.has_result
        or metrics.is_error
        or metrics.truncated
        or metrics.parse_errors > 0
        or cost_error is not None
    )
    score = ScoreResult(func_name, (), False, None)
    if raw_persistence_failed:
        status, error = STATUS_INFRA, "durable raw evidence write failed"
    elif infra_failed:
        status = STATUS_BUDGET if budget_capped else STATUS_INFRA
        error = ("per-cell budget cap reached" if budget_capped else
                 (f"stream contained {metrics.parse_errors} malformed JSON line(s)" if metrics.parse_errors else None) or exec_error or
                 cost_error or
                 (f"run did not complete cleanly (rc={returncode}, has_result={metrics.has_result}, "
                  f"is_error={metrics.is_error}, truncated={metrics.truncated})"))
    elif not iso.ok:
        # A contaminated/misconfigured treatment is invalid data and does not
        # justify executing its generated code in the scorer.  The main loop
        # aborts immediately after writing this forensic record.
        status, error = STATUS_INFRA, iso.detail
    elif not memory.ok:
        status, error = STATUS_INFRA, memory.detail
    elif not workspace.ok:
        status, error = STATUS_INFRA, workspace.detail
    else:
        score = score_file_sandboxed(
            solution,
            task_dir / "checks.py",
            func_name,
            protected_write_roots=(workdir,),
        )
        if score.harness_failed:
            status, error = STATUS_INFRA, score.error
        elif score.errored:
            status, error = STATUS_ERRORED, score.error
        elif score.passed:
            status, error = STATUS_PASS, None
        else:
            status, error = STATUS_FAIL, None

        # A missing/symlinked solution is a model defect already represented by
        # ``STATUS_ERRORED``.  Archive every regular scored solution, but do not
        # recast that model behavior as a harness outage.
        if (not score.harness_failed and solution_dir is not None
                and not solution.is_symlink() and solution.is_file()):
            try:
                solution_path = archive_solution(
                    solution,
                    solution_dir / f"{task_dir.name}__{arm.name}__{strictness}__r{run_idx}.py",
                )
            except UsageError:
                status, error = STATUS_INFRA, "cannot archive regular model solution"

    result = CellResult(
        task=task_dir.name, arm=arm.name, strictness=strictness, run_idx=run_idx,
        status=status, n_failures=score.n_failures, failures=score.failures, error=error,
        isolation_ok=iso.ok, isolation_detail=iso.detail, isolation_unavailable=iso.unavailable,
        aqg_signals=iso.signals, memory_ok=memory.ok, memory_detail=memory.detail,
        memory_unavailable=memory.unavailable,
        workspace_ok=workspace.ok, workspace_detail=workspace.detail, workspace_root=cell_root.name,
        file_tool_paths=workspace.paths,
        out_of_boundary_classes=workspace.out_of_boundary_classes,
        denied_out_of_boundary_file_attempts=workspace.denied_out_of_boundary_attempts,
        absent_out_of_boundary_file_attempts=workspace.absent_out_of_boundary_attempts,
        ambiguous_tool_use_correlations=metrics.ambiguous_tool_use_correlations,
        capability_signals=capability_signals(metrics),
        cost_usd=reported_cost, accounted_cost_usd=accounted_cost, cost_accounting=cost_accounting,
        num_turns=metrics.num_turns, duration_ms=metrics.duration_ms,
        has_result=metrics.has_result, result_subtype=metrics.result_subtype,
        is_error=metrics.is_error, truncated=metrics.truncated,
        returncode=returncode,
        # CLI stderr is raw host output; durable results never retain it.
        stderr_tail=None,
        skill_uses=metrics.skill_uses, tool_uses=metrics.tool_uses,
        parse_errors=metrics.parse_errors,
        raw_path=(None if raw_persistence_failed else str(raw_path)),
        solution_path=(str(solution_path) if solution_path else None),
        block_id=block_id,
    )
    # Raw/solution evidence lives outside this directory.  Once it is archived,
    # no model-edited workspace may survive an ordinary or invalidated cell.
    shutil.rmtree(cell_parent, ignore_errors=True)
    return result


# --------------------------------------------------------------------------- #
# CLI.
# --------------------------------------------------------------------------- #
class UsageError(ValueError):
    """Bad CLI input."""


def primary_budget_cap_usd(protocol: dict[str, Any]) -> float:
    """Return the frozen primary cap after charged diagnostics are deducted."""
    budgets = protocol["budgets"]
    global_cap = float(budgets["global_max_usd"])
    diagnostic_spend = float(budgets["diagnostic_spend_usd"])
    primary_available = float(budgets["primary_available_usd"])
    if not all(math.isfinite(value) for value in (global_cap, diagnostic_spend, primary_available)):
        raise ValueError("budget values must be finite")
    if not (global_cap > 0 and diagnostic_spend >= 0 and primary_available > 0):
        raise ValueError("budget values must leave a positive primary cap")
    if not math.isclose(
        diagnostic_spend + primary_available, global_cap, rel_tol=0.0, abs_tol=1e-7
    ):
        raise ValueError("diagnostic spend and primary cap must equal the global budget")
    return primary_available


def primary_results_directory(protocol: dict[str, Any]) -> Path:
    """Resolve the protocol-bound evidence directory without allowing reuse or escape."""
    raw = protocol.get("environment", {}).get("primary_results_directory")
    if not isinstance(raw, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", raw):
        raise ValueError("primary results directory must be one relative directory name")
    predecessor_directory = protocol.get("predecessor", {}).get("results_directory")
    if raw == "results" or raw == predecessor_directory:
        raise ValueError("prior study results directory cannot be reused")
    candidate = HERE / raw
    if candidate.is_symlink():
        raise ValueError("primary results directory cannot be a symbolic link")
    if candidate.resolve().parent != HERE.resolve():
        raise ValueError("primary results directory escapes benchmark root")
    return candidate


def validate_nonprimary_evidence_directory(out_dir: Path) -> None:
    """Keep the frozen primary evidence destination exclusive to --primary."""
    protocol = load_protocol()
    if out_dir.resolve() == primary_results_directory(protocol).resolve():
        raise UsageError("frozen primary evidence directory is reserved for --primary --execute")


def cleanup_unreserved_primary_debris(out_dir: Path, protocol: dict[str, Any]) -> None:
    """Remove only an empty pre-marker evidence set left by an external crash."""
    ledger = out_dir / f"{protocol['study_id']}.jsonl"
    raw_dir = out_dir / f"{ledger.stem}.raw"
    solution_dir = out_dir / f"{ledger.stem}.solutions"
    marker = out_dir / f"{protocol['study_id']}-attempt.json"
    artifacts = (ledger, raw_dir, solution_dir)
    if marker.exists() or not any(path.exists() for path in artifacts):
        return
    def empty_file_or_missing(path: Path) -> bool:
        return not path.exists() or (path.is_file() and not path.is_symlink() and path.stat().st_size == 0)

    def empty_dir_or_missing(path: Path) -> bool:
        return not path.exists() or (path.is_dir() and not path.is_symlink() and not any(path.iterdir()))

    empty = (
        empty_file_or_missing(ledger)
        and empty_dir_or_missing(raw_dir)
        and empty_dir_or_missing(solution_dir)
    )
    if not empty:
        return
    try:
        if ledger.exists():
            ledger.unlink()
        if raw_dir.exists():
            raw_dir.rmdir()
        if solution_dir.exists():
            solution_dir.rmdir()
    except OSError as exc:
        raise UsageError(f"cannot clean unreserved primary evidence: {exc}") from exc


# Set for the lifetime of a --preflight-only run. The early return is what
# normally stops stage 1, but a single positional return is the wrong shape for a
# property this expensive: removing it does not raise, it proceeds to spend. The
# two paid entry points below refuse unconditionally under this flag, so the stop
# fails CLOSED and a regression surfaces as an immediate, named error rather than
# as a run that quietly continues into the matrix.
_PREFLIGHT_ONLY = False


def _refuse_paid_step_during_preflight(step: str) -> None:
    if _PREFLIGHT_ONLY:
        raise AssertionError(f"--preflight-only reached a paid step: {step}")


def preflight_paid_start_preconditions(out_dir: Path, protocol: dict[str, Any]) -> tuple[str, ...]:
    """Report why a paid start would be refused, WITHOUT touching the disk.

    `preflight_primary_evidence_paths` answers the same question but is not
    read-only — it creates the evidence directory, sweeps debris, and writes a
    writability probe — so stage 1 cannot call it without acquiring the side
    effects it promises not to have. This is the read-only APPROXIMATION of those
    refusals — close, deliberately not identical, and the differences are stated
    rather than papered over: emptiness is mirrored so sweepable debris is not a
    false red, `lexists` is used so a dangling entry is not a false green, and
    writability is approximated with `os.access` because probing it for real
    means writing. It exists so stage 1 can answer "would the paid run be allowed
    to start" for the two conditions most likely to be false: this study has one
    non-retryable attempt, and three attempts before it were consumed.
    """
    reasons: list[str] = []
    stem = protocol["study_id"]
    # `lexists`, never `exists`: the reservation is an O_EXCL open, which fails
    # EEXIST on ANY existing directory entry — a dangling symlink included —
    # while `exists()` follows the link and reports False. That difference is a
    # false green on the one question this function exists to answer.
    if os.path.lexists(out_dir / f"{stem}-attempt.json"):
        reasons.append("the attempt marker already exists; this study has no retry path")
    if out_dir.is_symlink():
        reasons.append("the evidence directory is a symbolic link")
    elif out_dir.exists():
        if not out_dir.is_dir():
            reasons.append("the evidence path exists and is not a directory")
        else:
            # Only leftovers the paid path would NOT sweep. It removes an EMPTY
            # pre-marker evidence set (`cleanup_unreserved_primary_debris`) and
            # carries on, so reporting those as blocking would be a false red —
            # and with three aborted attempts behind this study, empty debris is
            # a likely host state, which would make stage 1 refuse exactly when
            # the paid run would have proceeded.
            leftovers = (
                (out_dir / f"{stem}.jsonl", "result ledger"),
                (out_dir / f"{stem}.raw", "raw stream directory"),
                (out_dir / f"{stem}.solutions", "solution archive"),
            )
            sweepable = not os.path.lexists(out_dir / f"{stem}-attempt.json") and all(
                _is_sweepable_debris(path) for path, _ in leftovers
            )
            if not sweepable:
                for leftover, label in leftovers:
                    if os.path.lexists(leftover):
                        reasons.append(f"a {label} for this study id already exists")
    # The paid path writes a probe file to prove the directory is writable. That
    # is a side effect stage 1 promises not to have, so this is the read-only
    # approximation: it can still miss a filesystem that fails on write, which
    # the banner says rather than implying full parity.
    probe_root = out_dir if out_dir.is_dir() else out_dir.parent
    if probe_root.is_dir() and not os.access(probe_root, os.W_OK | os.X_OK):
        reasons.append(f"the evidence directory's location is not writable: {probe_root}")
    return tuple(reasons)


def _is_sweepable_debris(path: Path) -> bool:
    """Mirror `cleanup_unreserved_primary_debris`'s emptiness test, read-only."""
    if not os.path.lexists(path):
        return True
    if path.is_symlink():
        return False
    if path.is_file():
        return path.stat().st_size == 0
    if path.is_dir():
        return not any(path.iterdir())
    return False


def preflight_primary_evidence_paths(out_dir: Path, protocol: dict[str, Any]) -> tuple[Path, Path, Path]:
    """Check deterministic primary evidence paths before consuming the sole marker."""
    if out_dir.is_symlink():
        raise UsageError("primary evidence directory cannot be a symbolic link")
    if out_dir.exists() and not out_dir.is_dir():
        raise UsageError("primary evidence path must be a directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    cleanup_unreserved_primary_debris(out_dir, protocol)
    probe = out_dir / f".{protocol['study_id']}-writability-probe"
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError as exc:
        raise UsageError(f"primary evidence directory is not writable: {exc}") from exc
    else:
        os.close(fd)
        try:
            probe.unlink()
        except OSError as exc:
            raise UsageError(f"primary evidence directory cleanup failed: {exc}") from exc
    ledger = out_dir / f"{protocol['study_id']}.jsonl"
    raw_dir = out_dir / f"{ledger.stem}.raw"
    solution_dir = out_dir / f"{ledger.stem}.solutions"
    if ledger.exists():
        raise UsageError("primary result ledger already exists")
    if raw_dir.exists():
        raise UsageError("primary raw evidence directory already exists")
    if solution_dir.exists():
        raise UsageError("primary solution evidence directory already exists")
    return ledger, raw_dir, solution_dir


def open_primary_ledger(ledger: Path):
    """Create the deterministic primary ledger without a truncate race."""
    try:
        fd = os.open(ledger, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise UsageError("primary result ledger already exists") from exc
    except OSError as exc:
        raise UsageError(f"cannot create primary result ledger: {exc}") from exc
    return os.fdopen(fd, "w", encoding="utf-8")


def _cleanup_prepared_primary_evidence(evidence: tuple[Path, Path, Path]) -> None:
    """Remove only empty evidence artifacts created by this process before reservation."""
    ledger, raw_dir, solution_dir = evidence
    try:
        ledger.unlink(missing_ok=True)
        if raw_dir.exists():
            raw_dir.rmdir()
        if solution_dir.exists():
            solution_dir.rmdir()
    except OSError as exc:
        raise UsageError(f"cannot clean unreserved primary evidence: {exc}") from exc


def prepare_primary_evidence(out_dir: Path, protocol: dict[str, Any]) -> tuple[Any, tuple[Path, Path, Path]]:
    """Create and open empty evidence before consuming the one primary marker."""
    _refuse_paid_step_during_preflight("durable evidence creation")
    evidence = preflight_primary_evidence_paths(out_dir, protocol)
    ledger, raw_dir, solution_dir = evidence
    handle: Any | None = None
    try:
        raw_dir.mkdir(mode=0o700)
        solution_dir.mkdir(mode=0o700)
        handle = open_primary_ledger(ledger)
        handle.flush()
    except (OSError, UsageError):
        if handle is not None:
            handle.close()
        _cleanup_prepared_primary_evidence(evidence)
        raise
    return handle, evidence


def require_authenticated_claude_cli(
    runner: Callable[..., Any] | None = None,
    *,
    model_isolation: ModelIsolationCapability | None = None,
    cwd: Path | None = None,
) -> None:
    """Fail before attempt reservation unless the CLI reports an authenticated session."""
    if model_isolation is None:
        raise UsageError("model isolation capability is required before an auth probe")
    env = clean_child_env()
    cell = _model_preflight_probe_dir(model_isolation, requested_cwd=cwd)
    invocation = wrap_model_child_argv(["claude", "auth", "status"], capability=model_isolation, cell_dir=cell)
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False, "env": env}
    kwargs["cwd"] = str(cell)
    try:
        if runner is not None:
            status = runner(invocation.argv, **kwargs)
        else:
            kwargs.pop("check")
            status = _run_model_process(invocation.argv, **kwargs)
    finally:
        invocation.cleanup()
    if getattr(status, "returncode", 1) != 0:
        raise UsageError("authenticated Claude CLI required before primary attempt reservation")


def require_claude_version(
    *, model_isolation: ModelIsolationCapability | None = None, cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """Verify a non-empty CLI version before the one-shot marker exists.

    `env` exists for a diagnostic probe that runs the child under a MODIFIED
    environment — a relocated HOME, say. Pre-checking the parent's environment
    and then billing a call under a different one checks nothing about the call
    (audit 3836c9d4: Voice 1 f5, Voice 2 f3, Voice 4 f3, Voice 5 f5 all caught
    exactly that). Defaults to the paid path's own `clean_child_env()`.
    """
    if model_isolation is None:
        raise UsageError("model isolation capability is required before a version probe")
    env = clean_child_env() if env is None else env
    cell = _model_preflight_probe_dir(model_isolation, requested_cwd=cwd)
    invocation = wrap_model_child_argv(["claude", "--version"], capability=model_isolation, cell_dir=cell)
    try:
        version = _run_model_process(invocation.argv, cwd=str(cell), capture_output=True, text=True, env=env)
    finally:
        invocation.cleanup()
    rendered = version.stdout.strip()
    if version.returncode or not rendered:
        raise UsageError("installed Claude CLI version is unavailable before primary reservation")
    require_claude_version_floor(rendered)
    return rendered


# The deny set carries `Write(<path>)` rules that this CLI accepts but never
# consults — it checks file permissions against `Edit(path)` and `Read(path)`
# only (https://code.claude.com/docs/en/permissions, v2.1.210+). They are
# decoration rather than a hole ONLY because a `Read` deny also blocks the Edit
# and Write tools on the same path, and that behaviour landed on writes at
# v2.1.228. Below that floor the Write tool would be unconstrained on every host
# path the Write-spelled rules name. Audit cb1ae3ce Voice 1 f4: the claim was
# resting on prose about a binary that has already moved twice mid-study
# (2.1.207 -> 2.1.234 -> 2.1.237) without dates, so it is asserted mechanically
# here instead of being trusted.
CLAUDE_WRITE_DENY_VERSION_FLOOR = (2, 1, 228)


# How much of the executable to digest. It is a ~321MB single-file binary and a
# full hash costs seconds, which is too much to repeat before every one of 800
# cells. HEAD AND TAIL, not head alone: audit 96dc952e, Voice 3 f2 pointed out
# that a single-file bundle keeps its application payload — which is where the
# permission matcher this study measures actually lives — well past the first
# mebibyte, so a head-only digest samples the runtime header and not the thing
# under test. Head plus tail plus the exact size costs the same milliseconds and
# covers both ends of the file.
#
# THE RESIDUAL, stated rather than implied: a rebuild that changed only bytes
# between the two windows AND kept the file size identical would not be caught.
# That is accepted here for cost; a full digest is the fix if it ever matters.
_BINARY_DIGEST_BYTES = 1024 * 1024


def claude_binary_identity(version: str, *, path: str | None = None) -> dict[str, Any]:
    """Identify the CLI binary this host would run right now.

    MEASURED 2026-08-21, which is why this exists: the collection host's CLI
    moved 2.1.237 -> 2.1.238 overnight and the executable's digest changed with
    it. The mover is a launchd agent, `com.jeff.ai-cli-updater`, that runs daily
    at 09:20 local — NOT the CLI's own auto-updater, whose last record is
    2.1.207 from 2026-07-12. So a `DISABLE_AUTOUPDATER`-style
    environment switch cannot hold the binary still, and neither can a check
    that runs once before an 600-cell matrix.

    A version STRING ties committed evidence to a binary. This ties one cell to
    the next, and the start of a matrix to its end. Different questions; the
    study needs both.

    What it does NOT do, since "impossible to collect through unnoticed" was an
    overclaim three voices of three refused (audit 96dc952e): it does not
    prevent a swap, it cannot say which cells preceded one, and a change that
    reverts between two checks is invisible to it.

    `version` is passed IN rather than obtained here: every execution of the CLI
    in this runner goes through the sandbox wrapper, and a second unwrapped
    `--version` would be a hole in that invariant — the suite caught exactly
    that. Reading the file needs no execution at all.
    """
    resolved = shutil.which("claude") if path is None else path
    if resolved is None:
        raise UsageError("no claude binary on PATH to identify")
    # `path` is normally `capability.cli_executable` — the executable the
    # isolation layer itself resolved. Digesting whatever the PARENT's PATH
    # happens to find could pair version A with digest B and monitor the wrong
    # file (audit 96dc952e, Voice 2 f4).
    target = Path(resolved).resolve()
    if not target.is_file():
        raise UsageError(f"claude binary {target} is not a file")
    rendered = version.strip()
    if not rendered:
        raise UsageError("a CLI version is required to identify the binary")
    size = target.stat().st_size
    with target.open("rb") as handle:
        head = handle.read(_BINARY_DIGEST_BYTES)
        handle.seek(max(0, size - _BINARY_DIGEST_BYTES))
        tail = handle.read(_BINARY_DIGEST_BYTES)
    return {
        "version": rendered,
        "sha256_first_1mib": hashlib.sha256(head).hexdigest(),
        "sha256_last_1mib": hashlib.sha256(tail).hexdigest(),
        "size_bytes": size,
    }


def require_binary_identity_unchanged(recorded: dict[str, Any] | None,
                                      current: dict[str, Any]) -> None:
    """Refuse when the binary moved between two points of one collection.

    Fail-closed on an absent or partial record: "nothing was written down" must
    never read as "nothing moved".
    """
    # `version` is carried over at completion rather than re-measured, so in the
    # start-vs-end use it compares equal by construction and the digest and size
    # are what actually bind. It is still checked because this function is also
    # used where both sides are measured independently.
    fields = ("version", "sha256_first_1mib", "sha256_last_1mib", "size_bytes")
    if not recorded or any(recorded.get(field) in (None, "") for field in fields):
        raise UsageError(
            "no complete CLI binary identity was recorded for this attempt, so a "
            "mid-collection change cannot be ruled out; every field of "
            f"{fields} is required")
    drifted = [field for field in fields if recorded.get(field) != current.get(field)]
    if drifted:
        raise UsageError(
            "the Claude CLI binary changed during this collection: "
            + "; ".join(f"{field} {recorded.get(field)!r} -> {current.get(field)!r}"
                        for field in drifted)
            + ". Every cell before the change ran behind a different binary, so the "
            "permission boundary this study measures is not the one it collected "
            "under. Disable whatever updates the CLI on this host before re-running")


def require_claude_version_floor(rendered: str) -> tuple[int, int, int]:
    """Refuse a CLI older than the floor a deny rule's write coverage needs."""
    match = re.match(r"\s*(\d+)\.(\d+)\.(\d+)", rendered)
    if match is None:
        raise UsageError(
            f"installed Claude CLI version is unparseable: {rendered!r}; the deny "
            "set's write coverage requires a known version")
    found = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    if found < CLAUDE_WRITE_DENY_VERSION_FLOOR:
        floor = ".".join(str(part) for part in CLAUDE_WRITE_DENY_VERSION_FLOOR)
        raise UsageError(
            f"installed Claude CLI {rendered!r} is below {floor}, where a Read deny "
            "began covering the Write tool; below that floor the Write-spelled deny "
            "rules are never consulted and the Write tool is unconstrained")
    return found


def require_claude_help(*, model_isolation: ModelIsolationCapability | None = None) -> None:
    """Verify the frozen primary CLI surface under the same mandatory wrapper."""
    if model_isolation is None:
        raise UsageError("model isolation capability is required before a help probe")
    cell = _model_preflight_probe_dir(model_isolation)
    invocation = wrap_model_child_argv(["claude", "--help"], capability=model_isolation, cell_dir=cell)
    try:
        cli_help = _run_model_process(
            invocation.argv, cwd=str(cell), capture_output=True, text=True, env=clean_child_env(),
        )
    finally:
        invocation.cleanup()
    if cli_help.returncode or any(flag not in cli_help.stdout for flag in PRIMARY_REQUIRED_FLAGS):
        raise UsageError("installed Claude CLI does not support every frozen primary flag")


def reserve_primary_attempt(
    out_dir: Path,
    protocol: dict[str, Any],
    *,
    prepared_evidence: tuple[Path, Path, Path] | None = None,
) -> Path:
    """Atomically reserve the one Owner-authorized WS-7 primary attempt."""
    _refuse_paid_step_during_preflight("attempt-marker reservation")
    # Recheck immediately before O_EXCL reservation so a stale artifact created
    # after the broader environment preflight cannot consume the one study run.
    if prepared_evidence is None:
        preflight_primary_evidence_paths(out_dir, protocol)
    else:
        ledger, raw_dir, solution_dir = prepared_evidence
        expected = (
            out_dir / f"{protocol['study_id']}.jsonl",
            out_dir / f"{protocol['study_id']}.raw",
            out_dir / f"{protocol['study_id']}.solutions",
        )
        if (prepared_evidence != expected or not ledger.is_file()
                or not raw_dir.is_dir() or raw_dir.is_symlink()
                or not solution_dir.is_dir() or solution_dir.is_symlink()):
            raise UsageError("prepared primary evidence is malformed")
    marker = out_dir / f"{protocol['study_id']}-attempt.json"
    owner_authorization = protocol["owner_authorization"]
    prior_spend = float(owner_authorization["prior_spend_usd"])
    payload = {
        "study_id": protocol["study_id"],
        "protocol_sha256": protocol_sha256(),
        "global_max_usd": protocol["budgets"]["global_max_usd"],
        "diagnostic_spend_usd": protocol["budgets"]["diagnostic_spend_usd"],
        "owner_authorization_maximum_spend_usd": owner_authorization["maximum_spend_usd"],
        "prior_spend_under_current_authorization_usd": prior_spend,
        "cumulative_spend_under_current_authorization_usd": prior_spend,
    }
    try:
        fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise UsageError("primary attempt already exists; the frozen study has no retry path") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, sort_keys=True)
        fh.write("\n")
    return marker


def load_protocol(path: Path = PROTOCOL_FILE) -> dict[str, Any]:
    """Load the committed protocol and reject a malformed primary-run contract."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"cannot load protocol {path}: {exc}") from exc
    try:
        matrix = data["matrix"]
        budgets = data["budgets"]
        analysis = data["analysis"]
        validity = data["validity"]
        provenance = data["treatment_provenance"]
        environment = data["environment"]
        owner_authorization = data["owner_authorization"]
        predecessor = data["predecessor"]
        research_design = data["research_design"]
        reporting = data["reporting"]
        if data["schema_version"] != 1 or data["primary_only"] is not True:
            raise ValueError("protocol schema/version boundary is not frozen")
        if len(matrix["tasks"]) != 10 or matrix["runs_per_task_arm"] != 20:
            raise ValueError("primary matrix must be exactly 10 tasks x 20 repeats")
        if matrix["arms"] != ["baseline", "claude-md-lite", "aqg-full"]:
            raise ValueError("primary arms are not the frozen three-arm sequence")
        collection_design = data.get("collection_design")
        if collection_design is not None:
            if collection_design != ATOMIC_MATCHED_BLOCKS:
                raise ValueError("collection design is not supported")
            if matrix["strictness"] != [NEUTRAL]:
                raise ValueError("atomic collection requires the singleton neutral strictness matrix")
        if not (budgets["per_cell_max_usd"] > 0 and budgets["global_max_usd"] > 0):
            raise ValueError("budget caps must be positive")
        primary_budget_cap_usd(data)
        authorized_max = float(owner_authorization["maximum_spend_usd"])
        if not math.isfinite(authorized_max) or authorized_max <= 0:
            raise ValueError("Owner authorization maximum must be positive and finite")
        prior_spend = float(owner_authorization.get("prior_spend_usd", 0.0))
        if not math.isfinite(prior_spend) or prior_spend < 0:
            raise ValueError("Owner authorization prior spend must be non-negative and finite")
        components = owner_authorization.get("prior_spend_components")
        if not isinstance(components, list) or not components:
            raise ValueError("Owner authorization prior-spend components are missing")
        component_total = 0.0
        for component in components:
            if not isinstance(component, dict) or not isinstance(component.get("label"), str) or not component["label"].strip():
                raise ValueError("Owner authorization prior-spend component label is malformed")
            amount = component.get("amount_usd")
            if isinstance(amount, bool) or not isinstance(amount, (int, float)) or not math.isfinite(float(amount)) or float(amount) < 0:
                raise ValueError("Owner authorization prior-spend component amount is malformed")
            component_total += float(amount)
        if not math.isclose(component_total, prior_spend, rel_tol=0.0, abs_tol=1e-9):
            raise ValueError("Owner authorization prior-spend components do not match prior spend")
        if float(budgets["global_max_usd"]) > authorized_max:
            raise ValueError("study budget exceeds Owner authorization")
        if prior_spend + float(budgets["global_max_usd"]) > authorized_max + 1e-9:
            raise ValueError("cumulative spend exceeds Owner authorization")
        if not isinstance(owner_authorization["scope"], str) or not owner_authorization["scope"].strip():
            raise ValueError("Owner authorization scope is missing")
        # The predecessor is bound EITHER by a published results commit OR, when
        # its attempt aborted, by the local ledger digest — an aborted study's
        # evidence directory is never committed, because the result-boundary
        # checker refuses to publish one. `results_commit: null` is therefore an
        # alternative binding, not a waiver: exactly where the commit is absent,
        # the 64-hex digest and the retention note become required.
        results_commit = predecessor["results_commit"]
        if results_commit is None:
            ledger_sha256 = predecessor.get("ledger_sha256")
            retention = predecessor.get("evidence_retention")
            if (not isinstance(ledger_sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", ledger_sha256)
                    or not isinstance(retention, str) or not retention.strip()):
                raise ValueError("predecessor evidence binding is missing")
        elif not isinstance(results_commit, str) or not re.fullmatch(r"[0-9a-f]{40}", results_commit):
            raise ValueError("predecessor results commit is malformed")
        if predecessor["outcome_data_observed"] is not True:
            raise ValueError("disclosed post-outcome replication requires an observed predecessor")
        if research_design.get("classification") != DISCLOSED_POST_OUTCOME_CLASSIFICATION:
            raise ValueError("observed predecessor requires disclosed post-outcome replication classification")
        if research_design.get("claim_limit") != DISCLOSED_POST_OUTCOME_CLAIM_LIMIT:
            raise ValueError("observed predecessor claim limit is missing")
        if reporting.get("claim_rule") != DISCLOSED_POST_OUTCOME_CLAIM_RULE:
            raise ValueError("observed predecessor reporting claim is missing")
        if predecessor["study_id"] == data["study_id"]:
            raise ValueError("authenticated study must use a new study id")
        # `predecessor` names only the LAST attempt that observed an outcome, so
        # promoting a newer one displaces the record before it. That ancestor is
        # kept structured rather than dropped to prose because it is the only
        # study in this lineage whose evidence was published, which makes its
        # results commit the contract's one third-party verifiable anchor.
        #
        # Indexed, never `.get`, for the reason the same pattern is indexed in
        # the result-boundary checker: a default would make a deleted record
        # indistinguishable from "there is no such ancestor" and would shrink
        # both this check and the protected evidence roots derived from it,
        # while still exiting successfully.
        ancestor = data["earlier_outcome_ancestor"]
        if not isinstance(ancestor, dict):
            raise ValueError("earlier outcome ancestor record is malformed")
        for key in ("study_id", "results_directory", "results_commit", "ledger_sha256"):
            if not isinstance(ancestor.get(key), str) or not ancestor[key].strip():
                raise ValueError("earlier outcome ancestor record is malformed")
        if not re.fullmatch(r"[0-9a-f]{40}", ancestor["results_commit"]):
            raise ValueError("earlier outcome ancestor results commit is malformed")
        if not re.fullmatch(r"[0-9a-f]{64}", ancestor["ledger_sha256"]):
            raise ValueError("earlier outcome ancestor ledger digest is malformed")
        results_pr = ancestor["results_pr"]
        if isinstance(results_pr, bool) or not isinstance(results_pr, int) or results_pr <= 0:
            raise ValueError("earlier outcome ancestor results PR is malformed")
        if ancestor["outcome_data_observed"] is not True:
            # Only an attempt that observed an outcome can occupy this slot; one
            # that observed nothing is an intervening attempt.
            raise ValueError("earlier outcome ancestor must have observed outcome data")
        if ancestor["study_id"] in (data["study_id"], predecessor["study_id"]):
            raise ValueError("earlier outcome ancestor duplicates another study id")
        intervening = data["intervening_attempts"]
        if not isinstance(intervening, list):
            raise ValueError("intervening attempts must be a list")
        for attempt in intervening:
            # A consumed attempt is what the result-boundary checker derives a
            # protected evidence root from, so a malformed record there silently
            # shrinks a security boundary rather than failing a study gate.
            if not isinstance(attempt, dict):
                raise ValueError("intervening attempt record is malformed")
            for key in ("study_id", "results_directory", "ledger_sha256"):
                if not isinstance(attempt.get(key), str) or not attempt[key].strip():
                    raise ValueError("intervening attempt record is malformed")
            if attempt["outcome_data_observed"] is not False:
                # An attempt that DID observe an outcome belongs in the
                # disclosure lineage, not in this list.
                raise ValueError("an intervening attempt that observed outcome data must be the predecessor")
            if attempt["study_id"] in (data["study_id"], predecessor["study_id"]):
                raise ValueError("intervening attempt duplicates another study id")
        # An attempt that observed outcome data and has since been displaced
        # from `predecessor`. Neither slot above can hold one: `intervening`
        # demands outcome_data_observed false, and `ancestor` demands a
        # published results commit and PR. Until this key existed, promoting a
        # newer attempt into `predecessor` dropped the previous one's evidence
        # directory out of `protocol_private_roots` — a silent shrink arriving
        # through a gap in the schema rather than a deletion, which is why the
        # 2026-08-21 abort's lineage record could not be written when its spend
        # was (audit 864cf832, Voice 1 f5; the reading was confirmed
        # independently by three of the four voices).
        superseded = data["superseded_outcome_attempts"]
        if not isinstance(superseded, list):
            raise ValueError("superseded outcome attempts must be a list")
        for attempt in superseded:
            if not isinstance(attempt, dict):
                raise ValueError("superseded outcome attempt record is malformed")
            for key in ("study_id", "results_directory", "ledger_sha256",
                        "evidence_retention", "decision"):
                if not isinstance(attempt.get(key), str) or not attempt[key].strip():
                    raise ValueError("superseded outcome attempt record is malformed")
            if not re.fullmatch(r"[0-9a-f]{64}", attempt["ledger_sha256"]):
                raise ValueError("superseded outcome attempt ledger digest is malformed")
            if attempt["outcome_data_observed"] is not True:
                # One that observed nothing is an intervening attempt; one that
                # published its evidence is the ancestor. This slot is for
                # neither, and letting it absorb them would make three records
                # interchangeable and the lineage unreadable.
                raise ValueError("a superseded outcome attempt must have observed outcome data")
            # The OTHER half of that distinction, which the first version of
            # this check left to prose: the slot exists for a LOCAL-ONLY
            # attempt. A record carrying a published commit or PR belongs in
            # `earlier_outcome_ancestor`, where the published anchor is the
            # point (audit 26d0a1c2: Voice 1 f6, Voice 2 f4, Voice 5 f5, three
            # of five, who found the slot accepted a record that both slots
            # would take).
            if attempt.get("results_commit") is not None or attempt.get("results_pr") is not None:
                raise ValueError(
                    "a superseded outcome attempt is local-only; a published one is the ancestor")
        # Every lineage study id distinct, across all four kinds at once. The
        # pairwise checks above each know only their own neighbours, so a fourth
        # kind could collide with the third and nothing would notice.
        lineage_ids = [
            data["study_id"], predecessor["study_id"], ancestor["study_id"],
            *(attempt["study_id"] for attempt in intervening),
            *(attempt["study_id"] for attempt in superseded),
        ]
        if len(set(lineage_ids)) != len(lineage_ids):
            raise ValueError("lineage records reuse a study id")
        # Distinct records must imply distinct roots. `protocol_private_roots`
        # derives one protected evidence root per lineage record, so two records
        # naming one directory yield FEWER roots than records — the boundary
        # shrinks and nothing fails. Distinct study ids do not imply distinct
        # directories, so this is a separate check, not a consequence of the
        # ones above.
        directories = [
            data["environment"]["primary_results_directory"],
            predecessor["results_directory"],
            ancestor["results_directory"],
            *(attempt["results_directory"] for attempt in intervening),
            *(attempt["results_directory"] for attempt in superseded),
        ]
        if len(set(directories)) != len(directories):
            raise ValueError("lineage records reuse a results directory")
        # Same argument for evidence: a locally-bound record is evidenced by its
        # digest alone, so a duplicated digest would let one attempt's evidence
        # stand in for another's. The ancestor's digest is included even though
        # it also has a results commit.
        digests = [
            predecessor["ledger_sha256"],
            ancestor["ledger_sha256"],
            *(attempt["ledger_sha256"] for attempt in intervening),
            *(attempt["ledger_sha256"] for attempt in superseded),
        ]
        if len(set(digests)) != len(digests):
            raise ValueError("lineage records reuse a ledger digest")
        # THE CLASS, not the instance. `superseded_outcome_attempts` was added
        # because one attempt had its money recorded and no lineage record, and
        # nothing would have caught that: the spend list and the lineage blocks
        # never referred to each other. Now every ledger digest named in a spend
        # component must appear in some lineage record, so the next promotion
        # cannot leave an attempt paid-for and unrecorded (audit 26d0a1c2,
        # Voice 1 f8).
        #
        # Digests only. A component may name none — several early charges are
        # diagnostics with no ledger of their own — and this does not require
        # one; it requires that a digest, once written down as money, is
        # traceable to the attempt that spent it.
        recorded_digests = set(digests)
        for component in data["owner_authorization"]["prior_spend_components"]:
            for digest in re.findall(r"\b[0-9a-f]{64}\b", str(component.get("label", ""))):
                if digest not in recorded_digests:
                    raise ValueError(
                        "a spend component names a ledger digest with no lineage record")
        primary_results_directory(data)
        if not (0 < validity["minimum_completion_rate"] <= 1 and 0 <= validity["maximum_infra_rate"] <= 1
                and 0 <= validity["maximum_budget_capped_rate"] <= 1):
            raise ValueError("validity rates must be probabilities")
        if not (validity["isolation_failure_invalidates_study"]
                and validity["file_workspace_failure_invalidates_study"]
                and validity["stream_parse_error_invalidates_study"]):
            raise ValueError("primary validity hard checks must be enabled")
        if analysis["primary_contrast"] != ["aqg-full", "claude-md-lite"]:
            raise ValueError("primary contrast is not frozen")
        # Every non-primary pairwise contrast, derived rather than counted. The
        # check was `!= 5`, which was the count for the four-arm matrix; a bare
        # count would have accepted any two pairs after amendment A5 dropped an
        # arm, including two that are not the ones the arms imply.
        expected_secondary = [
            sorted(pair) for pair in itertools.combinations(sorted(matrix["arms"]), 2)
            if sorted(pair) != sorted(analysis["primary_contrast"])
        ]
        if sorted(sorted(pair) for pair in analysis["secondary_contrasts"]) != sorted(expected_secondary):
            raise ValueError("all non-primary pairwise contrasts must be frozen")
        if not (0 < float(analysis["alpha"]) <= 1):
            raise ValueError("analysis alpha must be a probability")
        # The Owner's 2026-08-23 ruling — the Holm divisor stays at the
        # pre-narrowing family — lived only in a data key that nothing read
        # back, and `analyze` fetched it with `.get()`. A protocol that lost
        # the key would have analysed at the lenient divisor of two and said
        # nothing (audit 6867c40e: all five voices, convergent). It is a
        # contract term now, and absence is a KeyError by design.
        declared_family = analysis["secondary_family_size"]
        if isinstance(declared_family, bool) or not isinstance(declared_family, int):
            raise ValueError("secondary family size must be an integer")
        if declared_family < PRE_NARROWING_SECONDARY_FAMILY:
            raise ValueError(
                "secondary family size may not fall below the pre-narrowing "
                f"{PRE_NARROWING_SECONDARY_FAMILY}")
        if declared_family < len(analysis["secondary_contrasts"]):
            raise ValueError("secondary family size is smaller than the contrasts declared")
        if matrix["allowed_tools"] != DEFAULT_ALLOWED_TOOLS.split() or matrix["timeout_seconds"] <= 0:
            raise ValueError("primary tool surface or timeout is malformed")
        validate_workspace_boundary_contract(data)
        hashes = (
            data["aqg_skills_tree_sha256"], data["task_tree_sha256"],
            provenance["aqg_rules_sha256"], provenance["claude_md_lite_rules_sha256"],
        )
        if any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value) for value in hashes):
            raise ValueError("frozen source hash is malformed")
        power = analysis["power_simulation"]
        if len(power["lite_rules_defect_rates"]) != len(matrix["tasks"]) or len(power["aqg_defect_rates"]) != len(matrix["tasks"]):
            raise ValueError("power simulation does not cover every frozen task")
        if environment["budget_result_subtype"] != "error_max_budget":
            raise ValueError("budget result subtype is not frozen")
    except (KeyError, TypeError, ValueError) as exc:
        raise UsageError(f"invalid protocol {path}: {exc}") from exc
    return data


def protocol_sha256(path: Path = PROTOCOL_FILE) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_primary_request(
    protocol: dict[str, Any],
    *,
    tasks: list[str],
    arms: list[str],
    strictness: list[str],
    runs: int,
    model: str,
    allowed_tools: str,
    timeout: int,
) -> None:
    """Reject any primary-run drift instead of silently relabelling it."""
    matrix = protocol["matrix"]
    requested = {
        "tasks": tasks,
        "arms": arms,
        "strictness": strictness,
        "runs": runs,
        "model": model,
        "allowed_tools": allowed_tools.split(),
        "timeout_seconds": timeout,
    }
    expected = {
        "tasks": matrix["tasks"],
        "arms": matrix["arms"],
        "strictness": matrix["strictness"],
        "runs": matrix["runs_per_task_arm"],
        "model": matrix["model"],
        "allowed_tools": matrix["allowed_tools"],
        "timeout_seconds": matrix["timeout_seconds"],
    }
    drift = [name for name in expected if requested[name] != expected[name]]
    if drift:
        raise UsageError("primary request differs from frozen protocol: " + ", ".join(drift))


def clean_child_env(parent: dict[str, str] | None = None) -> dict[str, str]:
    """Allowlist only the values Claude needs without inheriting API-key env vars."""
    source = os.environ if parent is None else parent
    try:
        home = source["HOME"]
    except KeyError as exc:
        raise UsageError("HOME is required for the authenticated Claude CLI") from exc
    try:
        user = source["USER"]
    except KeyError as exc:
        raise UsageError("USER is required for the authenticated Claude CLI") from exc
    if not isinstance(user, str) or not user.strip():
        raise UsageError("USER is required for the authenticated Claude CLI")
    return {
        # Set defensively. The evidence originally cited for this switch — the
        # binary's own help text documenting it — does NOT hold on the installed
        # CLI: on 2.1.237 (checked 2026-08-20) `claude --help` mentions
        # auto-memory only inside the `--bare` description and does not document
        # this variable at all. So its recognition is UNVERIFIED on the binary
        # that will run the matrix — absence from help text is not proof the
        # binary ignores it, and equally not proof that it acts on it. The
        # consequence cuts both ways and is recorded as a limitation rather than
        # argued away: if the switch is inert, auto-memory is ON, which is a
        # cross-cell carryover pathway for a defect-rate measurement. The prior
        # out-of-root project-level read is not claimed to be caused or cured
        # by it.
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "PATH": source.get("PATH", os.defpath),
        "HOME": home,
        "USER": user,
        "LANG": source.get("LANG", "C.UTF-8"),
        "LC_ALL": source.get("LC_ALL", "C.UTF-8"),
        "TERM": source.get("TERM", "dumb"),
    }


def directory_tree_sha256(
    root: Path, *, ignored_parts: frozenset[str] = _FROZEN_SOURCE_IGNORED_PARTS,
) -> str:
    """Portable content hash of every regular file under one source root.

    Plugins can expose commands, agents, hooks, or other source beside
    ``skills/``. Hashing only a manifest and skill tree would leave those
    executable surfaces mutable during a primary run.
    """
    digest = hashlib.sha256()
    if not root.is_dir():
        raise UsageError(f"source directory is missing: {root}")
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if set(relative.parts) & ignored_parts:
            continue
        if path.is_symlink():
            raise UsageError(f"symlink is forbidden in frozen source tree: {relative}")
        if not path.is_file():
            continue
        digest.update(relative.as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def plugin_tree_sha256(plugin_dir: Path) -> str:
    """Hash the no-hook plugin treatment, excluding caches and hook runtime."""
    return directory_tree_sha256(
        plugin_dir, ignored_parts=_PLUGIN_IGNORED_PARTS
    )


def plugin_snapshot_sha256(plugin_dir: Path) -> str:
    """Hash every executed plugin file, including any accidentally copied hook."""
    return directory_tree_sha256(plugin_dir)


def _trusted_snapshot_dir(prefix: str) -> Path:
    """Create a plugin snapshot below /Users, which the scorer denies."""
    root = Path.home() / ".cache" / "aqg-ws7-snapshots"
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


# The ONE remote whose `main` this study calls authoritative, in the spellings
# git accepts for it. An allowlist, deliberately, and not a "does this look like
# a path" test: four voices of four (audit 0e29d811) found that the second
# question is the wrong one. `https://localhost/x`, `ssh://git@elsewhere/x` and
# simply ANOTHER GitHub repository all look like remotes, and fetching their
# `main` proves what THEY serve — not that the frozen commit is merged into this
# study's authoritative branch. An allowlist has one error direction and it is
# refusal.
# Both must appear in the quoted span. Word boundaries matter on the sha: a
# 64-hex ledger digest contains a 40-hex run, and an unanchored pattern would
# happily nominate the first forty characters of one.
_ATTESTED_MAIN_MARKERS = ("origin/main", "deeppatternai/agent-quality-gates")
_SHA40_IN_TEXT = re.compile(r"\b[0-9a-f]{40}\b")

_AUTHORITATIVE_ORIGIN_FORMS = frozenset({
    "https://github.com/deeppatternai/agent-quality-gates",
    "ssh://git@github.com/deeppatternai/agent-quality-gates",
    "git@github.com:deeppatternai/agent-quality-gates",
})


def attested_authoritative_main(protocol: dict[str, Any]) -> dict[str, Any]:
    """The Owner-attested authoritative main, or an empty mapping.

    A single lookup because two callers need it — `require_owner_attestation`,
    which validates it against the Owner's bytes, and the primary reservation,
    which hands the sha to `require_frozen_checkout`. Two hand-written key paths
    could drift onto different fields, and the failure would be silent in the
    direction that matters least (the gate would receive None and refuse) while
    hiding that the validated value and the trusted value were never the same
    one (audit 0e29d811, Voice 1 f4).
    """
    authorization = protocol.get("owner_authorization") or {}
    evidence = authorization.get("extension_evidence") or {}
    return evidence.get("authoritative_main") or {}


# Where the Owner writes, and this author cannot. Pinned here rather than taken
# from the protocol: the protocol lives in the frozen checkout and this author
# edits it, so a pointer read from there could name any file. The directory and
# the account name are the two facts the whole mechanism rests on, so they are
# frozen with the code that uses them.
COLLECTION_ATTESTATION_DIR = Path("/var/db/aqg-ws7-attestations")
COLLECTION_ACCOUNT_UID_NAME = "ws-7tester"


def _uid_name(path: Path) -> str:
    """The owning account's name, or the numeric uid when it has none."""
    uid = path.stat().st_uid
    try:
        return pwd.getpwuid(uid).pw_name
    except KeyError:
        return str(uid)


def attested_main_from_the_collection_account(
    path: Path, *,
    attestation_dir: Path = COLLECTION_ATTESTATION_DIR,
    owner_uid_name: str = COLLECTION_ACCOUNT_UID_NAME,
) -> str:
    """The authoritative main the Owner verified, read at RUN TIME.

    WHY NOT FROM THE PROTOCOL. The attested sha used to be read from the
    protocol inside the frozen checkout, and that could not be made to work: the
    commit carrying the attestation moves `main` past the sha the attestation
    names, and no attestation can name the commit that carries it. Attesting
    again does not help — the cycle is structural. Reading the original at run
    time means nothing has to be committed before the run.

    It is also the stronger source. The committed copy is a file this author can
    edit; the original lives on an account this author cannot write. The worst a
    tampered pointer can do is name an OLDER file in the same directory — every
    file there is Owner-written — and an older sha fails the ancestry check in
    `require_frozen_checkout`, which is the direction that costs a refusal.
    """
    resolved = path.resolve()
    if resolved.parent != attestation_dir.resolve():
        raise UsageError(
            f"the attested main file is not in the attestation directory "
            f"({attestation_dir}); a file elsewhere proves nothing about who wrote it")
    if not resolved.is_file():
        raise UsageError(f"the attested main file {path} is not a file")
    actual = _uid_name(resolved)
    if actual != owner_uid_name:
        raise UsageError(
            f"the attested main file is not written by {owner_uid_name} (owner: {actual}); "
            "the assertion is trusted only because this author cannot write that account")
    text = resolved.read_text(encoding="utf-8")
    for marker in _ATTESTED_MAIN_MARKERS:
        if marker not in text:
            raise UsageError(
                f"the attested main file does not contain {marker!r}; a file the author can "
                "point at is not an assertion about the authoritative branch merely because "
                "it sits in the right directory")
    found = _SHA40_IN_TEXT.findall(text)
    if len(found) != 1:
        raise UsageError(
            f"the attested main file carries {len(found)} commit ids and must carry exactly "
            "one, so the sha the gate trusts is unambiguous")
    return found[0]


def origin_is_the_authoritative_remote(url: str) -> bool:
    """Is `origin` the remote whose `main` this study treats as authoritative?

    Plan E (Owner, 2026-08-26) points the collection checkout's origin at a
    local mirror so that account can hold no GitHub credentials. The 2026-08-15
    decision that first did this recorded the price and accepted it: with a
    mirror origin, `require_frozen_checkout` proves the frozen commit is an
    ancestor of A COPY ON THIS HOST, not of the authoritative branch — and it
    said the same sentence either way, so the weaker claim was invisible.

    Anything that is not on the allowlist needs the Owner's attested sha. That
    covers the mirror this study actually uses, and it also covers the cases a
    local/remote heuristic waves through: another repository on the same host,
    a loopback URL, a look-alike org, a `file://` path.
    """
    normalised = url.strip().casefold().rstrip("/")
    if normalised.endswith(".git"):
        normalised = normalised[:-len(".git")]
    return normalised in _AUTHORITATIVE_ORIGIN_FORMS


def require_frozen_checkout(expected_commit: str, *, required_ancestors: Sequence[str] = (),
                            attested_main: str | None = None) -> str:
    """Primary data can only run from the clean, already-merged protocol commit.

    `required_ancestors` carries the commits the protocol claims fixed a cause
    that consumed an earlier attempt. Asserting that in prose is not enough:
    this study already lost its predecessor to an infrastructure bug, and
    proving the checkout is an ancestor of origin/main does NOT prove the fix
    is in it — every commit before the fix satisfies that too.
    """
    status = subprocess.run(
        ["git", "status", "--porcelain"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if status.returncode or status.stdout.strip():
        raise UsageError("primary run requires a clean checkout")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, check=False
    )
    if head.returncode or head.stdout.strip() != expected_commit:
        raise UsageError("primary run checkout does not match --frozen-commit")
    try:
        refreshed = subprocess.run(
            ["git", "fetch", "--quiet", "origin", "main"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False, timeout=30,
            # A helper that wants to prompt has no terminal here, so without
            # this it blocks until the timeout and the refusal can only name
            # the clock. With it git fails at once and says which credential it
            # could not read.
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except subprocess.TimeoutExpired as exc:
        # Whatever git managed to say before the kill, not a guess at why.
        # `text=True` does NOT decode what is attached to this exception —
        # measured as bytes on this runner's CPython 3.9.6 — so it goes through
        # `_as_text` like every other captured stream here. Left uncaught this
        # escaped `main`'s `except UsageError` as a traceback.
        detail = _as_text(exc.stderr).strip()[-500:]
        raise UsageError(
            "git fetch of origin/main timed out after 30s" + (f": {detail}" if detail else "")
        ) from exc
    if refreshed.returncode:
        # git's own stderr, not a paraphrase. This runs as the collection
        # account, which does not own the checkout, so the usual causes are
        # ones only git can name: no write permission on `.git`, no credential
        # helper, a refused network. Same convention as the regression-suite
        # gate below — a free preflight is where the difference gets named.
        raise UsageError(
            f"cannot refresh origin/main before primary reservation: {refreshed.stderr.strip()[-500:]}"
        )
    origin_main = subprocess.run(
        ["git", "rev-parse", "origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if origin_main.returncode or not re.fullmatch(r"[0-9a-f]{40}", origin_main.stdout.strip()):
        raise UsageError("cannot resolve origin/main for merged-protocol provenance")
    resolved = origin_main.stdout.strip()
    # WHAT `origin/main` MEANS depends on what origin is, and until plan E the
    # gate did not look. See `origin_is_a_local_mirror`.
    origin_url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if origin_url.returncode:
        raise UsageError("cannot resolve the origin remote for merged-protocol provenance")
    if not origin_is_the_authoritative_remote(origin_url.stdout):
        if attested_main is None:
            raise UsageError(
                "origin is not the authoritative remote, so origin/main proves only what "
                "this origin serves and not that the frozen commit is merged upstream; the "
                "protocol must carry the authoritative main the Owner attested from the "
                "collection account")
        attested = attested_main.strip()
        if not re.fullmatch(r"[0-9a-f]{40}", attested):
            raise UsageError("the Owner-attested authoritative main is malformed")
        # AGAINST THE FROZEN COMMIT, not against the mirror's `origin/main`.
        # What the Owner vouched for is that GitHub's main was `attested` when
        # they looked; a frozen commit at or before that point is therefore on
        # GitHub's main. Comparing against the mirror's CURRENT main instead
        # made the check unsatisfiable — main moves the moment anything is
        # committed, including the attestation record itself.
        covered = subprocess.run(
            ["git", "merge-base", "--is-ancestor", expected_commit, attested],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        if covered.returncode:
            raise UsageError(
                "the frozen commit is not an ancestor of the main the Owner verified, so "
                "the Owner did not verify it; freeze at or before the attested commit, or "
                "have the Owner attest a later one")
    merged = subprocess.run(
        ["git", "merge-base", "--is-ancestor", expected_commit, "origin/main"],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False,
    )
    if merged.returncode:
        raise UsageError("--frozen-commit is not an ancestor of origin/main; wait for protocol PR merge")
    for ancestor in required_ancestors:
        if not re.fullmatch(r"[0-9a-f]{40}", ancestor):
            raise UsageError("protocol names a malformed fix commit")
        carried = subprocess.run(
            ["git", "merge-base", "--is-ancestor", ancestor, "HEAD"],
            cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
        if carried.returncode:
            raise UsageError("primary checkout does not carry a fix commit the protocol requires")
    return resolved


def task_tree_sha256(task_dirs: list[Path]) -> str:
    """Hash every committed task/scorer file selected for a primary run."""
    digest = hashlib.sha256()
    for task_dir in sorted(task_dirs):
        for path in sorted(task_dir.rglob("*")):
            relative = path.relative_to(TASKS_DIR)
            if set(relative.parts) & _FROZEN_SOURCE_IGNORED_PARTS:
                continue
            if path.is_symlink():
                raise UsageError(f"symlink is forbidden in frozen task tree: {relative}")
            if not path.is_file():
                continue
            digest.update(path.relative_to(TASKS_DIR).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _under_users(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path("/Users"))
        return True
    except ValueError:
        return False


def validate_treatment_provenance(protocol: dict[str, Any]) -> None:
    """Fail before reservation when either administered prompt drifts."""
    provenance = protocol["treatment_provenance"]
    if hashlib.sha256(AQG_RULES_FILE.read_bytes()).hexdigest() != provenance["aqg_rules_sha256"]:
        raise UsageError("AQG rules content hash differs from frozen protocol")
    if hashlib.sha256(CLAUDE_MD_LITE_RULES.encode("utf-8")).hexdigest() != provenance["claude_md_lite_rules_sha256"]:
        raise UsageError("light-rules content hash differs from frozen protocol")


def validate_workspace_boundary_contract(protocol: dict[str, Any]) -> None:
    """Fail before reservation if the shared workspace-boundary prompt drifts."""
    instruction = protocol["matrix"].get("workspace_boundary_instruction")
    if instruction != WORKSPACE_BOUNDARY_INSTRUCTION:
        raise UsageError("workspace-boundary instruction differs from frozen protocol")
    neutral = render_prompt("Implement the task.", "seed.py", NEUTRAL)
    competing = render_prompt("Implement the task.", "seed.py", COMPETING)
    if (WORKSPACE_BOUNDARY_INSTRUCTION not in neutral
            or WORKSPACE_BOUNDARY_INSTRUCTION not in competing):
        raise UsageError("workspace-boundary instruction is absent from a runnable prompt")
    if protocol["environment"].get("model_file_permission_denies") != list(CELL_PERMISSION_DENY_RULES):
        raise UsageError("model file-permission denies differ from frozen protocol")


def require_verified_amendments(protocol: dict[str, Any], *, root: Path | None = None) -> None:
    """Refuse a paid primary matrix while a boundary amendment is unproven.

    An amendment to the shared execution boundary is a documentation change
    until a live run shows the amended rules actually do what they say. The
    previous deny-rule spelling passed every test in this suite for its whole
    life while being inert, so a static check is not evidence. This gate is on
    the PRIMARY path only — `load_protocol` must stay usable by the probe that
    produces the verification, or the gate would block its own key.

    `root` is the same seam `require_amendment_evidence` already carries, and
    exists for the same reason: between a payload amendment and the probe that
    attests it, NO committed report can carry the live fingerprint, so a test
    that wants to exercise the passing path has nowhere honest to point. The
    alternative was committing a fixture nobody measured, which is precisely the
    forgery this chain exists to refuse. No production call site passes it.
    """
    claimed: dict[str, str] = {}
    for amendment in protocol.get("amendments") or ():
        if amendment.get("verified") is not True:
            raise UsageError(
                f"protocol amendment {amendment.get('id')} to "
                f"{amendment.get('field')} is not verified: "
                f"{amendment.get('verification_required')}")
        require_amendment_evidence(amendment, claimed=claimed, protocol=protocol, root=root)


# Classes where a paid live call could add nothing: `documentation` records what
# a no-cost mechanical observation returned, `mechanical` what a named
# re-runnable command returned. Neither can be claimed for a field that decides
# what the model may actually do (audit 9d92d22c, Voice 1 f7 asked for the
# second route so an honest non-probe amendment is not pushed into mislabelling
# itself, which is the flag pressure this whole gate exists to remove).
# C(4,2) pairs over the four-arm matrix, less the primary: the size the
# secondary family had when a summary of this matrix was first read. Amendment
# A5 narrowed the arms for non-statistical reasons, so the divisor stays here.
PRE_NARROWING_SECONDARY_FAMILY = 5

NO_PROBE_AMENDMENT_CLASSES = ("documentation", "mechanical")
# Fields that decide what the model may actually do or receive. A no-probe
# class can never discharge an amendment that names one.
#
# `aqg_skills_tree_sha256` joined them on 2026-08-23. It freezes the tree
# `prepare_aqg_plugin` copies WHOLE into the plugin the treatment arm receives,
# so re-freezing it changes the object the study measures. A withdrawn draft of
# amendment A6 classed exactly that as `documentation` — a no-probe class — and
# paid for the shortcut with a paragraph conceding it was one. Five voices of
# five (audit 5d6f7bf1) refused it: disclosure does not do the work of the gate
# it bypasses. The field is listed here so that route is closed by machine
# rather than by the author's restraint.
OPERATIVE_AMENDMENT_FIELDS = (
    "environment.model_file_permission_denies",
    "aqg_skills_tree_sha256",
)

# Classes whose evidence is a DETERMINISTIC report rather than a paid probe.
#
# These are NOT no-probe classes and must never be added to
# NO_PROBE_AMENDMENT_CLASSES: they still require `verified_by_fixture`, and the
# fixture is re-derived below rather than read. The gap this fills is real —
# some operative changes have no live behaviour a probe could observe (which
# tree is frozen is a fact about bytes on disk, not about a model), and before
# this the only routes were "cite a probe report" or "claim a no-probe class".
# An author facing that pair will take the second, which is what happened.
DETERMINISTIC_EVIDENCE_CLASSES = ("treatment-freeze",)


def yaml_frontmatter(text: str) -> str:
    """The YAML block between the first two `---` FENCE LINES, or "" if absent.

    Line-anchored, because a substring scan is wrong in a way that matters here.
    Three voices of audit 6d31ad5c found it: `note: a---b` inside a value ended
    the block early, and a `----` rule was read as a fence. Both produce a
    frontmatter digest over the wrong bytes, and `frontmatter_unchanged` — the
    claim amendment A6's reachability argument leans on — could then be
    certified true while the real frontmatter differed.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return ""
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "\n".join(lines[1:index])
    return ""


def cell_permission_payload_fingerprint(extra_denies: Sequence[str] = ()) -> str:
    """Return a digest of the exact ordered deny payload a cell would send."""
    return hashlib.sha256(
        cell_permission_settings_json(extra_denies).encode("utf-8")).hexdigest()


def operative_fields_named(field: Any) -> frozenset[str]:
    """Which operative fields a `field` value names, however it is written."""
    if isinstance(field, (list, tuple, set)):
        rendered = " , ".join(str(item) for item in field)
    elif isinstance(field, dict):
        rendered = " , ".join(f"{key} {value}" for key, value in field.items())
    else:
        rendered = str(field or "")
    return frozenset(name for name in OPERATIVE_AMENDMENT_FIELDS if name in rendered)


def _names_an_operative_field(field: Any) -> bool:
    """True when a `field` value names an operative field, however it is written.

    The guard began as a whole-string membership test, which no comma-joined
    value could ever satisfy. Splitting on commas fixed that case and left the
    rest: a semicolon or newline joiner, a mapping, a nested list, a JSON
    pointer (audit 6867c40e, four voices, three of them blocking). Rather than
    enumerate separators, this renders the value and asks whether an operative
    name appears anywhere in it. The only way to be wrong is to demand a probe
    for an amendment that merely mentions the field in passing, which costs a
    probe; the other direction costs an unprobed boundary change.
    """
    return bool(operative_fields_named(field))


def _require_matching_digest(bad: Any, rel: str, label: str, text: str,
                             claimed_digest: Any) -> None:
    """Recompute one digest from bytes and refuse a fixture that disagrees."""
    recomputed = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if claimed_digest != recomputed:
        raise bad(f"records a {label} for {rel} that does not match the bytes "
                  f"({str(claimed_digest)[:12]} vs {recomputed[:12]})")


def require_deterministic_evidence(
    identifier: Any, fixture: str, report: dict[str, Any], *, superseded: str,
) -> None:
    """Re-derive a treatment-freeze report. Nothing in it is taken on trust.

    The A6 review (audit 5d6f7bf1) found the same defect twice over: the
    amendment asserted facts about the treatment tree, and the tests that were
    supposed to check them re-read the fixture's own fields and compared them to
    each other. Four voices of five called that trusting, not verifying. So this
    runs on the money-gate path and recomputes every claim from bytes on disk:

      * `current_sha256` against the live tree;
      * each file's `after_sha256` and frontmatter digest against the live file;
      * each file's `before_sha256` and frontmatter digest against the
        `before_text` the fixture must carry;
      * and `previous_sha256` by rebuilding a shadow tree from those before-texts.

    That last one also proves COMPLETENESS, which is why it is not optional: if
    substituting only the listed files reproduces the previous hash, then those
    files are the whole of the difference and the fixture cannot be
    under-reporting what moved.
    """
    def bad(why: str) -> UsageError:
        return UsageError(
            f"amendment {identifier} cites deterministic report {fixture}, which {why}")

    if report.get("record_type") != "treatment_refreeze":
        raise bad("is not a treatment_refreeze record")
    previous = report.get("previous_sha256")
    current = report.get("current_sha256")
    if not isinstance(previous, str) or not isinstance(current, str) or previous == current:
        raise bad("does not record two distinct tree digests")
    # The anchor. `previous` is now the digest the amendment declares it
    # supersedes, so the rebuild below reproduces a stated history rather than
    # one the fixture invented for itself.
    if previous != superseded:
        raise bad(f"rebuilds {previous[:12]}, which is not the {superseded[:12]} its "
                  "amendment declares it supersedes")
    live = directory_tree_sha256(AQG_SKILLS_DIR)
    if current != live:
        raise bad(f"claims a current tree {current[:12]} that is not the live tree {live[:12]}")
    entries = report.get("per_file")
    if not isinstance(entries, list) or not entries:
        raise bad("lists no changed files")
    listed = report.get("changed_files")
    if not isinstance(listed, list) or sorted(listed) != sorted(
            str(e.get("path")) for e in entries):
        raise bad("has a changed_files list that disagrees with its own per-file entries")

    with tempfile.TemporaryDirectory() as scratch:
        shadow = Path(scratch) / "skills"
        shutil.copytree(AQG_SKILLS_DIR, shadow, symlinks=False)
        seen: set[str] = set()
        for entry in entries:
            rel = str(entry.get("path") or "")
            # A refreeze is not always a modification. A wrapper regeneration can
            # add a skill or drop one, and an earlier draft of this function
            # handled neither: an addition has no before_text to give, a deletion
            # names a path that is not in the live tree, and both were refused by
            # checks written for the modify case. Refusing is the safe direction
            # but it made the route unusable for changes it claims to cover, so
            # the three cases are explicit now.
            status = str(entry.get("status") or "modified")
            if status not in ("modified", "added", "deleted"):
                raise bad(f"gives {rel} the unknown status {status!r}")
            if rel in seen:
                raise bad(f"lists {rel} twice")
            seen.add(rel)
            try:
                relative = Path(rel).relative_to(AQG_SKILLS_DIR.relative_to(REPO_ROOT))
            except ValueError as exc:
                raise bad(f"names {rel}, which is not inside the frozen tree") from exc
            if ".." in relative.parts or relative.is_absolute():
                raise bad(f"names {rel}, which is not a plain path inside the frozen tree")
            live_file = AQG_SKILLS_DIR / relative
            before_text = entry.get("before_text")
            wants_before = status in ("modified", "deleted")
            # `is None`, not falsiness: a file that was EMPTY at the previous
            # freeze is a real state, and rejecting "" would make it
            # inexpressible while looking like a missing field.
            if wants_before and not isinstance(before_text, str):
                raise bad(f"carries no before_text for {rel}, so nothing can be re-derived")
            if status == "added" and before_text is not None:
                raise bad(f"marks {rel} as added and still carries a before_text")

            if status == "deleted":
                if live_file.exists():
                    raise bad(f"marks {rel} as deleted, but it is present in the live tree")
                target = shadow / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(before_text, encoding="utf-8")
                _require_matching_digest(bad, rel, "before_sha256", before_text,
                                         entry.get("before_sha256"))
                continue

            if not live_file.is_file():
                raise bad(f"names {rel}, which is not a file in the live tree")
            try:
                live_text = live_file.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                # `directory_tree_sha256` hashes bytes, so a non-text file can sit
                # in the frozen tree quite legitimately. It cannot be re-derived
                # by this function, and crashing on it would turn a protocol
                # problem into a traceback on the money-gate path.
                raise bad(f"names {rel}, which is not UTF-8 text and cannot be "
                          f"re-derived by this route: {exc}") from exc

            if status == "added":
                _require_matching_digest(bad, rel, "after_sha256", live_text,
                                         entry.get("after_sha256"))
                (shadow / relative).unlink()
                continue

            _require_matching_digest(bad, rel, "after_sha256", live_text,
                                     entry.get("after_sha256"))
            _require_matching_digest(bad, rel, "before_sha256", before_text,
                                     entry.get("before_sha256"))
            _require_matching_digest(bad, rel, "frontmatter_sha256_after",
                                     yaml_frontmatter(live_text),
                                     entry.get("frontmatter_sha256_after"))
            _require_matching_digest(bad, rel, "frontmatter_sha256_before",
                                     yaml_frontmatter(before_text),
                                     entry.get("frontmatter_sha256_before"))
            unchanged = entry.get("frontmatter_unchanged")
            really = yaml_frontmatter(before_text) == yaml_frontmatter(live_text)
            if unchanged is not None and bool(unchanged) is not really:
                raise bad(f"claims frontmatter_unchanged={unchanged!r} for {rel}, "
                          f"which is not what the bytes say")
            (shadow / relative).write_text(before_text, encoding="utf-8")
        rebuilt = directory_tree_sha256(shadow)

    if rebuilt != previous:
        raise bad(
            f"does not reproduce its own previous digest: substituting the listed files "
            f"gives {rebuilt[:12]}, not {previous[:12]}. Either a digest is wrong or the "
            "listed files are not the whole of the difference")


def require_amendment_evidence(
    amendment: dict[str, Any], *, claimed: dict[str, str] | None = None,
    root: Path | None = None, protocol: dict[str, Any] | None = None,
) -> None:
    """Refuse a `verified: true` that rests on prose instead of a probe report.

    Audit cb1ae3ce, five voices of five: the first version of this check lived
    only in the test suite and accepted any path that happened to exist on
    disk, so an amendment could be self-certified by pointing at `run.py`. It
    is production now, on the same path as the money gate, and it demands a
    committed report that records a PASS and that the payload reached the CLI.
    """
    root = PROTOCOL_FILE.parent if root is None else root
    claimed = {} if claimed is None else claimed
    identifier = amendment.get("id")
    # An amendment that declares an Owner decision is a precondition cannot be
    # verified until that decision is recorded. Prose in `limitations` saying
    # "REQUIRES explicit Owner acceptance" is not a control if nothing reads it
    # (audit 9d92d22c: Voice 1 f4, Voice 2 f2, Voice 4 f3, Voice 5 f5).
    if str(amendment.get("owner_decision_required") or "").strip():
        acceptance = str(amendment.get("owner_acceptance") or "").strip()
        if not acceptance:
            raise UsageError(
                f"amendment {identifier} names an Owner decision as a precondition "
                "and no owner_acceptance is recorded; the Owner has to answer it "
                "before this amendment can be verified")
        if not re.match(r"^\d{4}-\d{2}-\d{2}\b", acceptance):
            raise UsageError(
                f"amendment {identifier} records an owner_acceptance that does not "
                "begin with the date it was given")
        # A date and a non-empty string were the WHOLE check, and that is the
        # shape of every failure this record has had: the numbered list grows,
        # the acceptance does not, and nothing notices. Four times now — twice
        # by omission, once in the very edit fixing the second, and once for an
        # item that lived in A1 rather than A3 (audits fa871c95, 64d73929,
        # 541694ec, 10b4813c). So the residuals still standing are declared as
        # data, and the acceptance must name every one of them.
        standing = amendment.get("residuals_standing")
        if standing is not None:
            if not isinstance(standing, list) or not standing or not all(
                    isinstance(item, str) and item.strip() for item in standing):
                raise UsageError(
                    f"amendment {identifier} declares residuals_standing that is "
                    "not a non-empty list of labels")
            missing = [item for item in standing if item not in acceptance]
            if missing:
                raise UsageError(
                    f"amendment {identifier} records an owner_acceptance that does "
                    f"not name every residual still standing; missing: "
                    f"{', '.join(missing)}. The Owner accepts each residual by "
                    "name, not a summary of them")
    if amendment.get("class") in NO_PROBE_AMENDMENT_CLASSES:
        if _names_an_operative_field(amendment.get("field")):
            raise UsageError(
                f"amendment {identifier} claims a no-probe class for the operative "
                f"field {amendment.get('field')}; an operative change is not "
                "discharged by mechanical observation. Cite a probe report, or — "
                "where the change has no live behaviour for a probe to see — use a "
                f"class in {DETERMINISTIC_EVIDENCE_CLASSES} and a report this code "
                "can re-derive")
        if not str(amendment.get("verified_by") or "").strip():
            raise UsageError(f"amendment {identifier} states no verification at all")
        return
    deterministic_route = amendment.get("class") in DETERMINISTIC_EVIDENCE_CLASSES
    superseded = ""
    if deterministic_route:
        # Bound to the field, not only to the class. Dispatching on `class`
        # alone made this route a probe bypass for ANY operative field — an
        # amendment widening the deny payload could have written
        # `class: treatment-freeze`, attached a tree report saying nothing about
        # denies, and skipped the probe requirement entirely. Five voices of
        # five (audit 6d31ad5c) called that the same hole in a new place.
        named = operative_fields_named(amendment.get("field"))
        if named != frozenset({"aqg_skills_tree_sha256"}):
            raise UsageError(
                f"amendment {identifier} claims the deterministic class "
                f"{amendment.get('class')!r} but names operative fields "
                f"{sorted(named) or '[]'}; that class discharges a treatment-tree "
                "refreeze and nothing else")
        # And the baseline the fixture rebuilds has to be the digest this
        # amendment SUPERSEDES, declared in the amendment where a reviewer sees
        # it in the pull-request diff. Without this the fixture chose its own
        # history: `previous_sha256` and every `before_text` came from the same
        # author, so the rebuild proved internal consistency and nothing else
        # (audit 6d31ad5c, all five voices).
        superseded = str(amendment.get("supersedes_tree_sha256") or "").strip()
        if not re.fullmatch(r"[0-9a-f]{64}", superseded):
            raise UsageError(
                f"amendment {identifier} takes the deterministic route without "
                "declaring supersedes_tree_sha256, so its fixture would be free to "
                "invent the history it claims to reproduce")
        retired = list((protocol or {}).get("retired_aqg_skills_tree_sha256") or [])
        if superseded not in retired:
            raise UsageError(
                f"amendment {identifier} supersedes tree {superseded[:12]}, which is "
                "not in the protocol's retired_aqg_skills_tree_sha256 chain; a "
                "refreeze appends the digest it replaces so the chain stays visible")

    fixture = str(amendment.get("verified_by_fixture") or "").strip()
    if not fixture:
        raise UsageError(
            f"amendment {identifier} is verified but names no committed report; set "
            "verified_by_fixture, or a no-probe class if the amendment names no "
            "operative field")
    # The report must live in this repository. An absolute path or a traversal
    # would let an amendment cite a file nobody reviewed (audit 9d92d22c,
    # Voice 3 f2, Voice 4 f2).
    candidate = Path(fixture)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UsageError(
            f"amendment {identifier} names probe report {fixture}, which is not a "
            "repository-relative path")
    path = (root / candidate).resolve()
    if not _is_under(path, root.resolve()):
        raise UsageError(
            f"amendment {identifier} names probe report {fixture}, which resolves "
            "outside the protocol directory")
    settled = str(path)
    if settled in claimed:
        raise UsageError(
            f"amendment {identifier} reuses the probe report already claimed by "
            f"{claimed[settled]}; one report verifies one amendment")
    claimed[settled] = str(identifier)
    if not fixture.endswith(".json") or not path.is_file():
        raise UsageError(
            f"amendment {identifier} names probe report {fixture}, which is not a "
            "committed JSON report")
    if amendment.get("class") in DETERMINISTIC_EVIDENCE_CLASSES:
        try:
            deterministic = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"amendment {identifier} names deterministic report {fixture}, which is "
                f"not readable: {exc}") from exc
        if not isinstance(deterministic, dict):
            raise UsageError(
                f"amendment {identifier} names deterministic report {fixture}, which is "
                "not a JSON object")
        require_deterministic_evidence(identifier, fixture, deterministic,
                                       superseded=superseded)
        if not str(amendment.get("verification_scope") or "").strip():
            raise UsageError(
                f"amendment {identifier} cites a deterministic report but states no "
                "verification_scope; a re-derived digest says WHICH bytes moved and "
                "nothing about what that means for the study, so the gap has to be "
                "written down")
        return
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
        verdict = report["verdict"]
        delivered = report["observed"]["payload_delivered"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise UsageError(
            f"amendment {identifier} names probe report {fixture}, which is not a "
            f"readable probe report: {exc}") from exc
    if verdict != "PASS" or delivered is not True:
        raise UsageError(
            f"amendment {identifier} cites {fixture}, which records verdict "
            f"{verdict!r} and payload_delivered {delivered!r}")
    if not str(amendment.get("verification_scope") or "").strip():
        raise UsageError(
            f"amendment {identifier} cites a probe report but states no "
            "verification_scope; what the probe did NOT cover has to be written down")
    require_amendment_payload_attestation(amendment, report, protocol=protocol, root=root)


# Acceptances recorded before the attestation mechanism existed. A3's was given
# 2026-08-21; its text is quoted from nothing and rests on the author having
# typed it, which is the trust boundary audit 64c54589 rejected. It is
# GRANDFATHERED, not fixed: re-attesting a decision two days after the fact
# would be manufacturing evidence rather than recovering it. Named here so the
# exemption is a visible code change instead of a silent absence — an
# acceptance added later and quietly appended to this tuple is a reviewable
# diff, which is the most this construction can buy.
# Keyed by the DIGEST of the acceptance text, not by the id. Keying by id left
# two holes five voices found (audit 84a257a9): any amendment calling itself A3
# inherited the exemption, and A3's own acceptance text stayed editable forever
# with nothing checking it. The digest freezes the exact bytes that were
# grandfathered; changing a character of A3's acceptance now costs the
# exemption, which is the behaviour "grandfathered" should have meant.
_UNATTESTED_LEGACY_ACCEPTANCES = {
    "A3": "15ee2b1f41e0541b0ea24d24269bfe4816299e044e339f65da68c31ca9d0c34c",
}


def _require_attested_acceptance(
    amendment: dict[str, Any], *, root: Path, claimed: dict[str, str],
) -> None:
    """Refuse an acceptance no committed attestation carries.

    `require_owner_attestation` checked A4 by name. A6 is the second amendment
    to need an acceptance, and adding a second hard-coded id is the pattern this
    record has already been burned by — the residuals list went wrong four times
    precisely because a mechanism was applied to one id rather than to the rule.
    So the rule is stated once: every acceptance except the grandfathered ones
    must quote a git-tracked file whose recorded digest matches its bytes, name
    the amendment, and name every residual it settles.

    WHAT THIS DOES NOT ESTABLISH, stated here because a draft of this docstring
    implied otherwise and five voices priced it (audit 84a257a9): it is NOT
    Owner authentication. Every byte it consults — the file, the digest, the
    quote, the amendment — is writable by whoever edits the protocol, in one
    commit. Nothing here proves the Owner composed the text or that the file
    came from the account it names; those limits live in
    `owner_authorization.extension_evidence` and are not argued away. What the
    check is worth is narrower and real: an acceptance cannot be an author's
    summary of what the Owner meant, cannot silently drift from the file it
    quotes, cannot borrow another decision's attestation, and cannot rest on an
    untracked file. It is a lint against paraphrase and replay, not a
    signature.
    """
    identifier = amendment.get("id")
    acceptance = str(amendment.get("owner_acceptance") or "").strip()
    if not acceptance:
        return
    legacy = _UNATTESTED_LEGACY_ACCEPTANCES.get(str(identifier))
    if legacy is not None:
        actual_legacy = hashlib.sha256(acceptance.encode("utf-8")).hexdigest()
        if legacy != actual_legacy:
            raise UsageError(
                f"amendment {identifier} is grandfathered for an acceptance that hashed to "
                f"{legacy[:12]}, and its acceptance now hashes to {actual_legacy[:12]}. The "
                "exemption covers the bytes it was granted for, not the field")
        return
    # NO FALLBACK TO A SHARED FILE. A draft let an amendment without its own
    # attestation fall back to the one named in owner_authorization, on the
    # reasoning that a span written later could not be in a file sealed
    # earlier. That reasoning was wrong, and demonstrably: the acceptance does
    # not have to be ABOUT this amendment, only to be SOME literal span. A line
    # of the sealed A4 attestation discussing unrelated rule coverage — and
    # containing "(1)" — satisfied both this check and the residuals check when
    # quoted as A6's acceptance. An old attestation could certify a new
    # decision the Owner never made, which is the whole of what this mechanism
    # exists to prevent. Every acceptance names its own file now.
    record = amendment.get("acceptance_attestation") or {}
    committed = str(record.get("committed_copy") or "").split(" -- ")[0].strip()
    if not committed:
        raise UsageError(
            f"amendment {identifier} records an owner_acceptance and no "
            "acceptance_attestation; an acceptance may not rest on the author having "
            "typed it, and it may not borrow another amendment's attestation")
    candidate = Path(committed)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UsageError(
            f"amendment {identifier} names an attestation copy that is not a "
            "repository-relative path")
    path = (root / candidate).resolve()
    if not _is_under(path, root.resolve()) or not path.is_file():
        raise UsageError(
            f"amendment {identifier} names attestation copy {committed}, which is not a "
            "file under the protocol directory")
    # ACTUALLY committed, not merely present. `is_file()` tests the working
    # tree, so an untracked file written by whoever edits the protocol passed
    # a check whose message and docstring both said "committed" — false as
    # written, and named by three voices as the eighth time a gap in this
    # record was described as a property it did not have (audit 84a257a9).
    # Scoped to paths inside the repository, because `root` is a test seam that
    # points at a temporary protocol directory. That is not a bypass: in
    # production `root` IS the protocol directory, the `_is_under` check above
    # already confines the path to it, and `test_the_production_root_is_inside_
    # the_repository` pins that the seam cannot be reached with the real root.
    inside_repo = _is_under(path, REPO_ROOT.resolve())
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", "--", str(path)],
        cwd=REPO_ROOT, capture_output=True, text=True, check=False)
    if inside_repo and tracked.returncode != 0:
        raise UsageError(
            f"amendment {identifier} names attestation copy {committed}, which is not "
            "tracked by git; an untracked file is not evidence of anything, since whoever "
            "edits the protocol can create it in the same breath")
    settled = str(path)
    if settled in claimed:
        raise UsageError(
            f"amendment {identifier} rests on the attestation already claimed by "
            f"{claimed[settled]}; one attestation records one decision. Re-using a file "
            "would let a sealed acceptance certify a decision taken after it was written")
    claimed[settled] = str(identifier)
    raw = path.read_bytes()
    recorded = str(record.get("sha256") or "").strip()
    actual = hashlib.sha256(raw).hexdigest()
    if recorded != actual:
        raise UsageError(
            f"amendment {identifier} records attestation digest {recorded or '<none>'}, "
            f"but {committed} hashes to {actual}")
    quoted = _quoted_span(acceptance)
    if not quoted.strip():
        raise UsageError(
            f"amendment {identifier} quotes nothing from its attestation; the acceptance "
            "has to carry the Owner's own words, not a summary of them")
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UsageError(
            f"amendment {identifier} names attestation copy {committed}, which is not "
            f"UTF-8 text: {exc}") from exc
    if quoted not in body:
        raise UsageError(
            f"amendment {identifier} quotes text that is not a literal span of "
            f"{committed}; it was transcribed rather than quoted, and the two have drifted")
    # Bound to THIS decision. Containment alone says only that the Owner wrote
    # something containing this — it survives quoting a span that means the
    # opposite in context, or one written about an earlier amendment entirely
    # (audit 84a257a9, four voices). So the attestation must name the amendment
    # and every residual it settles. That does not make the span mean what the
    # record says it means; it makes a span from an unrelated decision fail.
    if str(identifier) not in body:
        raise UsageError(
            f"amendment {identifier} rests on an attestation that never names it; an "
            "attestation records one decision and has to say which")
    for label in amendment.get("residuals_standing") or ():
        if str(label) not in quoted:
            raise UsageError(
                f"amendment {identifier} quotes a span that does not name residual "
                f"{label}; the Owner accepts each residual by name, not a summary")


def require_owner_attestation(protocol: dict[str, Any], *, root: Path | None = None) -> None:
    """Refuse an acceptance or an allowance the committed attestation does not carry.

    Five voices of five (audit 4e79c3e8) found the previous state: the Owner
    wrote a file on an account this agent cannot reach, and then NOTHING read
    it. The digest, the owning account and the quoted text were all strings the
    author typed into the file the author edits, and the only automated check
    was that the digest was 64 hex characters. That is the trust boundary audit
    64c54589 rejected, one indirection later — which is exactly what the panel
    called it.

    So the bytes are committed and this recomputes them. What it CAN establish:
    the acceptance and the allowance quoted in the protocol are literal spans of
    a file whose digest matches what the protocol records. What it CANNOT: that
    the Owner composed them, or that the committed copy came from the account it
    says it did — the copy is author-transported, and the uid observation lives
    with the original. Both limits are recorded in extension_evidence rather
    than argued away.
    """
    root = PROTOCOL_FILE.parent if root is None else root
    authorization = protocol.get("owner_authorization") or {}
    evidence = authorization.get("extension_evidence") or {}
    attestation = evidence.get("durable_attestation") or {}
    committed = str(attestation.get("committed_copy") or "").split(" -- ")[0].strip()
    if not committed:
        raise UsageError(
            "owner_authorization records no committed attestation copy; the acceptance rests on "
            "text nothing can recompute")
    candidate = Path(committed)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UsageError("the committed attestation copy is not a repository-relative path")
    path = (root / candidate).resolve()
    if not _is_under(path, root.resolve()) or not path.is_file():
        raise UsageError(f"the committed attestation copy {committed} is not a committed file")
    raw = path.read_bytes()
    recorded = str(attestation.get("sha256") or "").strip()
    actual = hashlib.sha256(raw).hexdigest()
    if recorded != actual:
        raise UsageError(
            f"the committed attestation hashes to {actual}, not the recorded {recorded or '<none>'}")
    text = raw.decode("utf-8")
    amendments = {a.get("id"): a for a in protocol.get("amendments") or ()}
    # Every acceptance, by rule rather than by id. A4's is additionally checked
    # against the shared attestation below because that file also carries the
    # attempt allowance, which is not an amendment field and has nowhere else
    # to live.
    acceptance_files: dict[str, str] = {}
    for amendment in protocol.get("amendments") or ():
        _require_attested_acceptance(amendment, root=root, claimed=acceptance_files)
    # ONLY the allowance is checked against the shared file, and only because it
    # is not an amendment field and has nowhere else to live.
    #
    # A4's acceptance used to be checked here too, by name. That was correct
    # while the allowance and A4's acceptance were the same file, and
    # `_require_attested_acceptance` was written to replace it — its docstring
    # says adding a second hard-coded id is the pattern this record has been
    # burned by — but the by-name clause was left beside the rule that
    # superseded it. The first rotation is what shows the cost: the fifth
    # attempt's allowance is a new file, A4's acceptance is still in the
    # 2026-08-23 one, and the by-name clause refused a protocol with nothing
    # wrong in it.
    #
    # IT IS A LOOSENING OF THIS FUNCTION, and saying otherwise was the first
    # draft's error (audit 22d37b45, Voice 1 f4 and Voice 5 f3, who asked for
    # the input on which the removed clause refuses and the survivor accepts).
    # There is one: `_require_attested_acceptance` opens `if not acceptance:
    # return`, so an A4 whose `owner_acceptance` is EMPTIED is now skipped here,
    # where the by-name clause said "quotes nothing from the attestation".
    # It is NOT a loosening of the paid path, which is the claim that matters
    # and the one that is measured: `require_verified_amendments` refuses that
    # same input ("amendment A4 names an Owner decision as a precondition"), the
    # two gates are independent conjuncts, and that the spend path calls BOTH is
    # already pinned by `test_the_spend_path_calls_both_gates_not_just_one`.
    # For every input where A4 keeps an acceptance, the surviving rule checks it
    # HARDER than this clause did: digest of the bytes, literal span, names the
    # amendment, names every residual it settles, and git-tracked — against this
    # clause's single "is a substring of whatever file the allowance points at".
    # PLAN E. If the protocol carries an Owner-attested authoritative main, it
    # has to be a literal span of THIS file and the sha has to be inside that
    # span. The second check is the load-bearing one: without it the author
    # could quote a real Owner paragraph and record a different commit beside
    # it, and the gate would trust a number the Owner never wrote. Absent is
    # allowed here and refused at the gate, so a checkout with a real remote is
    # not made to carry a human step git already performs.
    # THE SUPPLEMENT. A file nothing reads is not evidence — the state five
    # voices of five rejected in audit 4e79c3e8, when the Owner wrote a file on
    # an account this agent cannot reach and nothing recomputed it. So the same
    # treatment: the bytes are committed, this recomputes them, and it checks
    # that the two things audit f3f79ace said were missing are actually in
    # them. Absent is allowed — the supplement is not required to exist — but a
    # recorded one that does not hash, is not tracked, or has quietly lost what
    # it was written for, is refused.
    supplement = evidence.get("supplementary_attestation") or {}
    if supplement:
        named = str(supplement.get("committed_copy") or "").split(" -- ")[0].strip()
        if not named:
            raise UsageError("the supplementary attestation records no committed copy")
        # NOT `candidate` — that name belongs to the allowance attestation and
        # is read again below by the "newest wins" rule. Shadowing it here made
        # that rule compare the SUPPLEMENT against the fixtures directory, so a
        # superseded allowance stopped being refused. Caught by
        # `test_a_superseded_attestation_for_this_same_attempt_is_refused`.
        srel = Path(named)
        if srel.is_absolute() or ".." in srel.parts:
            raise UsageError(
                "the supplementary attestation copy is not a repository-relative path")
        spath = (root / srel).resolve()
        if not _is_under(spath, root.resolve()) or not spath.is_file():
            raise UsageError(
                f"the supplementary attestation copy {named} is not a committed file")
        if _is_under(spath, REPO_ROOT.resolve()) and subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", str(spath)],
                cwd=REPO_ROOT, capture_output=True, text=True, check=False).returncode != 0:
            raise UsageError(
                f"the supplementary attestation copy {named} is not tracked by git; an "
                "untracked file is not evidence of anything")
        sraw = spath.read_bytes()
        srecorded = str(supplement.get("sha256") or "").strip()
        sactual = hashlib.sha256(sraw).hexdigest()
        if srecorded != sactual:
            raise UsageError(
                f"the supplementary attestation hashes to {sactual}, not the recorded "
                f"{srecorded or '<none>'}")
        stext = sraw.decode("utf-8")
        # The two facts it exists to carry. Named individually so a supplement
        # that quietly lost one fails here rather than passing as "a real Owner
        # file" — the shape of failure audit 4e79c3e8 described.
        for fact, why in (("起草归属", "the drafting attribution"),
                          ("第 194 格", "the corrected three-arm ordinal")):
            if fact not in stext:
                raise UsageError(
                    f"the supplementary attestation no longer carries {why}, which is one "
                    "of the two things audit f3f79ace found missing from the Owner's bytes")

    a4 = amendments.get("A4") or {}
    quoted = str(evidence.get("allowance_quote") or "")
    if not quoted.strip():
        raise UsageError("the attempt allowance quotes nothing from the attestation")
    if quoted not in text:
        raise UsageError(
            "the attempt allowance is not a literal span of the committed attestation; it was "
            "transcribed rather than quoted, and the two have drifted")
    # A literal span of the right file can still be the wrong allowance. Every
    # attestation in this study grants ONE attempt by name, and each rotation
    # leaves the previous one committed and still quotable — so "is a substring
    # of a real Owner file" was satisfied by an allowance the last attempt had
    # already consumed. Bind the quote to the id the gate is about to admit
    # (audit 1d8685a3, Voice 1 f4).
    study_id = str(protocol.get("study_id") or "")
    if not study_id or study_id not in quoted:
        raise UsageError(
            f"the attempt allowance does not name {study_id or '<no study id>'}; a quote can be a "
            "literal span of a committed attestation and still grant a different attempt")
    # A SUPERSEDED attestation for the SAME attempt passes every check above:
    # the 2026-08-24 file also grants followup-5, so the study-id clause sees
    # nothing wrong, while the consent it carries is the consent the Owner
    # replaced -- it was given before the deterministic-coordinate finding and
    # the six-cell probe existed, which is the whole reason the 2026-08-25 file
    # was written.
    #
    # The first version of this change answered that with the literal pins in
    # test_protocol.py. Five voices of five refused it (audit f3f79ace), on a
    # distinction that is right: the pins protect the REPOSITORY at pytest time
    # and this gate protects the PAID RUN at --primary time, and a record
    # reverted between those moments spends an authorized attempt -- the study's
    # scarcest resource -- on withdrawn consent.
    #
    # The rule is "newest wins", by date-stamped filename, and it is scoped to
    # the ALLOWANCE only. An amendment's own `acceptance_attestation` may still
    # cite an older file, which A4's does: the allowance rotates every attempt,
    # a settled acceptance does not.
    fixtures = (root / "fixtures")
    newer = sorted(
        q.name for q in fixtures.glob("owner_attestation_*.txt") if q.name > candidate.name
    ) if fixtures.is_dir() else []
    if newer:
        raise UsageError(
            f"the attempt allowance cites {candidate.name}, which is superseded by "
            f"{newer[-1]}; a later attestation exists, so this consent is not the current one")

    # AFTER the allowance checks, deliberately. This asks a question ABOUT THE
    # CONTENT of the cited attestation, and the checks above decide WHICH
    # attestation is cited at all. Run first, it answered a stale citation with
    # "your provenance quote is not a literal span of it" — true, useless, and
    # it masked "that allowance is superseded", which is what the reader needs.
    # Four existing tests went red on exactly that inversion.
    authoritative = attested_authoritative_main(protocol)
    if authoritative:
        span = str(authoritative.get("quote") or "")
        commit = str(authoritative.get("commit") or "").strip()
        if not span.strip():
            raise UsageError("authoritative_main quotes nothing from the attestation")
        if span not in text:
            raise UsageError(
                "the authoritative-main assertion is not a literal span of the committed "
                "attestation; it was transcribed rather than quoted")
        # ANCHORED, and this is the repair audit 0e29d811 forced. A first draft
        # required only that the sha appear somewhere inside the span — but the
        # AUTHOR picks the span, and any literal substring qualifies, including
        # a span that IS a 40-hex string. Four voices of four found that the
        # pair then collapses to "the sha occurs somewhere in the file", so any
        # commit the Owner ever mentioned for any reason could be nominated as
        # the authoritative main. The span must now be recognisably ABOUT the
        # authoritative branch, and carry exactly one commit.
        for marker in _ATTESTED_MAIN_MARKERS:
            if marker not in span:
                raise UsageError(
                    f"the authoritative-main quote does not contain {marker!r}; a span the "
                    "author is free to choose is not an assertion about the authoritative "
                    "branch merely because it sits inside the Owner's file")
        found = _SHA40_IN_TEXT.findall(span)
        if len(found) != 1:
            raise UsageError(
                f"the authoritative-main quote carries {len(found)} commit ids and must carry "
                "exactly one, so the sha the gate trusts is the one the Owner wrote about the "
                "authoritative branch rather than one that happens to sit beside it")
        if not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise UsageError("authoritative_main records a malformed commit")
        if commit != found[0]:
            raise UsageError(
                "the authoritative-main commit is not the one inside the Owner's quote; the "
                "sha the gate would trust would be the author's, not theirs")
    # The composition ruling is a SEPARATE artifact with its own digest, because
    # it was written later. It gets the same treatment for the same reason: it
    # WEAKENS the rule governing acceptances, which is the last statement in
    # this record that should rest on the author having typed it.
    # COUPLED to the weakening, not optional beside it. The first version
    # returned clean when the field was absent — so deleting one JSON key
    # detached the check while the amended rule text stayed in force, which is
    # the bypass three voices of four found (audit 04a72883: Voice 1 f2,
    # Voice 2 f1, Voice 3 f4). The weakening lives in `verification_required`;
    # that is what decides whether a ruling is REQUIRED.
    ruling = a4.get("owner_composition_ruling")
    if _AMENDED_RULE_MARKER in str(a4.get("verification_required") or ""):
        if ruling is None:
            raise UsageError(
                "the standing acceptance rule is recorded as amended by an Owner ruling, and no "
                "owner_composition_ruling is present; a weakened rule may not outlive the ruling "
                "that weakened it")
    elif ruling is None:
        return
    quoted = _quoted_span(ruling)
    if not quoted.strip():
        raise UsageError("the composition ruling quotes nothing")
    for name in _RULING_FIXTURES:
        candidate = (root / name)
        if not candidate.is_file():
            continue
        body = candidate.read_bytes()
        if hashlib.sha256(body).hexdigest() in ruling and quoted in body.decode("utf-8"):
            return
    raise UsageError(
        "the composition ruling is not a literal span of a committed ruling artifact whose "
        "digest it records; a rule-weakening may not rest on the author having typed it")


# Ruling artifacts, named in code rather than derived from the amendment. What
# that buys is narrower than an earlier comment claimed: an author who edits
# protocol.json also edits this file, so substituting a self-written artifact is
# one line away. It makes the substitution a VISIBLE CODE CHANGE rather than a
# data change, and nothing more (audit 04a72883, Voice 1 f7 and Voice 4 f2).
_RULING_FIXTURES = ("fixtures/owner_ruling_2026-08-23_composition.txt",)
# The marker that says the standing rule has been weakened. Matching on the
# amendment's own prose is crude, but the alternative — a boolean the same
# amendment sets — is what the coupling exists to avoid.
_AMENDED_RULE_MARKER = "AMENDED 2026-08-23 by Owner ruling"


def _quoted_span(acceptance: Any) -> str:
    """The text between the acceptance's own quote markers, or the empty string."""
    if not isinstance(acceptance, str) or "Quote begins:" not in acceptance:
        return ""
    span = acceptance.split("Quote begins:", 1)[1]
    return span.rsplit("Quote ends.", 1)[0].strip("\n")


def require_authorized_attempt(protocol: dict[str, Any]) -> None:
    """Refuse an attempt the Owner's allowance does not name.

    The 2026-08-18 authorization was for "one further" follow-up. One has been
    run — it aborted at 254 of 800 cells — so whether a fourth attempt is
    covered is a question only the Owner can answer, and until 2026-08-22 the
    record answered it by assumption: the study id moved forward and nothing
    checked. Two voices of five found that independently (audit 26d0a1c2,
    Voice 1 f5 and Voice 2 f2).

    A paragraph is not a control. This is the same lesson `require_absolute_arm`
    and `require_scratch_arm` were built from, applied to the one precondition
    that is about permission rather than evidence.
    """
    authorization = protocol.get("owner_authorization") or {}
    authorized = str(authorization.get("authorized_attempt_study_id") or "").strip()
    if not authorized:
        raise UsageError(
            "owner_authorization names no authorized_attempt_study_id; a paid attempt cannot "
            "start on an allowance nobody recorded")
    if authorized != protocol.get("study_id"):
        raise UsageError(
            f"the Owner's attempt allowance names {authorized}, not {protocol.get('study_id')}. "
            "The previous allowance was consumed by that attempt; extending it to this one is an "
            "Owner decision, not an author's reading of the scope paragraph")


def require_scratch_arm(protocol: dict[str, Any], *, root: Path | None = None) -> None:
    """Refuse a verified A4 that never showed its scratch rule firing.

    A4 requires TWO probes and names them in prose. Prose is not a control:
    A3 declared its host-path requirement the same way, nothing read it, and
    five voices of five made it production as `require_absolute_arm` (audit
    6da54dbc). `scratch_verified_by_fixture` was about to repeat that exactly —
    a new key nothing loads, so A4 could be opened on the acceptance probe alone
    with residual (1) accepted as "firing unmeasured", and the one rule this
    whole amendment turns on would still never have been observed to fire
    (audit 864cf832, Voices 3 and 4, convergent).

    Silent while A4 is unverified: `require_verified_amendments` owns that
    refusal, exactly as it does for A3's absolute arm.
    """
    root = PROTOCOL_FILE.parent if root is None else root
    amendments = {a.get("id"): a for a in protocol.get("amendments") or ()}
    a4 = amendments.get("A4")
    if a4 is None or a4.get("verified") is not True:
        return
    fixture = str(a4.get("scratch_verified_by_fixture") or "").strip()
    if not fixture:
        raise UsageError(
            "A4 is verified but names no scratch-arm report; the CLI scratch has not been "
            "shown denied under the rule this amendment adds")
    report = _load_probe_report(fixture, root=root, label="A4 scratch arm")
    observed = report.get("observed") or {}
    if report.get("verdict") != "PASS" or observed.get("scratch_path_denied") is not True:
        raise UsageError(
            f"A4 cites scratch-arm report {fixture}, which records verdict "
            f"{report.get('verdict')!r} and scratch_path_denied "
            f"{observed.get('scratch_path_denied')!r}")


def require_absolute_arm(protocol: dict[str, Any], *, installed_version: str | None = None,
                         root: Path | None = None) -> None:
    """Refuse `--primary` until a HOST path was seen denied under a `//` rule HERE.

    Every rule protecting the account's home and the system roots is
    `//`-anchored. The 2026-08-20 acceptance probe showed such a rule denying a
    path INSIDE the cell on the installed binary — but the deny set exists for
    host paths, and the only observation of one being denied is the 2026-08-19
    stage-2 probe, whose binary this host can no longer identify.

    Audit 6da54dbc, five voices of five: the first version of this requirement
    was PROSE in A3's `verification_required` plus a test asserting that prose
    contained certain words. Nothing refused the gate if the arm never ran, or
    ran and failed. It is production now.

    The version check is what makes the anchor/absolute pair a within-version
    differential rather than the same cross-version comparison A1 is confounded
    by: both reports, and the binary in hand, must agree.
    """
    root = PROTOCOL_FILE.parent if root is None else root
    amendments = {a.get("id"): a for a in protocol.get("amendments") or ()}
    a3 = amendments.get("A3")
    if a3 is None or a3.get("verified") is not True:
        return  # `require_verified_amendments` owns that refusal.
    # The Owner chose DISABLE-AND-ASSERT. The detection half is machine-run; the
    # disable half happens on an account this one cannot inspect, so what the
    # gate can require is that the Owner recorded doing it, dated. Without this,
    # A3 could be opened while the updater is still running (audit 96dc952e,
    # Voice 2 f2).
    disabled = str(a3.get("owner_updater_disabled") or "").strip()
    if not disabled:
        raise UsageError(
            "A3 is verified but no owner_updater_disabled record exists; the host's CLI "
            "updater must be unloaded for the collection and the Owner must record it")
    if not re.match(r"^\d{4}-\d{2}-\d{2}\b", disabled):
        raise UsageError(
            "the owner_updater_disabled record does not begin with the date it was made")
    fixture = str(a3.get("absolute_verified_by_fixture") or "").strip()
    if not fixture:
        raise UsageError(
            "A3 is verified but names no absolute-arm report; a HOST path has not been "
            "shown denied under a //-anchored rule on this binary")
    report = _load_probe_report(fixture, root=root, label="A3 absolute arm")
    observed = report.get("observed") or {}
    if report.get("verdict") != "PASS" or observed.get("host_path_denied") is not True:
        raise UsageError(
            f"the absolute-arm report {fixture} records verdict {report.get('verdict')!r} "
            f"and host_path_denied {observed.get('host_path_denied')!r}; --primary must not "
            "start until a host path is provably denied under a //-anchored rule")
    if observed.get("payload_delivered") is not True or observed.get("attributable") is not True:
        raise UsageError(
            f"the absolute-arm report {fixture} is not attributable: payload_delivered "
            f"{observed.get('payload_delivered')!r}, attributable {observed.get('attributable')!r}")
    versions = {str(report.get("claude_version") or "")}
    partner = str(a3.get("differential_partner_fixture") or "").strip()
    if partner:
        versions.add(str(_load_probe_report(
            partner, root=root, label="A3 differential partner").get("claude_version") or ""))
    if installed_version is not None:
        versions.add(installed_version)
    if len(versions) != 1 or not next(iter(versions)):
        raise UsageError(
            "the absolute arm, its differential partner and the installed binary do not all "
            f"record one CLI version ({sorted(versions)}); a cross-version comparison is the "
            "confound amendment A1 already carries")


def _load_probe_report(fixture: str, *, root: Path, label: str) -> dict[str, Any]:
    """Read a committed probe report, refusing anything that is not one."""
    candidate = Path(fixture)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise UsageError(f"{label} names {fixture}, which is not a repository-relative path")
    path = (root / candidate).resolve()
    if not _is_under(path, root.resolve()) or not fixture.endswith(".json") or not path.is_file():
        raise UsageError(f"{label} names {fixture}, which is not a committed JSON report")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UsageError(f"{label} names {fixture}, which is not readable: {exc}") from exc


def require_amendment_payload_attestation(
    amendment: dict[str, Any], report: dict[str, Any], *,
    protocol: dict[str, Any] | None = None, root: Path | None = None,
    _seen: tuple[str, ...] = (),
) -> None:
    """Tie an operative amendment to a report of the payload the study will send.

    Audit 9d92d22c, five voices of five: without this, ANY committed JSON that
    records a PASS and `payload_delivered` discharged an operative boundary
    amendment — a copy of an older report under a new filename would do, and
    that older report was produced under a payload with 90 fewer entries. A
    probe report is evidence about ONE payload; the gate has to check it is
    THIS one.

    An amendment whose own report predates the current payload may instead name
    the amendment that carries the current attestation, via
    `payload_superseded_by`. That keeps an honestly-verified historical record
    verified for what it verified, while still requiring that SOMETHING in the
    chain attests the bytes a paid cell will actually send.
    """
    identifier = amendment.get("id")
    if amendment.get("field") not in OPERATIVE_AMENDMENT_FIELDS:
        return
    successor = str(amendment.get("payload_superseded_by") or "").strip()
    if successor:
        # RESOLVED, not trusted. Five voices of five (audit fa871c95) found this
        # branch accepting ANY non-empty string and skipping the fingerprint
        # comparison outright — so the invariant the docstring claims, that
        # something in the chain attests the bytes a paid cell sends, was not
        # implemented. The chain is walked here, and its terminal amendment must
        # pass this same check without a successor of its own.
        if protocol is None:
            raise UsageError(
                f"amendment {identifier} names a payload successor but the protocol was "
                "not supplied, so the chain cannot be resolved")
        if identifier in _seen:
            raise UsageError(
                f"amendment {identifier} is part of a payload_superseded_by cycle: "
                f"{' -> '.join((*_seen, str(identifier)))}")
        amendments = {a.get("id"): a for a in protocol.get("amendments") or ()}
        target = amendments.get(successor)
        if target is None:
            raise UsageError(
                f"amendment {identifier} names payload successor {successor!r}, which is "
                "not an amendment in this protocol")
        if target.get("verified") is not True:
            raise UsageError(
                f"amendment {identifier} defers its payload attestation to {successor}, "
                "which is not itself verified")
        if target.get("field") not in OPERATIVE_AMENDMENT_FIELDS:
            raise UsageError(
                f"amendment {identifier} defers its payload attestation to {successor}, "
                f"whose field {target.get('field')!r} is not operative and so attests nothing")
        successor_fixture = str(target.get("verified_by_fixture") or "").strip()
        if not successor_fixture:
            raise UsageError(
                f"amendment {identifier} defers to {successor}, which names no probe report")
        require_amendment_payload_attestation(
            target,
            _load_probe_report(successor_fixture, root=(PROTOCOL_FILE.parent if root is None
                                                        else root),
                               label=f"{successor} probe report"),
            protocol=protocol, root=root, _seen=(*_seen, str(identifier)))
        return
    live = cell_permission_payload_fingerprint()
    recorded = str((report.get("observed") or {}).get("payload_fingerprint") or "")
    if recorded != live:
        raise UsageError(
            f"amendment {identifier} attests the deny payload but its probe report "
            f"records fingerprint {recorded or '<none>'}, not the live {live}; a "
            "report is evidence about the payload it sent. Re-run the probe under "
            "the current payload, or name the amendment that carries the current "
            "attestation in payload_superseded_by")


def preflight_primary_environment(
    protocol: dict[str, Any],
    tasks: list[Path],
    out_dir: Path,
    *,
    model_isolation: ModelIsolationCapability | None = None,
) -> str:
    """Refuse a paid primary matrix when a frozen local safety prerequisite drifts."""
    if model_isolation is None:
        raise UsageError("model isolation capability is required before primary preflight")
    if sys.platform != "darwin" or not _SCORE_SANDBOX.is_file():
        raise UsageError("primary run requires macOS sandbox-exec")
    if not _under_users(REPO_ROOT) or not _under_users(out_dir):
        raise UsageError("primary checkout and result ledger must be under /Users for scorer isolation")
    if _under_users(Path(sys.executable)):
        raise UsageError("primary scorer requires a system Python outside /Users")
    if directory_tree_sha256(AQG_SKILLS_DIR) != protocol["aqg_skills_tree_sha256"]:
        raise UsageError("AQG skills content hash differs from frozen protocol")
    if task_tree_sha256(tasks) != protocol["task_tree_sha256"]:
        raise UsageError("task/scorer content hash differs from frozen protocol")
    validate_treatment_provenance(protocol)
    validate_workspace_boundary_contract(protocol)
    require_cell_tree_outside_the_deny_set(model_isolation.workspace_root)
    require_authenticated_claude_cli(model_isolation=model_isolation)
    require_claude_help(model_isolation=model_isolation)
    # A HOST path has to have been seen denied under a `//`-anchored rule on
    # THIS binary before 800 paid cells run behind rules of that shape. Placed
    # after the version probe below would be too late — so it is passed the
    # version the probe returns, at the bottom of this function.
    checks = (
        ("production scorer selftest", [sys.executable, str(HERE / "selftest.py"), "--sandboxed"]),
        ("protocol regression suite", [sys.executable, str(HERE / "test_protocol.py"), "-q"]),
        ("runner regression suite", [sys.executable, str(HERE / "test_run.py"), "-q"]),
        ("analyzer regression suite", [sys.executable, str(HERE / "test_analyze.py"), "-q"]),
        ("result-boundary regression suite", [sys.executable, str(HERE / "test_redaction.py"), "-q"]),
        ("fixed-seed power check", [sys.executable, str(HERE / "test_power.py"), "-q"]),
    )
    for label, command in checks:
        proof = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, check=False)
        if proof.returncode:
            raise UsageError(f"{label} failed: {proof.stdout[-500:]}{proof.stderr[-500:]}")
    # BELOW the free checks, not above them. While a payload amendment awaits its
    # probe this raises, and running it first meant `--preflight-only` could no
    # longer execute the six zero-cost regression suites at all — a capability
    # loss beyond the intended money gate, during exactly the window in which the
    # amendment is being validated (audit 864cf832, Voice 1 f7). Nothing paid
    # happens between the two points: `checks` is six local subprocesses.
    require_verified_amendments(protocol)
    require_owner_attestation(protocol)
    require_authorized_attempt(protocol)
    installed = require_claude_version(model_isolation=model_isolation)
    require_absolute_arm(protocol, installed_version=installed)
    require_scratch_arm(protocol)
    # Recorded here so the END of the matrix has something to compare against.
    # A start-of-run check alone cannot see a binary that moves mid-collection,
    # and on this host one does: see `claude_binary_identity`.
    return installed


def prepare_aqg_plugin() -> Path:
    """Create a private, immutable-by-design AQG plugin snapshot."""
    if not AQG_SKILLS_DIR.is_dir():
        raise UsageError(f"AQG skill source missing: {AQG_SKILLS_DIR}")
    root: Path | None = None
    built = False
    try:
        root = _trusted_snapshot_dir("ws7-aqg-plugin-")
        manifest_dir = root / ".claude-plugin"
        manifest_dir.mkdir()
        (manifest_dir / "plugin.json").write_text(
            json.dumps({"name": "aqg-ws7-wrapper", "version": "0.0.0-ws7", "skills": ["./skills/"]}),
            encoding="utf-8",
        )
        shutil.copytree(AQG_SKILLS_DIR, root / "skills", symlinks=False)
        built = True
    except OSError as exc:
        # Built as the collection account, under a home and umask the checkout
        # was not made under, before anything has been spent — and `main`
        # catches UsageError only, so this reached the operator as a stack.
        # Truncated like the regression-suite gate: `shutil.copytree` collects
        # per-file failures into one `shutil.Error` whose text can run to
        # hundreds of paths, which would bury the line it exists to show.
        raise UsageError(f"cannot build the AQG plugin snapshot: {str(exc)[-500:]}") from exc
    finally:
        # In `finally`, not in the handler above: `main` can only remove a path
        # it was RETURNED, so any exit without one — an operator's Ctrl-C as
        # much as an OSError — strands a partial snapshot in a cache that has
        # no reaper (issue #601).
        if not built and root is not None:
            shutil.rmtree(root, ignore_errors=True)
            if root.exists():
                # `ignore_errors=True` is right — a cleanup failure must not
                # replace the real cause — but it must not be silent either,
                # or the pile grows while the code reads as if it does not.
                print(f"WARN: partial AQG plugin snapshot left at {root}", file=sys.stderr)
    return root


def resolve_tasks(names: list[str] | None) -> list[Path]:
    """Task dirs under tasks/ (all, or the named subset). Raises UsageError on
    an unknown name so a typo can't silently shrink the matrix."""
    available = sorted(d for d in TASKS_DIR.iterdir() if d.is_dir() and (d / "task.md").exists())
    if not names:
        return available
    by_name = {d.name: d for d in available}
    missing = [n for n in names if n not in by_name]
    if missing:
        raise UsageError(f"unknown task(s): {', '.join(missing)} (have: {', '.join(by_name)})")
    return [by_name[n] for n in names]


_STATUS_FLAG = {
    STATUS_PASS: "OK ", STATUS_FAIL: "FAIL", STATUS_ERRORED: "ERR",
    STATUS_INFRA: "INFRA", STATUS_BUDGET: "BUDGET",
}


def _summary(results: list[CellResult]) -> list[str]:
    """Per-(arm, strictness) rollup. The strictness split is the point of the ladder:
    neutral vs competing must be comparable WITHIN an arm (does aqg-full resist the
    competing prompt better than baseline?). `mean_fail` is the mean defect count over
    GENUINELY-SCORED cells only (pass/fail); `errored` and `infra` are reported
    separately so a worst-case outcome cannot dilute the defect mean into a false win
    (audit 9aad54d9 f1/f6). Pass-rate still counts errored/infra as non-passes."""
    lines: list[str] = []
    for arm, strictness in sorted({(r.arm, r.strictness) for r in results}):
        cells = [r for r in results if r.arm == arm and r.strictness == strictness]
        n = len(cells)
        passed = sum(1 for r in cells if r.status == STATUS_PASS)
        failed = sum(1 for r in cells if r.status == STATUS_FAIL)
        errored = sum(1 for r in cells if r.status == STATUS_ERRORED)
        infra = sum(1 for r in cells if r.status == STATUS_INFRA)
        budget_capped = sum(1 for r in cells if r.status == STATUS_BUDGET)
        contam = sum(1 for r in cells if not r.isolation_ok)
        scored = [r for r in cells if r.scored]
        mean_fail = f"{sum(r.n_failures for r in scored) / len(scored):.2f}" if scored else "n/a"
        cost = sum(r.accounted_cost_usd for r in cells)
        lines.append(
            f"  {arm + '/' + strictness:<24} scored {len(scored)}/{n}  pass {passed} fail {failed} "
            f"errored {errored} infra {infra} budget_capped {budget_capped}  mean_fail {mean_fail}  "
            f"isolation_fail {contam}  ${cost:.4f}"
        )
    return lines


def main(argv: list[str] | None = None) -> int:
    """Restore `_PREFLIGHT_ONLY` on every exit, including exceptional ones.

    Setting it per call fixed the one-way leak, but the flag's lifetime was still
    the process: a run that raised left it set for any later in-process caller,
    and a nested `main()` without the flag cleared it for the outer one — which
    would silently remove the outer run's defence-in-depth. Restoring the PREVIOUS
    value, rather than clearing it, is what makes nesting safe in both directions.
    """
    global _PREFLIGHT_ONLY
    previous = _PREFLIGHT_ONLY
    try:
        return _main(argv)
    finally:
        _PREFLIGHT_ONLY = previous


def _main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AQG-workflow benchmark runner.")
    ap.add_argument("--tasks", nargs="*", help="task name(s); default all under tasks/")
    ap.add_argument("--arms", nargs="*", default=None,
                    help="arm name(s); default exploratory three arms, or the frozen primary matrix with --primary")
    ap.add_argument("--strictness", nargs="*", default=list(DEFAULT_STRICTNESS),
                    help=f"prompt-strictness level(s): {', '.join(STRICTNESS_LEVELS)}; "
                         "default neutral only (competing is opt-in — it multiplies the matrix)")
    ap.add_argument("--runs", type=int, default=None, help="repeats per (task, arm, strictness) cell")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--allowed-tools", default=DEFAULT_ALLOWED_TOOLS)
    ap.add_argument("--timeout", type=int, default=300, help="per-cell `claude` timeout (s)")
    ap.add_argument("--out", default=str(HERE / "results"), help="results dir for the JSONL ledger")
    ap.add_argument("--execute", action="store_true",
                    help="ACTUALLY call the API (spends money). Default: dry-run (plan + cost only).")
    ap.add_argument("--max-cost", type=float, default=None,
                    help="explicit USD ceiling; refuse to --execute if the projection exceeds it")
    ap.add_argument("--primary", action="store_true",
                    help="enforce the committed WS-7 protocol; any drift is refused")
    ap.add_argument("--frozen-commit",
                    help="merged protocol commit required for a primary --execute "
                         "or --preflight-only run")
    ap.add_argument("--attested-main-file", type=Path,
                    help="path, on the collection account, to the Owner's attestation "
                         "naming the authoritative origin/main they verified. Required for a "
                         "primary run whose origin is not the authoritative remote. Read at "
                         "run time and NOT from the frozen checkout: the commit carrying an "
                         "attestation moves main past the sha it names, so a committed "
                         "pointer can never be satisfied")
    ap.add_argument("--aqg-rules-body-only", action="store_true",
                    help="DIAGNOSTIC, exploratory runs only. Inject only the installable "
                         "AQG rule block, dropping the operator-facing preamble that tells "
                         "a human to `cat` it into their global rules file. Tests the "
                         "2026-09-02 hypothesis for u-hat = 0/120. Refused with --primary: "
                         "it changes the administered treatment, so such a run is NOT "
                         "comparable to followup-5 or to any earlier attempt.")
    ap.add_argument("--preflight-only", action="store_true",
                    help="run every free primary integrity check and stop before any "
                         "durable evidence, attempt marker, or model call (spends nothing)")
    args = ap.parse_args(argv)
    # Synchronised on EVERY call, not set once: a one-way assignment would leak
    # the refusal into every later main() in the same process — which is exactly
    # what it did, turning nine unrelated tests red.
    global _PREFLIGHT_ONLY
    _PREFLIGHT_ONLY = bool(args.preflight_only)

    protocol: dict[str, Any] | None = None
    aqg_plugin_dir: Path | None = None
    origin_main_commit: str | None = None
    attempt_marker: Path | None = None
    primary_ledger: Path | None = None
    primary_raw_dir: Path | None = None
    primary_solution_dir: Path | None = None
    primary_ledger_handle: Any | None = None
    primary_claude_version: str | None = None
    primary_binary_identity: dict[str, Any] | None = None
    primary_starting_task_hash: str | None = None
    primary_starting_aqg_snapshot_hash: str | None = None
    primary_starting_aqg_rules_hash: str | None = None
    primary_blocks: tuple[CollectionBlock, ...] = ()
    primary_model_isolation_by_arm: dict[str, ModelIsolationCapability] = {}
    workdir: Path | None = None
    model_isolation: ModelIsolationCapability | None = None
    try:
        if args.preflight_only:
            # Pure-argv checks, deliberately before anything touches the disk: a
            # bad flag combination is the user's to fix immediately, and burying
            # it behind a plugin-manifest or protocol error makes it look like a
            # different problem. Stage 1 answers "would the paid run be allowed
            # to start, on THIS checkout" — without --primary there is no frozen
            # protocol to check against, --execute would make the flag's own name
            # false, and without --frozen-commit it would report a pass for a
            # checkout the paid run would refuse.
            if not args.primary:
                raise UsageError("--preflight-only requires --primary")
            if args.execute:
                raise UsageError("--preflight-only cannot be combined with --execute")
            if not args.frozen_commit:
                raise UsageError("--preflight-only requires --frozen-commit")
        # REFUSED FOR PRIMARY, and not only because the primary path never
        # threads it: the protocol pins the sha256 of the WHOLE rules file, and
        # a paid matrix that silently administered a shorter treatment than the
        # one its provenance names is the failure this study has spent five
        # audits learning to refuse. The check is here, before anything is
        # loaded, so the message names the real conflict.
        if args.primary and args.aqg_rules_body_only:
            raise UsageError(
                "--aqg-rules-body-only changes the administered treatment, so it is "
                "refused for --primary: the protocol pins the sha256 of the whole "
                "rules file and every attempt to date administered the preamble")
        if args.primary:
            protocol = load_protocol()
            requested_arms = args.arms if args.arms is not None else list(protocol["matrix"]["arms"])
            requested_task_names = args.tasks if args.tasks is not None else list(protocol["matrix"]["tasks"])
            runs = args.runs if args.runs is not None else int(protocol["matrix"]["runs_per_task_arm"])
        else:
            requested_arms = args.arms if args.arms is not None else ["baseline", "claude-md-lite", "aqg-full"]
            requested_task_names = args.tasks
            runs = args.runs if args.runs is not None else 1
            if args.execute:
                validate_nonprimary_evidence_directory(Path(args.out))
        # Exploratory aqg-full uses the same explicit, private wrapper as the
        # primary matrix.  Otherwise project-only isolation would correctly
        # reject it as missing AQG before it could produce a useful smoke.
        if "aqg-full" in requested_arms:
            aqg_plugin_dir = prepare_aqg_plugin()
        arms_all = build_arms(aqg_plugin_dir=aqg_plugin_dir,
                              rules_body_only=args.aqg_rules_body_only)
        # nargs="*" lets `--arms` / `--strictness` be passed with NO values → []; an
        # empty selection would crash dry-run (`selected[0]` / `args.strictness[0]`) and
        # silently run a zero-cell no-op under --execute. Reject loudly (audit 15d2bac8
        # claude f1 + gpt f2 — convergent).
        if not requested_arms:
            raise UsageError("--arms requires at least one arm")
        if not args.strictness:
            raise UsageError("--strictness requires at least one level")
        unknown = [a for a in requested_arms if a not in arms_all]
        if unknown:
            raise UsageError(f"unknown arm(s): {', '.join(unknown)} (have: {', '.join(arms_all)})")
        bad_strict = [s for s in args.strictness if s not in STRICTNESS_LEVELS]
        if bad_strict:
            raise UsageError(f"unknown strictness: {', '.join(bad_strict)} "
                             f"(have: {', '.join(STRICTNESS_LEVELS)})")
        selected = [arms_all[a] for a in requested_arms]
        tasks = resolve_tasks(requested_task_names)
        if protocol is not None:
            validate_primary_request(
                protocol,
                tasks=[task.name for task in tasks],
                arms=requested_arms,
                strictness=args.strictness,
                runs=runs,
                model=args.model,
                allowed_tools=args.allowed_tools,
                timeout=args.timeout,
            )
            if protocol.get("collection_design") == ATOMIC_MATCHED_BLOCKS:
                primary_blocks = build_collection_blocks(
                    tasks,
                    selected,
                    runs=runs,
                    order_seed=int(protocol["matrix"]["order_seed"]),
                    strictness=args.strictness[0],
                    # The protocol's arms, not the requested ones: a `--arms`
                    # subset must not be able to redefine what a complete block
                    # is on the paid path.
                    expected_arms=list(protocol["matrix"]["arms"]),
                )
            expected_cap = primary_budget_cap_usd(protocol)
            if args.max_cost is not None and args.max_cost != expected_cap:
                raise UsageError("--primary requires the protocol primary-available budget cap")
            args.max_cost = expected_cap
            if args.execute and not args.frozen_commit:
                raise UsageError("primary --execute requires --frozen-commit")
            if args.execute or args.preflight_only:
                # Both gates apply to stage 1 as well, and that is the point of
                # running it: a dirty tree, a checkout that is not the frozen
                # commit, or one missing a fix an earlier attempt died without
                # would each refuse the paid run, so stage 1 has to refuse them
                # too or it certifies a start that would not happen.
                if Path(args.out).resolve() != primary_results_directory(protocol).resolve():
                    raise UsageError("primary evidence must use the frozen results directory")
                # RUN TIME, from the account this author cannot write — not from
                # the frozen protocol. See `attested_main_from_the_collection_account`
                # for why a committed pointer cannot work here.
                attested_main = (
                    attested_main_from_the_collection_account(args.attested_main_file)
                    if args.attested_main_file else None)
                # The absent-target tolerance rests on the model having no
                # delete. Checked here, before the money is committed.
                assert_absent_target_tolerance_premises(protocol)
                origin_main_commit = require_frozen_checkout(
                    args.frozen_commit,
                    required_ancestors=[
                        attempt["cause_fixed_in_commit"]
                        for attempt in protocol["intervening_attempts"]
                        if attempt.get("cause_fixed_in_commit")
                    ],
                    # None when the protocol carries none. `require_frozen_checkout`
                    # refuses only if origin is ALSO a local mirror, so a checkout
                    # with a real remote is unaffected.
                    attested_main=attested_main,
                )
    except UsageError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if aqg_plugin_dir is not None:
            shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
        return EXIT_USAGE

    estimate = float(protocol["budgets"]["estimated_per_cell_usd"]) if protocol else PER_CELL_COST_USD
    n_cells, projected = project_cost(len(tasks), len(selected), len(args.strictness), runs, estimate)
    # Budget is logged and hard-bounded for the frozen primary protocol.
    print(f"benchmark matrix: {len(tasks)} task(s) × {len(selected)} arm(s) × "
          f"{len(args.strictness)} strictness × {runs} run(s) = {n_cells} cells")
    print(f"projected cost: ~${projected:.2f} (@ ${estimate:.3f}/cell est., model {args.model})")
    if protocol is not None:
        theoretical = n_cells * float(protocol["budgets"]["per_cell_max_usd"])
        global_cap = float(protocol["budgets"]["global_max_usd"])
        diagnostic_spend = float(protocol["budgets"]["diagnostic_spend_usd"])
        prior_spend = float(protocol["owner_authorization"]["prior_spend_usd"])
        owner_ceiling = float(protocol["owner_authorization"]["maximum_spend_usd"])
        print(f"primary protocol: sha256={protocol_sha256()} study cap=${global_cap:.7f}; "
              f"charged diagnostics=${diagnostic_spend:.7f}; prior current-authorization spend (frozen; excludes those diagnostics)=${prior_spend:.8f}; "
              f"cumulative Owner ceiling=${owner_ceiling:.7f}; primary available cap=${args.max_cost:.7f}; "
              f"per-cell caps alone would total ${theoretical:.2f}")

    if args.max_cost is not None and projected > args.max_cost:
        print(f"REFUSED: projection ${projected:.2f} exceeds --max-cost ${args.max_cost:.2f}", file=sys.stderr)
        return EXIT_USAGE

    if not args.execute and not args.preflight_only:
        print("\n[dry-run] no API call. Cells:")
        for t in tasks:
            for a in selected:
                for s in args.strictness:
                    print(f"  - {t.name} × {a.name}/{s}  ({a.description})")
        sample_arm = selected[0]
        sample_strict = args.strictness[0]
        sample_prompt = render_prompt((tasks[0] / "task.md").read_text(encoding="utf-8"),
                                      "seed.py", sample_strict)
        sample = build_argv(sample_arm, sample_prompt, model=args.model, allowed_tools=args.allowed_tools)
        printable = [(c[:117] + "...") if len(c) > 120 else c for c in sample]
        print(f"\n[dry-run] sample argv ({sample_arm.name}/{sample_strict}):\n  {' '.join(printable)}")
        print("\nrun for real with --execute (this spends money).")
        if aqg_plugin_dir is not None:
            shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
        return EXIT_OK

    if protocol is not None:
        try:
            # The model child is never allowed to reach even an auth/version/help
            # preflight unwrapped.  Creating the private workspace is zero-cost and
            # happens before the irreversible attempt marker.
            workdir = make_cell_tree()
            model_isolation = preflight_model_isolation_capability(workdir)
            primary_claude_version = preflight_primary_environment(
                protocol, tasks, Path(args.out), model_isolation=model_isolation,
            )
            # The binary this attempt starts on, so the completion check has
            # something to compare against. On this host the CLI moved
            # 2.1.237 -> 2.1.238 overnight under a daily launchd agent, so a
            # start-of-run version check cannot speak for an 600-cell matrix.
            primary_binary_identity = claude_binary_identity(
                primary_claude_version, path=model_isolation.cli_executable)
            arms_all = build_arms(aqg_plugin_dir=aqg_plugin_dir)
            if aqg_plugin_dir is None:
                raise UsageError("AQG plugin snapshot was not prepared")
            selected = [arms_all[name] for name in requested_arms]
            primary_model_isolation_by_arm = {
                arm.name: model_isolation_for_arm(
                    model_isolation, arm.name, aqg_plugin_root=aqg_plugin_dir,
                )
                for arm in selected
            }
            probe = score_file_sandboxed(
                tasks[0] / "good_ref.py", tasks[0] / "checks.py", tasks[0].name.replace("-", "_"),
            )
            if probe.errored or not probe.passed:
                raise UsageError(f"primary scorer preflight failed: {probe.error or probe.failures}")
            # Every deterministic source/integrity probe precedes the marker.
            # A local drift cannot spend the one authorized attempt.
            primary_starting_task_hash = task_tree_sha256(tasks)
            primary_starting_aqg_snapshot_hash = directory_tree_sha256(aqg_plugin_dir / "skills")
            administered_aqg_rules = arms_all["aqg-full"].append_system_prompt
            if administered_aqg_rules is None:
                raise UsageError("AQG treatment prompt was not frozen")
            primary_starting_aqg_rules_hash = hashlib.sha256(administered_aqg_rules.encode("utf-8")).hexdigest()
            if primary_starting_aqg_rules_hash != protocol["treatment_provenance"]["aqg_rules_sha256"]:
                raise UsageError("frozen AQG treatment prompt differs from protocol")
            if args.preflight_only:
                # Stop HERE, and nowhere later. Everything above is a free
                # deterministic probe; the next statement writes durable
                # evidence and the one after it reserves the non-retryable
                # attempt marker. Stage 1 exists precisely so those two can be
                # reached with confidence rather than discovered mid-matrix,
                # so it must not perform either.
                # The checks after this point include free ones — whether the
                # non-retryable marker is already there, whether this study id's
                # evidence already exists. Three attempts before this one were
                # consumed, so those are the conditions most likely to be false;
                # certifying a start without them is the same error this flag
                # exists to prevent, one step later.
                blocked = preflight_paid_start_preconditions(Path(args.out), protocol)
                if blocked:
                    raise UsageError(
                        "a paid start would be refused: " + "; ".join(blocked)
                    )
                print(
                    "\npreflight-only: STAGE 1 of 2 PASS.\n"
                    "Stage 2 — the half that actually tests whether a denied model Read is\n"
                    "provable from the stream — has NOT run. It needs one paid diagnostic\n"
                    "cell and Owner authorization; see PREREGISTRATION.md.\n"
                    f"  frozen checkout : {args.frozen_commit} (ancestor of origin/main {origin_main_commit})\n"
                    f"  claude CLI      : {primary_claude_version}\n"
                    f"  arms constructed: {', '.join(arm.name for arm in selected)}\n"
                    f"  skills tree     : matches protocol {protocol['aqg_skills_tree_sha256'][:12]}\n"
                    f"  task tree       : matches protocol {primary_starting_task_hash[:12]}\n"
                    "  paid start      : not blocked by the attempt marker or unsweepable evidence\n"
                    "                    (writability approximated read-only; the paid run probes it)\n"
                    "  spent           : $0.00000000 — no durable evidence, no attempt marker, no model call."
                )
                # Same three resources, and the same shapes, the UsageError exit
                # releases — including the plugin dir's PARENT, which an earlier
                # draft of this block missed. Kept identical deliberately: this
                # is the duplication a reviewer flagged as drift-prone, and it
                # had already drifted once.
                if workdir is not None:
                    shutil.rmtree(workdir, ignore_errors=True)
                if aqg_plugin_dir is not None:
                    shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
                return EXIT_OK
            # Create every durable evidence artifact and the temporary cell root
            # before the non-retryable marker.  A local setup failure then makes
            # no paid call and does not consume this study's single attempt.
            primary_ledger_handle, prepared_evidence = prepare_primary_evidence(Path(args.out), protocol)
            primary_ledger, primary_raw_dir, primary_solution_dir = prepared_evidence
            try:
                attempt_marker = reserve_primary_attempt(
                    Path(args.out), protocol, prepared_evidence=prepared_evidence,
                )
            except UsageError:
                primary_ledger_handle.close()
                _cleanup_prepared_primary_evidence(prepared_evidence)
                primary_ledger_handle = None
                raise
        except UsageError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            if workdir is not None:
                shutil.rmtree(workdir, ignore_errors=True)
            if aqg_plugin_dir is not None:
                shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
            return EXIT_USAGE

    # Prefix deliberately free of an 'aqg-' substring — the sandbox path lands in
    # the cell's cwd, and the isolation signal scan must not false-trip on it.
    if workdir is None:
        workdir = make_cell_tree()
    out_dir = Path(args.out)
    if protocol is not None:
        if primary_ledger is None or primary_raw_dir is None or primary_solution_dir is None:
            raise RuntimeError("primary evidence paths were not preflighted")
        ledger, raw_dir, solution_dir = primary_ledger, primary_raw_dir, primary_solution_dir
    else:
        out_dir.mkdir(parents=True, exist_ok=True)
        ledger = out_dir / f"run-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
        if ledger.exists():
            print("ERROR: result ledger already exists", file=sys.stderr)
            if aqg_plugin_dir is not None:
                shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
            return EXIT_USAGE
        raw_dir = out_dir / f"{ledger.stem}.raw"
        solution_dir = None
    try:
        if protocol is not None:
            if primary_ledger_handle is None:
                raise RuntimeError("primary evidence ledger was not prepared")
            ledger_handle = primary_ledger_handle
        else:
            ledger_handle = ledger.open("w", encoding="utf-8")
    except (OSError, UsageError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        if aqg_plugin_dir is not None:
            shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
        return EXIT_USAGE
    print(f"\nexecuting → {ledger}\n  (sandboxes under {workdir})")

    blocks = primary_blocks
    atomic_collection = protocol is not None and protocol.get("collection_design") == ATOMIC_MATCHED_BLOCKS
    if atomic_collection:
        arms_by_name = {arm.name: arm for arm in selected}
        cells: list[tuple[str | None, Path, Arm, str, int]] = [
            (block.block_id, block.task, arms_by_name[arm_name], block.strictness, block.run_idx)
            for block in blocks
            for arm_name in block.arm_order
        ]
    else:
        cells = [
            (None, t, a, s, r)
            for t in tasks for a in selected for s in args.strictness for r in range(runs)
        ]
    block_plan = collection_plan_payload(blocks)
    block_plan_sha256 = collection_plan_sha256(blocks) if blocks else None
    results: list[CellResult] = []
    spent = 0.0
    infra_by_arm = {arm.name: 0 for arm in selected}
    per_cell_reservation = float(protocol["budgets"]["per_cell_max_usd"]) if protocol else estimate
    starting_task_hash = primary_starting_task_hash if protocol is not None else None
    starting_aqg_snapshot_hash = primary_starting_aqg_snapshot_hash if protocol is not None else None
    starting_aqg_rules_hash = primary_starting_aqg_rules_hash if protocol is not None else None
    aborted = False
    abort_reason: str | None = None
    completion_written = False
    if protocol is not None and attempt_marker is not None:
        # Ordinary uncaught Python exits after reservation must still leave a
        # terminal record. A hard kill or storage failure cannot be repaired by
        # user-space code, so this record remains fail-closed in the analyzer.
        def write_emergency_completion() -> None:
            if completion_written:
                return
            try:
                with ledger.open("a", encoding="utf-8") as recovery:
                    recovery.write(json.dumps({
                        "record_type": "run_completion",
                        "attempted_cells": len(results),
                        "primary_actual_spend_usd": spent,
                        "diagnostic_spend_usd": float(protocol["budgets"]["diagnostic_spend_usd"]),
                        "combined_actual_spend_usd": spent + float(protocol["budgets"]["diagnostic_spend_usd"]),
                        "owner_authorization_maximum_spend_usd": float(protocol["owner_authorization"]["maximum_spend_usd"]),
                        "prior_spend_under_current_authorization_usd": float(protocol["owner_authorization"]["prior_spend_usd"]),
                        "cumulative_spend_under_current_authorization_usd": spent + float(protocol["owner_authorization"]["prior_spend_usd"]),
                        "aborted": True,
                        "abort_reason": "process_exit_before_completion",
                        "task_tree_sha256_start": starting_task_hash,
                        "task_tree_sha256_end": None,
                        "aqg_skills_snapshot_sha256_end": None,
                        "aqg_rules_sha256_start": starting_aqg_rules_hash,
                        "aqg_rules_sha256_end": None,
                    }, sort_keys=True) + "\n")
                    recovery.flush()
            except OSError:
                pass

        atexit.register(write_emergency_completion)
    with ledger_handle as fh:
        if protocol is not None:
            if attempt_marker is None:
                raise RuntimeError("primary attempt marker was not reserved")
            if not primary_claude_version:
                raise RuntimeError("primary CLI version was not preflighted")
            model_child_env = clean_child_env()
            manifest = {
                "record_type": "run_manifest",
                "protocol_sha256": protocol_sha256(),
                "frozen_commit": args.frozen_commit,
                "protocol_merge_commit": args.frozen_commit,
                "origin_main_commit": origin_main_commit,
                "claude_version": primary_claude_version,
                # The identity the completion check compares against. Recorded
                # in the manifest so a reader of the ledger can see WHICH binary
                # produced the cells, not only which version string it printed.
                "claude_binary_identity_start": primary_binary_identity,
                "python_version": sys.version,
                "platform": platform.platform(),
                "model_child_environment_keys": sorted(model_child_env),
                "model_child_environment_constants": {
                    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": model_child_env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"],
                },
                "model_child_permission_denies": list(CELL_PERMISSION_DENY_RULES),
                "allowed_tools": args.allowed_tools,
                "model": args.model,
                "task_tree_sha256": task_tree_sha256(tasks),
                "primary_attempt_marker": attempt_marker.name,
                "primary_attempt_marker_sha256": hashlib.sha256(attempt_marker.read_bytes()).hexdigest(),
                "aqg_skills_tree_sha256": directory_tree_sha256(AQG_SKILLS_DIR),
                "aqg_skills_snapshot_sha256_start": starting_aqg_snapshot_hash,
                "aqg_rules_sha256": starting_aqg_rules_hash,
                "claude_md_lite_rules_sha256": hashlib.sha256(CLAUDE_MD_LITE_RULES.encode("utf-8")).hexdigest(),
                "research_design": protocol["research_design"],
                "reporting": protocol["reporting"],
                "predecessor_outcome_data_observed": protocol["predecessor"]["outcome_data_observed"],
                "matrix": {"tasks": [task.name for task in tasks], "arms": requested_arms,
                           "strictness": args.strictness, "runs": runs},
                "order_seed": protocol["matrix"]["order_seed"],
                "raw_artifact_dir": raw_dir.name,
                "study_budget_usd": float(protocol["budgets"]["global_max_usd"]),
                "diagnostic_spend_usd": float(protocol["budgets"]["diagnostic_spend_usd"]),
                "primary_budget_cap_usd": args.max_cost,
                "owner_authorization_maximum_spend_usd": float(protocol["owner_authorization"]["maximum_spend_usd"]),
                "prior_spend_under_current_authorization_usd": float(protocol["owner_authorization"]["prior_spend_usd"]),
                "cumulative_spend_under_current_authorization_usd": float(protocol["owner_authorization"]["prior_spend_usd"]),
            }
            if atomic_collection:
                manifest.update({
                    "collection_design": ATOMIC_MATCHED_BLOCKS,
                    "block_count": len(blocks),
                    "block_plan": block_plan,
                    "block_plan_sha256": block_plan_sha256,
                })
            fh.write(json.dumps(manifest, sort_keys=True) + "\n")
            fh.flush()
        active_block_id: str | None = None
        active_block_arms: list[str] = []

        def write_active_block(status: str, gate_class: str | None = None) -> None:
            nonlocal active_block_id, active_block_arms
            if not atomic_collection or active_block_id is None:
                return
            fh.write(json.dumps({
                "record_type": "block_completion",
                "block_id": active_block_id,
                "status": status,
                "arms": active_block_arms,
                "cell_count": len(active_block_arms),
                "gate_class": gate_class,
            }, sort_keys=True) + "\n")
            fh.flush()
            active_block_id = None
            active_block_arms = []

        for (block_id, t, a, s, r) in cells:
            if atomic_collection and block_id != active_block_id:
                write_active_block("structural_complete")
                active_block_id = block_id
                active_block_arms = []
            if protocol is not None and spent + per_cell_reservation > args.max_cost:
                print(f"\nABORT: reserved next-cell spend would exceed --max-cost ${args.max_cost:.2f}.", file=sys.stderr)
                aborted = True
                abort_reason = "reserved_budget"
                break
            if protocol is not None:
                # BEFORE the cell, not only at completion. The known mover on
                # this host fires daily at a fixed time, so an 600-cell matrix
                # is likely to span it; an end-only check would spend the whole
                # budget before refusing (audit 96dc952e, Voice 1 f5, Voice 3
                # f1). Two 1MiB reads per cell is the same millisecond cost that
                # justified the windows in the first place.
                try:
                    require_binary_identity_unchanged(
                        primary_binary_identity,
                        claude_binary_identity(
                            (primary_binary_identity or {}).get("version", ""),
                            path=model_isolation.cli_executable))
                except UsageError as exc:
                    print(f"\nABORT: {exc}", file=sys.stderr)
                    aborted = True
                    abort_reason = "binary_drift"
                    break
            try:
                cell_model_isolation = model_isolation
                if protocol is not None:
                    if a.name not in primary_model_isolation_by_arm:
                        raise RuntimeError("primary model isolation was not preflighted")
                    cell_model_isolation = primary_model_isolation_by_arm[a.name]
                cr = run_cell(t, a, r, model=args.model, allowed_tools=args.allowed_tools,
                              timeout=args.timeout, workdir=workdir, strictness=s,
                              max_budget_usd=(float(protocol["budgets"]["per_cell_max_usd"])
                                              if protocol is not None else None),
                              cost_reservation_usd=per_cell_reservation, raw_dir=raw_dir,
                              solution_dir=solution_dir, block_id=block_id,
                              model_isolation=cell_model_isolation,
                              require_model_isolation=protocol is not None)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
                # completion record must survive an expected cell runtime failure
                print("\nABORT: unexpected primary cell runtime failure.", file=sys.stderr)
                aborted = True
                abort_reason = f"cell_runtime:{type(exc).__name__}"
                break
            results.append(cr)
            if atomic_collection:
                active_block_arms.append(cr.arm)
            # Charge the completed model turn before serializing its detailed
            # evidence.  If a later local write crashes the process, the atexit
            # completion record still carries conservative spent evidence.
            spent += cr.accounted_cost_usd
            cell_record = cr.to_dict()
            for field in ("raw_path", "solution_path"):
                if cell_record.get(field) is not None:
                    cell_record[field] = str(Path(cell_record[field]).relative_to(out_dir))
            fh.write(json.dumps(cell_record) + "\n")
            fh.flush()
            if protocol is not None and (
                    cr.raw_path is None or cr.error == "cannot archive regular model solution"):
                print("\nABORT: durable evidence loss invalidates this primary matrix.", file=sys.stderr)
                aborted = True
                abort_reason = "evidence_durability"
                break
            if cr.cost_accounting == "reserved_missing":
                print("\nWARN: model result did not report cost; reserving the full per-cell cap.", file=sys.stderr)
            if cr.status == STATUS_INFRA:
                infra_by_arm[cr.arm] += 1
            iso = "" if cr.isolation_ok else f"  !!ISOLATION: {cr.isolation_detail}"
            print(f"  [{_STATUS_FLAG[cr.status]}] {cr.task} × {cr.arm}/{cr.strictness} r{cr.run_idx}  "
                  f"fails={cr.n_failures} turns={cr.num_turns} ${cr.cost_usd or 0:.4f}{iso}")
            # Runtime spend ceiling bounds ACTUAL cost, not just the pre-run estimate
            # (audit 9aad54d9 f5): stop the matrix the moment real spend crosses it.
            if args.max_cost is not None and spent > args.max_cost:
                print(f"\nABORT: cumulative spend ${spent:.4f} exceeded --max-cost "
                      f"${args.max_cost:.2f} after {len(results)}/{len(cells)} cells.", file=sys.stderr)
                aborted = True
                abort_reason = "cumulative_budget"
                break
            if protocol is not None and cr.status == STATUS_BUDGET:
                print("\nABORT: a per-cell budget cap invalidates this primary matrix.", file=sys.stderr)
                aborted = True
                abort_reason = "per_cell_budget"
                break
            if (protocol is not None and cr.result_subtype is not None
                    and cr.result_subtype.casefold() != "success"
                    and cr.status != STATUS_BUDGET):
                # The protocol freezes the known max-budget subtype.  Do not
                # quietly treat an unfamiliar non-success terminal as allowable
                # infrastructure attrition: it could be a CLI subtype drift
                # masking a per-cell budget stop.
                print("\nABORT: unrecognized non-success CLI terminal subtype invalidates this primary matrix.", file=sys.stderr)
                aborted = True
                abort_reason = "terminal_subtype"
                break
            if protocol is not None and cr.parse_errors:
                print("\nABORT: malformed stream JSON invalidates this primary matrix.", file=sys.stderr)
                aborted = True
                abort_reason = "stream_parse"
                break
            if protocol is not None and cr.cost_accounting in {"reserved_invalid", "reported_over_cap"}:
                print("\nABORT: invalid or over-cap CLI cost evidence invalidates this primary matrix.", file=sys.stderr)
                aborted = True
                abort_reason = "cost_evidence"
                break
            if protocol is not None:
                allowed_infra = int(float(protocol["validity"]["maximum_infra_rate"]) * len(tasks) * runs)
                if infra_by_arm[cr.arm] > allowed_infra:
                    print("\nABORT: infrastructure attrition has exceeded the frozen primary limit.", file=sys.stderr)
                    aborted = True
                    abort_reason = "infra_attrition"
                    break
            if ((not cr.isolation_ok and not cr.isolation_unavailable)
                    or (not cr.memory_ok and not cr.memory_unavailable)
                    or not cr.workspace_ok):
                print("\nABORT: isolation or file-workspace hard-check failed; primary data are invalid.", file=sys.stderr)
                aborted = True
                abort_reason = "isolation_or_workspace"
                break

        if protocol is not None:
            write_active_block("discarded" if aborted else "structural_complete", abort_reason if aborted else None)

        if protocol is not None:
            try:
                ending_task_hash = task_tree_sha256(tasks)
                ending_aqg_snapshot_hash = directory_tree_sha256(aqg_plugin_dir / "skills")
                ending_aqg_rules_hash = hashlib.sha256(AQG_RULES_FILE.read_bytes()).hexdigest()
            except (OSError, ValueError) as exc:
                ending_task_hash = ending_aqg_snapshot_hash = None
                ending_aqg_rules_hash = None
                aborted = True
                abort_reason = f"completion_integrity:{type(exc).__name__}"
            # In its OWN try, and never sharing the block above. Audit 96dc952e,
            # three voices of three: folding it in meant a UsageError here
            # nulled the snapshot hashes, the unguarded comparison below then
            # saw `None != <start hash>` and rewrote the reason to
            # `source_integrity`, printing "frozen task or plugin snapshot
            # changed" — a false diagnostic for the one failure this check
            # exists to surface.
            try:
                # File identity only: no second CLI execution at completion. The
                # version is carried from the start, so what binds here is the
                # digest and the size — see the residual in that function.
                ending_binary_identity = claude_binary_identity(
                    (primary_binary_identity or {}).get("version", ""),
                    path=model_isolation.cli_executable if model_isolation else None)
            except (OSError, ValueError, UsageError) as exc:
                ending_binary_identity = None
                binary_reason = f"binary_identity_unreadable:{type(exc).__name__}"
            else:
                binary_reason = None
            if (ending_task_hash != starting_task_hash
                    or ending_aqg_snapshot_hash != starting_aqg_snapshot_hash
                    or ending_aqg_rules_hash != starting_aqg_rules_hash):
                print("\nABORT: frozen task or plugin snapshot changed during primary collection.", file=sys.stderr)
                aborted = True
                abort_reason = "source_integrity"
            # Same shape, different thing: the CLI binary itself. Evaluated
            # UNCONDITIONALLY — an earlier abort must not suppress it, because
            # a source-integrity failure and a binary swap are different facts
            # and a reader needs both (audit 96dc952e, three voices of three).
            if ending_binary_identity is not None:
                try:
                    require_binary_identity_unchanged(
                        primary_binary_identity, ending_binary_identity)
                except UsageError as exc:
                    print(f"\nABORT: {exc}", file=sys.stderr)
                    binary_reason = "binary_drift"
            if binary_reason:
                aborted = True
                # Composed, not overwritten, so neither cause is lost.
                abort_reason = (f"{abort_reason},{binary_reason}" if abort_reason
                                else binary_reason)
            fh.write(json.dumps({
                "record_type": "run_completion",
                "attempted_cells": len(results),
                "primary_actual_spend_usd": spent,
                "diagnostic_spend_usd": float(protocol["budgets"]["diagnostic_spend_usd"]),
                "combined_actual_spend_usd": spent + float(protocol["budgets"]["diagnostic_spend_usd"]),
                "owner_authorization_maximum_spend_usd": float(protocol["owner_authorization"]["maximum_spend_usd"]),
                "prior_spend_under_current_authorization_usd": float(protocol["owner_authorization"]["prior_spend_usd"]),
                "cumulative_spend_under_current_authorization_usd": spent + float(protocol["owner_authorization"]["prior_spend_usd"]),
                "aborted": aborted,
                "abort_reason": abort_reason,
                "task_tree_sha256_start": starting_task_hash,
                "task_tree_sha256_end": ending_task_hash,
                "aqg_skills_snapshot_sha256_end": ending_aqg_snapshot_hash,
                "aqg_rules_sha256_start": starting_aqg_rules_hash,
                "aqg_rules_sha256_end": ending_aqg_rules_hash,
                "claude_binary_identity_start": primary_binary_identity,
                "claude_binary_identity_end": ending_binary_identity,
            }, sort_keys=True) + "\n")
            fh.flush()
            completion_written = True

    print(f"\nsummary ({len(results)}/{len(cells)} cells, actual spend ${spent:.4f}):")
    for line in _summary(results):
        print(line)
    contaminated = [
        r for r in results
        if ((not r.isolation_ok and not r.isolation_unavailable)
            or (not r.memory_ok and not r.memory_unavailable)
            or not r.workspace_ok)
    ]
    if aqg_plugin_dir is not None:
        shutil.rmtree(aqg_plugin_dir, ignore_errors=True)
    shutil.rmtree(workdir, ignore_errors=True)
    if contaminated:
        print(f"\nFAILED: {len(contaminated)} cell(s) failed the isolation hard-check — "
              f"results are NOT trustworthy until fixed.", file=sys.stderr)
        return EXIT_CONTAMINATED
    if aborted:
        return EXIT_USAGE
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
