# ADR-0031 — The Freshness Gate: no context-closing transition on an unread feed

- **Status:** accepted (2026-08-06); **phase 1 implemented** (REVIEW finalize +
  sweep disclosure); **phase 2 pending** (decomposition cancel gate — specified
  in Decision 5, not yet shipped)
- **Amends (on implementation):** `docs/03-mission-lifecycle.md` (new §
  beside §4.1's conflict routing; §8a cross-reference), `docs/04-orchestrator.md`
  (finalize_review), `docs/15-errors-and-retries.md` §2 (a second
  not-a-retry carve-out beside ADR-0022's).
- **Context:** A run's view of the PMO is assembled ONCE, at dispatch —
  the ACTIVITY.md mirror, blocker notes, and prompt are frozen at that
  moment. Anything posted to the feed after dispatch is invisible to the
  running Dev. The multiphase pipeline normally absorbs this: ONBOARD,
  PLAN, EXECUTE and REVIEW are separate dispatches, so material that
  lands mid-run is read at the next phase's context assembly. But that
  argument has a hole at the end of the pipeline: a positive-verdict
  REVIEW finalize closes the mission, and there is no next dispatch.
  Material posted in the window [REVIEW dispatch, finalize] — a human
  steering comment, a routed cross-mission note — is evaluated by no
  one, ever, while the verdict that ignored it becomes the mission's
  final word. The same race exists at every transition that removes all
  future read points, not just REVIEW (decomposition finalization
  cancels the original mission with the identical effect on anything
  posted mid-ONBOARD). Founder decision (2026-08-06): fix this as a
  general rule, named the **Freshness Gate**, designed as finalize-time
  gating plus a capped re-review loop — an earlier keep-the-container-
  alive proposal was considered and rejected (see Rejected below).

  Governing principle, restated from the same discussion: *the PMO
  carries every artifact that shapes a run's context; the app
  contributes only deterministic, documented selection rules over PMO
  content; per-run provenance of what was actually read lives on the
  Run record.* The gate is that principle applied to run **endings**:
  a verdict may only close a mission if its context was complete at
  finalize time.

## Decision 1 — the rule, and the inventory it governs

**No context-closing transition may be applied while material feed
entries exist that no run's context has included.** A transition is
*context-closing* when, after it, no future dispatch will ever read the
mission's feed. The known inventory, with dispositions:

| Transition | Context-closing? | Disposition |
|---|---|---|
| REVIEW approve → merge → status done (`review:done` / `_done_merged`) | yes | **gated (phase 1, this ADR)** |
| Decomposition finalization → cancel of the original | yes | **gated (phase 2)** — see Decision 5 |
| Deferred-merge sweep → merge → status done (`merge_sweep`, retry window) | yes, app-driven | **disclose-only (phase 1)**: the finalize-time gate passed minutes-to-hours earlier and the merge was operator-sanctioned; the sweep runs the pure material check against the newest finished REVIEW run's watermark and, on a hit, still closes but posts the ⚠ unread-material comment + audit. No new state-machine arm |
| REVIEW approve → `DEVCAKE-MERGE` (human merges) → done | yes, but human-attended | exempt: a person is at the merge button with the live feed in front of them; gating their close on comments they can see adds ceremony, not safety |
| `tracking_sweep` → project parent done (all children terminal) | yes | exempt, documented gap: no run ever reads project-update feeds, so there is no run context to be stale — a human project update posted mid-TRACKING is machine-read by nothing today. Classified, not omitted; revisit with discovery routing |
| REVIEW reject → EXECUTE | no | next EXECUTE dispatch re-reads |
| Operator cancel / `DEVCAKE-SKIP` | yes, human-originated | exempt: the operator is the authority the gate escalates *to*; gating their explicit action on their own unread comments is circular |
| `DEVCAKE-FAILED` terminal | yes | exempt: failure is already an operator surface — the red card is the escalation |

The inventory is part of the doctrine: any future transition that
closes a mission's read points MUST be classified against this table in
its own design, so the decomposition path (and whatever comes after it)
gets the gate deliberately rather than by later accident.

## Decision 2 — the watermark is the reading receipt

At context assembly, dispatch snapshots `feed_watermark` on the Run —
the adapter-native id (plus timestamp, display-only) of the newest feed
entry included in the mirror. This extends the existing dispatch-
snapshot pattern (`Run.blocker_work`, `Run.mirror_repos`): non-secret,
persisted, the run's receipt for what it read. The gate check is then a
pure function of PMO content plus that receipt: fetch the feed **full**
(shallow entries carry no ids in either adapter) and take every entry
after the watermark id as *new*. Truncation direction is
**adapter-specific and cannot be assumed benign**: Linear walks
newest-first (its hard stop eats the oldest end), but gitea_issues pages
*ascending* and drops the **newest** entries at its ceiling — exactly
the ones the gate exists to catch. Rule: `Activity.truncated is True` ⇒
material-UNKNOWN ⇒ the gate **trips** (fail loud, never pass).

Degradations, stated honestly: a legacy Run without the field falls
back to entry-timestamp > dispatch-time (clock-skew-tolerant only to
the extent the adapters' server clocks are; acceptable for a fallback
that ages out). A watermark entry deleted by a human is never found in
the walk, so everything looks new and the gate trips — self-limiting at
the loop cap, and consistent with the conflict-counter doctrine that
deliberate comment deletion resets marker-derived state (docs/03 §4.1).

## Decision 3 — "material" is defined to make the loop terminate

A new entry is **material** iff its unquoted body (IRON RULE —
`feed._unquoted`, markers may never be honored inside quotes) is
non-empty AND either:

- carries **no** `devcake:v1` sentinel — i.e. 🧑 HUMAN provenance per
  docs/03 §8a (steering posts bypass the sentinel by design), or
- carries the sentinel but matches a marker class in the **elevated
  registry** — a named allowlist in `markers.py`, shipped EMPTY. Future
  cross-mission delivery (the routed `DISCOVERY-IN` class from the
  2026-08-06 context-flow design) joins the registry when it ships;
  nothing is elevated implicitly.

Everything else the app posts — step markers, merge notes, replies,
deliverable notes, and the gate's own trip directive — is sentinel'd
bookkeeping and immaterial **by construction**. That is the livelock
guarantee: finalize-time posts cannot re-trigger the gate that just ran.
Label/status events and other bodyless entries are excluded by the
non-empty-body requirement; a human label flip is state, not context.

## Decision 4 — on trip: a freshness re-review, never a held container

When the gate finds material, the terminal transition is **withheld**
and the mission is routed through the existing directive pattern
(docs/03 §4.1's conflict-resolve is the template, same file, same
shape):

1. Post a short sentinel'd directive comment carrying a counted marker
   — `devcake:freshness-rereview:N` — naming the prior verdict, linking
   the prior report, and instructing the next REVIEW: *material arrived
   after this review's context was assembled; evaluate ONLY whether the
   new entries change the verdict/results.json.* Directive FIRST, then
   no transition — mirroring the conflict path's ordering so a failed
   post can never under-count.
2. The stage label is untouched, so the next poll re-dispatches REVIEW
   through ordinary machinery: fresh mirror (which now contains the new
   material, the directive, AND the prior REVIEW's transcript via the
   activity payload — that is where incrementality comes from), fresh
   watermark (closing the sub-race for the next landing).
3. `MAX_FRESHNESS_REREVIEWS = 2`, counted from the feed marker like
   `MAX_CONFLICT_RESOLVES` — PMO-derivable, restart-proof, no local
   counter. A freshness re-review is **not a failure retry**: it must
   not consume `attempt_of_step` budget nor feed ADR-0026's brakes
   (docs/15 §2 gains the carve-out beside ADR-0022's — the run being
   re-run did not fail; its context did).
4. **Exhaustion:** finalize proceeds on the standing verdict, plus a
   sentinel'd ⚠ feed comment naming the unevaluated entries (and the
   mission's cumulative recorded cost — re-reviews are otherwise
   invisible to every loop/cost warning surface), an audit event, and a
   best-effort `mgr.anomalies` entry. The **durable** operator record is
   the ⚠ comment + audit event: the anomaly entry is transient by
   construction — the poll loop prunes anomalies for done missions on
   the next cycle — and the ADR says so rather than pretending
   otherwise. The budget is **per mission lifetime** (max marker over
   the feed, conflict-count doctrine); the per-episode alternative
   (anchor the count at the newest prior REVIEW transcript marker) is
   the documented escalation if live data bites. Holding the mission
   open instead was rejected: a chatty comment stream could hold a
   verdict hostage indefinitely; the operator flag keeps the human in
   authority, which is where exhaustion belongs.

## Decision 5 — phasing

**Phase 1 (this ADR's implementation): REVIEW.** The gate check runs at
the top of the approve path in `finalize_review`, before the
`review:done` checkpoint family, so neither the status flip nor
`deliver_internal_zip` can precede it. Checkpoint/redelivery semantics
are unchanged — a redelivered finalize whose `review:done` already ran
is past the gate by definition.

**Phase 2: decomposition finalization.** The rule applies; the
mechanism differs because there is no run to re-dispatch (children
already exist). The coherent trip behavior falls out of ADR-0012's
family gate: withhold the cancel of the original, and the
`DEVCAKE-CREATED` children stay withheld automatically (the family gate
holds children while the marker-parent is open) — the whole family
pauses in a consistent state for the operator, surfaced via the same
anomaly path. Specified here so phase 2 is an implementation, not a
design; shipped separately.

## Rejected alternatives

- **Keep the REVIEW container running until "no additional material" is
  confirmed.** No principled stop condition — material can land one
  second after any check, so the hold only moves the race boundary
  while charging container-hours to every REVIEW for a rare event. It
  also requires a mid-run control channel into a deliberately
  fire-and-observe container (runspec at start, stdin closed, outcome
  by exit code) and makes run completion depend on app liveness — a new
  hang/timeout class. The finalize-time check covers the entire actual
  race window [dispatch, finalize] in one bounded feed read.
- **Cross-container session resume for the re-review.** ADR-0022's
  three operations all keep the container; harness session state dies
  with it. Post-exit resume would need session persistence outside the
  container — machinery bought for nothing, since the activity mirror
  already hands the next REVIEW the prior transcript and report.
- **A post-finalize grace period** ("the human might be mid-typing").
  Any window is arbitrary and still loses the race; the post-close
  recourse is the existing steering/reopen path, and the Done tier of
  cross-mission delivery is follow-up missions by design.
- **Gating every finalize.** Non-closing transitions re-read the feed
  at the next dispatch for free; gating them buys nothing and doubles
  feed traffic.

## Consequences

- The gate costs one `get_activity` per gated finalize — a fetch the
  review path already performs for marker counting — and a re-dispatch
  ONLY when material actually arrived, which live history says is rare.
  Zero idle container time; zero new channels.
- A human comment posted during REVIEW is now guaranteed either a
  reviewer's eyes or an explicit ⚠ "closed with unread material" flag —
  never silence. This holds for future routed discoveries the moment
  their marker class joins the elevated registry, with no further gate
  changes.
- `Run` gains `feed_watermark` (non-secret, empty on legacy records →
  timestamp fallback). Runs API/detail surface it like other dispatch
  snapshots.
- The re-review loop is PMO-derivable end to end (directive markers,
  like conflict-resolve): restarts lose nothing, and a human deleting
  the directive comments deliberately resets the count.
- Known blind spots, accepted: an *edited* pre-watermark entry is not
  detected (edits carry no new entry id — v1 watches arrivals only);
  the human-attended `DEVCAKE-MERGE` close and operator cancels are
  exempt by doctrine, not oversight.
- The `08` §1 caveat pattern applies: `MAX_FRESHNESS_REREVIEWS` is a
  constant beside `MAX_CONFLICT_RESOLVES`, not operator config, until
  someone demonstrates a need — knobs are debt too.

## Related

- Implement: `domain/orchestrator/review.py` (`finalize_review` gate +
  trip directive; conflict-resolve as the in-file template),
  `domain/orchestrator/dispatch.py` (watermark snapshot at context
  assembly), `domain/run.py` (`feed_watermark`), `domain/orchestrator/
  markers.py` (`FRESHNESS_MARKER`, the elevated registry),
  `domain/orchestrator/decomposition.py` (phase 2),
  `api/runs_service.py` (surfacing).
- Doctrine: ADR-0003/INV-1 (PMO as single source of truth — the gate is
  a deterministic rule over PMO content), ADR-0007/docs/03 §8a
  (sentinel provenance), ADR-0012 (family gate; phase-2 pause
  coherence), ADR-0022 (not-a-retry doctrine; the in-container boundary
  that forces re-dispatch over resume), ADR-0026 (brakes the re-review
  must never feed).
- Origin: 2026-08-06 founder/assistant design session — the context-
  flow campaign (PMO-based HANDOFF, in-band DISCOVERY, routed
  cross-mission delivery); the gate closes that campaign's last-read-
  point race and stands on its own regardless of when the rest ships.
