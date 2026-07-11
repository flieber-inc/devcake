# ADR-0005 — No-Lock Atomicity via PMO State

**Status:** accepted (foundational).

## Context

Dev work must be atomistic; runs may break at any point. The mission doc forbids code locks/checkouts ("a broken Dev could hold this lock indefinitely") and requires that "new Dev work can only begin once the PMO System reflects the previous changes."

## Decision

The only synchronization primitive is the PMO System's own state. A step's effects are applied by the app at finalization in a fixed order — transcript, token report, then **compare-and-transition** (re-read the mission live; apply the label swap only if the stage label still matches what the run started from; otherwise abort with an explanatory comment). Until the swap lands, the mission still derives as its old type; the in-flight Run guard and a one-cycle grace period keep it out of scheduling. Every finalization side effect is individually idempotent and checkpointed (`finalized_steps`), so crashed finalizations resume. Full protocol: `04-orchestrator.md` §§3–4.

## Alternatives considered

- **Redis-based locks with TTL** — reintroduces the stuck-lock problem in TTL-tuning disguise, plus split-brain when a "dead" Dev revives after its lock expires.
- **Dagu-level serialization** — puts business logic in the executor and doesn't survive an app-side view of the world diverging from Dagu's.
- **Optimistic writes without compare** — a human's mid-run status change would be silently overwritten, violating the human's authority over the source of truth.

## Consequences

A crashed Dev holds nothing; its Mission reschedules naturally (INV-3). Human edits always win over in-flight runs (`EXTERNAL_TRANSITION` is a first-class non-error outcome). The cost is extra live PMO reads at dispatch and finalization — negligible against Dev-run durations.
