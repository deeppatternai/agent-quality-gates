# Third-Party Notices

**Scope:** this repository — `agent-quality-gates` (AQG).

This product was built by **learning from** the open-source projects listed below and
**independently re-implementing** the relevant ideas. It does **not** fork or copy their
source. AQG redistributes **no** third-party code (see §1).

> **Maintainer note.** Legal / compliance sign-off is the Owner's. This file is an
> engineering-level inventory to support that review and downstream SBOM / license scans.
> Licenses were classified via GitHub's SPDX detection on 2026-06-26; confirm against each
> project's `LICENSE` original before relying on it.

---

## 1. Bundled third-party code (license obligations)

**None.** This repository vendors no third-party source; the AQG skills and scripts are
original. There is therefore no bundled-code notice that must travel with a distribution
of this repository.

> At release, re-scan the repository (e.g. any `*/vendor/` directories) and add a required
> notice here if a bundled third-party asset is ever introduced.

---

## 2. Acknowledgments & inspiration (no code copied)

The projects below informed AQG at the **idea / methodology / structure** level. We learned
from them and wrote our own implementations; **no source or prose was copied.** Under the
idea–expression distinction these are good-faith acknowledgments, not license obligations —
but we list them out of respect and for transparency.

| Project | License | What we learned from it (independently re-implemented) |
|---|---|---|
| [affaan-m/everything-claude-code](https://github.com/affaan-m/ECC) (ECC) | MIT | hooks-over-prompts reliability; skill/plugin ecosystem patterns; interop target |
| [DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) | MIT | YAGNI / anti-over-engineering ladder; agentic-benchmark methodology; invariant & drift canaries |
| [garrytan/gstack](https://github.com/garrytan/gstack) | MIT | cross-product skill structure; review / role patterns |
| [mattpocock/skills](https://github.com/mattpocock/skills) | MIT | write-a-skill authoring shape; `diagnose` feedback-loop tactics; `git-guardrails` policy idea |
| [obra/superpowers](https://github.com/obra/superpowers) | MIT | TDD / worktree / verification-before-completion flow (ideas only; suite not imported) |
| [addyosmani/agent-skills](https://github.com/addyosmani/agent-skills) | MIT | spec / TDD / review / security / ADR skill-structure reference |
| [trailofbits/skills](https://github.com/trailofbits/skills) | CC-BY-SA-4.0 | differential-review / static-analysis / supply-chain / property & mutation-testing **concepts** (reference only). AQG's security review is an **independent OWASP Top 10 + CWE Top 25** build — no prose copied, so ShareAlike is not triggered |
| [anthropics/skills](https://github.com/anthropics/skills) · [claude-cookbooks](https://github.com/anthropics/claude-cookbooks) | official / MIT | skill frontmatter standards; multi-agent patterns (orchestrator-workers, evaluator-optimizer) |
| [openai/skills](https://github.com/openai/skills) | unconfirmed | skill-spec authoring reference (idea only) |
| MPLP (Multi-Agent Lifecycle Protocol) | n/a | handoff-manifest **field-name** inspiration only — the AQG manifest is **not** valid MPLP output |
| [ai-boost/awesome-harness-engineering](https://github.com/ai-boost/awesome-harness-engineering) | curated list | factual index of which projects exist |

---

## 3. Verification (2026-06-26)

- **Method:** per-source SPDX license check + source-trace grep across this repository (names,
  attribution verbs, external links, structural fingerprints) + a targeted expression-copy check.
- **Existing hygiene:** strong inline attribution (`docs/ECOSYSTEM_MUST_INSTALL` "Reference Sources
  (Borrow, Not Adopt)" table + `inspired by` / `adapted from` notes throughout). No fork; no
  redistributed third-party code (§1).
- **Targeted check cleared:** trailofbits/skills (CC-BY-SA) → **reference only**, no prose copied
  (AQG security = independent OWASP/CWE build); ShareAlike not triggered.
- **Open items for final sign-off:** a few vendor-official licenses are unconfirmed by automated
  detection (`openai/skills`, `anthropics/skills`); since only ideas were referenced this carries no
  obligation — confirm before reproducing any of their text.
