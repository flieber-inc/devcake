# ADR-0039 — Repo-backed skill sources: declared mirror sharing, never inferred

- **Status:** accepted (2026-08-31, founder-approved design conversation)
- **Context:** A skill source and a repository card can address the same
  remote — a repo whose tree carries both code and `<skill>/SKILL.md`
  folders, or a dedicated skills repo the operator also wants agents to
  work on through missions and PRs. Because the ADR-0024 mirror is keyed
  by card name, that remote gets one full bare mirror per card: duplicate
  disk, duplicate fetches every sync window, duplicate tokens to keep
  alive — for one repository.

## Decision

### 1 — `backed_by` on the skill-source card

A skill source may declare `backed_by: <repo card name>` instead of a URL.
A backed source has **no mirror, no sync, no token of its own**: its skill
reads (`tree_head`, `read_skill_tree`, `read_skill_file`) are served from
the backing repository card's bare mirror, its freshness rides that card's
sync in the one dispatch gate, and its connection probe delegates to that
card's remote, URL, and token.

`backed_by` and `url` are mutually exclusive (config validation). The
backing name must resolve to a CONFIGURED repository card (a URL-less card
would leave a source that can never sync, failing with a message naming
the wrong card) — the same check refuses deleting the backing card while a
source still reads through it. A rename of the backing card rewrites the
citation like every other repo citation; converting an own-remote source
to backed deletes its old mirror (and with it the stale ledger/health
row). `default_branch` and `subdir` keep their meaning on the backed
card: a backed source may pin a branch (say `stable`) other than the one
work happens on, because the shared mirror holds every branch. An
EXISTING pin is honored end to end — reads serve it and the connection
probe checks it on the remote, so a missing pinned branch fails loud on
both surfaces, never a silent fallback. An EMPTY branch on a backed card
means the BACKING card's branch (the shared mirror's HEAD), not the
remote's own default.

The 2026-08-14 ruling stands untouched: a skill source remains a
first-class skills connection — its own card, its own `<source>/<skill>`
naming, read-only by construction, no PR surface, never PMO-selectable.
Only the physical mirror is shared.

### 2 — Sharing is declared, never inferred

The runtime must never deduplicate mirrors by comparing URLs. Equal-URL
inference would:

- **piggyback credentials** — a card would serve content fetched with
  another card's token, without ever proving its own access;
- **mute the dead-token signal** — each card's sync is a continuous
  per-credential health probe, and a shared fetch hides a revoked token
  until the surviving card disappears;
- **muddy failure attribution** — one physical fetch serving N cards has
  no honest answer to "whose credential failed";
- **guess identity** — URL canonicalization misses real duplicates
  (scheme and host-alias variants) and can be wrong in ways that couple
  cards the operator meant to keep apart.

Repo identity is a fact the operator knows; it therefore lives in
operator-owned, gated config data — one `backed_by` line — not in runtime
inference.

### 3 — One resolution chokepoint

`RepoCache.mirror_name_of(name)` maps a backed source to its backing card
and everything else to itself. Sync entry (`ensure_fresh`), stale-cache
checks (`has_last_good`), the tree reads, and the remote probe all resolve
through it, so the backed pair can never fetch one bare repo under two
locks. Both gates — mission dispatch and the steward context gate — resolve skill cards through `repo_sourcing.resolved_skill_cards` before the needed-set union, so run records (`mirror_repos`) snapshot PHYSICAL mirror names while `skill_repo_heads` provenance stays keyed by the source card;
a backing card that also rides sourcing (it *is* the work or reference
repo) classifies as sourcing — a fetch failure there defers dispatch and
is never downgraded to a context-card stale/omit. Mirror warm-up skips
backed sources; their physical mirror warms with the repository card.
Health likewise carries no mirror row for a backed source — its state IS
the backing card's row.

## Alternatives rejected

- **Runtime URL-dedup** — Decision 2's reasons.
- **Folder-scoped fetching** (mirror only the skills subtree): git's fetch
  protocol is ref-based, not path-based. Sparse checkout only thins a
  working tree (mirrors are bare); path-filter fetch (`sparse:path`) was
  removed from git itself; partial clone (`--filter=blob:none`) fetches
  blobs lazily **at read time**, which breaks three mirror guarantees at
  once — local atomic reads, fail-at-the-gate (never mid-payload), and
  stale-cache open mode while the forge is down. The duplication problem
  is a *count* of mirrors, not their size; `backed_by` fixes the count.
- **Skill facet on the repo card** — re-litigates the 2026-08-14
  first-class ruling; rejected without discussion.
