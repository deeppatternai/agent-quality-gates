# aqg-project-status Coverage Matrix

Scope: ledger projection, drain semantics, format contract, data-quality notes, translation guard, and repo-reality isolation.

| Requirement | Behavior | Existing Case | New Case | Oracle | Status | Risk |
|---|---|---|---|---|---|---|
| Ledger status is rendered | Progress/defect/milestone facts are projected | 001, bench-001 | — | structured JSON + report artifacts | COVERED | high |
| Empty/missing ledger is safe | Empty view returns a warning without fabricating state | 002 | — | event count/warnings/path absence | COVERED | high |
| Default drain consumes inbox | Pending events appear and inbox empties | 003, 005, bench-002 | — | counts/events/status | COVERED | critical |
| `--no-drain` is strictly read-only | Stored ledger hash remains unchanged | 001, 004, 006, bench-001 | — | before/after hash | COVERED | critical |
| Corrupt data is visible | Valid events survive and malformed data notes are surfaced | 004 | — | warnings + surviving event | COVERED | high |
| Terminal state is preserved | Contradictory later actions are ignored and warned | 006, bench-002 | — | terminal status + warning | COVERED | critical |
| Translation segments exclude facts | Only producer free-text is emitted for translation | — | 007-translation-fact-guard | segment schema + no fact tokens | ADDED | critical |
| Translation fact guard fails closed | Fabricated number/date/status falls back to canonical report | — | 007-translation-fact-guard | output equality/fallback note | ADDED | critical |
| Format/argument contract | `--json`/`--format` conflict exits 2; output formats remain parseable | self-test partially | 007-translation-fact-guard | exit code + JSON/Markdown shape | medium | medium |
| Repo reality is opt-in and bounded | Default path does not invoke git/gh; opt-in failures degrade in-band | — | 007-translation-fact-guard | subprocess probe + warnings | PARTIAL | high |

## Coverage Gaps

- Actual `gh` availability and remote service failures remain environment-sensitive; the harness patches the collector or uses a local repository.
- HTML visual layout is not exhaustively snapshot-tested; structural output is the stable oracle.

## Case Taxonomy

- Existing cases: `baseline`, `boundary`, `state`, `idempotency`, `integration`, `benchmark`.
- `007-translation-fact-guard`: `negative`, `adversarial`, `security`, `read-only`, `format`.

## Suspected Defects

None from static audit.
