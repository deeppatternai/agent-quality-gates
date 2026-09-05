---
name: aqg-memory-hygiene
description: Scan machine-local agent memory files for frontmatter schema compliance and staleness — the at-rest layer of memory hygiene, signal-only and read-only. The validate subcommand is a strict gate over each memory node's metadata frontmatter (type / status / volatility / last_verified enums, no node_type residue, superseded-pointer integrity); the staleness subcommand surfaces volatile memories whose last_verified is past a configurable horizon (default 90 days, env AQG_MEMORY_STALE_DAYS). Emits a structured report plus an exit-code gate; never mutates memory, calls audit-mcp, or touches the network — fixing stays the caller's action. Use to catch memory schema drift, an invalid or future-dated last_verified, a broken superseded pointer, or stale volatile memories needing re-verification. Complements the write-time memory-write-guard hook and the memory-hygiene discipline. Not a memory editor or coverage gate.
---

# AQG Memory Hygiene (signal-only)

At-rest scanner for the lifecycle of machine-local agent memory. Reads a memory
dir, classifies each node's frontmatter, and emits a structured report — it never
writes a memory file and never calls audit-mcp. The actual normalize / supersede
is the **caller's** action (the skill surfaces; the caller fixes).

## Three-layer memory governance (this is the middle layer)

| layer | tool | when / role |
|---|---|---|
| write-time **enforcement** | memory-write-guard hook (#162) | BLOCKS code-shaped content at write time |
| **lifecycle scanner (this)** | **aqg-memory-hygiene** | **audits schema compliance + staleness at rest (surfaces, does not block)** |
| discipline | `rules/common/memory-hygiene.md` | decides WHAT belongs in memory (user / feedback / reference / sparse project background) |

## What `validate` checks (strict gate — ADR §4.3)

Index / non-node files (`MEMORY.md`, `README.md`) are excluded first, then every
remaining node `.md` must satisfy: frontmatter present + parseable · `metadata` is
a nested mapping (a flat top-level `type:` is rejected) · required fields
`type` / `status` / `volatility` / `last_verified` present · those enums legal ·
no redundant `node_type` · `last_verified` a valid `YYYY-MM-DD` not in the future ·
`status: superseded` ⇒ all three `superseded_*` present and `superseded_by`
resolves (a bare slug must point to ANOTHER sibling node; a `repo:<path>` is
warn-only unless it traverses `..`; `null` = pure retirement) · `status: active`
carries no `superseded_*`.

## What `staleness` flags (soft signal — ADR §4.4)

`volatile` nodes whose `last_verified` is older than the threshold (default 90d,
`--days` / env `AQG_MEMORY_STALE_DAYS`). `durable` is never gated; `superseded` is
skipped; unparseable / missing-`last_verified` nodes graceful-skip with a note.
Always exits 0 (surface, never a gate).

## How To Run

```bash
source "${AQG_ROOT:-$HOME/.deeppattern/agent-quality-gates}/scripts/_aqg_context.sh" || {
  echo "ERROR: cannot resolve AQG root. Fix: export AQG_ROOT=/path/to/checkout." >&2
  exit 2
}
script="$aqg_root/skills/aqg-memory-hygiene/scripts/aqg_memory_hygiene.py"

# 1. validate frontmatter schema (strict gate; non-zero exit on any violation)
python3 "$script" validate                         # auto-discovers the current project's memory dir
python3 "$script" validate --memory-dir /path/to/memory --json

# 2. list stale volatile memories (always exit 0)
python3 "$script" staleness --days 60
python3 "$script" staleness --memory-dir /path/to/memory --json
```

Needs `pip install pyyaml>=6.0` (a DECLARED AQG runtime dependency, not stdlib) to
parse frontmatter. Exit codes: `0` ok / `1` validate found a schema violation /
`2` usage error or PyYAML unavailable.

## Boundaries

- **read-only / signal-only**: parses a memory dir and emits a report. Does NOT
  modify, normalize, supersede, or delete any memory file (verified by a
  zero-write fs-snapshot invariant test), does NOT call audit-mcp, does NOT touch
  the network. Decisions are deterministic rules (schema enums + date arithmetic).
- **caller acts**: normalize / supersede / re-verify is the caller's (human or
  Claude) action — the skill provides the verdict, not the edit.
- Production, secrets, raw private data, branch protection, and Owner/admin
  actions remain separate authorization gates.

## When to use vs alternatives

- **block code-shaped content from entering memory** → the write-guard hook (#162), not this
- **decide what belongs in memory at all** → `rules/common/memory-hygiene.md`
- **audit existing memory's schema + freshness at rest** → **this skill**
- Phase B (write-time conflict detection / infra-identifier-shape gate) is deferred
  (ADR §3 N5) — not in this skill.
