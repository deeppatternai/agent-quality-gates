---
next_action: "merge feat/foo PR after CI green and run install.sh --force on operator workstation"
blockers:
  - "PR review pending Owner approval"
  - "operator workstation symlinks stale; reinstall required"
---

# Owner cold-transfer input fixture (v1.0.0)

This is a Transfer Test Pack v1 fixture used by Task 6 to verify that a
cold operator (no chat history) can extract a deterministic
`next_action` + `blockers[]` from a frozen handoff artifact.

The frontmatter above is the schema-defined cold-operator surface;
the prose body below describes the prior session's terminal state for
human-readability but is **not parsed** by Task 6.

---

## Prior session terminal state (illustrative)

- Branch: `feat/foo` ahead of `origin/main` by 3 commits
- CI: 2 of 3 checks pass; quality-gates pending (~30s)
- Last audit: 469e66f5 (gpt-5.5 + gemini double, 9/12 accepted, $0)
- Closeout: complete; ledger saved at `docs/decisions/2026-05-04-foo.md`
- Owner authorization required before merge: yes

## Why a cold operator needs this artifact

Without the frontmatter `next_action` + `blockers[]`, a fresh Claude /
Codex session given only `repo + this artifact` would have to reason
through the whole `git status + gh pr view + commit history + closeout
ledger` chain to reach the same decision. The deterministic frontmatter
short-circuits that into a schema lookup — which is what Task 6
verifies.

True LLM-based cold reasoning (no frontmatter; LLM extracts the same
fields from prose alone) is **deferred to v1.1 / Continuity Drill**
because the deterministic gate posture cannot tolerate LLM
nondeterminism in v1.
