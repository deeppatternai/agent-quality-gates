# AQG Incident Records

Markdown-based incident records under `docs/incidents/<date>-<slug>.md`. Generated and indexed by `scripts/aqg_incident_index.py`.

> **About the shipped records.** The records currently in `INDEX.md` are sanitized,
> `dev`-scope, `audit`-detected write-ups of real bugs AQG's own gate caught **before merge**
> — not production outages. They double as format samples. `audit_id` and `pr_url` are left
> empty on purpose: the private audit ids and internal PR numbers are redacted for public
> release, not missing. Each record's prose names the fix and its regression test.

## Boundary

- AQG own: incident record schema (`scripts/_incident_redaction.py`), template (`templates/incident_record_template.md`), record CLI + INDEX.md generator (`scripts/aqg_incident_index.py`)
- AQG **not** own: incident detection (project monitoring own), incident response (Owner / SRE own), runbook automation
- v0 scope: schema + redaction + record-write + index generation. **No** weekly retro clustering, search filter, severity escalation, or runbook hooks (those are v1+ and may live in a separate skill)

## Schema (v1)

Required top-level fields (HTML metadata block + body):

| field | type | constraint |
|---|---|---|
| `schema_version` | int | accepted: `{1}` |
| `slug` | str | `^[a-z0-9][a-z0-9._-]{0,79}$` |
| `date` | str | `YYYY-MM-DD` |
| `actor` | str | `^[a-z][a-z0-9._-]{0,40}$` |
| `title` | str | ≤ 100 chars, single-line, redaction-checked |
| `severity` | str | `P1` / `P2` / `P3` / `P4` |
| `detection_source` | str | `monitoring` / `user_report` / `scheduled_check` / `audit` / `manual` |
| `impact_scope` | str | `production` / `staging` / `internal` / `dev` |
| `summary` | str | ≤ 200 chars, single-line, redaction-checked |
| `root_cause` | str | ≤ 800 chars, multiline OK, redaction-checked |
| `resolution` | str | ≤ 800 chars, multiline OK, redaction-checked |
| `followups` | list[str] | ≤ 20 items, each ≤ 300 chars, redaction-checked |
| `boundaries` | str | ≤ 800 chars, multiline OK, redaction-checked |

Optional: `audit_id` (8-hex) / `pr_url` (GitHub URL) / `marker`.

## Redaction guard

Every string field is run through `scripts/_incident_redaction.py` before write. The guard rejects:

- absolute POSIX / Windows / UNC paths
- email addresses (PII)
- JWT-shape tokens / PEM private key markers
- token-prefix substrings (`ghp_` / `sk-` / `AKIA` / `xoxb-` / `AIza` etc.)
- inline URLs (use the dedicated `pr_url` field for GitHub links)
- long base64 (≥ 40 char) — possible token leak
- newlines / control chars in inline fields (markdown injection guard)

## Usage

### Create a record (LLM JSON mode)

```bash
echo '{
  "slug": "ci-flaky-2026-q1",
  "title": "CI flaky on construction-check",
  "severity": "P3",
  "detection_source": "scheduled_check",
  "impact_scope": "dev",
  "actor": "claude",
  "summary": "construction-check timed out 3 times in 24h on main",
  "root_cause": "...",
  "resolution": "...",
  "followups": ["bump timeout to 240s in CI workflow"],
  "boundaries": "production/deploy/restart: not touched; secrets/private: not touched; Owner action: none"
}' | python3 scripts/aqg_incident_index.py record --json
```

### Create a record (args mode)

```bash
python3 scripts/aqg_incident_index.py record \
  --slug ci-flaky-2026-q1 --title "CI flaky on construction-check" \
  --severity P3 --detection-source scheduled_check --impact-scope dev \
  --actor claude \
  --summary "construction-check timed out 3 times in 24h on main" \
  --root-cause "..." --resolution "..." \
  --boundaries "production: not touched; secrets: not touched"
```

Output: `docs/incidents/2026-05-04-ci-flaky-2026-q1.md` (auto-suffix `-aN.md` if file exists; `--force` to overwrite).

### Generate INDEX.md

```bash
python3 scripts/aqg_incident_index.py index
# OK: wrote docs/incidents/INDEX.md (N incidents)
```

The index scans `docs/incidents/*.md` (skipping `INDEX.md`, `README.md`, and any file starting with `_`), parses each AQG_INCIDENT_RECORD metadata block, and writes a date-desc-sorted markdown table.

## Conventions

- Filename: `<date>-<slug>.md` (date prefix enables natural sort)
- Files starting with `_` (e.g. `_example_clean.md`) are documentation samples, not real incidents — indexer skips them
- One incident per file
- Optional `audit_id` / `pr_url` link the incident to its audit + remediation PR

## Roadmap

- v0 (this drop): schema + redaction + record CLI + index generator
- v1+: search/filter, weekly retro clustering hook, severity escalation, runbook integration
