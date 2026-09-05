#!/usr/bin/env python3
"""Handoff manifest v0 validator (Wave 1-0 P0).

Implemented per ENGINEERING_FRAMEWORK.md §4 (sub-agent contract + signed_attestation
phased rollout) + §2 (MPLP L2-inspired naming + aqg_ prefix disclaimer).

Wave 1 scope (schema + validator only):
- schema validation (required nodes / fields / type / enum values)
- env_fingerprint must pass the _surface_redaction gate
- high-stakes mode: gh api lookup of ci_run_id + triple cross-check
  * conclusion == success
  * head_sha == expected_commit_sha (guards against hallucinating / reusing a historical run_id)
  * workflow name == expected_workflow_name
- ordinary PR: schema validate only (Wave 1 does not enforce the contract)
- signed_attestation: required only from Wave 3+ (Q12 phased rollout)

Schema (per §10 prefix + §4 fields):
    aqg_context:
        actor: claude | codex          # required
        parent_session_id: string      # required
        task: string                   # required
    aqg_plan:                          # optional
        intended_action: string
        scope: string
    aqg_confirm:                       # several fields required in high-stakes mode
        command_line: string           # required in high-stakes
        tool_version: string           # required in high-stakes
        env_fingerprint: object        # must run redaction
        ci_run_id: int                 # required in high-stakes
        artifact_uri: string           # required in high-stakes
        log_digest: string             # required in high-stakes
        seeds: list                    # required in high-stakes (may be empty)
        scenario_ids: list             # required in high-stakes (may be empty)
        signed_attestation: object     # required only from Wave 3+
    aqg_trace:
        exit_code: int                 # required
        result: pass | warn | fail     # required
        blockers: list[string]         # optional

CLI:
    python3 scripts/validate_handoff_manifest.py <manifest.json>
    python3 scripts/validate_handoff_manifest.py <manifest.yaml> --high-stakes \\
        --repo deeppatternai/agent-quality-gates \\
        --commit-sha <sha> --workflow-name "Quality Gates Warn-only"

Exit codes:
    0 — manifest valid (in high-stakes mode the cross-check also passes)
    1 — manifest invalid (any of schema / redaction / cross-check fails)
    2 — usage error / file not found

Dependencies: stdlib + AQG-internal _surface_redaction; YAML manifest needs PyYAML (optional).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


EXIT_USAGE = 2
EXIT_INVALID = 1
EXIT_OK = 0


# ===== Schema constants =====

REQUIRED_NODES: tuple[str, ...] = ("aqg_context", "aqg_confirm", "aqg_trace")
OPTIONAL_NODES: tuple[str, ...] = ("aqg_plan",)

REQUIRED_CONTEXT_FIELDS: tuple[str, ...] = ("actor", "parent_session_id", "task")
ALLOWED_ACTORS: frozenset[str] = frozenset({"claude", "codex"})

REQUIRED_TRACE_FIELDS: tuple[str, ...] = ("exit_code", "result")
ALLOWED_RESULTS: frozenset[str] = frozenset({"pass", "warn", "fail"})

# aqg_confirm fields required in high-stakes mode
HIGH_STAKES_CONFIRM_REQUIRED: tuple[str, ...] = (
    "command_line",
    "tool_version",
    "ci_run_id",
    "artifact_uri",
    "log_digest",
    "seeds",
    "scenario_ids",
    # env_fingerprint MUST be present in high-stakes mode: it is the field the
    # redaction gate (_validate_env_fingerprint) scans. Omitting it previously
    # BYPASSED redaction entirely — that function treats an absent fingerprint as
    # "handled by the high_stakes schema check", but the schema check did not list
    # it here (audit bbf4ca5d gpt-f6 + gemini-f1; workflow B3 CRITICAL).
    "env_fingerprint",
)

# ===== v2 schema constants (ADR 2026-05-04 handoff manifest v2) =====

SCHEMA_VERSION_ACCEPTED: frozenset[int] = frozenset({1, 2})
DEFAULT_SCHEMA_VERSION: int = 1  # absent → v1 forward-compat

# v2 aqg_review node
ALLOWED_IMPL_STATUS: frozenset[str] = frozenset({"pending", "in_progress", "done", "blocked"})
ALLOWED_REVIEW_STATUS: frozenset[str] = frozenset({"pending", "passed", "findings", "re_review"})
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Top-level keys recognized by the validator. Unknown keys with `aqg_` prefix
# fail (typo guard); other unknown keys warn-only (forward-compat). The check
# itself lives in _check_unknown_top_level().
KNOWN_TOP_LEVEL: frozenset[str] = frozenset({
    "schema_version", "_doc",
    "aqg_context", "aqg_plan", "aqg_confirm", "aqg_trace", "aqg_review",
})


class _InvalidSchemaVersion(Exception):
    """Raised when schema_version is present but not parseable / not accepted.
    Distinct from absence (which defaults to 1)."""


@dataclass(frozen=True)
class ManifestValidationResult:
    """Record the three error classes separately so the caller can handle each one independently.

    schema_errors: required fields / types / enum values
    redaction_errors: violations of env_fingerprint against _surface_redaction
    cross_check_errors: violations from the gh api lookup in high-stakes mode
    """

    is_valid: bool
    schema_errors: tuple[str, ...] = field(default_factory=tuple)
    redaction_errors: tuple[str, ...] = field(default_factory=tuple)
    cross_check_errors: tuple[str, ...] = field(default_factory=tuple)


# ===== v2 helpers (schema_version dispatch + typo guard + aqg_review) =====


def _get_version(manifest: Mapping[str, Any]) -> int:
    """Return schema version (1 default). Raises _InvalidSchemaVersion on
    parse / accepted-set failure (distinct from absent which silently defaults).

    Per ADR 2026-05-04 handoff-manifest-v2 §5.1 + a2 audit #8 fix.
    """
    if "schema_version" not in manifest:
        return DEFAULT_SCHEMA_VERSION
    sv = manifest["schema_version"]
    if not isinstance(sv, int) or isinstance(sv, bool):
        raise _InvalidSchemaVersion(
            f"schema_version: must be int, got {type(sv).__name__}"
        )
    if sv not in SCHEMA_VERSION_ACCEPTED:
        raise _InvalidSchemaVersion(
            f"schema_version: {sv} not in {sorted(SCHEMA_VERSION_ACCEPTED)}"
        )
    return sv


def _check_unknown_top_level(manifest: Mapping[str, Any]) -> list[str]:
    """Per ADR §2.5 + a1 audit #5: unknown top-level keys with `aqg_` prefix
    fail (typo guard against e.g. aqg_reviwe silently masking review data);
    other unknown keys are forward-compat (no error)."""
    errors: list[str] = []
    for key in manifest:
        if key in KNOWN_TOP_LEVEL:
            continue
        if isinstance(key, str) and key.startswith("aqg_"):
            # truncate the echoed key — untrusted input (B5); 64 chars keeps the
            # typo-guard useful without echoing an unbounded value into logs.
            shown = key if len(key) <= 64 else key[:64] + "…"
            errors.append(
                f"unknown aqg_-prefixed top-level key: {shown!r} "
                f"(typo? known: "
                f"{sorted(k for k in KNOWN_TOP_LEVEL if k.startswith('aqg_'))})"
            )
    return errors


def _validate_review_subnode(
    name: str,
    sub: Any,
    errors: list[str],
    status_enum: frozenset[str],
) -> None:
    """Validate spec_review or quality_review sub-node."""
    if not isinstance(sub, dict):
        errors.append(
            f"aqg_review.{name}: must be mapping, "
            f"got {type(sub).__name__}"
        )
        return
    status = sub.get("status")
    if status not in status_enum:
        errors.append(
            f"aqg_review.{name}.status: must be one of "
            f"{sorted(status_enum)}, got {status!r}"
        )
    if status not in (None, "pending"):
        rm = sub.get("reviewer_model")
        if not isinstance(rm, str) or not rm:
            errors.append(
                f"aqg_review.{name}.reviewer_model: required when status != pending"
            )
    fc = sub.get("findings_count")
    if not isinstance(fc, int) or isinstance(fc, bool) or fc < 0:
        errors.append(
            f"aqg_review.{name}.findings_count: must be non-negative int"
        )
    li = sub.get("loop_iter")
    if not isinstance(li, int) or isinstance(li, bool) or li < 0:
        errors.append(
            f"aqg_review.{name}.loop_iter: must be non-negative int"
        )


def _validate_review_node(manifest: Mapping[str, Any]) -> list[str]:
    """Validate aqg_review node + invariants 1, 2, 3, 5 (per ADR §2.3 + §5).

    Invariant 6 (final-handoff fixed_before_next_task) is checked by
    _check_final_handoff because it depends on aqg_trace cross-node.

    Returns empty list when aqg_review is absent (v1 forward-compat).
    """
    review = manifest.get("aqg_review")
    if review is None:
        return []
    if not isinstance(review, dict):
        return [f"aqg_review: must be mapping, got {type(review).__name__}"]

    # ADR §2.1 lock-in: aqg_review present requires schema_version: 2 (a1 audit #1)
    sv = manifest.get("schema_version")
    if sv != 2:
        return [
            f"aqg_review present but schema_version={sv!r} (must be 2 — ADR §2.1 lock-in)"
        ]

    errors: list[str] = []

    # implementation sub-node
    impl = review.get("implementation")
    if not isinstance(impl, dict):
        errors.append(
            "aqg_review.implementation: required when aqg_review present"
        )
    else:
        impl_status = impl.get("status")
        if impl_status not in ALLOWED_IMPL_STATUS:
            errors.append(
                f"aqg_review.implementation.status: must be one of "
                f"{sorted(ALLOWED_IMPL_STATUS)}, got {impl_status!r}"
            )
        if impl_status not in (None, "pending"):
            im = impl.get("implementer_model")
            if not isinstance(im, str) or not im:
                errors.append(
                    "aqg_review.implementation.implementer_model: required when status != pending"
                )
        commit_shas = impl.get("commit_shas")
        if not isinstance(commit_shas, list):
            errors.append(
                "aqg_review.implementation.commit_shas: must be list"
            )
        else:
            for i, sha in enumerate(commit_shas):
                if not isinstance(sha, str) or not COMMIT_SHA_RE.match(sha):
                    errors.append(
                        f"aqg_review.implementation.commit_shas[{i}]: must match "
                        f"{COMMIT_SHA_RE.pattern}"
                    )
            # Invariant 2: implementation.status == done → commit_shas non-empty
            if impl_status == "done" and not commit_shas:
                errors.append(
                    "aqg_review: invariant 2 — implementation.status==done "
                    "requires at least one commit_sha"
                )

    # spec_review / quality_review sub-nodes
    sr = review.get("spec_review")
    qr = review.get("quality_review")
    if not isinstance(sr, dict):
        errors.append(
            "aqg_review.spec_review: required when aqg_review present"
        )
    else:
        _validate_review_subnode("spec_review", sr, errors, ALLOWED_REVIEW_STATUS)
    if not isinstance(qr, dict):
        errors.append(
            "aqg_review.quality_review: required when aqg_review present"
        )
    else:
        _validate_review_subnode("quality_review", qr, errors, ALLOWED_REVIEW_STATUS)

    # review_loop_count
    rlc = review.get("review_loop_count")
    if not isinstance(rlc, int) or isinstance(rlc, bool) or rlc < 0:
        errors.append(
            "aqg_review.review_loop_count: must be non-negative int"
        )

    # accepted_findings
    af = review.get("accepted_findings")
    if not isinstance(af, list):
        errors.append("aqg_review.accepted_findings: must be list")
    else:
        for i, fid in enumerate(af):
            if not isinstance(fid, str):
                errors.append(
                    f"aqg_review.accepted_findings[{i}]: must be str"
                )

    # Invariant 4 (ADR §2.3): when accepted_findings non-empty,
    # fixed_before_next_task must be explicit (true or false, not missing).
    # Implementation is **stricter** than ADR text: fixed_before_next_task is
    # ALWAYS required when aqg_review is present (not just when accepted_findings
    # is non-empty). The stricter rule subsumes invariant 4 — see test
    # test_invariant_4_fixed_before_next_task_required_even_when_findings_empty
    # for explicit coverage.
    if "fixed_before_next_task" not in review:
        errors.append(
            "aqg_review.fixed_before_next_task: required when aqg_review present"
        )
    elif not isinstance(review["fixed_before_next_task"], bool):
        errors.append(
            f"aqg_review.fixed_before_next_task: must be bool, "
            f"got {type(review['fixed_before_next_task']).__name__}"
        )

    # Invariant 1: quality_review.status != pending → spec_review.status == passed
    if isinstance(qr, dict) and isinstance(sr, dict):
        if qr.get("status") in ("passed", "findings", "re_review"):
            if sr.get("status") != "passed":
                errors.append(
                    "aqg_review: invariant 1 — quality_review.status cannot leave "
                    "pending until spec_review.status == passed"
                )

    # Invariant 3: review_loop_count >= max(spec.loop_iter, quality.loop_iter)
    if isinstance(rlc, int) and not isinstance(rlc, bool):
        sr_iter = sr.get("loop_iter") if isinstance(sr, dict) else 0
        qr_iter = qr.get("loop_iter") if isinstance(qr, dict) else 0
        sr_iter = sr_iter if (isinstance(sr_iter, int) and not isinstance(sr_iter, bool)) else 0
        qr_iter = qr_iter if (isinstance(qr_iter, int) and not isinstance(qr_iter, bool)) else 0
        if rlc < max(sr_iter, qr_iter):
            errors.append(
                f"aqg_review: invariant 3 — review_loop_count ({rlc}) must be >= "
                f"max(spec_review.loop_iter={sr_iter}, "
                f"quality_review.loop_iter={qr_iter})"
            )

    # Invariant 5: spec_review.status != pending → implementation.status == done
    if isinstance(sr, dict) and sr.get("status") in ("passed", "findings", "re_review"):
        if isinstance(impl, dict):
            if impl.get("status") != "done":
                errors.append(
                    "aqg_review: invariant 5 — spec_review.status cannot leave "
                    "pending until implementation.status == done"
                )
            elif not impl.get("commit_shas"):
                errors.append(
                    "aqg_review: invariant 5 — spec_review needs implementation "
                    "to have at least one commit_sha"
                )

    return errors


def _check_final_handoff(manifest: Mapping[str, Any]) -> list[str]:
    """Invariant 6 (per ADR §2.3 + a1 audit #2): when aqg_trace.result == "pass"
    and accepted_findings non-empty, fixed_before_next_task must be true.
    Cross-node, so factored out of _validate_review_node."""
    review = manifest.get("aqg_review")
    if not isinstance(review, dict):
        return []
    trace = manifest.get("aqg_trace")
    if not isinstance(trace, dict):
        return []
    if trace.get("result") != "pass":
        return []
    af = review.get("accepted_findings")
    # a3 audit fix: require list type explicitly. Truthy non-list values
    # (string, dict, etc.) should NOT trigger invariant 6 — the malformed-type
    # error from _validate_review_node is the single source of truth for that.
    if not isinstance(af, list) or not af:
        return []
    if review.get("fixed_before_next_task") is not True:
        return [
            "aqg_review: invariant 6 — when aqg_trace.result==pass and "
            "accepted_findings non-empty, fixed_before_next_task must be true"
        ]
    return []


# ===== Schema validation =====


def _validate_schema(manifest: Mapping[str, Any], *, high_stakes: bool) -> list[str]:
    """Schema-layer validation: required / type / enum. Returns an errors list (empty = pass)."""
    errors: list[str] = []

    if not isinstance(manifest, Mapping):
        return [f"<root>: manifest must be a mapping, got {type(manifest).__name__}"]

    # v2 schema_version dispatch (raises _InvalidSchemaVersion on bad version)
    try:
        _get_version(manifest)
    except _InvalidSchemaVersion as exc:
        errors.append(str(exc))
        # Continue with remaining checks so caller sees full error set

    # v2 typo guard (aqg_-prefixed unknown keys)
    errors.extend(_check_unknown_top_level(manifest))

    # required nodes
    for node in REQUIRED_NODES:
        if node not in manifest:
            errors.append(f"missing required node: {node}")
        elif not isinstance(manifest[node], dict):
            errors.append(
                f"{node}: must be a mapping, got {type(manifest[node]).__name__}"
            )

    # === aqg_context ===
    ctx = manifest.get("aqg_context")
    if isinstance(ctx, dict):
        for fld in REQUIRED_CONTEXT_FIELDS:
            if fld not in ctx:
                errors.append(f"aqg_context.{fld}: missing required field")
            elif not isinstance(ctx[fld], str) or not ctx[fld]:
                errors.append(f"aqg_context.{fld}: must be a non-empty string")
        # Do NOT echo the raw actor value — it is untrusted manifest input and
        # echoing it into a validation error (which may reach CI logs) is a leak
        # path (audit bbf4ca5d gpt-f6; workflow B5). Report the allowed set only.
        if isinstance(ctx.get("actor"), str) and ctx["actor"] not in ALLOWED_ACTORS:
            errors.append(f"aqg_context.actor: must be one of {sorted(ALLOWED_ACTORS)}")

    # === aqg_trace ===
    trc = manifest.get("aqg_trace")
    if isinstance(trc, dict):
        for fld in REQUIRED_TRACE_FIELDS:
            if fld not in trc:
                errors.append(f"aqg_trace.{fld}: missing required field")
        if "exit_code" in trc and not isinstance(trc["exit_code"], int):
            errors.append(
                f"aqg_trace.exit_code: must be int, "
                f"got {type(trc['exit_code']).__name__}"
            )
        # bool is a subclass of int, but exit_code should be int not bool
        if isinstance(trc.get("exit_code"), bool):
            errors.append("aqg_trace.exit_code: must be int, got bool")
        # raw value not echoed — untrusted input → CI-log leak path (B5).
        if "result" in trc and trc["result"] not in ALLOWED_RESULTS:
            errors.append(f"aqg_trace.result: must be one of {sorted(ALLOWED_RESULTS)}")

    # === aqg_confirm ===
    cnf = manifest.get("aqg_confirm")
    if isinstance(cnf, dict):
        if high_stakes:
            for fld in HIGH_STAKES_CONFIRM_REQUIRED:
                if fld not in cnf:
                    errors.append(
                        f"aqg_confirm.{fld}: required for high-stakes mode"
                    )
            # Type checks for PRESENT fields (audit bbf4ca5d gpt-f5; workflow B4):
            # presence alone let a malformed high-stakes manifest pass (e.g.
            # command_line as {} or seeds as a string). env_fingerprint type +
            # redaction are checked by _validate_env_fingerprint.
            for fld in ("command_line", "tool_version", "artifact_uri", "log_digest"):
                if fld in cnf and (not isinstance(cnf[fld], str) or not cnf[fld]):
                    errors.append(f"aqg_confirm.{fld}: must be a non-empty string")
            for fld in ("seeds", "scenario_ids"):
                if fld in cnf and not isinstance(cnf[fld], list):
                    errors.append(
                        f"aqg_confirm.{fld}: must be a list, "
                        f"got {type(cnf[fld]).__name__}"
                    )
            # ci_run_id type check (must be int in high-stakes mode; bool is an int subclass and must be excluded)
            if "ci_run_id" in cnf:
                if not isinstance(cnf["ci_run_id"], int) or isinstance(
                    cnf["ci_run_id"], bool
                ):
                    errors.append("aqg_confirm.ci_run_id: must be int")

    # === aqg_review (v2 only) ===
    errors.extend(_validate_review_node(manifest))

    # Invariant 6 (cross-node aqg_review × aqg_trace)
    errors.extend(_check_final_handoff(manifest))

    return errors


# ===== Redaction integration =====


def _validate_env_fingerprint(manifest: Mapping[str, Any]) -> list[str]:
    """env_fingerprint must pass the _surface_redaction gate."""
    cnf = manifest.get("aqg_confirm")
    if not isinstance(cnf, dict):
        return []  # already caught at the schema stage
    fp = cnf.get("env_fingerprint")
    if fp is None:
        return []  # missing field is handled by the high_stakes schema check
    if not isinstance(fp, dict):
        return [f"aqg_confirm.env_fingerprint: must be a mapping, got {type(fp).__name__}"]

    try:
        from _surface_redaction import check_fingerprint
    except ImportError as exc:
        return [f"aqg_confirm.env_fingerprint: cannot import _surface_redaction ({exc})"]

    result = check_fingerprint(fp)
    if result.is_safe:
        return []
    return [f"aqg_confirm.env_fingerprint.{v}" for v in result.violations]


# ===== Cross-check (high-stakes only) =====


def _gh_api(path: str, *, timeout: int = 15) -> tuple[int, str]:
    """thin wrapper for gh api. Returns (returncode, stdout)."""
    try:
        proc = subprocess.run(
            ["gh", "api", path],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip()
    except FileNotFoundError:
        return 127, "gh: not found (install GitHub CLI)"
    except subprocess.TimeoutExpired:
        return 124, f"gh api {path}: timed out after {timeout}s"
    except Exception as exc:  # noqa: BLE001 - validator must not crash on cli quirks
        return 1, f"{type(exc).__name__}: {exc}"


def cross_check_ci_run_id(
    *,
    repo: str,
    ci_run_id: int,
    expected_commit_sha: str,
    expected_workflow_name: str,
    gh_api_func=_gh_api,  # injectable for tests
) -> list[str]:
    """high-stakes: gh api lookup + triple cross-check.

    Returns an errors list (empty = pass).

    cross-check (revised per §4 step 4):
    1. conclusion == "success"
    2. head_sha == expected_commit_sha (guards against hallucinating / reusing a historical run_id)
    3. workflow name == expected_workflow_name
    """
    errors: list[str] = []

    rc, out = gh_api_func(f"/repos/{repo}/actions/runs/{ci_run_id}")
    if rc != 0:
        errors.append(
            f"ci_run_id={ci_run_id}: gh api failed (rc={rc}): {out[:200]}"
        )
        return errors

    try:
        run = json.loads(out)
    except json.JSONDecodeError as exc:
        errors.append(f"ci_run_id={ci_run_id}: response not JSON ({exc})")
        return errors

    # 1. conclusion
    conclusion = run.get("conclusion")
    if conclusion != "success":
        errors.append(
            f"ci_run_id={ci_run_id}: conclusion={conclusion!r} (expected success)"
        )

    # 2. head_sha
    actual_sha = run.get("head_sha", "")
    if actual_sha != expected_commit_sha:
        errors.append(
            f"ci_run_id={ci_run_id}: head_sha={actual_sha!r} != "
            f"expected {expected_commit_sha!r} (LLM hallucinate?)"
        )

    # 3. workflow name (call gh api once more to fetch the workflow detail)
    workflow_id = run.get("workflow_id")
    if workflow_id is None:
        errors.append(f"ci_run_id={ci_run_id}: missing workflow_id in response")
    else:
        rc2, out2 = gh_api_func(f"/repos/{repo}/actions/workflows/{workflow_id}")
        if rc2 != 0:
            errors.append(
                f"workflow_id={workflow_id}: gh api failed (rc={rc2}): {out2[:200]}"
            )
        else:
            try:
                workflow = json.loads(out2)
                actual_name = workflow.get("name", "")
                if actual_name != expected_workflow_name:
                    errors.append(
                        f"workflow name={actual_name!r} != "
                        f"expected {expected_workflow_name!r}"
                    )
            except json.JSONDecodeError as exc:
                errors.append(
                    f"workflow_id={workflow_id}: response not JSON ({exc})"
                )

    return errors


# ===== Manifest loading =====


def _load_manifest(path: Path) -> dict:
    """Supports .json / .yaml / .yml; YAML needs PyYAML."""
    if not path.is_file():
        raise FileNotFoundError(f"manifest not found: {path}")
    text = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()

    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-untyped]
        except ImportError as exc:
            raise RuntimeError(
                "YAML manifest requires PyYAML (pip install pyyaml) "
                "or convert to JSON"
            ) from exc
        loaded = yaml.safe_load(text)
        if not isinstance(loaded, dict):
            raise RuntimeError(f"YAML root must be a mapping, got {type(loaded).__name__}")
        return loaded

    # default: JSON
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"JSON parse error in {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise RuntimeError(f"JSON root must be an object, got {type(loaded).__name__}")
    return loaded


# ===== Main API =====


def validate_manifest(
    *,
    manifest: Mapping[str, Any] | None = None,
    manifest_path: Path | str | None = None,
    high_stakes: bool = False,
    expected_commit_sha: str | None = None,
    expected_workflow_name: str | None = None,
    repo: str | None = None,
    skip_remote_cross_check: bool = False,
    gh_api_func=_gh_api,
) -> ManifestValidationResult:
    """Main API. Accepts either a manifest dict or a manifest_path.

    In high-stakes mode, when skip_remote_cross_check is not set, repo +
    expected_commit_sha + expected_workflow_name must be provided.
    """
    if manifest is None:
        if manifest_path is None:
            raise ValueError("Either manifest or manifest_path must be provided")
        manifest = _load_manifest(Path(manifest_path))

    # Top-level guard: non-Mapping fails immediately; all downstream helpers assume Mapping
    if not isinstance(manifest, Mapping):
        return ManifestValidationResult(
            is_valid=False,
            schema_errors=(
                f"<root>: manifest must be a mapping, got {type(manifest).__name__}",
            ),
        )

    schema_errors = _validate_schema(manifest, high_stakes=high_stakes)
    redaction_errors = _validate_env_fingerprint(manifest)

    cross_check_errors: list[str] = []
    if high_stakes and not skip_remote_cross_check:
        # must provide repo / commit_sha / workflow_name
        missing = []
        if not repo:
            missing.append("repo")
        if not expected_commit_sha:
            missing.append("expected_commit_sha")
        if not expected_workflow_name:
            missing.append("expected_workflow_name")
        if missing:
            cross_check_errors.append(
                f"high-stakes cross-check requires: {', '.join(missing)}"
            )
        else:
            cnf = manifest.get("aqg_confirm", {})
            ci_run_id = cnf.get("ci_run_id") if isinstance(cnf, dict) else None
            if isinstance(ci_run_id, int) and not isinstance(ci_run_id, bool):
                cross_check_errors.extend(
                    cross_check_ci_run_id(
                        repo=repo,
                        ci_run_id=ci_run_id,
                        expected_commit_sha=expected_commit_sha,
                        expected_workflow_name=expected_workflow_name,
                        gh_api_func=gh_api_func,
                    )
                )
            # ci_run_id type issues are already caught at the schema stage

    is_valid = not (schema_errors or redaction_errors or cross_check_errors)
    return ManifestValidationResult(
        is_valid=is_valid,
        schema_errors=tuple(schema_errors),
        redaction_errors=tuple(redaction_errors),
        cross_check_errors=tuple(cross_check_errors),
    )


# ===== CLI =====


def _format_result(result: ManifestValidationResult) -> str:
    if result.is_valid:
        return "OK: manifest valid"
    lines = ["FAIL: manifest invalid"]
    for label, errors in (
        ("schema", result.schema_errors),
        ("redaction", result.redaction_errors),
        ("cross-check", result.cross_check_errors),
    ):
        if errors:
            lines.append(f"  {label}:")
            for err in errors:
                lines.append(f"    - {err}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_handoff_manifest",
        description="Validate AQG handoff manifest v0 (schema + redaction + optional cross-check).",
    )
    parser.add_argument("manifest", help="Path to manifest (.json / .yaml / .yml)")
    parser.add_argument(
        "--high-stakes",
        action="store_true",
        help="Enable high-stakes mode (cross-check ci_run_id × commit SHA × workflow name)",
    )
    parser.add_argument(
        "--commit-sha",
        help="Expected head commit SHA (required if --high-stakes and not --no-remote-check)",
    )
    parser.add_argument(
        "--workflow-name",
        help="Expected workflow name (required if --high-stakes and not --no-remote-check)",
    )
    parser.add_argument(
        "--repo",
        help="Repo in 'owner/name' format (required if --high-stakes and not --no-remote-check)",
    )
    parser.add_argument(
        "--no-remote-check",
        action="store_true",
        help="Skip gh api remote cross-check (still runs schema + redaction)",
    )
    args = parser.parse_args(argv)

    try:
        result = validate_manifest(
            manifest_path=args.manifest,
            high_stakes=args.high_stakes,
            expected_commit_sha=args.commit_sha,
            expected_workflow_name=args.workflow_name,
            repo=args.repo,
            skip_remote_cross_check=args.no_remote_check,
        )
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return EXIT_USAGE

    print(_format_result(result))
    return EXIT_OK if result.is_valid else EXIT_INVALID


# ===== Self-test =====


def _make_clean_manifest() -> dict[str, Any]:
    return {
        "aqg_context": {
            "actor": "claude",
            "parent_session_id": "session-abc-123",
            "task": "implement Wave 1-0 surface redaction",
        },
        "aqg_plan": {
            "intended_action": "add scripts/_surface_redaction.py",
            "scope": "AQG quality layer",
        },
        "aqg_confirm": {
            "command_line": "python3 scripts/_surface_redaction.py",
            "tool_version": "0.2.0",
            "env_fingerprint": {
                "claude_settings_json": {
                    "exists": True,
                    "size_bytes": 1024,
                    "sha256_first8": "a1b2c3d4",
                    "hooks_count": 1,
                },
                "path_dirs_count": 12,
            },
            "ci_run_id": 12345,
            "artifact_uri": "https://github.com/o/r/actions/runs/12345",
            "log_digest": "sha256:abcdef1234567890",
            "seeds": [],
            "scenario_ids": [],
        },
        "aqg_trace": {
            "exit_code": 0,
            "result": "pass",
            "blockers": [],
        },
    }


def _fake_gh_api_factory(responses: dict[str, tuple[int, str]]):
    """Build a fake gh_api_func: path -> (rc, json-stdout)."""
    def _fake(path: str, *, timeout: int = 15) -> tuple[int, str]:
        if path in responses:
            return responses[path]
        return (1, f"unexpected path: {path}")
    return _fake


def self_test() -> int:
    """Cover the main schema / redaction / cross-check cases + edge cases."""
    # === Clean manifest, schema-only mode ===
    clean = _make_clean_manifest()
    result = validate_manifest(manifest=clean)
    assert result.is_valid, f"clean manifest should pass schema-only: {result}"

    # === missing required node ===
    bad = _make_clean_manifest()
    del bad["aqg_context"]
    result = validate_manifest(manifest=bad)
    assert not result.is_valid
    assert any("aqg_context" in e for e in result.schema_errors)

    # === aqg_context.actor invalid enum ===
    bad = _make_clean_manifest()
    bad["aqg_context"]["actor"] = "gemini"
    result = validate_manifest(manifest=bad)
    assert not result.is_valid
    assert any("actor" in e for e in result.schema_errors), result.schema_errors

    # === aqg_trace.result invalid enum ===
    bad = _make_clean_manifest()
    bad["aqg_trace"]["result"] = "succeeded"
    result = validate_manifest(manifest=bad)
    assert not result.is_valid
    assert any("result" in e for e in result.schema_errors)

    # === aqg_trace.exit_code as bool rejected (Python bool is an int subclass) ===
    bad = _make_clean_manifest()
    bad["aqg_trace"]["exit_code"] = True  # bool, not int
    result = validate_manifest(manifest=bad)
    assert not result.is_valid

    # === missing aqg_context.task ===
    bad = _make_clean_manifest()
    del bad["aqg_context"]["task"]
    result = validate_manifest(manifest=bad)
    assert not result.is_valid

    # === high-stakes mode: ci_run_id missing ===
    high_stakes_bad = _make_clean_manifest()
    del high_stakes_bad["aqg_confirm"]["ci_run_id"]
    result = validate_manifest(
        manifest=high_stakes_bad,
        high_stakes=True,
        skip_remote_cross_check=True,
    )
    assert not result.is_valid
    assert any("ci_run_id" in e for e in result.schema_errors)

    # === redaction integration: env_fingerprint contains a violating field ===
    bad_redact = _make_clean_manifest()
    bad_redact["aqg_confirm"]["env_fingerprint"]["openai_api_key"] = "sk-leak"
    result = validate_manifest(manifest=bad_redact)
    assert not result.is_valid
    assert result.redaction_errors, f"expected redaction error: {result}"

    # === L2 hardening regression (audit bbf4ca5d) ===
    # B3 (CRIT): omitting env_fingerprint in high-stakes mode must NOT bypass the
    # redaction gate — it is now in HIGH_STAKES_CONFIRM_REQUIRED.
    b3 = _make_clean_manifest()
    del b3["aqg_confirm"]["env_fingerprint"]
    result = validate_manifest(manifest=b3, high_stakes=True, skip_remote_cross_check=True)
    assert not result.is_valid, "B3: high-stakes manifest without env_fingerprint must fail"
    assert any("env_fingerprint" in e for e in result.schema_errors), f"B3: {result.schema_errors}"

    # B4: present-but-wrong-type high-stakes fields are rejected (not passed on presence).
    b4 = _make_clean_manifest()
    b4["aqg_confirm"]["command_line"] = {"not": "a string"}
    b4["aqg_confirm"]["seeds"] = "not-a-list"
    result = validate_manifest(manifest=b4, high_stakes=True, skip_remote_cross_check=True)
    assert not result.is_valid, "B4: malformed high-stakes field types must fail"
    assert any("command_line" in e for e in result.schema_errors), f"B4: {result.schema_errors}"
    assert any("seeds" in e for e in result.schema_errors), f"B4: {result.schema_errors}"

    # B5: an invalid enum value must NOT be echoed into the error (CI-log leak guard).
    b5 = _make_clean_manifest()
    b5["aqg_context"]["actor"] = "leaked-sk-secret-value"
    result = validate_manifest(manifest=b5)
    assert not result.is_valid
    assert not any("leaked-sk-secret-value" in e for e in result.schema_errors), \
        f"B5: raw actor value leaked into error: {result.schema_errors}"

    # === high-stakes cross-check (using fake gh_api) ===
    # case 1: all cross-checks pass
    fake_pass = _fake_gh_api_factory({
        "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
            0,
            json.dumps({
                "conclusion": "success",
                "head_sha": "deadbeef" * 5,
                "workflow_id": 999,
            }),
        ),
        "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
            0,
            json.dumps({"name": "Quality Gates"}),
        ),
    })
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        repo="deeppatternai/agent-quality-gates",
        expected_commit_sha="deadbeef" * 5,
        expected_workflow_name="Quality Gates",
        gh_api_func=fake_pass,
    )
    assert result.is_valid, f"high-stakes clean should pass: {result}"

    # case 2: head_sha mismatch (LLM hallucinate run_id)
    fake_sha_drift = _fake_gh_api_factory({
        "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
            0,
            json.dumps({
                "conclusion": "success",
                "head_sha": "wrong_sha_xx",
                "workflow_id": 999,
            }),
        ),
        "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
            0,
            json.dumps({"name": "Quality Gates"}),
        ),
    })
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        repo="deeppatternai/agent-quality-gates",
        expected_commit_sha="deadbeef" * 5,
        expected_workflow_name="Quality Gates",
        gh_api_func=fake_sha_drift,
    )
    assert not result.is_valid
    assert any("head_sha" in e for e in result.cross_check_errors), (
        f"sha mismatch should fail: {result}"
    )

    # case 3: workflow name mismatch
    fake_workflow_drift = _fake_gh_api_factory({
        "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
            0,
            json.dumps({
                "conclusion": "success",
                "head_sha": "deadbeef" * 5,
                "workflow_id": 999,
            }),
        ),
        "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
            0,
            json.dumps({"name": "Other Workflow"}),
        ),
    })
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        repo="deeppatternai/agent-quality-gates",
        expected_commit_sha="deadbeef" * 5,
        expected_workflow_name="Quality Gates",
        gh_api_func=fake_workflow_drift,
    )
    assert not result.is_valid
    assert any("workflow name" in e for e in result.cross_check_errors)

    # case 4: conclusion != success
    fake_failed_run = _fake_gh_api_factory({
        "/repos/deeppatternai/agent-quality-gates/actions/runs/12345": (
            0,
            json.dumps({
                "conclusion": "failure",
                "head_sha": "deadbeef" * 5,
                "workflow_id": 999,
            }),
        ),
        "/repos/deeppatternai/agent-quality-gates/actions/workflows/999": (
            0,
            json.dumps({"name": "Quality Gates"}),
        ),
    })
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        repo="deeppatternai/agent-quality-gates",
        expected_commit_sha="deadbeef" * 5,
        expected_workflow_name="Quality Gates",
        gh_api_func=fake_failed_run,
    )
    assert not result.is_valid
    assert any("conclusion" in e for e in result.cross_check_errors)

    # case 5: gh api fails (offline / not authenticated)
    fake_offline = _fake_gh_api_factory({})
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        repo="deeppatternai/agent-quality-gates",
        expected_commit_sha="deadbeef" * 5,
        expected_workflow_name="Quality Gates",
        gh_api_func=fake_offline,
    )
    assert not result.is_valid

    # === high-stakes missing repo / commit_sha / workflow_name ===
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
    )
    assert not result.is_valid
    assert any("requires" in e for e in result.cross_check_errors)

    # === skip_remote_cross_check still enforces schema but does not call gh api ===
    result = validate_manifest(
        manifest=_make_clean_manifest(),
        high_stakes=True,
        skip_remote_cross_check=True,
    )
    assert result.is_valid, f"clean high-stakes with skip should pass: {result}"

    # === manifest_path JSON path loading ===
    import tempfile
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as tmp:
        json.dump(_make_clean_manifest(), tmp)
        tmp_path = tmp.name
    try:
        result = validate_manifest(manifest_path=tmp_path)
        assert result.is_valid
    finally:
        os.unlink(tmp_path)

    # === non-mapping input ===
    result = validate_manifest(manifest=["not", "a", "dict"])  # type: ignore[arg-type]
    assert not result.is_valid

    # === must pass manifest or manifest_path ===
    try:
        validate_manifest()
    except ValueError:
        pass
    else:
        raise AssertionError("validate_manifest() without args should raise ValueError")

    print("OK: validate_handoff_manifest self-test passed")
    return 0


if __name__ == "__main__":
    # run self_test when invoked as a module, run main when invoked as a CLI
    if len(sys.argv) == 2 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test())
    raise SystemExit(main())
