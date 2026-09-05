"""Shared fixture builders for aqg-memory-hygiene tests.

Writes synthetic memory-node `.md` files into a tmp dir so behavior tests can
exercise validate / staleness against a real on-disk corpus (never the user's
real ~/.claude memory). stdlib only.
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))


def compliant_meta(**override) -> dict:
    """A minimal schema-compliant metadata block. last_verified is far in the
    past so it is valid (<= today) on any realistic run date."""
    meta = {
        "type": "feedback",
        "status": "active",
        "volatility": "durable",
        "last_verified": "2020-01-01",
    }
    meta.update(override)
    return meta


def write_node(
    d: Path,
    filename: str,
    *,
    name: str | None = None,
    description: str = "a synthetic fixture node",
    metadata: dict | None = None,
    body: str = "body prose",
    raw: str | None = None,
) -> Path:
    """Write a memory-node markdown file.

    If ``raw`` is given it is written verbatim (used for malformed / no-frontmatter
    cases). Otherwise a well-formed `---` frontmatter block is emitted with the
    given metadata mapping (``None`` values render as YAML ``null``).
    """
    p = d / filename
    if raw is not None:
        p.write_text(raw, encoding="utf-8")
        return p
    node_name = name if name is not None else filename[:-3] if filename.endswith(".md") else filename
    lines = ["---", f"name: {node_name}", f"description: {description}", "metadata:"]
    for key, value in (metadata or {}).items():
        if value is None:
            lines.append(f"  {key}: null")
        else:
            lines.append(f"  {key}: {value}")
    lines.append("---")
    lines.append("")
    lines.append(body)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_index(d: Path, filename: str = "MEMORY.md") -> Path:
    """Write a non-node index file (no node frontmatter) — must be excluded."""
    p = d / filename
    p.write_text(
        "# Memory Index — sample\n\n## Feedback\n- [x](feedback_x.md) — hook\n",
        encoding="utf-8",
    )
    return p


def run_cli(argv: list[str]) -> tuple[int, str]:
    """Invoke the entry script's main() in-process, capturing stdout."""
    import aqg_memory_hygiene as mh

    out = io.StringIO()
    with redirect_stdout(out):
        rc = mh.main(argv)
    return rc, out.getvalue()
