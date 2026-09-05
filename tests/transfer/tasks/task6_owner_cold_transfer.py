"""Task 6 — Owner cold-transfer reproduction (deterministic schema extraction).

v1 implementation: artifact → schema field extraction → equality check
against Owner-curated golden file. NOT live-LLM cold transfer (deferred
to v1.1 per §9 + Q5).

Input fixture format (`owner_transfer_input.md`): markdown with YAML
frontmatter exposing the schema-defined fields the cold operator needs:

    ---
    next_action: <imperative one-line action a cold operator would take>
    blockers:
      - <blocker 1>
      - <blocker 2>
    ---

    # Free-form prose describing the prior session's terminal state ...

Golden file format (`owner_transfer_golden.yaml`):

    next_action: <expected string>
    blockers:
      - <expected blocker 1>
      - <expected blocker 2>

Pass criteria (§3 Task 6): extracted `next_action` string equality +
`blockers[]` set equality (order-independent) against golden.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from runner import TaskOutcome  # noqa: E402


_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _extract_frontmatter(text: str) -> dict[str, Any] | None:
    m = _FRONTMATTER_RE.match(text.replace("\r\n", "\n"))
    if not m:
        return None
    try:
        import yaml
    except ImportError:
        return None
    try:
        loaded = yaml.safe_load(m.group(1))
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    return loaded


def _load_yaml(path: Path) -> Any:
    import yaml
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def run(repo: Path) -> TaskOutcome:
    transfer = repo / "tests" / "transfer"
    input_path = transfer / "fixtures" / "owner_transfer_input.md"
    golden_path = transfer / "fixtures" / "owner_transfer_golden.yaml"

    checks_total = 2  # next_action equality + blockers[] set equality

    if not input_path.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=f"owner_transfer_input.md missing at {input_path}",
        )
    if not golden_path.is_file():
        return TaskOutcome(
            result="fail",
            exit_code=2,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=f"owner_transfer_golden.yaml missing at {golden_path}",
        )

    try:
        input_text = input_path.read_text(encoding="utf-8")
    except OSError as exc:
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=f"failed to read input: {exc}",
        )

    frontmatter = _extract_frontmatter(input_text)
    if frontmatter is None:
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=(
                f"input lacks parseable YAML frontmatter; expected schema "
                f"with next_action + blockers[]"
            ),
        )

    extracted_next = frontmatter.get("next_action")
    extracted_blockers = frontmatter.get("blockers")
    if not isinstance(extracted_next, str):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=(
                f"input frontmatter.next_action: must be str, got "
                f"{type(extracted_next).__name__}"
            ),
        )
    if not isinstance(extracted_blockers, list):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=(
                f"input frontmatter.blockers: must be list, got "
                f"{type(extracted_blockers).__name__}"
            ),
        )

    try:
        golden = _load_yaml(golden_path)
    except Exception as exc:
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=f"failed to load golden: {exc}",
        )

    if not isinstance(golden, dict):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes="owner_transfer_golden.yaml: top-level must be mapping",
        )

    golden_next = golden.get("next_action")
    golden_blockers = golden.get("blockers", [])
    if not isinstance(golden_next, str) or not isinstance(golden_blockers, list):
        return TaskOutcome(
            result="fail",
            exit_code=1,
            required_checks_passed=0,
            required_checks_total=checks_total,
            notes=(
                f"golden file malformed: next_action={type(golden_next).__name__}, "
                f"blockers={type(golden_blockers).__name__}"
            ),
        )

    checks_passed = 0
    notes_lines: list[str] = []

    if extracted_next.strip() == golden_next.strip():
        checks_passed += 1
        notes_lines.append("next_action: equal")
    else:
        notes_lines.append(
            f"next_action mismatch: extracted={extracted_next!r}, "
            f"golden={golden_next!r}"
        )

    extracted_set = {str(b).strip() for b in extracted_blockers if isinstance(b, str)}
    golden_set = {str(b).strip() for b in golden_blockers if isinstance(b, str)}
    if extracted_set == golden_set:
        checks_passed += 1
        notes_lines.append(f"blockers[] equal (n={len(golden_set)})")
    else:
        notes_lines.append(
            f"blockers[] mismatch: extracted={sorted(extracted_set)}, "
            f"golden={sorted(golden_set)}"
        )

    if checks_passed == checks_total:
        return TaskOutcome(
            result="pass",
            exit_code=0,
            required_checks_passed=checks_passed,
            required_checks_total=checks_total,
            notes="; ".join(notes_lines),
        )
    return TaskOutcome(
        result="fail",
        exit_code=1,
        required_checks_passed=checks_passed,
        required_checks_total=checks_total,
        notes="; ".join(notes_lines),
    )
