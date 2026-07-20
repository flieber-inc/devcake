---
name: systematic-debugging
description: >-
  Four-phase root-cause debugging — investigate, pattern-match, hypothesize,
  then fix. Use when facing a bug, test failure, build break, unexpected
  behavior, or flaky path, before proposing or shipping a fix. Forbids
  symptom patches without a causal chain. Companion: verification-before-completion
  for proof after the fix; test-driven-development when encoding the fix as a test.
metadata:
  source: original (devcake)
  author: devcake
---

# Systematic debugging

Random fixes waste time and create new bugs. Symptom patches hide the cause.

**Iron law:** find root cause before changing production behavior.

## When this skill applies

Use for any technical defect: failing tests, production anomalies, wrong
outputs, performance regressions, integration breaks, build failures.

Use especially when speed pressure tempts a one-line “just try this” patch.

Do **not** use this skill to invent mission outcomes or hand-off rules —
those live in the mission playbook. If you are blocked by missing credentials,
permissions, or an external decision only a human can make, stop after a real
attempt and report the exact error through the playbook’s hand-off path.

## Phase 1 — Investigate (no fix yet)

1. **Reproduce** with a concrete command, input, or path. Note environment
   (branch, config, dependency versions) from evidence, not guesswork.
2. **Collect symptoms**: exact error text, exit codes, logs, stack frames,
   timestamps, IDs. Prefer primary evidence over paraphrases.
3. **Locate first incorrect durable state** — separate UI, API response,
   database/file state, queue, cache, and logs. Name which layer is first wrong.
4. **Read recent relevant changes** (git log/blame on the failing path) only
   after you know what fails; history is a hypothesis source, not proof.

Do not propose a production fix until Phase 1 has a written causal sketch.

## Phase 2 — Patterns

Ask, with evidence:

- Is this a regression (worked before) or a new path?
- Local vs environment (CI, credentials, clocks, network)?
- Data-dependent (one tenant/input) vs universal?
- Race, ordering, or idempotency?
- Wrong layer (caller vs callee vs contract)?

Compare failing and succeeding cases. One difference often pins the cause.

## Phase 3 — Hypothesize

State one falsifiable hypothesis at a time:

> “X fails because Y (mechanism), so Z observation should hold.”

Design the **smallest** check that would disprove it. Prefer reading state
and running a focused command over large refactors. If disproved, record
why and form the next hypothesis — do not stack speculative patches.

## Phase 4 — Fix the owning cause

Change the component that owns the broken invariant or contract. Do not add
compensating logic in a caller merely because it is easier to reach.

After the fix:

1. Re-run the reproduction path.
2. Run the narrowest relevant tests, then widen with blast radius.
3. Claim success only with fresh evidence (see `verification-before-completion`
   if that skill is available).

If encoding the bug as a failing test first fits the change, use
`test-driven-development` — this skill still owns the causal investigation.

## Evidence order

Prefer, in order:

1. Live failure output, logs, and durable state you just observed
2. Current source, tests, and configs for the revision under test
3. Owner docs and recent commits
4. Older notes and second-hand reports

Surface contradictions; do not blend generations of code or config.

## Anti-patterns

- Fixing without a reproduction
- “It works on my machine” without capturing the delta
- Broad refactors to silence one symptom
- Catching and swallowing exceptions to hide the failure
- Multiple independent changes in one commit “to see if it helps”
- Declaring root cause from a stack overflow post without matching evidence

## Output when reporting a diagnosis

Keep it short and actionable:

1. Symptom (one line)
2. First incorrect state and evidence
3. Root-cause chain (observed facts)
4. Fix (what changed and why it is the owner)
5. Verification commands and results
