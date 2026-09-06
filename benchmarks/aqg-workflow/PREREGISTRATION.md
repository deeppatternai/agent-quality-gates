# WS-7 AQG-workflow empirical benchmark — Owner-authorized follow-up protocol

This document and `protocol.json` are the contract for a
**disclosed post-outcome replication**. This is a new independent
Owner-authorized follow-up under the same cumulative authorization envelope.
Its outcome-disclosure predecessor — the last attempt that observed any outcome
data, and the one `protocol.json`'s `predecessor` block binds — is the 2026-08-21
primary under study id `ws-7-aqg-workflow-2026-08-owner-authorized-followup-3`,
aborted at **254 of 800 cells** by the strict file-workspace hard check. One
model `Read` resolved outside the cell boundary and no permission denial could be
proven from the stream, so the frozen check invalidated the matrix. **The
declared path is not recoverable:** the runner blanks the raw stream on any
out-of-boundary event and the ledger deliberately retains no path, so this
contract claims no cause beyond that. The 2026-08-17 attempt it displaced — study id
`ws-7-aqg-workflow-2026-08-owner-authorized-followup-2`, 242 of 800 cells, the
same failure — is recorded under `superseded_outcome_attempts`, a slot added for
it: an attempt whose evidence is local-only and which *did* observe outcome data
fits neither `intervening_attempts` (which requires the opposite) nor
`earlier_outcome_ancestor` (which requires a published results commit), and
without a slot the promotion would have dropped its evidence directory out of
the result-boundary checker's protected roots.

What the 2026-08-21 ledger establishes about its own 254 cells is narrower than
what this paragraph used to claim of the 2026-08-17 attempt, and in one respect
the opposite. It is **not** true that no out-of-boundary attempt was ever
provably denied: cell r4 of the same task and arm recorded exactly that — one
attempt, provably denied, counted and tolerated, the cell scored normally. What
aborted the matrix was a *different* cell, r13, whose out-of-boundary read could
not be proven denied. The distinction matters because it is the whole shape of
the failure: an out-of-boundary attempt is fatal only when the stream cannot
prove it was refused.

**AMENDED 2026-08-26, and this is a change to what invalidates a matrix.** That
rule has one further tolerance now. An out-of-boundary attempt is also kept when
BOTH of these hold: the tool result **errored**, and the runner — which is
unsandboxed and holds the resolved path at check time — can independently see
that **nothing occupies that name**. An errored read of a name nothing occupies
read nothing, so there is no content that could have crossed the boundary. It is
counted separately, as `absent_out_of_boundary_file_attempts`, because it rests
on different evidence from a proven denial and a reader has to be able to tell
which one kept a cell.

Two things this is not. It is **not** a widening of the denial patterns: audit
`e8de7a82` established that a permission layer can answer a blocked path with an
existence-shaped message, so existence-shaped WORDING stays routed to the
unmatched branch. What is added is an independent positive fact from a second
observer, not a new way to read the stream. And it is **not** satisfied by
absence alone: `workspace_check` runs after the model process and the class
these aborts fall in is `ephemeral-shared` — sibling cell directories, which are
torn down — so a file read successfully and then removed would look absent here.
Requiring the tool to have errored is what stops that buying tolerance for real
contamination.

**What the second tolerance rests on, and what it does not prove.** It does
NOT prove no bytes reached the model. `is_error` and the result content are
independent — an error carrying file bytes is possible in principle, and four
external voices of four (audit `c9206b21`) named that as the hole. What makes
the leak unreachable here is configuration, so it is stated as configuration and
checked rather than assumed: cells run serially, so no sibling cell is torn down
while a model is running; a cell's own teardown runs after its own check; and
the model's tool set — Edit, Write, Read, Skill — has no delete, so it cannot
remove a file it has just read. A primary run refuses to start if that tool set
widens, and the first two are pinned by tests. **The residual, disclosed rather
than argued away: a process outside this study removing the target mid-cell.
Nothing detects that.**

**What is reported with the result.** The per-matrix total of
`absent_out_of_boundary_file_attempts` is published alongside the primary
estimate, so a reader can see how much of a completed matrix depended on the new
tolerance rather than on a proven denial.

**The three attempts already aborted under the stricter rule** (cells 242, 254,
182) are NOT re-scored under it. They stay reported as aborted attempts, with
the outcome data the fourth of them observed disclosed as `reporting.claim_rule`
already requires. The amendment applies forward only; a rule relaxed after
seeing which runs died must not also be used to revive them.

Why it was amended, stated plainly rather than left to be inferred: under the
previous rule the matrix could not finish. Three consecutive attempts died on
this one mechanic at cells 242, 254 and 182 — 678 cells, three fatal events,
about one per 226 — which makes a 600-cell run roughly a 7% proposition. The
2026-08-25 absent-path probe (issue #611) identified the mechanic: an
out-of-boundary read of a path that does not exist returns wording no denial
pattern matches. The amendment removes that specific fatal case and leaves every
other one standing.

Those 254 cells completed and were **scored**, and their four-arm pass/fail
summary was read before this contract was written. That is what makes this
attempt the predecessor rather than either earlier one: it is the most recent
point at which outcome data entered view, and recording it as an attempt that
observed nothing would make this document assert something false about its own
lineage. **No arm result from it was used and none may be:** the 254 scored cells
are discarded evidence, not a partial finding. Its `$5.64500230` accounted cost
is charged to prior spend below, and its evidence directory is retained locally
and not reused. That evidence was never committed — the result-boundary checker
refuses to publish a results directory — so the ledger SHA-256 recorded in
`predecessor`, rather than a results commit, is what binds it.

The outcome-observing ancestor that promotion displaced is permanently preserved
by results commit `045c78410f1348f611264980e571b295c84c6f13` and PR #507, and is
recorded structurally under `earlier_outcome_ancestor` rather than left to this
prose. That primary attempt was
**aborted validity-failed** after 13 of 800 planned cells because the strict
file-workspace hard-check detected a model-initiated absolute `README.md` read
outside the cell root. Its frozen
analyzer decision remains `inconclusive`; neither label is a completed test or
efficacy evidence. The raw stream was redacted and no solution provenance was
retained for the invalid cell. It is the only study in this lineage whose frozen
analyzer output was published, so it remains this contract's third-party
verifiable anchor. No raw stream, absolute path, or solution is
reproduced here.

One attempt sits between that ancestor and the predecessor, and it observed no
outcome data: the
2026-08-15 primary under study id `ws-7-aqg-workflow-2026-07-owner-authorized-followup`,
aborted at 3 of 800 cells by a CLI scratch infrastructure failure. Every
attempted cell reported no init event, so it produced **no outcome data** and
does not extend what has been observed; it consumed that study's single
authorized attempt, its `$0.75000000` charge is carried to prior spend below,
and the infrastructure cause is fixed in `run.py` by commit
`9bc84fd097b84d204e3a6d9508ac1cc98a8f52a5`, which is an ancestor of the commit
this protocol is authorized to run from. That `$0.75000000` is not a measured
cost: the aborted cells died before any usage was reported, so it is the
conservative reservation of 3 attempted cells x the `$0.25` per-cell cap. The
attempt is also recorded structurally in `protocol.json` under
`intervening_attempts`, which is what keeps its evidence directory inside the
result-boundary checker's protected roots; that directory is retained locally,
is never committed, and is bound by the ledger SHA-256 recorded there. The
predecessor's and the ancestor's directories are protected the same way, which
the checker derives from `predecessor` and `earlier_outcome_ancestor` — but
their provenance differs and is not interchangeable: the predecessor's evidence
is local-only and ledger-bound like the intervening attempt's, while the
ancestor's was published and is bound by a results commit. All four roots are
asserted by name in `test_redaction.py`, and no two lineage records may name the
same directory or the same ledger digest, so a later edit that would drop one
out of that boundary — or quietly collapse two into one — fails a test instead
of shrinking it silently.

The 2026-08-17 abort's *undiagnosability* prompted a review of the sandbox
profile against the deny set, and that review found a gap this contract closes.
The change is a **shared execution boundary, not a treatment**: the deny rules
never mentioned the namespaces the sandbox profile itself grants — `/System`,
`/usr`, `/bin`, `/sbin`, `/dev`, and, through the imported `system.sb`, the
ephemeral paths named below. An attempt there could therefore succeed instead of
being provably denied. Those namespaces are now denied at the CLI layer, which
makes such an attempt a **counted denial under the rule that already existed**.
Each out-of-boundary event's class — never its path — is also recorded in the
ledger, because the raw stream is blanked on any such event and the 2026-08-17
abort was consequently undiagnosable after the fact.

**The 2026-08-21 abort closed the two that were known reachable (amendment A4).**
The second attempt died the same way, in the same task and arm, and this time the
class was recorded: `ephemeral-shared`, denial unproven. Reading the two profile
sources against the deny set again leaves exactly two ephemeral namespaces whose
*contents* the child could both reach and not have denied:
`/private/tmp/claude-<uid>`, which `_model_child_profile` grants `file*` because
the CLI dies without it, and four `/private/var` files that `system.sb` grants
`file-read*`. Both are now denied — as the directory object *and* as a subtree,
in both tmp spellings and both firmlink spellings, Read and Edit only.

**Why the whole ephemeral root is not denied, and what pins that.** The cells
live in `/private/tmp` themselves, so a blanket rule would deny each cell its own
workspace. `claude-*` cannot match `benchcell-*`, and no cell is under `/var` —
but only because the cell tree is now *pinned* there by `run.make_cell_tree`
rather than inherited from `TMPDIR`, which on macOS normally points into
`/var/folders`. That failure mode would not abort: a denied read of `seed.py` is
a read *inside* the workspace boundary, so the matrix would complete as 600 cells
whose model could not read its own task. `run.require_cell_tree_outside_the_deny_set`
refuses a primary whose workspace any frozen rule could match, and the
`ephemeral-shared` class is split so that a scratch path records `cli-scratch` —
a recurrence then says whether a rule failed to fire or the sandbox refused the
path.

**What the tolerated event in the aborted run does and does not show.** Cell r4
of the same task and arm recorded an `ephemeral-shared` attempt with
`denied_out_of_boundary_file_attempts == 1` and was scored and kept. That shows
*some* ephemeral out-of-boundary attempt produced a denial this runner
recognises, on the binary that will run the matrix. It does **not** identify the
path or the layer that denied it, and r13 of the same task and arm is the
counterexample in the same run: same class, denial not proven. An earlier draft
of this section read r4 as establishing that every sandbox-refused ephemeral path
is provably denied, and therefore that the two namespaces above were the only
remaining explanation. Four reviewers of four refused that inference, and the
runner's own matcher already records the residual it ignores: a path refused
because it was never *granted* may use wording neither matcher covers.

**These gaps are reachable in principle; they are not a diagnosed cause, and the
list is not proven complete.** Nothing here claims either abort took either path,
and nothing can: both streams are blanked, the 2026-08-17 ledger kept no class at
all, and the 2026-08-21 class spans both groups. The deny rules close holes found
by reading the profile, which is a sufficient reason to close them. Whether
anything *else* under the ephemeral roots can produce an unproven denial is
unmeasured; amendment A4's second probe is designed to answer exactly that, and
until it has, a third abort of the same class remains possible.

**Required preflight before `--primary`, in two stages.** The claim that a CLI
deny over a path the sandbox profile *grants* produces a permission denial the
runner scores as denied is a claim about the CLI's layering, and the committed
tests assert only that the rules are configured. This lineage has already lost one
paid attempt to a sandbox-versus-CLI mismatch, so it must be measured rather than
assumed. Both stages run on the collection host under the frozen deny list and
`TMPDIR=/tmp`.

The two stages differ in cost, and the split is deliberate: stage 1 can veto the
deny list for free, so it runs first and stage 2 is only reached if it passes.

**Stage 1 — zero cost, no model call.** The CLI accepts the frozen
`--settings` payload and starts; the isolation capability preflight passes; all
three arms construct; the dry-run planner completes. In particular, runtime use of
`/usr/local` and `/usr/lib` must still work — `/usr/local` is a declared runtime
parent root and a lexical descendant of the newly denied `/usr`. This stage is
expected to pass on the layering argument that the deny list reaches the model's
*tool calls* through Claude Code's permission system, while the process's own
file access is governed by the seatbelt profile, which still grants those roots;
stage 1 is what turns that argument into a measurement. A failure here means the
deny list cannot ship as written, and it costs nothing to learn.

**Stage 2 — one paid diagnostic cell, and it must be accounted.** A deliberate
model `Read` of a path under a newly denied namespace must produce a
stream-visible permission denial, so that `workspace_check` returns `ok` with
`denied_out_of_boundary_attempts == 1` and a recorded class; an abort here means
the deny list cannot ship as written. This *cannot* be done without a real model
call, so it is not free, and this document does not pretend otherwise. Its cost is
charged to `budgets.diagnostic_spend_usd`, which reduces `primary_available_usd`
by the same amount — the runner already enforces
`diagnostic_spend_usd + primary_available_usd == global_max_usd`, so the
accounting cannot be skipped silently. Running stage 2 therefore requires explicit
Owner authorization and a protocol amendment recording the measured amount, before
`--primary`.

A green unit suite is neither stage and substitutes for neither.

**Amendment A4's two probes ran on 2026-08-22, and one of them settled a
question this document had left open.** The acceptance arm re-attested the
enlarged 150-entry payload (`$0.01397455`). The scratch arm (`$0.01052490`)
read two host paths in one call, and the two came back denied *in different
wording*, which is what separates the layers:

| target | granted by the profile? | named by a deny rule? | result |
|---|---|---|---|
| `/private/tmp/claude-<uid>/…` | yes (`file*`) | yes, since A4 | denied — `File is in a directory that is denied by your permission settings.` (99 chars) |
| `/private/tmp/…` (ungranted control) | no | no | denied — `EPERM: operation not permitted, stat …` (78 chars) |

The first is a deny **rule** firing: A4's new rule, on the binary that will run
the matrix. The second is the **sandbox** refusing a path it never granted —
and the runner recognises that wording too, so such an attempt is counted and
tolerated rather than voiding the study.

That second row is the important one. Until it was measured, this document could
not rule out that a sandbox refusal came back in wording no matcher covers, in
which case the deny rules would not have removed the abort class at all. With
both wordings recognised, the only remaining route to an *unproven* denial is a
path the sandbox **grants** — and the two such namespaces are exactly what A4
denies. Scoped to CLI 2.1.238 and to this host's `system.sb`; nothing re-reads
that file at run time.

**No validity gate is relaxed.** A wider change was considered and rejected on
measurement: tolerating reads of "arm-invariant" system roots cannot be expressed
as a path-prefix test, because `/System/Volumes/Data` is the firmlink mount of the
entire data volume — so `/System/Volumes/Data/Users/...` is a lexical descendant
of `/System` — and `/usr` contains `/usr/local`, which this runner declares a
runtime parent root whose granted contents differ by arm. A successful or
unprovable out-of-boundary operation therefore still invalidates the whole study,
exactly as before.

The PR #507 outcomes, and the earlier PR #504 outcomes, were already observed before
this contract. They are not rerun, overwritten, or reinterpreted. This is
therefore **not an outcome-blind preregistration**. It has a distinct study ID,
results directory, shuffle seed, and atomic marker; its claim is limited to the
new fixed-suite replication described below. No primary model cell may run
until this protocol PR is merged. A later results PR may add only frozen
analyzer output and a redaction-safe evidence summary; ledgers, markers, raw
streams, and solution archives remain local.

## Disclosure of prior observed outcomes

Both attempts that observed an outcome are immutable. The following
redaction-safe metadata binds this new study to each of them without reproducing
a raw model transcript or solution. Two rows differ in kind and are not
interchangeable: the predecessor is bound by a ledger digest because its
evidence was never published, while the ancestor is bound by a results commit
anyone can fetch.

| field | immutable predecessor value (`predecessor`) |
|---|---|
| results PR / commit | none — evidence local-only, never committed |
| study ID / results directory | `ws-7-aqg-workflow-2026-08-owner-authorized-followup-3` / `results-ws-7-aqg-workflow-2026-08-owner-authorized-followup-3` |
| frozen-ledger SHA-256 | `f3e59a1defe4614034df2f700be011deb9100bfc79cf43b38b337d3769472d00` |
| observed state | aborted at 254/800 on the file-workspace hard check; 254 cells completed and scored, four-arm pass/fail summary read |
| invalid cell, redaction-safe metadata | `migrate-api-version × baseline × neutral r13`, class `ephemeral-shared`, denial unproven. Recoverable this time because `_out_of_boundary_class` existed by then; the declared PATH is still not, by design |
| recorded accounted cost | `$5.64500230` (measured from the ledger) |

| field | displaced attempt (`superseded_outcome_attempts[0]`) |
|---|---|
| results PR / commit | none — evidence local-only, never committed |
| study ID / results directory | `ws-7-aqg-workflow-2026-08-owner-authorized-followup-2` / `results-ws-7-aqg-workflow-2026-08-owner-authorized-followup-2` |
| frozen-ledger SHA-256 | `b3c34d101bf36272ec8e46def221bbbddf316f415ac86dba759e6b6198beb350` |
| observed state | aborted at 242/800 on the file-workspace hard check; 242 cells completed and scored, four-arm pass/fail summary read |
| invalid cell, redaction-safe metadata | not recoverable — `_out_of_boundary_class` did not exist yet, so the ledger recorded no class and the stream is blanked |
| recorded accounted cost | `$5.08475150` (measured from the ledger) |
| why this slot | it observed outcome data, so `intervening_attempts` rejects it; its evidence was never published, so `earlier_outcome_ancestor` rejects it. Without this row its evidence directory leaves the result-boundary checker's protected roots |

| field | immutable ancestor value (`earlier_outcome_ancestor`) |
|---|---|
| results PR / commit | #507 / `045c78410f1348f611264980e571b295c84c6f13` |
| study ID / results directory | `ws-7-aqg-workflow-2026-07-disclosed-post-outcome-replication` / `results-disclosed-post-outcome-replication-2026-07` |
| frozen-ledger SHA-256 | `88d5941035dd590c1b362d01fbef72a0dd72b70405573dd14277e143decda67a` |
| observed state | aborted validity-failed at 13/800; frozen analyzer `inconclusive` |
| invalid cell, redaction-safe metadata | `merge-allowed-fields` / `baseline` / `neutral` / repeat 15 |
| recorded accounted cost | `$0.53757445` |

After observing those aborts, this follow-up receives a new study ID,
result-directory name, shuffle seed, atomic marker, and fixed shared workspace
boundary instruction. The ten tasks, 20 repeats, neutral strictness, primary
contrast, 10pp threshold and exact test are retained. The arms are NOT: the
fourth, third-party active control was removed on 2026-08-23 (amendment A5),
leaving three. Its removal shrank the Holm family, and the Owner ruled that the
divisor stays at the pre-narrowing five so the change collects no statistical
dividend. This disclosure does not turn any invalid attempt into efficacy
evidence.

## Fixed matrix

The primary matrix is exactly **10 deterministic small-code tasks × 3 arms × 20 conditionally independent repeats**
at neutral prompt strictness: 600 cells. It was four arms and 800 cells until
2026-08-23, when amendment A5 removed the third-party active control on Owner
instruction; see that amendment for what the removal costs, including the
Holm family shrinking from five secondary contrasts to two. The deterministic shuffle seed is
`20260818`; cells are not executed in arm-major order. Each study takes a
distinct seed, as required above; this one is the successor's authorization
date in `YYYYMMDD`. It was chosen by that rule, not searched against the cell
positions the aborted attempts exposed. Those positions are far from three now:
the 2026-08-15 abort exposed 3 identities under its own superseded seed
`20260716` and the 2026-08-17 abort exposed **242** task × arm × repeat
identities under the superseded seed `20260816`. They are
execution metadata only: they may not be used to alter the tasks, arms,
thresholds, primary contrast, or claim rule, none of which this successor
changes.

| task | scenario category | held-out behavior |
|---|---|---|
| `parse-positive-int` | bug fix | page-size incident boundary |
| `parse-page-window` | bug fix | bounded pagination repair |
| `format-display-name` | new feature | robust profile-card label |
| `load-json-object` | new feature | structured configuration ingest |
| `refactor-tag-list` | normalization hardening | deduplication and input validation |
| `safe-relative-path` | security-sensitive | path traversal |
| `merge-allowed-fields` | security-sensitive | mass assignment/type validation |
| `build-redirect-path` | security-sensitive | open redirect |
| `parse-utc-date` | contract change | canonical API date |
| `migrate-api-version` | contract change | v1-to-v2 profile conversion |

Each task contains the agent-visible `task.md` and `seed.py`, plus a held-out
`checks.py`, disciplined `good_ref.py`, and plausible `bad_ref.py`. The
zero-cost `selftest.py` must show every good reference passes and every bad
reference is caught before primary collection.

The treatments are:

1. `baseline` — project-only settings, strict MCP, no session plugin, and
   machine-local auto-memory disabled.
2. `claude-md-lite` — baseline plus the frozen short validation prompt.
3. `aqg-full` — shorthand for the **no-shell AQG rules-and-skills** treatment:
   baseline plus an ephemeral wrapper exposing this checkout's AQG Claude
   skills and the frozen AQG rules prompt. It deliberately excludes hooks and
   therefore is not a measurement of hook-enforced AQG workflow execution.
4. `third-party-ecc` — baseline plus an explicitly supplied ECC `2.0.0-rc.1`
plugin. Its manifest and loaded plugin-source-tree SHA-256 values are fixed in
   `protocol.json` (runtime lease markers, VCS metadata, caches, and
   `node_modules` are excluded; `hooks/` is also excluded because Claude Code
   convention-loads it even without a manifest declaration); its init event
   must show `tdd-workflow` and no `aqg-*`. The executed private snapshot is
   independently hashed *with hooks included* and must equal the frozen no-hook
   hash, so an accidentally copied hook invalidates collection before it starts.
   Because its convention-loaded SessionStart hook is removed, this arm does not
   measure ECC as users install it; no contrast may support a claim about ECC's
   shipped configuration.

No third-party source is copied into this repository. A plugin path is supplied
only at execution and must match the frozen hashes.

## Outcome and decision rule

Because the earlier outcomes and the 13-cell aborted validity-failed primary
were observed, this experiment is a transparent replication after that
observation, not a blind first test. Passing the gates below can support only
the stated result for this exact fixed suite and setup; it cannot be presented
as outcome-blind preregistration evidence.

The primary endpoint is the **intention-to-treat defect indicator**: every
attempted cell whose status is not `pass` is a defect, including agent errors
and infrastructure failures. The primary contrast is `aqg-full − claude-md-lite`,
calculated as the mean of the ten within-task defect-rate differences.

The study may report “a lower measured defect rate for this fixed task suite”
only if all of the following hold:

- every arm completes all of its planned cells and has at most 1%
  infrastructure failures;
- no cell is stopped by the per-cell budget cap;
- every observable treatment/isolation and file-workspace boundary check passes,
  and every nonempty stream reparses cleanly with zero malformed JSON lines;
  an init-less infrastructure cell remains a defect and counts toward the fixed
  1% per-arm attrition ceiling rather than falsely proving missing treatment; and
- the observed `aqg-full − claude-md-lite` mean is at most −10 percentage
  points; and
- a two-sided exact stratified cell-level permutation test (stratified by task)
  has `p ≤ 0.05`.

The five other pairwise comparisons are secondary and use Holm-Bonferroni
adjustment: `claude-md-lite − baseline`, `aqg-full − baseline`,
`third-party-ecc − baseline`, `third-party-ecc − claude-md-lite`, and
`aqg-full − third-party-ecc`. Capability
presence and observed `Skill` invocations are recorded
as manipulation diagnostics; they never remove an assigned cell from the
primary ITT denominator. Any failed validity gate is reported as
**inconclusive**, not as a quality result.
The `aqg-full − third-party-ecc` secondary compares complete configurations
(AQG rules plus plugin versus ECC plugin alone), so it cannot isolate skill
quality from rules-prompt effects.

The 20-repeat choice assumes a heterogeneous 15-point average change (30%
light-rules defect rate to 15% AQG defect rate) over ten task blocks and tests a
10-point practical threshold. A low realized light-rules defect rate can make
that absolute threshold unattainable; that outcome remains inconclusive rather
than retuning the frozen study. The exact test is conditional on the fresh,
`--no-session-persistence` model calls being independent draws; provider-side sampling and
cache behavior are not inferred from the protocol. The frozen analyzer's
fixture coverage and a fixed-seed power simulation are checked before
collection; the study makes no claim beyond this task suite or this exact
model/plugin setup. The remaining Owner envelope before this study is
$393.19805795; the **fixed $299.00 primary cap** is the only amount authorized
for this primary and
$94.19805795 remains locked after a full-cap primary; with the 100% completion
rule and no
post-data retuning, a budget stop, low control defect rate, or validity failure
is a first-class **inconclusive** outcome, not a recoverable result.

## Safety, provenance, and budget

The runner uses project-only settings, strict MCP configuration, explicit
session plugins, a fixed `Edit`/`Write`/`Read`/`Skill` tool surface (no shell
tool),
fresh per-cell work directories, and an allowlisted model-process environment.
After the model turn, the solution is copied once into an immutable private
temporary scoring directory; hidden checks run there through macOS `sandbox-exec`, which denies
network, all `/Users` reads/writes, all `/Volumes` access, and writes to common operator PATH/config
prefixes (`/usr/local`, `/opt`, `/private/etc`) and the shared per-run cell
root and its immutable solution copy. The worker has CPU and output-file
limits and a dedicated process group, which is reaped with descendants after
each hidden-check invocation.
A missing sandbox fails the cell closed. The primary runner refuses any checkout
or result ledger outside `/Users`, and refuses a user-home Python interpreter,
because those are boundaries enforced by this scorer profile. This is **not** a
deny-default container or VM; host-memory and other allow-default residuals
remain documented limitations, so it is run only as this bounded local study.

This measures AQG rules plus invokable explicit skills under an intentionally
no-shell tool surface; it does **not** claim to measure hook enforcement,
unrestricted command execution, arbitrary test-running workflows, or general
software engineering. The durable ledger records only cell-relative declared
Read/Edit/Write paths and a logical cell identifier; its raw stream remains
local. The analyzer reconstructs both the file-workspace and AQG-isolation
verdicts from that stream rather than trusting ledger booleans, independently
re-scores every durable regular solution against its frozen held-out checks,
and rejects any endpoint mismatch. A stream containing a successful or
unproven workspace escape or malformed JSON is redacted to avoid retaining
possible private data, and its per-cell temporary workspace is removed; that
redaction is itself an explicit, whole-study-invalidating evidence state. An explicitly permission-denied
out-of-boundary file attempt is instead retained only as a redacted count,
but only when the stream includes both `is_error: true` and explicit
permission/access-denied text; a missing-file or other error is unproven and
invalidates. The runner uses
Claude's documented current-directory plus `--add-dir` access model rather
than trusting an unrecorded path assumption.

To address the known absolute-path read without changing a treatment, the same
frozen user instruction is appended to every arm's task ticket: work only in a
private cell; edit/write only inside its `workspace/` directory; read only the
workspace or its otherwise-empty private cell root; never inspect a shared
parent, repository documentation, user-home file, or other host path. Every
arm requests the same frozen CLI permission-deny rules for user-home and
common host-private roots. These are shared execution boundaries, not a plugin
setting, audit setting, or arm-specific treatment. The hard file-workspace
check remains authoritative and still invalidates the whole study if a model
successfully crosses the boundary or the stream cannot prove a shared deny.
Home shorthand is always rejected rather than being interpreted as a relative
cell path. A zero-cost contract check runs before the
primary-attempt marker is reserved and verifies the exact prompt and deny-rule
set. The CLI permission-result schema is not separately claimed as an
experimental manipulation; an unproven external operation fails the study
closed. These controls prevent documented drift and high-risk file-tool access;
they do not claim a deny-default container or VM.
All no-cost capability, source-hash, scorer, version, regression-suite, and
fixed-seed power checks complete
before the primary-attempt marker is reserved.

`Skill` remains necessary to administer the two plugin treatments, but the
stream schema does not expose plugin-internal file I/O as `Read`/`Edit`/`Write`
events; this is an explicit observability limit rather than a claim that those
unobservable effects are path-confined.
Even a `supported` result is evidence for this fixed, deterministic small-code
suite **only**: it does not show fewer defects in general, and does not satisfy
the D4×D5 bar for an outward causal claim of that kind. That mechanical release
gate remains closed until a separate, appropriately scoped study supplies
evidence.

Every primary run requires a clean checkout at the merged protocol commit; the
runner freshly fetches and proves that commit is an ancestor of `origin/main`, records both SHAs,
runs the no-cost environment, scorer, and workspace-boundary checks, requires
`claude auth status` to succeed without a model call, and only then atomically
reserves the sole authorized study attempt before its first paid cell. If a
pre-marker check fails, no marker exists and no paid call is made. Once the
marker is reserved, any post-reservation abort (including a cap, infrastructure,
or file-workspace validity failure) consumes this study's one attempt: it is
recorded as aborted with the frozen analyzer's `inconclusive` decision, cannot
support an effect claim, and has no retry path. Its actual recorded accounted
cost becomes prior spend for any future study; it does not unlock or spend the
headroom below without a new explicit Owner authorization. The marker path and
SHA-256 are included in the manifest and rechecked by the analyzer, binding
result evidence to that single local attempt.
The run records the protocol SHA-256, task/scorer tree hash,
frozen treatment prompt hashes, start/end snapshot hashes, full model
identifier, Claude CLI version, ECC manifest/tree hashes, deterministic order
seed, and every attempted cell record. Each raw model stream and every regular
post-turn solution are saved beside
the JSONL in a durable per-run archive; raw/solution artifact references are
relative and SHA-256-bound, while the ledger keeps no host root, absolute file
tool path, CLI stderr, or unsanitized model/checker diagnostic path. The frozen CLI analyzer reparses each stream and verifies every
solution archive hash before trusting
derived terminal cost, stream validity, capability, Skill-use, isolation, or
file-workspace fields. The held-out scorer status remains a separate recorded
outcome, rather than a fact claimed to be recoverable from a model stream alone.
The model child receives no inherited token or cloud-credential environment
variable. It also receives `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` through the
allowlisted child environment. That switch is set defensively and its effect is
UNVERIFIED on the binary that will run the matrix. This text previously claimed a
no-cost inspection of a Claude Code 2.1.207 binary confirmed the switch; no
snapshot of that inspection was retained, so it is recorded here as a prior
claim rather than as preserved evidence. Re-checked 2026-08-20 on the installed
2.1.237, `claude --help` mentions auto-memory only inside the `--bare`
description and does not document this variable. The runner fixture still locks
that the exact environment reaches the model process; what is no longer claimed
is that the CLI acts on it. Absence from help text is not proof the binary
ignores the variable, and not proof that it honours it. The residual is recorded
rather than argued away: if the switch is inert, auto-memory is on during
collection, which is a cross-cell carryover pathway. No other claim in this
protocol depends on the switch working.
That switch is an independent machine-local control, not a claim that it caused
or cures the PR #507 attempt's project-level `spec.md` read. A separate no-cost
fixture proves a read of the private cell root is harmless while a shared-parent
read remains invalid and redacted. This is a scoped read boundary, not an
exception for shared host paths.

The frozen estimate is $0.08 per cell ($64 for 800 cells), an unvalidated
planning estimate rather than a post-freeze calibration result. Each Claude call
is limited to $0.25 through `--max-budget-usd`; the separately frozen primary
cap is **$299.00** within the Owner-authorized cumulative ceiling of **$400**.
BOTH were raised by $200 on 2026-08-21, on the Owner's instruction given with
the acceptance that opened this gate. The study cap could not be raised alone:
`run.load_protocol` refuses a study cap above the envelope, and refuses prior
spend plus that cap above it too. Raising both by the same $200 leaves the
locked headroom unchanged at `$94.19805795`.
The current-authorization prior spend is exactly `$6.80194205`: the earlier
authenticated attempt and two isolation diagnostics total `$0.16957775`, the
six-cell inconclusive PR #504 attempt totals `$0.20630260`, the completed
2026-07-13 disclosed-replication isolation diagnostic totals `$0.05373575`, the
aborted validity-failed 13-cell PR #507 primary totals `$0.53757445`, and the
2026-08-15 attempt under the preceding study id, aborted at 3 of 800 cells by a
CLI scratch infrastructure failure, totals `$0.75000000`, the 2026-08-17
attempt under study id `…-followup-2`, aborted at 242 of 800 cells on the
file-workspace hard check, totals `$5.08475150`, and the 2026-08-21 attempt
under `…-followup-3`, aborted at 254 of 800 cells the same way, totals
`$5.64500230` — both of those last figures are measured sums of their attempted
cells, not reservations. Those attempts
did not contribute outcome data in the same way: the 2026-08-15 cells never
reached an init event, so that attempt adds spend without changing what has been
seen and is recorded under `intervening_attempts`; the 2026-08-17 and 2026-08-21
matrices were each invalidated before any arm result could be **used**, but
their cells were
scored and their four-arm summaries were **seen**. That is why the later of the
two — 2026-08-21 — occupies `predecessor`, the earlier one sits in
`superseded_outcome_attempts`, and the PR #507 attempt, which is the only one
whose evidence was published, stays in `earlier_outcome_ancestor`.
Every component above except the 2026-08-15 figure is a recorded accounted cost
from the structured ledgers and immutable results PRs; that one is instead the
conservative reservation described earlier, because its ledger recorded no cost
at all. This protocol does not claim external provider-billing reconciliation. The diagnostic and all three aborted attempts are excluded from this
new primary matrix and analysis; each accounted cost is recorded in prior spend, not against
the $299.00 study cap (`global_max_usd`), of which the primary matrix may use
`primary_available_usd` — $298.82278605 after the two 2026-08-22 probes.
The separate `$0.1451403` full-ECC diagnostic remains allocated to the older $75 authorization recorded in the implementation adjudication; it is excluded
from this envelope rather than silently treated as available headroom.
The fresh $299.00 cap is set explicitly for this distinct study rather than
inherited from the prior protocol; its maximum cumulative spend is
`$305.80194205`, below the Owner ceiling by `$94.19805795`. This `$94.19805795`
headroom is locked: this primary can spend at most `$299.00`, and use of any
headroom requires a later explicit Owner authorization.
The frozen 1% infrastructure ceiling is a fail-closed operational assumption,
not an empirically estimated provider reliability rate: the available prior
attempts are too few and were not a representative uninterrupted sample. The
Owner authorized one primary attempt only, so this protocol neither adds paid
pilots nor retries cells nor relaxes that ceiling after data collection; excess
attrition is reported as inconclusive.
The per-cell ceiling is raised from the original `ws-7-aqg-workflow-2026-07`
study's `$0.15` — every attempt since, including both outcome-observing ones, has
used `$0.25` — to reduce the risk
that one truncated cell trips the zero-tolerance budget-capped validity gate;
the same ceiling applies to every arm. At 800 cells, `800 × $0.25 = $200` in
per-cell caps, while the fresh `$299` global cap is the binding study stop.
Before the 2026-08-21 raise the binding stop was the $99 cap, BELOW that $200
worst case, so a run costing near the per-cell ceiling would have aborted
mid-matrix; that is why the Owner raised it. The abort machinery is unchanged
and still fires at the new cap. Before
each call, the runner performs a temporary affordability check for one `$0.25`
cap; after that call it charges only `accounted_cost_usd` (the reported cost, or
the full cap only if cost evidence is missing or invalid). Thus unused temporary
capacity is not permanently debited. A missing cost record is tolerated but is
conservatively charged at the full cap; malformed, negative, or over-cap cost
evidence fails closed and makes the study inconclusive. The ledger
records each cell's reported and accounted cost, and the analyzer independently
checks that identity plus the current-study and cumulative current-authorization
ceilings. The runner records the Owner maximum, prior spend, and cumulative
current-authorization spend in its atomic marker, manifest, and completion
record; the analyzer rejects drift. An actual-spend or budget-capped abort is
inconclusive. Any non-success terminal subtype other than the frozen documented
budget subtype also aborts the matrix rather than being absorbed into allowable
infrastructure attrition.

After this PR merges, the authorized primary invocation is:

```bash
python3 benchmarks/aqg-workflow/run.py --primary --execute \
  --out benchmarks/aqg-workflow/results-ws-7-aqg-workflow-2026-08-owner-authorized-followup-4 \
  --frozen-commit "$FROZEN_PROTOCOL_COMMIT"
```

Amendment A5 removed the third-party arm and with it the
`--third-party-plugin-dir` flag; a command carrying it now fails on an
unrecognized argument. This block still named the flag after that change, which
is the class of error A5's own review kept finding — a removal corrected in one
place and left standing in another.

`$FROZEN_PROTOCOL_COMMIT` is the merge commit of this Owner-authorized
follow-up protocol PR, and the result branch must start from it.

When all validity gates pass, the frozen analyzer reports `supported` (the
observed AQG-minus-light-rules difference met the −10pp practical threshold and
the exact test), `harmful` (the observed difference met the +10pp threshold and
the same exact test),
or `inconclusive`. No result is
generalized beyond these small tasks, this model, tool surface, and frozen
plugin snapshots.
