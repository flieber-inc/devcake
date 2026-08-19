# ADR-0036 — Offer every decomposition ancestor's activity to dispatched Devs

- **Status:** accepted (2026-08-19)
- **Amends:** ADR-0014 (activity payload / activity repos), cross-references
  ADR-0012 (decomposition-depth bound) and ADR-0017 (blocker-mount contract)
- **Ticket:** CAKE-124

## Context

During the 2026-08 hardening rounds, missions that were decompositions of
decompositions (grandchildren in the mission graph) did not inherit the
context of their grandparent. Playbooks pointed Devs at attachments that had
been delivered as *parent* mission context — but each run only built/cloned
its own `activity-*` repo. Context inheritance was effectively one generation
deep, so grandchild Devs worked from reconstructed scope.

Founder ruling (2026-08-19, verbatim intent):

> Adequate correction is to offer the **activity repository of all upstream
> Missions of a given graph** to a dispatched Dev.

Depth is already bounded by the decomposition-depth limit (ADR-0012), so the
ancestor chain is finite by construction.

An operator hotfix published the review ledger as a domain-knowledge skill in
the skill store. That violated the skills philosophy (`app/devcake/skills/
README.md`): skills are domain modules, not mission scripts. This ADR's
mechanism retires that workaround.

## Decision

### 1 — Directed `parent_ref` chain, not undirected `family_of`

Ancestry is the directed walk of `markers.decomposition_parent_ref` from the
dispatched mission toward the graph root (nearest parent first). The walk is
cycle-safe and stops at an unresolvable parent. It does **not** reuse
`family_graph.family_of`, whose undirected union of decomposition-parent and
`blocked_by` edges would also pull siblings, cousins, and blockers.

Public seam: `family_graph.decomposition_ancestors(mission, missions)`.

### 2 — Delivery shape (a): payload `upstream/{MISSION-KEY}/`

Extend `activity_payload` (ADR-0014 D3/D4) so the Dev's `/workspace/activity/`
gains an `upstream/{MISSION-KEY}/` subtree per ancestor — each subtree mirrors
that ancestor's `MISSION.md`, `ACTIVITY.md`, and attachments under the existing
per-file / total byte caps (`MissionManager._attachment_cap`). The same layout
rides the activity-repo snapshot push and the Redis `activity.get` fallback.

**Rejected — (b) provision-step RO clones of ancestor `activity-*` repos:** full
fidelity at repo scale, but requires DAG/runspec/`extra_repos` surface changes
and a second clone contract beside ADR-0014's single activity clone. Caps and
quoting-quarantine already live in the payload builder; (a) reuses them.

### 3 — `blocked_by` stays on ADR-0017

Direct done-blockers' **work-repo** RO mounts (ADR-0017) remain a separate
contract. This ADR does **not** mirror blocker activity feeds into
`upstream/`, and does not make `blocked_by` edges participate in the ancestor
walk. Widening blocker-transitive *activity* context would be a future ADR.

### 4 — Strictness and honest truncation

- **`context_sourcing_strict` on:** an unreadable ancestor activity source is a
  fail-closed dispatch gate (provisioning family — no container, no attempt
  burned). Reason: `upstream activity unavailable — dispatch deferred: …`.
- **Strict off:** dispatch proceeds; ACTIVITY.md carries an explicit
  `⚠ UPSTREAM GAP` banner. Never silent.
- **Caps:** pack ancestors nearest-parent-first into a total upstream budget
  equal to `_attachment_cap()`. Over-budget ancestors truncate
  **oldest-first** (root-ward); the banner names the truncated (oldest) end
  truthfully (`⚠ UPSTREAM TRUNCATED — oldest ancestors omitted…`).

### 5 — Hotfix skill retirement

The operator-published `review-ledger` skill is out of band for the bundled
seed (`app/devcake/skills/` catalog is the five built-ins only). Shipping this
mechanism includes (a) a regression test that the bundled seed never
reintroduces `review-ledger`, and (b) deleting the live skill-store copy /
deselection from Dev Types where the operator still has it selected.

## Consequences

- Grandchild Devs see grandparent (and all further ancestors') activity under
  `upstream/{KEY}/` without reconstructing scope from playbook prose.
- Playbooks (docs/03) and the docs/07 §2 workspace tree document the layout.
- Activity payload builders and the dispatch strict gate share one chokepoint
  (`activity_payload` → `upstream_gaps`).
- No new config field; reuses `context_sourcing_strict` and attachment caps.
