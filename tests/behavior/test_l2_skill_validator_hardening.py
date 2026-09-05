"""L2 pre-launch hardening regression suite for aqg_skill_validator.py.

Pins workflow PR-D findings:
  D2  — doctor registration must be ast-structural (a commented-out / prose
        mention can't false-pass); shell install.sh matched after stripping `#`.
  D6  — the validator actually REQUIRES PyYAML (triggers/openai parsing); the
        module docstring now declares the dependency instead of claiming stdlib.
  D9  — schema-invalid sidecar must not flow an unvalidated entry_script into a
        file read: validate_skill halts on schema failure, and
        _check_exit_code_documentation has a containment + size-cap belt.
  D13 — a non-python entry's exit-code contract is read from the top-of-file
        comment block only, not the whole body.

Each bug-repro FAILS against the pre-fix source
(`git stash push -- scripts/aqg_skill_validator.py`). Lands under
tests/behavior/ so behavior-tests.yml runs it in PR CI.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _skill_template_schema  # noqa: E402,F401  (warm sys.modules: validate_skill re-imports it)
import aqg_skill_validator as v  # noqa: E402


def _valid_sidecar(name: str = "aqg-vx") -> dict:
    cases = [
        {"id": f"c{i}", "style": "description_based", "prompt": "do x",
         "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}
        for i in range(4)
    ] + [{"id": "c4", "style": "explicit_invocation", "prompt": "/x",
          "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}]
    return {
        "skill_template_schema": 1, "name": name,
        "description": "Use when validating the validator.", "boundary_class": "read-only",
        "trigger_grammar": {"keywords": ["test"], "sentence_patterns": ["use when testing"]},
        "entry_script": f"skills/{name}/scripts/run.py",
        "cli_contract": {"0": "ok", "1": "fail", "2": "usage", "3": "nf", "70": "internal"},
        "output_shape": "json", "reads_paths": ["src/data"], "writes_paths": [],
        "forbidden_paths": [], "aqg_agent_gating": True, "owner_only_actions": [],
        "cases": cases, "self_test_entrypoint": f"skills/{name}/scripts/self_test.py",
        "fixed_before_next_task_required_when_findings": True, "numeric_values_quoted": True,
    }


# --- D2: doctor registration is ast-structural ------------------------------


def test_d2_real_skill_is_registered():
    assert v._doctor_registers_skill(REPO / "scripts/aqg_doctor.py", "aqg-startup-preflight")


def test_d2_unknown_skill_not_registered():
    assert not v._doctor_registers_skill(REPO / "scripts/aqg_doctor.py", "aqg-not-a-real-skill")


def test_d2_commented_registration_rejected(tmp_path):
    doctor = tmp_path / "aqg_doctor.py"
    doctor.write_text(
        'CLAUDE_SKILL_NAMES = (\n  "aqg-real",\n  # "aqg-commented",\n)\n'
        'CODEX_SKILL_NAMES = (\n  "aqg-real",\n  # "aqg-commented",\n)\n',
        encoding="utf-8",
    )
    assert v._doctor_registers_skill(doctor, "aqg-real")
    assert not v._doctor_registers_skill(doctor, "aqg-commented")  # ast can't see a comment


def test_d2_must_be_in_both_tuples(tmp_path):
    doctor = tmp_path / "aqg_doctor.py"
    doctor.write_text('CLAUDE_SKILL_NAMES = ("aqg-x",)\nCODEX_SKILL_NAMES = ()\n', encoding="utf-8")
    assert not v._doctor_registers_skill(doctor, "aqg-x")  # only in CLAUDE, not CODEX


def test_d2_shell_strip_comments_rejects_commented(tmp_path):
    sh = tmp_path / "install.sh"
    sh.write_text('skills=(\n  "aqg-real"\n  # "aqg-commented"\n)\n', encoding="utf-8")
    assert v._file_contains_quoted_skill(sh, "aqg-real", strip_shell_comments=True)
    assert not v._file_contains_quoted_skill(sh, "aqg-commented", strip_shell_comments=True)


# --- D6: the module docstring declares the PyYAML dependency -----------------


def test_d6_docstring_declares_pyyaml_dependency():
    doc = v.__doc__ or ""
    assert "PyYAML" in doc and "dependency" in doc.lower()


# --- D9: containment + size-cap belt + schema-failure halt ------------------


def test_d9_belt_rejects_entry_outside_repo(tmp_path):
    (tmp_path / "skills").mkdir()
    errs = v._check_exit_code_documentation(tmp_path, {"entry_script": "../../../etc/passwd"})
    assert any("outside repo root" in e for e in errs), errs


def test_d9_belt_rejects_oversized_entry(tmp_path):
    sd = tmp_path / "skills" / "aqg-x" / "scripts"
    sd.mkdir(parents=True)
    big = sd / "big.py"
    big.write_bytes(b"x" * (2 * 1024 * 1024))  # 2 MiB > the 1 MiB cap; hardcoded
    # (not _MAX_ENTRY_SCAN_BYTES) so the pre-fix repro fails on BEHAVIOR (no cap →
    # reads the file), not because the constant is absent (audit f3).
    errs = v._check_exit_code_documentation(tmp_path, {"entry_script": "skills/aqg-x/scripts/big.py"})
    assert any("too large" in e for e in errs), errs


def test_d9_validate_skill_halts_on_schema_failure(tmp_path):
    # A schema-INVALID sidecar (absolute entry_script) must stop before the
    # exit-code check ever reads the file. Pre-fix, validate_skill continued and
    # read the absolute path, emitting an exit-code-contract violation for it.
    skill = "aqg-halt"
    sd = tmp_path / "skills" / skill
    (sd / "scripts").mkdir(parents=True)
    sidecar = _valid_sidecar(skill)
    sidecar["entry_script"] = "/etc/passwd"  # absolute → schema rejects
    (sd / "skill.template.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (sd / "SKILL.md").write_text(f"---\nname: {skill}\ndescription: x\n---\n", encoding="utf-8")
    result = v.validate_skill(skill, repo_root=tmp_path)
    assert not result.is_valid
    assert any("entry_script" in x for x in result.sidecar_violations)   # schema caught it
    # halt: never reached the exit-code documentation read of /etc/passwd
    assert not any("exit-code" in x for x in result.skill_dir_violations)


# --- D13: non-python exit-code contract is read from the top comment block ---


def test_d13_incidental_body_exit_code_no_longer_passes():
    body = ("#!/bin/bash\n# header only\ncase $x in\n  0: echo a;;\n  1: echo b;;\n"
            "  2: c;;\n  3: d;;\n  70: e;;\nesac\n")
    top = v._top_comment_block(body)
    assert "case" not in top and "0: echo" not in top
    assert not v._has_exit_code_block(top)   # top block carries no contract
    assert v._has_exit_code_block(body)      # whole-body scan WOULD false-pass (the bug)


def test_d13_legit_top_of_file_contract_passes():
    good = ("#!/bin/bash\n# Exit codes:\n# 0: ok\n# 1: fail\n# 2: usage\n"
            "# 3: cfg\n# 70: internal\nset -e\n")
    assert v._has_exit_code_block(v._top_comment_block(good))


# --- audit f1: a symlink-loop entry is a controlled violation, not a crash ---


def test_f1_symlink_loop_entry_returns_violation_not_crash(tmp_path):
    sd = tmp_path / "skills" / "aqg-x" / "scripts"
    sd.mkdir(parents=True)
    os.symlink("run.py", sd / "run.py")  # self-referential loop
    # pre-fix: resolve() raises RuntimeError, caught only for OSError → crash.
    errs = v._check_exit_code_documentation(tmp_path, {"entry_script": "skills/aqg-x/scripts/run.py"})
    assert any("cannot resolve" in e for e in errs), errs


# --- audit f2: a multi-paragraph top comment (with a blank) keeps the contract


def test_f2_multiparagraph_top_contract_passes():
    multi = ("#!/bin/bash\n# My Script\n#\n# Exit codes:\n\n# 0: ok\n# 1: fail\n"
             "# 2: usage\n# 3: cfg\n# 70: internal\n\nset -e\n")
    # pre-fix: the blank line after `# Exit codes:` truncated the block, dropping
    # the numbered list → false-fail.
    assert v._has_exit_code_block(v._top_comment_block(multi))


# --- audit f3: black-box tests through the public-ish functions ---------------


def test_f3_d2_cross_cutting_blackbox_rejects_comment_only_registration(tmp_path):
    # Skill mentioned ONLY in comments of doctor + both install.sh; the real
    # doctor tuples + install arrays are empty. Pre-fix the raw quote/path regex
    # false-passed the comment; the fix (doctor ast + shell comment strip) must
    # report it unregistered. Goes through _validate_cross_cutting so a revert of
    # the WIRING (not just a helper) is caught.
    skill = "aqg-cmt"
    (tmp_path / "scripts").mkdir()
    (tmp_path / "agent-packs" / "claude-code").mkdir(parents=True)
    (tmp_path / "tests" / "behavior" / "fixtures").mkdir(parents=True)
    (tmp_path / "scripts" / "aqg_doctor.py").write_text(
        f'# registered: "{skill}" and skills/{skill}/\n'
        "CLAUDE_SKILL_NAMES = ()\nCODEX_SKILL_NAMES = ()\n", encoding="utf-8")
    for p in (tmp_path / "scripts" / "install.sh",
              tmp_path / "agent-packs" / "claude-code" / "install.sh"):
        p.write_text(f'#!/bin/bash\n# includes "{skill}" at skills/{skill}/\nskills=()\n',
                     encoding="utf-8")
    (tmp_path / "tests" / "behavior" / "fixtures" / "triggers.yaml").write_text(
        "cases:\n" + "".join(
            f"  - id: c{i}\n    expected_skill: {skill}\n" for i in range(5)),
        encoding="utf-8")
    sidecar = _valid_sidecar(skill); sidecar["wrapper_generated"] = True
    errs = v._validate_cross_cutting(tmp_path, skill, sidecar=sidecar)
    assert any("aqg_doctor.py" in e for e in errs), errs
    assert any("scripts/install.sh" in e for e in errs), errs
    assert any("agent-packs/claude-code/install.sh" in e for e in errs), errs


def test_f3_d13_blackbox_incidental_body_through_check(tmp_path):
    # A non-.py entry whose ONLY exit-code lines live in a case body, not the top
    # comment. Pre-fix (whole-file scan) false-passed; the fix (top comment block
    # only) must report the contract missing. Through the public function, so a
    # revert to whole-file scanning is caught (the _top_comment_block unit test
    # alone would not).
    sd = tmp_path / "skills" / "aqg-x" / "scripts"
    sd.mkdir(parents=True)
    (sd / "run.sh").write_text(
        "#!/bin/bash\n# does stuff\ncase $x in\n  0: a;;\n  1: b;;\n  2: c;;\n"
        "  3: d;;\n  70: e;;\nesac\n", encoding="utf-8")
    errs = v._check_exit_code_documentation(tmp_path, {"entry_script": "skills/aqg-x/scripts/run.sh"})
    assert any("exit-code contract" in e for e in errs), errs


def test_f3_d6_install_sh_warns_on_missing_pyyaml():
    sh = (REPO / "scripts" / "install.sh").read_text(encoding="utf-8")
    # actionable fix now points at the SOT manifest (requirements.txt declares pyyaml)
    assert "import yaml" in sh and "requirements.txt" in sh


def test_f3_d6_no_pyyaml_error_is_actionable():
    import inspect
    src = inspect.getsource(v._validate_cross_cutting)
    assert "requirements.txt" in src  # actionable fix → SOT; pre-fix: generic "loader unavailable"


# --- verification-round f1: invalid-UTF-8 entry is a controlled violation -----


def test_vf1_invalid_utf8_entry_returns_violation_not_crash(tmp_path):
    # A schema-valid in-repo entry path (under skills/<name>/scripts/) can point
    # at a file with invalid UTF-8 bytes — the schema constrains the path, not
    # the target's encoding. read_text raises UnicodeDecodeError (a ValueError,
    # NOT an OSError); the belt must return a controlled violation, not crash
    # (same DoS class as the symlink-loop f1).
    sd = tmp_path / "skills" / "aqg-x" / "scripts"
    sd.mkdir(parents=True)
    (sd / "run.py").write_bytes(b"\xff\xfe not valid utf-8 \x80\x81\n")
    errs = v._check_exit_code_documentation(tmp_path, {"entry_script": "skills/aqg-x/scripts/run.py"})
    assert any("read/decode failed" in e for e in errs), errs
