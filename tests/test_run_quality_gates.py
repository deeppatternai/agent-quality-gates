#!/usr/bin/env python3
"""Local Slice 3 adapter regression tests using only the standard library."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]


def run_cmd(args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    run_env = os.environ.copy()
    for key in (
        "GITHUB_OUTPUT",
        "GITHUB_STEP_SUMMARY",
        "GITHUB_EVENT_PATH",
        "GITHUB_EVENT_NAME",
        "GITHUB_REPOSITORY",
        "GH_TOKEN",
    ):
        run_env.pop(key, None)
    if env:
        run_env.update(env)
    return subprocess.run(
        args,
        cwd=REPO,
        env=run_env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


class RunQualityGatesTest(unittest.TestCase):
    def run_adapter(self, fixture: str, config: str = "examples/quality-gates.json") -> tuple[subprocess.CompletedProcess[str], dict[str, object]]:
        with tempfile.TemporaryDirectory() as tmp_s:
            output = Path(tmp_s) / "quality-gates.json"
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    config,
                    "--repo",
                    ".",
                    "--pr-body-file",
                    fixture,
                    "--output-json",
                    str(output),
                ]
            )
            payload = json.loads(output.read_text()) if output.exists() else {}
            return proc, payload

    def test_valid_fixture_passes_without_raw_body_leak(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/valid.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue(payload["ok"])
        text = json.dumps(payload, ensure_ascii=False)
        self.assertNotIn("Implemented local AQG fixture behavior", text)
        self.assertNotIn("adapter fixture and checks completed", text)
        self.assertEqual(payload["pr_body"]["redaction"]["total_secret_like_matches"], 0)

    def test_warn_mode_evidence_failure_does_not_block(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/missing_evidence.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["gates"]["evidence_closeout"]["status"], "fail")
        self.assertEqual(payload["gates"]["evidence_closeout"]["mode"], "warn")

    def test_missing_audit_blocks(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/missing_audit.md")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(payload["blocking_failures"], ["audit_adjudication"])

    def test_invalid_audit_skip_blocks(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/audit_skipped_invalid_reason.md")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertEqual(payload["blocking_failures"], ["audit_adjudication"])

    def test_valid_audit_skip_passes(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/audit_skipped_valid_reason.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["gates"]["audit_adjudication"]["status"], "skipped")

    def test_blocking_evidence_config_passes_valid_fixture(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/valid.md", "tests/fixtures/config/valid.json")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertEqual(payload["gates"]["evidence_closeout"]["mode"], "blocking")

    def test_unauthorized_boundary_surfaces_finding_under_warn(self) -> None:
        # WS-8 P2-5a: declaring a production boundary `unauthorized` must not clear
        # the evidence gate silently. Under the default warn mode it surfaces as a
        # finding (gate fail) but does not block (returncode 0).
        proc, payload = self.run_adapter("tests/fixtures/pr_body/boundary_unauthorized.md")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        gate = payload["gates"]["evidence_closeout"]
        self.assertEqual(gate["mode"], "warn")
        self.assertEqual(gate["status"], "fail")
        messages = [f["message"] for f in gate["findings"]]
        self.assertTrue(
            any("production boundary" in m and "unauthorized" in m.lower() for m in messages),
            f"expected an unauthorized production-boundary finding, got {messages}",
        )

    def test_unauthorized_boundary_blocks_under_blocking_config(self) -> None:
        # Same declaration under a blocking evidence gate is a hard stop, and the
        # block must be *caused by* the unauthorized-boundary finding (not some
        # unrelated evidence failure on the same fixture).
        proc, payload = self.run_adapter(
            "tests/fixtures/pr_body/boundary_unauthorized.md", "tests/fixtures/config/valid.json"
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("evidence_closeout", payload["blocking_failures"])
        gate = payload["gates"]["evidence_closeout"]
        self.assertEqual(gate["status"], "fail")
        messages = [f["message"] for f in gate["findings"]]
        self.assertTrue(
            any("production boundary" in m and "unauthorized" in m.lower() for m in messages),
            f"block must be caused by the unauthorized finding, got {messages}",
        )

    def test_required_context_missing_file_warns(self) -> None:
        # WS-8 P2-5b: a declared required_context_files entry that does not exist
        # under --repo must surface. README.md exists (must NOT be flagged); the
        # bogus path must be flagged. Default warn mode → returncode 0.
        proc, payload = self.run_adapter(
            "tests/fixtures/pr_body/valid.md", "tests/fixtures/config/required_context_missing.json"
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        gate = payload["gates"]["required_context"]
        self.assertEqual(gate["mode"], "warn")
        self.assertEqual(gate["status"], "fail")
        messages = [f["message"] for f in gate["findings"]]
        self.assertTrue(any("__aqg_ws8b_does_not_exist__" in m for m in messages), messages)
        self.assertFalse(any("README.md" in m for m in messages), f"present file must not be flagged: {messages}")

    def test_required_context_missing_blocks_under_blocking(self) -> None:
        proc, payload = self.run_adapter(
            "tests/fixtures/pr_body/valid.md", "tests/fixtures/config/required_context_blocking.json"
        )
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("required_context", payload["blocking_failures"])

    def test_required_context_all_present_passes(self) -> None:
        # Dedicated fixture declaring only README.md (present in the test --repo) →
        # clean pass. Isolated from the live default config so unrelated config or
        # tree edits cannot flip this test.
        proc, payload = self.run_adapter(
            "tests/fixtures/pr_body/valid.md", "tests/fixtures/config/required_context_present.json"
        )
        gate = payload["gates"]["required_context"]
        self.assertEqual(gate["status"], "pass", gate)
        self.assertEqual(gate["findings"], [])

    def test_required_context_omitted_config_skips_gate(self) -> None:
        # A config with no gates.required_context block defaults the gate to
        # disabled → skipped, so existing configs are unaffected.
        proc, payload = self.run_adapter("tests/fixtures/pr_body/valid.md", "tests/fixtures/config/valid.json")
        self.assertEqual(payload["gates"]["required_context"]["status"], "skipped")

    def test_invalid_config_returns_3(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/valid.md", "tests/fixtures/config/invalid.json")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(payload, {})

    def test_unknown_boundary_config_returns_3(self) -> None:
        proc, payload = self.run_adapter("tests/fixtures/pr_body/valid.md", "tests/fixtures/config/unknown_boundary.json")
        self.assertEqual(proc.returncode, 3)
        self.assertEqual(payload, {})

    def test_missing_pr_body_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    "examples/quality-gates.json",
                    "--repo",
                    ".",
                    "--pr-body-file",
                    str(Path(tmp_s) / "missing.md"),
                    "--output-json",
                    str(Path(tmp_s) / "out.json"),
                ]
            )
        self.assertEqual(proc.returncode, 2)

    def test_output_json_directory_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    "examples/quality-gates.json",
                    "--repo",
                    ".",
                    "--pr-body-file",
                    "tests/fixtures/pr_body/valid.md",
                    "--output-json",
                    tmp_s,
                ]
            )
        self.assertEqual(proc.returncode, 2)

    def test_missing_repo_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    "examples/quality-gates.json",
                    "--repo",
                    str(Path(tmp_s) / "missing-repo"),
                    "--pr-body-file",
                    "tests/fixtures/pr_body/valid.md",
                    "--output-json",
                    str(Path(tmp_s) / "out.json"),
                ]
            )
        self.assertEqual(proc.returncode, 2)

    def test_redaction_counts_secret_like_value_without_storing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            body = Path(tmp_s) / "body.md"
            output = Path(tmp_s) / "out.json"
            fake_secret = "ghp_" + ("A" * 24)
            fake_openai = "sk-proj-" + ("A" * 24)
            body.write_text(
                "# Summary\n\n"
                f"token: {fake_secret}\n\n"
                f"openai: {fake_openai}\n\n"
                "## Evidence Block\n\n"
                "| item | evidence |\n"
                "|---|---|\n"
                "| scope completed | temp fixture |\n"
                "| verification run | local |\n"
                "| audit adjudicated | audit-skip: docs-only |\n"
                "| durable state updated | temp only |\n"
                "| production boundary | status: not-touched |\n"
                "| secrets boundary | status: not-touched |\n"
                "| raw private data boundary | status: not-touched |\n"
                "| remaining blockers | none |\n",
                encoding="utf-8",
            )
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    "examples/quality-gates.json",
                    "--repo",
                    ".",
                    "--pr-body-file",
                    str(body),
                    "--output-json",
                    str(output),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            text = output.read_text()
            payload = json.loads(text)
            self.assertEqual(payload["pr_body"]["redaction"]["secret_like_match_counts"]["github_token"], 1)
            self.assertEqual(payload["pr_body"]["redaction"]["secret_like_match_counts"]["openai_key"], 1)
            self.assertNotIn(fake_secret, text)
            self.assertNotIn(fake_openai, text)
            self.assertNotIn(tmp_s, text)


class BoundaryStatusNormalizationTest(unittest.TestCase):
    """WS-8 P2-5a: no decorated / mixed-case / typo boundary status may clear the
    gate silently. This locks the normalize_status_value + invalid-status catch-all
    contract directly so a future refactor of either cannot re-open the fail-open."""

    @staticmethod
    def _validate(prod_status: str) -> list[str]:
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import run_quality_gates as rqg
        finally:
            sys.path.pop(0)
        config = {
            "boundaries": {
                "production": {"require_explicit_statement": True},
                "secrets": {"require_explicit_statement": True},
                "raw_private_data": {"require_explicit_statement": True},
            }
        }
        body = (
            "## Evidence Block\n\n| item | evidence |\n|---|---|\n"
            f"| production boundary | {prod_status} |\n"
            "| secrets boundary | status: not-touched |\n"
            "| raw private data boundary | status: not-touched |\n"
        )
        return [
            f["message"]
            for f in rqg.validate_boundaries(config, body)
            if "production boundary" in f["message"]
        ]

    def test_no_unauthorized_form_clears_the_gate_silently(self) -> None:
        # Every one of these must produce SOME production-boundary finding: an
        # empty list here is a fail-open (the exact bug WS-8 P2-5a closes).
        for form in (
            "status: unauthorized",
            "status: Unauthorized",
            "status: UNAUTHORIZED",
            "status: unauthorized (owner escalated)",
            "unauthorized",
            "status: **unauthorized**",
            "status: `unauthorized`",
            "status: unauthorzed",
        ):
            with self.subTest(form=form):
                self.assertTrue(self._validate(form), f"fail-open: {form!r} produced no finding")

    def test_case_variants_still_recognized_as_unauthorized(self) -> None:
        # Mixed/upper case must land on the unauthorized branch (not merely the
        # invalid-status catch-all) — proves case-normalization keeps the branch live.
        for form in ("status: Unauthorized", "status: UNAUTHORIZED"):
            with self.subTest(form=form):
                msgs = self._validate(form)
                self.assertTrue(any("unauthorized" in m.lower() and "declared" in m for m in msgs), msgs)

    def test_not_touched_and_authorized_stay_clean(self) -> None:
        # Criterion (4): the branch must not make legitimate declarations noisy.
        self.assertEqual(self._validate("status: not-touched"), [])
        self.assertEqual(self._validate("status: authorized; auth_ref=OWNER-2026-07-11"), [])


class RequiredFilePresentTest(unittest.TestCase):
    """WS-8 P2-5b: `required_file_present` must enforce in-repo containment, not
    just reject absolute paths. Locks the escape guard (absolute / `..` / symlink /
    directory) so a future refactor cannot silently reopen it."""

    @staticmethod
    def _present(repo: Path, rel: str) -> bool:
        sys.path.insert(0, str(REPO / "scripts"))
        try:
            import run_quality_gates as rqg
        finally:
            sys.path.pop(0)
        return rqg.required_file_present(repo, rel)

    def test_in_repo_file_present(self) -> None:
        self.assertTrue(self._present(REPO, "README.md"))

    def test_directory_is_not_a_context_file(self) -> None:
        # `docs` exists but is a directory; required_context_FILES → must be absent.
        self.assertFalse(self._present(REPO, "docs"))

    def test_missing_file_absent(self) -> None:
        self.assertFalse(self._present(REPO, "docs/__aqg_ws8b_missing__.md"))

    def test_absolute_path_outside_repo_absent(self) -> None:
        # An absolute path to a real file OUTSIDE the repo must not count as present.
        with tempfile.NamedTemporaryFile(suffix=".md") as tf:
            self.assertFalse(self._present(REPO, tf.name))

    def test_parent_traversal_escape_absent(self) -> None:
        # A `..` path that resolves to a real file OUTSIDE the repo must not pass —
        # the old absolute-only guard would have let this through.
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside.md"
            outside.write_text("x", encoding="utf-8")
            rel = os.path.relpath(outside, REPO)
            self.assertTrue(rel.startswith(".."), rel)
            self.assertFalse(self._present(REPO, rel), rel)

    def test_symlink_escaping_repo_absent(self) -> None:
        # An in-repo symlink whose target is outside the repo resolves outside →
        # must not count as a present in-repo context file.
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "target.md"
            outside.write_text("x", encoding="utf-8")
            with tempfile.TemporaryDirectory(dir=REPO) as repo_sub:
                link = Path(repo_sub) / "link.md"
                link.symlink_to(outside)
                rel = os.path.relpath(link, REPO)
                self.assertFalse(self._present(REPO, rel), rel)


class AuditConverterTest(unittest.TestCase):
    def test_converter_extracts_valid_table(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            output = Path(tmp_s) / "adjudication.md"
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/audit_to_adjudication_table.py",
                    "--audit-output",
                    "tests/fixtures/audit_output/valid.md",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            check = run_cmd([sys.executable, "scripts/validate_audit_adjudication.py", str(output)])
            self.assertEqual(check.returncode, 0, check.stdout + check.stderr)

    def test_converter_handles_metadata_table_before_adjudication(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            output = Path(tmp_s) / "adjudication.md"
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/audit_to_adjudication_table.py",
                    "--audit-output",
                    "tests/fixtures/audit_output/multiple_tables.md",
                    "--output",
                    str(output),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_converter_empty_input_returns_1(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/audit_to_adjudication_table.py",
                    "--audit-output",
                    "tests/fixtures/audit_output/empty.md",
                    "--output",
                    str(Path(tmp_s) / "adjudication.md"),
                ]
            )
            self.assertEqual(proc.returncode, 1)

    def test_converter_missing_input_returns_2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/audit_to_adjudication_table.py",
                    "--audit-output",
                    str(Path(tmp_s) / "missing.md"),
                    "--output",
                    str(Path(tmp_s) / "adjudication.md"),
                ]
            )
        self.assertEqual(proc.returncode, 2)


class GitHubPrAdapterScriptsTest(unittest.TestCase):
    def test_fetch_pr_body_skips_non_pr_event_without_output_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            gh_output = tmp / "github-output.txt"
            summary = tmp / "summary.md"
            event.write_text('{"repository":{"full_name":"owner/repo"}}', encoding="utf-8")
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "push",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--github-output",
                    str(gh_output),
                    "--summary",
                    str(summary),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(body.exists())
            self.assertIn("skipped=true", gh_output.read_text(encoding="utf-8"))
            self.assertIn("non-PR event push", summary.read_text(encoding="utf-8"))

    def test_fetch_pr_body_uses_gh_without_printing_raw_body(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            fake_gh = tmp / "gh"
            raw_body = "Fetched body from fake gh"
            event.write_text(
                '{"number":7,"pull_request":{"number":7},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"body\":\"Fetched body from fake gh\\\\n\\\\n## Evidence Block\"}'\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--gh",
                    str(fake_gh),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn(raw_body, body.read_text(encoding="utf-8"))
            self.assertNotIn(raw_body, proc.stdout)
            self.assertNotIn(raw_body, proc.stderr)

    def test_fetch_pr_body_skips_gracefully_when_gh_fails(self) -> None:
        """A transient gh failure (GraphQL 401 / network) feeding the WARN-ONLY gate must
        DEGRADE to a visible skip (exit 0, skipped=true), not crash the job into a
        hard-red check (Owner 2026-06-10: a flapping GitHub GraphQL 401 reded the gate)."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            gh_output = tmp / "github-output.txt"
            summary = tmp / "summary.md"
            fake_gh = tmp / "gh"
            event.write_text(
                '{"number":7,"pull_request":{"number":7},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            # `gh pr view` hits a GraphQL 401: exit non-zero with the error on stderr.
            fake_gh.write_text(
                "#!/bin/sh\n"
                "echo 'HTTP 401: Requires authentication (https://api.github.com/graphql)' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--github-output",
                    str(gh_output),
                    "--summary",
                    str(summary),
                    "--gh",
                    str(fake_gh),
                ]
            )
            # Graceful skip, NOT a crash (the bug was EXIT_INTERNAL / exit 70).
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertFalse(body.exists(), "no body file should be written on a fetch failure")
            out = gh_output.read_text(encoding="utf-8")
            self.assertIn("skipped=true", out)
            self.assertIn("could not fetch PR body", out)
            # The skip reason is surfaced in the job summary (visible, not silent).
            self.assertIn("Skipped:", summary.read_text(encoding="utf-8"))

    def test_fetch_pr_body_skip_reason_cannot_inject_github_output(self) -> None:
        """Audit a786236c (convergent): gh stderr flows into skip_reason -> GITHUB_OUTPUT.
        A multiline stderr smuggling `skipped=false` must NOT win over the real
        skipped=true (newlines collapsed at the source + by append_github_output), else it
        would defeat the downstream `skipped != 'true'` guard the whole fix relies on."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            gh_output = tmp / "github-output.txt"
            fake_gh = tmp / "gh"
            event.write_text(
                '{"number":7,"pull_request":{"number":7},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            # gh stderr tries to smuggle a second GITHUB_OUTPUT line.
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf 'boom\\nskipped=false\\n' >&2\n"
                "exit 1\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--github-output",
                    str(gh_output),
                    "--gh",
                    str(fake_gh),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            out = gh_output.read_text(encoding="utf-8")
            # Parse key=value (last write wins, mirroring GitHub Actions) — the smuggled
            # skipped=false must not have landed as its own output line.
            kv: dict[str, str] = {}
            for line in out.splitlines():
                if "=" in line:
                    key, val = line.split("=", 1)
                    kv[key] = val
            self.assertEqual(kv.get("skipped"), "true", f"smuggled skipped=false must not win: {out!r}")

    def test_fetch_pr_body_warns_on_secret_like_content(self) -> None:
        """P1-2: a PR body containing secret-like content should trigger a stderr WARN + step summary notice,
        but still exit 0 without blocking, and the raw secret must not reach stdout/stderr."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            gh_output = tmp / "github-output.txt"
            summary = tmp / "summary.md"
            fake_gh = tmp / "gh"
            event.write_text(
                '{"number":7,"pull_request":{"number":7},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            fake_token = "ghp_" + "A" * 30
            fake_gh.write_text(
                "#!/bin/sh\n"
                f"printf '%s\\n' '{{\"body\":\"see token {fake_token}\"}}'\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--gh",
                    str(fake_gh),
                    "--github-output",
                    str(gh_output),
                    "--summary",
                    str(summary),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("WARN", proc.stderr)
            self.assertIn("github_token=1", proc.stderr)
            # the raw token must not be printed to stdout/stderr, only in the persisted PR body file
            self.assertNotIn(fake_token, proc.stdout)
            self.assertNotIn(fake_token, proc.stderr)
            self.assertIn("secret_like_total=1", gh_output.read_text(encoding="utf-8"))
            self.assertIn("github_token=1", summary.read_text(encoding="utf-8"))
            # the PR body file keeps the original text (hash must be based on the original bytes; downstream decides the redact policy)
            self.assertIn(fake_token, body.read_text(encoding="utf-8"))

    def test_fetch_pr_body_no_secret_does_not_warn(self) -> None:
        """P1-2: with no secret there should be no WARN + secret_like_total=0."""
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "body.md"
            gh_output = tmp / "github-output.txt"
            fake_gh = tmp / "gh"
            event.write_text(
                '{"number":7,"pull_request":{"number":7},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '{\"body\":\"clean body without tokens\"}'\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--output",
                    str(body),
                    "--gh",
                    str(fake_gh),
                    "--github-output",
                    str(gh_output),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertNotIn("WARN", proc.stderr)
            self.assertIn("secret_like_total=0", gh_output.read_text(encoding="utf-8"))

    def test_fetch_pr_body_rejects_invalid_repo_slug(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            event.write_text('{"number":7,"pull_request":{"number":7}}', encoding="utf-8")
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/fetch_pr_body.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--repo",
                    "--bad-repo",
                    "--output",
                    str(tmp / "body.md"),
                ]
            )
            self.assertEqual(proc.returncode, 2)

    def test_render_pr_comment_uses_redacted_json_and_bounded_markers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            json_path = tmp / "quality-gates.json"
            comment = tmp / "comment.md"
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/run_quality_gates.py",
                    "--config",
                    "examples/quality-gates.json",
                    "--repo",
                    ".",
                    "--pr-body-file",
                    "tests/fixtures/pr_body/valid.md",
                    "--output-json",
                    str(json_path),
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            render = run_cmd(
                [
                    sys.executable,
                    "scripts/render_pr_comment.py",
                    "--input-json",
                    str(json_path),
                    "--output",
                    str(comment),
                ]
            )
            self.assertEqual(render.returncode, 0, render.stdout + render.stderr)
            text = comment.read_text(encoding="utf-8")
            self.assertIn("<!-- aqg:quality-gates-comment:start -->", text)
            self.assertIn("<!-- aqg:quality-gates-comment:end -->", text)
            self.assertIn("Raw PR body text is intentionally excluded", text)
            self.assertNotIn("Implemented local AQG fixture behavior", text)

    def test_render_pr_comment_requires_redaction_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            bad_json = Path(tmp_s) / "bad.json"
            bad_json.write_text('{"ok": true, "pr_body": {"sha256": "abc", "bytes": 1}, "gates": {}}', encoding="utf-8")
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/render_pr_comment.py",
                    "--input-json",
                    str(bad_json),
                    "--output",
                    str(Path(tmp_s) / "comment.md"),
                ]
            )
            self.assertEqual(proc.returncode, 70)
            self.assertIn("missing pr_body.redaction", proc.stderr)

    def test_post_pr_comment_dry_run_updates_existing_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            event = tmp / "event.json"
            body = tmp / "comment.md"
            fake_gh = tmp / "gh"
            event.write_text(
                '{"number":8,"pull_request":{"number":8},"repository":{"full_name":"owner/repo"}}',
                encoding="utf-8",
            )
            body.write_text(
                "<!-- aqg:quality-gates-comment:start -->\n"
                "### Agent Quality Gates\n"
                "redacted summary\n"
                "Raw PR body text is intentionally excluded from this comment and from the JSON artifact.\n"
                "<!-- aqg:quality-gates-comment:end -->\n",
                encoding="utf-8",
            )
            fake_gh.write_text(
                "#!/bin/sh\n"
                "printf '%s\\n' '[{\"id\":123,\"body\":\"<!-- aqg:quality-gates-comment:start -->old<!-- aqg:quality-gates-comment:end -->\"}]'\n",
                encoding="utf-8",
            )
            fake_gh.chmod(0o755)
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/post_pr_comment.py",
                    "--event-name",
                    "pull_request",
                    "--event-path",
                    str(event),
                    "--body-file",
                    str(body),
                    "--repo",
                    "owner/repo",
                    "--gh",
                    str(fake_gh),
                    "--dry-run",
                ]
            )
            self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
            self.assertIn("would update AQG comment 123", proc.stdout)
            self.assertNotIn("redacted summary", proc.stdout)

    def test_post_pr_comment_rejects_empty_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_s:
            tmp = Path(tmp_s)
            body = tmp / "comment.md"
            body.write_text(
                "<!-- aqg:quality-gates-comment:start -->\n"
                "Raw PR body text is intentionally excluded from this comment and from the JSON artifact.\n"
                "<!-- aqg:quality-gates-comment:end -->\n",
                encoding="utf-8",
            )
            proc = run_cmd(
                [
                    sys.executable,
                    "scripts/post_pr_comment.py",
                    "--body-file",
                    str(body),
                    "--repo",
                    "owner/repo",
                    "--pr-number",
                    "1",
                    "--marker-start",
                    "",
                    "--dry-run",
                ]
            )
            self.assertEqual(proc.returncode, 2)
            self.assertIn("must be non-empty", proc.stderr)


if __name__ == "__main__":
    unittest.main()
