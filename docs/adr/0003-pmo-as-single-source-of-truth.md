# ADR-0003 — The PMO System Is the Single Source of Truth

**Status:** accepted (foundational; unlikely to ever change).

## Context

Dev runs break for a multitude of reasons; any locally-held mission state would drift or dangle across crashes. The mission doc mandates: "no local information is deemed current; every information on the status of Missions must always come from the external PMO System."

## Decision

All Mission status, labels, and priority are read live from the PMO System (INV-1). Mission Type is a pure function of live PMO state (`02-domain-model.md` §2). Local files are advisory telemetry: wiping `/data/state` resets attempt counters and history but corrupts nothing. Anything that must survive a local wipe is pushed *into* the PMO as labels or comments (`DEVCAKE-FAILED`, loop warnings, transcripts).

## Alternatives considered

- **Local mission database with PMO sync** — the classic two-source-of-truth trap: every crash becomes a reconciliation problem; every human edit in Linear becomes a conflict.
- **Distributed locks/leases per mission** — a broken Dev can hold a lock indefinitely (explicitly forbidden by the mission doc); leases need clocks and renewal machinery that PMO-state-as-lock gets for free.

## Consequences

Every scheduling and finalization decision pays a live PMO read. Attempt/loop counters are legally resettable — a documented, accepted trade (`10-persistence.md` §5). Compare-and-transition (`04-orchestrator.md` §4) plus the grace cycle absorb read-after-write staleness.

**Amendment — enumeration reuses the cycle's fetch (with ADR-0040).** Vendor quotas are metered and no longer assumed comfortable, so the live-read rule is stated precisely: a decision that **mutates** a mission on the strength of that mission's state — dispatch, transition, completion, decomposition, edge writes — pays a live read of that mission. **Enumeration within one poll cycle** — listing a project's children, a mission's ancestors, or the tickets a scheduled task has in flight, to decide whether a live read is even due — reuses the cycle's `list_all` snapshot (`BoardSnapshot`, `04-orchestrator.md` §1). Staleness there can only delay a decision by one cycle, never wrongly commit one: the snapshot is immutable for the cycle, no reader decides on a field an own write changed within the cycle (the one enumeration that could — a scheduled task's single-flight check, which runs after the sweeps' completions — confirms a busy answer with a live read; an own create retires the snapshot outright), and the next fetch reconciles. The board stays the single source of truth; the snapshot is that truth as of this cycle.
