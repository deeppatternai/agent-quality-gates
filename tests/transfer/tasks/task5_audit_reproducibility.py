"""Task 5 — Audit reproducibility against golden expected finding set.

v1 advisory (warn-only) per §5.1 + Q4 default.

Implementation strategy: this task compares a previously-saved audit
result (run by the caller — typically Owner or CI workflow that has
audit-mcp access) against a curated golden minimum expected finding set.
It does NOT invoke gpt_audit directly from this module — the audit
result is read from a conventional path:

    tests/transfer/_task5_run_result.json

If that file does not exist, Task 5 returns `warn` with a note
explaining the absence (advisory skip; does not block threshold).

If the file exists, parse + canonical-key match against
tests/transfer/fixtures/audit_repro_golden.yaml. Every
expected_min_findings[i].canonical_key must appear in the actual
run with severity in the allowed set. Extra findings are tolerated.
Fewer findings → fail (advisory; sets task5_advisory='fail' which
DOES block threshold per §5.4).

Canonical finding key (per §3 Task 5 + Q8 default):
- Take the `issue` string field of each finding.
- Lowercase + collapse non-alphanumerics to single space + token-split.
- First 4 tokens, hyphen-joined.
- Empty / fewer tokens → use whatever tokens exist.

v1 ships with this rule; v1.1+ may upgrade to embedding similarity.

Result-file fixture binding (audit 297dccac gpt #2):
The result file MUST embed a `bound_to` block referencing the current
fixture state, otherwise a stale result file would silently pass:

    {
      "bound_to": {
        "artifact_sha256": "<sha256 of fixtures/audit_repro_artifact.md at run time>",
        "fixture_version": "v1.0.0"
      },
      "issues": [...]    // or "audits": [...]
    }

Task 5 recomputes the artifact sha256 at runtime and rejects the result
file unless `bound_to.artifact_sha256` matches and the fixture_version
agrees with the golden file. This prevents reuse of an audit result
captured against a different artifact (intentional rotation OR
accidental drift).

Pass criteria (§3 Task 5):
- pass = every expected canonical_key appears with severity in allowed set
- warn = no audit result file present (advisory skip)
- fail = audit ran but missing required canonical_key OR severity out of allowed set
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


_TOKEN_SPLIT_RE = re.compile(r"[^a-z0-9]+")


def canonical_key(issue: str, *, n_tokens: int = 4) -> str:
    """Extract canonical finding key from `issue` field.

    Lowercase + collapse non-alphanum to single delimiter + token split +
    take first n_tokens + hyphen-join. Empty input → empty key.
    """
    if not isinstance(issue, str):
        return ""
    lowered = issue.lower()
    tokens = [t for t in _TOKEN_SPLIT_RE.split(lowered) if t]
    if not tokens:
        return ""
    return "-".join(tokens[:n_tokens])


def _load_yaml_or_json(path: Path) -> Any:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        import yaml
        return yaml.safe_load(text)
    return json.loads(text)


def _extract_findings(data: Any) -> list[dict[str, Any]]:
    """Pull findings list out of the audit result payload.

    Audit results may be shaped as `{"issues": [...], ...}` (single
    auditor) or `{"audits": [{"issues": [...]}], ...}` (panel). We
    flatten the panel form.
    """
    findings: list[dict[str, Any]] = []
    if isinstance(data, dict):
        if isinstance(data.get("issues"), list):
            findings.extend(i for i in data["issues"] if isinstance(i, dict))
        if isinstance(data.get("audits"), list):
            for a in data["audits"]:
                if isinstance(a, dict) and isinstance(a.get("issues"), list):
                    findings.extend(
                        i for i in a["issues"] if isinstance(i, dict)
                    )
    elif isinstance(data, list):
        findings.extend(i for i in data if isinstance(i, dict))
    return findings


def run(repo: Path) -> TaskOutcome:
    transfer = repo / "tests" / "transfer"
    result_path = transfer / "_task5_run_result.json"
    golden_path = transfer / "fixtures" / "audit_repro_golden.yaml"

    if not golden_path.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=1,
            notes=(
                f"audit_repro_golden.yaml missing at {golden_path}; "
                f"v1 fixture incomplete"
            ),
        )

    if not result_path.is_file():
        # B1: explicit advisory-skip path. skipped=True is the source of truth
        # for the runner — no shape-inference re-introduces v1 ambiguity.
        return TaskOutcome(
            result="warn",
            exit_code=0,
            required_checks_passed=0,
            required_checks_total=1,
            notes=(
                f"task5 advisory skipped: no audit result at "
                f"{result_path.relative_to(repo)}; provide result file or run "
                f"audit manually then place result there. v1 advisory "
                f"posture allows skip without blocking threshold."
            ),
            task5_model_id=None,
            task5_provider=None,
            skipped=True,
        )

    try:
        golden = _load_yaml_or_json(golden_path)
        result_data = _load_yaml_or_json(result_path)
    except Exception as exc:
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=1,
            notes=f"failed to load golden or result: {exc}",
        )

    if not isinstance(golden, dict):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=1,
            notes="audit_repro_golden.yaml: top-level must be mapping",
        )

    # Audit 297dccac gpt #2: bind result file to current fixture state so a
    # stale / fabricated _task5_run_result.json cannot false-pass. Result
    # MUST carry `bound_artifact_sha256` matching the current artifact's
    # sha256 (re-computed at runtime) and `bound_fixture_version` matching
    # the golden file's fixture_version.
    artifact_path = transfer / "fixtures" / "audit_repro_artifact.md"
    if not artifact_path.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=1,
            notes=f"audit_repro_artifact.md missing at {artifact_path}",
        )
    import hashlib
    current_sha = hashlib.sha256(
        artifact_path.read_bytes()
    ).hexdigest()
    bound_sha = None
    bound_fixture_version = None
    if isinstance(result_data, dict):
        binding = result_data.get("bound_to") or {}
        if isinstance(binding, dict):
            bound_sha = binding.get("artifact_sha256")
            bound_fixture_version = binding.get("fixture_version")

    if not isinstance(bound_sha, str) or bound_sha != current_sha:
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=1,
            notes=(
                f"task5 result file is not bound to current artifact: "
                f"bound_to.artifact_sha256={bound_sha!r}, current sha256="
                f"{current_sha!r}. Re-run audit against current "
                f"audit_repro_artifact.md and embed bound_to block."
            ),
        )

    golden_fv = golden.get("fixture_version")
    if (
        isinstance(golden_fv, str)
        and isinstance(bound_fixture_version, str)
        and bound_fixture_version != golden_fv
    ):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=1,
            notes=(
                f"task5 result fixture_version={bound_fixture_version!r} "
                f"!= golden fixture_version={golden_fv!r}; rotate together"
            ),
        )

    expected_min = golden.get("expected_min_findings", [])
    if not isinstance(expected_min, list):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=1,
            notes="expected_min_findings: must be list",
        )

    # Audit 297dccac gpt #1 + gemini #2 (convergent): canonical_key
    # collisions across multiple findings must collect ALL observed
    # severities, not silently overwrite. Match passes when actual
    # severities ∩ allowed severities is non-empty.
    findings = _extract_findings(result_data)
    actual_keys: dict[str, set[str]] = {}
    for f in findings:
        issue = f.get("issue", "")
        sev = f.get("severity", "")
        if not isinstance(sev, str):
            sev = ""
        ck = canonical_key(issue)
        if ck:
            actual_keys.setdefault(ck, set()).add(sev.lower())

    model_id = None
    provider = None
    if isinstance(result_data, dict):
        if isinstance(result_data.get("model_used"), str):
            model_id = result_data["model_used"]
        if isinstance(result_data.get("transport"), str):
            provider = result_data["transport"]
        elif isinstance(result_data.get("audits"), list) and result_data["audits"]:
            first = result_data["audits"][0]
            if isinstance(first, dict):
                model_id = model_id or first.get("model_used")
                provider = provider or first.get("transport")

    if isinstance(model_id, str) and not model_id:
        model_id = None
    if isinstance(provider, str) and not provider:
        provider = None

    checks_total = len(expected_min)
    checks_passed = 0
    notes_lines: list[str] = [f"loaded {len(findings)} findings from result"]
    missing: list[str] = []
    severity_mismatches: list[str] = []

    for entry in expected_min:
        if not isinstance(entry, dict):
            notes_lines.append(f"malformed expected entry: {entry!r}")
            continue
        ek = entry.get("canonical_key")
        allowed_sev = entry.get("severity", [])
        if not isinstance(ek, str) or not ek:
            notes_lines.append(f"expected entry missing canonical_key: {entry!r}")
            continue
        if not isinstance(allowed_sev, list):
            allowed_sev = [str(allowed_sev)]
        allowed_lower = {
            str(s).lower() for s in allowed_sev if isinstance(s, (str, int))
        }
        if ek not in actual_keys:
            missing.append(ek)
            continue
        observed = actual_keys[ek]
        if allowed_lower and not (observed & allowed_lower):
            severity_mismatches.append(
                f"{ek}: actual severities {sorted(observed)} ∩ "
                f"{sorted(allowed_lower)} = ∅"
            )
            continue
        checks_passed += 1

    if missing:
        notes_lines.append(f"missing required canonical_keys: {missing}")
    if severity_mismatches:
        notes_lines.append(f"severity mismatch: {severity_mismatches}")

    if checks_passed == checks_total and not missing and not severity_mismatches:
        return TaskOutcome(
            result="pass",
            exit_code=0,
            required_checks_passed=checks_passed,
            required_checks_total=checks_total,
            notes="; ".join(notes_lines),
            task5_model_id=model_id,
            task5_provider=provider,
        )
    return TaskOutcome(
        result="fail",
        exit_code=1,
        required_checks_passed=checks_passed,
        required_checks_total=checks_total,
        notes="; ".join(notes_lines),
        task5_model_id=model_id,
        task5_provider=provider,
    )
