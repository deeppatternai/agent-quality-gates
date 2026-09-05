"""Regression guard for shipped CI integration templates.

The files under ``examples/github-actions/`` and ``examples/gitlab-ci/`` are
SHIPPED reference templates that adopters copy into their own CI. A silent edit
that breaks YAML well-formedness, or introduces a shell bug in an embedded
``script:``/``run:`` block, ships a broken example that fails only in the
adopter's pipeline — never in ours. These tests lock that contract:

  * every template parses as well-formed YAML;
  * every embedded *bash* shell block (GitHub Actions ``run:`` without a
    non-bash ``shell:`` + GitLab top-level / ``default:`` / per-job
    ``before_script``/``script``/``after_script``) passes
    ``shellcheck --shell=bash -S warning``.

``--shell=bash`` is required: the blocks carry no shebang (CI states the shell
out of band), so without it shellcheck emits SC2148. bash is what GitHub
Actions and a Debian-based GitLab image run these under.

SCOPE — deliberately bounded (audit b374d2aa):
  * This is a *syntax + shell-lint* guard, NOT a platform-schema validator. A
    file that is well-formed YAML but structurally invalid for the platform
    (mistyped ``steps``/``stage``, bad ``uses`` ref, broken ``needs``/``rules``)
    is NOT caught here. ``actionlint`` / ``gitlab-ci lint`` would cover that and
    are a possible future extension; kept out of scope to stay dependency-free.
  * Shell factored into ``include:``/``extends:`` files or a local composite
    ``action.yml`` is out of scope — only inline blocks in the shipped template
    files are walked.
  * Non-bash ``run:`` blocks (a step's ``shell: pwsh``/``python``…) are skipped
    rather than mis-linted as bash.
  * Each shipped template is currently expected to carry >=1 shell block; the
    shell test asserts extraction is non-empty, which also catches a GHA
    ``steps``->``step`` typo collapsing to zero segments.

shellcheck is not guaranteed in every environment, so the shell test SKIPS
(does not error) when the binary is absent — the same ``shutil.which`` pattern
used elsewhere in ``tests/behavior/``.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"

_SHELLCHECK_TIMEOUT_S = 30
_BASH_SHELLS = {"bash", "sh"}  # shells `shellcheck --shell=bash` can soundly lint


def _discover(subdir: str) -> list[Path]:
    """Every YAML template under ``examples/<subdir>``, recursively, both exts.

    Recursive + ``.yml``/``.yaml`` matches the breadth of the workflow paths
    filter (``examples/<subdir>/**``), so a template added as ``.yaml`` or in a
    subdirectory cannot trigger CI yet stay invisible to this guard
    (audit b374d2aa f1 — convergent claude/gpt/grok).
    """
    root = EXAMPLES / subdir
    return sorted(set(root.rglob("*.yml")) | set(root.rglob("*.yaml")))


GITHUB_ACTIONS = _discover("github-actions")
GITLAB_CI = _discover("gitlab-ci")
ALL_TEMPLATES = GITHUB_ACTIONS + GITLAB_CI

_HAS_SHELLCHECK = shutil.which("shellcheck") is not None


def test_templates_discovered() -> None:
    """Guard against path drift silently emptying the parametrize sets.

    An empty ``parametrize`` set makes pytest report the test as skipped, i.e.
    a false green — so if a future move renames the dirs, this fails loudly.
    """
    assert GITHUB_ACTIONS, "no github-actions templates found under examples/ — path drift?"
    assert GITLAB_CI, "no gitlab-ci templates found under examples/ — path drift?"


@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda p: p.name)
def test_template_is_well_formed_yaml(template: Path) -> None:
    with template.open(encoding="utf-8") as fh:
        yaml.safe_load(fh)  # raises yaml.YAMLError on malformed input


def _gha_segments(doc: dict) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for job_id, job in (doc.get("jobs") or {}).items():
        if not isinstance(job, dict):
            continue
        for idx, step in enumerate(job.get("steps") or []):
            if not isinstance(step, dict):
                continue
            run = step.get("run")
            if not (isinstance(run, str) and run.strip()):
                continue
            shell = step.get("shell")
            # Skip a non-bash step rather than mis-lint it as bash: a
            # `shell: pwsh`/`python` block linted as bash is a false result
            # either way (audit b374d2aa f2).
            if shell is not None and shell not in _BASH_SHELLS:
                continue
            segments.append((f"jobs.{job_id}.steps[{idx}]", run))
    return segments


def _gitlab_script_keys(label: str, body: dict) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    for key in ("before_script", "script", "after_script"):
        seg = body.get(key)
        if seg is None:
            continue
        shell = "\n".join(seg) if isinstance(seg, list) else str(seg)
        if shell.strip():
            segments.append((f"{label}.{key}", shell))
    return segments


def _gitlab_segments(doc: dict) -> list[tuple[str, str]]:
    segments: list[tuple[str, str]] = []
    # Top-level global before_script/after_script are a list/scalar (not a job
    # mapping), so they were silently skipped by the job loop before
    # (audit b374d2aa f3 — gpt).
    for key in ("before_script", "after_script"):
        seg = doc.get(key)
        if seg is None:
            continue
        shell = "\n".join(seg) if isinstance(seg, list) else str(seg)
        if shell.strip():
            segments.append((f"<root>.{key}", shell))
    # `default:` block and every job mapping carry the same script keys.
    for name, body in doc.items():
        if isinstance(body, dict):
            segments.extend(_gitlab_script_keys(name, body))
    return segments


def _shell_segments(template: Path) -> list[tuple[str, str]]:
    """Return ``(label, shell_source)`` for every embedded bash shell block.

    GitHub Actions: ``jobs.<id>.steps[].run`` (skipping non-bash ``shell:``).
    GitLab CI: top-level + ``default:`` + per-job
    ``before_script``/``script``/``after_script``. Multi-line blocks arrive as
    a list of lines under a YAML block scalar; join them into one script.

    Dispatch is by directory for shipped templates and by the presence of a
    top-level ``jobs:`` mapping otherwise (so unit tests on tmp files work).
    """
    with template.open(encoding="utf-8") as fh:
        doc = yaml.safe_load(fh)
    if not isinstance(doc, dict):
        return []
    if template.parent.name == "github-actions" or isinstance(doc.get("jobs"), dict):
        return _gha_segments(doc)
    return _gitlab_segments(doc)


@pytest.mark.skipif(not _HAS_SHELLCHECK, reason="shellcheck not installed")
@pytest.mark.parametrize("template", ALL_TEMPLATES, ids=lambda p: p.name)
def test_embedded_shell_passes_shellcheck(template: Path, tmp_path: Path) -> None:
    segments = _shell_segments(template)
    assert segments, f"no shell segments extracted from {template.name} — schema drift?"
    for idx, (label, shell) in enumerate(segments):
        script = tmp_path / f"seg_{idx}.sh"
        script.write_text(shell, encoding="utf-8")
        result = subprocess.run(
            ["shellcheck", "--shell=bash", "-S", "warning", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=_SHELLCHECK_TIMEOUT_S,
        )
        # Surface BOTH streams: shellcheck writes lint findings to stdout but
        # invocation/internal errors (bad flag, crash) to stderr — a stdout-only
        # message would be empty and undiagnosable (audit b374d2aa f3/grok f1).
        assert result.returncode == 0, (
            f"{template.name} [{label}] failed `shellcheck --shell=bash -S warning`:\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


# --- unit tests for the schema-walking extractor (audit b374d2aa f8/grok) ----


def test_shell_segments_extracts_gha_run_skipping_non_bash(tmp_path: Path) -> None:
    template = tmp_path / "gha.yml"
    template.write_text(
        "jobs:\n"
        "  build:\n"
        "    steps:\n"
        "      - run: echo hi\n"
        "      - run: |\n"
        "          echo multi\n"
        "          echo line\n"
        "      - uses: actions/checkout@v4\n"  # no run -> skipped
        "      - run: Write-Host pwsh\n"
        "        shell: pwsh\n",  # non-bash -> skipped
        encoding="utf-8",
    )
    segments = _shell_segments(template)
    assert [label for label, _ in segments] == [
        "jobs.build.steps[0]",
        "jobs.build.steps[1]",
    ]
    assert "echo multi\necho line" in segments[1][1]


def test_shell_segments_extracts_gitlab_all_levels(tmp_path: Path) -> None:
    template = tmp_path / "gitlab.yml"
    template.write_text(
        "before_script:\n"  # top-level global (was silently skipped)
        "  - echo global-before\n"
        "default:\n"
        "  after_script:\n"
        "    - echo default-after\n"
        "myjob:\n"
        "  script: echo job-script\n"  # scalar form
        "variables:\n"  # dict without script keys -> nothing
        "  FOO: bar\n",
        encoding="utf-8",
    )
    assert sorted(label for label, _ in _shell_segments(template)) == [
        "<root>.before_script",
        "default.after_script",
        "myjob.script",
    ]
