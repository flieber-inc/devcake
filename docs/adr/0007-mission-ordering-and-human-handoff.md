# ADR-0007 — Mission Ordering via Native PMO Relations + the Human Hand-off

**Status:** accepted (post-v0 traffic-control increment). Confirmed with the founder 2026-07-12.

## Context

v0 assumed every schedulable Mission was unblocked and fully parallelizable. A real refactor exposed the flaw: ONBOARD decomposed it into "write the new documentation" and "do the coding," and both children dispatched concurrently — the coding had to wait for the docs. Separately, Devs sometimes hit obstacles only a human can clear (a GitHub token missing a scope, an external account decision) with no way to pass the baton; and once humans are actively steering, they need to pause intake while rearranging Linear, and Devs need to distinguish human comments from DevCake's own — which credentials cannot do, since DevCake may post with the operator's own Linear API key.

## Decision

Five coupled mechanisms, all live-derivable from PMO state (ADR-0003/0005 preserved — no locks, no local authority):

1. **Ordering = native Linear `blocked by` issue relations.** `Mission.blocked_by` is read from `inverseRelations`; the scheduler skips any Mission with a blocker not `done`/`canceled` (re-verified live at dispatch; unreadable blocker ⇒ blocked, fail-safe). It is a **scheduler gate, not a derivation row** — `derive()` stays a pure single-Mission function. ONBOARD's decomposition declares `blocked_by` as 1-based indexes of *earlier* siblings (structurally acyclic; app-validated); the app creates the relations. Because the gate honors *any* relation, humans steer ordering directly in Linear's UI.
2. **Human hand-off = `DEVCAKE-NEEDS-HUMAN` (the tenth label) + the `human_needed` outcome.** A clean, deliberate hand-off: the run finishes, never counts toward `max_attempts`, the stage label stays, and an ONBOARD hand-off restores `backlog`. Removing the label resumes at the same step.
3. **Intake pause (`intake_paused`).** One config bool gating only the dispatch step of the poll cycle; sweeps, finalization, and the watchdog keep running.
4. **Comment provenance = the `` `devcake:v1` `` sentinel.** Every app-posted comment ends with the footer (single choke-point `_feed`); `ACTIVITY.md` classifies entries 🧑 HUMAN / 🤖 DevCake by content, never by author, and playbooks make human comments authoritative.
5. **Relations Mapper.** A team-scoped `MAPPER` run kind (interval service + manual trigger, admin-configured Dev Type) that proposes missing edges across existing open Missions; the app validates (unknown/self/terminal/duplicate/cycle ⇒ dropped) and applies survivors as native relations with a signed notification comment.

## Alternatives considered

- **`DEVCAKE-BLOCKED` label for ordering** — a label cannot reference the blocking issue, so the actual dependency would need a second, hidden representation that can drift; Linear already renders "Blocked by ENG-42" natively, which is better human UX. Rejected (a cosmetic derived label was also rejected: extra writes each cycle, drift risk).
- **Description-embedded dependency metadata** — invisible in list views, fragile under human edits. Rejected.
- **Dagu-level serialization** — Dagu has no cross-run concurrency semantics here (unique `dagRunId` per run, retry 0); ordering must live in the scheduler. Rejected.
- **Author-based comment provenance** — broken by design under shared credentials; Linear `botActor` fields don't survive the operator-key case either. Rejected.
- **ONBOARD scanning pre-existing team missions for blockers inline** — deferred; sibling ordering covers the observed failure, human-added relations cover the rest immediately, and the Mapper service covers it systematically.

## Consequences

- A blocker carrying `DEVCAKE-FAILED`/`DEVCAKE-SKIP` parks its dependents indefinitely — correct (the prerequisite won't complete autonomously) and surfaced in `/api/v1/missions` reason strings; recovery is human (fix the blocker or delete the relation). Human-created cycles park all members, visibly.
- `derive()` purity and the compare-and-transition model are untouched; every new behavior is re-derivable from live PMO state.
- Dev images must be rebuilt with the app (the entrypoint's legal-outcome list gains `human_needed`/`relations_mapped`); an old image rejects them as exit 11 — fail-safe, but a counted attempt.
- Comments posted before the sentinel convention classify as human — harmless noise on pre-migration Missions.
- The Mapper may re-propose an edge a human deleted (it only knows current relations); the persistent notification comment gives context, and the service can be disabled. Remembering deleted edges is deferred.
