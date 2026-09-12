# Changelog

All notable changes to AQG (Agent Quality Gates) are recorded here. Source of truth for `VERSION` is the `VERSION` file at repo root.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/) loosely; SemVer ("0.MINOR.PATCH" while pre-1.0).

Future entries are intentionally concise and user-facing. Record only observable
behavior changes, required migrations, compatibility or security impact, and
known limitations. Keep audit metadata, internal decision history, implementation
logs, and path-by-path inventories in engineering records rather than this file.

## [Unreleased]

## [0.14.21] - 2026-09-12

### Changed

- Release a new version for automatic-update verification. There are no
  functional or source-code changes from 0.14.20; only release metadata and
  documentation are updated.

## [0.14.20] - 2026-09-12

### Fixed

- Managed hook refreshes no longer reject an installation merely because the
  updater runs under a different Python. The updater retains the installed
  absolute interpreter when it is still an executable file and every remaining
  command argument is unchanged.
- Refresh remains fail-closed when the installed interpreter was removed or any
  command tail changed; it reports the configuration as unrecognized instead of
  silently switching the hook to another Python. A machine blocked before this
  updater becomes active still needs the affected installer reapplied once.

### Changed

- Managed host installers on every platform continue to write the running
  interpreter's absolute path. They do not resolve `python3` from the host's
  runtime `PATH`. Project-scope hook configurations remain outside updater
  inventory; reapply their installer to repair a removed interpreter. Codex
  hooks are unchanged and still use the interpreter reviewed via `/hooks`.

## [0.14.19] - 2026-09-11

### Changed

- Release a new version for automatic-update verification; runtime behavior
  is unchanged from 0.14.18.

## [0.14.18] - 2026-09-10

### Changed

- Release a new version for another automatic-update verification;
  runtime behavior is unchanged from 0.14.17.

## [0.14.17] - 2026-09-10

### Changed

- Release a new version for automatic-update verification; runtime behavior
  is unchanged from 0.14.16.

## [0.14.16] - 2026-09-10

### Fixed

- Fix intermittent Git startup failures (0xC0000142) during Windows
  skill-triggered automatic updates. Updates remain quiet and do not block
  foreground work; macOS and Linux behavior is unchanged.

## [0.14.15] - 2026-09-10

### Fixed

- Keep private discussion records under `docs/discussion/**` out of the public
  B repository, including case, separator, dot-segment and source-alias forms.
- Remove the two previously published audit-trigger delivery plans from the
  generated B snapshot and inline the remaining architecture references.
- Exclude structurally private paths from publish-drift suggestions so they
  cannot be proposed for addition to the public allowlist.

## [0.14.14] - 2026-09-10

### Fixed

- Keep newly installed skill links and link markers on the stable AQG entrance,
  so switching releases does not pin them to the previous version directory.
- Accept legacy version-specific markers for the same managed installation
  after an update, while retaining rejection of wrong, broken or pinned links.
- Normalize Windows extended path prefixes and home-directory aliases when
  comparing managed skill sources. Apply shared routing across Cursor, work
  clients, Qoder, registry-based agent clients and Pi installers.

### Added

- Publish regression coverage for repeated upgrades, retired releases, Windows
  client junctions, legacy markers and unrelated-link protection.

## [0.14.13] - 2026-09-09

### Changed

- Metadata-only release for testing automatic updates from signed 0.14.12
  (release sequence 14). Runtime code, skills, hooks and client configuration
  are unchanged from 0.14.12.
- Update VERSION and both README version labels. Publication reuses existing
  development verification without rerunning tests or installing clients.
  Publishing this version does not itself confirm that a client has updated.

## [0.14.12] - 2026-09-09

### Fixed

- Accept byte-identical Python executable aliases in managed hook commands,
  including Windows native, Git Bash and WSL path spellings. Foreign commands
  no longer influence the interpreter comparison.
- Check Qoder shared hook configurations against their installed owner profiles,
  so a Qoder CLI-only installation does not require the desktop hook definition.

### Changed

- Signed updates can refresh existing AQG-owned hooks from the candidate's
  installer definitions while preserving user hooks and unrelated settings.
  Shared files are rendered once; Codex approval remains a host action.
- Back up hook edits before writing, verify them after activation, restore only
  files changed by the transaction on failure, and permit retries after rollback.
  Interrupted recovery requires trusted configuration evidence and known install
  state; ambiguous or explicitly repair-required transactions remain protected.
- Preserve the installed-host roster and remove completed transaction backups.
  Add the shared reconciliation module and its behavior tests to the public list.

Existing installations blocked by the old inspector need a manual repair or
reinstall to receive this updater before testing a later automatic update.
Publication reuses development verification; it does not rerun test suites.

## [0.14.11] - 2026-09-09

### Changed

- Metadata-only release for observing automatic updates from the signed
  0.14.10 reissue (release sequence 12). Runtime code, skills, hooks and client
  configuration are unchanged from that reissue.
- Update VERSION and the version labels in both READMEs. Publication reuses
  existing development verification without rerunning tests or installing clients.
  Publishing this version does not itself confirm that a client has updated.

## [0.14.10] - 2026-09-09

### Signed reissue (release sequence 12)

- The reissue adds background update triggers to the startup-preflight and
  code-construction Python CLIs, update failure retry recovery, and the Python
  3.9 rules-module import fix. The original publication below was sequence 11.

### Changed

- Original metadata-only release providing a new signed version for observing automatic
  updates from an existing installation. Runtime code, skills, hooks and client
  configuration are unchanged from 0.14.9.
- Update VERSION and the version labels in both READMEs. Publication reuses
  existing development verification without rerunning tests or installing clients.
  Publishing this version does not itself confirm that a client has updated.

## [0.14.9] - 2026-09-09

### Changed

- Sourcing the shared skill context helper now starts the existing managed
  updater with apply enabled, preserving its signature, lock and host gates.
- Project status and decision capture retain the sourced script's physical
  resource root when the managed entrance moves to another version.
- SKILL.md files and client configuration are unchanged. The pin covers one
  sourced skill invocation; in-flight root-relative hooks remain outside it.

### Verification

- Development: 140 selected regressions passed; 14 focused tests passed after
  transfer to the release source. Project-status and decision-capture self-tests
  passed (26 and 5). Six Windows lock failures and one Bash path failure also
  reproduce on the unchanged baseline. The full suite was not run.
- Independent review: four voices completed, one failed; 13 findings adjudicated.
  Publication reuses this development evidence without rerunning test suites.

## [0.14.6] - 2026-09-08

### Changed

- New managed version directories use readable release labels, with a short
  commit suffix for reissued versions and full-commit fallback for unsafe labels.
  Existing hash directories remain available for hooks and rollback. An old
  updater installs this release into a hash directory; later updates use labels.
- Live revision checks use the checkout's actual Git identity. Mixed-layout
  migration verifies ownership of newly admitted short-name directories.
- Repeated verified updates reconcile newer release metadata under the existing
  transaction lock without reinstalling or lowering the recorded release sequence.

### Verification

- Development verification: 328 tests passed, 2 skipped. Seven existing Windows
  path-assertion failures were reproduced on the unchanged baseline; one known
  Git Bash environment assertion was excluded after baseline reproduction.
  Native macOS execution remains unverified.

## [0.14.5] - 2026-09-08

### Fixed

- Doctor preserves the managed AQG entrance when verifying Codex hooks, so a
  valid symlink-based install no longer reports `codex_hooks: stale` while the
  installer reports success. Skill ownership still uses resolved physical paths.
- Root resolution failures receive an AQG_ROOT diagnostic, and standalone
  symlinks to the Doctor script retain checkout discovery.
- Definitions pinned to a physical version or carrying an outdated hook digest
  still require explicit reapplication; verification does not relax hook trust.

### Verification

- Development verification: 124 focused regression tests passed. Six existing
  Windows JSON-fixture failures were reproduced on the unchanged baseline.
  macOS hardware and Codex runtime discovery/trust remain unverified.

## [0.14.4] - 2026-09-08

### Fixed

- Windows installs now preserve native profile resolution and exact hook bytes,
  migrate to a usable directory link, and atomically switch signed versions.
- Hooks and rules keep the managed entrance across version changes. Target-tree
  evidence no longer causes redundant merges; legacy physically pinned Codex
  hooks remain protected while changed digests through the entrance stay pending.
- Interrupted migration retains its original diagnostic and recovery instructions.
- AQG and DE install entrances share the managed-layout contract; new runtime,
  regression tests and Windows guidance are included in the public distribution.

### Verification

- Native isolated tests cover clean reinstall, signed content updates, real
  SessionStart background updates, changed-digest refusal and 13 other hook
  configurations following a root swap. See docs/WINDOWS_AUTO_UPDATE.md for
  supported clients, requirements and configuration-reconciliation limits.

## [0.14.3] - 2026-09-07

### Fixed

- **Automatic updates could not complete on any machine.** The planner diffed the
  shipped skill roster against `state["hosts"][id]["routed_skills"]` — a key written
  only by `run._record_installed`, from *inside* an apply. No installer writes it. So
  every fresh machine read an empty roster while sixteen routes sat on disk, planned
  one `route_skill` per shipped skill, and `route_skill` is host-touching: `_apply`
  applied **nothing** and returned `pending`. Nothing applied meant nothing recorded,
  so the next check planned exactly the same thing. There was no exit.
  Compounding it, the link text carried a release. Ownership of a route is an exact
  raw link-text comparison — deliberately, because parsing once admitted shapes this
  layer never wrote and deleted entries it did not create — and the installer spelled
  those links through the *resolved* root, so each named `versions/<sha>` and stopped
  being recognised at the next release. `docs/UPDATE_ARCHITECTURE.md` §8's premise,
  that a skill's content rides the root symlink for free, was true of no install this
  repo had ever produced. Fixing either alone leaves it broken: the planner still
  stalls without the second, and the first silently strands skills on a tree that
  retention will delete.
  The root cause was one variable serving two jobs. Executing out of the checkout
  wants the PHYSICAL path so a skill invocation cannot tear across a swap;
  `scripts/_aqg_context.sh` resolves for exactly that reason and is right to. Owning a
  route wants a spelling that does not move. `Evidence` now carries the routes read
  off the host's own directory, the managed root is *named* rather than derived, and
  the installer and the planner arrive at one string through one function.
  **Everyone must uninstall and reinstall once**: existing routes carry the old
  version-pinned spelling and the new code does not recognise them.
- **Every release but the last was unrecoverable.** The publish runbook's archive step
  wrote the literal `refs/aqg-release-archive/stable-<prev>` instead of substituting
  the hash. `<` and `>` are legal in a ref name, so nothing errored — every release
  wrote the same ref and overwrote it. The comment three lines above said the archive
  exists so the overwrite is reversible. The read, the archive and the publish now run
  in one `set -e` subshell, because fixing the names still left the archive skippable:
  pasted into an ordinary shell those lines keep going after a failure, so a rejected
  archive push scrolls past and the channel push two lines down unreferences the
  release that just failed to be archived.
- **Two release tests passed locally and failed on CI with `fatal: empty ident name`.**
  A reused clone copies no user config and a post-receive hook commits as the pushing
  user; a developer machine derives an identity from its passwd entry and a CI runner
  has nothing to derive one from. Worse than the failure was what it hid: with the hook
  broken, no race was ever injected, so a test reported "the lease must refuse a
  channel that moved" about a channel that never moved. The fixture precondition is now
  asserted before the property it exists to exercise.

### Added

- **A file under a managed prefix must carry a publication decision.** Absence from
  `internal/carve/allowlist.txt` was silent, and three times a behaviour test was
  written, committed and never allowlisted — the public repo shipped an engine without
  its tests while all four carve gates reported green. The only check that looked was
  advisory, and stayed advisory because there was nowhere to record "reviewed,
  excluded". Now there is: `internal/carve/allowlist-exempt.txt` holds
  `<path>  # <reason>`, a bare path is refused, and under seven managed prefixes every
  tracked file must appear in one list or the other. `docs/`, `benchmarks/` and
  `workpackets/` stay advisory — their exceptions are hundreds of evidence artefacts,
  and a gate demanding a line per artefact is a gate people delete.

## [0.14.2] - 2026-09-07

### Fixed

- **Automatic updates had never worked for anybody.** `_apply` refuses any root
  that is not a symlink into a `versions/` directory — correctly, because the
  atomic one-symlink swap is the whole reason an unattended update cannot leave a
  half-written tree — and **no install path produced one**. `scripts/install.sh`
  and decision-engine's `de-aqg-install` both clone into a plain directory, so
  every install since the channel shipped could reach the remote, fetch a
  release, verify its signature against the shipped keyring, and then refuse to
  apply it. Silently: the outcome was not surfaced anywhere a user looks.
  `scripts/install_aqg_clients.py` now puts the install on the managed layout,
  because it is the one place both install paths pass through — so the other team
  does not have to change how they call us. It runs **after** the commands and
  **only** when they all succeeded: the conversion renames the checkout this very
  script is running from, and doing it after a failed install would leave that
  team's own retry and repair paths meeting a symlink root none of their code put
  there. It converts only `~/.deeppattern/agent-quality-gates`, the location AQG
  installs to and therefore owns; anywhere else needs `AQG_MIGRATE=1`, and
  `AQG_NO_MIGRATE=1` declines everywhere. It never raises — an install that
  cannot be converted is still a working install — but it never claims more than
  it did either: the message reports whether updates are actually on, because
  reporting "the call worked" printed `Automatic updates: enabled` on the one
  path that leaves them off.
- **A check started by a skill could swap the tree that skill was reading.**
  `scripts/_aqg_context.sh` is what every skill sources to resolve `$aqg_root`,
  and then immediately uses that path to run scripts and read templates out of.
  Starting an update there let the symlink swap land between one dereference and
  the next, leaving a skill running the script from one version against the
  template from another — silent, and the hardest shape to diagnose. The two
  trigger paths are not in the same position: the session-start hook runs before
  any skill does, so nothing is reading the tree and it still applies; the helper
  runs while something is, so it now passes `--check-only` and leaves the swap to
  the next session start, or on a host with no hooks to the next skill
  invocation, by which time the previous one is over. An update can take a
  session longer to land; a tree half one version and half another while a skill
  walks it cannot be observed at all.
- **`doctor` told a machine with a pending update that it was up to date.**
  `check(apply=False)` returned `outcome="current"` with a detail saying an
  update was available and not applied, and `report()` reads the outcome and
  nothing else. There is now a `deferred` outcome that says what happened.

- **A file written with a Bash heredoc got no discipline at all.**
  The PostToolUse construction reminder read `tool_input.file_path`, which a Bash
  payload does not carry, and exited on the empty string — so `cat > src/pay.py <<EOF`
  produced silence while the same edit through `Edit` produced the full gate. That is
  the defect this whole workstream started from. The reminder now also parses
  `tool_input.command` for redirect targets (`>`, `>>`, `tee`, `sed -i`) and gets its
  own matcher entry, `Bash|Edit|Write|MultiEdit`, instead of being one of four scripts
  on the edit-tool entry: the other three read `file_path` only, so widening that entry
  would have invoked them on every shell call to exit immediately, and mounting the
  same script under two matchers makes the installer warn about double-fire.
  It parses rather than scanning the worktree, which was the first design: a worktree
  scan costs a git invocation per shell command, its cheap pre-check does not exist (a
  directory's mtime does **not** change when an existing file is rewritten in place —
  measured), and its dedup would have gone silent for a whole session resumed in a
  dirty tree, or blamed the agent for a human's edit in another terminal. What parsing
  cannot see is stated in the hook and asserted silent by a test — writes that produce
  no shell redirect (`cp` / `mv` / `patch`, editors, an interpreter opening the file
  itself), `sed -i`, variable-built targets, and `>|`. Whether a `>` is a file write is
  decided by what FOLLOWS it: an fd duplication is `>` followed by `&N` and only that is
  excluded, so `2> f`, `1>> f` and `&> f` are all caught while `2>&1` and `>&2` are not.
  Quoted spans and heredoc bodies are removed before anything is parsed, and `tee`
  operands are read with `shlex`, so a `>` inside a commit message or a heredoc document
  is data rather than a redirection and a quoted path containing a space survives whole.

- **A session whose preflight could not run was told nothing, which reads as clean.**
  `sessionstart_preflight.sh` had four early exits before it assembled anything, so a
  non-git directory, a missing `AQG_ROOT`, or an incomplete install produced no
  context at all — precisely the sessions most likely to be flying blind. Three of the
  four are now states passed to the single emitter rather than exits, and the summary
  carries a discipline block: the entry-point line, the policy's own `skip-clause` and
  `gate-a-clause` read from the file that publishes them, and the resolved policy path
  (or the literal `UNRESOLVED`). `python3` is checked first and remains the only hard
  exit, because the emitter is a python3 heredoc. It is **one** emitter, deliberately:
  a hook process may write exactly one JSON object, every reader here does
  `json.loads` over the whole stream, and a second `json.dump` would have concatenated
  to `{...}{...}` and destroyed the summary that already worked rather than adding to
  it. A skip path that used to be asserted silent is now asserted to emit exactly one
  valid JSON object carrying the discipline — the invariant behind the old assertion,
  stated more strongly.

- **Four skill descriptions advertised what the skill contains, not when to call it.**
  A skill's `description` is the text a host loads into the model's context and routes
  on; the body is only read after the skill is selected. So anything a caller needs in
  order to DECIDE belongs in the description, and everything else is competing for that
  budget. `aqg-code-construction` was the worst case and the highest leverage — the
  audit-before-commit gate lives inside it, yet its trigger was the last sentence and
  phrased as a statement (`Triggers on intent to write...`) while four sibling skills
  used an imperative. It now opens with one, in the same breadth as before: imperative
  mood, not a new `PROACTIVELY` token, which the 2026-08-11 over-firing ruling bounds.
  `aqg-multi-review` and `aqg-test-quality-review` moved their trigger conditions ahead
  of their mechanics and now name each other, so the pair a router is least able to
  separate says which is which from both sides. `aqg-phase-transition` swapped dedup and
  override syntax for the depth rule's location and shape — deliberately WITHOUT the ten
  sensitivity categories, because enumerating them would have duplicated
  `aqg-security-review`'s trigger keywords in the same context window while answering
  "how deep" rather than "call me now". `aqg-security-review` is unchanged: its trigger
  was already first-class, and reordering it for uniformity would have been churn on the
  one description that worked. All four are shorter than what they replace, keep every
  canary-locked trigger keyword, add no urgency token, and name no audit depth.

- **The reminder guards proved the hosts agreed with each other, not with the policy.**
  Only the sensitivity categories had a machine-readable source; the sentence around
  them had none, and `docs/policies/audit-trigger.md` said so about itself —
  "Reminder wording at hook time | must be kept consistent with this file
  (**manual today** — no automated derivation exists)". So a coordinated edit to both
  carriers passed, and so did a policy edit with no carrier edit at all. The policy
  now carries the three reminder sentences itself, in a **visible** list under
  *The reminder text every host adapter emits* — visible because text an adapter is
  required to reproduce is not a note to maintainers, and a reader of the rendered
  policy has to be able to see what their tools are telling them. Comment markers
  fence the list for the parser and carry no content of their own.
  `test_every_carrier_emits_the_policys_own_reminder_clauses` compares what each
  adapter ACTUALLY emits against that list, word for word once whitespace is
  collapsed, so the comparison has an authority instead of a quorum. The extraction
  anchors are built from the policy's own sentences rather than restated in the test.
  Reading the list fails closed four ways — a missing fence, an unparseable row, a
  required clause with no row, and a clause the policy publishes that no carrier is
  required to emit. `test_the_two_gate_a_markers_agree_in_both_directions` holds the
  clause's category list equal to `gate-a-tokens` in count, order and naming, in both
  directions, and the unsourced-depth scan now checks the policy's own clauses before
  the carriers. Three claims in the policy that were no longer true were corrected:
  the "manual today" row, and two migration-debt rows that credited the category
  guard with wording coverage it never had. Formatting stays per host — bullets,
  pipes, the resolved pointer path — as the policy's transport clause requires.

- **Two hosts were emitting different Gate A sentences, and the suite was green.**
  `test_audit_gate_sensitivity_list_does_not_drift_between_adapters` asserts every
  policy category token survives into what each adapter actually emits, and its
  docstring already stated the hole it leaves: it checks the tokens, so "a wording
  difference between the two adapters would still pass". One had opened — the shell
  hook emitted `size. The sensitivity list ...` while the Cursor adapter emitted
  `size; the sensitivity list ...`. Cursor is now aligned to the shell wording, which
  the policy's own migration-debt table singles out as the deliberate condensation.
  `tests/behavior/test_gate_a_carriers_do_not_drift.py` compares all three policy
  clauses the two carriers emit — the skip clause, the Gate A sensitivity clause and
  the once-per-change frequency guard — character for character after collapsing
  whitespace, and fails a clause that names a depth the policy did not put there
  (the forbidden set is derived from the `depth-by-stakes` marker, not typed out).
  It compares those clauses and not the whole emission: the policy permits transport
  differences between hosts — bullets versus a pipe-joined line — and forbids policy
  differences, so asserting raw equality would contradict the file it serves. The
  pointer line is excluded for the same reason, since it resolves to a host-specific
  absolute path. A checked-in negative fixture that paraphrases one category, with
  its comments stripped by code so they cannot answer on the clause's behalf, proves
  the token check can still fail. Residual risk is stated in the module docstring:
  this asserts the carriers agree with each other, not that either agrees with the
  policy's prose, because only the category tokens have a machine-readable source.

- **One parser for the policy's machine-readable markers.** `gate-a-tokens` and
  `depth-by-stakes` exist so guards can compare a carrier against the policy instead
  of a hand-maintained copy, but three test call sites had each written the same
  regex — three copies of a single-source parser. They now import
  `scripts/aqg_policy_markers.py`, which raises rather than returning empty when a
  marker is missing, so a broken parse fails loudly instead of turning every
  assertion built on it into a vacuous pass.

- **Five places decided audit depth instead of the policy.**
  `docs/policies/audit-trigger.md` has been the single depth authority since the
  2026-08-11 Owner ruling, but depth was still being set elsewhere: the phase
  router's and emit CLI's docstrings described themselves as a `Phase x Stakes`
  matrix — a framing the code had already dropped, and one that
  `aqg_doctor.RETIRED_RULES_MARKERS` fails an installed rules block for, so the
  router was flunking its own doctor in prose; `aqg-security-review` hard-coded
  `mode=standard` although its trigger list IS the policy's Gate A sensitivity
  list, which routes to `deep` and wins; `aqg-test-quality-review` named no depth
  at all, so its `/audit` calls took the tool default with the policy never
  consulted; and `aqg-multi-review` told the caller
  `mode=<phase-transition recommended>` with no fallback for the common case where
  phase-transition was never invoked. Each now points at the policy instead of
  prescribing a value, and `tests/behavior/test_depth_authority_is_single_sourced.py`
  fails any skill that routes to `/audit` without naming the policy, any `/audit`
  line that prescribes a single literal depth anyway, and any phase-transition
  script that reintroduces the retired framing — plus a fixture pair pinning the
  detector itself, so none of those can pass vacuously. It checks citation and the
  absence of competing values, not whether a skill paraphrases the mapping
  correctly; that stays with `test_router_depth_matches_the_policy_single_source`.
  Still open, deliberately: aqg-multi-review's `description:` frontmatter keeps its
  `mode=<phase-transition recommended>` placeholder, which the description rewrite
  owns along with the sidecar re-sync.

- **Claude never saw any PostToolUse reminder.** Claude Code does not forward a
  hook's stderr to the model when the hook exits 0, and all five PostToolUse
  reminders wrote only to stderr — so on Claude they reached the terminal and
  nothing else, while Codex (which forwards the same scripts' stderr verbatim)
  and Cursor (own adapter) both received theirs. This is why the same discipline
  fired reliably on one host and erratically on another. Every reminder now also
  emits `hookSpecificOutput.additionalContext` on stdout, which IS injected;
  stderr is unchanged, so the terminal view and the Codex fallback still work.
  Verified live against the shipping harness rather than inferred; the behaviour
  is pinned by `AllPostToolUseHooksAreModelVisibleTest` in
  `tests/behavior/test_aqg_hooks.py`, which runs every hook and asserts its JSON
  payload matches its stderr.
- **Shipped skills named a tool the server does not expose.** Nineteen references
  to `de_audit` across five skills and their sidecars — two in the `description:`
  frontmatter the skill picker matches on. They now name the `/audit` skill, which
  is stable across hosts; a scan test keeps it that way.
- **Two pointers named files no installer has ever shipped.**
  `~/.claude/rules/common/audit-self-routing.md` was referenced as the canonical
  depth authority by the reminder hook, `AUDIT_DECISION_MODEL.md` §1 and both
  rules templates. It does not exist, so the "main path" it named never fired.

### Changed

- **The managed update check runs hourly, and the interval is now a setting.**
  It was twenty hours and hardcoded, so every adjustment to it was a code change
  and a release. The default is one hour and `AQG_UPDATE_INTERVAL_SECONDS`
  overrides it — a day of debugging wants minutes, a settled install may want
  longer. The value is clamped between 60 seconds and 7 days; anything that is
  not a whole number of seconds, including `0` and negatives, is treated as
  unset and takes the default rather than raising, because this runs detached at
  session start where an exception is a check that silently never happens. `0`
  does **not** mean "check every time", and stopping the check remains
  `AQG_NO_UPDATE_CHECK`'s job — the ceiling exists so that permanently
  suppressing a signature-verifying updater stays reachable only through the
  switch that leaves no record and is therefore the visible one. The accepted
  cost of the new default is up to 24 git ref reads per install per day, where
  the old one made about 1. The SessionStart launcher forwards the variable
  through its `env -i` allowlist, which is the only path on which the interval is
  ever consulted; `-E` was never what blocked it, since `-E` makes PYTHON\*
  interpreter settings inert and does not touch ordinary environment reads.
- **The audit trigger policy is being collapsed to one source.**
  `docs/policies/audit-trigger.md` now owns when to audit and how deeply for
  everything in this repository; the ladder previously lived in three drifted
  copies here. **One copy remains outside it** — see the Upgrade note. Two escalation gates (sensitivity, complexity) are evaluated
  **before** the size-ordered ladder, because a two-line change to an auth check
  is not a trivial change — the previous first-match ordering made the deep rungs
  unreachable for exactly the changes they existed to catch.
- **The per-edit gate reminder is no longer a mandate.** It read `Required for
  executable-code commits` — categorical, no exemption, re-injected on every code
  file edit — which outranked the policy's own "audit is the exception, not the
  reflex" and drove observed over-firing on Codex. It now carries its own skip
  clause and an explicit sensitivity override, in fewer lines than before.
- **`aqg-phase-transition` no longer decides audit depth.** Its phase × stakes
  matrix routed trivial changes to `standard`, contradicting the policy. Depth
  now comes from the policy's `depth-by-stakes` mapping; the skill decides *when*
  to ask, never how deep. The `recommended_audit_mode` interface is unchanged.
  (Owner ruling 2026-08-11, `docs/decisions/LOG.md`.)
- **Cursor's adapter carries the same policy text as the other hosts.** It
  previously named the gate without the exemption, sensitivity list or pointer.

### Added

- **`docs/policies/audit-trigger.md`** — the agent-neutral policy the two dangling
  pointers were describing, shaped after `dangerous-command-guard.md`.
- **`aqg_doctor` checks the two channels that actually deliver discipline.** It
  validated skills and hook *definitions* and reported 73 PASS / 0 WARN on a
  machine whose `CLAUDE.md` had no AQG rules block at all. It now reports an
  absent block, a block carrying a retired framing, and a hook that cannot reach
  the model. Everything new is WARN, never FAIL; a host that is not installed
  stays quiet.
- **The managed update check now fires on every host that has a session start,
  not just Claude Code.** `sessionstart_update_check.sh` shipped wired into one
  installer; the other five each carry their own hook table and none of them was
  asked, so twelve hook-capable hosts silently never checked for an update.
  Codex, Cursor, CodeBuddy, WorkBuddy AI, Kimi Code, Qoder CLI (both locales),
  Trae (both locales), Devin and Pi now mount it. The four Cursor-family hosts
  mount one adapter command per lifecycle event rather than naming hook scripts,
  so for them the trigger lives in `cursor_aqg_hook.py` and starts *before* the
  adapter's no-workspace return — a session opened outside a repo is still a
  session that should learn its checkout is stale. Everywhere it is a trigger
  only, in no host's blocking set: a slow remote must never stop a session
  starting. Qoder Desktop, Qoder CN Desktop and QoderWork get nothing, because
  AQG installs no session-start event for them at all; that is recorded with its
  reason in `docs/UPDATE_ARCHITECTURE.md` §10.1 and pinned by
  `tests/behavior/test_update_check_host_coverage.py`, which fails when a host
  gains a hook surface and nobody decides what it should do. Those three are
  reached by `_aqgctx_nudge_update` in `scripts/_aqg_context.sh` instead, which
  shares this check's throttle file — but on skill invocation, not on session
  start, so a session that never invokes a skill never checks.
  `docs/UPDATE_ARCHITECTURE.md` §10.1 states that difference rather than calling
  the two paths equivalent, and records that a Windows host is reached by
  neither: both gate on `command -v python3`, which a Windows Python does not
  satisfy.
- **`pytest` no longer starts a real update check.** With the trigger on twelve
  hosts, the suites that drive those adapters as subprocesses were reaching the
  network and writing the developer's own `aqg-state`; `check()` applies by
  default, so a run whose 20-hour throttle had expired could stamp an
  install-state that does not describe the tree it names. The root `conftest.py`
  sets `AQG_NO_UPDATE_CHECK` for the whole run, `test_update_run.py` takes it
  back off because it owns that channel, and the one test there that started a
  real check without saying where its state goes now points at `tmp_path`.

### Upgrade note

`scripts/upgrade.sh` refreshes skills and hooks but **does not rewrite your rules
block** — that has always been a manual `cat >>` step, so an upgrade leaves an
older block in place. If yours predates this release it still carries the retired
main-path/fallback framing. After upgrading:

```bash
python3 "$AQG_ROOT/scripts/aqg_doctor.py" | grep rules_block
```

A `stale rules block` warning means re-syncing the block from the current
`examples/aqg-claude-rules.example.md` / `examples/aqg-codex-agents.example.md`.
Cursor's block is installed by `install_cursor_support.py --apply` and needs no
manual step.

**Codex users: re-run the hook installer and re-trust.** The update check is a
new member of the managed hook set, and Codex pins each hook's definition by a
digest over the runner and that hook's script — so every existing pin is now
stale and Codex is silently degraded until:

```bash
python3 "$AQG_ROOT/scripts/install_aqg_codex_hooks.py" --apply
```

then re-approve the definitions in Codex `/hooks`.

**Codex users: expect two ladders in `~/.codex/AGENTS.md` for now.** The Decision
Engine installs its own routing block into the same file
(`python3 -m installer.codex_routing`), and that block still carries a full copy
of the audit ladder from *before* the escalation gates were added — so a two-line
change to an auth check reads as trivial there. Until it is reduced to a pointer,
the AQG block in that file is the current one; where they disagree,
`docs/policies/audit-trigger.md` wins. Tracked in the policy's
"Known remaining copies" table.

### Changed

- **Centralized backup storage across every installer** — instead of scattering
  `.bak` files and per-client `aqg-backups/` dirs across each client root, all
  installers now stash pre-existing config/assets in one central store
  (`scripts/_aqg_backup.py`). Layout:
  `<base>/<client>/<scope>/<UTC>-<pid>[-N]/<original relative path>` with a
  `manifest.json` recording the absolute `source_root` for restore. `base`
  resolves as `AQG_BACKUP_DIR` (verbatim) → `dirname(AQG_ROOT)/aqg-backups` →
  `$HOME/.deeppattern/aqg-backups`; `scope` is `user` or `project-<12hex of the
  abs project root>`. Retention keeps the newest 10 runs per `<client>/<scope>/`
  (tunable via `AQG_BACKUP_KEEP`; `0` disables, newest always kept).
  - **Uninstall/rollback reads only the central store**; new installs write only
    the central store — no legacy in-place fallback path remains.
  - **Legacy in-place backups are migrated then removed** on the next
    install/uninstall (idempotent, copy-before-delete; a migrated run is
    restore-able like a normal run, tagged `origin="legacy-inplace"`).
  - **The construction hook's overwritten `core.hooksPath`** (a git-config
    *value*, not a file) is centralized too — stashed via a git-config manifest
    entry and restored from the central store on `--uninstall`, replacing the
    in-tree `.aqg/.hookspath_backup`.

## [0.14.1] - 2026-09-06

The first release cut through the signed update channel was 0.14.0 on
2026-09-05; this is the second, and it exists because the public tree changed
while the version string did not. Two different trees answering to one number is
a thing a person cannot check, even though `release_sequence` separates them
mechanically.

### Added

- **WorkBuddy AI and Qoder lifecycle support.** Both clients now go through the
  same registry and installer path as the rest, rather than being partially
  wired. Covers `aqg_client_registry.py`, `install_aqg_clients.py`,
  `install_aqg_qoder.py` and `install_aqg_work_clients.py`, with the client
  support matrix and `AI_SETUP` updated to match.

### Fixed

- **A bare `pytest` at the repository root failed to collect.** Sixteen
  `skills/*/scripts/self_test.py` files errored, so the first thing a new reader
  is likely to run produced a wall of noise. CI ran `pytest tests/` and never
  saw it. Addressed in `pytest.ini`, `conftest.py` and
  `scripts/run_skill_self_tests.sh`.

### Changed

- `PREREGISTRATION.md` states its two constrained claims in a form the outward
  claim gate can read, rather than in one it had to be waived past.

## [0.14.0] - 2026-07-09

Minor: **Behavior Contract** — absorb OpenSpec's requirement/scenario model into the
`aqg-code-construction` Behavior Lock. The free-text step-2 evidence gains an optional
structured contract (RFC-2119 `MUST`/`SHALL` + `GIVEN`/`WHEN`/`THEN`) that tests cite,
the audit gate reuses as acceptance criteria, and closeout renders into durable evidence.
Studied [Fission-AI/OpenSpec](https://github.com/Fission-AI/OpenSpec); design converged
over two Deep audit rounds (`7f24f960` → `0041b460`).

This version also hardens the runtime security gates (evidence gate, boundary gate,
preflight render) and completes the dev-repo open-source-release documentation prep
(WS-8 / WS-10 / WS-9). Each code change went through the construction workflow plus a
Standard 4-voice external audit before commit.

### Added

- **Behavior Contract section** in `templates/code-construction-ledger.md` — a
  `## Behavior Contract` block (`### R<n>` requirement + `- Scenario S<n>.<k>` GIVEN/WHEN/THEN)
  plus a `behavior_contract_exception` header opt-out.
- **`parse_behavior_contract()` + `_check_behavior_contract()`** in
  `aqg_construction_check.py` — a total (never-raises) line-based parser (zero new deps)
  and five **warn-only** advisories: BC0 (parse error surfaced), BC1 (per-requirement
  normative keyword), BC2 (per-scenario GIVEN/WHEN/THEN), BC3 (row-2 `covers:` coverage +
  dangling), BC4 (duplicate id). Advisories run on their own stderr channel — NOT fed to
  the warning-ack gate or the hard-block list — so BC **cannot change the exit code** in
  0.14.0 (warn-only Phase 1). Exit code 7 is reserved for the Phase-2 hard block.
- **Closeout render** — `aqg-evidence-closeout` imports the shared parser and renders the
  **full** contract (requirement id + normative statement + each scenario's GIVEN/WHEN/THEN
  lines) into durable evidence, secret-redacted, malformed-safe (never crashes / dumps raw).
- **Docs** — `aqg-code-construction` SKILL §4b, `docs/TESTING_METHODOLOGY.md` §2.5, the
  LOCKED DesignSpec (`docs/discussion/2026-07-09-behavior-contract-openspec-absorb-a3.md`),
  and a warn→block gate-rollout record (`docs/gate-rollouts/2026-07-09-behavior-contract-warn.md`)
  with the Phase-2 promotion criterion.
- **Tests** — `tests/test_aqg_behavior_contract.py` (28) + `tests/test_aqg_closeout_behavior_contract.py` (9),
  covering per-requirement/per-scenario granularity, `covers:` coverage/dangling, the
  crash-safety warn-only-exit-0 invariant, exception null/vague handling, legacy-ledger
  backward compat, durable render, and secret redaction.
- **Outward-narrative overclaim gate** — `scripts/scan_overclaim.py` +
  `scripts/_overclaim_terms.py`: a reusable, fail-closed check enforcing the D4×D5
  constraint — until an empirical benchmark exists, outward copy may describe MECHANISMS
  but must not assert causal quality outcomes (e.g. "fewer bugs", "higher quality"). A
  `--tree <dir>` mode walks a whole tree (blocklist code/data/binary, refuse nested
  append-only history, fail-closed on a partial walk); root append-only history
  (`LOG.md`/`CHANGELOG.md`) is exempt so a historical mention never deadlocks the gate.
  Hardened over several Deep audit rounds.
- **Community files** — `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1) and
  `.github/ISSUE_TEMPLATE/` (bug report + feature request + config routing security
  reports to private advisories). The public repo is a read-only mirror; PRs are not
  accepted (`CONTRIBUTING.md`).

- **`required_context` quality gate** — a new, backward-compatible config gate
  (`gates.required_context`, default off) that fails when a declared
  `project.required_context_files` entry is missing under the target repo, with in-repo
  containment enforced (an absolute path, a `..` escape, or an out-of-repo symlink counts
  as missing) — `scripts/run_quality_gates.py` + `scripts/quality_gates_config.py` (#487).

### Security

- **Evidence gate: an `unauthorized` production / secrets / raw-data boundary status now
  surfaces a finding** instead of silently clearing the gate — `validate_boundaries` in
  `scripts/run_quality_gates.py`. Warn mode surfaces it; blocking mode denies (#484).
- **Evidence gate now requires a genuine evidence heading** — a headingless document that
  merely scatters the required labels no longer passes; the headingless fallback is kept
  for lenient item extraction only (`scripts/check_evidence_closeout.py`) (#485).
- **Preflight renders untrusted git/gh output fence-safe** — the working-tree status block
  and PR/issue listings neutralize backticks (a malicious filename or PR/issue title can no
  longer break the markdown fence and inject into the agent-facing report), and
  `sanitize_external` now strips the trojan-source bidi / zero-width control set
  (CVE-2021-42574), with ZWNJ/ZWJ deliberately preserved — `skills/aqg-startup-preflight`
  (closes #364) (#488).

### Docs (open-source release prep — WS-10 / WS-9)

- Reconciled the secret-scan fail-closed vs skill-validator warn-open gate error philosophy
  in `docs/ENGINEERING_FRAMEWORK.md` (+ zh-CN twin) (#486).
- Rescoped `THIRD_PARTY_NOTICES.md` to this repository (no bundled third-party code) and
  corrected a stale `scripts/_aqg_context.py` → `.sh` reference in `SKILL_AUTHORING_GUIDE.md`
  (#489); removed the "protected-IP release package" strategy sentence from two skill docs
  per decision D4 (#490); made `AQG_PRIVATE_READ_TOKEN` optional in the GitHub Actions
  example workflows (#491); converted dead CHANGELOG PR/issue hyperlinks to plain text
  (#492); corrected a stale skill count in a trigger-canary docstring (#493).

### Internal (release tooling — not shipped)

- **Public-release hard gate** (`internal/carve/`) — the public mirror is produced by a
  single fail-closed path chaining a content gate (secret/residue/allowlist), the
  overclaim gate, a git-metadata identity gate, and a provenance record, with a governed
  override scoped to the overclaim gate alone. Hardened over multiple Deep audits.

## [0.13.0] - 2026-06-24

Minor: the **16th skill** (`aqg-decision-capture`) + a Phase 0 decision log turn "why did we
decide this" into a grep-able, redaction-safe one-line record; plus a layer of commit-time
**secret-scan + tamper-resistance** hooks, a true **WIP checkpoint** promoted to a managed hook,
a preflight **recent-commits** echo, and a batch of adjudication / debugging hardening.
44 PRs (#306–#366).

### Added

- **`aqg-decision-capture` — the 16th skill** (#361 design, #362 impl, #363 dogfood, #366 audit
  fixes) — captures durable decisions (Owner rulings / agent autonomous choices /
  agent-via-audit adjudications) as one 6-field line (`date | actor | decision | rationale | basis |
  supersedes`) in `docs/decisions/LOG.md`. Three read-only subcommands: `format` (emit + validate
  a candidate line; all-field secret-scan; an `actor=agent` row requires ≥1 permanent basis
  pointer), `query` (grep + read-time redaction, each hit fenced as untrusted `<decision-data>`),
  `validate` (re-lint every line's grammar + secret, fail-closed). Does NOT write the log — caller
  appends, owner-confirms. The design went through a 6-voice Deep audit (`a35e7dec`); the impl + an
  8-finding Deep re-audit (`999eeccc`) closed a validate-vs-format grammar gap and a regex/parser
  inconsistency.
- **Phase 0 decision log + capture discipline** (#317) — `docs/decisions/LOG.md` as a lightweight
  append-only timeline, with the capture-trigger rules synced into the CLAUDE.md / Codex rule
  templates (#349).
- **WIP checkpoint → managed hook** (#355 true code snapshot to `refs/aqg-wip`; #356 promote to
  managed so every `--apply` machine gets it) — snapshots the working tree (incl untracked) to a
  private dangling commit on Stop / PreCompact, zero-touching worktree / index / branch; SessionStart
  surfaces a recovery prompt (never auto-restores). AQG-vs-EAF execution-substrate boundary
  documented (#357).
- **Secret-scan PreToolUse gate** (#330; exec-bit fix #331) on Write/Edit/MultiEdit/NotebookEdit/
  Bash, plus **two dormant deny gates activated** by fixing the block exit code 1→2 (#332; deny-contract
  smoke probe #343).
- **`aqg-startup-preflight` recent-commits echo** (#365) — preflight now lists each repo's last few
  commits so a continuation session can spot a stale-handoff baseline at a glance.
- **AQG-workflow benchmark MVP** (#320 instrument + selftest; #321 feasibility probe; #327/#334/#335
  three-arm runner) — a synthetic-task harness making "does the AQG workflow actually fire in
  headless `claude -p`" a falsifiable measurement.
- **code-construction YAGNI ladder** absorbed into step 4 (#319); a **skill-body safety-boundary
  canary** (#318) guards each skill's Production / secrets / Owner-admin / read-only promises.
- **GitLab CI quality-gate template** for non-GitHub runners (#353); a **CI-template regression
  guard** (well-formed YAML + embedded-shell shellcheck) for the shipped templates (#359).

### Changed

- **Hook / pattern-bank tamper-resistance (#328)** — a secret-scan tamper canary (#336), a
  dangerous-guard tamper canary (#344), and a PreToolUse anti-tamper guard (#360, opt-in) all fail
  closed when the engine / pattern bank is neutered.
- **`aqg-systematic-debugging` step 7** is now stakes-aware — regression scope scales with stakes
  (#352).
- **Rules-template sync** — no-Co-Authored-By commit convention (#345), a required AQG discipline
  anchor in `spawn_task` prompts (#347), and the Codex template kept in step with the Claude one
  (#348).
- **AQG stays a discipline layer, not a CI/CD platform** — positioning recorded + corrected after
  withdrawing two line-crossing gap-fillers (#350, #351, #354); plugin-ization deferred to a later release
  (#324).

### Fixed

- **`aqg-audit-adjudication` hardening (#306)** — CJK false-friend disambiguation in
  needs_user_detail (#337), an `administrator` actor marker (#338), blank-line-orphaned decision rows
  now flagged (#339), the GFM no-leading-pipe fail-safe locked (#340), converter fail-closed on
  silently-dropped orphan rows (#341), and a single shared header predicate (#342).
- **Handoff copy-box** — block triple-backtick fences in the handoff body so the single paste box
  never breaks (#346).

## [0.12.1] - 2026-06-20

Patch: a `aqg-session-handoff` fix so a handoff always copies as one block. One PR (#315).

### Fixed

- **Handoff copy-box no longer breaks on a code fence** (#315) — a filled handoff whose
  body contained a triple-backtick code fence broke the single paste box (the inner fence
  closed the outer one), so the next-session reader could not select the whole handoff at
  once and had to re-paste the tail. The fix is proactive: the `new` skeleton hints (First
  step / Current Working State / Next) and the SKILL.md fill guidance now tell the author up
  front to use 4-space-indented commands instead of a fence; `validate` adds a non-blocking
  backstop warning if one slips through (never a hard failure).

## [0.12.0] - 2026-06-19

Minor: consolidated + hardened the audit-adjudication validator, and turned the "parallel agent sessions on one repo" hazard into a codified-then-detected discipline; plus an EAF decoupling of `aqg-session-handoff` (#309) and a needs-user-decision actor-matching fix (#308). Seven PRs (#307–#313).

### Added

- **`aqg-startup-preflight` concurrency / shared-tree advisory** (#313) — preflight now
  surfaces an advisory (never a blocker) about a possibly shared working tree, from two
  **independent** signals: a tree already dirty at session start (uncommitted changes that
  are not this session's — possible shared-tree contamination), and `>1` registered
  `git worktree` (a reminder to confirm you are in your own isolated one). Read-only; skipped
  for missing repos.
- **`aqg-skill-validator` read-only wrapper-sync check** (#312) — the validator now flags
  (under `--strict`, failing) a managed skill whose committed Claude wrapper is out of sync
  with its source SKILL.md. Closes the local gap where editing a SKILL.md without
  regenerating its wrapper passed `--strict` yet broke CI.
- **Parallel-session worktree isolation discipline (codified)** (#311) — a new rule in the
  CLAUDE.md rules template (`examples/aqg-claude-rules.example.md`) + the
  `aqg-startup-preflight` boundary: concurrent agent sessions work in isolated `git worktree`s,
  never a shared checkout; a shared/dirty tree's pass/fail is untrustworthy (verify on an
  isolated base); prefer single-target commands (`regen <skill>`, not a repo-wide
  `regen --all`). The matching runtime detection is the #313 advisory above.

### Changed

- **Merged the two adjudication-table validators into one engine** (#307) — the gate-pipeline
  validator (`scripts/check_audit_adjudication_table.py`) and the skill validator were
  consolidated into a single `scripts/validate_audit_adjudication.py` (the LAX normalization
  parser and the STRICT validation path kept deliberately distinct). Consumers, tests, docs,
  and the skill manifest repointed; behavior preserved (verified against an A∪B invariant suite).
- **Decoupled EAF from `aqg-session-handoff`** (#309) — the generic session-handoff skill is
  now EAF-agnostic; EAF-specific workspace detection / routing was removed so the skill
  installs cleanly without the sibling EAF project.

### Fixed

- **Adjudication validator hardening** (#310) — landed the remaining accepted findings from
  the validator-merge Deep audit (#306). Reader-visible impact: the converter no longer corrupts
  a cell that contains a shell pipeline (it escapes the pipe, code-span aware, instead of
  rewriting `|`→`/`), and a malformed table now **fails closed** — a duplicate required column
  or a silently-dropped row raises instead of shipping a wrong/incomplete adjudication table.
  (Under the hood: `normalize` final-strips, `_is_separator` requires a hyphen, `filled` treats
  only a whole-cell `<...>` token as a placeholder, `split_cells` matches backtick spans by run length.)
- **`needs_user_detail` word-boundary actor matching** (#308) — the needs-user-decision
  "names who and what" check used bare substring matching, so an object word like `production`
  satisfied the actor test via its embedded `product`. Actor markers now match as whole words
  (ASCII-boundary, plural-tolerant, CJK-glued tolerant); object markers stay substring so
  inflected forms keep matching.

## [0.11.5] - 2026-06-18

Patch: finish the Node 24 CI migration — the SHA-pinned `softprops/action-gh-release` was the last action still on Node 20. One PR (#304).

### Changed

- **`softprops/action-gh-release` bumped to v3.0.0** (#304) — `release.yml` pinned the
  third-party release action at a v2.6.2 SHA that still declared Node 20; the v0.11.4
  release run force-migrated it to Node 24 with a deprecation annotation. Re-pinned to the
  v3.0.0 SHA (`b4309332…`, `runs.using: node24`) — its only change v2.6.2 → v3.0.0 is the
  Node 20 → Node 24 runtime move (no input/behavior change; `name` / `body_path` / `draft`
  / `prerelease` / `GITHUB_TOKEN` unaffected). Stays full-SHA-pinned per #239. Closes the
  Node 24 migration started in #301 (which covered the official `actions/*`).

## [0.11.4] - 2026-06-18

Patch: CI maintenance (Node 24 action runtimes) + an `aqg-session-handoff` doc fix to stop temp-file name collisions. Two PRs (#301, #302).

### Changed

- **CI actions bumped to Node 24 runtimes** (#301) — `actions/setup-python@v5` (Node 20)
  → `@v6` and `actions/setup-node@v4` (Node 20) → `@v6` across the workflows, ahead of
  GitHub's forced Node 20 → Node 24 migration (2026-06-16 default flip, 2026-09-16 runner
  removal). `actions/checkout@v6.0.2` and `actions/upload-artifact@v7.0.1` already run on
  Node 24 (verified via each action's `runs.using`); the third-party
  `softprops/action-gh-release` stays SHA-pinned. No behavior change.

### Fixed

- **`aqg-session-handoff` temp-file name collisions** (#302) — the How-To example used a
  fixed scratch filename (`/tmp/handoff-filled.md`) for the intermediate handoff that `new`
  writes and `validate` reads. Because the skill is read-only (it never names the file
  itself), callers copied the fixed name verbatim, so parallel sessions / repeated handoffs
  overwrote each other. The example now uses a timestamped unique name
  (`/tmp/handoff-$(date +%Y%m%d-%H%M%S).md`) with a note explaining why. Docs only — the
  script stays read-only.

## [0.11.3] - 2026-06-17

Patch: fix a false-positive in `aqg-session-handoff validate`. One PR (#299).

### Fixed

- **`aqg-session-handoff validate` continuity-footer strip** (#299) — `validate` no
  longer false-positives `R7 Open decisions ... actor not named` when the handoff carries
  the continuity footer (#295) and the footer's long bullet lines were re-wrapped on a
  round-trip. The old exact end-anchored `endswith()` strip broke on any inserted
  whitespace, leaving the whole footer inside section 8 where its lines were counted as
  no-actor decisions. The strip now anchors on the LAST sentinel and matches the footer
  body whitespace-insensitively, with any below-footer content folded back into the body
  so it is still validated — the two invariants from #295 (a sentinel quoted in the body
  can't truncate real sections; content below the footer can't escape the rules) are
  preserved, and the secret scan still covers the full text.

## [0.11.2] - 2026-06-12

Patch: a scannable at-a-glance scorecard up front in the status report. One PR (#297).

### Added

- **`aqg-project-status` at-a-glance scorecard** (#297) — the business overview now
  leads with a one-line KPI row (`milestones done · open decisions · open defects`)
  right after the health one-liner, so the key counts are visible without scanning
  the sections below. Rendered in all human formats (html / markdown / text); the
  `json` already carries the derivable data. Counts are view-computed facts protected
  by the §4.2 translation fact-guard (renderer chrome, never a translation segment).
  The chip is suppressed for an all-cancelled milestone set and a decisions-untracked
  view, so it never asserts `0/total done` or a spurious `0 open decisions`.

## [0.11.1] - 2026-06-12

Patch: close the handoff dogfooding gap so the *next* session actually uses AQG
(it was reading a proper AQG handoff but doing manual `git` equivalents instead
of invoking the skills). One PR (#295).

### Added

- **`aqg-session-handoff` continuity footer** (#295) — `new` appends a footer
  after section 8 carrying the two things sections 1-8 don't say: invoke the
  skills (don't do manual `git status` / `gh api` equivalents), and the next
  session writes ITS handoff with the skill too — breaking the
  freestyle-degradation chain (each session less AQG-disciplined than the last).
  `validate` strips it by exact end-anchored suffix before section parsing (so a
  sentinel quoted in the body never truncates real sections, and content below
  the footer can't escape the rules); the secret scan still covers the full text.

### Fixed

- **SessionStart auto-preflight cwd-scope hint** (#295) — the auto-preflight only
  covers the session cwd; when the work repo differs (a handoff points to another
  repo) the injected summary now nudges re-invoking `aqg-startup-preflight` on the
  actual work repo instead of doing manual git checks — the root cause of an
  auto-preflight that ran on the wrong repo (cwd was one repo, the work another).
  The cwd path is sanitized before interpolation into model context (audit 2c4654ce).

## [0.11.0] - 2026-06-11

Handoff enforcement + hooks installed by default, a stream-json fix that unblocks
headless / image-attach `claude`, the session-handoff skill absorbing an
externally-audited handoff-prompt design, and CI robustness. Seven PRs (#287–#293).

### Added

- **UserPromptSubmit handoff-mandate hook** (#287) — when the user asks for a
  handoff (handoff / handover / next session), injects a hard mandate to invoke
  `aqg-session-handoff` instead of freestyling; selective (silent on non-handoff
  prompts) and never blocks. 10th managed hook.
- **`aqg-session-handoff --minimal` degraded mode** (#292) — a 3-section
  CTX-exhausted handoff (mission / 🟡 stop-point / first step) for the
  no-budget / passive-compaction path; the secret-scan still runs. Plus
  fill-guidance enrichment absorbed from the audited design: evidence anchoring
  vs memory drift, reverse-order fill, reader-simulation + cross-section
  consistency self-checks, no-raw-output source control — and a soft 150-line
  length advisory (non-failing).
- **PreCompact/Stop reminder points to `--minimal`** (#293) — surfaces the
  degraded path when there is no budget for the full 8-section handoff.

### Changed

- **Hooks install by DEFAULT** (#288) — flipped from opt-in to default-on. Pack
  install prompts `[Y/n]` (defaults to Yes); `upgrade.sh` installs the managed
  set on a fresh machine, refreshes an already-managed set, and leaves an
  explicit warn-only opt-in untouched; `AI_SETUP.md` installs them too. `--no-hooks`
  opts out. "Installed AQG = the enforcement layer is resident."

### Fixed

- **SessionStart preflight no longer kills `--input-format stream-json` / headless
  `claude`** (#289) — its plain-text stdout was read as a malformed event line and
  aborted the session (broke image-attach + audit subprocesses). Now emits the
  `hookSpecificOutput.additionalContext` JSON form (which also injects in more
  modes than plain stdout did); `errors="replace"` so non-UTF-8 repo output no
  longer discards the whole summary.
- **Warn-only quality-gate no longer reds on a transient `gh` failure** (#290) —
  `fetch_pr_body.py` degrades to a visible skip instead of crashing the job when
  `gh pr view` hits a GraphQL 401 / network error / timeout.

### CI

- **quality-gates adapter is now PR-CI-gated** (#291) — `tests/test_run_quality_gates.py`
  plus `scripts/fetch_pr_body.py` / `run_quality_gates.py` added to
  `behavior-tests.yml`; previously they had zero PR coverage (the #290 fix was
  verifiable locally only).

## [0.10.0] - 2026-06-10

Pre-launch wave (launch-candidate cut): a new cross-session handoff skill (the
15th), an AI-executable self-install guide, a single source of truth for audit
orchestration, the Project-status business-progress ledger, plus a batch of
skill-sidecar hygiene and hook-config hardening.

### Added

- **`aqg-session-handoff`** (15th skill) — generic cross-session handoff: emits a
  paste-ready handoff prompt (background + precise state + next step + discipline
  traps) at CTX-near-limit / post-compaction / handoff, plus precompact/stop hook
  reminders and a First-step preflight nudge (#272–#277).
- **`AI_SETUP.md`** — an AI-executable self-install guide: paste it to a Claude
  Code / Codex session and the agent installs AQG + idempotently re-syncs the rules
  into `CLAUDE.md` / `AGENTS.md` (first-install + drift re-sync; with backup,
  segment-bounded edits, and rollback) (#281).
- **`docs/AUDIT_DECISION_MODEL.md`** — the single source of truth for audit
  orchestration (who decides depth, the external-audit chokepoints, dimensions,
  self-review-vs-external, term map); the rule templates now point to it (#278).
- **`CONTRIBUTING.md`** — collaboration runbook; headlines the parallel-AI-session
  per-worktree rule (avoids shared-HEAD contention) (#285).
- **Project-status Ledger v1.1** — milestone + decision event contract, projection
  reduction (completion / health-light / coverage), and an injection-proof
  business-progress report (#263–#267).
- **phase-transition `high_stakes_skip_confirm`** — a high-stakes audit skipped via
  explicit user opt-out now flags a required second confirmation (signal-only;
  still honors ADR §4 invariant 4 — the user can skip, but confirms twice)
  (#284, issue #282).

### Changed

- **Audit-orchestration consolidation** — `second-review/double-review/cross-validation/re-confirm` reconciled to
  `standard` across `audit-self-routing.md` (main path) and the phase-transition
  router (fallback); rule templates + README skill accounting aligned to 15 skills
  (#278–#280).
- **Explicit hook blocking flag** — `_AQG_BLOCKING_HOOK_SCRIPTS` is now the single
  source of truth for which hooks block (a `raise`, not an `assert`, so a mis-named
  security gate cannot silently downgrade to warn-only under `python -O`); the
  installer-vs-example drift test reads it instead of reverse-engineering the
  ` || true` tail (#285).
- **Skill-sidecar hygiene** — boundary-metadata alignment to GUIDE §6.1,
  output_shape/path drift fixes, and description-length compression across several
  skills (#263–#271).

### Fixed

- **`settings.blocking.example.json`** silently dropped the PreToolUse
  `memory_write_guard` blocking gate — restored, plus a test that locks the
  hand-copy example to the installer's canonical hook set (#283).
- **automation-audit** plugin-hook liveness false positives from a
  disabled / stale-cache cross-join (#271).

### Notes

- The README version header is brought current in this release (it had lagged at
  `0.8.5` since the release process bumps `VERSION` + `CHANGELOG` only).
- Dated provenance preserved verbatim (`docs/audit-evidence/`, `docs/discussion/`,
  historical `CHANGELOG` entries).

## [0.9.3] - 2026-06-06

audit-mcp tool rename: the backend Decision Engine renamed its audit tool
`gpt_audit` → `de_audit` (live MCP tool `mcp__decision-engine__de_audit`; the old
`mcp__audit__gpt_audit` registration is gone). This release migrates every LIVE
reference across the skill pack to the new name. The project name "audit-mcp" and
the `audit-self-routing.md` filename are unchanged.

### Changed

- **`gpt_audit` → `de_audit`** across all LIVE surfaces: the 6 skills that
  reference the audit tool (code-construction / multi-review / phase-transition /
  re-anchor / security-review / test-quality-review) — their `SKILL.md` source +
  regenerated Claude wrappers + `skill.template.json` sidecars + helper scripts —
  plus `examples/*.example.md`, the live `docs/decisions/*` ADRs, top-level
  `scripts/` references, `README.md`, and the post-tool-use construction hook
  (fully-qualified ids → `mcp__decision-engine__de_audit`).
- **aqg-security-review three-layer table** — the LLM-external-review row's stale
  mode enum `(single / two / three)` → `(fast / standard / deep)` (and the same
  `mode=fast|single|two|three` in the construction hook), completing the #258
  mode migration that missed these spots.

### Notes

- Dated provenance is preserved verbatim — `CHANGELOG.md` historical entries,
  `docs/audit-evidence/`, and `docs/discussion/` keep their original `gpt_audit`
  references (not rewriting history). `tests/transfer/` (which invokes the audit
  tool + carries golden reproduction fixtures) is left for a separate test-infra
  follow-up.

## [0.9.2] - 2026-06-04

Upgrade ergonomics: a default upgrade now auto-refreshes managed hooks on a
machine already opted into them, so newly-shipped hooks land without re-running
`--hooks`.

### Added

- **auto-refresh managed hooks on default `upgrade.sh`** (#257) — a machine that
  has opted into the managed hook set (≥1 managed hook) is refreshed UP to the
  current canonical set on a plain `scripts/upgrade.sh` (no `--hooks`), so a
  newly-shipped hook (e.g. security-review) lands without re-running `--hooks`.
  warn-only-only and never-installed machines stay byte-for-byte untouched —
  preserving opt-in and keeping the cf5adc7f warn-only→full-blocking mis-promotion
  guarded. New `install_aqg_hooks.py --is-installed` (managed-set-only detection).

### Changed

- **`docs/DISTRIBUTION.md`** — record the unified install cli = `deeppattern-cli`
  (one binary across the toolkit's install surface).

## [0.9.1] - 2026-06-04

Post-0.9.0 follow-ups: a security-review event hook, plus a memory-hygiene
description-limit fix and the CI guard that closes the gap which let it land.

### Added

- **security-review event hook** (#255) — `posttooluse_security_review_reminder.sh`:
  a PostToolUse(Edit\|Write\|MultiEdit) on a security-sensitive file (path keyword OR
  content high-signal OWASP pattern) prompts `aqg-security-review`. Warn-only, never
  blocks. Wired across installer / settings.blocking / README (Hook layer 8→9).
- **description-limit corpus guard** (#256) — `tests/behavior/test_wrapper_description_limit.py`:
  every managed skill's description (wrapper / source / sidecar) stays ≤1024 chars
  (validate_agent_pack hard limit), now enforced in CI-run `tests/behavior/` — closing
  the gap that let an over-limit description ship (validate_agent_pack is not in CI).

### Fixed

- **`aqg-memory-hygiene` description** (#256) — compressed 1057→1010 to clear the
  1024-char hard limit (a #253 leftover); trigger keywords preserved, byte-identical
  across source / wrapper / sidecar.
- **security-review hook robustness** (#255) — content scan no longer SIGPIPE-misses
  an early match in a large file under `pipefail`; test-file skip covers rs/java/kt/
  swift/rb conventions (commit-gate audit f1/f2).

## [0.9.0] - 2026-06-04

Largest hardening wave since 0.8.5 (~85 commits): two new skills, the executable
Project Ledger, write-time memory governance, the source→wrapper generator
migration, and an L1–L4 pre-launch robustness pass — most findings cross-LLM
audited (double-review/triple-review).

### Added

- **`aqg-memory-hygiene`** (#253) — signal-only memory-lifecycle scanner
  (frontmatter `validate` + `staleness`); read-only, never mutates memory. Plus a
  fresh-review hardening follow-up (#254: durable-label / repo-traversal /
  node_type-recursion / cycle-safe).
- **`aqg-project-status`** (#168) + the **AQG Project Ledger** — executable
  contract, append-only `events.jsonl` store, projection→ProjectView render, and
  the v2 non-EAF hook capture writer (#163–#180).
- **PreToolUse memory-write guard** (#162) — blocks code-shaped content from
  landing in machine-local memory.
- **security-review** OWASP 2025 + CWE 2024/2025 net-new checks (#184);
  **skill-validator** emit-only SKILL.md quality judge (#197); **multi-review**
  convergent-findings confidence boost (#196).
- Opt-in advisories: preflight `--check-codemap` (#165), project-status
  `--with-repo-reality` reconciliation banner (#252).

### Changed

- **skill-gen wrapper-overlay migration complete (13/13)** (#193–#206) — single
  `.tmpl` source of truth + continuous generator + git-diff gate; retires the
  drift-hash re-baseline ritual.
- **`docs/DISTRIBUTION.md`** — new single-entry index for the release posture and
  rollout backlog.
- External-benchmark absorption (#183 Tier A+B) folded into debugging / testing /
  security guidance.

### Fixed

- **L1–L4 pre-launch hardening** (~50 PRs, most cross-LLM double-review/triple-review):
  redaction-engine false-negative bypasses + input-length caps (#207–#209,
  #225–#238, #248), dangerous-command-guard robustness (#210, #229, #250),
  validator / manifest / installer / doctor input-robustness (#211–#235,
  #241–#247), skill-gen generated-artifact injection-proofing (#218–#222).

## [0.8.5] - 2026-05-28

One-command upgrade + zero-residue install + opt-in hook discoverability — makes
upgrading an already-installed machine reliable, self-cleaning, and obvious.

### Added

- #157 —
  **`scripts/upgrade.sh`**: one command replaces the error-prone 5-step manual
  upgrade (pull / `--ref` pin / `--clean-only` → reinstall Codex + Claude skills
  → optional `--hooks` → doctor). Adaptive (skips absent clients); refuses the
  git update on a tracked-dirty tree and exits non-zero rather than report false
  success.
- #157 —
  **zero-residue install**: both `install.sh` now prune stale skill symlinks
  (renamed / removed skills, incl the 0.3.0 `ai-team-*` rename). Tight boundary:
  only symlinks whose target is exactly `$skills_root/<one-segment>` — never real
  directories, third-party links, or `..`-escaping targets.
- #159 —
  **install-time hook discoverability**: the Claude installer recommends the
  opt-in hooks (plus a `[y/N]` prompt at a real terminal — never when scripted),
  and `upgrade.sh` recommends `--hooks`. Users no longer install the skills and
  silently miss the resident gates.

### Fixed

- #158 —
  `upgrade.sh` hooks are now strictly **opt-in**. The first cut auto-installed the
  full hook set whenever a broad marker matched `settings.json` — which also
  matched the documented warn-only example, risking promotion of a warn-only
  opt-in to the blocking set. Default now never touches `settings.json`; `--hooks`
  installs.
- #159 — the hook
  recommendation now states the `AQG_ROOT` runtime requirement (hooks silently
  skip if it is unset, so they would install but be inert) and corrects the
  PreToolUse "silent unless…" wording.

### Changed

- #158 —
  **`README.md` + `docs/INSTALL_VERSIONING.md`**: `scripts/upgrade.sh` is now THE
  documented upgrade command (replaced the stale multi-command blobs); the install
  flow cross-references the "Hook Layer" section and the `AQG_ROOT` runtime step.

## [0.8.4] - 2026-05-28

`aqg-re-anchor` (12th skill) + `smoke_test.sh` + 2 new hooks + a 12-skill
trigger-keyword canary + a Codex `display_name` validator gate, on top of the
production-readiness hardening line — plus trigger-word / README / generator
catch-up and post-merge double-audit hardening.

### Added

- #140 —
  **`aqg-re-anchor`** (12th skill): emit-only re-anchor restatement (goal +
  active discipline gates + progress ✓▶·) for a long *orchestrating* session at
  step / WorkPacket boundaries, fighting goal-drift + attention decay. Routed
  from an EAF discussion (verified: the one-shot worker doesn't drift — its
  whole context is one bounded WorkPacket — so ② "restate to re-anchor" is a
  general orchestration-layer discipline → belongs in AQG). emit-only mirrors
  `aqg-phase-transition` (never calls audit / mutates / self-injects; caller
  owns cadence + injection). single-audit `d607d662` f1 (data-not-instructions
  disclaimer) + f2 (current-item-beyond-display-cap) fixed before merge.
- #150 —
  `scripts/smoke_test.sh`: one command for all 4 local verification layers
  (doctor / agent-pack validation / 12 skill self-tests / full pytest), ~20s.
  Also fixed an `automation-audit` self-test 167s bottleneck (`AQG_SKIP_MCP=1`).
- #149 — 2 new
  hooks: PostToolUse(Bash) on error → `aqg-systematic-debugging` reminder;
  PostToolUse(Edit|Write) on test files → `aqg-test-quality-review` reminder.
- #151 — 12-skill
  trigger-keyword canary (`tests/behavior/test_aqg_skill_trigger_canary.py`):
  word-boundary asserts each skill's description still carries the terms Claude
  Code routes on, and names the missing keyword (complements `test_drift.py`).

### Fixed

- #142 —
  `aqg-re-anchor` post-merge **double-audit** (gpt-5.5 + gemini, `4404b8a3`)
  hardening: sanitize now strips Unicode line/paragraph separators (U+2028/9),
  C1 controls (U+0085/U+009B), and bidi (U+202x/U+206x) — an LLM parses these as
  newlines, so a caller value could otherwise break out of its single-line
  prefix and spoof an instruction (stricter than the borrowed human-terminal
  `anchor_render`, because re-anchor feeds an LLM). Plus encoding robustness
  (invalid-UTF-8 input + ASCII stdout no longer crash the "never raises"
  contract) and bounded pre-truncation work.
- #141 —
  `tests/test_v080_hooks.py` session-start preflight test made deterministic
  offline (was env-flaky when the preflight hook's `git fetch` exceeded its 20s
  timeout).
- #154 — 4 skills
  (automation-audit / multi-review / phase-transition / security-review) were in
  `CODEX_SKILL_NAMES` but shipped without `agents/openai.yaml`, so the Codex
  picker fell back to the slug and rendered "Aqg <Name>". Added the 4 manifests +
  a hard validator gate (`_validate_codex_interface`) + generator scaffold + a
  CI-gated roster guard so it cannot recur (double-audited `57f41525`).
- #152 —
  `aqg-evidence-closeout` wrapper description restored the user-facing trigger
  words "closeout" / "handoff" (picker text Claude Code routes on); drift +
  canary baselines re-based.
- #155 — skill
  generator: dropped an unfounded 70-char `short_description` cap (shipped values
  run to ~97) and added the trigger-canary step to the `GENERATED.md` checklist
  (release audit `b9d506d2`).

### Changed

- `README.md` skill listing **10 → 12**: caught up `aqg-test-quality-review`
  (#124, review-side test-quality) and `aqg-re-anchor` (#140), both merged but
  not previously listed.
- #148 — hook
  script filenames made version-neutral (dropped the `v0.8.0` prefix).
- README catch-up #144–#147:
  version badge → 0.8.4, stale skill/hook counts + trigger table corrected, hook
  section modernized.
- #153 — recorded
  in the trigger-canary docstring why it targets the pack (wrapper) `SKILL.md`
  (the runtime symlinks `~/.claude/skills/<x>` to the agent-packs wrapper).
- Production-readiness hardening line #124–#139
  (summary; internal): CI self-test gate (#128), self-computing fixture gate
  (#126), 3-batch cross-vendor double-audit of the process-gate skills
  (#130/#132/#133), and the Finding-D shared-resolver migration (#136/#138).

## [0.8.3] - 2026-05-19

TDD enforce ladder + audit-before-commit gate. 2 PRs bundle implementing Owner's
2026-05-19 "Option A" decision: top-tier LLM (Opus 4.7 / GPT-5.5) + cross-vendor
audit as the backstop, **rejecting** cross-LLM test-writer (redundant + adds dependency +
lowers robustness). Background reasoning chain is documented in detail
in the PR #121 commit body.

Bundle contains 2 PRs:
- #121 —
  `aqg-code-construction` Step 2 vertical-TDD anti-horizontal ban +
  Step 5 audit-before-commit gate
- #122 —
  `posttooluse_code_construction_reminder.sh` v0.8.3 warn-only reminders
  surfaced

### Added

#### `skills/aqg-code-construction/SKILL.md` — Step 2 vertical TDD ban + scope caveat (#121)

Step 2 Mode A TDD cycle adds:
- **Explicit ban on vertical slicing**: 1 slice = 1 RED → 1 GREEN → 1 (optional)
  REFACTOR. Cross-link `docs/TESTING_METHODOLOGY.md` (v0.8.2 #119)
  anti-horizontal slicing counter-example.
- **5 scope caveat categories**:
  - New behavior → full RED → GREEN → REFACTOR
  - Bug fix → regression test FIRST → fix
  - Pure refactor → no new test written; existing tests stay GREEN
  - Pure doc / spec / config → skip TDD
  - Migration / DDL / API contract → contract test in Step 5
- **File-type fast check table** aligned with posttooluse hook detection

#### `skills/aqg-code-construction/SKILL.md` — NEW Step 5: audit-before-commit gate (#121)

Adds Step 5 (renumbered old 5 → 6, old 6 → 7); Workflow total 6 → 7 sections.

Required for executable-code changes; skip pure doc/spec/config. Includes:
- `mcp__audit__gpt_audit` invocation pattern + audit depth selection
  (fast / single / two / three per `~/.claude/rules/common/audit-self-routing.md`)
- Finding integration via `aqg-audit-adjudication` (accept / reject /
  needs-user-decision)
- `audit_id` reference in commit body locks the audit chain
- Reasoning embedded: top-tier LLM + cross-vendor audit catches design /
  idiom / edge-case gaps; cited real example T1-B audit `5f22628d` F1
  ("prerequisite satisfies all hard deps" — caught by GPT-5.5, missed by
  primary author)

#### `agent-packs/claude-code/skills/aqg-code-construction/SKILL.md` — condensed mirror (#121)

Same enhancements in distribution-condensed form; Workflow steps 1-6 with
v0.8.3 vertical TDD + audit gate inline.

#### `agent-packs/claude-code/hooks/posttooluse_code_construction_reminder.sh` — 2 new reminder blocks (#122)

v0.8.3 adds stderr warn-only reminders (still never blocks; exit 0 invariant):

1. **Vertical-TDD reminder**: anti-horizontal slicing hint + scope caveat
   inline
2. **Audit-before-commit gate reminder**: cite `mcp__audit__gpt_audit` +
   `audit-self-routing.md` + `aqg-audit-adjudication`

File-type filter unchanged: `.md / .yaml / .json / .txt` stay silent; test
files stay silent. Per scope caveat: doc/spec/config commits trigger 0
v0.8.3 reminder (per `test_v083_doc_file_skips_all_reminders`).

#### `tests/test_v080_hooks.py` — 3 new tests (#122)

- `test_v083_vertical_tdd_reminder_appears` — anti-horizontal language
  present on code-file edits
- `test_v083_audit_gate_reminder_appears` — audit gate language present
- `test_v083_doc_file_skips_all_reminders` — per scope caveat

8/8 tests pass in PostToolUseConstructionReminderTest class (was 5/5).

### Changed

#### `tests/behavior/test_drift.py` + `tests/behavior/fixtures/triggers.yaml` (#121)

Rebase `aqg-code-construction` trigger_section_sha256 fixture
`1236c6f1 → d6187292` to match Step 2 + Step 5 SKILL.md body change.
description_sha256 unchanged `9d16b60c` (per SKILL_AUTHORING_GUIDE no
jargon in description). 27 drift tests pass.

### Migration

No manual action needed; AQG automatically follows symlink install (per v0.7.0+ contract).

```bash
bash agent-packs/claude-code/install.sh --scope user --mode link --force
# Restart Claude session
ls ~/.claude/skills/aqg-* | wc -l   # still = 10 (skills count unchanged)
```

Skills count unchanged at 10; `aqg-code-construction` body enhanced
in-place (Workflow steps 1-7; inner 6-step construction unchanged).

### Reasoning chain (Owner 2026-05-19)

Documents the reasoning chain behind the v0.8.3 design decision (per audit-adjudication transparency
requirement + to prevent future contributors from mistakenly thinking "TDD is on hold"):

**Option A adopted** (top-tier LLM + cross-vendor audit gate; cross-LLM
test-writer rejected):

Supporting arguments:
- Top-tier LLMs (Opus 4.7 / GPT-5.5) already catch most first-pass typos /
  edge cases (SWE-bench ~60-70% solve rate at session date)
- Cross-vendor audit panel already catches design/idiom/spec issues the top-tier LLM misses
  (case-of-1: today's T1-B/T1-G audits caught 11 issues including
  Python json.dumps default not visiting str — would have been hard
  for primary author to spot)
- Cross-LLM test-writer is a **redundant** defense layer:
  - Audit panel is already cross-vendor (catches what test-writer would catch)
  - Test-writer adds a hard dependency on always-on the audit hub
  - Per-slice latency 10-15s + the audit hub down → dev flow gets stuck
  - Cost-benefit ratio's marginal gain isn't enough to justify it

Rejection arguments:
- Test-writer guards against confirmation bias — but audit panel already covers that
- Vertical-slice discipline doesn't require cross-LLM — single-LLM impl self-testing +
  audit catching design is enough

**Net decision**: enforce vertical TDD discipline (impl LLM self-tests) +
mandatory audit-before-commit gate. Document caveats for refactor /
bug-fix / doc.

**Future escape hatch**: cross-LLM test-writer remains a future option (Phase 2
/ Owner explicit trigger / high-stakes paths), documented but not default.

### Audit chain

| Item | Audit | Reasoning |
|------|-------|-----------|
| PR #121 SKILL.md changes | **skipped** (per spec) | Pure doc + mechanical fixture rebase per Step 5 scope caveat |
| PR #122 hook + tests | **skipped** (per spec) | Warn-only stderr addition; full test coverage; exit 0 invariant preserved |
| v0.8.3 release (this PR) | **skipped** (per spec) | VERSION + CHANGELOG bookmark only |

3 PRs zero audit calls. Self-dogfooding Step 5 scope caveat's actual application — pure
doc/spec/config commits SHOULD skip audit gate.

## [0.8.2] - 2026-05-18

AQG cross-product absorption batch — absorbing gstack (98K ⭐) + mattpocock/skills
(90K ⭐) engineering methodology, 4 PR bundle (triple-review adjudicated, 10/10 items shipped).

Bundle contains 4 PRs:
- #116 — `aqg-code-construction` step 2 TDD methodology expansion (Mode A)
- #117 — `aqg-systematic-debugging` Phase 0 build-the-feedback-loop-FIRST (AQG-T1-A)
- #118 — `aqg-skill-validator` Hard/Soft dep + description style lint (AQG-T1-B + T1-C)
- #119 — `docs/TESTING_METHODOLOGY.md` anti-horizontal slicing (AQG-T2-B)

Cross-product related specs live in `the cloud backend/docs/specs/` (not in this repo, but referenced):
T1-F prompt-injection L1-L6 / T1-G egress sanitization / T1-D CONTEXT-FORMAT /
T1-E ADR-FORMAT / T1-H multi-actor security / T2-A architecture-LANGUAGE.

### Added

#### `skills/aqg-systematic-debugging/SKILL.md` — Phase 0 build-feedback-loop-FIRST (#117, AQG-T1-A)

Adds Phase 0 prerequisite section (before the 8-step Workflow), from mattpocock's
`engineering/diagnose/SKILL.md` framework + keeps AQG's existing 8-tier latency table:
- 0a: 10 ways to construct a loop (failing test / curl / CLI / playwright /
  replay / harness / property / bisection / differential / structured-HITL)
- 0b: existing fidelity-cost 8-tier table (renamed from old "Phase 1")
- 0c: iterate-on-the-loop (faster / sharper / more deterministic)
- 0d: non-deterministic bugs (raise reproduction rate)
- 0e: existing 5 rules (bisect / print-before-debugger / minimal repro /
  one-variable / record what tried)
- 0f: when you cannot build a loop — escalate to env access / artifact /
  prod instrumentation permission

Gate target: Workflow steps 5-6 (hypothesis / fix); steps 1-2 record + run
the loop itself. Workflow step 2 added explicit "first iteration of the
Phase 0 loop" reference.

Single audit `339c2a86` (gpt-5.5); 5 findings (2 major + 3 minor) all
integrated.

#### `agent-packs/claude-code/skills/aqg-systematic-debugging/SKILL.md` — Phase 0 condensed

Same content in distribution-condensed form. Adds restored step 8 (closeout
fallback) + validate command in single shell block (audit M2/m1 fixes).

#### `docs/TESTING_METHODOLOGY.md` — brand-new platform-level testing methodology (#119, AQG-T2-B)

Cross-product methodology doc (spanning AQG / EAF / the audit hub):
- 80% coverage minimum (mirrors Owner's personal testing.md)
- TDD MANDATORY workflow
- **Anti-pattern: horizontal slicing produces crap tests** — verbatim from
  mattpocock tdd SKILL.md
- Vertical slices via tracer bullets (one test → one impl → repeat)
- Cross-product application table
- Legitimate deviation cases (API design / test infrastructure setup)
- Links to sister specs T1-A / T1-B/C / T1-E

Source: mattpocock commit `67bce91c80cd1020a4f068ced32d0281656842ad` (MIT).

#### `scripts/validate_agent_pack.py` — T1-B Hard/Soft dep + T1-C description style lint (#118)

**T1-B Hard/Soft dependency validation** (per mattpocock ADR 0001):
- New optional frontmatter `dependencies: name1:hard, name2:soft, ...`
- `parse_dependencies()` parses entries
- `validate_dependencies_field()` — per-name + same-paragraph rule:
  - Hard dep: dep name + setup-pointer phrase MUST appear in same paragraph
    (avoids one generic word satisfying all hard deps)
  - Soft dep: case-insensitive boundary-aware match (`re.escape` + `\b` +
    `IGNORECASE`) on body
- 6 setup-pointer regex patterns (should have been provided / if not
  installed run X / required setup / setup pointer / setup
  {command,script,cli,tool})
- Backward compat: missing `dependencies:` field = opt-in skip
- `extract_body()` helper separates frontmatter from body

**T1-C Description style lint**:
- > 1024 chars → FAIL (mattpocock hard cap)
- > 800 chars → WARN (soft cap)
- Missing "Use when" / "trigger" / "when [action]" → WARN
- First-person agent self-reference ("I help" / "we are") → WARN
- "you" allowed (legitimate trigger sentence "Use when you ...")

`validate_skill()` now returns `list[str]` of WARN messages (was `None`);
caller `validate_claude_code_pack` prints WARN to stderr.

Real-pack run: 10/10 skills pass; 6 WARN about missing "Use when" trigger
(real gap surfaced for future per-skill cleanup).

Single audit `5f22628d` (gpt-5.5); 4 findings — F1+F2 in-scope fixed (per-
name + same-paragraph + re.escape); F3+F4 deferred (pre-existing v0.8.0
`validate_hook` bugs, separate PR).

#### `tests/test_validate_agent_pack.py` — 15 new tests (T1-B + T1-C)

`T1BHardSoftDependencyTest` (8 tests) + `T1CDescriptionStyleTest` (6 tests
+ real-pack smoke). 27/27 in test_validate_agent_pack.py pass; full suite
1207 → 1234 tests pass (+27 new, no regression).

### Changed

#### `skills/aqg-code-construction/SKILL.md` (#116)

Step 2 TDD methodology expansion — Mode A shipped (Mode B Phase v2 planned).
See PR #116 for details.

#### `tests/behavior/test_drift.py` + `tests/behavior/fixtures/triggers.yaml` (#117)

Rebaseline `aqg-systematic-debugging` trigger hash `256e7127 → f35ca218`
to match Phase 0 enhanced SKILL.md. 5 fixture occurrences updated. AQG's
own drift discipline caught this in CI — dogfood Phase 0 feedback loop
self-applied during the fix.

### Migration

No manual action needed; AQG automatically follows symlink install (per v0.7.0+ contract).

```bash
bash agent-packs/claude-code/install.sh --scope user --mode link --force
# Restart Claude session
ls ~/.claude/skills/aqg-* | wc -l   # still = 10 (skill count unchanged)
```

If the project uses the `dependencies:` field (per T1-B opt-in), run the validator once to verify
the new rules are OK:

```bash
python3 scripts/validate_agent_pack.py --agent claude-code \
    --pack agent-packs/claude-code
```

### Audit Chain

Cumulative audit across the whole v0.8.2 absorption batch:

| Item | Audit | Mode | Findings | Status |
|------|-------|------|----------|--------|
| AQG-T1-F (in the cloud backend) | `95ed3ee7` | two (gpt-5.5 + gemini) | 11 | all integrated |
| AQG-T1-G (in the cloud backend) | `cb0506c5` | single (gpt-5.5) | 7 | all integrated |
| AQG-T1-A (this repo) | `339c2a86` | single | 5 | all integrated |
| AQG-T1-B+T1-C (this repo) | `5f22628d` | single | 4 | F1+F2 fixed; F3+F4 deferred (pre-existing v0.8.0) |
| AQG-T1-D+T1-E (in the cloud backend) | `233ef33c` | single | 8 | all integrated |
| AQG-T1-H + T2-A | n/a | per plan | — | doc-only |
| AQG-T2-B (this repo) | self-review | XS | 0 | content 95% verbatim source |

### Deferred to follow-up PRs (pre-existing v0.8.0 bugs)

- F3 from T1-B audit: `validate_hook()` `blocking_commands` is command-string
  scoped, not entry-scoped (existing v0.8.0 bug; not introduced by T1-B)
- F4 from T1-B audit: `BLOCKING_COMMAND_RE` single-digit only ("exit 10"
  bypass); needs `[1-9]\d*` (existing v0.8.0 regex bug)

Both have audit evidence in `5f22628d`; tracked for separate PR.

## [0.8.1] - 2026-05-15

Fixes #114 — `agent-packs/claude-code/install.sh` skill array was missing `aqg-phase-transition` (Sprint 11, PR #103) and `aqg-multi-review` (Sprint 11.5, PR #104). README / example / 5 sibling repo CLAUDE.md (Phase 2) all said "10 skill" but the Claude installer only linked 8; Codex source `scripts/install.sh` had the same gap. v0.8.0 dogfood bug — took issue #114 Path B (normalize, build a real wrapper).

Ran single audit (gpt-5.5, audit_id `1877cef7`) before commit; 4 findings (2 major + 1 minor + 1 nit) all ACCEPT + landed. Major #2 caught the killer issue: registering Codex skills without sidecar = knowingly broken vs v0.8.0 validator → added 2 source `skill.template.json` sidecars + fixed the source script docstring exit-code contract + source SKILL.md `## Boundary` → `## Boundaries`. Full 6 cross-cutting anchor fixes.

### Added

#### `agent-packs/claude-code/skills/aqg-phase-transition/SKILL.md` (Claude wrapper)

Adapted from source `skills/aqg-phase-transition/SKILL.md` with Claude Code-specific intro line. Preserves decision matrix + 5 user-signal overrides + safety floor + 5-min dedup + ADR §5 boundary (skill emits signal, doesn't call audit-mcp). 81 lines.

#### `agent-packs/claude-code/skills/aqg-multi-review/SKILL.md` (Claude wrapper)

Adapted from source `skills/aqg-multi-review/SKILL.md` with Claude Code-specific intro line. Preserves 5-dim x cross-LLM panel architecture + Anthropic /ultrareview comparison table + cloud/local mode parity + ADR §5 boundary. 78 lines.

#### `skills/aqg-phase-transition/skill.template.json` (audit fix #2 — sidecar)

Required by `aqg-skill-validator` (v0.7.0+ contract). 92-line schema-1 manifest with `boundary_class: writes-evidence` (writes `.aqg/phase-state-<task>.json`), `output_shape: json`, `audit_mode_required: single`, 5 fixture cases mirroring `triggers.yaml`, drift_hash baseline. Validates clean against `_skill_template_schema.py`.

#### `skills/aqg-multi-review/skill.template.json` (audit fix #2 — sidecar)

Same schema-1 manifest. `boundary_class: read-only` (no writes), `output_shape: json`, `audit_mode_required: two`, 5 fixture cases. Validates clean.

#### `skills/aqg-phase-transition/scripts/self_test.py` (sidecar `self_test_entrypoint`)

Smoke test: emits a PLAN_DONE phase + parses returned `recommended_audit_mode` ∈ {fast, single, two, three, skip}. Uses tmp `--repo` so no real `.aqg/` polluted.

#### `skills/aqg-multi-review/scripts/self_test.py` (sidecar `self_test_entrypoint`)

Smoke test: runs `new` mode + verifies all 5 expected dimensions (logic / edge_cases / security / performance / concurrency) appear in skeleton.

### Changed

#### `agent-packs/claude-code/install.sh` — skills array 8 → 10

Add `aqg-phase-transition` + `aqg-multi-review` to Claude pack installer.

#### `scripts/install.sh` — skills array 8 → 10 (Codex pack source)

Same fix on Codex pack source — both installers should agree (per `aqg_doctor.py` "aligned with install.sh / Codex install.sh" comment).

#### `scripts/aqg_doctor.py` — `CLAUDE_SKILL_NAMES` + `CODEX_SKILL_NAMES` tuples 8 → 10

Both health-check tuples updated to include the 2 new skills. `aqg_doctor` now reports PASS for all 10 install targets when properly installed.

#### `tests/behavior/test_drift.py` — `TestRealAqgSkills` parametrize 8 → 10

Add 2 new (skill, description_hash, trigger_hash) tuples:
- `aqg-phase-transition`: desc=`64e134ef`, trig=`fa19c0fd`
- `aqg-multi-review`: desc=`08f233f1`, trig=`3ab6fb5f`

Drift detection now covers all 10 wrappers.

#### `tests/behavior/fixtures/triggers.yaml` — +10 fixture cases (5 per new skill) + header sync

Following existing pattern (4 description + 1 explicit per skill). Total fixtures: 40 → 50, covering 10 skills × 5 cases each. Header comment also updated from stale "5 case/skill × 5 skill = 25 case" to current "5 case/skill × 10 skill = 50 case" + per-skill breakdown (audit fix #3).

#### `skills/aqg-phase-transition/scripts/aqg_phase_emit.py` — exit-code docstring contract

Add reserved 1 + 3 entries (validator regex needs all of 0/1/2/3/70 with `<code>: <text>` format). Reformatted from list bullets to canonical 2-space indent.

#### `skills/aqg-multi-review/scripts/aqg_multi_review.py` — exit-code docstring contract

Same: add reserved 3 entry + reformat list bullets to canonical format.

#### `skills/aqg-multi-review/SKILL.md` — `## Boundary` → `## Boundaries` (H2)

Validator requires plural form (one of `## Boundaries / ## Boundary Rules / ## Stop Boundaries / ## Stop Rules`). Sidecar drift_hash updated to new SHA-256 (`ddb546b9...`).

### Migration

No manual action needed; v0.8.1 install will auto-link 10 skills:

```bash
bash agent-packs/claude-code/install.sh --scope user --mode link --force
# Restart Claude session; verify ~/.claude/skills/aqg-* has 10 entries
ls ~/.claude/skills/aqg-* | wc -l   # should = 10
```

If on v0.8.0 with the workaround `ln -s` symlinks for `aqg-phase-transition` / `aqg-multi-review`, `--force` will overwrite cleanly.

## [0.8.0] - 2026-05-13

Hook hardening — 5 Claude Code hook layers upgrade the CLAUDE.md rule from "description reminder" to a "framework-enforceable point". First cut of a landed solution for the self-criticism historical conclusion (CLAUDE session often forgets to invoke skills / skips closeout / doesn't follow the 6 steps): the AQG repo itself goes live dogfooding it. Sibling repo CLAUDE.md sync is Phase 2 (per Owner's decision).

### Added

#### `agent-packs/claude-code/hooks/` — 5 new hook scripts

| Hook | Trigger | Enforcement |
|---|---|---|
| `pretooluse_bash_skill_validator.sh` | PreToolUse(Bash matcher) → `git commit ...` with staged files including `skills/aqg-X/SKILL.md` or `scripts/install.sh` | BLOCK (exit 1; `AQG_AGENT=human-opt-in` can downgrade to warn) |
| `posttooluse_skill_edit_reminder.sh` | PostToolUse(Edit\|Write\|MultiEdit) → `file_path` under `skills/aqg-*/` | WARN (invoke `aqg-skill-validator` before commit) |
| `posttooluse_code_construction_reminder.sh` | PostToolUse(Edit\|Write\|MultiEdit) → non-test file with suffix `.py / .go / .ts / .tsx / .js / .jsx / .rs / .java / .kt / .swift / .rb / .sh / .sql / .cpp / .c / .h` | WARN (lists the `aqg-code-construction` 6 steps) — Phase 1 warn-only, Phase 2 enforce |
| `precompact_closeout_reminder.sh` | PreCompact + Stop | WARN (lists the `aqg-evidence-closeout` 6 questions) |
| `sessionstart_preflight.sh` | SessionStart | INFO + auto-run (automatically runs `aqg-startup-preflight` under a git repo cwd; summary capped at 50 lines) |

Design principles: all POSIX bash; stdin JSON parsed via `python3 -c "json.loads(sys.stdin.read())"`; when `AQG_ROOT` isn't exported everything silently skips (Claude Code unaffected); the only blocking one = PreToolUse skill-validator gate (same `AQG_AGENT` escape pattern as the existing `pre_commit_construction.sh`).

#### `scripts/install_v080_hooks.py` — installer / verifier / uninstaller

`--apply` / `--verify` / `--uninstall`. Default target = `~/.claude/settings.json` (user-level, across all projects). `--target /path/to/repo/.claude/settings.json` installs at project level.

Behavior:
- **Does not clobber** pre-existing hooks (semgrep / ECC / custom) — uses dedup-by-script-filename, appends to the existing matcher block instead of replacing it
- **idempotent** — repeated `--apply` doesn't re-add; reports `skip ... already wired` instead of failing
- **backup** writes `settings.json.aqg-v080.bak` before modifying (adds timestamp suffix on re-run); uninstall also backs up
- **uninstall only removes v0.8.0 entries** — matches exactly by script filename, other hooks 100% preserved

#### `agent-packs/claude-code/hooks/settings.v0.8.0.example.json` — documented example

per-command `"_blocking": true` marks the v0.8.0 contract change (PreToolUse blocking gate by design). The `_layers` + `_blocking_contract` sections explain each hook's enforcement level + contract boundary.

### Changed

#### `scripts/validate_agent_pack.py` — per-command `_blocking: true` opt-out (audit hardened)

`validate_hook()` now honors the `"_blocking": true` boolean attribute on each hook entry (per-command granularity, not file-level). Only the marked command skips the warn-only blocking checks (`exit N` / `decision=block` / `continue=false` / forbidden tokens); other unmarked commands in the same file still enforce warn-only. All commands uniformly enforce JSON valid + CLAUDE_PROJECT_DIR usage. A `_blocking` value that isn't the `true` literal raises ValidationError (prevents stringly-typed drift).

The old file-level `_warn_only: false` key now raises ValidationError to force migration (prevents silent contract drift; audit gpt-5.5 #3 fix).

### Audit (gpt-5.5 single, $0)

External review by gpt-5.5 (`audit_id: 5cf35e26`, 5m 48s) returned 6 findings (5 major + 1 minor), all ACCEPT + landed:

| Finding | Severity | Fix |
|---|---|---|
| 1: PreToolUse regex too narrow (`\b` not POSIX portable; misses env-prefix + `git -C`) | major | Expanded to `(^|[[:space:]/;&|])([A-Za-z_]+=...)*git(\s+-C\s+\S+)?\s+commit([[:space:]]|$)`; replaced `\b` with `([[:space:]]|$)` |
| 2: Missing `git commit -a` / `git add ...; git commit` (commit-time staging) | major | When detecting `-a` / `--all` / `git add` forms, also scans `git diff --name-only HEAD` working tree; documents that `pre_commit_construction.sh` is the backstop authoritative gate (git pre-commit level sees final index) |
| 3: `_warn_only: false` file-level too loose (4 warn + 1 block mixed in one file opt-out → future accidental blocking goes undetected) | major | Changed to per-command `_blocking: true` attribute; per-command granularity; old `_warn_only` key forcibly rejected to prevent drift |
| 4: installer dedup only looks at filename, missing (matcher, script) granularity (stale matcher not fixed) | major | dedup uses `(matcher, script)` tuple; `--verify` reports stale entries; `--apply` adds the canonical entry alongside, doesn't auto-delete stale ones (avoids breaking user customization) |
| 5: SessionStart preflight has no timeout (slow remote / credential prompt stalls session) | major | Wrapped in Python subprocess.run(timeout=20s); friendly timeout message; output capped inside Python to avoid a shell `head` race |
| 6: re-apply still backs up + writes even when everything's already installed | minor | `_merge_hooks` returns `added_count`; backup + write only when added>0; fully idempotent no-op |

Every finding has a matching hardening test (`tests/test_v080_hooks.py` 36 cases, `tests/test_validate_agent_pack.py` 13 cases). Test coverage:
- regex expansion: `git -C dir commit` / `FOO=bar git commit` / `FOO=1 BAR=2 git commit` / staged AQG skill actually blocks / human-opt-in downgrade / `-a` triggers working-tree scan
- per-command opt-out: `_blocking: true` only lets that command through / other commands in the same file still enforce / `_blocking` non-strict-True rejected / old `_warn_only` rejected
- installer: 2nd apply produces no new backup / no mtime change / stale matcher detect + repair

### Tests

- `tests/test_v080_hooks.py` (new, 36 cases) — 5 hook scripts' stdin parse / path match / exit code + installer apply/verify/uninstall round-trip + idempotency + merges with pre-existing hooks without clobbering + AQG_ROOT unset silent skip + audit fix coverage (regex expansion 6 cases + `-a` working-tree scan + 2nd apply no-op + stale matcher detect/repair)
- `tests/test_validate_agent_pack.py` (+7 cases) — per-command `_blocking: true` opt-out / other commands in same file still enforce / `_blocking` strict-True / old `_warn_only` reject / marked command still enforces CLAUDE_PROJECT_DIR / default unmarked still rejects blocking exit

Total 1178 baseline + 10 new audit-fix = 1188 cases; all pass (except 3 pre-existing infra failures — related to local `~/.codex/skills/` install state, out of v0.8.0 scope).

### Migration

New install: `python3 "$AQG_ROOT/scripts/install_v080_hooks.py" --apply` then restart Claude session.

Upgrade (v0.7.0 → v0.8.0): same `--apply`. installer is idempotent + doesn't clobber pre-existing hooks, safe to run directly.

### Phase 2 (out of scope this release)

- Sibling repos' project-level CLAUDE.md "## AQG Engineering Discipline" section append — decided per Owner after Phase 1 validation
- Hook 3 (code construction reminder) upgrade from warn-only to enforce — needs Phase 1 dogfood data collected first (false-positive rate / actual behavior-change rate)
- PreCompact closeout hook upgrade from pure reminder to transcript scan (detects missing ledger keyword and strongly prompts)

## [0.7.0] - 2026-05-13

Documentation completion + 2 already-shipped skills surface in README + dogfood self-discipline reminder + open backlog section. No new skill code ships in this release.

### Changed (doc + version correction)

#### Skill count drift corrected: README 8 → **10**

Two skills landed in Sprints 11+11.5 (2026-05-09) via PRs #103-#105 but README/examples still claimed "8 skills" / "5 triggers". This release surfaces them:

- **`aqg-multi-review`** (Sprint 11.5, PR #104) — 5-dimension (logic / edge_cases / security / performance / concurrency) × cross-LLM panel review router. Counterpart to Anthropic `/ultrareview` but uses cross-family LLM independence (gpt-5.5 + gemini + o3 are different training distributions, not double-Claude anchoring). Signal-only — emits `needs_llm_judgement` for caller to dispatch `gpt_audit`. ADR: `docs/decisions/2026-05-09-multi-dimension-review-skill-a1.md`.
- **`aqg-phase-transition`** (Sprint 11+, PR #103) — Audit decision flow **fallback layer**. Reactive `audit-self-routing` covers user-question + complexity-tree triggers but misses phase boundary moments (`PLAN_DONE` / `IMPL_DONE` / `TESTS_WRITTEN`). Phase × Stakes matrix → recommends `fast` / `single` / `two` / `three`; higher tier wins on collision (fail-safer); high-stakes blocked from downgrading below `two`; same artifact + 5-min window dedup. Signal-only emit. ADR: `docs/decisions/2026-05-09-phase-transition-audit-trigger-a1.md`.

#### README §"Skills (currently 8)" → 10

Skill list + trigger table ("5 triggers" → 10) + "AQG framework's 8 skills" callout all updated. Cross-references to ADRs added.

#### README new sections

- **"Claude / Codex sessions dogfooding AQG themselves (self-discipline reminder)"** — explicit reminder that AQG project owners are themselves AQG users. Records common "build but don't consume" pitfalls (direct `gpt_audit` skip of `aqg-security-review`, ad-hoc 6-question closeout skip of `aqg-evidence-closeout` schema, etc) + applied remedies. Reflects 2026-05-12 self-criticism conversation.
- **"Open backlog / suggested improvements (v0.7.x → v1.0)"** — living section with prioritized backlog: CLAUDE session dogfood metric / `aqg-phase-transition` real-world invoke validation / `aqg-multi-review` cross-LLM panel measurement / Cloudflare WAF guide / Homebrew tap / EAF hardening mirror verification / multi-worker Redis backend / etc.

#### `examples/aqg-claude-rules.example.md` + `aqg-codex-agents.example.md` (parallel update)

- "5 skills" → "10 skills"
- 5 new trigger sections added (in phase order): `aqg-phase-transition`, `aqg-security-review`, `aqg-multi-review`, `aqg-automation-audit`, `aqg-skill-validator`
- Existing 5 sections (preflight / code-construction / systematic-debugging / audit-adjudication / evidence-closeout) preserved

### Not changed (intentionally)

No new skill code shipped. No script changes. No installation flow change. The 10 skills have been installed by `scripts/install.sh` since Sprint 11.5; this release **truthfully documents that count**.

## [0.6.0] - 2026-05-05

Phase 2 collection (#84–#91 plus a release-infrastructure PR). Three thematic streams: 2 new skills, Q-Open framework expansion (Q-Open-9 retrieval contract), release infrastructure + README onboarding clarity.

### Added

#### New skills (skill pack: 6 → 8)

- **#84** `feat(skills)` — `aqg-security-review`: in-session OWASP Top 10 + CWE Top 25 + 8 secure-by-default library checklist (HTTP headers / XSS / CSRF / crypto / input validation / SSRF / deserialization / templates). Complementary to semgrep SAST + audit-mcp external review; three-layer security model. Audit `29d587e5` double-review 14/14 accepted (Bleach/SerialKiller currency / YAML syntax / SKILL Codex-neutral resolver / cost cap global Phase 1 / etc.).
- **#85** `feat(skills)` — `aqg-automation-audit`: inventory automation stack (hooks / MCP / plugins / skills / env) + overlap-check vs AQG workflow + emit structured verdict (`disable` / `migrate` / `keep-as-tool` / `fix`). Audit `48f13046` single-review 6/6 accepted (SessionStart duplicate detection / `--out` read-only boundary / atomic JSON write / env state allowlist / etc.).

#### Q-Open framework expansion (Q-Open-9 Retrieval Evidence Contract)

- **#88** `docs(q-open)` — Pivot 4: Q-Open-9 Retrieval Mechanism sketch a1; per Owner 2026-05-03 archive guidance, doc title uses **Retrieval Evidence Contract** to avoid RAG/vector/LangChain semantic baggage; AQG owns schema only, engine implementation distributed to Q-Open-8 / EAF / business agents (audit `046c42d6` 6/6 accepted).
- **#90** `feat(decisions)` — Pivot 5: Q-Open-9 Retrieval Evidence Contract ADR a1 promote. 6 decisions D1-D6 (rename / schema-only scope / 7-source Phase 1 allowlist / step limit 10 + char budget 80k / Owner-only 5-item detector reuse Q-Open-7 D6 at TWO enforcement points / dual embedding promote trigger). Audit `382d6f01` triple-review 13/13 accepted (Layer 1/2 split + redact-before-truncate + step record schema gaps + cross-repo detector pin + promote-signal ledger + 7 more).

#### Release infrastructure

- **(this PR)** `feat(ci)` — `.github/workflows/release.yml`: triggered on `v*` tag push; invokes `transfer-test-pack.yml` via `workflow_call` with `is_release_callee=true` (callee-gate semantic per A3 PR #75 + audit b2211ad2 #2 design). On gate pass, verifies tag matches VERSION + extracts matching CHANGELOG section + creates GitHub Release. Replaces manual `git tag + push` flow used for v0.5.0. v0.6.0 is the first tag to use this workflow.

### Documentation

- **#91** `docs(readme)` — Expand README to reflect the 8-skill list + onboarding clarity.

### Audit cumulative across 0.6.0

49 findings 49/49 accepted across 6 audit panels (audit a97df300 #6 fix — reconciled count):

- `382d6f01` Q-Open-9 ADR a1 promote, mode=three (gpt-5.5 + gemini + o3): **13 findings**
- `29d587e5` aqg-security-review skill, mode=two (gpt-5.5 + gemini): **14 findings**
- `48f13046` aqg-automation-audit skill, mode=single (gpt-5.5): **6 findings**
- `04706056` trim_noisy_plugin_hooks.sh, mode=single (gpt-5.5): **4 findings**
- `046c42d6` Q-Open-9 sketch a1, mode=single (gpt-5.5): **6 findings**
- `a97df300` release.yml + VERSION + CHANGELOG (this PR), mode=single (gpt-5.5): **6 findings**

Total: 13 + 14 + 6 + 4 + 6 + 6 = 49 findings, all accepted.

Cost: $0 subscription (codex-cli + gemini-cli) + minimal o3 API (~tens of seconds o3 portion of single mode=three audit).

## [0.5.0] - 2026-05-05

Wave 3 cap. 19 PRs (#58 was the 0.4.0 release; #59-#76 are in this release). Three thematic streams plus a sibling project naming + ops hardening.

### Added

#### Skill template tooling — machine-readable AI template (B series)

- **#59** `docs(skill-authoring)` — `SKILL_AUTHORING_GUIDE` §10 machine-readable AI template sketch (a1 + a2 + a3; audit `0d5b23e4` 6/6 + `8137caed` 5/5 accepted).
- **#64** `feat(skill-template)` — **B-1** validator + sidecar schema. Per-skill `skill.template.json` defines schema/CLI contract/evidence ledger; validator gates skill quality at write-time (audit `81647aab` 5/5 accepted).
- **#65** `feat(skill-template)` — **B-2** generator CLI emits skeleton + `GENERATED.md` from sidecar (audit `a9554660` 5/5 accepted).
- **#66** `feat(skill-template)` — **B-3** ship `aqg-skill-validator` skill (6th shipped skill, eats own dogfood).
- **#68** `feat(skill-template)` — **B-4** sidecar backfill for the 5 originally-shipped skills (audit `469e66f5` 9/12 accepted; 1 audit finding spawned as #69 followup).

#### Handoff manifest v2

- **#60** `docs(decisions)` — handoff manifest v2 ADR a1 + a2 (Wave 3 §7 #8: sub-agent review fields; dual audit `65371a6f` 10/10 accepted).
- **#63** `feat(handoff-manifest)` — v2 schema upgrade implementation (sub-agent review fields phase 2; audit `4afe34c9` 2/2 accepted).

#### Transfer Test Pack v1 (Q-Open-1)

- **#62** `docs(transfer-test-pack)` — Q-Open-1 v1 sketch a1 + a2 (audit `e3f602b5` 7/7 accepted).
- **#70** `feat(transfer)` — **C** Transfer Test Pack v1 implementation: `tests/transfer/` runner + 6 task modules + fixtures + GHA workflow (audit `297dccac` 6/9 accepted).
- **#73** `feat(closeout)` — **A1** closeout 7th-line: surface Transfer Test Pack threshold state in evidence ledger; bold-fail when threshold not met (audit `d4598abf` 3/3 accepted).
- **#74** `feat(transfer)` — **A2** `aqg_metrics` + `aqg_incident_index` integration: `--record-metric` writes a metric ledger event with explicit `event_id`; `--raise-incident` files an incident on threshold-not-met (audit `299b566d` 4/4 accepted).
- **#75** `feat(transfer-ci)` — **A3** structural fix per audit `297dccac` gpt #3: pre-merge `pull_request` gate (the real release block) + `workflow_call` reusable for future release/publish workflow (audit `b2211ad2` 2/2 accepted; first dogfood run self-caught a missing-Claude-pack-install bug → fixed in same PR).
- **#76** `feat(transfer)` — **B1** schema v2: explicit `skipped: bool` field decouples "audit ran" from `result` enum, resolving the gemini #4 deferred finding from #70 audit (audit `73619b3e` 3/3 accepted).

#### Wave 3 Layer 3 incident index

- **#61** `feat(incident-index)` — Wave 3 Layer 3 v0: markdown record + `INDEX.md` generator under `docs/incidents/` (audit `97254298` 6/6 accepted).

#### EAF (Engineering Agent Foundry) — sibling project naming + scope

- **#72** `docs(eaf)` — EAF naming ADR + bootstrap sketch. Locks the working title "Engineering Sub-Agent Runtime" → **EAF**; defines AQG ↔ EAF boundary (AQG owns schema/contracts/skills/audit gates; EAF owns invoker/orchestrator/sandbox/router); 7-question Q-EAF agenda for Wave 4 implementation phase (audit `e2ac7312` 6/6 accepted).

### Fixed

- **#69** `fix(audit-adjudication)` — align `parse_table()` to the documented `cli_contract`: reject extra columns + row mismatch (was previously silently lenient). Spawned from #68 audit gpt #6 finding (audit `15a637f2` 2/2 accepted).
- **#71** `chore` — `agents/openai.yaml` `display_name` for `aqg-skill-validator` + `aqg-code-construction` so Codex skill picker shows "AQG ..." (was falling back to slug-based "Aqg ...").

### Changed

- **#67** `chore` — source skill `display_name` "AI Team" → "AQG" naming alignment with the 0.3.0 namespace unification (audit `95db7561` 2/2 accepted).

### Project conventions reaffirmed

- `AQG` / `EAF` always uppercase in user-facing brand surfaces (display_name, SKILL.md H1, prose, commit subjects)
- `aqg-` / `eaf-` lowercase only in file slugs / module names / shell variables
- All non-trivial changes go via PR; `deeppatternai` is the default GitHub owner; AI may self-merge AQG PRs after CI green per merge-authorization rule

### Audit total (this release)

69 audit findings across all PRs in this release; 65 accepted+fixed inline, 4 rejected with reasoning (1 spawned as a separate PR #69, 3 deferred to followup work). Total audit cost: $0 (subscription-only, codex-cli + gemini-cli).

### Quality gate posture

- **Pre-merge PR gate** (Layer 1): every PR runs Transfer Test Pack via the `pull_request` trigger; failure blocks merge once Owner marks `transfer-test-pack / run-pack` as a required check on `main` in repo branch protection.
- **Pre-publish gate** (Layer 2): `transfer-test-pack.yml` is `workflow_call`-callable; a future `release.yml` can `uses:` it with `is_release_callee: true` to gate any publish step.
- **Nightly visibility**: warn-only cron (UTC 13:00; gated by repo variable `AQG_TRANSFER_NIGHTLY_ENABLED=true`).
- **Tag-push visibility**: post-event signal; the structural block lives in Layers 1+2.

### Owner action items for 0.5.0 → 0.6.0 transition

1. Tag `v0.5.0` from `main` HEAD after this PR merges (manual; release.yml automation deferred).
2. Mark `transfer-test-pack / run-pack` as a required status check on `main` in GitHub branch protection settings (turns A3 PR-gate from informational into a real block).
3. (Optional) Enable repo variable `AQG_TRANSFER_NIGHTLY_ENABLED=true` to start collecting nightly Transfer Test Pack data.

## [0.4.0] - 2026-05-04

Released in #58. SKILL_AUTHORING_GUIDE landing + AQG_ROOT shared lib v0 (PR-3a sketch #53 + PR-3b1 helper #54 + doctor baseline bump #55 + agent-pack examples sync #56 + behavior-nightly comment update #57).

Earlier history: not retroactively documented in this changelog. See git log for pre-0.4.0 commits.
