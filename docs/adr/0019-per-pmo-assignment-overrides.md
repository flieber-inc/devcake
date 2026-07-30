# ADR-0019 — Per-PMO assignment overrides (dual-crew staffing)

- **Status:** accepted (2026-07-28)
- **Context:** One deployment runs N PMO instances (ADR-0009) — for example a
  Customer Success board and a Development board — but `AppConfig.assignments`
  was a single global Mission-Type → Dev-Type map, so every instance staffed
  every mission type identically. A CS EXECUTE and an eng EXECUTE always got
  the same vehicle. That defeats the point of multi-PMO for genuinely
  different domains: the boards are separate, the crews were not. The
  cross-instance blocker plan (2026-07-28) explicitly deferred this as the
  "assignment key" design; the founder requested it the same day the blocker
  work shipped.

## Decision

### 1 — Overrides live on the instance, resolution lives in one function

`PMOInstance.assignments: dict[MissionType, Assignment]` (default `{}`) holds
per-instance override rows. `assignment_for(config, instance, mission_type)`
(`config.py`) is the ONLY resolver: the instance's row **wholesale** when the
key is present, else the global row. Both orchestrator read sites — Dev-Type
choice at schedule time and `extra_cli_args` at dispatch time
(`schedule.py`, `dispatch.py`) — go through it, so the type chosen and the
args delivered always come from the same row.

**Wholesale, never merged:** an override carries `dev_type` AND
`extra_cli_args` together. CLI args are harness-specific (docs/08 §1); an
override must never inherit the global row's args written for a different
harness. A fresh override therefore starts with empty args.

**Presence = override:** an absent key inherits the global row *live* — a
later global edit applies to inheriting instances immediately. An override
row with an empty `dev_type` is refused at validation ("remove the key to
inherit"); unknown mission-type keys are refused loudly (a typo would
otherwise be silently inert).

### 2 — Placement on `PMOInstance`, not a parallel map

Overrides ride the `pmos` list: deleting an instance deletes its overrides
(config lists replace wholesale — docs/10 §3), profiles/export/import carry
them for free (ADR-0013 serializes the whole config), and no
instance-name-keyed sibling structure can drift against the instance list.

### 3 — Reference hygiene matches the global map

- Dev Type **DELETE** 409s while any instance override names it (the error
  names the holding instances).
- Dev Type **RENAME** remaps override rows in place.
- Bundle/profile **apply** (`check_assignments`) scans instance overlays as
  well as the global map — a bundle whose override names a Dev Type it does
  not carry is refused before anything persists.
- At **schedule time** an override naming a vanished Dev Type behaves exactly
  like an unassigned global row: skip with reason, never crash (docs/15
  fail-safe posture). PUT `/config` deliberately does NOT validate dev-type
  existence (same split as the global map: the SPA may save config before a
  new Dev Type lands; PUT `/assignments` and apply own that check).

### 4 — Admin UI

The Assignments section keeps the global table ("global defaults") and adds
one override block per configured PMO card: a tri-state select per mission
type whose inherit option names the effective global Dev Type, an args input
only when overridden, and the review-independence (EXECUTE ≠ REVIEW) and
harness-mismatch advisories evaluated per instance on **effective** rows.
The draft seeds `assignments: {}` on every PMO card at load so diffs stay
per-row and removing the last override round-trips to a clean draft.

## Consequences

- Dual-crew staffing works today: the CS instance can route EXECUTE to a
  CS-shaped Dev Type while eng keeps the implementer — one deployment, one
  control plane, different crews (the ADR-0009 operator story completed).
- The 1-Mission-Type → 1-Dev-Type rule now holds **per instance** rather than
  per deployment; docs/02 §9's global-map description gains the override
  clause.
- Priority-conditional assignment (docs/16 backlog) remains out — this ADR
  adds an instance dimension, not a condition language.
- Two instances sharing one board domain but needing different staffing no
  longer motivates a second deployment — the remaining single-global knobs
  (adoption mode, concurrency) are listed in docs/16 as open per-instance
  candidates, deliberately unbundled from this change. **`auto_merge` moved
  to per-repo doctrine** (ADR-0020), not per-PMO.
