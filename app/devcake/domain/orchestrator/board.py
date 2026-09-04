"""The cycle's board snapshot — enumeration reads reuse the poll's own fetch.

ADR-0003 (amended): a decision that MUTATES a mission on the strength of
that mission's state — dispatch, transition, completion, decomposition,
edge writes — still pays a live PMO read. ENUMERATION within one poll cycle
(which children a project has, which ancestors a mission offers, which
scheduled tickets are in flight) reuses the `list_all` the cycle already
paid for: staleness there can only delay a decision by one cycle, never
wrongly commit one. Vendor quotas are metered (ADR-0040), so every read
the board does not need is one the write-backs keep.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from ..model import Mission
from ..run import utcnow


@dataclass(frozen=True)
class BoardSnapshot:
    """The FULL fetched set of one instance's team (terminal included), as
    `poll_instance` saw it. Immutable for the cycle: nothing reads it after
    an own write within the same cycle, and the next fetch reconciles."""
    missions: tuple[Mission, ...]
    cycle: int
    fetched_at: datetime

    def children_of(self, project_id: str) -> list[Mission]:
        """Issues that belong to the project (`parent_ref` is the containing
        project's id on vendors with projects; None elsewhere, so this is
        empty there and the caller falls back to a live read)."""
        return [m for m in self.missions
                if m.pmo_kind == "issue" and m.parent_ref == project_id]

    def age(self, now: datetime | None = None) -> timedelta:
        return (now or utcnow()) - self.fetched_at


def bump(mgr: Any, key: str) -> None:
    """Per-cycle demand counter (reset by `poll_instance`; surfaced on
    /health `pmo_demand` and the `poll.instance` span)."""
    stats = getattr(mgr, "cycle_stats", None)
    if stats is not None:
        stats[key] = stats.get(key, 0) + 1


async def board_missions(mgr: Any, *, max_age: timedelta) -> list[Mission]:
    """Enumeration read: the cycle snapshot when younger than `max_age`,
    else a live `list_all` (which the caller may itself re-seed)."""
    snap = getattr(mgr, "snapshot", None)
    if snap is not None and snap.age() <= max_age:
        bump(mgr, "snapshot_hits")
        return list(snap.missions)
    bump(mgr, "snapshot_misses")
    return await mgr.pmo.list_all(mgr.instance.team_key)
