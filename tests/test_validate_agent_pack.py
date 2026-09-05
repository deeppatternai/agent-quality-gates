#!/usr/bin/env python3
"""Regression tests for AQG agent-pack validation."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))


def run_validator(pack: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "scripts/validate_agent_pack.py",
            "--agent",
            "claude-code",
            "--pack",
            str(pack),
        ],
        cwd=REPO,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_pack(root: Path, hook_text: str, frontmatter_extra: str = "") -> None:
    skill = root / "skills" / "aqg-example"
    hook = root / "hooks"
    skill.mkdir(parents=True)
    hook.mkdir(parents=True)
    (root / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        "---\n"
        "name: aqg-example\n"
        "\n"
        "description: Example AQG skill\n"
        f"{frontmatter_extra}"
        "---\n"
        "# Example\n\n"
        "Use AQG_ROOT and CLAUDE_SKILL_DIR to find shared scripts/quality-gates.json.\n",
        encoding="utf-8",
    )
    (hook / "settings.warn-only.example.json").write_text(hook_text, encoding="utf-8")


class ValidateAgentPackTest(unittest.TestCase):
    def test_repository_claude_pack_validates(self) -> None:
        proc = run_validator(REPO / "agent-packs" / "claude-code")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_frontmatter_allows_blank_lines(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            pack = Path(tmp_s) / "pack"
            write_pack(
                pack,
                '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo $CLAUDE_PROJECT_DIR || true"}]}]}}',
            )
            proc = run_validator(pack)
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_structured_blocking_fields_are_rejected_without_spacing_dependency(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            pack = Path(tmp_s) / "pack"
            write_pack(
                pack,
                '{"hooks":{"Stop":[{"hooks":[{"type":"command","command":"echo $CLAUDE_PROJECT_DIR || true","decision":"block"}]}]}}',
            )
            proc = run_validator(pack)
            self.assertEqual(proc.returncode, 1)
            self.assertIn("decision=block", proc.stderr)

    def test_validate_skill_rejects_missing_script_refs(self) -> None:
        # P0-4: the $aqg_root/.../*.py referenced by SKILL.md must actually exist, otherwise
        # after Codex skills are renamed/moved the Claude Code SKILL.md would fail silently.
        from validate_agent_pack import validate_skill, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            repo_root = Path(tmp_s)
            (repo_root / "scripts").mkdir()
            (repo_root / "scripts" / "real_script.py").write_text("# present")

            skill_md = repo_root / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\ndescription: t\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n"
                "$aqg_root/scripts/real_script.py\n"
                "$aqg_root/scripts/missing-script.py\n"
                "$aqg_root/skills/imaginary/scripts/nope.py\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_skill(skill_md, repo_root=repo_root)
            msg = str(cm.exception)
            self.assertIn("missing scripts", msg)
            self.assertIn("scripts/missing-script.py", msg)
            self.assertIn("skills/imaginary/scripts/nope.py", msg)
            # the already-present real_script.py must not appear in the missing list
            self.assertNotIn("real_script.py", msg.split("missing scripts:")[1])

    def test_extract_script_refs_accepts_braced_and_unbraced_aqg_root(self) -> None:
        # P2: Shell snippets naturally drift between $aqg_root and
        # ${aqg_root}; the validator must catch both forms.
        from validate_agent_pack import extract_script_refs

        refs = extract_script_refs(
            'script="$aqg_root/scripts/run_quality_gates.py"\n'
            'other="${aqg_root}/skills/aqg-startup-preflight/scripts/aqg_preflight.py"\n'
            'script="$aqg_root/scripts/run_quality_gates.py"\n'
        )
        self.assertEqual(
            refs,
            [
                "scripts/run_quality_gates.py",
                "skills/aqg-startup-preflight/scripts/aqg_preflight.py",
            ],
        )

    def test_validate_skill_skips_cross_check_when_repo_root_is_none(self) -> None:
        # Keep backward compatibility: old callers (no repo_root) do only basic checks, not enforcing script existence.
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\ndescription: t\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n"
                "$aqg_root/scripts/definitely-missing.py\n",
                encoding="utf-8",
            )
            # repo_root=None: does not raise
            validate_skill(skill_md, repo_root=None)

    def test_per_command_blocking_true_opts_out_of_blocking_checks(self) -> None:
        # v0.8.0 contract (audit gpt-5.5 #3 fix): per-command `_blocking: true`
        # marks a specific hook entry as intentionally blocking. Validator skips
        # warn-only checks for that command; other commands in the same file
        # remain validated. Prevents file-level opt-out drift.
        from validate_agent_pack import validate_hook

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.blocking.example.json"
            tmp.write_text(
                '{"hooks": {"PreToolUse": [{"matcher": "Bash", '
                '"hooks": [{"type": "command", "_blocking": true, '
                '"command": "echo $CLAUDE_PROJECT_DIR; exit 1"}]}]}}',
                encoding="utf-8",
            )
            # Should NOT raise — _blocking=true on this command relaxes blocking check.
            validate_hook(tmp)

    def test_per_command_blocking_does_not_relax_other_commands_in_same_file(self) -> None:
        # Critical contract: per-command opt-out must NOT let warn-only commands
        # in the same file slip blocking exits past validation (the file-level
        # opt-out flaw the audit caught).
        from validate_agent_pack import validate_hook, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.blocking.example.json"
            tmp.write_text(
                '{"hooks": {'
                '"PreToolUse": [{"matcher": "Bash", '
                '"hooks": [{"type": "command", "_blocking": true, '
                '"command": "echo $CLAUDE_PROJECT_DIR; exit 1"}]}], '
                '"Stop": [{"matcher": "", '
                '"hooks": [{"type": "command", '
                '"command": "echo $CLAUDE_PROJECT_DIR; exit 2"}]}]'
                '}}',
                encoding="utf-8",
            )
            # Stop hook has blocking exit but no _blocking marker → must fail.
            # Either the token-level check ("forbidden blocking token: exit N") or the
            # command-level regex check ("blocking exit/return") may fire first.
            with self.assertRaises(ValidationError) as cm:
                validate_hook(tmp)
            msg = str(cm.exception)
            self.assertTrue(
                "blocking exit/return" in msg or "forbidden blocking token" in msg,
                f"expected blocking-rejection message, got: {msg}",
            )

    def test_default_unmarked_command_still_rejects_blocking(self) -> None:
        # Existing pre-v0.8.0 contract preserved: command without _blocking marker
        # falls under warn-only rules.
        from validate_agent_pack import validate_hook, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.warn-only.example.json"
            tmp.write_text(
                '{"hooks": {"Stop": [{"matcher": "", '
                '"hooks": [{"type": "command", "command": "echo $CLAUDE_PROJECT_DIR; exit 1"}]}]}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_hook(tmp)
            self.assertIn("blocking exit/return", str(cm.exception))

    def test_blocking_command_still_requires_claude_project_dir(self) -> None:
        # _blocking=true relaxes BLOCKING checks but NOT the CLAUDE_PROJECT_DIR contract.
        from validate_agent_pack import validate_hook, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.blocking.example.json"
            tmp.write_text(
                '{"hooks": {"PreToolUse": [{"matcher": "Bash", '
                '"hooks": [{"type": "command", "_blocking": true, '
                '"command": "echo no-project-dir-ref; exit 1"}]}]}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_hook(tmp)
            self.assertIn("CLAUDE_PROJECT_DIR", str(cm.exception))

    def test_blocking_must_be_strict_true_boolean(self) -> None:
        # _blocking must be `true` literal; reject other truthy values to prevent
        # contract drift via stringly-typed config.
        from validate_agent_pack import validate_hook, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.blocking.example.json"
            tmp.write_text(
                '{"hooks": {"PreToolUse": [{"matcher": "Bash", '
                '"hooks": [{"type": "command", "_blocking": "true", '
                '"command": "echo $CLAUDE_PROJECT_DIR; exit 1"}]}]}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_hook(tmp)
            self.assertIn("_blocking", str(cm.exception))

    def test_legacy_warn_only_file_level_key_rejected(self) -> None:
        # Audit gpt-5.5 #3 fix: file-level `_warn_only` key (the previous interim
        # contract) is REJECTED to force migration to per-command marking.
        # Prevents silent contract drift if someone copies an old example.
        from validate_agent_pack import validate_hook, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s) / "settings.blocking.example.json"
            tmp.write_text(
                '{"_warn_only": false, "hooks": {"PreToolUse": [{"matcher": "Bash", '
                '"hooks": [{"type": "command", "command": "echo $CLAUDE_PROJECT_DIR; exit 1"}]}]}}',
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_hook(tmp)
            self.assertIn("_warn_only", str(cm.exception))
            self.assertIn("_blocking", str(cm.exception))


class T1BHardSoftDependencyTest(unittest.TestCase):
    """T1-B: Hard/Soft dependency frontmatter validation."""

    def test_parse_dependencies_well_formed(self) -> None:
        from validate_agent_pack import parse_dependencies

        result = parse_dependencies("dep-a:hard, dep-b:soft, dep-c:hard")
        self.assertEqual(
            result,
            [("dep-a", "hard"), ("dep-b", "soft"), ("dep-c", "hard")],
        )

    def test_parse_dependencies_empty_returns_empty(self) -> None:
        from validate_agent_pack import parse_dependencies

        self.assertEqual(parse_dependencies(""), [])
        self.assertEqual(parse_dependencies("   "), [])

    def test_parse_dependencies_rejects_invalid_kind(self) -> None:
        from validate_agent_pack import parse_dependencies, ValidationError

        with self.assertRaises(ValidationError) as cm:
            parse_dependencies("dep-a:medium")  # only hard/soft allowed
        self.assertIn("invalid dependencies entry", str(cm.exception))

    def test_parse_dependencies_rejects_no_colon(self) -> None:
        from validate_agent_pack import parse_dependencies, ValidationError

        with self.assertRaises(ValidationError) as cm:
            parse_dependencies("dep-a hard")  # missing colon
        self.assertIn("invalid dependencies entry", str(cm.exception))

    def test_validate_skill_hard_dep_requires_setup_pointer(self) -> None:
        from validate_agent_pack import validate_skill, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Test skill. Use when running test.\n"
                "dependencies: setup-foo:hard\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n"
                "Body content here without the required phrase.\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_skill(skill_md, repo_root=None)
            self.assertIn("hard dependency 'setup-foo'", str(cm.exception))

    def test_validate_skill_hard_dep_pointer_found(self) -> None:
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            # Audit F1 fix: dep name + pointer must be in SAME paragraph.
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Test skill. Use when running test.\n"
                "dependencies: setup-foo:hard\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n\n"
                "The setup-foo dep is required setup; "
                "if not installed, run setup-foo.\n",
                encoding="utf-8",
            )
            # Should NOT raise: name 'setup-foo' + 'if not installed, run' in
            # same paragraph satisfies hard dep.
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertIsInstance(warnings, list)

    def test_validate_skill_hard_dep_pointer_in_different_paragraph_fails(self) -> None:
        """Audit F1 regression: pointer phrase in different paragraph than
        dep name should NOT satisfy (per-name + same-paragraph rule)."""
        from validate_agent_pack import validate_skill, ValidationError

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Test skill. Use when running test.\n"
                "dependencies: setup-foo:hard\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n\n"
                "The setup-foo dep does this thing.\n\n"
                "Required setup is documented elsewhere.\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_skill(skill_md, repo_root=None)
            self.assertIn("hard dependency 'setup-foo'", str(cm.exception))

    def test_validate_skill_soft_dep_warns_when_not_mentioned(self) -> None:
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Test skill. Use when running test.\n"
                "dependencies: glossary:soft\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n"
                "Body does NOT mention the soft dep name.\n",
                encoding="utf-8",
            )
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertTrue(
                any("soft dependency 'glossary'" in w for w in warnings),
                f"expected soft-dep WARN; got {warnings}",
            )

    def test_validate_skill_missing_dependencies_field_does_not_warn(self) -> None:
        """Backward compat: existing skills without `dependencies:` must not break."""
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Test skill. Use when running test.\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            # Should NOT raise + no T1-B warnings
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertFalse(
                any("dependency" in w for w in warnings),
                f"opt-in T1-B should not warn on missing field; got {warnings}",
            )


class T1CDescriptionStyleTest(unittest.TestCase):
    """T1-C: Description frontmatter style lint."""

    def test_description_over_hard_limit_fails(self) -> None:
        from validate_agent_pack import validate_skill, ValidationError

        long_desc = "x" * 1025  # exceeds 1024 hard limit
        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                f"---\nname: aqg-test\ndescription: {long_desc}\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            with self.assertRaises(ValidationError) as cm:
                validate_skill(skill_md, repo_root=None)
            self.assertIn("1024 char hard limit", str(cm.exception))

    def test_description_over_soft_limit_warns(self) -> None:
        from validate_agent_pack import validate_skill

        long_desc = "x" * 850  # exceeds 800 warn limit, under 1024 hard limit
        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                f"---\nname: aqg-test\ndescription: {long_desc}\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertTrue(
                any("800 char soft limit" in w for w in warnings),
                f"expected soft-limit WARN; got {warnings}",
            )

    def test_description_missing_use_when_warns(self) -> None:
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Helps with foo bar baz.\n---\n"  # no trigger
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertTrue(
                any("Use when" in w for w in warnings),
                f"expected trigger-missing WARN; got {warnings}",
            )

    def test_description_with_use_when_does_not_warn(self) -> None:
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: Format strings. Use when normalizing text.\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertFalse(
                any("Use when" in w for w in warnings),
                f"description with 'Use when' should not warn; got {warnings}",
            )

    def test_description_first_person_warns(self) -> None:
        from validate_agent_pack import validate_skill

        with tempfile.TemporaryDirectory() as tmp_s:
            skill_md = Path(tmp_s) / "SKILL.md"
            skill_md.write_text(
                "---\nname: aqg-test\n"
                "description: I help debug code. Use when bug.\n---\n"
                "AQG_ROOT and CLAUDE_SKILL_DIR; scripts/foo.py\n",
                encoding="utf-8",
            )
            warnings = validate_skill(skill_md, repo_root=None)
            self.assertTrue(
                any("first-person" in w for w in warnings),
                f"expected first-person WARN; got {warnings}",
            )

    def test_real_aqg_pack_passes_t1bc(self) -> None:
        """Run validator against repo's real AQG pack — must exit 0
        (T1-B opt-in + T1-C WARN don't FAIL existing 10 skills).
        """
        proc = run_validator(REPO / "agent-packs" / "claude-code")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
