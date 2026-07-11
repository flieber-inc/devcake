# ADR-0006 — Linear Projects Always Take the Decompose Path

**Status:** accepted (v0). Confirmed with the founder 2026-07-11 (including the auto-complete behavior).

## Context

Missions can be Linear Projects or Issues. Projects verified to support labels, statuses (5 fixed categories), and priorities — so the label state machine *could* run on them directly. But a Project is by definition a container for multiple work items; running PLAN/EXECUTE/REVIEW on a whole Project in one Dev pass contradicts the sizing the pipeline assumes. The mission doc itself says a high-complexity Project "must create its corresponding Issues."

## Decision

At ONBOARD, a Project always takes the high-complexity path: decompose into standalone child Issues created **inside the Project** (each `DEVCAKE-CREATED`, explicit priority). The Project is not canceled — it receives `DEVCAKE-TRACKING` and stays open as the natural tracking container; the poll loop **auto-completes it (status Done, label removed) once all child Issues reach Done/Canceled**. Trivial and normal ONBOARD verdicts are illegal for Projects.

## Alternatives considered

- **Cancel the Project after decomposition** (literal reading of the Issue behavior) — misleading: the Project legitimately groups its own children; canceling it hides live work.
- **Run the full state machine on Projects** — one PLAN/EXECUTE/REVIEW cycle per Project is the wrong granularity and produces monolithic PRs.
- **Leave the Project open, never complete it** — leaks human bookkeeping that DevCake has all the data to do itself.

## Consequences

Project completion is derived entirely from PMO state (children's statuses) — no local tracking, consistent with ADR-0003. The sweep runs every poll cycle (`04-orchestrator.md` §1.3). A Project with zero children never auto-completes (guard: ≥1 child required).
