"""Scheduling and blocked-by gate (docs/04 §§2–3)."""

from __future__ import annotations

import logging

from ..model import (LABEL_FAILED, LABEL_SKIP, Mission, MissionRef, MissionType,
                     PRIORITY_RANK, derive, find_cycles)
from .markers import DISPATCHABLE_TYPES

log = logging.getLogger("devcake.missions")


async def gate_map(self, missions: list[Mission]) -> dict[str, str]:
    """The blocked-by gate as a first-class poll artifact (docs/04 §2):
    pmo_id → human-readable reason for every open mission the gate holds
    back. Computed EVERY cycle — paused or not — so /api/v1/missions never
    serves stale gate info. Members of a dependency cycle get an explicit
    unsatisfiable-wait reason instead of ordinary blocking (docs/04 §2a).
    Also refreshes self.blocked_reasons / self.cycles (advisory mirrors)."""
    by_id = {m.pmo_id: m for m in missions}
    id_to_key = {m.pmo_id: m.key for m in missions}
    graph = {m.pmo_id: set(m.blocked_by) for m in missions
             if m.pmo_kind == "issue" and m.blocked_by}
    cycle_of: dict[str, list[str]] = {}
    self.cycles = []
    for cyc in find_cycles(graph):
        keys = [id_to_key.get(i, i) for i in cyc]
        self.cycles.append(keys)
        for i in cyc:
            cycle_of[i] = keys
    gate: dict[str, str] = {}
    memo: dict[str, Mission | None] = {}
    for m in missions:
        if not m.blocked_by or m.status in ("done", "canceled"):
            continue
        if m.pmo_id in cycle_of:
            loop = " → ".join(cycle_of[m.pmo_id] + [cycle_of[m.pmo_id][0]])
            gate[m.pmo_id] = (f"dependency cycle: {loop} — will never "
                              f"unblock; delete one relation in the PMO")
            continue
        open_blockers = await self._open_blockers(m, by_id, memo)
        if open_blockers:
            gate[m.pmo_id] = "blocked by " + ", ".join(open_blockers)
    self.blocked_reasons = gate
    return gate


async def schedule(self, missions: list[Mission],
                   gate: dict[str, str] | None = None) -> int:
    if gate is None:                           # poll_loop passes its own
        gate = await self.gate_map(missions)
    candidates = []
    for m in missions:
        d = derive(m, self.config.adoption_mode)
        if not d.schedulable or d.mission_type not in DISPATCHABLE_TYPES:
            continue
        if m.pmo_id in self._grace:
            continue  # grace cycle after our own writes (docs/04 §2)
        if any(r.mission_pmo_id == m.pmo_id and self._run_is_ours(r)
               for r in self.runs.store.active()):
            continue  # in-flight guard
        if m.pmo_id in gate:                   # blocked-by gate (docs/04 §2)
            log.info("mission %s not scheduled — %s", m.key, gate[m.pmo_id])
            continue
        candidates.append((m, d))

    candidates.sort(key=lambda md: (PRIORITY_RANK[md[0].priority],
                                    md[0].updated_at, md[0].pmo_id))
    dispatched = 0
    active = self.runs.store.active()
    for mission, d in candidates:
        # per-mission repo gate (M10): unresolved missions surface WHY and
        # never dispatch; a latched breaker on repo A never stops repo B
        if mission.repo is None:
            self.blocked_reasons[mission.pmo_id] = (
                mission.repo_reason or "no repository resolved")
            continue
        if mission.repo in self.forges.breakers:
            continue  # this repo's breaker is latched (docs/15 §4)
        dev_type = self.dev_types.get(self.config.assignments[d.mission_type.value].dev_type)
        if dev_type is None or dev_type.name in self.breakers:
            continue  # unassigned or auth breaker tripped (docs/15 §4)
        if sum(1 for r in active if r.dev_type == dev_type.name) >= dev_type.max_concurrency:
            continue
        if len(active) >= self.config.concurrency.global_max:
            break
        run = await self.dispatch(mission, d.mission_type, dev_type)
        if run:
            active.append(run)
            dispatched += 1
    return dispatched


async def _open_blockers(self, m: Mission, by_id: dict[str, Mission],
                         memo: dict[str, Mission | None]) -> list[str]:
    """Blockers of `m` that are still open (status not done/canceled), as
    human-readable keys. A blocker we cannot read counts as open (fail-safe;
    self-heals next cycle). ADR-0007."""
    open_ = []
    for bid in m.blocked_by:
        b = by_id.get(bid)
        if b is None:
            if bid not in memo:
                try:
                    memo[bid] = await self.pmo.get(MissionRef(bid, "issue"))
                except Exception:
                    log.warning("blocker %s of %s unreadable — treated as open",
                                bid, m.key)
                    memo[bid] = None
            b = memo[bid]
        if b is None:
            open_.append(f"{bid} (unreadable)")
        elif b.status not in ("done", "canceled"):
            guard = next((f" ({l})" for l in (LABEL_FAILED, LABEL_SKIP)
                          if l in b.labels), "")
            open_.append(b.key + guard)
    return open_

