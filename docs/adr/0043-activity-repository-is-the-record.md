# ADR-0043 — The activity repository is the record of a mission's run

- **Status:** accepted (2026-09-10); delivered in stages (see §5)
- **Amends:** ADR-0014 (activity repos), ADR-0036 (upstream offer; reverses
  its rejected option (b)), ADR-0017 (blocker mounts: unchanged, widened
  beside)
- **Ticket:** none (founder ruling)

## Context

A Dev's workspace carries the activity of the missions its work descends
from: the decomposition ancestors to the root and, since the ADR-0036
addendum, the containing project. Two things were still missing, and the
founder ruled that neither is affordable:

1. **A done blocker contributed its work-repository clone and a
   700-character handoff excerpt only.** Everything on the blocker's feed —
   its transcripts, its plan, its review report, an attachment a person
   placed on its ticket — was invisible to the dependent. A starting
   attachment on a blocker was lost in the thread.
2. **A project's own run left no record a Dev could read.** DevCake
   suppresses every write to a project (projects have no comment feed), so
   the ONBOARD step that split the project, its transcript and its reasoning
   went to the observability stream only. Every child then worked under a
   root whose folder held the brief and an empty feed.

Founder ruling (2026-09-10, verbatim intent): the current mission inherits
**all** the activity of the missions upstream of it, down to the first one,
in the shape the internal activity repositories hold it, as separate folders
for free consultation. The prompt need not carry it; the workspace must.

The upstream offer was rebuilt from the vendor at every dispatch. Widening it
to every direct blocker that way would cost one full feed read per blocker
per dispatch — hundreds of vendor requests for a mission gated on hundreds of
siblings, the hourly-budget failure fixed a week earlier.

## Decision

### 1 — The activity repository is the record

Every mission's activity repository (`activity-{instance}-{key}`, ADR-0014
D4) holds the mission's complete activity: the brief, the feed mirror, every
attachment. It is refreshed at **every run boundary**, not only at dispatch:
after each step's close (success, failure, bad output), at completion, at a
hand-off, when a pull request is closed unmerged, and when a conflict is
routed back. The push is the same snapshot commit as the dispatch push
(`push_activity_snapshot`), rebuilt from the vendor for the mission's OWN
folder only; the `upstream/` subtree the Dev cloned at dispatch is left in
place (`keep_prefixes`). Best-effort, audited, never a gate — exactly the
dispatch push's contract.

### 2 — Project runs write their record

Project writes are no longer suppressed. A project run's step card, its
notices and its hand-off baton post as **project updates** (the vendor's
project-native feed), with the transcript uploaded and linked like any
issue attachment. The project's feed mirror already reads updates and their
asset links, so the record push and the upstream offer see them with no new
adapter surface. The status comment stays issue-only.

### 3 — Upstream folders are copies of the record

`upstream/{KEY}/` folders are built from the upstream missions' activity
repositories through two read-only port operations
(`activity_snapshot_tree`, `activity_snapshot_file`), not rebuilt from the
vendor. The tree carries sizes, so the byte budget is decided before a byte
is downloaded. A mission with no repository (never dispatched: a ticket a
person completed by hand, a project that never ran) falls back to the
vendor rebuild of ADR-0036, which still carries its brief and its
mission-level attachments. An upstream repository's own `upstream/` subtree
is not nested; the chain lays every folder out flat.

### 4 — The upstream chain: ancestors, the project, then every direct done blocker

`family_graph.upstream_chain` = decomposition ancestors nearest first, the
containing project, then every **direct** `blocked_by` mission whose status
is `done`, in relation order, each marked with its relation in the ACTIVITY.md
banner (`decomposition parent`, `containing project`, `blocker`). No count
cap; the byte budget (the vendor's attachment cap) is the only limit, and it
truncates from the end, so blockers drop before the project, and the project
before the nearest parent. Blockers are not followed transitively. A blocker
outside the dispatching board's snapshot is counted in one banner line and
not mirrored. An unreadable **ancestor or project** stays a strict-gate gap
(ADR-0036 §4); an unreadable **blocker** is a disclosed gap that never defers
a dispatch, because no dispatch was ever gated on blocker context.

### 5 — Delivery

Three stages, each releasable: (1) the port read operations and the record
push at every boundary; (2) project runs writing their record as project
updates; (3) the upstream chain from repositories with direct done blockers.

## Alternatives rejected

- **Rebuild blockers from the vendor at dispatch** (extend ADR-0036 (a)):
  one full feed read per blocker per dispatch; blows the vendor's hourly
  budget on a mission gated on hundreds of siblings.
- **A local feed for project runs** (persist suppressed posts app-side and
  merge them at mirror time): a second record beside the vendor with its own
  persistence; the project-native feed already exists and mirrors.
- **Transitive blockers**: fan-out; a blocker's blockers are in its own
  handoff and its own record if a person needs them.

## Consequences

- A dependent Dev sees every direct blocker's full record, attachments
  included, under `upstream/`, and a project's children see the split's
  reasoning and transcript.
- One Gitea commit per run boundary (only changed blobs are written;
  identical snapshots commit nothing) and one feed read finalize already
  pays. Upstream reads cost nothing against the vendor budget.
- A blocker's folder reflects its last boundary; a human comment left on a
  done ticket afterward is not in it. The handoff excerpt and the ticket link
  remain.
- ADR-0036's rejected option (b) is reversed by design: repository-scale
  fidelity was the cost then; it is the requirement now.
