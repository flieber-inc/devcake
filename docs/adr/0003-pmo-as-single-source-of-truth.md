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

Every scheduling and finalization decision pays a live PMO read (rate limits are comfortable, `05-pmo-adapter.md` §2). Attempt/loop counters are legally resettable — a documented, accepted trade (`10-persistence.md` §5). Compare-and-transition (`04-orchestrator.md` §4) plus the grace cycle absorb read-after-write staleness.
