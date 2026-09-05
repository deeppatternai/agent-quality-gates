# Security Policy

Agent Quality Gates (AQG) is a **private** engineering-quality toolkit — skills, hooks, and
scripts installed into a local agent harness (Claude Code / Codex). It runs with the
**developer's own privileges**: it is not a sandbox and does not attempt to contain a hostile
agent. This policy covers vulnerabilities in AQG's own code and in the guards it ships.

## Supported versions

Pre-1.0. Fixes land on `main` first and ship in the next tagged release (source of truth:
[`VERSION`](VERSION) / [`CHANGELOG.md`](CHANGELOG.md)).

| Version | Supported |
| --- | --- |
| latest release (`0.13.x`) + `main` | :white_check_mark: |
| older tags | :x: — upgrade to latest |

## Reporting a vulnerability

**Do not open a public / regular GitHub issue for a security report.**

Report it **privately to the maintainers**:

- **Preferred** — GitHub private vulnerability reporting: repo **Security → Report a
  vulnerability**. If that option is absent, a maintainer enables it once in
  **Settings → Security → Private vulnerability reporting** (it is off by default on this
  private repo).
- **Otherwise** — reach a repository maintainer through a private channel; do not post the
  details anywhere public.

Please include:

- the affected file / hook / script + commit, and how it was installed;
- steps to reproduce from a clean checkout;
- the trust boundary crossed, and whether exploitation needs local shell access, a malicious
  repo, a malicious package, or maintainer credentials;
- any PoC logs with secrets, tokens, and private paths **redacted**.

AQG is a small project: response is **best-effort**, not a calendar SLA.
We will say plainly whether a report is reproducible, out of scope, already fixed, or needs a
stronger attack path.

## What is in scope

AQG ships security-sensitive guards; the highest-value reports are ways to make one **fail
open** or leak. For example:

- A **fail-closed guard that can be made to silently pass** — the secret-scan gate
  (`agent-packs/claude-code/hooks/pretooluse_secret_scan.sh`), the anti-tamper guard
  (`pretooluse_aqg_tamper_guard.sh`, [#328](https://github.com/deeppatternai/agent-quality-gates/issues/328)),
  or a tamper canary — certifying "clean" while a real secret or a neutered engine slips through.
- A **secret-scan false negative** in `scripts/_secret_patterns.py` / `scripts/_redaction_common.py`
  that lets a real credential reach a decision log, ledger, or agent context.
- A skill helper that can be driven into a **destructive or out-of-boundary action**, a
  **command-injection / path-traversal** in a script, or a **raw-secret echo** in an error or
  redaction path.

## What is out of scope (known + documented)

AQG's in-process hooks are **defense-in-depth, not a security boundary** (see
[`docs/INTEGRATION_GUIDE.md`](docs/INTEGRATION_GUIDE.md)). These are known limitations, not
vulnerabilities:

- An agent **with shell access** that edits the guard itself, mutates files via `Bash`
  (`mv` / `tee` / `python -c`), or manipulates `cwd` to bypass an in-band hook. The real fix is
  the read-only / signed install path (a planned hardening).
- The hooks being **advisory / opt-in** for a human who does not set `AQG_AGENT` (transparent
  by design).

## Boundaries AQG does not cross

By design, AQG **never** touches production, secrets, branch protection, or raw private data;
any destructive / irreversible action requires explicit Owner authorization; skill helpers are
read-only / print-only. A demonstrated violation of one of these invariants **is** in scope.
