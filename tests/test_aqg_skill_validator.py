"""Tests for scripts/_skill_template_schema.py + scripts/aqg_skill_validator.py.

Wave 4+ machine-template B-1 — covers schema validation, frontmatter
checks, cross-cutting registration, and CLI exit codes.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import _skill_template_schema as sts  # noqa: E402
import aqg_skill_validator as asv  # noqa: E402


# ===== Helpers =====


def _make_valid_sidecar(skill_name: str = "aqg-test-skill") -> dict:
    """Minimal sidecar that passes schema check."""
    return {
        "skill_template_schema": 1,
        "name": skill_name,
        "description": "Test skill demonstrating the AQG sidecar manifest schema; runs no-op example check. Use for testing only; not for production.",
        "boundary_class": "read-only",
        "trigger_grammar": {
            "keywords": ["test", "demo"],
            "sentence_patterns": ["Run the test"],
        },
        "entry_script": f"skills/{skill_name}/scripts/run.py",
        "cli_contract": {
            "0": "success",
            "1": "expected check failure",
            "2": "usage misuse",
            "3": "config or schema error",
            "70": "internal error",
        },
        "output_shape": "markdown_table",
        "reads_paths": [f"skills/{skill_name}/"],
        "writes_paths": [],
        "forbidden_paths": ["production/"],
        "aqg_agent_gating": False,
        "owner_only_actions": [],
        "cases": [
            {"id": f"{skill_name}-desc-001", "style": "description_based",
             "prompt": "Run test 1", "expected_skill": skill_name,
             "skill_file_ref": f"skills/{skill_name}/SKILL.md"},
            {"id": f"{skill_name}-desc-002", "style": "description_based",
             "prompt": "Run test 2", "expected_skill": skill_name,
             "skill_file_ref": f"skills/{skill_name}/SKILL.md"},
            {"id": f"{skill_name}-desc-003", "style": "description_based",
             "prompt": "Run test 3", "expected_skill": skill_name,
             "skill_file_ref": f"skills/{skill_name}/SKILL.md"},
            {"id": f"{skill_name}-desc-004", "style": "description_based",
             "prompt": "Run test 4", "expected_skill": skill_name,
             "skill_file_ref": f"skills/{skill_name}/SKILL.md"},
            {"id": f"{skill_name}-explicit-001", "style": "explicit_invocation",
             "prompt": f"Run {skill_name}", "expected_skill": skill_name,
             "skill_file_ref": f"skills/{skill_name}/SKILL.md"},
        ],
        "self_test_entrypoint": f"skills/{skill_name}/scripts/self_test.py",
        "fixed_before_next_task_required_when_findings": True,
        "numeric_values_quoted": True,
    }


# ===== _skill_template_schema tests =====


def test_valid_sidecar_passes():
    result = sts.check_skill_template(_make_valid_sidecar())
    assert result.is_safe, result.violations


def test_missing_required_field_fails():
    s = _make_valid_sidecar()
    del s["entry_script"]
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("entry_script" in v for v in result.violations)


def test_unknown_top_level_field_fails():
    s = _make_valid_sidecar()
    s["something_else"] = "x"
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_doc_field_allowed_for_samples():
    """_doc field is reserved for sample manifests (mirror handoff pattern)."""
    s = _make_valid_sidecar()
    s["_doc"] = "Sample manifest for documentation."
    result = sts.check_skill_template(s)
    assert result.is_safe, result.violations


def test_invalid_skill_name_fails():
    s = _make_valid_sidecar()
    s["name"] = "BadName"  # uppercase + no aqg- prefix
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_skill_name_must_start_aqg():
    s = _make_valid_sidecar()
    s["name"] = "my-skill"  # missing aqg- prefix
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_invalid_boundary_class_fails():
    s = _make_valid_sidecar()
    s["boundary_class"] = "writes-prod"
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_audit_mode_legal_value_is_silent():
    """Finding D-a: a legal audit_mode_required emits NO blanket advisory warn —
    that noise fired for every skill each run and failed --strict pack-wide.
    The field stays (recommended-depth metadata); only the noise is gone."""
    s = _make_valid_sidecar()
    s["audit_mode_required"] = "deep"
    result = sts.check_skill_template(s)
    assert result.is_safe, result.violations
    assert not any("audit_mode_required" in w for w in result.warnings), result.warnings


def test_invalid_audit_mode_fails():
    s = _make_valid_sidecar()
    s["audit_mode_required"] = "quintuple"
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_cli_contract_missing_code_fails():
    s = _make_valid_sidecar()
    del s["cli_contract"]["70"]  # missing internal error code
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("70" in v for v in result.violations)


def test_cli_contract_str_keys_accepted():
    s = _make_valid_sidecar()
    # Already str keys in default; verify int keys also OK
    s["cli_contract"] = {0: "ok", 1: "fail", 2: "misuse", 3: "config", 70: "internal"}
    result = sts.check_skill_template(s)
    assert result.is_safe, result.violations


def test_invalid_output_shape_fails():
    s = _make_valid_sidecar()
    s["output_shape"] = "csv"
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_path_with_leading_slash_fails():
    s = _make_valid_sidecar()
    s["reads_paths"] = ["/etc/secrets"]
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_path_with_dotdot_fails():
    s = _make_valid_sidecar()
    s["reads_paths"] = ["../parent/file"]
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_aqg_agent_gating_must_be_bool():
    s = _make_valid_sidecar()
    s["aqg_agent_gating"] = "true"  # str not bool
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_cases_too_few_fails():
    s = _make_valid_sidecar()
    s["cases"] = s["cases"][:3]  # only 3 cases
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("at least 5" in v for v in result.violations)


def test_cases_mix_rule_too_few_description():
    s = _make_valid_sidecar()
    # 1 description-based + 4 explicit (violates ≥3 description rule)
    s["cases"][1]["style"] = "explicit_invocation"
    s["cases"][2]["style"] = "explicit_invocation"
    s["cases"][3]["style"] = "explicit_invocation"
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("description_based" in v for v in result.violations)


def test_cases_mix_rule_no_explicit_fails():
    s = _make_valid_sidecar()
    s["cases"][4]["style"] = "description_based"  # zero explicit
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("explicit_invocation" in v for v in result.violations)


def test_case_without_drift_hash_is_valid():
    """M3 (spec §6): drift_hash was retired with the drift-hash mechanism (PR-3).
    A sidecar case without drift_hash is now valid — the schema no longer knows
    the field, and unknown case keys are tolerated (forward-compat)."""
    s = _make_valid_sidecar()
    assert "drift_hash" not in s["cases"][0]  # _make_valid_sidecar no longer emits it
    assert sts.check_skill_template(s).is_safe, "case without drift_hash must be valid"
    # audit cb3a562c: a stray drift_hash — even the int the retired check would
    # have rejected — is now ignored (unknown case keys are tolerated).
    s["cases"][0]["drift_hash"] = 12345678
    assert sts.check_skill_template(s).is_safe, "stray drift_hash must be ignored"


def test_case_invalid_style_fails():
    s = _make_valid_sidecar()
    s["cases"][0]["style"] = "implicit"
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_case_missing_required_field_fails():
    s = _make_valid_sidecar()
    del s["cases"][0]["expected_skill"]
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_schema_version_unknown_fails():
    s = _make_valid_sidecar()
    s["skill_template_schema"] = 99
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_schema_version_bool_fails():
    s = _make_valid_sidecar()
    s["skill_template_schema"] = True
    result = sts.check_skill_template(s)
    assert not result.is_safe


def test_assert_safe_raises():
    s = _make_valid_sidecar()
    s["boundary_class"] = "bogus"
    with pytest.raises(sts.SkillTemplateSchemaError):
        sts.assert_safe_skill_template(s)


# ===== aqg_skill_validator frontmatter tests =====


def test_parse_frontmatter_valid():
    text = "---\nname: aqg-foo\ndescription: Foo skill description\n---\n\n# Foo\n"
    parsed, errs = asv._parse_frontmatter(text)
    assert not errs
    assert parsed == {"name": "aqg-foo", "description": "Foo skill description"}


def test_parse_frontmatter_missing_open_delimiter():
    text = "name: aqg-foo\ndescription: Foo\n---\n\n# Foo\n"
    parsed, errs = asv._parse_frontmatter(text)
    assert errs
    assert any("must start with" in e for e in errs)


def test_parse_frontmatter_missing_close_delimiter():
    text = "---\nname: aqg-foo\ndescription: Foo\n# Foo\n"
    parsed, errs = asv._parse_frontmatter(text)
    assert errs
    assert any("missing closing" in e for e in errs)


def test_validate_frontmatter_extra_keys_fail():
    sidecar = _make_valid_sidecar("aqg-test")
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text(
            "---\nname: aqg-test\n"
            "description: " + sidecar["description"] + "\n"
            "version: 1.0\n"  # extra key — should fail
            "---\n\n# Test\n## Boundaries\nNo prod\n",
            encoding="utf-8",
        )
        errs = asv._validate_frontmatter(skill_md, sidecar)
        assert errs
        assert any("extra keys" in e for e in errs)


def test_validate_frontmatter_name_mismatch_fails():
    sidecar = _make_valid_sidecar("aqg-test")
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text(
            "---\nname: aqg-other\ndescription: " + sidecar["description"] + "\n---\n\n# X\n## Boundaries\nNo\n",
            encoding="utf-8",
        )
        errs = asv._validate_frontmatter(skill_md, sidecar)
        assert errs
        assert any("frontmatter.name" in e for e in errs)


def test_validate_boundary_section_missing_fails_for_writes_class():
    """a3 audit #2: missing boundary H2 is hard error when boundary_class != read-only."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text("---\nname: x\ndescription: y\n---\n\n# X\nNo H2 here.\n",
                            encoding="utf-8")
        errs, warns = asv._validate_boundary_section(skill_md, "writes-evidence")
        assert errs
        assert any("boundary section" in e for e in errs)
        assert not warns


def test_validate_boundary_section_missing_advisory_for_read_only():
    """a3 audit #2: missing boundary H2 is advisory warn for read-only class."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text("---\nname: x\ndescription: y\n---\n\n# X\nNo H2 here.\n",
                            encoding="utf-8")
        errs, warns = asv._validate_boundary_section(skill_md, "read-only")
        assert not errs
        assert warns


def test_validate_boundary_section_present_ok():
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text(
            "---\nname: x\ndescription: y\n---\n\n# X\n## Stop Boundaries\nfoo\n",
            encoding="utf-8",
        )
        errs, warns = asv._validate_boundary_section(skill_md, "writes-code")
        assert not errs


def test_validate_boundary_section_owner_h2_no_longer_passes():
    """a3 audit #2: explicit allowlist excludes generic 'Owner-pinned defaults' H2."""
    with tempfile.TemporaryDirectory() as tmp:
        skill_md = Path(tmp) / "SKILL.md"
        skill_md.write_text(
            "---\nname: x\ndescription: y\n---\n\n# X\n## Owner-pinned defaults\nfoo\n",
            encoding="utf-8",
        )
        errs, warns = asv._validate_boundary_section(skill_md, "writes-code")
        # Owner-pinned defaults is NOT in allowlist (was greenlit by old loose regex)
        assert errs


# ===== a3 audit fix tests =====


def test_cross_cutting_substring_match_rejected():
    """a3 audit #1: aqg-audit substring-of aqg-audit-adjudication should NOT match."""
    # In real repo, "aqg-audit" should NOT pass cross-cutting check
    # because no skill named exactly "aqg-audit" is registered.
    errs = asv._validate_cross_cutting(REPO, "aqg-audit")
    assert errs
    # doctor / install / drift_test must all complain since exact "aqg-audit"
    # is not registered (only "aqg-audit-adjudication" is)
    assert any("aqg_doctor.py" in e for e in errs)


def test_cross_cutting_real_skill_passes_doctor_check():
    """Sanity: a real shipped skill name passes doctor + install + managed checks."""
    errs = asv._validate_cross_cutting(REPO, "aqg-audit-adjudication")
    doctor_errs = [e for e in errs if "aqg_doctor.py" in e]
    install_errs = [e for e in errs if "install.sh" in e]
    # All shipped skills are managed (wrapper_generated: true), so the
    # wrapper-generated clause must not fire (spec 2026-05-31 §6/§7).
    managed_errs = [e for e in errs if "not managed" in e]
    assert not doctor_errs
    assert not install_errs
    assert not managed_errs


def test_exit_code_documentation_in_module_docstring():
    """a3 audit #3: exit code contract checked in MODULE docstring, not anywhere."""
    with tempfile.TemporaryDirectory() as tmp:
        # Script with full exit-code contract in module docstring
        good = Path(tmp) / "good.py"
        good.write_text(
            '"""Tool X.\n\n'
            "Exit codes:\n"
            "    0: success\n"
            "    1: expected check failure\n"
            "    2: usage misuse\n"
            "    3: config error\n"
            "    70: internal error\n"
            '"""\n'
            "import sys\n"
            "sys.exit(0)\n",
            encoding="utf-8",
        )
        sidecar = {"entry_script": str(good.relative_to(REPO))} if good.is_relative_to(REPO) else None
        # Use absolute path through repo_root pattern
        # We pass an absolute entry_script; build a mini-repo root
        # Simpler: directly test the function with a constructed sidecar
        # Move file under REPO/scripts/_temp_test.py? Risky. Instead run via abs path and patch
        # Just test the helper _has_exit_code_block:
        assert asv._has_exit_code_block(good.read_text(encoding="utf-8"))


def test_exit_code_block_helper_rejects_partial():
    """Partial exit code documentation (missing 70) must fail."""
    incomplete = (
        "Exit codes:\n"
        "    0: success\n"
        "    1: failure\n"
    )
    assert not asv._has_exit_code_block(incomplete)


def test_exit_code_block_helper_rejects_prose_match():
    """Prose like 'we use codes 0-1' must NOT count as documented contract."""
    prose = (
        "This script uses exit codes 0-3 and 70 sometimes.\n"
        "But this isn't a real contract block.\n"
    )
    assert not asv._has_exit_code_block(prose)


def test_yaml_frontmatter_handles_quoted_value():
    """a3 audit #5: yaml.safe_load handles quoted values + CRLF."""
    text = '---\r\nname: "aqg-foo"\r\ndescription: \'a quoted value\'\r\n---\r\n\r\n# Foo\n'
    parsed, errs = asv._parse_frontmatter(text)
    assert not errs, errs
    assert parsed.get("name") == "aqg-foo"
    assert parsed.get("description") == "a quoted value"


def test_yaml_frontmatter_handles_folded_scalar():
    """a3 audit #5: folded scalar `>` for long descriptions."""
    text = (
        "---\n"
        "name: aqg-foo\n"
        "description: >\n"
        "  This is a long\n"
        "  description folded\n"
        "  into multiple lines.\n"
        "---\n\n# Foo\n"
    )
    parsed, errs = asv._parse_frontmatter(text)
    assert not errs, errs
    # Folded scalar joins lines with spaces
    assert "long" in parsed.get("description", "")
    assert "folded" in parsed.get("description", "")


# ===== Cross-field invariants (a3 audit #4) =====


def test_read_only_with_writes_paths_fails():
    s = _make_valid_sidecar()
    s["boundary_class"] = "read-only"
    s["writes_paths"] = ["docs/incidents/"]
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("read-only requires writes_paths empty" in v for v in result.violations)


def test_forbidden_overlaps_reads_fails():
    s = _make_valid_sidecar()
    s["reads_paths"] = ["secrets/"]
    s["forbidden_paths"] = ["secrets/", "production/"]
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("forbidden_paths overlaps reads_paths" in v for v in result.violations)


def test_forbidden_overlaps_writes_fails():
    s = _make_valid_sidecar()
    s["boundary_class"] = "writes-evidence"
    s["writes_paths"] = ["docs/incidents/"]
    s["forbidden_paths"] = ["docs/incidents/", "production/"]
    result = sts.check_skill_template(s)
    assert not result.is_safe
    assert any("forbidden_paths overlaps writes_paths" in v for v in result.violations)


def test_writes_evidence_with_writes_paths_ok():
    s = _make_valid_sidecar()
    s["boundary_class"] = "writes-evidence"
    s["writes_paths"] = ["docs/incidents/"]
    result = sts.check_skill_template(s)
    assert result.is_safe, result.violations


# ===== Cross-cutting tests (use real repo files) =====


def test_cross_cutting_known_skill_passes():
    """A shipped skill (e.g. aqg-startup-preflight) must pass cross-cutting check."""
    errs = asv._validate_cross_cutting(REPO, "aqg-startup-preflight")
    # We may or may not trip on triggers.yaml count depending on fixture state,
    # but doctor / install / managed (wrapper_generated) must be clean.
    doctor_install_errs = [e for e in errs if "aqg_doctor.py" in e or "install.sh" in e or "not managed" in e]
    assert not doctor_install_errs, doctor_install_errs


def test_cross_cutting_unknown_skill_fails():
    errs = asv._validate_cross_cutting(REPO, "aqg-nonexistent-bogus")
    assert errs
    assert any("aqg_doctor.py" in e for e in errs)


# ===== validate_skill end-to-end =====


def test_validate_skill_nonexistent_dir():
    result = asv.validate_skill("aqg-totally-fake")
    assert not result.is_valid
    assert any("does not exist" in e for e in result.skill_dir_violations)


# ===== CLI integration =====


def test_cli_unknown_skill_returns_invalid():
    script = REPO / "scripts" / "aqg_skill_validator.py"
    proc = subprocess.run(
        [sys.executable, str(script), "aqg-totally-fake"],
        text=True, capture_output=True,
    )
    assert proc.returncode == asv.EXIT_INVALID


def test_cli_json_output_shape():
    script = REPO / "scripts" / "aqg_skill_validator.py"
    proc = subprocess.run(
        [sys.executable, str(script), "aqg-totally-fake", "--json"],
        text=True, capture_output=True,
    )
    parsed = json.loads(proc.stdout)
    assert "skill_name" in parsed
    assert "is_valid" in parsed
    assert "sidecar_violations" in parsed
    assert "skill_dir_violations" in parsed
    assert "cross_cutting_violations" in parsed
    assert "warnings" in parsed


# ===== Sample sidecar validation =====


def test_sample_sidecar_passes_schema():
    """templates/skill_template_example.json must pass schema check."""
    sample_path = REPO / "templates" / "skill_template_example.json"
    assert sample_path.is_file(), f"sample sidecar missing: {sample_path}"
    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    result = sts.check_skill_template(sample)
    assert result.is_safe, result.violations


# ===== Batch-2 audit 73f7e6f6 regression tests =====
#
# C1a: cross-cutting FALSE-PASS — a commented-out / prose mention of the skill
#      name must NOT satisfy registration (must be quote- or path-delimited).
# C1b: cross-cutting fail-open — a missing required drift test / triggers
#      fixture must yield a cross_cutting violation (fail-closed).
# C2 : exit-code-contract FALSE-FAIL — comment / markdown-table / 2-char
#      description forms must satisfy `_has_exit_code_block`.
# P2 : `_count_triggers_for_skill` must count only the documented fixture
#      container (top-level list OR explicit `cases` key), not arbitrary lists.


def _make_fake_repo(tmp: str, *, skill_name: str = "aqg-fake-skill") -> Path:
    """Build a minimal repo skeleton with the cross-cutting registry files,
    each registering `skill_name` in its real (quote-delimited) form.

    Includes a managed sidecar (skills/<name>/skill.template.json with
    `wrapper_generated: true`) so the skill satisfies the wrapper-generated
    cross-cutting clause (spec 2026-05-31 §6/§7); tests that exercise the
    unmanaged path overwrite it via `_write_managed_sidecar(..., managed=False)`.
    """
    root = Path(tmp)
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "agent-packs" / "claude-code").mkdir(parents=True, exist_ok=True)
    (root / "tests" / "behavior" / "fixtures").mkdir(parents=True, exist_ok=True)

    (root / "scripts" / "aqg_doctor.py").write_text(
        f'CLAUDE_SKILL_NAMES = (\n    "{skill_name}",\n)\n'
        f'CODEX_SKILL_NAMES = (\n    "{skill_name}",\n)\n',
        encoding="utf-8",
    )
    (root / "scripts" / "install.sh").write_text(
        f'skills=(\n  "{skill_name}"\n)\n', encoding="utf-8"
    )
    (root / "agent-packs" / "claude-code" / "install.sh").write_text(
        f'skills=(\n  "{skill_name}"\n)\n', encoding="utf-8"
    )
    fixture = "cases:\n"
    for i in range(5):
        fixture += f'  - id: {skill_name}-{i:03d}\n    expected_skill: {skill_name}\n'
    (root / "tests" / "behavior" / "fixtures" / "triggers.yaml").write_text(
        fixture, encoding="utf-8"
    )
    _write_managed_sidecar(root, skill_name, managed=True)
    return root


# --- C1a: comment / prose mentions do NOT satisfy registration ---


def test_c1a_comment_only_mention_does_not_register():
    """C1a: a skill name appearing only inside a `#` comment must NOT satisfy
    `_file_contains_quoted_skill` — only quote/path-delimited forms count."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "install.sh"
        f.write_text("# aqg-fake-skill is documented here but not registered\n",
                     encoding="utf-8")
        assert not asv._file_contains_quoted_skill(f, "aqg-fake-skill")


def test_c1a_prose_mention_does_not_register():
    """C1a: a bare prose mention (no quotes / no path) must NOT register."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "doctor.py"
        f.write_text("Note: aqg-fake-skill should eventually be added.\n",
                     encoding="utf-8")
        assert not asv._file_contains_quoted_skill(f, "aqg-fake-skill")


def test_c1a_double_quoted_still_registers():
    """C1a: the real double-quoted registration form must STILL register."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "install.sh"
        f.write_text('skills=(\n  "aqg-fake-skill"\n)\n', encoding="utf-8")
        assert asv._file_contains_quoted_skill(f, "aqg-fake-skill")


def test_c1a_single_quoted_still_registers():
    """C1a: single-quoted Python/list entry must STILL register."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "doctor.py"
        f.write_text("NAMES = (\n    'aqg-fake-skill',\n)\n", encoding="utf-8")
        assert asv._file_contains_quoted_skill(f, "aqg-fake-skill")


def test_c1a_path_form_still_registers():
    """C1a: a `skills/<name>/` path reference must STILL register."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "install.sh"
        f.write_text("link skills/aqg-fake-skill/SKILL.md\n", encoding="utf-8")
        assert asv._file_contains_quoted_skill(f, "aqg-fake-skill")


def test_c1a_comment_only_yields_cross_cutting_violation():
    """C1a end-to-end: a fake repo whose doctor mentions the skill ONLY in a
    comment must produce a cross_cutting (doctor) violation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        # Demote doctor registration to a comment-only mention.
        (root / "scripts" / "aqg_doctor.py").write_text(
            "# aqg-fake-skill — TODO register me\n", encoding="utf-8"
        )
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert any("aqg_doctor.py" in e for e in errs), errs


# --- C1b: missing required triggers file fails closed ---


def test_c1b_missing_triggers_fixture_yields_violation():
    """C1b: absent tests/behavior/fixtures/triggers.yaml must yield a
    cross_cutting violation (fail-closed), not silently pass."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        (root / "tests" / "behavior" / "fixtures" / "triggers.yaml").unlink()
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert any("triggers.yaml" in e for e in errs), errs


def test_c1b_all_registries_present_no_violation():
    """C1b sanity: when every required registry is present and correctly
    registers the skill, there is NO cross_cutting violation."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert not errs, errs


# ===== Cross-cutting wrapper-generated clause (managed-skill requirement) =====
#
# SKILL.md-from-source overlay (spec 2026-05-31 §6/§7): the legacy test_drift.py
# hash baseline was retired once all skills migrated, so a skill MUST opt into
# wrapper generation (is_managed_skill(sidecar) is True); its committed wrapper
# is then guarded by the CI regen→git-status gate. An unmanaged (unflagged)
# skill yields a cross_cutting violation (fail-closed). Threaded via the one
# shared is_managed_skill helper.


def _write_managed_sidecar(root: Path, skill_name: str, *, managed: bool) -> None:
    """Drop a sidecar at skills/<name>/skill.template.json toggling the flag."""
    skill_dir = root / "skills" / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)
    sidecar = {"name": skill_name}
    if managed:
        sidecar["wrapper_generated"] = True
    (skill_dir / "skill.template.json").write_text(
        json.dumps(sidecar), encoding="utf-8"
    )


def test_managed_skill_passes_cross_cutting():
    """A managed skill (wrapper_generated: true) must NOT produce a
    wrapper-generated violation — its wrapper is regen→git-status gated."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        _write_managed_sidecar(root, "aqg-fake-skill", managed=True)
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert not errs, errs


def test_unmanaged_skill_fails_cross_cutting():
    """An unmanaged (unflagged) skill must fail the wrapper-generated clause —
    the retired test_drift.py hash baseline no longer covers it (fail-closed)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        _write_managed_sidecar(root, "aqg-fake-skill", managed=False)
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        managed_errs = [e for e in errs if "not managed" in e]
        assert managed_errs, "unmanaged skill must fail the wrapper-generated clause"


def test_managed_skill_still_requires_other_registries():
    """The wrapper-generated clause is one of several — doctor / install /
    triggers are still independently required."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        # Demote the doctor registration to a comment-only mention (not a real
        # quoted/path registration → must still fail the doctor clause).
        (root / "scripts" / "aqg_doctor.py").write_text(
            "# aqg-fake-skill not yet registered here\n", encoding="utf-8"
        )
        _write_managed_sidecar(root, "aqg-fake-skill", managed=True)
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert any("aqg_doctor.py" in e for e in errs), errs
        assert not any("not managed" in e for e in errs), errs


def test_explicit_sidecar_arg_overrides_disk_load():
    """When a sidecar dict is passed explicitly, it is used (not re-loaded from
    disk) — lets the caller at validate_skill thread the already-parsed sidecar."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        # Overwrite the on-disk sidecar with an unmanaged one, then pass a
        # managed one explicitly — the explicit arg must win (no violation).
        _write_managed_sidecar(root, "aqg-fake-skill", managed=False)
        errs = asv._validate_cross_cutting(
            root, "aqg-fake-skill", sidecar={"name": "aqg-fake-skill", "wrapper_generated": True}
        )
        assert not any("not managed" in e for e in errs), errs


def test_absent_sidecar_yields_not_managed():
    """audit 454077e6 gpt-f2: when skills/<name>/skill.template.json is absent,
    _validate_cross_cutting loads it best-effort (None → {}) and the
    wrapper-generated clause fails closed with a 'not managed' violation — it
    must NOT raise. PR-3 made the sidecar the sole drift-registration judge, so
    its absent/malformed paths now carry the fail-closed guarantee."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        (root / "skills" / "aqg-fake-skill" / "skill.template.json").unlink()
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert any("not managed" in e for e in errs), errs


def test_malformed_sidecar_yields_not_managed():
    """audit 454077e6 gpt-f2: a malformed (unparseable JSON) sidecar must fail
    closed as 'not managed' — _load_sidecar raises RuntimeError, the loader
    catches it and treats the skill as unmanaged, never crashing the validator."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-fake-skill")
        (root / "skills" / "aqg-fake-skill" / "skill.template.json").write_text(
            "{ not valid json", encoding="utf-8"
        )
        errs = asv._validate_cross_cutting(root, "aqg-fake-skill")
        assert any("not managed" in e for e in errs), errs


# --- C2: exit-code-block heuristic tolerates comment / table / short desc ---


def test_c2_comment_prefixed_exit_code_block():
    """C2: a shell-style `#`-comment exit-code block must satisfy the heuristic."""
    text = (
        "# Exit codes:\n"
        "#   0: success\n"
        "#   1: expected failure\n"
        "#   2: usage error\n"
        "#   3: config error\n"
        "#   70: internal error\n"
    )
    assert asv._has_exit_code_block(text)


def test_c2_markdown_table_exit_code_block():
    """C2: a markdown-table exit-code block (leading `|` pipe) must satisfy."""
    text = (
        "| code | meaning |\n"
        "|------|---------|\n"
        "| 0 | success |\n"
        "| 1 | failure |\n"
        "| 2 | usage |\n"
        "| 3 | config |\n"
        "| 70 | internal |\n"
    )
    assert asv._has_exit_code_block(text)


def test_c2_two_char_description_exit_code_block():
    """C2: a 2-char description such as `0: OK` must satisfy (was rejected by
    the old \\S{3,} requirement)."""
    text = (
        "Exit codes:\n"
        "    0: OK\n"
        "    1: no\n"
        "    2: hm\n"
        "    3: ok\n"
        "    70: hi\n"
    )
    assert asv._has_exit_code_block(text)


def test_c2_still_requires_all_codes():
    """C2 guard: relaxation must not make a partial block (missing 70) pass."""
    text = (
        "# Exit codes:\n"
        "#   0: OK\n"
        "#   1: no\n"
        "#   2: hm\n"
        "#   3: ok\n"
    )
    assert not asv._has_exit_code_block(text)


# --- P2: count only the documented fixture container ---


def test_p2_non_cases_top_level_list_does_not_satisfy_minimum():
    """P2: 5 matching entries placed in a NON-`cases` top-level dict key must
    NOT satisfy the >=5 minimum (only `cases` or a top-level list counts)."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "triggers.yaml"
        body = "archived:\n"
        for i in range(5):
            body += f"  - id: x-{i}\n    expected_skill: aqg-fake-skill\n"
        f.write_text(body, encoding="utf-8")
        count = asv._count_triggers_for_skill(f, "aqg-fake-skill")
        assert count == 0, f"archived list must not count, got {count}"


def test_p2_cases_key_counts():
    """P2: entries under the explicit `cases` key must count."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "triggers.yaml"
        body = "schema_version: 1\ncases:\n"
        for i in range(5):
            body += f"  - id: x-{i}\n    expected_skill: aqg-fake-skill\n"
        f.write_text(body, encoding="utf-8")
        count = asv._count_triggers_for_skill(f, "aqg-fake-skill")
        assert count == 5, count


def test_p2_top_level_list_counts():
    """P2: a bare top-level list (no `cases` wrapper) must still count."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "triggers.yaml"
        body = ""
        for i in range(5):
            body += f"- id: x-{i}\n  expected_skill: aqg-fake-skill\n"
        f.write_text(body, encoding="utf-8")
        count = asv._count_triggers_for_skill(f, "aqg-fake-skill")
        assert count == 5, count


def test_p2_real_fixture_still_counts_five():
    """P2 regression guard: the REAL repo fixture (dict with `cases` key) must
    still report >=5 for a shipped skill."""
    triggers = REPO / "tests" / "behavior" / "fixtures" / "triggers.yaml"
    count = asv._count_triggers_for_skill(triggers, "aqg-skill-validator")
    assert count is not None and count >= 5, count


# ===== Codex interface (display_name) tests — fallback-bug guard =====
#
# Surfaced by the 2026-05-28 memory audit: 4 shipped skills (automation-audit,
# multi-review, phase-transition, security-review) had NO agents/openai.yaml, so
# the Codex picker rendered the "Aqg <Name>" slug fallback. The convention lived
# only in machine-local memory; with no validator gate the bug recurred 4 times.
# These tests pin the gate in the repo so it travels with the code.


def test_extract_display_name_from_yaml():
    text = 'interface:\n  display_name: "AQG Foo"\n  short_description: "x"\n'
    assert asv._extract_display_name(text) == "AQG Foo"


def test_extract_display_name_missing_returns_none():
    text = 'interface:\n  short_description: "no display name here"\n'
    assert asv._extract_display_name(text) is None


def test_validate_codex_interface_missing_file_violation():
    with tempfile.TemporaryDirectory() as tmp:
        errs = asv._validate_codex_interface(Path(tmp))  # no agents/openai.yaml
        assert errs
        assert any("openai.yaml" in e for e in errs)


def test_validate_codex_interface_empty_display_name_violation():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        (skill_dir / "agents").mkdir()
        (skill_dir / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: ""\n', encoding="utf-8"
        )
        errs = asv._validate_codex_interface(skill_dir)
        assert errs
        assert any("display_name" in e for e in errs)


def test_validate_codex_interface_present_ok():
    with tempfile.TemporaryDirectory() as tmp:
        skill_dir = Path(tmp)
        (skill_dir / "agents").mkdir()
        (skill_dir / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "AQG Foo"\n', encoding="utf-8"
        )
        assert asv._validate_codex_interface(skill_dir) == []


def test_extract_display_name_regex_fallback(monkeypatch):
    """audit 57f41525: exercise the no-PyYAML regex fallback (the yaml path runs
    in this env, so force ImportError). The fallback must read an empty quoted
    value — even with a trailing comment — as empty, and strip an inline comment
    from a real value."""
    monkeypatch.setitem(sys.modules, "yaml", None)  # makes `import yaml` raise ImportError
    assert asv._extract_display_name('interface:\n  display_name: "AQG Foo"\n') == "AQG Foo"
    assert asv._extract_display_name('interface:\n  display_name: ""\n') is None
    assert asv._extract_display_name('interface:\n  display_name: "" # TODO\n') is None
    assert asv._extract_display_name('interface:\n  display_name: "AQG Foo" # note\n') == "AQG Foo"
    assert asv._extract_display_name('interface:\n  short_description: "no name here"\n') is None


# NB: the all-shipped-skills roster regression guard lives in
# tests/behavior/test_codex_display_name.py, NOT here — top-level tests/ is not
# collected by behavior-tests.yml (audit 57f41525 f2), so a CI-gating guard must
# live under tests/behavior/.


# ===== B5: description competing-context / pushiness advisory (#183) =====
#
# Anthropic skill-creator: a description should scope WHEN to use the skill, not
# claim universal priority. Advisory only (never a hard violation) — surfaced in
# result.warnings. The CI-gating shipped-roster calibration lives in
# tests/behavior/test_skill_description_pushiness.py.


def test_pushiness_flags_always_use():
    out = asv._check_description_pushiness("ALWAYS use this skill before editing.")
    assert out and "pushiness" in out[0].lower()


def test_pushiness_flags_superlative():
    assert asv._check_description_pushiness("The best tool for reviewing diffs.")
    assert asv._check_description_pushiness("This is the most powerful skill available.")


def test_pushiness_flags_hash_one_rank():
    """audit f93ffaab gpt-5.5 f2: `#1 skill/tool` must flag — a leading word
    boundary before `#` never matches, so the anchor had to change."""
    assert asv._check_description_pushiness("This is the #1 skill for reviews.")
    assert asv._check_description_pushiness("#1 tool you will ever need.")


def test_pushiness_flags_exclusivity():
    assert asv._check_description_pushiness("The only skill you need for reviews.")
    assert asv._check_description_pushiness("Use this; never use any other reviewer.")


def test_pushiness_flags_over_broad_scope():
    assert asv._check_description_pushiness("Run this for all tasks in any repo.")
    assert asv._check_description_pushiness("Use it for everything you do.")


def test_pushiness_flags_urgency():
    assert asv._check_description_pushiness("You must always run this; never skip it.")


def test_pushiness_scoped_proactively_is_clean():
    """The AQG house style ('Use PROACTIVELY when …' / 'Use before …') is scoped
    guidance, NOT pushiness — it must stay clean."""
    for clean in (
        "Verify worktree + GitHub live state at SESSION START. Use exactly once "
        "per session, not for repeated mid-session checks.",
        "Review code for OWASP Top 10. Use PROACTIVELY when writing authentication "
        "or handling user input.",
        "Decide accept / reject for each audit finding. Use AFTER a review returns.",
    ):
        assert asv._check_description_pushiness(clean) == [], clean


def test_pushiness_empty_description_is_clean():
    assert asv._check_description_pushiness("") == []
    assert asv._check_description_pushiness("   ") == []


def test_pushiness_is_single_advisory_listing_all_hits():
    """Multiple pushy phrases collapse into ONE advisory (no flooding) that names
    each matched category."""
    out = asv._check_description_pushiness(
        "The best skill — always use it for all tasks; never skip."
    )
    assert len(out) == 1, out
    assert "GUIDE §2.3b" in out[0]


def test_pushiness_wired_into_validate_skill_warnings():
    """End-to-end: a skill whose sidecar description is pushy surfaces the advisory
    in result.warnings (not as a hard violation). validate_skill imports the schema
    module from REPO/scripts, already on sys.path, so a temp repo_root works."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_fake_repo(tmp, skill_name="aqg-pushy-skill")
        skill_dir = root / "skills" / "aqg-pushy-skill"
        # _make_fake_repo already created skills/<name> with a managed sidecar
        # (PR-3 wrapper-generated clause), so tolerate the existing dir.
        skill_dir.mkdir(parents=True, exist_ok=True)
        sidecar = _make_valid_sidecar("aqg-pushy-skill")
        sidecar["description"] = (
            "ALWAYS use this skill for all code changes — the best reviewer."
        )
        (skill_dir / "skill.template.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        (skill_dir / "SKILL.md").write_text(
            f"---\nname: aqg-pushy-skill\ndescription: {sidecar['description']}\n---\n\n"
            "# X\n## Boundaries\nfoo\n",
            encoding="utf-8",
        )
        (skill_dir / "agents").mkdir()
        (skill_dir / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "AQG Pushy"\n', encoding="utf-8"
        )
        result = asv.validate_skill("aqg-pushy-skill", repo_root=root)
        assert any("pushiness" in w.lower() for w in result.warnings), result.warnings


# ===== output_shape drift detector (C6, Codex audit meta-fix) =====


def _write_drift_entry(tmp_path: Path, body: str) -> dict:
    """Write a python entry_script under tmp_path; return a minimal sidecar."""
    entry_rel = "skills/x/scripts/e.py"
    entry = tmp_path / entry_rel
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text(body, encoding="utf-8")
    return {"entry_script": entry_rel}


def test_output_shape_drift_flags_json_gated_by_flag(tmp_path: Path) -> None:
    sidecar = _write_drift_entry(
        tmp_path,
        "def main(args):\n    if args.json:\n        print('{}')\n    else:\n        print('# md')\n",
    )
    sidecar["output_shape"] = "json"
    warns = asv._check_output_shape_drift(tmp_path, sidecar)
    assert warns, "json output_shape with an args.json gate must warn"
    assert "output_shape" in warns[0]


def test_output_shape_drift_silent_when_not_json(tmp_path: Path) -> None:
    sidecar = _write_drift_entry(tmp_path, "def main(args):\n    print('# md')\n")
    sidecar["output_shape"] = "multi_section_report"
    assert asv._check_output_shape_drift(tmp_path, sidecar) == []


def test_output_shape_drift_silent_when_json_is_default(tmp_path: Path) -> None:
    sidecar = _write_drift_entry(
        tmp_path, "import json\ndef main(args):\n    print(json.dumps({}))\n"
    )
    sidecar["output_shape"] = "json"
    assert (
        asv._check_output_shape_drift(tmp_path, sidecar) == []
    ), "a true json-default script must not warn"


def test_load_entry_ast_silent_on_defensive_paths(tmp_path: Path) -> None:
    """audit 422a3e5f f4: _load_entry_ast must return None (never raise) on every
    defensive path — the silent contract that lets _check_output_shape_drift run
    inside validate_skill for every skill without crashing validation."""
    # non-.py entry
    assert asv._load_entry_ast(tmp_path, {"entry_script": "skills/x/e.sh"}) is None
    # out-of-tree (..) entry — containment guard
    assert asv._load_entry_ast(tmp_path, {"entry_script": "../../escape.py"}) is None
    # missing file
    assert asv._load_entry_ast(tmp_path, {"entry_script": "skills/x/missing.py"}) is None
    # invalid Python syntax
    bad = _write_drift_entry(tmp_path, "def (:\n")
    assert asv._load_entry_ast(tmp_path, bad) is None
    # oversized (> _MAX_ENTRY_SCAN_BYTES)
    big = _write_drift_entry(tmp_path, "#" + "x" * (1 << 20))
    assert asv._load_entry_ast(tmp_path, big) is None
    # and the public check stays silent ([]) for the oversized case too
    big["output_shape"] = "json"
    assert asv._check_output_shape_drift(tmp_path, big) == []


# ===== wrapper-sync (managed wrapper drift vs source) =====

# Minimal managed-skill source: valid frontmatter + body, ending in exactly one
# newline (build_wrapper asserts that). With no host_overrides the rendered
# wrapper is a verbatim copy, so an in-sync wrapper == this source text exactly.
_WRAPSYNC_SRC = "---\nname: aqg-wrapsync-fixture\ndescription: x\n---\n\n# Body\n\nA line.\n"


def _write_wrapsync_fixture(root: Path, wrapper_text: str, *, managed: bool = True) -> dict:
    """Scaffold skills/<name>/{SKILL.md, sidecar} + the committed wrapper under an
    agent-pack path. Returns the sidecar dict (same shape on disk) for the
    _check_wrapper_sync managed gate."""
    name = "aqg-wrapsync-fixture"
    src_dir = root / "skills" / name
    src_dir.mkdir(parents=True, exist_ok=True)
    (src_dir / "SKILL.md").write_text(_WRAPSYNC_SRC, encoding="utf-8")
    sidecar: dict = {"name": name}
    if managed:
        sidecar["wrapper_generated"] = True
    (src_dir / "skill.template.json").write_text(json.dumps(sidecar), encoding="utf-8")
    wdir = root / "agent-packs" / "claude-code" / "skills" / name
    wdir.mkdir(parents=True, exist_ok=True)
    (wdir / "SKILL.md").write_text(wrapper_text, encoding="utf-8")
    return sidecar


def test_wrapper_sync_clean_when_wrapper_matches_source(tmp_path: Path) -> None:
    # no host_overrides → rendered wrapper is a verbatim copy of the source
    sidecar = _write_wrapsync_fixture(tmp_path, _WRAPSYNC_SRC)
    assert asv._check_wrapper_sync(tmp_path, "aqg-wrapsync-fixture", sidecar) == []


def test_wrapper_sync_warns_on_drift(tmp_path: Path) -> None:
    sidecar = _write_wrapsync_fixture(tmp_path, _WRAPSYNC_SRC + "DRIFT\n")
    warns = asv._check_wrapper_sync(tmp_path, "aqg-wrapsync-fixture", sidecar)
    assert len(warns) == 1, warns
    assert "OUT OF SYNC" in warns[0] and "regen --all" in warns[0]


def test_wrapper_sync_skips_unmanaged_skill(tmp_path: Path) -> None:
    # unmanaged → no generated wrapper → skip even though the committed file differs
    sidecar = _write_wrapsync_fixture(tmp_path, _WRAPSYNC_SRC + "DRIFT\n", managed=False)
    assert asv._check_wrapper_sync(tmp_path, "aqg-wrapsync-fixture", sidecar) == []


def test_wrapper_sync_failure_surfaces_advisory_not_crash(tmp_path: Path, monkeypatch) -> None:
    # audit b00eb16e f1/f2 (convergent 3/3): a managed skill whose render/compare
    # raises ANY error (here a simulated unreadable-file OSError, which regen_one
    # does NOT wrap as RuntimeError) must degrade to one advisory line, never crash
    # — the validator runs over every skill and one bad skill can't abort the batch.
    sidecar = _write_wrapsync_fixture(tmp_path, _WRAPSYNC_SRC)  # managed
    import aqg_skill_gen

    def _boom(*_a, **_k):
        raise OSError("simulated unreadable wrapper")

    monkeypatch.setattr(aqg_skill_gen, "regen_one", _boom)
    warns = asv._check_wrapper_sync(tmp_path, "aqg-wrapsync-fixture", sidecar)
    assert len(warns) == 1, warns
    assert "could not verify wrapper" in warns[0]


def test_wrapper_sync_warning_wired_into_validate_skill() -> None:
    """End-to-end: a managed skill with a drifted wrapper surfaces the OUT OF SYNC
    advisory in result.warnings — so `--strict` (which fails on any warning) fails,
    matching the CI regen→git-status gate. Mirrors the pushiness wiring test."""
    with tempfile.TemporaryDirectory() as tmp:
        name = "aqg-wrapsync-wired"
        root = _make_fake_repo(tmp, skill_name=name)
        skill_dir = root / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        sidecar = _make_valid_sidecar(name)
        sidecar["wrapper_generated"] = True  # managed → wrapper-sync check runs
        (skill_dir / "skill.template.json").write_text(
            json.dumps(sidecar), encoding="utf-8"
        )
        # source SKILL.md: frontmatter matches sidecar; body ends in one newline
        source = (
            f"---\nname: {name}\ndescription: {sidecar['description']}\n---\n\n"
            "# X\n## Boundaries\nfoo\n"
        )
        (skill_dir / "SKILL.md").write_text(source, encoding="utf-8")
        (skill_dir / "agents").mkdir(exist_ok=True)
        (skill_dir / "agents" / "openai.yaml").write_text(
            'interface:\n  display_name: "AQG WrapSync"\n', encoding="utf-8"
        )
        # committed wrapper DRIFTS from the (verbatim, no host_overrides) render
        wdir = root / "agent-packs" / "claude-code" / "skills" / name
        wdir.mkdir(parents=True, exist_ok=True)
        (wdir / "SKILL.md").write_text(source + "DRIFT\n", encoding="utf-8")
        result = asv.validate_skill(name, repo_root=root)
        assert any("OUT OF SYNC" in w for w in result.warnings), result.warnings
