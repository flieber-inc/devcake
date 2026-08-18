"""Scheduling and blocked-by gate (docs/04 §§2–3)."""

from __future__ import annotations

import logging

from ...config import assignment_for
from .. import backend_health
from ..model import (LABEL_FAILED, LABEL_SKIP, Mission,
                     MissionType, PRIORITY_RANK, derive, find_cycles)
from .markers import DISPATCHABLE_TYPES, decomposition_parent_ref

log = logging.getLogger("devcake.missions")


async def gate_map(mgr, missions: list[Mission]) -> dict[str, str]:
    """The blocked-by gate as a first-class poll artifact (docs/04 §2):
    pmo_id → human-readable reason for every open mission the gate holds
    back. Computed EVERY cycle — paused or not — so /api/v1/missions never
    serves stale gate info. Members of a dependency cycle get an explicit
    unsatisfiable-wait reason instead of ordinary blocking (docs/04 §2a).
    Also refreshes mgr.blocked_reasons / mgr.cycles (advisory mirrors)."""
    by_id = {m.pmo_id: m for m in missions}
    by_key = {m.key.upper(): m for m in missions if m.key}
    id_to_key = {m.pmo_id: m.key for m in missions}
    graph = {m.pmo_id: set(m.blocked_by) for m in missions
             if m.pmo_kind == "issue" and m.blocked_by}
    cycle_of: dict[str, list[str]] = {}
    mgr.cycles = []
    for cyc in find_cycles(graph):
        keys = [id_to_key.get(i, i) for i in cyc]
        mgr.cycles.append(keys)
        for i in cyc:
            cycle_of[i] = keys
    gate: dict[str, str] = {}
    memo: dict = {}   # bid → Resolved | None (BlockerLocator-managed)
    for m in missions:
        if not m.blocked_by or m.status in ("done", "canceled"):
            continue
        if m.pmo_id in cycle_of:
            loop = " → ".join(cycle_of[m.pmo_id] + [cycle_of[m.pmo_id][0]])
            gate[m.pmo_id] = (f"dependency cycle: {loop} — will never "
                              f"unblock; delete one relation in the PMO")
            continue
        open_blockers = await _open_blockers(mgr, m, by_id, memo)
        if open_blockers:
            gate[m.pmo_id] = "blocked by " + ", ".join(open_blockers)
    # family gate (ADR-0012): a decomposition child whose ISSUE parent is
    # still open is mid-wiring — its inherited/sibling edges may not all
    # exist yet, because the parent's cancel is finalization's LAST step.
    # An open issue-parent therefore means the family graph is incomplete
    # (or the parent is parked for a human) and the child must wait.
    # Project parents stay open by design (DEVCAKE-TRACKING) and vanished
    # parents can never terminate — both exempt (fail-open, pre-ADR
    # behavior; the snapshot includes terminal missions, docs/04 §2).
    # Parent trust + resolve match family_of: decomposition_parent_ref
    # (LABEL_CREATED) and pmo_id-or-key lookup (key is a defensive alias).
    for m in missions:
        if m.pmo_kind != "issue" or m.status in ("done", "canceled") \
                or m.pmo_id in gate:
            continue
        pref = decomposition_parent_ref(m)
        if not pref:
            continue
        parent = by_id.get(pref) or by_key.get(pref.upper())
        if parent is not None and parent.pmo_kind == "issue" \
                and parent.status not in ("done", "canceled"):
            gate[m.pmo_id] = (f"decomposition of {parent.key} not finalized "
                              f"— the parent issue is still open")
    mgr.blocked_reasons = gate
    return gate


async def schedule(mgr, missions: list[Mission],
                   gate: dict[str, str] | None = None) -> int:
    if gate is None:                           # poll_loop passes its own
        gate = await gate_map(mgr, missions)
    candidates = []
    for m in missions:
        d = derive(m, mgr.config.adoption_mode)
        if not d.schedulable or d.mission_type not in DISPATCHABLE_TYPES:
            continue
        if m.pmo_id in mgr._grace:
            continue  # grace cycle after our own writes (docs/04 §2)
        if any(r.mission_pmo_id == m.pmo_id and mgr._run_is_ours(r)
               for r in mgr.runs.store.active()):
            continue  # in-flight guard
        if m.pmo_id in gate:                   # blocked-by gate (docs/04 §2)
            log.info("mission %s not scheduled — %s", m.key, gate[m.pmo_id])
            continue
        candidates.append((m, d))

    candidates.sort(key=lambda md: (PRIORITY_RANK[md[0].priority],
                                    md[0].updated_at, md[0].pmo_id))
    dispatched = 0
    active = mgr.runs.store.active()
    for mission, d in candidates:
        # per-mission repo gate (M10): unresolved missions surface WHY and
        # never dispatch; a latched breaker on repo A never stops repo B
        if mission.repo is None:
            mgr.blocked_reasons[mission.pmo_id] = (
                mission.repo_reason or "no repository resolved")
            continue
        if mission.repo in mgr.forges.breakers:
            continue  # this repo's breaker is latched (docs/15 §4)
        assignment = assignment_for(mgr.config, mgr.instance,
                                    d.mission_type.value)
        dev_type = mgr.dev_types.get(assignment.dev_type)
        if dev_type is None:
            # surface WHY (M10 repo-gate precedent): overrides skip PUT-time
            # existence checks, so a vanished/typo'd name is reachable via a
            # raw config PUT and must never park missions invisibly
            mgr.blocked_reasons[mission.pmo_id] = (
                f"{d.mission_type.value} is unassigned — set an assignment"
                if not assignment.dev_type else
                f"{d.mission_type.value} is assigned to Dev Type "
                f"{assignment.dev_type!r}, which does not exist — fix the "
                f"assignment (global or this instance's override)")
            continue
        if dev_type.name in mgr.breakers:
            continue  # auth breaker tripped (docs/15 §4)
        # ADR-0018: a dev type whose model backend looks sick is throttled to a
        # single probe run rather than blocked. The probe IS the half-open — it
        # is what lets the store-derived condition clear itself, so this must
        # never become a hard skip.
        cap = (backend_health.DEGRADED_CONCURRENCY
               if dev_type.name in mgr.backend_degraded else dev_type.max_concurrency)
        if sum(1 for r in active if r.dev_type == dev_type.name) >= cap:
            continue
        if len(active) >= mgr.config.concurrency.global_max:
            break
        run = await mgr.dispatch(mission, d.mission_type, dev_type)
        if run:
            active.append(run)
            dispatched += 1
    return dispatched


async def open_blockers_live(mgr, m: Mission) -> list[str]:
    """The all-live variant (ADR-0034 PR-3): dispatch's pre-launch re-read
    resolves every blocker fresh — empty by_id (no snapshot index) and a
    fresh memo (no cross-mission reuse). The two empty dicts USED to be
    magic arguments hand-rolled at the call site."""
    return await _open_blockers(mgr, m, {}, {})


async def _open_blockers(mgr, m: Mission, by_id: dict[str, Mission],
                         memo: dict) -> list[str]:
    """Blockers of `m` that are still open (status not done/canceled), as
    human-readable keys. Off-snapshot ids resolve through the deployment-wide
    BlockerLocator (ADR-0009 amendment): owner map → same-system peers
    (Linear v1) → local adapter — so a native edge to a peer instance's
    mission gates exactly like a local one. A blocker no path can read
    counts as open (fail-safe; self-heals next cycle). ADR-0007. `memo`
    holds `Resolved | None` per bid (locator-managed, one walk per cycle)."""
    open_ = []
    for bid in m.blocked_by:
        b = by_id.get(bid)
        if b is None:
            first = bid not in memo
            r = await mgr.blocker_locator.resolve(
                bid, local_mgr=mgr, memo=memo)
            b = r.mission if r is not None else None
            if b is None and first:
                log.warning("blocker %s of %s unreadable — treated as open",
                            bid, m.key)
        if b is None:
            open_.append(f"{bid} (unreadable)")
        elif b.status not in ("done", "canceled"):
            guard = next((f" ({l})" for l in (LABEL_FAILED, LABEL_SKIP)
                          if l in b.labels), "")
            open_.append(b.key + guard)
    return open_

