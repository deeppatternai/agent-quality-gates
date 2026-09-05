"""L2 pre-launch hardening regression suite for _skill_template_schema.py.

Pins audit 32e2c571 (gpt-5.5 + gemini) findings D3/D4/D5/D7/D10/D11/D12
(workflow PR-D). Each bug-repro FAILS against the pre-fix source (stash-proven).
check_skill_template never raises — it returns a result with .is_safe + .violations.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _skill_template_schema as sch  # noqa: E402


def _valid(name: str = "aqg-test") -> dict:
    """A minimal sidecar record that passes check_skill_template cleanly."""
    cases = [
        {"id": f"c{i}", "style": "description_based", "prompt": "do x",
         "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}
        for i in range(4)
    ] + [
        {"id": "c4", "style": "explicit_invocation", "prompt": "/x",
         "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}
    ]
    return {
        "skill_template_schema": 1,
        "name": name,
        "description": "Use when testing the schema.",
        "boundary_class": "read-only",
        "trigger_grammar": {"keywords": ["test"], "sentence_patterns": ["use when testing"]},
        "entry_script": f"skills/{name}/scripts/run.py",
        "cli_contract": {"0": "ok", "1": "fail", "2": "usage", "3": "not-found", "70": "internal"},
        "output_shape": "json",
        "reads_paths": ["src/data"],
        "writes_paths": [],
        "forbidden_paths": [],
        "aqg_agent_gating": True,
        "owner_only_actions": [],
        "cases": cases,
        "self_test_entrypoint": f"skills/{name}/scripts/self_test.py",
        "fixed_before_next_task_required_when_findings": True,
        "numeric_values_quoted": True,
    }


def _violations(rec: dict) -> tuple[str, ...]:
    return sch.check_skill_template(rec).violations


def test_valid_record_is_safe():
    assert sch.check_skill_template(_valid()).is_safe, _violations(_valid())


# --- D3: whitespace-only strings must not pass "non-empty" gates --------------


def test_d3_blank_description_rejected():
    rec = _valid(); rec["description"] = "   "
    assert any("description" in v for v in _violations(rec))


def test_d3_blank_path_list_item_rejected():
    rec = _valid(); rec["reads_paths"] = [" "]
    assert any("reads_paths[0]" in v for v in _violations(rec))


def test_d3_blank_cli_contract_value_rejected():
    rec = _valid(); rec["cli_contract"]["1"] = "  "
    assert any("cli_contract" in v for v in _violations(rec))


def test_d3_blank_trigger_keyword_rejected():
    rec = _valid(); rec["trigger_grammar"]["keywords"] = [" "]
    assert any("keywords[0]" in v for v in _violations(rec))


# --- D4: $ matched before a trailing newline; \Z does not ---------------------


def test_d4_name_trailing_newline_rejected():
    rec = _valid(); rec["name"] = "aqg-test\n"
    assert any("name" in v for v in _violations(rec))


def test_d4_case_id_trailing_newline_rejected():
    rec = _valid(); rec["cases"][0]["id"] = "c0\n"
    assert any("cases[0].id" in v for v in _violations(rec))


# --- D5: control chars / NUL / drive letter in a path -------------------------


def test_d5_nul_in_path_rejected():
    rec = _valid(); rec["reads_paths"] = ["src/\x00etc"]
    assert any("reads_paths[0]" in v and "control" in v for v in _violations(rec))


def test_d5_newline_in_path_rejected():
    rec = _valid(); rec["writes_paths"] = ["a/b\nc"]
    assert any("writes_paths[0]" in v and "control" in v for v in _violations(rec))


def test_d5_drive_letter_rejected():
    rec = _valid(); rec["reads_paths"] = ["C:/windows"]
    assert any("reads_paths[0]" in v for v in _violations(rec))


# --- D7: a skills/-rooted ref must belong to THIS skill -----------------------


def test_d7_entry_script_other_skill_rejected():
    rec = _valid(); rec["entry_script"] = "skills/aqg-other/scripts/run.py"
    assert any("entry_script" in v for v in _violations(rec))


def test_d7_self_test_other_skill_rejected():
    rec = _valid(); rec["self_test_entrypoint"] = "skills/aqg-other/scripts/self_test.py"
    assert any("self_test_entrypoint" in v for v in _violations(rec))


def test_d7_skill_file_ref_other_skill_rejected():
    rec = _valid(); rec["cases"][0]["skill_file_ref"] = "skills/aqg-other/SKILL.md"
    assert any("skill_file_ref" in v for v in _violations(rec))


def test_d7_top_level_scripts_entry_still_allowed():
    # a shared top-level scripts/ entry stays valid (only the skills/ branch is pinned)
    rec = _valid(); rec["entry_script"] = "scripts/shared.py"
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


# --- D10: non-str enum value must be a clean violation, not a TypeError --------


def test_d10_list_boundary_class_clean_violation():
    rec = _valid(); rec["boundary_class"] = ["read-only"]
    # must NOT raise (unhashable list in `not in frozenset`); clean violation instead
    assert any("boundary_class" in v for v in _violations(rec))


def test_d10_dict_output_shape_clean_violation():
    rec = _valid(); rec["output_shape"] = {"x": 1}
    assert any("output_shape" in v for v in _violations(rec))


def test_d10_list_case_style_clean_violation():
    rec = _valid(); rec["cases"][0]["style"] = []
    assert any("cases[0].style" in v for v in _violations(rec))


# --- D11: non-str case id + expected_skill must equal the sidecar name --------


def test_d11_non_str_case_id_rejected():
    rec = _valid(); rec["cases"][0]["id"] = 123
    assert any("cases[0].id" in v for v in _violations(rec))


def test_d11_expected_skill_mismatch_rejected():
    rec = _valid(); rec["cases"][0]["expected_skill"] = "aqg-other"
    assert any("cases[0].expected_skill" in v for v in _violations(rec))


# --- D12: forbidden_paths overlap must normalize trailing-slash ---------------


def test_d12_trailing_slash_overlap_detected():
    rec = _valid()
    rec["reads_paths"] = ["src/"]
    rec["forbidden_paths"] = ["src"]  # same path, different spelling
    assert any("forbidden_paths overlaps reads_paths" in v for v in _violations(rec))


# ===========================================================================
# Round-2 VERIFICATION audit (1f050d3e, gpt-5.5 + gemini, convergent) — the
# round-1 fix left present-null fields and non-canonical path spellings open.
# ===========================================================================


# --- present-null required fields (gpt fix-review #1/#3) ----------------------


def test_null_required_top_level_field_rejected():
    rec = _valid(); rec["name"] = None
    assert any("name" in v and "null" in v for v in _violations(rec))


def test_null_required_boundary_class_rejected():
    rec = _valid(); rec["boundary_class"] = None
    assert any("boundary_class" in v and "null" in v for v in _violations(rec))


def test_null_required_case_field_rejected():
    rec = _valid(); rec["cases"][0]["id"] = None
    assert any("cases[0].id" in v and "null" in v for v in _violations(rec))


# --- non-canonical path spellings must not dodge D7 / D12 (convergent) --------


def test_dotslash_skill_file_ref_other_skill_rejected():
    rec = _valid(); rec["cases"][0]["skill_file_ref"] = "./skills/aqg-other/SKILL.md"
    assert any("skill_file_ref" in v for v in _violations(rec))


def test_dotslash_entry_script_other_skill_rejected():
    rec = _valid(); rec["entry_script"] = "./skills/aqg-other/scripts/run.py"
    assert any("entry_script" in v for v in _violations(rec))


def test_dotslash_top_level_scripts_entry_allowed():
    rec = _valid(); rec["entry_script"] = "./scripts/shared.py"
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


def test_d12_double_slash_overlap_detected():
    rec = _valid()
    rec["reads_paths"] = ["a//b"]
    rec["forbidden_paths"] = ["a/b"]  # canonical-equal to a//b
    assert any("forbidden_paths overlaps reads_paths" in v for v in _violations(rec))


def test_d12_interior_dot_overlap_detected():
    rec = _valid()
    rec["writes_paths"] = ["a/./b"]
    rec["boundary_class"] = "writes-code"  # writes non-empty needs non-read-only
    rec["forbidden_paths"] = ["a/b"]
    assert any("forbidden_paths overlaps writes_paths" in v for v in _violations(rec))


# --- forbidden DIRECTORY must preclude a read/write under it (round-2 verify) -


def test_d12_forbidden_ancestor_of_read_detected():
    rec = _valid()
    rec["forbidden_paths"] = ["secrets/"]
    rec["reads_paths"] = ["secrets/prod.env"]  # under a forbidden dir
    assert any("forbidden_paths overlaps reads_paths" in v for v in _violations(rec))


def test_d12_forbidden_ancestor_of_write_detected():
    rec = _valid()
    rec["boundary_class"] = "writes-code"
    rec["forbidden_paths"] = ["production"]
    rec["writes_paths"] = ["production/db/x"]
    assert any("forbidden_paths overlaps writes_paths" in v for v in _violations(rec))


def test_d12_sibling_prefix_not_false_overlap():
    # 's' must NOT be treated as an ancestor of 'src' (trailing-/ boundary).
    rec = _valid()
    rec["forbidden_paths"] = ["s"]
    rec["reads_paths"] = ["src"]
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


# ===========================================================================
# PR-D3-r2 (#221) verify-round — gpt-5.5 739c55dd found these in the merged
# PR-D3 (#218), whose modify→verify round was skipped. Stash-proven RED
# against pre-fix scripts/_skill_template_schema.py.
# ===========================================================================


# --- f2: exec-path fields reject shell metacharacters -------------------------
#
# entry_script / self_test_entrypoint / case.skill_file_ref are interpolated
# into the generated How-To-Run bash (`python3 "$aqg_root/<entry_script>"`).
# _rel_path_violation blocks traversal but not a '"' / '$' / '`' / ';' / space
# that closes the double-quote and appends a command.
SHELL_INJECTION_PATHS = [
    'skills/aqg-test/scripts/a";rm -rf ~ #.py',  # closes the quote, appends cmd
    "skills/aqg-test/scripts/$(whoami).py",       # command substitution
    "skills/aqg-test/scripts/`id`.py",            # backtick substitution
    "skills/aqg-test/scripts/a b.py",             # space
    "skills/aqg-test/scripts/a;ls.py",            # statement separator
]


@pytest.mark.parametrize("evil", SHELL_INJECTION_PATHS)
def test_f2_entry_script_shell_metachar_rejected(evil):
    rec = _valid(); rec["entry_script"] = evil
    assert any("entry_script" in v for v in _violations(rec)), evil


@pytest.mark.parametrize("evil", SHELL_INJECTION_PATHS)
def test_f2_self_test_entrypoint_shell_metachar_rejected(evil):
    rec = _valid(); rec["self_test_entrypoint"] = evil
    assert any("self_test_entrypoint" in v for v in _violations(rec)), evil


@pytest.mark.parametrize("evil", SHELL_INJECTION_PATHS)
def test_f2_skill_file_ref_shell_metachar_rejected(evil):
    rec = _valid(); rec["cases"][0]["skill_file_ref"] = evil
    assert any("cases[0].skill_file_ref" in v for v in _violations(rec)), evil


def test_f2_clean_exec_paths_still_allowed():
    # The strict charset must still accept ordinary skill/script paths.
    rec = _valid()
    rec["entry_script"] = "skills/aqg-test/scripts/run.py"
    rec["self_test_entrypoint"] = "scripts/shared_self_test.py"
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


def test_f2_reads_paths_keeps_documentation_env_path():
    # OVER-TIGHTENING GUARD: reads_paths / writes_paths / forbidden_paths are
    # DOCUMENTATION paths and must NOT inherit the strict exec-path charset — a
    # shipped sidecar (aqg-project-status) legitimately reads an env-expanded
    # placeholder path. Tightening the shared _rel_path_violation would break
    # `regen --check` for it.
    rec = _valid()
    rec["reads_paths"] = [
        "${XDG_DATA_HOME:-$HOME/.aqg}/aqg/ledger/<project_id>/events.jsonl"
    ]
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


# --- f1: lone UTF-16 surrogates are rejected (utf-8 cannot encode them) --------
#
# A lone surrogate is legal in JSON (decodes to a Python str) and passed every
# pre-fix gate, then crashed the generator's utf-8 write. chr(0xD800) keeps this
# source pure ASCII.
LONE_SURROGATE = chr(0xD800)


def test_f1_surrogate_in_description_rejected():
    rec = _valid(); rec["description"] = "Use when " + LONE_SURROGATE + " testing."
    assert any("surrogate" in v.lower() for v in _violations(rec))


def test_f1_surrogate_in_keyword_rejected():
    rec = _valid()
    rec["trigger_grammar"]["keywords"] = ["te" + LONE_SURROGATE + "st"]
    assert any("surrogate" in v.lower() for v in _violations(rec))


def test_f1_surrogate_in_entry_script_rejected():
    rec = _valid()
    rec["entry_script"] = "skills/aqg-test/scripts/" + LONE_SURROGATE + ".py"
    assert any("surrogate" in v.lower() for v in _violations(rec))


def test_f1_surrogate_in_nested_cli_contract_value_rejected():
    # The recursive scan must reach values nested inside dicts, not just the
    # top-level string fields.
    rec = _valid(); rec["cli_contract"]["0"] = "ok " + LONE_SURROGATE
    assert any("surrogate" in v.lower() for v in _violations(rec))


def test_f1_astral_non_surrogate_still_allowed():
    # A real astral char (emoji / 𝕏) is fine — json.loads already folded its
    # surrogate PAIR into one code point, so it carries NO surrogate and utf-8
    # encodes it cleanly. The scan must reject LONE surrogates, not astral text.
    rec = _valid()
    rec["description"] = "Run " + chr(0x1F600) + " when astral " + chr(0x1D54F) + " ready."
    assert sch.check_skill_template(rec).is_safe, _violations(rec)


def test_f1_surrogate_object_key_rejected_with_encodable_message():
    # Round-2 verify (gemini BLOCKING, second-order): a surrogate in a mapping
    # KEY must be rejected AND must never leak verbatim into the violation's path
    # prefix. The validator / generator print violations to stderr, where a raw
    # surrogate is a FRESH UnicodeEncodeError — the exact crash class this scan
    # exists to prevent. host_overrides is an optional mapping field, so a
    # surrogate key + value exercises the recursive walk's key path.
    rec = _valid()
    rec["host_overrides"] = {LONE_SURROGATE: LONE_SURROGATE}
    vios = _violations(rec)
    assert any("surrogate" in v.lower() for v in vios)
    for v in vios:
        v.encode("utf-8", "strict")  # pre-revision: raw surrogate in path prefix -> crash


def test_f1_surrogate_in_name_pin_messages_encodable():
    # Round-2 verify (gpt-5.5 BLOCKING, residual): a surrogate in `name` is
    # reported safely (via !r) by the name-regex check, but is STILL interpolated
    # RAW into the same-skill pin diagnostics — skills/{name}/ for entry_script /
    # self_test_entrypoint and skills/{expected_name}/ for case.skill_file_ref.
    # The final _surrogate_safe pass must make ALL such violations encodable.
    rec = _valid()
    rec["name"] = LONE_SURROGATE
    rec["entry_script"] = "skills/aqg-other/scripts/run.py"
    rec["self_test_entrypoint"] = "skills/aqg-other/scripts/self_test.py"
    for c in rec["cases"]:
        c["expected_skill"] = LONE_SURROGATE  # == name, so the pin check is reached
        c["skill_file_ref"] = "skills/aqg-other/SKILL.md"  # cross-skill -> pin fails
    vios = _violations(rec)
    assert any("surrogate" in v.lower() for v in vios)
    for v in vios:
        v.encode("utf-8", "strict")  # pre-final-pass: pin messages carried raw `name`
