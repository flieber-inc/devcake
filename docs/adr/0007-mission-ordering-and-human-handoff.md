# ADR-0007 — Mission Ordering via Native PMO Relations + the Human Hand-off

**Status:** accepted (post-v0 traffic-control increment). Confirmed with the founder 2026-07-12.

## Context

v0 assumed every schedulable Mission was unblocked and fully parallelizable. A real refactor exposed the flaw: ONBOARD decomposed it into "write the new documentation" and "do the coding," and both children dispatched concurrently — the coding had to wait for the docs. Separately, Devs sometimes hit obstacles only a human can clear (a GitHub token missing a scope, an external account decision) with no way to pass the baton; and once humans are actively steering, they need to pause intake while rearranging Linear, and Devs need to distinguish human comments from DevCake's own — which credentials cannot do, since DevCake may post with the operator's own Linear API key.

## Decision

Five coupled mechanisms, all live-derivable from PMO state (ADR-0003/0005
preserved — no persistent per-Mission lease and no local Mission authority):

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

## Addendum — hardening decisions (2026-07-12, post-adversarial-review)

Shipped as the immediate follow-up increment, after live verification (relation direction, idempotent duplicate creates, complexity 1,310/3M-hr, sentinel roundtrip, `projectUpdateCreate`) and an adversarial review of the original commit:

1. **Outcome legality is an app-side invariant** (`LEGAL_OUTCOMES`, docs/03 §6): illegal/forged outcomes park with `DEVCAKE-SKIP`, never act. The entrypoint mirrors the table but the app check is authoritative. Structurally invalid payloads behind legal outcomes fail as `DEV_BAD_OUTPUT` (counted attempt) instead of poisoning the ingress.
2. **The Dev's own forge token is contained by branch protection, not scoping** — push-branch and merge are the same `contents: write`; docs/13 §8a makes protection a deployment requirement, DevCake verifies and surfaces the state, and an out-of-pipeline-merge tripwire detects violations (docs/14 §2).
3. **All Linear list reads paginate; `inverseRelations` reads 50 with a full-page WARNING** — silent truncation of the gate's inputs (or the mapper's validation graph) is never acceptable.
4. **The gate is a poll artifact** (`gate_map`), computed even while paused, with **dependency-cycle detection** (`pmo.find_cycles`) naming unsatisfiable waits explicitly — an undetected routing deadlock is the one failure a traffic-routing product cannot afford.
5. **Hand-off guardrail: evidence required, warnings only** — escalating "Hand-off #N" headers from the 2nd repeat; never auto-park (founder decision: the human always decides).
6. **The Mapper runs on the seeded `mapper`** (claude-code, `claude-haiku-4-5`) by default, manual-only out of the box; `MapperService` serializes manual/periodic dispatch, advances its watermark only on success, and backs off after 3 consecutive dead runs (store-derived). The repo clone is kept (founder decision: preserves future code-aware ordering).
7. **Blocking stays pipeline-coarse** (founder decision): better bottlenecked than accumulating parallel garbage — routing quality is the product thesis (docs/04 §2).
8. Provenance classification ignores `>`-quoted lines (a human quoting DevCake stays human); project-kind baton passes go out as project updates.
