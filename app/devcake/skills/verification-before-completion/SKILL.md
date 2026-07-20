---
name: verification-before-completion
description: >-
  Evidence before success claims — identify the proof command, run it fresh,
  read exit codes and output, then claim only what the evidence supports. Use
  before marking work done, fixed, or passing; before commit or PR; whenever
  tempted to assert status from build alone. Companion: test-driven-development
  for constructing tests; systematic-debugging when verification fails.
metadata:
  source: original (devcake)
  author: devcake
---

# Verification before completion

Claiming work is complete without verification is dishonesty, not efficiency.

**Iron law:** no completion claims without fresh verification evidence.

If you have not run the proof command in this session and read its output,
you cannot claim it passes.

## When this skill applies

Use whenever you would say: done, fixed, green, shipped, ready for review,
or “should work.”

Use before committing, opening/updating a PR, or writing a summary that
asserts quality.

This skill does **not** invent mission outcomes. Completion of a mission step
is defined by the playbook; this skill owns honesty of **evidence** for
technical claims inside that work.

## The gate

Before any success claim:

1. **IDENTIFY** — what command(s) or checks prove this claim for *this* change?
2. **RUN** — execute them fresh and complete (not a remembered earlier pass).
3. **READ** — full relevant output, exit code, failure counts.
4. **VERIFY** — does the output confirm the claim?
   - If no: state actual status with evidence.
   - If yes: state the claim **with** the evidence (command + result).
5. **ONLY THEN** make the claim.

## Choose proof that matches the change

Map the change type to the repository’s real verification path. Prefer what
owner docs, CI, and nearby code already use. Typical patterns:

| Change type | Minimum proof pattern |
|---|---|
| Library / app logic | Unit/integration tests for the touched seam |
| Container / image / compose | Image build **and** runtime/health where the user path is run/up |
| UI package | Build **and** load/health or the project’s UI test suite |
| Docs-only | Re-read for accuracy; no false runtime claims |

**Stale image trap:** if tests run inside an image that COPY’d sources at
build time, re-running pytest on an old image grades the last bake, not the
working tree. Rebake, bind-mount, or run against the tree on the documented
runtime — never claim green from a stale artifact.

“Build succeeded” alone is not proof when the user-facing path is run/up.

## Report honestly

When summarizing:

- Name what ran and what passed.
- Name what could **not** run and what remains unproven.
- Never imply full-system proof from a unit slice.

## Anti-patterns

- “Should work” / “looks good” without a command
- Reusing an earlier test run after further edits
- Claiming CI green without seeing CI
- Fixing a test by deleting or weakening the assertion
- Partial suite green sold as full suite green

## Companion routing

- Failure under verification with unclear cause → `systematic-debugging`
- Need a new failing test for the fix → `test-driven-development`
- Packaging the change for review → `pr-hygiene` (if available)
