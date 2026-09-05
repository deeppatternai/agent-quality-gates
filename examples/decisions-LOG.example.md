# Decision Log (example)

> **Sample file.** A sanitized, illustrative `docs/decisions/LOG.md` you can copy into a
> project to adopt AQG's lightweight decision capture. Every entry below is fictional and
> exists only to show the format — replace them with your own. Generate validated rows with
> the `aqg-decision-capture` skill (`format` to preview, `commit` to append); it runs each
> line through the same grammar + secret-scan gate, fail-closed, append-only.

## What to record

A **durable-impact** decision you could NOT reconstruct from code or commits alone:
terminology, a process preference, an authorization boundary, a product trade-off, an
architecture/contract choice, or an external-audit adjudication.

**Skip** pure implementation details (they live in the diff) and one-off choices with no
lasting effect — recording those just adds noise.

## Format

One line, **6 positional fields** (no inline labels):

```
date | actor | decision | rationale | basis | supersedes
```

- `actor` ∈ `{owner, agent, eaf}` — the decider. An auditor is **not** an actor; it goes in `basis`.
- `basis` — pointer(s) to the evidence, comma-joined. Durable: `PR#<n>` / `commit:<sha7-12>` /
  `ADR:<slug>`. Short-lived: `audit:<id>`. Or the none-token `-`. An `actor=agent` row must
  carry **≥1 durable pointer** (a format-level rule — existence is not verified, but a
  fabricated pointer names an external artifact and is caught in review).
- Free-text fields must not contain a raw `|` (it breaks the table) — reword instead
  (e.g. "use A or B", not "A|B").
- `supersedes` — `<date>#<slug>` of the row this overturns, or `-`. The `slug` is a short
  kebab-case tag you pick for a decision's topic (same discipline as an ADR slug, e.g.
  `tamper-guard`); it is **not** a seventh column — it lives only inside a `supersedes`
  pointer, and you only need one when a row may later be overturned. Match it to the older
  row by that row's topic.

**Append-only.** Overturning an earlier decision means adding a new row marked `supersedes`,
never editing or deleting the old one. Query with `aqg-decision-capture query` (read-time
redacted) rather than reading the whole file into context.

## Log

| date | actor | decision | rationale | basis | supersedes |
|---|---|---|---|---|---|
| 2026-01-05 | owner | Standardize on review depth fast / standard / deep | Retire the older single, two and three names so docs and tooling share one vocabulary | ADR:audit-depth-naming | - |
| 2026-01-05 | owner | Production secrets and branch protection stay Owner-only | Agents may surface state but must not mutate the release boundary | - | - |
| 2026-01-08 | owner | Tamper guard ships opt-in, not installed by default | Avoid surprising adopters with an enforced gate on first install | - | - |
| 2026-01-12 | agent | Store the roster in one manifest and generate every count from it | A single source removes the count-drift bug class seen when a hardcoded list fell out of sync | PR#128 | - |
| 2026-01-14 | agent | Accept the external finding and harden the write guard against symlink escape | A cross-vendor audit reproduced the bypass; fix plus regression test landed before merge | audit:a1b2c3d4, PR#131 | - |
| 2026-01-18 | eaf | Route higher-risk work packets through a second reviewer before merge | The extra pass caught a class of contract regressions the single reviewer missed | commit:9f3c1a2 | - |
| 2026-01-20 | owner | Keep trigger-word matching in the author's own language | The non-English match is deliberate; only user-facing output gets translated | - | - |
| 2026-02-02 | owner | Install the tamper guard by default with a documented opt-out | Default-off left the gate unenforced in practice; keep an escape hatch for human-driven work | ADR:tamper-guard-default | 2026-01-08#tamper-guard |
