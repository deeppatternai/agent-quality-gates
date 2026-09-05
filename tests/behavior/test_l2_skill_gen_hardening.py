"""L2 pre-launch hardening regression suite for aqg_skill_gen.py.

Pins workflow PR-D findings:
  D1     — injection-proof generated artifacts (frontmatter scalar + python
           docstring / string-literal escaping)
  D7-gen — generator containment belt (every output path resolves in repo_root)
  D8     — single-source cross-cutting touchpoint count (no stale hardcoded '6')
  D14    — deeply-nested entry_script gets its parent dirs created

Each bug-repro FAILS against the pre-fix source (stash-proven:
`git stash push -- scripts/aqg_skill_gen.py`). Tests land under tests/behavior/
so behavior-tests.yml runs them in PR CI.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

import _skill_template_schema  # noqa: E402,F401  (warm sys.modules: generate_skill re-imports it)
import aqg_skill_gen as gen  # noqa: E402


def _sidecar(name: str = "aqg-genx") -> dict:
    """A minimal sidecar that passes check_skill_template cleanly."""
    cases = [
        {"id": f"c{i}", "style": "description_based", "prompt": "do x",
         "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}
        for i in range(4)
    ] + [{"id": "c4", "style": "explicit_invocation", "prompt": "/x",
          "expected_skill": name, "skill_file_ref": f"skills/{name}/SKILL.md"}]
    return {
        "skill_template_schema": 1, "name": name,
        "description": "Use when testing.", "boundary_class": "read-only",
        "trigger_grammar": {"keywords": ["test"], "sentence_patterns": ["use when testing"]},
        "entry_script": f"skills/{name}/scripts/run.py",
        "cli_contract": {"0": "ok", "1": "fail", "2": "usage", "3": "nf", "70": "internal"},
        "output_shape": "json", "reads_paths": ["src/data"], "writes_paths": [],
        "forbidden_paths": [], "aqg_agent_gating": True, "owner_only_actions": [],
        "cases": cases, "self_test_entrypoint": f"skills/{name}/scripts/self_test.py",
        "fixed_before_next_task_required_when_findings": True, "numeric_values_quoted": True,
    }


def _frontmatter(md: str) -> dict:
    return yaml.safe_load(md.split("---\n", 2)[1])


# --- D1: injection-proof frontmatter + generated python --------------------

# A description that breaks a naive `description: {value}` frontmatter (interior
# colon + newline) AND a naive `"""{value}"""` docstring (the triple-quote run).
EVIL = 'Use when: testing """ injection\nwith colons: and "quotes"'


def test_d1_skill_md_frontmatter_roundtrips_evil_description():
    s = _sidecar(); s["description"] = EVIL
    fm = _frontmatter(gen._render_skill_md(s))
    assert fm["description"] == EVIL
    assert set(fm.keys()) == {"name", "description"}  # no injected second key


def test_d1_wrapper_frontmatter_roundtrips_evil_description():
    s = _sidecar(); s["description"] = EVIL
    fm = _frontmatter(gen._render_claude_wrapper_skill_md(s))
    assert fm["description"] == EVIL


def test_d1_entry_script_compiles_with_evil_description_and_cli_contract():
    s = _sidecar(); s["description"] = EVIL
    s["cli_contract"]["0"] = 'ok """ closes the docstring\nand a newline'
    compile(gen._render_entry_script(s), "<entry>", "exec")  # SyntaxError if """ leaked


def test_d1_self_test_compiles_with_quote_in_entry_basename():
    # A double-quote is schema-valid in a path (it is not a control char / drive
    # / .. / leading slash), and the entry stem flows into a python string
    # literal in self_test — json.dumps must guard it.
    s = _sidecar()
    s["entry_script"] = 'skills/aqg-genx/scripts/ru"n.py'
    compile(gen._render_self_test(s), "<self_test>", "exec")


def test_d1_generated_md_case_block_is_valid_yaml():
    s = _sidecar(); s["cases"][0]["prompt"] = 'p "quoted: thing" x'
    block = re.search(r"```yaml\n(.*?)```", gen._render_generated_md(s), re.S).group(1)
    cases = yaml.safe_load(block)
    assert cases[0]["prompt"] == 'p "quoted: thing" x'


# --- D7-gen: generator containment belt ------------------------------------


def test_d7_belt_rejects_path_outside_repo(tmp_path):
    outside = tmp_path.parent / "evil_outside" / "x.py"
    with pytest.raises(RuntimeError, match="outside repo root"):
        gen._assert_outputs_within_repo({"entry_script": outside}, tmp_path)


def test_d7_belt_allows_path_inside_repo(tmp_path):
    inside = tmp_path / "skills" / "aqg-x" / "SKILL.md"
    gen._assert_outputs_within_repo({"skill_md": inside}, tmp_path)  # must not raise


# --- D8: single-source cross-cutting touchpoint count ----------------------


def test_d8_touchpoint_constant_matches_rendered_sections():
    md = gen._render_generated_md(_sidecar())
    section_nums = [int(n) for n in re.findall(r"^## (\d+)\.", md, re.M)]
    assert section_nums == list(range(1, gen.CROSS_CUTTING_TOUCHPOINTS + 1))


def test_d8_generated_md_heading_uses_constant():
    md = gen._render_generated_md(_sidecar())
    assert f"the {gen.CROSS_CUTTING_TOUCHPOINTS} surgical edits" in md


# --- D14: nested entry_script parent-dir creation --------------------------


def test_d14_nested_entry_script_is_written(tmp_path):
    s = _sidecar("aqg-deep")
    s["entry_script"] = "skills/aqg-deep/scripts/sub/deep/run.py"
    outs = gen.generate_skill(s, repo_root=tmp_path, force=False)
    entry = outs["entry_script"]
    assert entry.is_file()  # pre-fix: FileNotFoundError (only top-level scripts/ made)
    assert entry == tmp_path / "skills/aqg-deep/scripts/sub/deep/run.py"
    compile(entry.read_text(encoding="utf-8"), str(entry), "exec")


# --- audit f1: non-BMP unicode round-trips (no surrogate-pair breakage) -----

# ensure_ascii=True would emit `😀` as a JSON surrogate PAIR (😀);
# yaml.safe_load then keeps two surrogate code points (frontmatter != sidecar)
# and a generated python literal carrying surrogates raises UnicodeEncodeError
# when argparse prints --help. _quoted_scalar uses ensure_ascii=False.
NON_BMP = 'Run 😀 when astral 𝕏: "go"'


def test_f1_non_bmp_description_roundtrips_frontmatter():
    s = _sidecar(); s["description"] = NON_BMP
    assert _frontmatter(gen._render_skill_md(s))["description"] == NON_BMP


def test_f1_non_bmp_entry_script_source_is_clean_utf8():
    s = _sidecar(); s["description"] = NON_BMP
    src = gen._render_entry_script(s)
    compile(src, "<entry>", "exec")
    src.encode("utf-8", "strict")  # pre-fix: surrogates leaked -> UnicodeEncodeError


def test_f1_non_bmp_openai_yaml_roundtrips():
    s = _sidecar(); s["description"] = NON_BMP
    iface = yaml.safe_load(gen._render_openai_yaml(s))["interface"]
    # short_description is seeded from the description's first sentence (no '.'
    # here -> the whole string); pre-fix surrogate pairs make it != NON_BMP.
    assert iface["short_description"] == NON_BMP


# --- audit f3: belt is wired into generate_skill + D8 CLI line uses constant -


def test_f3_belt_is_invoked_by_generate_skill(tmp_path, monkeypatch):
    # Bypass the schema gate to prove generate_skill ITSELF blocks an escaping
    # output path — a future regression deleting the belt call (while leaving the
    # helper) must fail here, not only in the direct-helper unit tests. dry_run
    # reaches the belt (it runs before the dry_run return) without writing files.
    import _skill_template_schema as sch
    safe = type("R", (), {"is_safe": True, "violations": []})()
    monkeypatch.setattr(sch, "check_skill_template", lambda rec: safe)
    s = _sidecar()
    s["name"] = "../../evil"  # skill_dir resolves to repo_root.parent/evil
    s["entry_script"] = "skills/../../evil/scripts/run.py"  # paired so it hits the belt
    with pytest.raises(RuntimeError, match="outside repo root"):
        gen.generate_skill(s, repo_root=tmp_path, force=False, dry_run=True)


def test_f3_cli_next_steps_uses_constant_not_hardcoded_digit():
    import inspect
    src = inspect.getsource(gen.main)
    assert "CROSS_CUTTING_TOUCHPOINTS" in src  # pre-fix: constant absent
    assert "Apply the 6 " not in src and "Apply the 7 " not in src


# ===========================================================================
# PR-D3-r2 (#221) verify-round — the modify→verify pass #218 skipped found a
# GENERATED.md YAML-typing gap (r2-f3), a generation crash on lone surrogates
# (r2-f1), and a missing main() encoding belt. Stash-proven RED against the
# pre-fix scripts/aqg_skill_gen.py (+ scripts/_skill_template_schema.py for the
# end-to-end surrogate case).
# ===========================================================================


# --- r2-f3: GENERATED.md YAML scalars are quoted (no implicit typing) ----------


def _case_block(s: dict) -> list:
    block = re.search(r"```yaml\n(.*?)```", gen._render_generated_md(s), re.S).group(1)
    return yaml.safe_load(block)


def test_f3_generated_md_yaml_implicit_id_stays_string():
    # `on` (YAML 1.1 bool) and `123` (int) are schema-VALID case ids (CASE_ID_RE
    # allows them), so the generator MUST quote them — else the copy-paste-ready
    # triggers.yaml block parses id as bool / int instead of the intended string.
    s = _sidecar()
    s["cases"][0]["id"] = "on"
    s["cases"][1]["id"] = "123"
    cases = _case_block(s)
    assert all(isinstance(c["id"], str) for c in cases)  # pre-fix: bool / int leak
    ids = [c["id"] for c in cases]
    assert "on" in ids and "123" in ids


def test_f3_generated_md_sha_fields_stay_null():
    # The two *_sha256_first8 fields must stay YAML null — check_fixture_mix.py
    # I7 reads them as null for the managed⟺M2-null invariant; quoting the other
    # scalars must not touch them.
    cases = _case_block(_sidecar())
    assert all(c["description_sha256_first8"] is None for c in cases)
    assert all(c["trigger_section_sha256_first8"] is None for c in cases)


# --- r2-f1: lone surrogate is a clean schema error, not a generation crash -----


def test_f1_surrogate_sidecar_is_clean_schema_error_not_crash(tmp_path):
    # End-to-end: a surrogate description now fails the schema (RuntimeError), so
    # generate_skill never reaches the utf-8 write that pre-fix crashed with
    # UnicodeEncodeError (which is NOT a RuntimeError, so pytest.raises errors).
    s = _sidecar()
    s["description"] = "Use " + chr(0xD800) + " when testing."
    with pytest.raises(RuntimeError):
        gen.generate_skill(s, repo_root=tmp_path, force=False, dry_run=False)


def test_f1_main_belts_unicode_error(tmp_path, monkeypatch, capsys):
    # Defense-in-depth: if a surrogate ever bypasses the schema and reaches the
    # write, main() must return a clean exit code, not propagate UnicodeError.
    # Force generate_skill to raise it; pre-fix main has no UnicodeError branch,
    # so the exception escapes main() and the test errors instead of asserting.
    sc = tmp_path / "s.json"
    sc.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(gen, "_load_sidecar", lambda p: {"name": "aqg-x"})
    monkeypatch.setattr(gen, "_find_repo_root", lambda: tmp_path)

    def _boom(*a, **k):
        raise UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(gen, "generate_skill", _boom)
    rc = gen.main([str(sc)])
    assert rc == gen.EXIT_SCHEMA_OR_WRITE
    assert "encoding" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("ch,label", [
    (chr(0x2028), "U+2028-line-sep"),
    (chr(0x2029), "U+2029-para-sep"),
    (chr(0x00), "NUL"),
    (chr(0xFEFF), "BOM"),
])
def test_f1_nonsurrogate_edge_chars_roundtrip_no_crash(ch, label):
    # f4: U+2028 / U+2029 / NUL / BOM are valid UTF-8 (_quoted_scalar escapes NUL
    # to \\u0000 and emits the separators raw), so they round-trip through the
    # frontmatter without the utf-8 write crash that ONLY lone surrogates cause.
    # They are intentionally NOT schema-rejected (no crash, no type confusion);
    # this pins that disposition so a future "reject all control chars" change
    # that would break host_overrides' legitimate multi-line text is caught.
    s = _sidecar(); s["description"] = "a" + ch + "b"
    md = gen._render_skill_md(s)
    md.encode("utf-8", "strict")  # no UnicodeEncodeError
    fm = _frontmatter(md)
    assert fm["description"] == "a" + ch + "b"
    assert set(fm.keys()) == {"name", "description"}  # structure intact
