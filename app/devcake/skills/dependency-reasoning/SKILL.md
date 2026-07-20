---
name: dependency-reasoning
description: >-
  Conservative dependency edges between work items — declare that A blocks B
  only when B clearly consumes A’s output or cannot start until A is done.
  Use when ordering decomposition parts, proposing blocked-by relations across
  a backlog, or explaining why two items should (or should not) serialize.
  Never invent keys or speculative “nice to have” waits. Step schemas and
  output formats stay in the mission playbook.
metadata:
  source: original (devcake)
  author: devcake
---

# Dependency reasoning

Ordering work is a graph problem. Bad edges serialize independent work and
waste capacity; missing edges cause races and rework.

**Iron law:** propose an edge only when one item clearly consumes another’s
output or cannot start until the other finishes.

## When this skill applies

- Splitting a large effort into parts that may need an order
- Scanning open work for missing “blocked by” relations
- Explaining why two items should run in parallel (no edge)

This skill owns **judgment about dependencies**. Concrete field names, JSON
shapes, index rules, and legal outcomes live in the mission playbook — follow
those formats exactly when you emit them.

## Consume-output test

Declare B depends on A when **at least one** is true and evidenced:

1. B’s implementation or deliverable **requires an artifact** A produces
   (API, schema, design decision, document, migration landed, …).
2. Starting B before A is done would force **throwaway work** or a high risk
   of rework (not mere convenience).
3. A is an explicit **prerequisite policy** stated by a human in the feed
   (human instructions win).

Do **not** add an edge when:

- Items are merely “related” or “same area”
- You want a preferred calendar order without a consumption link
- Keys/titles are similar but scopes are independent
- The dependency is “soft” review preference rather than start-blocking

## Conservatism

- Prefer **fewer** edges. Parallel default; serialize only with cause.
- **Never invent identifiers.** Only reference keys or part indexes the
  playbook and inputs actually give you.
- Prefer **earlier → later** chains that cannot cycle. If the playbook forbids
  forward references, obey it.
- When uncertain, **omit the edge** and state the uncertainty in the summary
  rather than guessing.

## How to inspect candidates

1. Read titles and descriptions for producer/consumer language (“after the
   API exists”, “needs the schema from”, “blocked on design”).
2. Prefer human feed comments over agent speculation when they conflict.
3. Look for shared interfaces: one part defines a contract another implements.
4. Implementation after documentation/design is a common true edge; two
   independent features usually are not.

## Explaining an edge

When you emit or propose an edge, be able to finish this sentence with a
concrete noun:

> “B cannot start before A because B needs **&lt;specific output of A&gt;**.”

If you cannot name the output, there is no edge.

## Anti-patterns

- Full serial chain “to be safe” across independent parts
- Edges from name similarity alone
- Invented mission keys or out-of-range part indexes
- Using dependencies to express priority (use priority fields instead)
- Edges that create cycles or mutual waits

## Companion routing

This skill is pure dependency craft. For causal bugs inside a codebase use
`systematic-debugging`. For shipping ordered work as code use the other
engineering skills as attached on the Dev Type.
