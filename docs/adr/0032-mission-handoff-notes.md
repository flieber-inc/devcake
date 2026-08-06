# ADR-0032 — Mission handoff notes: narrative flows with the graph

- **Status:** accepted (2026-08-06), implemented with ADR-0031 phase 1
- **Amends:** ADR-0017's prompt contract (the blocker note gains narrative),
  ADR-0014 D3 (MISSION.md gains a blocked-by/handoffs block),
  `append_description`'s lineage-note-only charter (ADR-0012), docs/03 §1
  ONBOARD (staleness check).
- **Context:** Live stress-testing on large decompositions showed missions
  are *narratively* isolated: DevCake shares upstream **code** perfectly
  (merged default branch + ADR-0017 blocker RO mounts) and upstream
  **narrative** not at all — a blocker contributes exactly
  `` `KEY` (`repo`) → /workspace/repo/… `` to downstream prompts: no title,
  no summary, no record of what was discovered or deferred. Mission
  descriptions freeze at decomposition time, so downstream Devs plan against
  a stale world. Rejected first (founder + assistant sparring, 2026-08-06):
  transitive `blocked_by` context inheritance — edges are permanent (no
  `remove_relation`, ADR-0012), transitive closure blows the 8-mount cap
  while adding almost no information the merged code doesn't carry, and it
  cannot reach cross-subtree missions at all. The scalable shape is a
  **chain of summaries**: each mission compresses what it inherited plus
  what it did into one small note; downstream reads only direct blockers'
  notes; information propagates transitively with per-hop compression.

  Governing doctrine (shared with ADR-0031): *the PMO carries every
  artifact that shapes a run's context; the app contributes only
  deterministic, documented selection rules over PMO content; per-run
  provenance lives on the Run record.*

## Decision 1 — the handoff is authored by REVIEW, on approve

The REVIEW result contract gains an optional `handoff_md` field, REQUIRED by
the playbook on approve: what changed, what was **discovered**, what
downstream work must know — compressing anything inherited from the
mission's own blockers instead of repeating it (the chain-of-summaries
hop). Structured field, not marker-scraping of the last message: the
result.json is the trusted channel and the entrypoint needs no change
(unknown keys pass through; app-side `LEGAL_OUTCOMES` is untouched).
A freshness re-review (ADR-0031) inherits the prior review's handoff and
amends only what the newer entries change — otherwise the missions with the
most mid-flight churn would get the thinnest notes from a delta-focused run.
Missing/empty `handoff_md` degrades to pre-ADR behavior, never fails a run.

## Decision 2 — it lives in the mission's DESCRIPTION (founder decision)

Appended at approve-finalize as a marked section: `---` +
`` `devcake:handoff:v1` `` + the note. Description, not a feed comment,
because (a) **zero extra PMO calls at dispatch** — `resolve_blocker_work`
already fetches every blocker Mission whole, description included, and
today throws the narrative away; (b) **immunity to feed truncation** — the
gitea adapter's hard stop drops the *newest* feed entries (ADR-0031's
correction), which is exactly where a close-time comment would sit; (c) the
`devcake-repo:`/decomposition-footer precedent: descriptions already carry
machine-readable markers. The trade accepted with eyes open: a description
append notifies no one — the feed remains where humans watch — and the
founder amends by *editing in place* rather than superseding by comment.
Parse rule: the **last** marker line wins (`markers.handoff_of`) — appends
accumulate across re-approves, and a human-authored marker line is a
feature (the description is operator-owned), not a forgery.

The append site is the TOP of the approve path, right after the Freshness
Gate: one site covers every eventual-done path (auto-merge now, deferred
sweep or human merge later) because the handoff is a property of the
*approve*, not of the merge mechanics. A rejected re-entry that later
re-approves simply appends again; last-wins absorbs it.

## Decision 3 — three write-time hardenings, none optional

`append_description` is a raw pass-through in both adapters — the only
model-output sink without its own redaction choke point — and the appended
text is re-injected into every downstream prompt and MISSION.md. Therefore,
in `review._append_handoff`:

1. **`redact()` first** — parity with `_feed` and result persistence.
2. **Backtick defang** — `` `devcake: `` and `` `devcake-repo: `` lose
   their opening backtick (the decomposition-child precedent,
   `decomposition.py`): description markers anchor last-match on the
   assumption nothing app-side appends live marker syntax after them; a
   handoff quoting a marker must never shadow a real one — including the
   handoff marker itself.
3. **Cap + best-effort** — `HANDOFF_APPEND_MAX` (4000) app-side (the
   entrypoint does not bound the field), and the append runs inside its
   checkpoint with the lineage-note try/except: a vendor description-cap
   failure is audited (`handoff_append_failed`) and the close proceeds — a
   missing note must never wedge an approved mission's finalize.

## Decision 4 — consumption: prompt note, MISSION.md, ONBOARD staleness

`resolve_blocker_work` returns a third list — `{mission_key, title,
handoff}` per **done** blocker — collected BEFORE the same-repo drop, the
mountability drop, the repo_ref dedup, and the 8-cap: narrative is
deliberately decoupled from the mount list, so a blocker whose repo
collapsed into another's (or overflowed the cap) still speaks. Excerpts are
bounded (`HANDOFF_EXCERPT_MAX`, 700). The prompt's blocker note renders a
`Handoff:` line under each mounted blocker plus note-only lines for
unmounted ones, and now states the staleness rule: *the mission description
predates the handoffs; where they conflict, the handoff is newer*.
MISSION.md gains a "Blocked by (completed — handoffs)" block on the
dispatch path (the Redis fallback rebuild has no resolved blockers and
omits it — the prompt note still carries the content). The ONBOARD playbook
gains the explicit staleness check: reconcile the description against
blocker handoffs before classifying, and name the drift in the summary.
`Run.blocker_work` stays mount-only — the notes are a deterministic
function of PMO state, so the receipt is unchanged.

## Consequences

- A three-mission chain now hands narrative forward at each hop for the
  cost of one short model-authored paragraph per mission — no new PMO
  calls, no new storage, no new run types.
- Pre-ADR missions have no handoff: their blockers render exactly as
  today. Operator prompt-template overrides that omit the `handoff_md`
  requirement degrade the same way — documented, not failure.
- The handoff's quality ceiling is REVIEW's view of the work (it reads the
  full EXECUTE transcript in its mirror, but did not live the discoveries);
  if live use shows thin notes, the escalation is an EXECUTE-contributed
  draft the REVIEW curates — deliberately NOT built now.
- Cross-subtree missions (no `blocked_by` path) still get nothing — by
  design; that is discovery routing's lane (deferred, see the campaign
  plan), for which the elevated-marker seam already exists (ADR-0031).
- Known accepted risks: the description grows by ≤ ~4 KB per approve
  episode; a human editing the *middle* of an appended note can widen it
  past the excerpt cap at read time (excerpting re-caps); Linear renders
  the marker line as literal code — cosmetic.

## Related

- Implement: `domain/orchestrator/markers.py` (`HANDOFF_MARKER`,
  `handoff_of`, caps), `review.py` (`_append_handoff`), `dispatch.py`
  (`resolve_blocker_work` third return, `_blocker_repos_note`),
  `activity_payload.py` (MISSION.md block, plumbing), `prompts/__init__.py`
  + `prompts/customer_success.py` (contracts + staleness), tests in
  `app/tests/test_handoff_notes.py`.
- Doctrine: ADR-0003/INV-1, ADR-0012 (edge permanence; lineage-note
  best-effort pattern), ADR-0014 (MISSION.md), ADR-0017 (blocker
  contract), ADR-0031 (gate ordering; elevated-marker seam; the shared
  PMO-truth doctrine sentence).
