"""The migration-debt table must be checkable, not merely self-declared.

`docs/policies/audit-trigger.md` carries a "Known remaining copies (migration
debt)" table whose entire job is stating honestly what has NOT been collapsed
into that file yet. It has now been wrong twice: a row kept claiming the
Decision Engine's Codex integration still held the full original ladder for days
after `d445315` reduced it to a pointer on that repo's `origin/main`.

A stale debt table is worse than no table. It is read as an inventory of what is
left to do, so a false row either invents work that is already done or hides work
that is not. Owner ruling was to keep the table and machine-check the rows a test
can actually judge, rather than hand-editing the stale line a third time.

Three of the four rows are judgeable, and each gets one assertion here:

1. **Decision Engine row** — assert the old ladder stays absent from that repo's
   `origin/main`, so a revert cannot silently re-open the debt.
2. **`audit-self-routing.md` row** — assert the dead pointer stays swept out of
   `docs/`, `skills/` and `examples/`.
3. **every row that cites a test** — assert the cited test still exists and is
   still collected, so the citations themselves cannot rot.

The fourth row (`posttooluse_code_construction_reminder.sh`) is a deliberate
design choice rather than debt — the exemption must travel with the timing
signal — and its category list is already held in step by an existing adapter
test. There is nothing left for this file to assert about it, so no assertion is
faked here.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
POLICY = REPO / "docs" / "policies" / "audit-trigger.md"

TABLE_HEADING = "## Known remaining copies (migration debt)"

# The Decision Engine's Codex integration file. Absence markers alone are a weak
# proxy — a ladder reintroduced in DIFFERENT words would slip past all of them —
# so the check is two-sided: these must stay ABSENT, and the deferral pointer
# below must stay PRESENT. Directionality was verified against the commit rather
# than assumed: all three markers are present in `d445315^` and gone in
# `origin/main`, while the deferral pointer is absent before and present after.
DE_LADDER_PATH = "integrations/codex/AGENTS.md"
DE_LADDER_MARKERS = (
    "DO NOT audit",
    "codex-config/audit-routing",
    # The drifted rung-1 wording the reduction commit called out by name: it read
    # a two-line auth change as trivial. Its return is the regression that matters.
    "a small single-file edit",
)
# What the file was reduced TO. A reworded ladder that still defers is survivable;
# one that drops this line is the failure this row exists to catch.
DE_DEFERRAL_POINTER = "docs/policies/audit-trigger.md"

# Files that legitimately still NAME the retired pointer because they document
# its retirement, mapped to the EXACT number of mentions each may carry.
#
# A whole-file allowlist was the first version and was too coarse: it excused the
# file forever, so a NEW live pointer added to an excused file would have been
# invisible. Pinning the count means a fourth mention in this policy fails even
# though the file is allowlisted.
POINTER_ALLOWANCE = {
    "docs/AUDIT_DECISION_MODEL.md": 1,
    "docs/AUDIT_DECISION_MODEL.zh-CN.md": 1,
    "docs/policies/audit-trigger.md": 2,
}
RETIRED_POINTER = "audit-self-routing"

# Append-only archives are exempt from the sweep. They record what WAS decided, and
# a record of this pointer's own retirement necessarily names it — rewriting them to
# satisfy the gate would falsify the history. Only LIVE, shipped docs must stay
# swept; that is what the 2026-08-11 sweep was about. Scoped to whole trees rather
# than pinned per-file counts because these trees grow by design: a new incident
# write-up citing the retired pointer as history is correct, not a regression.
ARCHIVE_TREES = (
    "docs/audit-evidence/",
    "docs/decisions/",
    "docs/discussion/",
)

# Language by which a row asserts something keeps it honest. Deliberately broad:
# a narrow phrase list would let a row claim coverage in slightly different words
# and escape the requirement to name its test.
COVERAGE_CLAIM = re.compile(r"\bheld\b|\bguard(?:ed|s)?\b|cannot drift", re.I)


def _migration_debt_table() -> str:
    """The table text alone — not the whole policy.

    Scoped so that prose elsewhere in the policy (which discusses the retired
    pointer's history in its opening section) cannot satisfy or trip the row
    assertions below.
    """
    text = POLICY.read_text(encoding="utf-8")
    start = text.index(TABLE_HEADING)
    rest = text[start + len(TABLE_HEADING):]
    end = rest.find("\n## ")
    return rest if end == -1 else rest[:end]


def _decision_engine_repo() -> "tuple[Path | None, str]":
    """Find a Decision Engine checkout; return `(repo, reason_it_was_not_found)`.

    Most machines running AQG's suite have no Decision Engine checkout, so plain
    absence must skip rather than fail. Presence must still be verified by the
    remote URL: a directory named `decision-engine*` is not proof of identity.

    Any checkout will do, including a worktree parked on an old feature branch,
    because the assertion reads `origin/main` rather than the working tree —
    worktrees share remote-tracking refs. That matters: 10 of 12 local worktrees
    on this author's machine sit on branches predating the fix, so a working-tree
    probe would have been red on all of them for no real reason.

    The reason string exists so the caller can tell "this host simply has none"
    apart from "someone asked for this check and it could not run" — the latter
    must never be a silent skip.
    """
    override = os.environ.get("AQG_DECISION_ENGINE_REPO")
    candidates = [Path(override)] if override else sorted(
        Path.home().joinpath("work").glob("decision-engine*")
    )
    reasons: list[str] = []
    for candidate in candidates:
        if not (candidate / ".git").exists():
            reasons.append(f"{candidate}: not a git checkout")
            continue
        try:
            url = subprocess.run(
                ["git", "-C", str(candidate), "remote", "get-url", "origin"],
                capture_output=True, text=True, timeout=30,
            )
            ref = subprocess.run(
                ["git", "-C", str(candidate), "rev-parse", "--verify", "origin/main"],
                capture_output=True, text=True, timeout=30,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            reasons.append(f"{candidate}: git invocation failed ({exc})")
            continue
        if url.returncode != 0 or "decision-engine" not in url.stdout:
            reasons.append(f"{candidate}: origin is not a decision-engine remote")
            continue
        # No origin/main ref means nothing to assert against. Refusing is right;
        # passing would be a false green on an unfetched checkout.
        if ref.returncode != 0:
            reasons.append(f"{candidate}: no origin/main ref (never fetched?)")
            continue
        return candidate, ""
    return None, "; ".join(reasons) or "no decision-engine checkout under ~/work"


def test_decision_engine_ladder_stays_reduced_to_a_pointer() -> None:
    """The row this table got wrong. Assert it, so a revert cannot hide.

    Residual risk, stated rather than implied: this reads the LOCAL
    `origin/main` ref and deliberately issues no `git fetch` — a test suite must
    not do network I/O. On a checkout that has not fetched since a hypothetical
    upstream revert, this would stay green while upstream had regressed. The
    window is real; it is much narrower than the prose-only claim it replaces.
    """
    repo, reason = _decision_engine_repo()
    if repo is None:
        # Setting the override IS a request to run this check. Skipping then would
        # let a misconfigured CI report green forever on the one row with a proven
        # staleness history, indistinguishable from a host that simply has no
        # checkout. Only the unset-and-not-found case is a legitimate skip.
        if os.environ.get("AQG_DECISION_ENGINE_REPO"):
            pytest.fail(
                f"AQG_DECISION_ENGINE_REPO is set but unusable, so the cross-repo row "
                f"went unchecked: {reason}"
            )
        pytest.skip(f"cross-repo row unverifiable on this host ({reason})")

    proc = subprocess.run(
        ["git", "-C", str(repo), "show", f"origin/main:{DE_LADDER_PATH}"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, (
        f"{DE_LADDER_PATH} is missing from origin/main in {repo}: {proc.stderr.strip()}"
    )
    offenders = [m for m in DE_LADDER_MARKERS if m in proc.stdout]
    assert not offenders, (
        f"the migration-debt table says the Decision Engine ladder was reduced to a "
        f"pointer, but origin/main:{DE_LADDER_PATH} still holds {offenders}. Either "
        f"the reduction was reverted upstream, or this row is stale again."
    )
    # The positive half. Absence markers only catch a VERBATIM revert; a ladder
    # reintroduced in fresh words would pass them all. What must hold either way
    # is that the file still defers to this policy instead of restating it.
    assert DE_DEFERRAL_POINTER in proc.stdout, (
        f"origin/main:{DE_LADDER_PATH} no longer defers to `{DE_DEFERRAL_POINTER}`. "
        f"The row claims that file was reduced to a pointer, but the pointer is gone — "
        f"so it is restating the ladder again, in whatever words."
    )


def test_audit_self_routing_pointer_stays_swept() -> None:
    """The 2026-08-11 sweep must stay swept in the three trees the row names.

    Every readable file is scanned, with NO extension filter. The first version
    filtered to `.md/.py/.sh/.json`, which silently exempted 20 files in these
    trees — including the shipped CI templates under `examples/github-actions/`
    and `examples/gitlab-ci/`, a thoroughly plausible place to cite a pointer.
    Binary files drop out by failing to decode, not by being guessed at.
    """
    offenders: list[str] = []
    counts: dict[str, int] = {}
    for tree in ("docs", "skills", "examples"):
        for path in sorted((REPO / tree).rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(REPO).as_posix()
            if rel.startswith(ARCHIVE_TREES):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            hits = [
                f"{rel}:{lineno}: {line.strip()[:90]}"
                for lineno, line in enumerate(text.splitlines(), 1)
                if RETIRED_POINTER in line
            ]
            if not hits:
                continue
            counts[rel] = len(hits)
            # An allowlisted file is excused only up to its PINNED count, so a new
            # live pointer added to a documenting file is still a failure.
            if len(hits) > POINTER_ALLOWANCE.get(rel, 0):
                offenders.extend(hits[POINTER_ALLOWANCE.get(rel, 0):])
    assert not offenders, (
        f"{len(offenders)} line(s) reintroduce the retired `{RETIRED_POINTER}` pointer "
        f"the migration-debt table calls swept:\n" + "\n".join(offenders)
    )
    # A pinned count that has SHRUNK means the documenting prose was removed; the
    # pin is then stale and silently over-permissive, so surface it rather than
    # letting it rot into unearned headroom.
    stale = {
        rel: (allowed, counts.get(rel, 0))
        for rel, allowed in POINTER_ALLOWANCE.items()
        if counts.get(rel, 0) != allowed
    }
    assert not stale, (
        f"POINTER_ALLOWANCE is out of date (file: allowed vs actual) {stale}. "
        f"Lower the pin to match, or the extra headroom silently excuses a future pointer."
    )


def test_migration_debt_table_cites_only_tests_that_exist() -> None:
    """A citation that names a deleted test is the same lie in a new place.

    Two rows earn their "this is held in step by X" claim by naming a test. That
    claim rots exactly like the DE row did the moment someone renames the test,
    so the names are read back out of the table and checked against what pytest
    actually collects.

    Scope, stated plainly: this guards citation INTEGRITY — the named test exists
    and is collected. It does not judge whether the cited test is strong. A test
    gutted to `assert True` would still satisfy this.
    """
    # `\w+`, NOT `[a-z0-9_]+`: the lowercase-only class silently failed to match
    # a citation containing uppercase, so a deliberately bogus name was invisible
    # to this scan rather than reported by it — the check passed the very
    # mutation it exists to catch. An unparseable citation must be a finding.
    table = _migration_debt_table()
    cited = sorted(set(re.findall(r"`(test_\w+)`", table)))
    assert cited, "no test citations found in the migration-debt table"

    # Widening the character class was only half the fix. The pattern still
    # requires BACKTICKS, so a citation written without them stays invisible to
    # the scan rather than being reported by it — the identical false-green shape
    # as the lowercase-only bug. Every `test_` token in the table must therefore
    # be accounted for by the backticked form.
    # A FILE PATH is not a citation — the section prose names this test file, and
    # counting `...test_migration_debt_table_is_honest.py` as an uncited test name
    # is a false positive. Drop paths first, then everything left must be ticked.
    prose = re.sub(r"[\w./-]*test_\w+\.py", "", table)
    all_mentions = re.findall(r"test_\w+", prose)
    ticked = re.findall(r"`(test_\w+)`", prose)
    unparseable = [m for m in all_mentions if m not in ticked]
    assert not unparseable, (
        f"these citations are not in backticked `test_name` form, so the citation "
        f"check cannot see them: {unparseable}. Wrap them in backticks."
    )

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "tests"]
        + [str(p) for p in sorted(REPO.glob("skills/*/tests"))],
        cwd=REPO, capture_output=True, text=True, timeout=300,
    )
    assert proc.returncode == 0, f"collection failed:\n{proc.stdout[-2000:]}"

    # Compare against the exact collected names. A substring test over the raw
    # output would let a citation that is merely a PREFIX of a real test pass.
    collected = {
        line.rsplit("::", 1)[-1].split("[", 1)[0].strip()
        for line in proc.stdout.splitlines()
        if "::" in line
    }
    # A sentinel, not a global count. `len(collected) > 500` coupled this test to
    # the size of an unrelated suite: legitimate consolidation would fail it, and
    # a large-but-partial collection would satisfy it. This file's own tests are
    # in the collected roots by construction, so their presence proves collection
    # actually reached tests/behavior/ without asserting anything about scale.
    assert "test_migration_debt_table_cites_only_tests_that_exist" in collected, (
        f"collection did not reach this file, so a missing citation could not be "
        f"distinguished from a broken scan; got {len(collected)} names"
    )

    missing = [name for name in cited if name not in collected]
    assert not missing, (
        f"the migration-debt table cites {missing}, which pytest does not collect. "
        f"A renamed or deleted test leaves the row claiming coverage that is gone."
    )


def test_every_row_claiming_a_guard_names_the_test() -> None:
    """A row may not claim it is "held in step" without saying by what.

    Citing a test by name is what makes the row checkable at all — the citation
    check above can only verify names it can see. A row that gestures at coverage
    in prose ("same guard as the row above") is back to being self-declared, and
    that is the exact property this file exists to remove. This caught the
    posttooluse row, which claimed a guard and named none.
    """
    unnamed: list[str] = []
    for line in _migration_debt_table().splitlines():
        if not line.startswith("|") or set(line) <= set("|- "):
            continue
        if line.split("|")[1].strip() == "location":  # header
            continue
        if COVERAGE_CLAIM.search(line) and not re.search(r"`test_\w+`", line):
            unnamed.append(line.split("|")[1].strip()[:70])
    assert not unnamed, (
        "these rows claim a guard holds them but name no test, so nothing verifies "
        "the claim:\n" + "\n".join(unnamed)
    )


def test_the_scans_would_catch_a_regression() -> None:
    """Guard against the scans being vacuous — the failure mode that already bit.

    An earlier check in this same work-stream passed while missing a whole
    directory, because it asserted on the files it happened to match rather than
    on the trees it was supposed to reach.
    """
    table = _migration_debt_table()
    assert TABLE_HEADING not in table, "table slice must not include its own heading"
    assert "\n## " not in table, "table slice leaked into the following section"
    assert table.count("\n|") >= 5, f"table looks truncated:\n{table}"

    # Every allowlisted file must still exist, so the list cannot quietly become
    # a set of dead paths that excuse nothing while looking protective.
    for rel in POINTER_ALLOWANCE:
        assert (REPO / rel).is_file(), f"allowlisted file no longer exists: {rel}"
    assert len(POINTER_ALLOWANCE) <= 4, (
        f"pointer allowlist has grown to {len(POINTER_ALLOWANCE)}; a growing allowlist "
        f"turns the sweep assertion into a formality"
    )

    # The sweep must reach all three trees named in the row, not just the one
    # that happens to contain a hit today — and must not silently skip a file
    # type. The extension filter that used to live here exempted 20 files.
    suffixes: set[str] = set()
    for tree in ("docs", "skills", "examples"):
        assert (REPO / tree).is_dir(), f"scanned tree is missing: {tree}"
        assert any((REPO / tree).rglob("*.md")), f"no markdown scanned under {tree}/"
        suffixes |= {p.suffix for p in (REPO / tree).rglob("*") if p.is_file()}
    # YAML is the type the old filter dropped; assert it is present in these trees
    # so the coverage claim is anchored to a real file type, not a hypothetical.
    assert {".yaml", ".yml"} & suffixes, (
        f"expected YAML under the scanned trees; found only {sorted(suffixes)}"
    )

    # The coverage-claim rule must actually fire on a row that claims a guard
    # without naming a test — otherwise it is decoration.
    assert COVERAGE_CLAIM.search("| x | y | held in step by something |")
    assert not COVERAGE_CLAIM.search("| x | y | plain descriptive text |")
