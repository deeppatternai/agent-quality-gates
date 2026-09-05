"""Tests for scripts/aqg_skill_gen.py — skill template generator (B-2)."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))

import aqg_skill_gen as gen  # noqa: E402
import aqg_skill_validator as asv  # noqa: E402


# ===== Helpers =====


def _make_valid_sidecar(skill_name: str = "aqg-gen-test") -> dict:
    """Sidecar that should pass schema check."""
    return {
        "skill_template_schema": 1,
        "name": skill_name,
        "description": (
            "Test skill from generator demonstrating skeleton emission "
            "for unit tests; runs no-op stub. Use for testing only."
        ),
        "boundary_class": "read-only",
        "trigger_grammar": {
            "keywords": ["test", "demo", "generator"],
            "sentence_patterns": ["Run the test", "Demo the generator"],
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


@pytest.fixture
def skill_cleanup():
    """Track created skills and remove them after test."""
    created: list[str] = []
    yield created
    for name in created:
        for path in (
            REPO / "skills" / name,
            REPO / "agent-packs" / "claude-code" / "skills" / name,
        ):
            if path.exists():
                shutil.rmtree(path)


# ===== generate_skill direct API tests =====


def test_dry_run_no_files_written(skill_cleanup):
    skill_cleanup.append("aqg-dryrun-test")
    sidecar = _make_valid_sidecar("aqg-dryrun-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO, dry_run=True)
    assert "skill_md" in outputs
    assert "self_test" in outputs
    assert "sidecar" in outputs
    assert "generated_md" in outputs
    assert "claude_wrapper" in outputs
    assert "entry_script" in outputs
    # No files actually written
    assert not (REPO / "skills" / "aqg-dryrun-test").exists()


def test_full_emit_creates_all_files(skill_cleanup):
    skill_cleanup.append("aqg-emit-test")
    sidecar = _make_valid_sidecar("aqg-emit-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    for role, path in outputs.items():
        assert path.is_file(), f"{role} not created: {path}"


def test_invalid_sidecar_raises(skill_cleanup):
    bad = _make_valid_sidecar()
    bad["boundary_class"] = "BOGUS"
    with pytest.raises(RuntimeError, match="schema invalid"):
        gen.generate_skill(bad, repo_root=REPO)


def test_no_overwrite_without_force(skill_cleanup):
    skill_cleanup.append("aqg-noforce-test")
    sidecar = _make_valid_sidecar("aqg-noforce-test")
    gen.generate_skill(sidecar, repo_root=REPO)
    # Second call should refuse
    with pytest.raises(FileExistsError):
        gen.generate_skill(sidecar, repo_root=REPO)


def test_force_overwrites(skill_cleanup):
    skill_cleanup.append("aqg-force-test")
    sidecar = _make_valid_sidecar("aqg-force-test")
    gen.generate_skill(sidecar, repo_root=REPO)
    # Mutate description, regen with --force
    sidecar["description"] = (
        "MUTATED test skill from generator demonstrating skeleton emission "
        "for unit tests; runs no-op stub. Use for testing only."
    )
    outputs = gen.generate_skill(sidecar, repo_root=REPO, force=True)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "MUTATED" in skill_md


# ===== Output content shape tests =====


def test_skill_md_frontmatter_only_name_description(skill_cleanup):
    skill_cleanup.append("aqg-fm-test")
    sidecar = _make_valid_sidecar("aqg-fm-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    parsed, errs = asv._parse_frontmatter(skill_md)
    assert not errs, errs
    # frontmatter ONLY has name + description
    assert set(parsed.keys()) == {"name", "description"}
    assert parsed["name"] == "aqg-fm-test"
    assert parsed["description"] == sidecar["description"]


def test_skill_md_has_boundary_h2(skill_cleanup):
    """Generated SKILL.md must contain the explicit Boundaries H2 (not loose)."""
    skill_cleanup.append("aqg-boundary-h2")
    sidecar = _make_valid_sidecar("aqg-boundary-h2")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "## Boundaries" in skill_md


def test_skill_md_sources_aqg_context_helper(skill_cleanup):
    """Generated SKILL.md How To Run must source _aqg_context.sh per ADR §3.5."""
    skill_cleanup.append("aqg-helper-source")
    sidecar = _make_valid_sidecar("aqg-helper-source")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "_aqg_context.sh" in skill_md
    assert "source" in skill_md


def test_entry_script_has_exit_code_docstring(skill_cleanup):
    """Entry script module docstring must document 0/1/2/3/70 contract."""
    skill_cleanup.append("aqg-exitcode-test")
    sidecar = _make_valid_sidecar("aqg-exitcode-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    entry = outputs["entry_script"].read_text(encoding="utf-8")
    # Module docstring contains exit-code block
    assert "Exit codes:" in entry
    for code in ("0:", "1:", "2:", "3:", "70:"):
        assert code in entry, f"missing exit code line {code!r}"


def test_sidecar_persisted(skill_cleanup):
    skill_cleanup.append("aqg-sidecar-persist")
    sidecar = _make_valid_sidecar("aqg-sidecar-persist")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    persisted = json.loads(outputs["sidecar"].read_text(encoding="utf-8"))
    assert persisted["name"] == "aqg-sidecar-persist"


def test_generated_md_has_seven_touchpoints(skill_cleanup):
    skill_cleanup.append("aqg-checklist-test")
    sidecar = _make_valid_sidecar("aqg-checklist-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    text = outputs["generated_md"].read_text(encoding="utf-8")
    # 7 numbered sections (trigger canary added as #6 — audit b9d506d2 f2)
    for n in range(1, 8):
        assert f"## {n}." in text, f"missing checklist item {n}"
    # the trigger-canary step must be present so a new-skill author isn't left to
    # discover it only via a born-red roster failure (audit b9d506d2 f2)
    assert "test_aqg_skill_trigger_canary.py" in text
    assert "SKILL_TRIGGER_KEYWORDS" in text


def test_claude_wrapper_minimal(skill_cleanup):
    skill_cleanup.append("aqg-wrapper-test")
    sidecar = _make_valid_sidecar("aqg-wrapper-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    wrapper = outputs["claude_wrapper"].read_text(encoding="utf-8")
    parsed, errs = asv._parse_frontmatter(wrapper)
    assert not errs
    assert set(parsed.keys()) == {"name", "description"}


def test_aqg_agent_gating_emits_section(skill_cleanup):
    skill_cleanup.append("aqg-gating-test")
    sidecar = _make_valid_sidecar("aqg-gating-test")
    sidecar["boundary_class"] = "writes-evidence"
    sidecar["writes_paths"] = ["docs/incidents/"]
    sidecar["aqg_agent_gating"] = True
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "AQG_AGENT" in skill_md
    assert "## AQG_AGENT gating" in skill_md


def test_no_gating_section_when_flag_false(skill_cleanup):
    skill_cleanup.append("aqg-nogating-test")
    sidecar = _make_valid_sidecar("aqg-nogating-test")
    sidecar["aqg_agent_gating"] = False
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "## AQG_AGENT gating" not in skill_md


def test_generated_skill_passes_validator_schema_layer(skill_cleanup):
    """Generated skill's sidecar persists correctly and re-validates."""
    skill_cleanup.append("aqg-roundtrip-test")
    sidecar = _make_valid_sidecar("aqg-roundtrip-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    # The persisted sidecar JSON must round-trip through schema check
    sys.path.insert(0, str(REPO / "scripts"))
    from _skill_template_schema import check_skill_template
    persisted = json.loads(outputs["sidecar"].read_text(encoding="utf-8"))
    result = check_skill_template(persisted)
    assert result.is_safe, result.violations


# ===== CLI integration =====


def test_cli_dry_run_exit_zero(skill_cleanup):
    skill_cleanup.append("aqg-cli-dryrun")
    sidecar = _make_valid_sidecar("aqg-cli-dryrun")
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(sidecar, tmp)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "aqg_skill_gen.py"),
             tmp_path, "--dry-run"],
            text=True, capture_output=True,
        )
        assert proc.returncode == gen.EXIT_OK, proc.stderr
        assert "DRY RUN" in proc.stdout
    finally:
        Path(tmp_path).unlink()


def test_cli_invalid_sidecar_returns_schema_fail(skill_cleanup):
    sidecar = _make_valid_sidecar()
    sidecar["boundary_class"] = "BOGUS"
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8") as tmp:
        json.dump(sidecar, tmp)
        tmp_path = tmp.name
    try:
        proc = subprocess.run(
            [sys.executable, str(REPO / "scripts" / "aqg_skill_gen.py"), tmp_path],
            text=True, capture_output=True,
        )
        assert proc.returncode == gen.EXIT_SCHEMA_OR_WRITE
    finally:
        Path(tmp_path).unlink()


def test_cli_missing_sidecar_returns_usage(skill_cleanup):
    proc = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "aqg_skill_gen.py"),
         "/nonexistent/path/sidecar.json"],
        text=True, capture_output=True,
    )
    assert proc.returncode == gen.EXIT_USAGE


# ===== a3 audit fixes (audit a9554660) =====


def test_stub_entry_script_fails_closed_until_implemented(skill_cleanup):
    """a3 audit #1: generated entry script must NOT report success while it is
    still a stub. Non-help invocation returns EXIT_INTERNAL=70 until the author
    flips _SKILL_IMPLEMENTED to True."""
    skill_cleanup.append("aqg-stub-fail")
    sidecar = _make_valid_sidecar("aqg-stub-fail")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    entry = outputs["entry_script"]
    # Run with --mode (a real invocation, not --help) — should fail closed
    proc = subprocess.run(
        [sys.executable, str(entry), "--mode", "test"],
        text=True, capture_output=True,
    )
    assert proc.returncode == 70, proc.stdout + proc.stderr
    assert "generator stub" in proc.stderr or "_SKILL_IMPLEMENTED" in proc.stderr


def test_stub_entry_script_help_still_works(skill_cleanup):
    """a3 audit #1: --help must still exit 0 (so self_test smoke passes)."""
    skill_cleanup.append("aqg-stub-help")
    sidecar = _make_valid_sidecar("aqg-stub-help")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    entry = outputs["entry_script"]
    proc = subprocess.run(
        [sys.executable, str(entry), "--help"],
        text=True, capture_output=True,
    )
    assert proc.returncode == 0


def test_generated_md_no_drift_hash(skill_cleanup):
    """PR-3 (spec 2026-05-31 §6/§7): GENERATED.md must NOT reference the retired
    drift-hash mechanism; it instructs wrapper_generated + regen instead."""
    skill_cleanup.append("aqg-recompute-test")
    sidecar = _make_valid_sidecar("aqg-recompute-test")
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    text = outputs["generated_md"].read_text(encoding="utf-8")
    # The retired drift-hash mechanism must be entirely gone from the checklist.
    assert "drift_hash" not in text
    assert "RECOMPUTE-AFTER-SKILL-MD-FINALIZED" not in text
    assert "test_drift.py" not in text
    # The overlay requirement replaces it: opt into wrapper generation + regen.
    assert "wrapper_generated" in text
    assert "regen" in text
    # audit 454077e6 f1 (convergent): the generated triggers.yaml sample must
    # emit the M2 hash fields as explicit null (not omit them) so a managed
    # skill carries the `managed ⟺ M2-null` marker check_fixture_mix I7 reads.
    assert "description_sha256_first8: null" in text
    assert "trigger_section_sha256_first8: null" in text


def test_repo_level_entry_script_must_exist(skill_cleanup):
    """a3 audit #3: repo-level entry_script must exist before generation;
    else generator fails with clear message."""
    sidecar = _make_valid_sidecar("aqg-repo-entry-bad")
    sidecar["entry_script"] = "scripts/nonexistent_repo_level.py"
    with pytest.raises(RuntimeError, match="does not exist"):
        gen.generate_skill(sidecar, repo_root=REPO)


def test_repo_level_entry_script_existing_ok(skill_cleanup):
    """a3 audit #3 happy path: repo-level entry_script that EXISTS proceeds."""
    skill_cleanup.append("aqg-repo-entry-ok")
    sidecar = _make_valid_sidecar("aqg-repo-entry-ok")
    # Use an existing repo-level script
    sidecar["entry_script"] = "scripts/aqg_skill_gen.py"
    outputs = gen.generate_skill(sidecar, repo_root=REPO)
    # entry_script not in outputs because path is at repo level
    assert "entry_script" not in outputs


def test_force_overwrite_atomic(skill_cleanup):
    """a3 audit #5: --force must use atomic temp+rename so partial state
    cannot leak. Verify regen produces complete content."""
    skill_cleanup.append("aqg-atomic-test")
    sidecar = _make_valid_sidecar("aqg-atomic-test")
    gen.generate_skill(sidecar, repo_root=REPO)
    # Mutate description, force regen
    sidecar["description"] = (
        "ATOMIC FORCE TEST: regenerated content must fully replace original "
        "with no partial state visible. Use for atomic-write test only."
    )
    outputs = gen.generate_skill(sidecar, repo_root=REPO, force=True)
    skill_md = outputs["skill_md"].read_text(encoding="utf-8")
    assert "ATOMIC FORCE TEST" in skill_md
    # After atomic write, no .tmp sidings should remain
    parent = outputs["skill_md"].parent
    leftover_tmp = list(parent.glob(".SKILL.md.*.tmp"))
    assert not leftover_tmp, f"leftover tmp files: {leftover_tmp}"


def test_e2e_generated_skill_passes_b1_validator_emit_layer(skill_cleanup):
    """a3 audit #4: end-to-end — generate skill, then run B-1 validator;
    sidecar + frontmatter + boundary + exit-code-doc layers must all PASS.
    Cross-cutting registration is expected to FAIL because the generated
    skill is not yet in install/doctor/triggers (that's GENERATED.md's job)."""
    skill_cleanup.append("aqg-e2e-test")
    sidecar = _make_valid_sidecar("aqg-e2e-test")
    gen.generate_skill(sidecar, repo_root=REPO)

    result = asv.validate_skill("aqg-e2e-test", repo_root=REPO)
    # Sidecar layer: PASS (the just-emitted sidecar is round-trip valid)
    assert not result.sidecar_violations, result.sidecar_violations
    # Skill dir layer: PASS (frontmatter + boundary H2 + exit-code in docstring)
    # The stub _SKILL_IMPLEMENTED=False does NOT affect docstring contract
    # (validator checks the docstring text, not runtime behavior)
    assert not result.skill_dir_violations, result.skill_dir_violations
    # Cross-cutting layer: EXPECTED FAIL because new skill is not registered
    assert result.cross_cutting_violations
    # is_valid is False overall (cross-cutting fail), which is by design
    assert not result.is_valid


# ===== regen subcommand (SKILL.md-from-source overlay, spec 2026-05-31 §4/§5) =====
#
# These exercise the regen helpers + CLI against the migrated pilot
# (aqg-startup-preflight) and synthetic temp repos. The legacy scaffold path
# (positional sidecar) is covered by the tests above and must stay green.


PILOT = "aqg-startup-preflight"


def test_regen_reproduces_committed_pilot_wrapper():
    """`regen` of the migrated pilot reproduces its committed wrapper byte-for-
    byte (spec §8 acceptance: byte-reproducible from source + sidecar)."""
    committed = (
        REPO / "agent-packs" / "claude-code" / "skills" / PILOT / "SKILL.md"
    ).read_text(encoding="utf-8")
    rendered = gen.render_wrapper_for_skill(REPO, PILOT)
    assert rendered == committed


def test_regen_check_all_pilot_clean():
    """regen --check --all reports no drift on the committed tree (the pilot
    wrapper is in sync with its source)."""
    rc = gen.main(["regen", "--check", "--all"])
    assert rc == gen.EXIT_OK


def test_all_real_skills_managed_after_overlay_migration():
    """managed_skill_names returns every migrated skill. With the overlay
    migration complete (final-3 batch), every real aqg-* skill carries
    wrapper_generated, so the managed set equals the on-disk roster. The
    unmanaged-exclusion path is covered synthetically by
    test_regen_unmanaged_skill_errors + test_is_managed_agrees_across_call_sites
    (no real legacy skill remains to assert against)."""
    managed = set(gen.managed_skill_names(REPO))
    assert PILOT in managed
    all_skills = {p.name for p in (REPO / "skills").glob("aqg-*") if p.is_dir()}
    assert managed == all_skills, f"unmanaged real skills remain: {sorted(all_skills - managed)}"


def test_regen_writes_wrapper_to_temp_repo():
    """regen_one (write mode) emits the wrapper into a synthetic managed repo."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_overlay_repo(tmp, managed=True)
        out = gen.regen_one(root, "aqg-ov-skill", check=False)
        assert out is None
        wrapper = (
            root / "agent-packs" / "claude-code" / "skills" / "aqg-ov-skill" / "SKILL.md"
        ).read_text(encoding="utf-8")
        # the body invoke 'OLD' anchor was swapped for 'NEW'
        assert "NEW-INVOKE" in wrapper
        assert "OLD-INVOKE" not in wrapper


def test_regen_check_nonzero_on_corrupted_wrapper():
    """regen --check returns non-zero + a diff when the committed wrapper has
    been hand-corrupted away from the regen output."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_overlay_repo(tmp, managed=True)
        gen.regen_one(root, "aqg-ov-skill", check=False)  # write the correct wrapper
        # Corrupt it.
        wrapper_path = (
            root / "agent-packs" / "claude-code" / "skills" / "aqg-ov-skill" / "SKILL.md"
        )
        wrapper_path.write_text("---\nname: aqg-ov-skill\ndescription: TAMPERED\n---\n\nx\n")
        diff = gen.regen_one(root, "aqg-ov-skill", check=True)
        assert diff is not None and "TAMPERED" not in diff or diff  # a diff was produced
        # And the CLI maps it to a non-zero exit.
        rc = _run_regen_in_root(root, ["regen", "--check", "aqg-ov-skill"])
        assert rc == gen.EXIT_SCHEMA_OR_WRITE


def test_regen_unmanaged_skill_errors():
    """regen of a non-managed skill is an error (spec §4: not managed → error)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_overlay_repo(tmp, managed=False)
        with pytest.raises(RuntimeError, match="not managed"):
            gen.render_wrapper_for_skill(root, "aqg-ov-skill")
        rc = _run_regen_in_root(root, ["regen", "aqg-ov-skill"])
        assert rc == gen.EXIT_SCHEMA_OR_WRITE


def test_regen_orphans_nonzero_on_sourceless_wrapper():
    """regen --check --orphans returns non-zero when a wrapper dir has no
    matching source dir (spec §5 round-2 gemini-f1)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_overlay_repo(tmp, managed=True)
        # Create an orphan wrapper dir with no skills/<name> source.
        orphan = root / "agent-packs" / "claude-code" / "skills" / "aqg-orphan"
        orphan.mkdir(parents=True)
        (orphan / "SKILL.md").write_text("---\nname: aqg-orphan\ndescription: d\n---\n\nx\n")
        assert "aqg-orphan" in gen.find_orphan_wrappers(root)
        rc = _run_regen_in_root(root, ["regen", "--check", "--orphans"])
        assert rc == gen.EXIT_SCHEMA_OR_WRITE


def test_regen_orphans_clean_when_all_have_sources():
    with tempfile.TemporaryDirectory() as tmp:
        root = _make_overlay_repo(tmp, managed=True)
        gen.regen_one(root, "aqg-ov-skill", check=False)
        assert gen.find_orphan_wrappers(root) == []
        rc = _run_regen_in_root(root, ["regen", "--check", "--orphans"])
        assert rc == gen.EXIT_OK


def test_is_managed_agrees_across_call_sites():
    """The ONE shared is_managed_skill helper is identical across regen,
    validator, and check_fixture_mix imports (spec §4 — no predicate drift)."""
    import _aqg_wrapper_overlay as ov
    import aqg_skill_validator as asv_mod
    import check_fixture_mix as cfm
    assert gen.is_managed_skill is ov.is_managed_skill
    assert asv_mod.is_managed_skill is ov.is_managed_skill
    assert cfm.is_managed_skill is ov.is_managed_skill
    # And it agrees on valid / malformed / zero-override sidecars.
    assert ov.is_managed_skill({"wrapper_generated": True}) is True
    assert ov.is_managed_skill({"wrapper_generated": True, "host_overrides": {"claude": []}}) is True
    assert ov.is_managed_skill({"wrapper_generated": 1}) is False
    assert ov.is_managed_skill("malformed") is False


# --- regen test helpers ---


def _make_overlay_repo(tmp: str, *, managed: bool) -> Path:
    """Minimal repo with one skill whose source has an 'OLD-INVOKE' body anchor.

    Has VERSION + scripts/ + templates/ so _find_repo_root resolves it.
    """
    root = Path(tmp)
    (root / "VERSION").write_text("0.0.0\n")
    (root / "scripts").mkdir()
    (root / "templates").mkdir()
    skill_dir = root / "skills" / "aqg-ov-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: aqg-ov-skill\ndescription: An overlay test skill.\n---\n\n"
        "# Overlay Skill\n\n## Workflow\n\nrun OLD-INVOKE here\n\n"
        "## Boundary Rules\n\n- read only\n",
        encoding="utf-8",
    )
    sidecar = {"name": "aqg-ov-skill"}
    if managed:
        sidecar["wrapper_generated"] = True
        sidecar["host_overrides"] = {
            "claude": [{"anchor": "OLD-INVOKE", "replacement": "NEW-INVOKE"}]
        }
    (skill_dir / "skill.template.json").write_text(json.dumps(sidecar), encoding="utf-8")
    return root


def _run_regen_in_root(root: Path, argv: list[str]) -> int:
    """Run gen.main(argv) with _find_repo_root pinned to `root` (regen_main calls
    _find_repo_root() with no args, which walks from the script dir, so we patch
    it for the duration of the call)."""
    import aqg_skill_gen as g
    orig = g._find_repo_root
    g._find_repo_root = lambda start=None: root
    try:
        return g.main(argv)
    finally:
        g._find_repo_root = orig
