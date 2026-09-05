"""Behavior-lock suite for aqg-session-handoff (a3 §4 fixture matrix).

Each test pins one row of the ADR §4 matrix so the round-2 second-order fixes stay
locked. Run by CI via `pytest skills/*/tests/`.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
import aqg_session_handoff as h  # noqa: E402

SCRIPT = SCRIPTS / "aqg_session_handoff.py"


# ---- builder: a baseline-valid handoff, override one section at a time ----

_BASELINE: dict = {
    "Goal": "Ship the generic session-handoff skill.",
    "Environment": "repo ~/aqg, branch feat/x, Python 3.9, read-only work.",
    "Done so far": "Wrote a3 ADR; audit_id 03ecef71 — 14 accepted, all closed.",
    "Current Working State": "worktree clean",
    "Next": "Implement validate gate, then write the fixture suite.",
    "Discipline traps": "none",
    "First step": "Run /aqg-startup-preflight then read a3 §3.5 R1-R8.",
    "Open decisions": "- Owner: 默认 full 还是加 mini 档 (已定 full).",
}


def _render(**overrides: str) -> str:
    body = dict(_BASELINE)
    body.update(overrides)
    out: list = []
    for num, name, _ in h.SECTIONS:
        out.append(f"## {num}. {name}")
        out.append(body[name])
        out.append("")
    return "\n".join(out)


def _violation_rules(text: str) -> set:
    """Return the set of rule prefixes (R1..R8) present in violations."""
    res = h.validate_handoff(text)
    return {v.split()[0] for v in res["violations"]}


# ---- happy path ----------------------------------------------------------

def test_happy_path_valid() -> None:
    res = h.validate_handoff(_render())
    assert res["valid"], res["violations"]


# ---- R6 First step blacklist (whole-section equals, not substring) -------

def test_first_step_blacklist_fails() -> None:
    assert "R6" in _violation_rules(_render(**{"First step": "继续之前的活"}))


def test_first_step_conceptual_passes() -> None:
    # conceptual step without a command must NOT be killed (C2)
    res = h.validate_handoff(_render(**{"First step": "先读 X 设计再决定 Y 怎么做"}))
    assert res["valid"], res["violations"]


def test_first_step_substring_none_not_killed() -> None:
    # "none of the tests pass" contains 'none' but is real content (self-review)
    res = h.validate_handoff(_render(**{"First step": "none of the tests pass — rerun pytest -x"}))
    assert res["valid"], res["violations"]


def test_first_step_hint_nudges_preflight() -> None:
    # discipline propagation: at cold-start the next session should preflight first
    assert "preflight" in h.SECTION_HINTS["First step"]


# ---- R4 required section may not be a bare none-token (F14) ---------------

def test_required_bare_none_fails() -> None:
    assert "R4" in _violation_rules(_render(Goal="none"))


def test_required_empty_fails() -> None:
    assert "R4" in _violation_rules(_render(Goal=""))


# ---- R5 / R7 none-token section-specificity (F2) -------------------------

def test_open_decisions_none_passes() -> None:
    res = h.validate_handoff(_render(**{"Open decisions": "none"}))
    assert res["valid"], res["violations"]


def test_open_decisions_na_passes() -> None:
    # N/A is a valid none-token for Open decisions → R7 must NOT false-kill (F2)
    res = h.validate_handoff(_render(**{"Open decisions": "N/A"}))
    assert res["valid"], res["violations"]


def test_open_decisions_worktree_clean_fails() -> None:
    # worktree clean is NOT a none-token here → treated as content → R7 actor fires
    rules = _violation_rules(_render(**{"Open decisions": "worktree clean"}))
    assert "R7" in rules


def test_open_decisions_missing_actor_fails() -> None:
    assert "R7" in _violation_rules(_render(**{"Open decisions": "- 是否加 mini 档?"}))


# ---- R5 Current Working State -------------------------------------------

def test_cws_blank_fails() -> None:
    assert "R5" in _violation_rules(_render(**{"Current Working State": ""}))


def test_cws_worktree_clean_passes() -> None:
    res = h.validate_handoff(_render(**{"Current Working State": "worktree clean"}))
    assert res["valid"], res["violations"]


def test_cws_hint_includes_running_state() -> None:
    # absorbed from SKILL4: running processes (shell IDs) are the next agent's
    # lifeline — Current Working State must prompt for them
    hint = h.SECTION_HINTS["Current Working State"]
    assert "shell ID" in hint and "worktrees" in hint, hint


# ---- R8 Done-so-far audit token -----------------------------------------

def test_done_missing_audit_fails() -> None:
    assert "R8" in _violation_rules(_render(**{"Done so far": "Wrote the script and tests."}))


def test_r8_message_keeps_chinese_audit_forms() -> None:
    # The R8 output is English (WS-10), but must still advertise the Chinese forms a
    # Chinese author relies on — `审` and `无 audit` (audit 2e0d40fe V2). Locks the
    # trigger-preservation so a future edit can't silently drop them.
    res = h.validate_handoff(_render(**{"Done so far": "Wrote the script and tests."}))
    r8 = next(v for v in res["violations"] if v.startswith("R8"))
    assert "审" in r8 and "无 audit" in r8, r8


def test_done_explicit_no_audit_passes() -> None:
    res = h.validate_handoff(_render(**{"Done so far": "Explored the repo. 无 audit run yet."}))
    assert res["valid"], res["violations"]


# ---- R2 placeholder residue (FILL sentinel) ------------------------------

def test_placeholder_residue_fails() -> None:
    assert "R2" in _violation_rules(_render(Goal="<FILL: 一句话本线目标>"))


def test_bare_angle_bracket_not_placeholder() -> None:
    # impl-audit F: legit `<branch>` / `<email>` must NOT trip R2
    res = h.validate_handoff(_render(Environment="run `git checkout <branch>`; contact <jeff@x.com>"))
    assert "R2" not in {v.split()[0] for v in res["violations"]}, res["violations"]


def test_unfilled_done_so_far_slot_caught() -> None:
    # impl-audit B: the (long) Done-so-far FILL slot left unfilled must fail R2,
    # even though it textually contains "audit" (R8 must not rescue it)
    long_slot = h._fill_slot(h.SECTION_HINTS["Done so far"])
    assert len(long_slot) > 80  # the bug was an 80-char cap
    assert "R2" in _violation_rules(_render(**{"Done so far": long_slot}))


# ---- R3 secret scan (lineno + redacted type, never raw secret) -----------

def test_secret_detected_fails_with_lineno() -> None:
    text = _render(Environment="key AKIAIOSFODNN7EXAMPLE in env")
    res = h.validate_handoff(text)
    assert not res["valid"]
    sec = [v for v in res["violations"] if v.startswith("R3")]
    assert sec and "[REDACTED:AWS access key]" in sec[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in sec[0]  # never re-leak the raw secret


def test_redaction_marker_not_false_positive() -> None:
    # a prefilled, already-redacted line must NOT trip the secret scan (F12)
    res = h.validate_handoff(_render(Environment="prefill had [REDACTED:AWS access key] here"))
    assert "R3" not in {v.split()[0] for v in res["violations"]}


def test_pem_multiline_secret_detected() -> None:
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBdummykey\n-----END RSA PRIVATE KEY-----"
    text = _render(Environment=pem)
    rules = {v.split()[0] for v in h.validate_handoff(text)["violations"]}
    assert "R3" in rules


# ---- impl-audit A: NO rule may echo a raw secret into a violation message ----

def test_secret_in_open_decisions_line_not_re_leaked() -> None:
    # R7 echoes a non-actor Open-decisions line — it must be redacted (impl-audit A)
    res = h.validate_handoff(_render(**{"Open decisions": "ship key AKIAIOSFODNN7EXAMPLE now"}))
    assert not res["valid"]
    assert all("AKIAIOSFODNN7EXAMPLE" not in v for v in res["violations"]), res["violations"]


def test_secret_in_fill_span_not_re_leaked() -> None:
    # an angle-bracketed token in a FILL slot — R2 echoes the span, must redact (A/F12)
    res = h.validate_handoff(_render(Goal="<FILL: ghp_000000000000000000000000000000000000>"))
    assert not res["valid"]
    assert all("ghp_0000" not in v for v in res["violations"]), res["violations"]


# ---- impl-audit C: R1 enforces the exact 8-section contract ------------------

def test_duplicate_header_fails_r1() -> None:
    text = _render() + "\n## 3. Done so far\nnone\n"
    assert "R1" in _violation_rules(text)


def test_numbered_markdown_in_body_not_misparsed() -> None:
    # a body line `## 2. did X` must NOT be treated as a section header (impl-audit C)
    body = "Wrote code; audit done.\n## 2. ran the tests\nall green."
    sections, ordered = h.parse_sections(_render(**{"Done so far": body}))
    assert ordered == h.SECTION_NAMES  # no phantom section, no truncation of the 8
    assert "ran the tests" in sections["Done so far"]


# ---- impl-audit D: wrong-section none-token rejected -------------------------

def test_cws_na_rejected_wrong_section_token() -> None:
    assert "R5" in _violation_rules(_render(**{"Current Working State": "n/a"}))


def test_traps_worktree_clean_rejected_wrong_section_token() -> None:
    assert "R5" in _violation_rules(_render(**{"Discipline traps": "worktree clean"}))


# ---- impl-audit E: R8 audit-token broadened ---------------------------------

def test_audit_id_with_space_passes_r8() -> None:
    res = h.validate_handoff(_render(**{"Done so far": "Shipped. audit id: 03ecef71 adjudicated."}))
    assert res["valid"], res["violations"]


def test_chinese_audit_mention_passes_r8() -> None:
    res = h.validate_handoff(_render(**{"Done so far": "实现完成,审计已过 (3 声部)。"}))
    assert res["valid"], res["violations"]


# ---- impl-audit G / V2: oversized input → early-return, ONLY size violation --

def test_oversized_input_only_size_violation() -> None:
    big = _render() + "\n" + ("x" * (h.MAX_INPUT_BYTES + 10))
    res = h.validate_handoff(big)
    assert len(res["violations"]) == 1 and "too large" in res["violations"][0]


# ---- V1: R7 never echoes line content (multi-line PEM leak) ------------------

def test_actor_labelled_pem_in_open_decisions_not_leaked() -> None:
    pem = (
        "- Owner: 用了这个 key\n"
        "-----BEGIN PRIVATE KEY-----\n"
        "MIIBVQIBADANBgkqhkiG9w0BAQEFAASCAT8wEXAMPLEbodyEXAMPLEbody\n"
        "-----END PRIVATE KEY-----"
    )
    res = h.validate_handoff(_render(**{"Open decisions": pem}))
    assert not res["valid"]
    assert all("MIIBVQIBADAN" not in v for v in res["violations"]), res["violations"]


# ---- V3: ledger-prefill boilerplate must not self-satisfy R8 ----------------

def test_prefill_prefix_does_not_self_satisfy_r8(tmp_path: Path) -> None:
    aqg = tmp_path / ".aqg"
    aqg.mkdir()
    (aqg / "current_ledger.md").write_text(
        "task_slug: t\ncreated_at: 2026-06-09\n", encoding="utf-8"
    )
    prefill = h._prefill_done_so_far(tmp_path)
    # simulate the user replacing the FILL slot with real (non-audit) content
    prefix = h.PLACEHOLDER_RE.sub("did the work, all tests green", prefill)
    assert not h._AUDIT_PRESENT_RE.search(prefix), prefix
    res = h.validate_handoff(_render(**{"Done so far": prefix}))
    assert "R8" in {v.split()[0] for v in res["violations"]}, res["violations"]


# ---- R1 missing header ---------------------------------------------------

def test_missing_header_fails() -> None:
    text = _render().replace("## 5. Next\n", "")
    assert "R1" in _violation_rules(text)


# ---- round-trip invariant (F1/F6): new skeleton → validate ---------------

def test_round_trip_skeleton_fails_r2_passes_r1(tmp_path: Path) -> None:
    skeleton = h.render_skeleton(tmp_path)
    rules = _violation_rules(skeleton)
    assert "R2" in rules        # unfilled <...> slots caught
    assert "R1" not in rules    # all 8 headers present (preamble-tolerant)


def test_minimal_banner_preamble_does_not_break_r1() -> None:
    # minimal banner → `>` preamble before headers; R1 must still find all 3 headers
    skeleton = h.render_minimal_skeleton()
    assert skeleton.lstrip().startswith(">")  # banner is preamble
    rules = {v.split()[0] for v in h.validate_handoff(skeleton, minimal=True)["violations"]}
    assert "R1" not in rules


def test_full_validate_tolerates_leading_preamble() -> None:
    # parse_sections skips any preamble before the first contract header (R1/F6);
    # a hand-pasted `>` block before section 1 must not break full-contract R1.
    # (Regression guard kept after the EAF advisory — the old preamble source — was
    # removed; preamble-tolerance is generic, not EAF-specific.)
    body = "> some leading note\n\n" + _render()
    assert "R1" not in _violation_rules(body)


# ---- new mode graceful behaviors -----------------------------------------

def test_new_without_ledger_no_error(tmp_path: Path) -> None:
    skeleton = h.render_skeleton(tmp_path)
    assert "## 3. Done so far" in skeleton
    assert all(f"## {n}." in skeleton for n, _, _ in h.SECTIONS)


def test_new_with_ledger_prefills(tmp_path: Path) -> None:
    aqg = tmp_path / ".aqg"
    aqg.mkdir()
    (aqg / "current_ledger.md").write_text(
        "task_slug: ship-handoff\ncreated_at: 2026-06-09\n# 6-step ...\n", encoding="utf-8"
    )
    skeleton = h.render_skeleton(tmp_path)
    assert "ship-handoff" in skeleton
    assert "construction ledger prefill" in skeleton


# ---- CLI exit codes ------------------------------------------------------

def test_cli_new_exit_0() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT), "new"], text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 0
    assert "## 1. Goal" in proc.stdout


def test_cli_validate_stdin_valid_exit_0() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "validate"], input=_render(), text=True, encoding="utf-8", capture_output=True
    )
    assert proc.returncode == 0, proc.stderr


def test_cli_validate_stdin_invalid_exit_1() -> None:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "validate"],
        input=_render(**{"First step": "TBD"}), text=True, encoding="utf-8", capture_output=True,
    )
    assert proc.returncode == 1


def test_cli_no_subcommand_exit_2() -> None:
    proc = subprocess.run([sys.executable, str(SCRIPT)], text=True, encoding="utf-8", capture_output=True)
    assert proc.returncode == 2


def test_cli_round_trip_new_pipe_validate_exit_1() -> None:
    new = subprocess.run([sys.executable, str(SCRIPT), "new"], text=True, encoding="utf-8", capture_output=True)
    val = subprocess.run(
        [sys.executable, str(SCRIPT), "validate"], input=new.stdout, text=True, encoding="utf-8", capture_output=True
    )
    assert val.returncode == 1  # unfilled skeleton must fail the gate


# ---- Codex review P2: EAF routing discriminator locked in the description ----
# The cross-pack EAF negative cannot live in triggers.yaml (check_fixture_mix I2:
# expected_skill must be in the AQG roster; eaf-session-handoff is not). So the
# layer-1 routing guard is the description hard-negative — lock it here (the pack
# canary also asserts the keyword).

def test_eaf_routing_discriminator_in_description() -> None:
    skill_md = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
    frontmatter = skill_md.split("---", 2)[1]
    assert "eaf-session-handoff" in frontmatter      # redirect target
    assert "EAF engine" in frontmatter               # explicit discriminator


# ---- Codex review P3b: secret bank must not drift from aqg-evidence-closeout --

def test_secret_bank_matches_closeout_source() -> None:
    sys.path.insert(
        0, str(Path(__file__).resolve().parents[2] / "aqg-evidence-closeout" / "scripts")
    )
    import aqg_closeout as co  # noqa: E402

    h_bank = [(p.pattern, n) for p, n in h._LOCAL_SECRET_PATTERNS]
    c_bank = [(p.pattern, n) for p, n in co._LOCAL_SECRET_PATTERNS]
    assert h_bank == c_bank, "handoff secret bank drifted from aqg-evidence-closeout — re-sync"


# ---- Design absorption: minimal/degraded mode (CTX-exhausted path) -------------

_MINIMAL_VALID = (
    "## 1. Goal\nShip the handoff skill; done when validate passes.\n\n"
    "## 4. Current Working State\n🟡 停在 foo.py:42 KeyError；策略：先加 guard。\n\n"
    "## 7. First step\nRun pytest skills/aqg-session-handoff/tests/ -q\n"
)


def test_minimal_skeleton_has_only_3_sections() -> None:
    out = h.render_minimal_skeleton()
    for n in ("1. Goal", "4. Current Working State", "7. First step"):
        assert f"## {n}" in out, f"missing minimal header {n}"
    for n in ("2. Environment", "3. Done so far", "5. Next", "8. Open decisions"):
        assert f"## {n}" not in out, f"minimal must omit {n}"
    assert "Minimal handoff" in out  # degraded-mode banner present


def test_minimal_validate_passes_three_sections() -> None:
    res = h.validate_handoff(_MINIMAL_VALID, minimal=True)
    assert res["valid"], res["violations"]


def test_minimal_handoff_under_full_validate_fails_r1() -> None:
    res = h.validate_handoff(_MINIMAL_VALID, minimal=False)
    assert not res["valid"]
    assert any(v.startswith("R1") for v in res["violations"])


def test_minimal_validate_still_secret_scans() -> None:
    # design §4: secret check is never skipped in the degraded path.
    bad = _MINIMAL_VALID + "\ntoken AKIAIOSFODNN7EXAMPLE\n"
    res = h.validate_handoff(bad, minimal=True)
    assert not res["valid"]
    assert any(v.startswith("R3") for v in res["violations"])
    assert all("AKIAIOSFODNN7EXAMPLE" not in v for v in res["violations"])  # never re-leak


def test_minimal_validate_rejects_placeholder_first_step() -> None:
    bad = (
        "## 1. Goal\nShip X.\n\n## 4. Current Working State\n🟡 foo.py:42；策略：guard.\n\n"
        "## 7. First step\n继续之前的活\n"
    )
    res = h.validate_handoff(bad, minimal=True)
    assert not res["valid"]
    assert any(v.startswith("R6") for v in res["violations"])


def test_minimal_validate_rejects_extra_recognized_section() -> None:
    # audit daec6046 (convergent): minimal = EXACTLY the 3 sections. An extra contract
    # section (here ## 5. Next) must fail R1 — `ordered != MINIMAL_SECTIONS` strict ==.
    extra = _MINIMAL_VALID + "\n## 5. Next\nstep two then three\n"
    res = h.validate_handoff(extra, minimal=True)
    assert not res["valid"]
    assert any(v.startswith("R1") for v in res["violations"])


def test_validate_json_output_includes_warnings() -> None:
    # audit daec6046 gpt-5.5 #3: the JSON CLI path must carry `warnings`.
    long_next = "\n".join(f"- step {i}: do thing {i}" for i in range(200))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "validate", "--json"],
        input=_render(Next=long_next), text=True, encoding="utf-8", capture_output=True,
    )
    import json as _json
    payload = _json.loads(proc.stdout)
    assert payload["valid"] is True                       # length alone passes
    assert any("soft max" in w for w in payload["warnings"]), payload


# ---- Design absorption: soft length advisory (non-failing) --------------------

def test_soft_length_warns_but_does_not_fail() -> None:
    long_next = "\n".join(f"- step {i}: do thing {i}" for i in range(200))
    res = h.validate_handoff(_render(Next=long_next))
    assert res["valid"], res["violations"]            # length ALONE never fails
    assert any("soft max" in w for w in res["warnings"]), res["warnings"]


# ---- Continuity footer: handoff carries AQG discipline forward (Owner 2026-06-11) ----

def test_skeleton_appends_continuity_footer() -> None:
    out = h.render_skeleton(Path("."))
    assert h.HANDOFF_FOOTER_SENTINEL in out
    assert "Continuity discipline" in out
    assert "invoke" in out            # anti-manual-equivalent instruction
    assert "don't freestyle the md" in out      # chain-breaking instruction (NOT in sections 1-8)


def test_minimal_skeleton_also_has_footer() -> None:
    out = h.render_minimal_skeleton()
    assert h.HANDOFF_FOOTER_SENTINEL in out
    assert "Continuity discipline" in out


def test_footer_does_not_break_validate() -> None:
    res = h.validate_handoff(_render() + h.HANDOFF_FOOTER)
    assert res["valid"], res["violations"]


def test_footer_with_none_open_decisions_validates() -> None:
    # Open decisions = explicit none; the appended footer must NOT merge into section 8
    # (which would make it non-none + the footer bullets trip R7). Strip keeps it `none`.
    res = h.validate_handoff(_render(**{"Open decisions": "none"}) + h.HANDOFF_FOOTER)
    assert res["valid"], res["violations"]
    assert not any(v.startswith("R7") for v in res["violations"])


def test_footer_region_still_secret_scanned() -> None:
    # R3 scans the FULL text incl. the footer region — the footer is static but the area
    # must not become a secret blind spot.
    bad = _render() + h.HANDOFF_FOOTER + "\nleak AKIAIOSFODNN7EXAMPLE\n"
    res = h.validate_handoff(bad)
    assert not res["valid"]
    assert any(v.startswith("R3") for v in res["violations"])
    assert all("AKIAIOSFODNN7EXAMPLE" not in v for v in res["violations"])


def test_footer_sentinel_in_body_not_truncated() -> None:
    # audit 2c4654ce f1(a): a handoff whose §2 quotes the footer machinery (the literal
    # sentinel) + the real footer at the end must NOT truncate at the embedded sentinel —
    # all 8 sections still parse (end-anchored strip, not first-occurrence split).
    env = f"repo ~/x; 本 handoff 文档化了 {h.HANDOFF_FOOTER_SENTINEL} 续链机制。"
    res = h.validate_handoff(_render(**{"Environment": env}) + h.HANDOFF_FOOTER)
    assert res["valid"], res["violations"]


def test_content_after_footer_is_not_an_unvalidated_escape() -> None:
    # audit 2c4654ce f1(b): content appended BELOW the footer must NOT escape the section
    # rules (false PASS). End-anchored strip leaves it inside section 8 → a no-actor line
    # there trips R7, so validation sees it.
    sneaky = _render() + h.HANDOFF_FOOTER + "\n- 偷偷塞的没点名 actor 的待决项\n"
    res = h.validate_handoff(sneaky)
    assert not res["valid"], "content after the footer must be validated, not escaped"


def test_soft_length_silent_under_cap() -> None:
    res = h.validate_handoff(_render())
    assert res["warnings"] == [], res["warnings"]


# ---- Continuity footer: round-trip re-wrap must not pollute §8 (R7 false-positive) ----
# The footer's two bullets are single long lines; an LLM/editor re-emit of the handoff
# commonly re-wraps them, inserting a newline mid-bullet. The exact-suffix endswith()
# strip broke on ANY such whitespace change, leaving the whole footer inside section 8 →
# its `---` / title / bullet lines were counted as no-actor decisions → spurious R7. Fix:
# whitespace-insensitive match anchored on the LAST sentinel, below-footer content folded
# back (audit 2c4654ce f1(b): not dropped). Repro: §8 = 2 actor-named decisions; pre-fix
# the wrapped footer made it fail R7, post-fix it passes.

def _wrapped_footer() -> str:
    """HANDOFF_FOOTER with a newline inserted into its longest line (a realistic round-trip
    re-wrap). Computed from the actual footer text — NOT a literal-substring replace — so it
    keeps inserting a real mid-line newline even if the footer wording later changes (audit
    1444cdb8 claude-f2 / grok-f4: a literal target that drifts away would silently no-op the
    re-wrap, making the regression guard below vacuous)."""
    lines = h.HANDOFF_FOOTER.split("\n")
    longest = max(range(len(lines)), key=lambda i: len(lines[i]))  # one of the long bullets
    mid = len(lines[longest]) // 2
    lines[longest] = lines[longest][:mid] + "\n  " + lines[longest][mid:]
    return "\n".join(lines)


def test_wrapped_footer_helper_actually_rewraps() -> None:
    # guard-the-guard (audit 1444cdb8 claude-f2 / grok-f4): if the footer text ever drifts so
    # the injection no-ops, the wrapped-footer tests would silently pass on the VERBATIM path
    # (which the old code also handled) — making the re-wrap guard vacuous. Fail loudly here.
    wrapped = _wrapped_footer()
    assert wrapped != h.HANDOFF_FOOTER                              # a real change happened
    assert wrapped.count("\n") > h.HANDOFF_FOOTER.count("\n")       # a newline was inserted
    assert h.HANDOFF_FOOTER_SENTINEL in wrapped                    # sentinel left intact


def test_wrapped_footer_does_not_pollute_section8() -> None:
    # core regression (pre-fix: R7 fired on the footer lines). §8 = 2 real decisions, each
    # naming Owner → a PASS proves the re-wrapped footer was peeled, not that §8 is none.
    two = "- 是否上 prod：Owner 拍板\n- 用 X 还是 Y：Owner 决定"
    res = h.validate_handoff(_render(**{"Open decisions": two}) + _wrapped_footer())
    assert res["valid"], res["violations"]
    assert not any(v.startswith("R7") for v in res["violations"]), res["violations"]


def test_verbatim_footer_multi_decision_validates() -> None:
    # bug report's literal shape: multiple actor-named decisions + the verbatim footer.
    # (Only the single-decision baseline and the `none` case were locked before.)
    two = "- 是否上 prod：Owner 拍板\n- 用 X 还是 Y：Owner 决定"
    res = h.validate_handoff(_render(**{"Open decisions": two}) + h.HANDOFF_FOOTER)
    assert res["valid"], res["violations"]
    assert not any(v.startswith("R7") for v in res["violations"]), res["violations"]


def test_wrapped_footer_with_none_open_decisions_validates() -> None:
    # the re-wrap fix must hold for the none case too (footer peeled → §8 stays `none`).
    res = h.validate_handoff(_render(**{"Open decisions": "none"}) + _wrapped_footer())
    assert res["valid"], res["violations"]


def test_wrapped_footer_region_still_secret_scanned() -> None:
    # R3 scans the full original text — a re-wrapped footer must not become a secret blind
    # spot (the strip path changed; the scan input did not).
    bad = _render(**{"Open decisions": "- Owner 决定"}) + _wrapped_footer() + "\nleak AKIAIOSFODNN7EXAMPLE\n"
    res = h.validate_handoff(bad)
    assert not res["valid"]
    assert any(v.startswith("R3") for v in res["violations"])
    assert all("AKIAIOSFODNN7EXAMPLE" not in v for v in res["violations"])  # never re-leak


def test_consume_footer_template_ignores_rewrap() -> None:
    # unit: the footer body after the sentinel, re-wrapped, is fully consumed (no real
    # trailing residue) — whitespace differences are skipped.
    tail = _wrapped_footer().split(h.HANDOFF_FOOTER_SENTINEL, 1)[1]
    residue = h._consume_footer_template(tail)
    assert residue is not None and residue.strip() == "", repr(residue)


def test_consume_footer_template_returns_below_footer_residue() -> None:
    # unit: content appended below the footer is returned as residue (caller folds it back
    # into the body so the section rules still validate it — no unvalidated escape).
    tail = h.HANDOFF_FOOTER.split(h.HANDOFF_FOOTER_SENTINEL, 1)[1] + "\n- sneaky 待决项"
    residue = h._consume_footer_template(tail)
    assert residue is not None and "sneaky 待决项" in residue


def test_consume_footer_template_rejects_quoted_sentinel() -> None:
    # unit: a sentinel quoted in section text is NOT followed by the footer body → None,
    # so the caller declines to strip (don't truncate a handoff that documents the footer).
    assert h._consume_footer_template("\n续链机制说明。\n\n## 3. Done so far\nx") is None


# ---- Escape protection (b) under fold-back: audit 1444cdb8 f1 (claude/grok) adjudication --
# Auditors claimed below-footer residue could escape R7 if it re-declares a contract header
# (parse_sections first-wins dedup shadows the duplicate body). Verified FALSE: a duplicate
# contract header trips R1 (ordered != contract), a non-contract header folds into §8 → R7.
# These lock that escape protection (b) is held jointly by R1 + R7, not by R7 alone.

def test_below_footer_residue_with_contract_header_caught_by_r1() -> None:
    sneaky = _render() + h.HANDOFF_FOOTER + "\n## 8. Open decisions\n- 没点名 actor 的待决项\n"
    res = h.validate_handoff(sneaky)
    assert not res["valid"], "duplicate contract header in residue must not pass"
    assert any(v.startswith("R1") for v in res["violations"]), res["violations"]


def test_below_footer_residue_noncontract_header_caught_by_r7() -> None:
    # `## 9. Notes` is not a contract header → not a boundary → folds into §8 → R7 sees it.
    sneaky = _render() + h.HANDOFF_FOOTER + "\n## 9. Notes\n- 没点名 actor 的行\n"
    res = h.validate_handoff(sneaky)
    assert not res["valid"], "non-contract header residue must not escape validation"
    assert any(v.startswith("R7") for v in res["violations"]), res["violations"]


# ---- copy-box / triple-backtick fence (proactive guidance + blocking gate) ----

def test_first_step_hint_nudges_indent_not_fence() -> None:
    # Proactive: the fill hint tells the author to use 4-space indent (so they write
    # it right the first time).
    assert "indent" in h.SECTION_HINTS["First step"]


def test_no_section_hint_contains_literal_fence() -> None:
    # Recursive-bug guard (audit 34275bcd claude f1): a literal triple-backtick fence
    # inside ANY hint would itself break the skeleton's copy-box. Pin every hint, not
    # just one, so a future edit can't silently reintroduce a fence.
    for name, hint in h.SECTION_HINTS.items():
        assert "```" not in hint, f"{name} hint must not contain a literal fence"


def test_validate_fails_on_code_fence() -> None:
    # A triple-backtick fence in the body breaks the single paste copy-box: the inner
    # fence closes the outer one, so the next reader cannot select the whole handoff at
    # once (the recurring "section truncated" failure). validate fails CLOSED — a fenced
    # handoff cannot be pasted as-is, so the author must reformat to 4-space indent first
    # (a non-blocking warning let fenced handoffs through and the break kept recurring).
    fenced = "run setup:\n\n```bash\nshasum -a 256 x\n```"
    res = h.validate_handoff(_render(**{"First step": fenced}))
    assert not res["valid"], res
    # Tightened (audit 5fa4ee14 claude-f2): the fence violation message always contains
    # "fence", so match on that alone — `or "indent"` would let an unrelated indentation
    # violation satisfy this and mask a missing fence-specific violation.
    assert any("fence" in v for v in res["violations"]), res["violations"]


def test_validate_no_fence_violation_on_4space_indented_block() -> None:
    # 4-space-indented commands (the recommended form) are NOT a fence (CommonMark:
    # a fence has <=3 leading spaces) → no copy-box violation, handoff stays valid.
    indented = "run setup:\n\n    shasum -a 256 x\n    python3 -m pytest tests/"
    res = h.validate_handoff(_render(**{"First step": indented}))
    assert res["valid"], res["violations"]
    assert not any("fence" in v for v in res["violations"]), res["violations"]


def test_validate_no_fence_violation_on_indented_backtick_line() -> None:
    # False-positive boundary (audit 5fa4ee14 claude-f1 / gpt-5.5-f1): now that a fence
    # FAILS validate, a triple-backtick line that is itself >=4-space INDENTED is literal
    # text inside a code block, NOT a CommonMark fence opener (fences allow <=3 leading
    # spaces) — it must NOT block. Pins _has_code_fence's leading-space boundary.
    indented_fence = (
        "show the rendered block:\n\n"
        "        ```bash\n        echo hi\n        ```"
    )
    res = h.validate_handoff(_render(**{"First step": indented_fence}))
    assert res["valid"], res["violations"]
    assert not any("fence" in v for v in res["violations"]), res["violations"]


def test_validate_no_fence_violation_on_inline_backtick_span() -> None:
    # False-positive boundary: an INLINE triple-backtick span (mid-line, not line-initial)
    # is not a fence opener and must not block — guards that _has_code_fence keys off a
    # line-initial ``` (startswith), not a substring match anywhere in the line.
    inline = "use the ```x``` style marker then proceed to the next step"
    res = h.validate_handoff(_render(**{"First step": inline}))
    assert res["valid"], res["violations"]
    assert not any("fence" in v for v in res["violations"]), res["violations"]


def test_footer_template_has_no_literal_fence() -> None:
    # Recursive-bug guard (audit 5fa4ee14 grok-f2): the continuity footer is appended to
    # every handoff and PEELED before the fence check, so it must itself stay fence-free —
    # a future footer edit adding a ``` line would break the copy-box it protects. Mirrors
    # test_no_section_hint_contains_literal_fence, for the footer.
    assert not h._has_code_fence(h.HANDOFF_FOOTER), repr(h.HANDOFF_FOOTER)


def test_footer_explicitly_is_not_a_handoff_trigger() -> None:
    # A pasted handoff starts continuation work; its auto-added footer must not be
    # misread as a fresh request to immediately generate another handoff.
    output = h.render_skeleton(Path("."))
    denial = "This continuity footer is not a handoff request or trigger."
    assert denial in output
    assert "Only after a separate observable trigger" in output
    assert "an explicit user request for a handoff" in output
    assert "Write YOUR next handoff" not in output
    assert output.index(denial) < output.index("Don't just read — act")

    result = h.validate_handoff(_render(**{"Open decisions": "none"}) + h.HANDOFF_FOOTER)
    assert result["valid"], result["violations"]
