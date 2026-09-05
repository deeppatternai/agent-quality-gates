# AQG workflow benchmark — public exploratory harness

This directory ships the public, runnable exploratory part of the AQG workflow
benchmark. It lets a user validate every task instrument, inspect the benchmark
matrix without spending money, and run an explicitly requested exploratory
collection with Claude Code.

The private canonical repository also contains frozen Owner-authorized research
protocols, attestations, primary-run gates, and historical evidence. Those are
not part of this public distribution. In particular, `--primary` requires a
private frozen protocol and is intentionally unavailable here. This boundary
does not affect the default exploratory commands below.

## Included files

    selftest.py          validates every task's good and bad reference
    run.py               plans or executes the exploratory matrix
    score_subprocess.py  isolated task scorer used during execution
    tasks/<task>/        task statement, seed, checks, and held-out references

The task scorer is deterministic and uses no model call. Real collection uses
Claude Code and can spend money; dry-run is always the default.

## Verify the instruments

From the repository root:

    python3 benchmarks/aqg-workflow/selftest.py

Every task must report `[OK]`, followed by `SELFTEST PASSED`.

## Inspect the matrix without spending

    python3 benchmarks/aqg-workflow/run.py

To inspect a smaller plan:

    python3 benchmarks/aqg-workflow/run.py \
      --tasks parse-positive-int \
      --arms baseline aqg-full claude-md-lite \
      --runs 1

Neither command launches a model because `--execute` is absent.

## Run an exploratory collection

Execution currently targets macOS because the runner uses `sandbox-exec` for
the child scorer and model workspace boundary. Install and authenticate Claude
Code first, review the printed matrix and cost estimate, then set a hard cap:

    python3 benchmarks/aqg-workflow/run.py \
      --execute \
      --tasks parse-positive-int \
      --runs 1 \
      --max-cost 0.20

This command spends money. Results are local execution artifacts under
`benchmarks/aqg-workflow/results/` and are ignored by Git.

The public harness makes no efficacy claim: it provides an instrument and an
exploratory runner. A result is not an Owner-authorized primary study and must
not be presented as one.
