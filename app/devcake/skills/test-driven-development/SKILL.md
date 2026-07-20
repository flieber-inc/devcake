---
name: test-driven-development
description: >-
  Strict red-green-refactor discipline at public seams — write a failing test,
  watch it fail, write minimal code to pass. Use when implementing features,
  bug fixes with a known reproduction, or new modules, before production code.
  Prefer port/fakes over private-helper tests. Companion: verification-before-completion
  for proof after green; systematic-debugging when the failure’s cause is unclear.
metadata:
  source: original (devcake)
  author: devcake
---

# Test-driven development

Write the test first. Watch it fail. Write the minimum code to pass.

**Iron law:** no production behavior without a failing test that names it first.

If you did not watch the test fail, you do not know it tests the right thing.

## When this skill applies

**Always for:** new features, bug fixes with a reproduction, behavior changes,
new public modules or ports.

**Usually skip (document why):** pure renames, docs-only, config/copy, mechanical
follow-the-existing-pattern refactors with no behavior change — still run the
existing suite.

Do not use this skill to redefine mission playbooks or legal outcomes. It owns
**how** you construct tested code, not **what step** you are on.

## Public seams only

- Tests hit the **public interface** under test: functions, port Protocols,
  HTTP paths the product owns — not private helpers or call-count spies on
  internal collaborators.
- Prefer fakes at **port** seams. Fakes must honor the Protocol contract
  (Liskov): same error classes and observable behavior shape as production.
- Assertions use known literals and domain rules — do not recompute the same
  algorithm as production to derive the expected value.

## Vertical slices

One behavior at a time. Do not bulk-write a suite of imagined tests then
implement everything. Agree the seam first (what the caller can see), then one
failing test for one behavior, then the minimum pass, then the next.

## Red → green → refactor

1. **Red** — write one test that fails for the right reason. Run it; confirm
   failure mode. If it passes immediately, the test is wrong or the behavior
   already exists.
2. **Green** — smallest change to pass. No drive-by cleanups.
3. **Refactor** — only after green. Keep tests green. Do not add behavior in
   refactor.

Thinking “skip TDD just this once”? That is the rationalization this skill
exists to block.

## Test environment habits

- Match the project’s stated language/runtime and test runner (read nearby
  tests, Makefile, CI, owner docs). Prefer the same invocation the repository
  documents for local/CI truth.
- Async code: use the project’s explicit event-loop pattern; do not rely on
  deprecated implicit loops.
- Never weaken production invariants “so the test passes.” Never test against
  real secrets or production data.

## Anti-patterns

- Implementation first, “tests later”
- Asserting private structure or mock call counts instead of outcomes
- Giant untested functions with a token test that only imports the module
- Sharing mutable global fixtures that hide order dependence
- Using production code paths to compute expected values in the test

## Companion routing

- Unclear failure cause before you can write a reproduction → 
  `systematic-debugging`
- About to claim the work is complete → `verification-before-completion`
- Shipping the change as a PR → `pr-hygiene` (if available)
