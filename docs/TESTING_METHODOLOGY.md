# AQG Testing Methodology

- date: 2026-05-18
- type: cross-product methodology doc (AQG framework-level)
- status: **accepted** (AQG-T2-B; §2.3-2.4 RED-semantics absorbed from an external `tdd-workflow` skill 2026-05-30)
- source: mattpocock/skills commit `67bce91c80cd1020a4f068ced32d0281656842ad`
  ([skills/engineering/tdd/SKILL.md](https://github.com/mattpocock/skills/blob/67bce91c80cd1020a4f068ced32d0281656842ad/skills/engineering/tdd/SKILL.md))
- relates_to:
  - `skills/aqg-systematic-debugging/SKILL.md` (Phase 0 feedback loop)
  - the project's testing conventions (anti-horizontal section mirrored)

## 1. Why This Doc

The `aqg-systematic-debugging` skill (Phase 0 feedback loop) + the
project's testing rules require **vertical slicing** for TDD. Plan
AQG-T2-B identified missing **explicit anti-pattern documentation** —
this doc fills the gap as framework-level methodology reachable from any
AQG-based project.

## 2. Core Rules

### 2.1 Coverage minimum: 80%

Test Types (ALL required):

1. **Unit Tests** — individual functions, utilities, components
2. **Integration Tests** — API endpoints, database operations
3. **E2E Tests** — critical user flows (framework chosen per language)

### 2.2 TDD MANDATORY workflow

1. Write test first (RED)
2. Run test → it should FAIL
3. Write minimal implementation (GREEN)
4. Run test → it should PASS
5. Refactor (IMPROVE)
6. Verify coverage (80%+)

### 2.3 What counts as a valid RED

Step 1-2 say "write test first → it should FAIL", but not every failure is
a legitimate RED. A RED is valid only when **both** hold:

**(a) Reached by one of two legitimate paths:**

- **Runtime RED** — the test target compiles, the new test is actually
  executed, and it fails on an assertion. A test only *written* but never
  compiled and executed does NOT count as RED.
- **Compile-time RED** — the new test references a not-yet-implemented
  public API / type contract, and the compile / type-check failure IS the
  intended assertion (the diagnostic itself encodes the missing contract).
  Legitimate for statically-typed languages (Rust / Go / TypeScript-strict /
  Java) where "the API doesn't exist yet" surfaces at compile time. **Limit**:
  this covers a *missing contract* only — an ordinary behavior bug still
  needs a Runtime-RED assertion once the target compiles; an arbitrary type
  error or wrong test-code API usage does NOT count as compile-time RED.

**(b) Attributable to the intended gap, not to noise.** The failure must
come from the intended business-logic bug / missing implementation /
undefined-behavior-under-test — NOT from unrelated syntax errors, broken
test setup or fixtures, missing dependencies, unrelated pre-existing
regressions, flaky nondeterminism (timing / randomness / wall-clock /
external-service or network dependence / timeouts / resource exhaustion),
or the test itself calling the wrong contract or misusing the API.

> Symmetry with §3: §3 guards against fake **GREEN** (a test that passes
> without verifying anything); (a)+(b) guard against fake **RED** (an
> environment-noise failure mistaken for a real red). Both directions hold.

### 2.4 RED→GREEN symmetry + commit ordering

- **Same-target symmetry** — GREEN must be confirmed by re-running the
  **same** test target that produced the RED: confirm *the previously
  failing test* is now green, not that *some other* test passes (guards
  against a red-A / green-B substitution).
- **Defer implementation commit until GREEN** — within a vertical slice, do
  not commit production / implementation code while its test is still RED. A
  RED reproducer may be committed (`test:`) on a **local / feature branch
  only**; the implementation follows after the same-target GREEN
  (`fix:` / `feat:`). Before the slice reaches **shared / integration
  history** (merge to main, CI-gated branches) the same target must be
  GREEN — for AQG's squash-merge PR flow this is automatic (the PR squashes
  to one green commit on main). Net: shared history never shows
  implementation-before-test and stays bisectable.

### 2.5 Behavior Contract (structured Behavior Lock — v0.14.0)

The test you write in RED locks a **behavior**. Make that behavior explicit as a
structured contract in the construction ledger's `## Behavior Contract` section
(absorbed from OpenSpec's requirement/scenario model), so the test, the
audit-before-commit acceptance criteria, and the closeout evidence all cite one thing:

- A **requirement** is a normative statement — `The system MUST/SHALL <observable behavior>`
  (RFC-2119 keyword, upper-case).
- A **scenario** is `GIVEN <state> / WHEN <action> / THEN <observable outcome>`.
- The RED→GREEN test **covers** a requirement: cite it in ledger row 2 via `covers: R1, R2`.

`THEN` must be an **observable, falsifiable** outcome (an output, a side effect, an error,
a status) — not an implementation detail (`THEN FooService.bar() is called`). The
structural check (BC0–BC4) is **warn-only in 0.14.0** and validates only *shape*; whether
the requirement is *correct* and the `THEN` is *falsifiable* is the job of the human and the
audit-before-commit gate. See the `aqg-code-construction` SKILL §4b for the checker rules.

## 3. Anti-Pattern: Horizontal Slicing

**DO NOT write all tests first, then all implementation.** This is
"horizontal slicing" — treating RED as "write all tests" and GREEN as
"write all code".

### 3.1 Why horizontal slicing produces crap tests

- Tests written in bulk test **imagined** behavior, not **actual**
  behavior
- You end up testing the **shape** of things (data structures, function
  signatures) rather than user-facing behavior
- Tests become **insensitive to real changes** — they pass when behavior
  breaks, fail when behavior is fine
- You **outrun your headlights**, committing to test structure before
  understanding the implementation

### 3.2 Correct approach: Vertical slices via tracer bullets

One test → one implementation → repeat. Each test responds to what you
learned from the previous cycle. Because you just wrote the code, you
know exactly what behavior matters and how to verify it.

```
WRONG (horizontal):
  RED:   test1, test2, test3, test4, test5
  GREEN: impl1, impl2, impl3, impl4, impl5

RIGHT (vertical):
  RED→GREEN: test1→impl1
  RED→GREEN: test2→impl2
  RED→GREEN: test3→impl3
  ...
```

### 3.3 Cross-product application

| Project type | Where vertical slicing applies |
|---------|--------------------------------|
| **Framework / validator code** | New validator rules (`scripts/validate_agent_pack.py` style) — one rule + one test at a time; not all rules then all tests |
| **Pipeline / DAG orchestration** | Per-stage test (vertical) — not all stage schemas first then all execution code |
| **API service** | Per-endpoint test (vertical) — not all schema docs first then all impl |

### 3.4 When to legitimately deviate

- **API contract design phase**: writing schema interfaces (OpenAPI spec)
  before any test — fine for design exploration, but DON'T commit those
  as "tests"; mark as ADR / spec
- **Test infrastructure setup**: harness scaffolding tests need a
  baseline before any product test — exception, document setup tests
  separately

## 4. Bad Test Red Flags

Precision-first: a test that looks green but asserts the wrong thing is worse
than no test — it wears a passing badge over an untested path.

### 4.1 Bad-test patterns (how the *test* is wrong)

- **Assert-through-external-means** — verifying behavior through a back channel
  (a DB query, internal state, a private field) instead of the public surface
  the caller actually uses. It passes when the public contract is broken and
  fails on harmless internal refactors: it tests the *shape*, not the
  *behavior*. Assert through the same interface the caller hits.
- **Skipped tests** — a `@skip` / `xfail` / commented-out test silently lowers
  coverage and hides a real gap. No skipped tests in a green suite without an
  explicit, linked reason (a tracking issue). A skipped test is an untested
  path wearing a passing badge.

### 4.2 AI-regression categories (what the *code* regresses on)

AI-written code regresses on a recognizable set of seams — check each has a
test before GREEN:

- **sandbox / prod-path mismatch** — works on the test/sandbox path but the prod
  path diverges (different env var, table, auth, base URL).
- **SELECT-clause omission** — a query or serializer drops a field the caller
  needs (added to the model later, never to the SELECT / projection).
- **error-state leakage** — an error path returns or leaks internal state
  (stack trace, partial object, secret) instead of a clean, typed error.
- **missing rollback** — a multi-step mutation has no rollback on partial
  failure, leaving half-applied state.

### 4.3 When the *whole suite* is green but wrong

§4.1 and §4.2 are about one test lying. This one is about the runner lying: a
green suite is evidence only if **the bytes that executed are the bytes on
disk**. When a compilation cache decides that question for you, "252 passed"
can describe code nobody wrote.

**The concrete trap (macOS, CommandLineTools Python).** Apple's Python
redirects the bytecode cache to
`~/Library/Caches/com.apple.python/<absolute source dir>/`. No `__pycache__`
ever appears beside the source, so the reflexive `rm -rf __pycache__` clears
nothing and reports success. The cache is validated by source mtime and size,
not content.

**How it bites mutation testing.** The standard loop — copy the file aside,
mutate, run, `cp` it back — restores a file with *identical size* and an mtime
inside the same granularity. The stale `.pyc` still validates, so the restored
tree keeps executing the mutated code. Observed in this repo: the constant on
disk read `…6d`, the imported module returned `…6e`, and the mutation results
of an entire round were meaningless.

**The direction that should scare you** is the inverse of the one that gets
caught. A mutation that *should* fail and does not looks like "my test is
weak"; broken code that tests green looks like "we're done".

**Detection, when a test contradicts the file you are reading:**

- `exec(compile(open(f).read(), f, "exec"), {})` and compare with `import` —
  `exec` bypasses the cache, so a disagreement localizes the problem instantly.
- `sys.addaudithook` on the `open` event prints the path actually read, which
  names the cache directory outright.
- `inspect.getsource()` re-reads the file and will happily agree with disk
  while the loaded module disagrees — do not use it as proof.

**Rules.**

1. Invalidate the compilation cache between every mutation *and* every restore,
   at the location the runtime actually uses — not the one you assume.
2. After restoring, re-run the baseline and require green. A restore is a
   claim; the baseline run is its evidence.
3. Treat this as a class, not a macOS quirk. Any cache keyed on metadata rather
   than content has the same failure mode: JVM class caches, transpiler caches
   (`ts-node`, SWC, Babel), bundler caches, and pytest's own assertion-rewrite
   cache.

## 5. Linking to Other AQG Methodology

| Concern | Related doc |
|---------|-------------|
| Build feedback loop FIRST | `skills/aqg-systematic-debugging/SKILL.md` Phase 0 (AQG-T1-A; 2026-05-18) |
| Skill description style | the CONTEXT-FORMAT spec + validate_agent_pack.py T1-C lint |
| Hard/Soft dependency declaration | T1-B validator rules |
| ADR for irreversible test methodology changes | the ADR-FORMAT spec |

## 6. Troubleshooting Test Failures

1. Use a **tdd-guide** agent
2. Use **aqg-systematic-debugging** Phase 0 feedback loop
3. Check test isolation
4. Verify mocks are correct
5. Fix implementation, not tests (unless tests are wrong)

## 7. Sources

- gstack review SKILL.md — assert-through-external-means bad-test pattern +
  no-skipped-tests criterion (§4.1; absorbed 2026-05-30 per benchmark review
  #183 Tier B2)
- an external agentic-engineering skill — AI-regression seam categories
  (§4.2; #183 Tier B3)
- mattpocock/skills tdd SKILL.md (anti-horizontal section verbatim)
- the project's testing rules (TDD workflow + coverage minimum)
- an external `tdd-workflow` skill — §2.3-2.4 RED legitimacy (runtime /
  compile-time dual path, failure-attribution filter, same-target symmetry,
  defer-commit-until-GREEN); semantics absorbed 2026-05-30, not the file
  itself (the third-party plugin cache is not vendored)
- AQG absorption plan §AQG-T2-B
