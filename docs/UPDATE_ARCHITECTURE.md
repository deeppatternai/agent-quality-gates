# AQG Update Architecture

English | [中文](UPDATE_ARCHITECTURE.zh-CN.md)

> **Status**: design. Not implemented. This is the spec the implementation must satisfy.
>
> Companion documents — this one does not repeat their reasoning:
> - [`INSTALL_VERSIONING.md`](INSTALL_VERSIONING.md) — how to install/pin a reviewed ref today.
> - The product is heading toward a thin client with the verdict computed in the cloud — an *end state*
>   where most updates stop touching the client at all. This document covers the layer that survives that
>   migration: whatever ships to the machine still has to be updated on the machine.

## 0. Scope and the load-bearing invariant

**In scope**: what an update must change on an already-installed machine, and how to apply it without
breaking a running install.

**Out of scope, deliberately**: what *triggers* the check. §10 inventories the candidates and gives a
recommendation, but the apply path must not know or care which one fired it.

> **Invariant**: the update logic is a **library with one entry point**, callable from any trigger,
> idempotent, concurrency-safe, and crash-recoverable. A trigger's only job is to call it and get out of
> the way.

Everything below follows from that. If a design choice would make the apply path depend on being called
from a hook, it is wrong.

## 1. What an update must actually propagate

Verified install shape (2026-09-02, this checkout):

- `AQG_ROOT` = `~/.deeppattern/agent-quality-gates`, a **git checkout**.
- Skills are **symlinks** into each host's skills dir. All 20 clients in
  [`scripts/aqg_client_registry.py`](../scripts/aqg_client_registry.py) default to `link` mode.
- Hooks are **host-owned config entries whose command strings point back into `AQG_ROOT`**. The Claude Code
  shape, from `~/.claude/settings.json`:

  ```
  if [ -z "${AQG_ROOT:-}" ]; then exit 0; fi; CLAUDE_PROJECT_DIR="${CLAUDE_PROJECT_DIR:-}" \
    bash "$AQG_ROOT/agent-packs/claude-code/hooks/sessionstart_preflight.sh" "$CLAUDE_PROJECT_DIR"
  ```

  `aqg_root` is deliberately **not** baked into the string
  ([`install_aqg_hooks.py:138`](../scripts/install_aqg_hooks.py)); it is resolved at run time.
- Rules are a managed block inside a host-owned file (`CLAUDE.md`, `AGENTS.md`, `.cursor/rules/aqg.mdc`, …).

From that shape, the payload of an update decomposes into five classes with genuinely different cost:

| # | Change | Bare `git pull` enough? | What else is needed | Touches host config? |
|---|---|---|---|---|
| 1 | Hook **script body** changed | ✅ yes — the command string points into the checkout | nothing | no |
| 2 | Skill **content** changed (link mode) | ✅ yes | nothing | no |
| 3 | Skill **added** | ❌ | create one symlink per host | no |
| 4 | Skill **removed / renamed** | ❌ | prune owned symlinks per host | no |
| 5 | **Hook set membership changed** — new hook file, new lifecycle event, changed args | ❌ | rewrite the host's hook config | **yes** |

> **The central rule this yields**: *only class 5 requires mutating host-owned configuration.* Classes 1–4
> are repo-internal plus symlink bookkeeping. Class 5 is the only stateful merge, the only one with a real
> blast radius, and the only one that can leave a host half-configured.
>
> This is mechanically decidable from a release diff (did any file under a host's `hooks/` directory get
> added, removed, or renamed? did any adapter's event list change?). **The release pipeline must compute
> and publish this classification**, so the client does not have to infer it.

Copy mode (`--copy`) collapses this: classes 1 and 2 stop being free and need a re-copy. Copy mode is a
supported install option, so the apply path must handle both and must read which mode a host is on from
recorded state rather than assuming.

## 2. Three layers, and which two this document specifies

| Layer | Responsibility | Where it lives |
|---|---|---|
| **Trigger** | decide *when* to look | §10 — several, all optional, all interchangeable |
| **Plan** | decide *what* this machine needs | this document, §4–§6 |
| **Apply** | make it so, or leave nothing half-done | this document, §5, §7, §8 |

## 3. Prior art

### 3.1 Superpowers / Claude Code plugins — it sidesteps class 5 entirely

The honest answer to "how does superpowers handle needing to change `settings.json`" is: **it never
changes it.**

- A plugin declares its hooks in `hooks/hooks.json` **inside the plugin**. Claude Code loads plugin hooks
  directly and **does not write them into `~/.claude/settings.json`**. Class 5 becomes "ship a different
  file in the package" — the same cost as class 1.
- Paths use `${CLAUDE_PLUGIN_ROOT}`, a host-provided variable, so a version bump that moves the install
  directory does not invalidate any command string. (`${CLAUDE_PLUGIN_DATA}` is the counterpart that
  *survives* updates — the place for state that must not be wiped.)
- Per-host variation is handled as **separate declarative files** in the same package: superpowers ships
  `hooks/hooks.json` and `hooks/hooks-cursor.json` side by side.
- The version comes from `plugin.json`; the host checks for updates after session start with a random delay
  of up to ten minutes, updates **on disk only**, never disturbs the running session, and prompts
  `/reload-plugins`.

**What we take**: (a) express each host's hook set as **declarative data**, not as installer code;
(b) never bake an absolute root into a command string — AQG already satisfies this via `$AQG_ROOT`;
(c) update the disk, never the live session.

> **Decision (2026-09-02, Owner): the marketplace route is not being pursued.** It covers exactly one host,
> while the update library still has to exist for the other 19 hosts, for migrating the installed base, and
> for state migration — it adds a leg, it removes no work. Costs evaluated and declined: publishing the
> methodology in plaintext; the `AQG_ROOT`-versus-host-managed-plugin-root dilemma; losing the ability to
> express a user's install-time choice (warn-only vs blocking set); the one-time cleanup of AQG entries
> already present in existing users' `settings.json`; and giving up our own signature verification.
> Revisit only if a **public community edition** is ever built — and then the plugin package should be a
> **generated artifact rendered from the same registry**, never a fork.

**Why we cannot simply adopt it**: the host owns the plugin lifecycle *only inside Claude Code*. AQG's
registry carries 20 clients, most with no plugin system at all, and their hook surfaces are mutually
incompatible (§6). Additionally, a plugin's contents are plaintext, which is exactly the exposure
the release posture calls "the leakage source". **Plugin packaging is therefore at most
one delivery channel among several — never the update mechanism.**

### 3.2 Decision Engine `installer/` — the in-house prior art to actually borrow

DE (`~/.deeppattern/decision-engine`) already runs a managed self-update in production shape. It is the
closest thing we have to a reference implementation, and it was built under the same discipline.

| DE module | What it does | Borrow? |
|---|---|---|
| `updater.py` (2135 ln) | **read-only** inspection. Docstring is explicit: "`UpdateInspection` is ephemeral diagnostic data, **never an authorization capability**"; the mutator must re-run every check under its own lock. | **Directly** — the read/authorize split is the single most valuable idea here. |
| `update_transaction.py` (2598 ln) | crash-recoverable apply: acquire lock → reject live sessions → re-inspect → recovery copy + write-ahead journal → reset → smoke → commit or roll back. Journal phases: `prepared`, `reset_started`, `candidate_applied`, `smoke_started`, `rollback_started`, `retry_pending`, `repair_required`. | **Phase model directly**; mechanics adapted (§5). |
| `update_coordination.py` (560 ln) | cross-process admission. "**File existence is never authority**" — every lock is an OS advisory lock on an open handle; live sessions hold leases. | **Directly** — AQG has more concurrent sessions than DE, not fewer. |
| `release_contract.py` + `release-trust.json` | detached RSA-PKCS1v15-SHA256 over a deterministic manifest, verified against a **pinned key set** with `revoked` flags and a release **sequence** (anti-rollback). No network, no git, no filesystem. | **Directly** — this is what §9 requires, already written and tested. |
| `client_hosts/` (`contract.py` + `registry.py` + `hosts/*.py`) | `AgentHostSpec` frozen dataclass + static registry + `validate_host_specs` fail-closed at import. "The dispatcher owns vocabulary and validation. Host adapters own only their format-specific mutation mechanics." | **Pattern directly**; AQG's own registry already exists and should be extended rather than duplicated (§6). |
| `install.py` `_route_skills` / `_prune_stale_routes` | symlink (POSIX) / directory junction (Windows) routing; prune only links pointing **into this component's own** skills dir; `SkillRouteConflict` rather than clobbering a real directory. | **Directly** — §8. AQG's `install.sh` already has an equivalent prune; unify them. |
| `launcher.py` | the bounded update gate at MCP startup; if HEAD changes, a **fresh child interpreter inherits the same stdio connection** so no agent restart is needed and old/new modules never mix. | As a **trigger** (§10.3), and as the "never mix module generations" rule. |

**The one thing DE does not have: hooks.** DE routes skills and writes MCP registrations. It never merges
entries into a host's lifecycle-hook configuration. Class 5 — the hardest part of AQG's problem — has no DE
precedent and must be designed here (§7).

## 4. Update state: what the machine must record

Today an installed machine records **nothing** about its own install: not which hosts were configured, not
which skills were routed, not which hook set shape is present, not link vs copy. Every operation
re-derives it by probing, and a rename is therefore indistinguishable from an unrelated stale link.

Define one owned state file, written atomically, `0600`:

```
$AQG_STATE_ROOT/install-state.json
default:  ~/.deeppattern/aqg-state/install-state.json
Windows:  %USERPROFILE%\.deeppattern\aqg-state\install-state.json
```

> **Decision (2026-09-02, Owner, provisional): `state_root` lives outside the checkout.**
> Hard constraint: **it must never sit inside `AQG_ROOT`.** The atomic swap in §5.1 replaces `AQG_ROOT`
> wholesale with a different tree, so state kept inside it would vanish or revert to an older version at
> exactly the moment a rollback needs it most.
>
> `~/.deeppattern/aqg-state/` (a sibling of `agent-quality-gates` and `decision-engine`) is chosen because
> it matches the existing install layout — DE keeps its device config under
> `~/.deeppattern/decision-engine/` under the same mental model. The name is `aqg-state` rather than `.aqg`
> to avoid confusion with the project-local `.aqg/` **inside** a repo (ledger / adjudication), which is a
> different thing entirely. An `AQG_STATE_ROOT` environment variable overrides it. Marked provisional
> because the Windows profile layout has not been tested (§13).

| Field | Why it is needed |
|---|---|
| `schema` | so a future updater can migrate this file itself |
| `channel` | `stable` (signed tags) \| `edge` (main, manual only) — §9 |
| `installed_version`, `installed_commit`, `release_sequence` | what is actually live; sequence blocks rollback attacks |
| `installed_at`, `applied_by` | provenance: which trigger applied it, when |
| `hosts[]` | **per host**: `client_id`, `scopes`, `skills_dest`, `skills_mode` (link/copy), `routed_skills[]`, `hook_config_path`, `hook_set_hash`, `rules_block_hash`, `last_applied_version`, `status` |
| `pending[]` | plan items deferred or failed, with reason — what `doctor` reports and the next run retries |

Two properties matter more than the field list:

1. **`routed_skills[]` is the machine-side roster.** [`skills.list`](../skills.list) is the *repo-side*
   roster. Diffing the two is what makes add / remove / **rename** decidable — a rename is only ever
   observable as (delete, add) against a recorded previous state. Without this file there is no previous
   state to diff.
2. **Per-host records, because partial success is normal, not exceptional.** One host's config can be
   locked, missing, or hand-edited while five others apply cleanly. DE's `managed_activation` accepts
   exactly this ("a wiring failure can leave some clients unchanged, but never points a changed client at a
   partial launcher"). The state file must be able to express "12 hosts at 0.15.0, 1 host stuck at 0.14.0,
   here is why".

## 5. The apply pipeline

Phases, adapted from DE's journal model. Each phase is journaled before it starts; a crash resumes or rolls
back from the journal.

| # | Phase | Does | On failure |
|---|---|---|---|
| 0 | **admission** | acquire the OS advisory install lock; refuse if another apply holds it; check throttle; resolve channel | exit quietly (not an error — another process has it) |
| 1 | **acquire** | fetch the target ref; verify signature and sequence against the pinned keyring; **no mutation whatsoever** | abort, record, leave the install untouched |
| 2 | **plan** | diff `install-state.json` against the target → an ordered, per-host action list; classify each item 1–5 (§1) | abort with a readable plan dump |
| 3 | **stage** | materialize the target as `versions/<commit>/` beside the live root; run repo-internal checks there | discard the staged tree; nothing was live |
| 4 | **apply-repo** | **atomically swap** the `AQG_ROOT` symlink to `versions/<commit>` | swap back — one `rename(2)`, no intermediate state |
| 5 | **apply-hosts** | per adapter, in registry order, each idempotent and individually journaled: route/prune skills, then (only for class 5) merge hook config, then rules block | roll back **that host**; continue to the next; record in `pending[]` |
| 6 | **smoke** | `aqg_doctor.py --no-cli` against the new root | roll back phases 4–5 |
| 7 | **commit** | write `install-state.json`, clear the journal, prune old `versions/` beyond N | — |

### 5.1 Two-checkout swap, not in-place reset — and why AQG differs from DE

DE resets its checkout **in place** and relies on a recovery tree. AQG must not copy that, for one specific
reason: **AQG's checkout is read by other processes at arbitrary times.** Every hook command string in every
concurrently-running session dereferences `$AQG_ROOT/agent-packs/.../*.sh` on every tool call. `git checkout`
is not atomic across files, so an in-place reset has a window in which a concurrently firing hook sources a
half-updated tree. DE has no such exposure — only its own launcher reads its checkout, and the launcher
holds the lock.

So: stage into `versions/<commit>/`, then one `rename(2)` on the symlink.

**Disk layout** — use `git worktree` so a single object store is shared rather than copying the 26 MB `.git`
N times:

```
~/.deeppattern/
├── aqg-repo/                                       ← the only .git (~26 MB)
├── aqg-versions/
│   ├── 0.15.0-a1b2c3d/                             ← ~17 MB
│   └── 0.15.1-e4f5a6b/                             ← ~17 MB (current)
├── agent-quality-gates -> aqg-versions/0.15.1-e4f5a6b   ← AQG_ROOT; the swap moves only this symlink
└── aqg-state/install-state.json                    ← state, outside the checkout (§4)
```

> **Decision (2026-09-02, Owner): retain 2 trees — current plus previous.** About 60 MB total. A bad update
> needs at most one step back; older versions still have all their objects in `aqg-repo/` and can be checked
> out again on demand. Pruning happens only after a successful phase 7, and **never** prunes the tree
> `AQG_ROOT` currently points at, nor one the journal still references.

### 5.2 When someone is using it: neither stop nor force

The trigger moment **always** overlaps with use — SessionStart fires as a session begins working, and a
skill script fires while the user is working. On a machine with a few sessions permanently open, waiting for
"nobody is using it" means **never updating**.

So the design does not try to avoid the overlap; it makes the overlap harmless. **We never overwrite a file
in place.** The new version lands in a new directory, the old tree stays untouched on disk, and one symlink
is `rename(2)`d at the end. Therefore:

| When the swap lands | Result |
|---|---|
| **before** a hook resolves its path | runs the new version. Fine |
| **after** a hook already opened the file | on POSIX the open handle keeps the old inode alive; it finishes on the old version. Fine |
| while bash is **halfway through reading** the script | bash reads scripts incrementally — a disaster under in-place overwrite; but we did not overwrite, and the inode it is reading has not changed by one byte. **Safe** |

**This is the deeper value of the two-checkout design**: it turns "update" from a destructive operation into
an additive one.

**The one real hazard is mixed generations**: a script resolves `$AQG_ROOT` at start, then a second later
invokes another file under `$AQG_ROOT` and gets the new version — one logical operation spanning two
generations. **Fix: `scripts/_aqg_context.sh` must export the `realpath`-resolved absolute path rather than
the symlink**, pinning a whole skill run to one tree. Ten lines, but without it there will be intermittent,
hard-to-diagnose bugs. Same rule as DE's launcher: **never mix module generations within one process.**

**Host config writes**: a host reads its hook configuration at session **startup** and not again. Writing
`settings.json` mid-update therefore does not affect running sessions; it takes effect on the next one — the
same semantics as a plugin update landing on disk and deferring activation.

**The only thing we ever wait for is another updater** (the install lock). A hook that finds the lock held
no-ops and exits; it **never blocks the user's session waiting**.

### 5.3 Failure and rollback

| Failure point | What the user sees | Recovery |
|---|---|---|
| 1 acquire (network down / bad signature / sequence regressed) | **nothing happened** | record, try next time; invisible to the user |
| 3 stage (disk full / checkout failed) | nothing happened | discard the staged tree |
| 4 swap (rename failed) | nothing happened | `rename` is atomic — it either happened or it did not |
| 5 hosts (one host's write failed) | **partial success** | roll that host back from its pre-write bytes, keep the others on the new version, record in `pending[]`, doctor reports it |
| 6 smoke failed (new version does not run) | back on the old version | swap the symlink back and roll back every host config already changed |
| process killed / power loss | depends on the phase | next trigger reads the journal: resume if it can, roll back if it cannot |
| **rollback itself failed** | surfaced in-session | set `repair_required` and print the backup path. **Never retry into a loop** |

Three rules that run through all of it:

1. **Partial failure at phase 5 is expected, not a bug.** Twelve hosts succeeding and one stuck is a normal
   outcome, and the state file must be able to say so (§4).
2. **Every file is written atomically** (temp file + rename), so the granularity of partial failure is "one
   host's one file was not written" — **never half a file**.
3. **The default outcome is that nothing happened.** A failed update is not the exceptional path; it is the
   most common normal path (no network, bad signature, lock held). Anything uncertain aborts and leaves the
   install as it was — **the updater never blocks the user**.

### 5.4 Crash re-entry

- One OS advisory lock on an open handle for the whole apply. **File existence is never authority.**
- Hooks and the context helper must read an "apply in progress" lease and no-op while it is held.
- Re-entry after a crash reads the journal, validates that the live tree still matches what the journal
  recorded, and then either resumes or rolls back. If it matches neither, the state is `repair_required` and
  the next session surfaces it — never a silent guess.

## 6. Host adapter contract — what already exists and what is missing

**Answering "do we actually need modularization?": yes, and roughly 80% of it already exists.**
[`scripts/aqg_client_registry.py`](../scripts/aqg_client_registry.py) already defines `ClientAdapterSpec`
with `installer_command`, `verify_command`, `uninstall_command`, `is_installed_command`, `skills_source`,
`skills_install_mode_default`, `hooks_surface`, `rules_surface`, and `adapter_actions`, across 20 clients.
Do not build a parallel subsystem. Extend this one.

The gap is specific: **`hooks_surface` and `rules_surface` are prose.** Actual current values:

```
claude-code   hooks=('~/.claude/settings.json via scripts/install_aqg_hooks.py',)
codex         hooks=('~/.codex/hooks.json via scripts/install_aqg_codex_hooks.py',)
cursor        hooks=('.cursor/hooks.json via scripts/install_cursor_support.py',)
kimi-code     hooks=('Kimi Code config.toml hooks; official docs describe fail-open hook failures',)
pi            hooks=('Pi TypeScript extension for session_start/tool_call/...',)
qoder-cli     hooks=('Qoder settings.json with CLI lifecycle hooks, SessionStart, PreCompact, WIP save/recover',)
zed           hooks=('no managed hooks: official lifecycle-hook surface not verified',)
```

Those are sentences for a human. An updater cannot plan against them. Four distinct config **formats**
(Claude `settings.json` merge, Codex/Cursor `hooks.json`, Kimi `config.toml`, Pi TypeScript extension), plus
a set of hosts with no hook surface at all — this heterogeneity is precisely the argument for a typed
adapter boundary.

### 6.1 Structured fields to add

| Field | Values | Purpose |
|---|---|---|
| `hook_config_format` | `claude-settings-json` \| `hooks-json` \| `toml-table` \| `ts-extension` \| `none` | selects the renderer/merger |
| `hook_config_path` | callable → `Path` | never a mutable path handed in by a caller (DE's rule) |
| `hook_delivery` | `managed-merge` \| `plugin-manifest` \| `none` | merge-into-host-config vs ship-a-file vs unsupported |
| `hook_entry_ownership` | marker policy | how an AQG-owned entry is recognized among third-party ones |
| `hook_event_support` | frozenset of events | so a plan can skip an event this host does not have |
| `skills_dest`, `skills_mode` | callable → `Path`, `link`/`copy` | already present in spirit; make it machine-readable |
| `rules_surface_spec` | path + block markers | same treatment as hooks |
| `update_actions` | the four verbs below | the update-specific extension of `adapter_actions` |

### 6.2 The four verbs every adapter implements

```
plan(state, target)   -> [Action]        # pure. no I/O beyond reading host config.
apply(action)         -> Outcome         # idempotent. journaled by the dispatcher, not by the adapter.
verify(state)         -> Evidence        # what doctor consumes.
rollback(action)      -> Outcome         # restores the pre-apply bytes of that host's config.
```

Follow DE's division exactly: **the dispatcher owns vocabulary, ordering, journaling, and validation; the
adapter owns only format-specific mutation mechanics.** And follow `validate_host_specs`: validate the whole
registry **fail-closed at import**, so a spec whose declared metadata contradicts its executable flags is a
startup error, not a runtime surprise on someone's machine.

## 7. Class 5 in detail — merging into host-owned hook config

This is the only genuinely dangerous operation. Requirements:

1. **Ownership marking.** An AQG entry must be recognizable without heuristics.
   [`install_aqg_hooks.py`](../scripts/install_aqg_hooks.py) already does this by matching the canonical
   script-path fragment, and already **converges a divergent AQG-owned command to canonical in place** (its
   "C6" path) — reuse both, do not reinvent.
2. **Never touch what we do not own.** Third-party hook entries, and entries at events we do not manage, are
   preserved byte-for-byte.
3. **Preserve explicit user choices.** `upgrade.sh` already refuses to promote an explicit warn-only opt-in
   to the blocking set. That precedent generalizes: a recorded deliberate user divergence is data, not drift.
   The plan must be able to express "skipped, user opt-out" distinctly from "failed".
4. **Backup before write**, via the existing [`scripts/_aqg_backup.py`](../scripts/_aqg_backup.py), and keep
   the pre-write bytes for `rollback()`.
5. **The four operations** are add-entry, remove-entry, rename-target, reshape-command. Renames and reshapes
   are the ones that need the recorded `hook_set_hash` to be decidable.
6. **Relationship to the tamper guard (verified — no exemption needed).** `pretooluse_aqg_tamper_guard.sh`
   is a **PreToolUse hook that only intercepts the agent's `Write`/`Edit`/`MultiEdit` tool calls**; the
   updater is a Python process writing through ordinary file I/O and never passes through PreToolUse.
   **There is a real relationship in the threat model, though**: that guard exists to stop "an agent reaching
   into `$AQG_ROOT` to edit a gate-bearing file (a hook script, `_secret_patterns.py`, the redaction core) —
   neutering the gate before doing the thing the gate would have caught." **An automatic updater is exactly
   that capability, legitimized and automated** — it may write those protected files, and the guard cannot
   see it. The guard's own comment already says so: "Defense-in-depth, not a boundary. Full tamper-resistance
   needs the read-only / signed install path." So the updater either *becomes* that signed install path
   (upgrading the guard from partial to a real boundary) or becomes the highest-value way around it —
   **depending entirely on whether §9 is implemented. This is what makes §9 non-optional.**

**Policy for automatic application of class 5.** In a fully automatic channel there is no human confirmation
step, so the signature *is* the consent (§9). Class 5 is therefore applied automatically **only** when: the
release is signature-verified on the `stable` channel, the pre-write bytes are captured, the host adapter
implements `rollback`, and the item is recorded in `install-state.json` so `doctor` can report "your hook set
changed in 0.15.0". If any of those is unavailable, the item is deferred to `pending[]` rather than forced.

> **Ordering, and why the manifest need not carry a hook-set digest.** A client
> learns that hook-set membership changed by comparing the installed tree with
> the new one — which is safe only because of the order the pipeline runs in:
> verify the signature, then verify the tree against the manifest, and only then
> read or execute anything from it. Nothing derives class-5 membership from code
> it has not already authenticated. Recorded here because the audit of
> `internal/release/manifest.py` (aud_ifGNyyl_I7UTFOEJ) noted the question was
> left open when a per-host digest was cut from the manifest under YAGNI.

## 8. Skills: add, update, delete, rename

- **Ownership rule** (already implemented in [`scripts/install.sh`](../scripts/install.sh), keep it verbatim):
  only ever remove a **symlink** whose target points **into this checkout's own `skills/` dir**. Never a real
  directory, never a third-party link, never a link pointing outside. DE's `_prune_stale_routes` uses
  `commonpath` against its own source root for the same guarantee.
- **Never clobber a real directory.** DE raises `SkillRouteConflict` and reports it rather than overwriting a
  user's own skill of the same name. Adopt that — silently replacing a user's directory is unrecoverable.
- **Rename** is (prune, route) driven by the state-file diff (§4). There is no rename signal in the payload
  and there does not need to be.
- **A correct existing route is left untouched**, not re-created. Churning symlinks on every check races
  concurrent sessions for no benefit (DE makes this point explicitly in `_route_skills`).
- **Copy mode** must re-copy on content change; the mode is read from per-host recorded state.
- **Windows**: directory junctions, and `core.symlinks` / `core.autocrlf` must be pinned in a managed
  checkout — DE's `updater.py` treats these as install-time invariants, and it is right to.

## 9. Trust — what replaces the human confirmation

Fully automatic application means the "should I upgrade?" prompt is gone. That prompt was the only point at
which a human inspected what was about to run. Something must replace it, because AQG's hooks execute
arbitrary shell as the user on every tool call **and are themselves the guardrails** — an attacker who owns
the update channel owns the alarm system too.

Required before any automatic channel is enabled:

1. **Automatic follows signed tags only** (`stable`), never `main`. `edge` = `main`, manual invocation only.
   `upgrade.sh --ref` already supports pinning; the default and the signing are what is missing.
2. **Verify before apply**, against a keyring **pinned at install time**, not fetched. DE's
   `release_contract.py` + `release-trust.json` is a working implementation with key revocation and a
   monotonic release **sequence** (anti-rollback).
3. **Fail closed.** Signature invalid, key revoked, sequence regressed → do not apply, record, surface next
   session.
4. **Provenance.** Every automatic apply records tag, commit, key id, sequence, and trigger in
   `install-state.json`.
5. **Protected `main` + reviewed releases**, since a tag is only as good as who can create one.

> This upgrades [`INSTALL_VERSIONING.md`](INSTALL_VERSIONING.md)'s "signed release tags are planned but not
> required" from a nice-to-have to a **hard prerequisite**. Automatic update without signature verification
> is a silent remote-code-execution channel into every installed machine simultaneously.

## 10. What can trigger the check

All of these call the same entry point with the same throttle file. They are interchangeable, and more than
one can be installed without conflict — the admission lock makes a double fire a no-op.

| Mechanism | Hosts covered | Fires | New install surface | Verdict |
|---|---|---|---|---|
| **`scripts/_aqg_context.sh`** | **all 20** — every host that can run a skill sources it | first skill invocation | **none** — already sourced by migrated SKILL.md "How To Run" blocks, and `install.sh` already hard-fails if it is absent | **Baseline. Widest coverage at zero cost.** |
| **Lifecycle hook** (`SessionStart`) | hosts with a real hook surface (claude-code, codex, cursor, codebuddy, qoder-cli, kimi-code, trae, devin, pi) | session start | none — one more line in an already-installed hook | **Fast path where available.** Must spawn detached and return immediately; SessionStart is synchronous. |
| **MCP server startup** | any MCP-capable host | server process start | a server the user must configure | **Only if we already ship one.** DE does exactly this (`launcher.py`'s bounded gate) and it works well — but an MCP server cannot push a message to the model unprompted, so its value here is purely "the host starts this process". Not worth building for update alone. |
| **OS scheduler** (launchd / systemd timer / Task Scheduler) | host-independent | truly daily, regardless of usage | a background job per OS; DE precedent in `stopper_launch_agent.py` | **Later, if "daily even when unused" turns out to matter.** Heaviest surface, three OS implementations. |
| **`scripts/upgrade.sh`** | wherever a shell is | when a human runs it | none, exists today | **Keep permanently** as the manual escape hatch and the recovery path. |

**Recommendation: ship the context-helper trigger first, add the hook trigger second, and do not build the
other two yet.** The first gives complete host coverage for free; the second only improves latency on hosts
that have hooks. Both are ten-line callers of the same library, which is the whole point of §0's invariant.

A note on ordering that matters for the roadmap: the context-helper trigger works on hosts where **no hook
surface exists at all** (zed, trae-work-cn, qoderwake, workbuddy, kimi-work). Those are exactly the machines
that would otherwise never update. Building the hook trigger first would leave them behind.

## 11. Prompt surfaces: what this mechanism has to change (answer: almost nothing)

AQG is a prompt-dense product; every new line of text competes with every skill description for the same
attention budget. Batch E of
[`docs/discussion/2026-08-23-audit-trigger-delivery-plan-v3-a1.md`](discussion/2026-08-23-audit-trigger-delivery-plan-v3-a1.md)
already set the discipline: "when everything is proactive, proactive stops being a signal." The update
mechanism obeys the same rule.

| Surface | Change needed | Note |
|---|---|---|
| the 16 `SKILL.md` bodies / descriptions | **no** | the trigger lives in `scripts/_aqg_context.sh`, which is **sourced shell**, not prompt text. Not one word of a How To Run block changes. |
| `CLAUDE.md` / `AGENTS.md` managed rule block | **no** | updating is infrastructure behavior, not a discipline the agent must follow. |
| hook-injected `additionalContext` | **one conditional line only** | see below. |
| `aqg_doctor` output | yes | but that is a CLI report; it never enters the model's context. |

**The only place new prompt text is required**: when `install-state.json` has a non-empty `pending[]`, or
the state is `repair_required` — that is, when **something needs a human** — append one line to the existing
SessionStart preflight `additionalContext`. A successful automatic update emits **nothing** (Owner decision:
fully automatic, no notification).

Worth pinning as an invariant: **prompt text appears only when action is required.** A successful update is
a non-event, and a non-event must not consume context.

## 12. Repository boundary: what stays here and what does not

This is the **development** repo; actual packaging happens in the B repo. The line:

| Belongs to | Content | Why |
|---|---|---|
| **This repo (A)** | the update **client**: state file read/write, planner, transaction/journal, host adapters, the four verbs, trigger call sites | this is the code that ships to a user's machine with the product |
| **This repo (A)** | the `install-state.json` schema and the host adapter contract | they are contracts; both sides must align on them |
| **Outside** | **packaging / signing / release pipeline**: manifest generation, tag signing, key custody, payload classification (the §1 class-5 determination), mirror distribution | runs only on the release side, never ships to users, and handles private keys, which must not enter a development repo |

Proposed location: `../aqg-release/`, a directory sitting alongside this checkout, eventually owned by the B repo.

> **Decision (2026-09-02, Owner): the release-signing skill lives in this repo but is never pushed to the B
> repo.** Location: **`internal/release/skills/aqg-release/`**, private twice over: ① it is absent from
> `internal/carve/allowlist.txt` — an explicit "a path here ships public"
> allowlist, so omission means private; ② it sits under `internal/`, which currently has **zero** entries in
> that allowlist and is the established private zone. Not merely "remember not to add it".
>
> Putting it under `internal/` rather than `skills/` also avoids three things: the `skills.list` roster
> guard, the hardcoded 16-skill array in `scripts/install.sh`, and shipping it to users by accident. The
> Owner symlinks it into `~/.claude/skills/` by hand — one user does not justify changing the installer.
>
> Its mechanics follow DE's `permanent_setup.py`: a masked dialog asks for a **passphrase** (never the key
> material itself), and the passphrase never enters argv, environment variables, shell history, or any
> persisted config; key material is cleared from memory immediately after signing. It signs two things —
> `git tag -s` (human-checkable with `git verify-tag`) and a detached manifest signature (what the client's
> `trust.py` verifies; the anti-rollback sequence can only live there).

**One thing that must stay here**: the pinned **public** key set (the counterpart of DE's
`release-trust.json`). It ships with the product and is what the client verifies against — public keys only,
never a private key.

## 13. Implementation plan: six PRs

Dependencies: PR1 → PR2 → PR3 → PR4 → PR6; **PR5 is independent and can run in parallel with PR1–4**, but
PR6 depends on it.

| PR | Content | What it enables | Changes existing behavior? | Audit depth |
|---|---|---|---|---|
| **1** | `state.py` (`install-state.json` read/write, atomic, 0600, self-migrating schema) + structured fields on `aqg_client_registry.py` (including the already-planned D1 for `rules_surface`) | install state can be recorded and read; `doctor` can report per-host versions | **no** — fields and files added only | **deep** |
| **2** | the four-verb contract in `hosts/base.py` plus `claude_code` / `codex` / `cursor` / `generic` adapters, **wrapping** the existing installers | a uniform way to ask "what would change on this host" (`plan`) and "what is installed" (`verify`) | **no** — every existing CLI is preserved | **deep** |
| **3** | version-tree layout (`git worktree`) + `stage.py` + atomic symlink swap + `lock.py` + `_aqg_context.sh` exporting `realpath` | the install can be swapped to another version tree by hand, and swapped back | slightly — `aqg_root` becomes a resolved path (§5.2) | **deep** |
| **4** | `plan.py` + `transaction.py` (journal / phases / rollback) + `dispatch.py` + `skills_route.py` | **full dry-run**: what this update would change and which class each item is; `--apply` must be passed explicitly, nothing automatic yet | no | **deep** |
| **5** | `internal/release/skills/aqg-release/` + the manifest format + landing the public key set in this repo | signed `stable` tags can be cut | no — never ships to users | **deep** |
| **6** | `trust.py` + `acquire.py` + trigger wiring (`_aqg_context.sh` / SessionStart) + `doctor` reporting `pending` + `upgrade.sh` calling the new engine internally | **automatic update goes live** | yes — this is the only step that actually turns automation on | **deep** |

### Acceptance line per PR

- **PR1** — a corrupt, truncated, or future-schema state file fails closed on read without crashing; the new
  registry fields validate fail-closed at import; existing install / verify / uninstall tests match baseline.
- **PR2** — one **contract test suite run against every adapter** (the same assertions, once per host);
  `plan` is pure and writes nothing; `rollback` restores a host's config to its pre-write bytes.
- **PR3** — a hook running concurrently with the swap never reads a half-updated tree (this needs a real
  test, not reasoning); a failed `rename` leaves state unchanged; pruning never removes the currently
  pointed-at tree or one the journal references.
- **PR4** — **crash-injection tests**: kill the process in each phase and verify the next re-entry either
  resumes or rolls back, leaving no half-configured state; a partial failure (one host unwritable) lands
  correctly in `pending[]` while the other hosts stay on the new version.
- **PR5** — the passphrase never appears in argv, environment, shell history, or any file on disk (verify
  with this repo's own secret-scan); signing self-verifies afterwards (a failure means the package is
  wrong); `internal/release/` is absent from the carve allowlist, pinned by a test.
- **PR6** — a bad signature, a regressed sequence, or a revoked key **never applies**; offline degrades
  silently; `AQG_NO_UPDATE_CHECK=1` works; the trigger does not block session startup (measured, not assumed).

> **Correction (2026-09-02, found while implementing PR1): all six PRs are `deep`; there is no
> `standard` slice.** Marking PR1/PR2 `standard` in the first draft was wrong — under Gate A of
> [`docs/policies/audit-trigger.md`](policies/audit-trigger.md), both "**data model**, schema, or
> migration" and "install / integrity machinery" route straight to `deep`, **independent of how many
> lines change**. `install-state.json` hits both; the host adapters hit the second. This is not
> process weight for its own sake — it is what this feature *is*: **integrity machinery end to end,
> with no cheap parts**. Labelling a slice `standard` would only invite skipping an audit that the
> policy requires.

### Why this order

PR1 and PR2 **change no existing behavior**, so they can land and run in real use for a while, confirming
that the recorded state and `plan`'s judgments match reality — these two are the source of truth for every
later decision, and if they are wrong everything after them is wrong.

After PR3 and PR4 the system **can already compute and execute a complete update**, just not automatically.
That is a valuable intermediate state: `upgrade.sh` can move onto the new engine and gain transactions and
rollback while **the automatic channel is still closed**.

PR6 is the only step that opens automation, and it **hard-depends on PR5** — no signature, no automatic
channel (§9). That dependency is deliberate: binding "can be automatic" and "can verify a signature" to the
same precondition prevents a temporary unsigned automatic path from ever existing.

## 14. Open decisions

- [ ] Signing key **rotation and emergency revocation**. Custody and tagging
  authority are settled (see below); what is not settled is how an install
  learns that a key it already trusts is compromised. The pinned keyring stops
  an unknown key from ever signing a release, but the `revoked` flag only
  protects an install that has already received the revocation — and the
  revocation would ship through the very channel the compromised key controls.
  Closing this needs something the release key cannot forge on its own: a
  separate offline root that signs keyring updates, or a threshold of two
  signatures. Named here in its concrete form because the audit of
  `scripts/aqg_update/trust.py` (aud_EqcBMQUXLJFRgS3o) rated it critical, and
  because it is the one gap that turns from a paragraph into an incident on the
  day a key leaks.
- [ ] Code organization: split the new update layer per host into `hosts/<client>.py` (DE's shape), or
      fold it into the existing single-file registry. Splitting is preferred, but the hard constraint is
      that **existing install / verify / uninstall behavior must not change** — so
      `aqg_client_registry.py` gains fields only, and the split happens only in the new update layer.
- [x] ~~`state_root` location~~ — **decided (2026-09-02, provisional)**: outside the checkout, at
      `~/.deeppattern/aqg-state/`; see §4. Still needs a real Windows check that the path is writable and
      not subject to roaming profile sync.
- [x] ~~Tamper-guard exemption for the updater~~ — **clarified**: no exemption is needed (the guard only
      intercepts PreToolUse tool calls), but it is what makes §9's signature verification mandatory rather
      than optional. See §7.6.
- [x] ~~Retention~~ — **decided (2026-09-02)**: keep 2 trees (current + previous), ~60 MB. See §5.1.
- [x] ~~Signing key custody~~ — **decided (2026-09-02)**: the Owner holds the key and is the only one who
      cuts a `stable` tag; the signing action is packaged as
      `internal/release/skills/aqg-release/`. See §12.
- [ ] Whether `edge` may ever be automatic for internal machines. **Recommendation: manual only.** The value
      of signature verification lies entirely in having no exceptions — a legitimate unsigned automatic path
      is a hole in the trust model, and "this machine is internal" is just a config field with no
      enforcement behind it. Internal machines lagging by one manual `upgrade.sh --ref main` is a cheap price.
- [ ] Make `_aqg_context.sh` export the `realpath`-resolved path (the mixed-generation fix in §5.2) — ten
      lines, but confirm it breaks no assumption in any of the 16 existing `SKILL.md` files.
