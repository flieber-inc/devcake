# ADR-0042 — The feed for readers: step cards, one fold for the record, one status comment

Status: Accepted 2026-09-09 — implementation in progress (the plan's PR sequence: the port operation, the primitives, the edit chokepoint and the step card are the first four PRs).

## Context — the feed is a record that people are asked to read

Every mission's PMO feed (a Linear issue's comments, an issue's comments on
GitHub, GitLab or Gitea, a Linear project's updates) is two things at once.
It is DevCake's **record**: the orchestrator re-derives state from it after
a restart, from any instance, on any vendor — step sequence from the
transcript's file token, discovery arithmetic from harvest and receipt
markers, merge mode from the settle/retry/hand-off markers, provenance from
the sentinel (ADR-0014, ADR-0031, ADR-0033, docs/03 §8). And it is the place
a **person** looks to learn what happened, what the result is, and whether
something is waiting on them.

The record won. Read as a person, a three-step mission is fifteen comments:
the Dev's closing message quoted in full, then the same message again under
a reply marker that one vendor renders as visible text; a token report as
its own entry; a harvest entry whose first line is a marker; a receipts
entry that is nothing but markers; the version sentinel as a code chip on
every post; the run id in every title; prose cut mid-sentence with a
pointer to the attachment. About a third of the entries exist only for the
scanner, and nothing answers the three questions a reader has in one place.
The Dev, by contrast, is well served: its activity folder is a faithful
mirror of the feed with every attachment beside it (ADR-0014 D3), and its
prompts and skills rely on that folder's shape.

Three constraints govern the redesign:

1. **Nothing the Devs receive changes.** The activity folder — `MISSION.md`,
   `ACTIVITY.md`, the `N_TYPE.md` transcripts, `PLAN_N.md`,
   `DISCOVERY_N.md`, review reports, deliverable archives, ancestor mirrors
   — keeps its file set, its section structure and its content. No loss,
   no reshuffle. Prompts and skills that grep it keep working.
2. **Any human reader, on any vendor.** The improvement must reach a Linear
   board and an issue board alike, with one structure, not a Linear
   feature with a degraded fallback.
3. **The record stays the record.** Every marker the orchestrator scans
   is still posted, still on the feed, still read with the same scans; the
   append-only doctrine and the fail-safe derivations are untouched.

Two facts about vendors, verified in the field, shape the mechanics. An
HTML comment (`<!-- … -->`) is hidden by GitHub, GitLab and Gitea and shown
as text by Linear, so no marker may ride as an HTML comment. Every vendor
renders a collapsible block (a `<details>` element on the issue vendors,
Linear's own collapsible section in its editor and markdown), every vendor
lets the app edit its own comment, and only Linear threads comments.

## Decision

### 1 — One record, two projections

The feed remains the single durable record. What changes is how the record
is **arranged** for a reader and how it is **unfolded** for the Dev.

- For the reader, the record is arranged into a small number of shapes with
  one voice: a **step card** per step, a **notice** when a person is
  addressed, a **status comment** kept current, and folds that hold the
  bookkeeping out of the way.
- For the Dev, the activity mirror becomes an explicit **projection**: it
  unfolds each card into the entries the folder carried before this ADR, in
  the same order, with the same headers, bodies and attachment markers. The
  precedent is the one-thread-per-step addendum to ADR-0014, which already
  renders threaded bookkeeping as flat top-level entries so the Dev reads
  the same file on a threading vendor and on a flat one. This ADR makes
  that rule general: **the Dev folder is derived from the record, not
  copied from the rendering.**

### 2 — The step card

Every step end (ONBOARD, PLAN, EXECUTE, REVIEW; STEWARD runs are the named
exception, docs/03 §4b) posts exactly **one top-level comment**, the step
card, in this order:

1. **Header line.** An outcome glyph, the step number and type, the
   outcome word, the wall-clock duration and the recorded cost:
   `✅ Step 3 · REVIEW · approved · 12 min · $3.05`. The glyph vocabulary is
   the one docs/03 already reserves (✅ done or approved, 🔀 PR opened,
   📋 plan, ✋ a person is needed, 🔁 rejected, ⚠️ failed or gave up,
   ⏳ waiting, 🧩 conflict directive, 🔄 freshness re-review).
2. **The Dev's answer**, blockquoted (the quoting quarantine of
   ADR-0014 D2 is unchanged: model text never counts for a scan), cut at a
   **paragraph or sentence boundary** under the inline budget, never
   mid-sentence, with the pointer to the full transcript. This is the text
   the reply comment used to repeat; the duplicate is gone.
3. **Result and Next.** Two DevCake-authored lines: what now exists (the
   PR, the plan file, the verdict) and who acts next — DevCake, the merge
   sweep, or the reader, with the one command a reader might run (the
   approval footer of docs/03 §5 moves here).
4. **The transcript attachment** (`N_TYPE.md`), as today.
5. **The fold** — a collapsed block titled for what it holds
   (`Details — token report · 1 discovery · receipts · run`), containing,
   verbatim and in this order: the token report (docs/03 §8, normative
   text unchanged), the discovery harvest with its markers and attachment
   pointer, and the run identity. Every backticked marker the orchestrator
   scans rides here, unquoted, so the scans read exactly what they read
   today from `feed.unquoted(body)`. The provenance sentinel closes the
   card, AFTER the fold: the sentinel's classifier anchors at the end of
   the unquoted body, so nothing may follow it — a fold that closed after
   the sentinel would turn every card into a human comment and trip the
   Freshness Gate on DevCake's own record.

The card carries the step's whole record in one body: transcript token,
answer, token report, harvest, sentinel. Three top-level entries and one
thread become one entry.

### 3 — The fold, and what arrives later

The fold is rendered by **one function behind the feed chokepoint**, with
the vendor's collapsible syntax chosen by the adapter's capability row: a
`<details><summary>` element on the issue vendors, Linear's collapsible
section on Linear. It is the only vendor-specific rendering in this design.

Bookkeeping that arrives **after** the card was posted — the steward's
routing receipts on a source mission, the deliverable note on the
completion notice — is **appended to the fold of the comment it belongs
to**, through a new port operation, `edit_feed`, that edits DevCake's own
comment. The merge-state markers (settle, retry, hand-off) and the
directive counters are NOT appended: the merge sweep picks its driver by
the marker's entry time and an edit keeps the old entry's time, so those
stay on notices of their own (§4) — an append is only ever a set-valued
record whose order does not matter. The fold only ever grows: the record stays
monotonic, `pending = posted − receipted` reads the same, and the edit is
DevCake's own write, which the feed memo already handles. A vendor without
an edit operation (none today) would fall back to a reply where the vendor
threads and to a compact top-level line elsewhere. Editing is not used for
anything a person is asked to read; notices are always new comments.

### 4 — Notices: what is written to a person

A notice is a top-level comment whose reader is a person and whose purpose
is to make them act or to disclose something to them. It opens with a
fixed lead — `✋ **Needs you.**`, `⚠️ **For the record.**`, `ℹ️` — then one
sentence saying what, then what to do, then the fold with any markers. The
hand-off baton, the plan-approval request, "awaiting your merge", the
give-up notice, loop and unlimited-attempt warnings, freshness disclosures,
out-of-pipeline merge detection, illegal or unknown outcomes, decomposition
notes and depth limits are all notices. A reader scanning a feed finds the
hand glyph and knows every place they were addressed. Every notice
carries today's comment text, verbatim, in a `Record` section of its fold:
the head is for people, the fold is the record, and the Dev's folder
unfolds the record — so a notice, like a card, changes nothing a Dev
reads.

Directives addressed to the **next Dev** (the conflict-resolve directive,
the freshness re-review directive) are notices too: a person should see
that a Dev was redirected and why. Their counted markers ride in the fold.

### 5 — The status comment

Each mission gets **one status comment**, **edited in place** at every step
end, park, hand-off, merge event and completion. It is created by
**whichever dispatch finds the feed without one** — not by ONBOARD, not by
"the first step". DevCake has no privileged entry point: a scheduled task
starts at EXECUTE, a person orders a mission into REVIEW by choosing that
label, a mission that predates this ADR is next dispatched at whatever
step it is on, a hand-off resumes mid-pipeline. Every dispatch therefore
looks for the status comment by its marker and creates it when absent,
before the run starts (a stalled deferral creates it too — addendum
below); the check is idempotent and needs no stored id, so
a restart or a second instance neither duplicates nor loses it. It carries the step ladder with outcomes and costs, the current
state in one sentence (running step 2; parked, waiting on you to approve
the plan; awaiting merge of the PR; done), the PR link, the cumulative
cost, and the ancestry link when the mission is a decomposition child. It
is the reader's first stop and it is a **view**: it carries no counted
marker, it is not material to the Freshness Gate, and every fact in it is
derived from the record. DevCake finds it again by a marker in its own
fold (`devcake:status:v1`), not by an id kept elsewhere, so a restart or a
second instance edits the same comment.

### 6 — Deliveries and receipts

- **Discovery deliveries** on a recipient mission (the elevated
  `discovery-in` posts that trip a recipient's Freshness Gate, ADR-0033) are
  a notice class of their own — `📨 Leads from KEY, step N` — with the
  findings visible in the body, the elevated marker and fingerprints in
  the fold, and the pointer to the source's record. Their materiality to
  the gate is unchanged; a person sees leads as leads.
- **Routing receipts** on the source never make a new entry: they are
  appended to the fold of the step card that harvested them (§3).
- **The deliverable archive** note and the packaging-failure note become
  a fold entry on the completion notice.
- **Description appends** (the closing handoff, the lineage note, the
  decomposition marker on children) are unchanged; they already read well
  and downstream prompts depend on them.

### 7 — Markers and sentinels

- No marker rides as an HTML comment. The reply marker and the deliverable
  marker retire in favour of backticked tokens in the fold
  (`devcake:answer:v1 step=N`, `devcake:deliverable:v1`). Any outside
  consumer that relied on the reply comment's `startswith` contract finds
  the answer as the card's blockquote and the token in the card; pre-v1,
  no compatibility shim is owed (ADR on the v1 evidence gate).
- The provenance sentinel is still appended by the single chokepoint to
  every DevCake post, inside the fold. Provenance classification stays
  content-based.
- The run id leaves every title; it is in the fold.
- Every existing scan (`discovery_posts`, `discovery_receipts`, the step
  file token, the merge markers, the freshness and conflict counters, the
  elevated marker) keeps reading `feed.unquoted(body)` over every DevCake
  entry; a fold is not a quote.

### 8 — The Dev folder as a projection

`ACTIVITY.md` is produced from the record by unfolding: a step card becomes
the entry sequence the folder carried before this ADR — the transcript
entry (header line, blockquoted answer, attachment marker), the answer
entry, the token report entry, the harvest entry — each under the same
`### {time} — {author} — 🤖 DevCake ({kind})` header, in the same order,
with the same bodies. A notice becomes the entry it was. Fold contents are
unfolded into their legacy entries; the collapsible wrapper never reaches
the folder. The status comment is **omitted**: it is a view whose every
fact is already in the folder, and a mutable entry would move the Dev's
reading watermark for nothing. Attachments, ancestor mirrors, banners,
provenance and the watermark are unchanged.

The contract is enforced by a **golden test**: a recorded feed in the
pre-ADR shape and the same mission's feed in the post-ADR shape render the
same `ACTIVITY.md` and `MISSION.md`, byte for byte, once timestamps and
vendor ids are normalised.

### 9 — Vendor mapping

| Primitive | Linear | GitHub / GitLab / Gitea Issues |
|---|---|---|
| Step card, notice, status comment | top-level comment | top-level comment |
| Fold | `+++ Title … +++` fence (capability `plus`; renders as a native toggle — verified) | `<details><summary>` (capability `details`) |
| Late bookkeeping | `edit_feed` on the card | `edit_feed` on the card |
| Status comment | `edit_feed` | `edit_feed` |
| Threads | available; not required | none; not needed |
| Long bodies | attachment (as today) | vendor cap paging (as today) |
| Hidden markers | none (HTML shown) | none (uniform rule) |

One structure, one voice, one vendor-specific line: the fold's syntax.

## Consequences

- A three-step mission reads as one status comment, three step cards and
  the notices that actually addressed a person; the record inside is
  complete.
- `PMOPort` gains `edit_feed(entry_id, markdown)` and the capability row
  gains `feed_collapsible` (the fold syntax family) — every adapter today
  supports both.
- The feed chokepoint gains a card renderer and a fold renderer; call
  sites stop composing bodies by hand and pass structured parts. The 51
  post sites of the inventory collapse into four shapes.
- The activity mirror gains an unfolding step and a golden test. The
  Dev-side contract (docs/07 §1–2) is restated as a projection contract.
- Editing own comments is a new kind of write; it is audited like any
  feed write and rate-governed like any PMO call.
- Verification before the first shipping PR: (a) Linear's collapsible
  markdown accepted through the API, read back with the fold's markers
  intact; (b) a marker inside a fold is found by every scan on every
  adapter's read path; (c) the golden test passes on a recorded field
  feed; (d) screenshots of one mission on Linear and on an issue vendor,
  before and after.

## Sequencing

1. Quick wins, independent of the redesign: drop the duplicate answer
   comment; cut at a sentence boundary; move the run id out of titles.
2. Port and adapters: `edit_feed`, `feed_collapsible`, the fold renderer,
   the Linear collapsible round-trip test.
3. The step card and the mirror unfolding, with the golden test.
4. Notices on one template; deliveries as the leads notice; receipts and
   late markers into folds.
5. The status comment.

## Related

- ADR-0014 (step-end contract, faithful mirror, one thread per step),
  ADR-0031 (feed as PMO truth, elevated markers), ADR-0032 (handoff note in
  the description), ADR-0033 (discovery routing, harvest and receipt
  arithmetic), ADR-0036 (ancestor mirrors).
- docs/03 §4–§8 (comment shapes, token report, threading, sentinel),
  docs/05 §1 (port surface), docs/07 §1–2 (activity folder).

## Addendum — a mission that cannot start is a state a person must see (2026-09-10)

**Context.** A decomposition child sat for eight days because its
original had been archived after the split: the ancestor walk could not
find it, strict context sourcing deferred the dispatch every cycle, and
nothing but a log line said so. The refusal was the doctrine working; the
silence was the defect. The founder's conditions: the ticket must carry it
for the human, the Dev's context must never carry it, and it must not be
spammed.

**Decision.** `stalls.py` clocks every dispatch the scheduler refused or
deferred for a reason a person can act on (a missing harness receipt,
upstream context missing, a repository or the board unreadable, a Dev Type
not ready). A *dependency* wait is never a stall. A block has an identity
(kind + subject) and belongs to an *episode*, which ends when the mission
dispatches or a person parks or closes it. Past a threshold
(`stall_after_seconds`, default thirty minutes):

- `/health.stalled_dispatches` lists the block with its age; the admin
  Overview raises a self-clearing alert and the board card carries a badge.
- ONE ✋ notice per identity per episode is posted — a new comment, so the
  ticket's subscribers are notified — and the mission's status comment is
  **created when none exists** (§5's rule gains "or by a stalled
  deferral") and edited so its Now line reads "waiting to start since
  <date> — <why>". The date does the aging; nothing is rewritten for the
  same block. A different block notifies again; the same block flickering
  within an episode writes nothing; a block that cleared without a dispatch
  and returns after a day counts as new; at most one write per mission per
  hour regardless.
- Both carry a marker the projection drops (`` `devcake:stall:v1` `` beside
  `` `devcake:status:v1` ``): they are about DevCake's inability to proceed,
  not about the mission's work, so they never enter ACTIVITY.md, the record
  or any upstream folder. Pinned by test.

**Consequences.** A stall is visible on the panel within a poll of the
threshold and on the ticket once. A project-kind mission is clocked and
shown on the panel but never written to (its feed is its children's
context). The ledger persists under `/data/state/stalls.json` so a restart
keeps the clocks.

