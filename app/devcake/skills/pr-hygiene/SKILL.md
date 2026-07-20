---
name: pr-hygiene
description: >-
  Commit and pull-request discipline — one intent per PR, honest scoped
  commits, reviewable descriptions, no force-push of shared branches, no
  tooling junk in the tree. Use when committing work, opening or updating a
  pull/merge request, or writing a PR description. Companion:
  verification-before-completion before you claim the branch is ready.
metadata:
  source: original (devcake)
  author: devcake
---

# PR hygiene

A pull request is a unit of review, not a dumping ground. Optimize for one
thing: a reviewer (human or AI) can verify the change without follow-up
questions you could have answered in the description.

## Scope

- **One PR = one intent.** Bug fix plus unrelated refactor → split. Mixed
  intent gets worse reviews of both halves.
- **Never widen scope mid-branch.** Unrelated problems go under “Out of scope
  / follow-ups,” not into this diff.
- **No drive-by edits** (formatting churn, renames, comment rewrites in files
  you did not otherwise touch) unless that *was* the task.

## Commits

- Commit only work that is complete enough to stand as a checkpoint you would
  be comfortable explaining. Prefer logical commits over a single megadump when
  the history helps review; never use commits as a panic save of broken state
  you intend to leave behind.
- **Message:** imperative summary under ~72 chars stating the change and, where
  it fits, the why. Body only for what the diff cannot say: constraints,
  rejected alternatives, migration notes.
- Never overstate (“fix all flaky tests”) or hide runtime changes under a
  “docs” subject.
- **Do not commit:** generated artifacts, credentials, editor configs, local
  virtualenvs, agent scratch files, or skill trees that tooling materialised
  outside the product’s source contract. Check `git status` for strangers
  before every commit.

## Branch and remote

- Follow the branch naming and push rules the mission playbook and forge
  instructions already give you. Do **not** force-push shared mission branches.
- Prefer updating an existing PR for the same branch over opening duplicates.
- If the remote rejects the push, capture the error; do not invent workarounds
  that bypass protection.

## PR description

Include:

1. **Intent** — one or two sentences on what this PR does and why.
2. **Plan link / context** — mission key, plan summary, or “deviation from
   plan” if reality forced a change (state the deviation prominently).
3. **How to verify** — commands or checks a reviewer can run (fresh evidence;
   see `verification-before-completion` if available).
4. **Risk / rollout** — migrations, flags, backward compatibility.
5. **Out of scope** — explicit non-goals.

Title: concise, matches intent; include the mission key when the playbook or
forge template requires it.

## Anti-patterns

- Force-push to rewrite published history on a shared branch
- PR body that only says “fixes stuff” or pastes a raw commit dump
- Secrets or `.env` in the diff
- “Also reformatted the repo” as a side effect
- Opening a second PR for the same branch when one already exists

## Companion routing

- Not yet proven green → `verification-before-completion`
- Defect not understood → `systematic-debugging`
- Implementation under construction → `test-driven-development`
