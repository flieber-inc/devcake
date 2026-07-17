---
name: pr-hygiene
description: Commit and pull-request discipline — scoped commits, honest messages, reviewable PRs. Use when committing work, opening or updating a pull request, or writing a PR description.
metadata:
  source: original (devcake)
  author: devcake
---

# PR hygiene

A pull request is a unit of review, not a dumping ground. Everything below
optimizes for one thing: a reviewer (human or AI) can verify your change is
correct without asking you follow-up questions.

## Scope

- One PR = one intent. If your branch fixes a bug AND refactors an unrelated
  module, split it. Mixed-intent PRs get worse reviews of both halves.
- Never widen scope mid-branch. Found an unrelated problem? Note it in the PR
  description under "Out of scope / follow-ups" instead of fixing it here.
- Drive-by edits (formatting churn, renames, comment rewrites in files you
  didn't otherwise touch) are scope creep. Revert them unless they were the
  task.

## Commits

- Commit only work that is complete and tested. A commit is a checkpoint you
  would be comfortable shipping, not a save button.
- Message format: an imperative summary line under ~72 chars that states the
  change and, where it fits, the why: `fix(poll): skip archived missions —
  label reads 404 on them`. Body paragraphs only for what the diff cannot
  say: constraints, rejected alternatives, migration notes.
- Never write a message that overstates the change ("fix all flaky tests")
  or hides part of it (a "docs" commit that also edits runtime code).
- Do not commit generated artifacts, credentials, editor configs, or files
  materialized into your workspace by tooling (agent scratch files, local
  skills, virtualenvs). Check `git status` for strangers before every commit.

## The PR description

Structure it as claim → evidence:

1. **What & why** — two or three sentences: the problem, the change, the
   user-visible effect. Link the ticket/mission.
2. **How it works** — only what a reviewer can't infer from the diff:
   design choices, invariants preserved, edge cases handled.
3. **Evidence** — what you ran and what you saw: test suite output summary,
   a manual run, screenshots for UI. "Should work" is not evidence; paste
   what happened. Name anything NOT verified and why.
4. **Out of scope / follow-ups** — anything you deliberately did not do.

## Before requesting review

- Re-read your own diff hunk by hunk as if you were the reviewer. Remove
  leftover debug output, TODOs you meant to resolve, and commented-out code.
- Run the project's test suite and linters; the PR that arrives red wastes
  everyone's first round.
- Confirm the branch is current with its target; resolve conflicts yourself
  rather than shipping them to the reviewer.
