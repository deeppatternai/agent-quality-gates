"""What the installers actually write into a user's always-resident rules block.

Five installers (agent-clients, qoder, work-clients, cursor, and the manual paste
path) slice the same two templates from the
`## Agent Quality Gates (AQG) engineering discipline` heading to end of file and
copy the result verbatim into a user-level rules file. Everything below that
heading is therefore resident in the model's context for every session on that
machine, and every factual error in it is repeated to every agent.

Two problems this module locks down:

1. **Size.** The block ran to ~190 lines, most of it restating what each of the
   16 skills does. Those descriptions are already loaded by the host as the skill
   index, so the block was paying a per-session token cost to say them twice. Only
   what must be present BEFORE a decision belongs here: the Gate A criteria (they
   answer "does this need an audit at all", which is asked before anything is
   read), the skip clause, and the entry point.

2. **Truth.** The single Codex-shaped template was shipped verbatim to every
   client, so a Zed user's `~/.config/zed/AGENTS.md` claimed its skills were
   installed at `~/.codex/skills/` by `scripts/install.sh`, and named a count that
   no longer matched the shipped set.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "scripts"))

HEADING = "## Agent Quality Gates (AQG) engineering discipline"
TEMPLATES = (
    "examples/aqg-codex-agents.example.md",
    "examples/aqg-claude-rules.example.md",
)
# The block a client receives is everything from the heading down, which is what
# every installer slices — not the paste instructions above it.
MAX_BLOCK_LINES = 30


def _block(rel: str) -> str:
    text = (REPO / rel).read_text(encoding="utf-8")
    start = text.find(HEADING)
    assert start >= 0, f"{rel} lost the heading every installer slices on"
    return text[start:]


def test_the_resident_block_stays_small() -> None:
    """Every line here is paid for in every session on the machine."""
    for rel in TEMPLATES:
        lines = _block(rel).rstrip().splitlines()
        assert len(lines) <= MAX_BLOCK_LINES, (
            f"{rel} block is {len(lines)} lines (cap {MAX_BLOCK_LINES}); "
            "skill usage belongs in the skills themselves, not in resident context"
        )


def test_no_client_specific_install_paths_in_a_shared_block() -> None:
    """One template reaches Zed, Devin, Qoder, Kimi and Cursor as well as Codex."""
    for rel in TEMPLATES:
        block = _block(rel)
        for claim in ("~/.codex/skills", "~/.claude/skills", "scripts/install.sh"):
            assert claim not in block, (
                f"{rel} states {claim!r}, which is false for most clients that "
                "receive this exact text"
            )


def test_no_hardcoded_skill_count() -> None:
    """A number here goes stale the moment a skill is added or removed."""
    for rel in TEMPLATES:
        stale = re.findall(r"\b\d+\s+(?:AQG|aqg-\*)?\s*skills?\b", _block(rel))
        assert not stale, f"{rel} hardcodes a skill count: {stale}"


def test_the_gate_a_categories_survive_verbatim() -> None:
    """The criteria are the reason this block exists; drift makes it useless."""
    policy = (REPO / "docs" / "policies" / "audit-trigger.md").read_text(encoding="utf-8")
    marker = re.search(r"<!--\s*gate-a-tokens:\s*(.+?)\s*-->", policy)
    assert marker, "policy lost its gate-a-tokens marker"
    tokens = [t.strip() for t in marker.group(1).split(",") if t.strip()]
    for rel in TEMPLATES:
        block = _block(rel)
        for token in tokens:
            pattern = r"(?<![\w-])" + re.escape(token) + r"(?![\w-])"
            assert re.search(pattern, block), f"{rel} dropped Gate A category {token!r}"


def test_the_skip_clause_survives() -> None:
    """Without it the block only says "audit", and over-firing comes straight back."""
    for rel in TEMPLATES:
        assert "NOT audited" in _block(rel), (
            f"{rel} lost the trivial-skip clause; 2026-08-11 removed a categorical "
            "mandate for exactly this reason"
        )


def test_the_entry_point_is_named() -> None:
    for rel in TEMPLATES:
        assert "aqg-code-construction" in _block(rel), f"{rel} lost the entry point"


# --- the resolved path -------------------------------------------------------
#
# The block ends with a pointer to the full ladder. A literal `<AQG_ROOT>` reaching
# a user's rules file is a dangling pointer: nothing expands it there (a rules file
# is not a shell), so the one line that leads to the authoritative criteria would
# lead nowhere. Each installer knows the checkout it is installing from, so each
# substitutes it — and the placeholder must never survive into what is written.

PLACEHOLDER = "<AQG_ROOT>"


def test_the_template_carries_a_placeholder_for_the_installers_to_resolve() -> None:
    for rel in TEMPLATES:
        assert PLACEHOLDER in _block(rel), (
            f"{rel} lost the placeholder the installers substitute"
        )


def test_agent_clients_resolves_the_placeholder() -> None:
    import install_aqg_agent_clients as m  # noqa: PLC0415

    text = m._rule_text(REPO, "zed")
    assert PLACEHOLDER not in text, "placeholder survived into the written rule"
    assert f"{REPO}/docs/policies/audit-trigger.md" in text


def test_work_clients_resolves_the_placeholder() -> None:
    import install_aqg_work_clients as m  # noqa: PLC0415

    profile = next(iter(m.PROFILES.values()))
    text = m._render_rule(profile)
    assert PLACEHOLDER not in text
    assert "docs/policies/audit-trigger.md" in text


def test_qoder_resolves_the_placeholder() -> None:
    import install_aqg_qoder as m  # noqa: PLC0415

    text = m._rule_text(REPO)
    assert PLACEHOLDER not in text
    assert f"{REPO}/docs/policies/audit-trigger.md" in text


def test_cursor_resolves_the_placeholder() -> None:
    import install_cursor_support as m  # noqa: PLC0415

    text = m._render_project_rule()
    assert PLACEHOLDER not in text
    assert "docs/policies/audit-trigger.md" in text


def test_the_manual_paste_path_resolves_the_placeholder_too() -> None:
    """`cat template >> AGENTS.md` runs no installer, so nothing substitutes.

    Audit aud_TImE-8uBNHJv0D_w (google, blocking): the preamble tells users to cat
    the file into their rules file. With a bare cat the literal <AQG_ROOT> lands in
    their AGENTS.md and the one line leading to the authoritative criteria points
    nowhere — a regression this change introduced for exactly the users who have no
    installer to fix it for them.
    """
    for rel in TEMPLATES:
        text = (REPO / rel).read_text(encoding="utf-8")
        preamble = text[: text.find(HEADING)]
        paste = [ln for ln in preamble.splitlines() if ">>" in ln and "example.md" in ln]
        assert paste, f"{rel} preamble no longer shows a paste command"
        for line in paste:
            assert "<AQG_ROOT>" in line or "sed" in line, (
                f"{rel} paste command copies the placeholder verbatim: {line.strip()}"
            )


def test_doctor_flags_a_stale_long_block() -> None:
    """Publishing a shorter block reaches nobody who already installed the long one.

    Audit aud_TImE-8uBNHJv0D_w (google): an existing user keeps paying the token
    cost and keeps reading claims that were false for their client. That is the
    failure mode RETIRED_RULES_MARKERS exists for, so the retired framing has to be
    registered rather than left to be noticed.
    """
    import aqg_doctor  # noqa: PLC0415

    markers = {m for m, _ in aqg_doctor.RETIRED_RULES_MARKERS}
    assert any("Audit orchestration" in m for m in markers), (
        "the superseded long block is not registered as a retired framing, so an "
        "installed copy of it reports clean"
    )


def test_cursor_does_not_bake_a_machine_path_into_a_committed_file() -> None:
    """`.cursor/rules/aqg.mdc` is a project file teammates get from git.

    Audit aud_TImE-8uBNHJv0D_w (opus, blocking): it is not gitignored, so an
    absolute path from whoever ran the installer is committed and is wrong for
    every other machine. Project-scope writers must use the documented default
    location, which is the same everywhere, not the local checkout.
    """
    import install_cursor_support as m  # noqa: PLC0415

    text = m._render_project_rule()
    assert str(REPO) not in text, "local checkout path baked into a committed file"
    assert "~/.deeppattern/agent-quality-gates/docs/policies/audit-trigger.md" in text


def test_delegation_discipline_survives_the_cut() -> None:
    """It is not a skill description, so the skill index does not carry it.

    Audit aud_TImE-8uBNHJv0D_w (opus): a delegated cold-start agent inherits none
    of this session's context and may run where AQG_ROOT is unset — the exact
    condition that silently disables every hook. Nothing else in the resident set
    says to write the discipline into the prompt.
    """
    for rel in TEMPLATES:
        block = _block(rel)
        assert "delegat" in block.lower(), f"{rel} dropped the delegation discipline"


# The setup guide used to carry the rules block's install procedure as prose:
# locate the section, sed the placeholder, write, then grep the result against a
# checklist of tokens. Two bugs came out of that shape — the checklist went stale
# when the block shrank, and the copy step never substituted the placeholder at
# all. scripts/install_aqg_rules.py owns those steps now, and its own tests
# (tests/behavior/test_rules_installer.py) lock them against the real template:
# `--apply` resolves the placeholder, `--verify` compares the installed region to
# the shipped block byte for byte. What is asserted here is that the guide does
# not grow a second, hand-written copy of that procedure to drift again.
SETUP_DOCS = ("AI_SETUP.md", "AI_SETUP.zh-CN.md")


def test_the_setup_guide_delegates_the_block_to_the_installer() -> None:
    for doc in SETUP_DOCS:
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "install_aqg_rules.py" in text, (
            f"{doc} does not route the rules block through its installer"
        )
        assert "--verify" in text, f"{doc} drops the post-install verification step"


def test_the_setup_guide_does_not_re_grow_a_hand_edit_procedure() -> None:
    """A prose copy of the write path is what drifted from the block twice."""
    for doc in SETUP_DOCS:
        text = (REPO / doc).read_text(encoding="utf-8")
        assert "s|<AQG_ROOT>|" not in text, (
            f"{doc} hand-substitutes the placeholder again; the installer does that"
        )
        assert "rules-block-tokens" not in text, (
            f"{doc} hand-lists the block's content again; --verify compares it in full"
        )


README_DOCS = ("README.md", "README.zh-CN.md")


def test_the_readme_does_not_hand_users_the_bare_cat_paste() -> None:
    """`cat template >> CLAUDE.md` appends the template's install preamble too.

    The preamble is the "how to paste this" instructions, which AI_SETUP is
    explicit do not belong in the rules file — and a bare cat also leaves the
    literal <AQG_ROOT>, so the one line that leads to the authoritative criteria
    lands in the user's file pointing nowhere. The installer does both correctly.
    """
    for doc in README_DOCS:
        text = (REPO / doc).read_text(encoding="utf-8")
        for rel in TEMPLATES:
            name = rel.rsplit("/", 1)[-1]
            for line in text.splitlines():
                if name in line and ">>" in line:
                    assert "sed" in line, (
                        f"{doc} pastes {name} verbatim into a rules file: {line.strip()}"
                    )
        assert "install_aqg_rules.py" in text, (
            f"{doc} does not name the installer that writes this channel"
        )


def test_the_readme_does_not_claim_triggers_the_block_no_longer_states() -> None:
    """The README describes the block; the block is the source of truth.

    The slimmed block names ONE entry point and defers the rest to each skill's
    own description ("The other `aqg-*` skills announce their own triggers").
    A README that promises N enumerated mandatory trigger points is describing
    the ~190-line block that was retired, and a reader who installs today gets
    something else.
    """
    for doc in README_DOCS:
        text = (REPO / doc).read_text(encoding="utf-8")
        assert not re.search(r"\d+\s*(mandatory|个 mandatory)", text), (
            f"{doc} still counts mandatory trigger points; the block enumerates none"
        )
