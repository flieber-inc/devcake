"""Relations Mapper cadence service (ADR-0007). Split from the orchestrator
god object for maintainability (ISSUES #36).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..config import AppConfig, DevType
from .model import Mission
from .run import Run

if TYPE_CHECKING:
    from .orchestrator import MissionManager

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


class MapperBusy(Exception):
    """A mapper run is already active."""


class MapperUnconfigured(Exception):
    """relations_mapper.dev_type does not name an existing Dev Type."""


class MapperService:
    """Cadence + concurrency for MAPPER runs. One lock closes the manual-vs-
    interval double-dispatch window; the watermark advances only AFTER a
    successful dispatch (a transient executor error costs one poll cycle, not a
    full interval); degradation is derived from the run store — restart-safe,
    and a successful run clears it naturally."""

    def __init__(self, config: AppConfig, dev_types: dict[str, DevType],
                 mgr: MissionManager):
        self.config = config
        self.dev_types = dev_types
        self.mgr = mgr
        self._lock = asyncio.Lock()
        # first auto-run lands one interval after boot; "Run now" covers immediacy
        self._last_at = time.monotonic()
        self._last_periodic_outcome: str | None = None  # span-on-transition dedupe

    def dev_type(self) -> DevType | None:
        rm = self.config.relations_mapper
        return self.dev_types.get(rm.dev_type) if rm.dev_type else None

    def active(self) -> bool:
        return any(r.mission_type == "MAPPER"
                   for r in self.mgr.runs.store.active())

    def degraded(self) -> str | None:
        """The 3 most recent MAPPER runs all dead ⇒ the periodic service backs
        off (docs/15). Run now stays available; a success clears the condition."""
        recent = sorted((r for r in self.mgr.runs.store.all()
                         if r.mission_type == "MAPPER"),
                        key=lambda r: r.created_at, reverse=True)[:3]
        if len(recent) == 3 and all(r.state in ("failed", "timed_out", "orphaned")
                                    for r in recent):
            return recent[0].error or "3 consecutive mapper failures"
        return None

    async def maybe_dispatch(self, missions: list[Mission]) -> None:
        """The interval path, called once per poll cycle (never while paused)."""
        rm = self.config.relations_mapper
        dt = self.dev_type()
        if not rm.enabled or dt is None or "forge" in self.mgr.breakers:
            return
        if time.monotonic() - self._last_at < rm.interval_minutes * 60:
            self._last_periodic_outcome = None
            return
        # a periodic run is DUE — resolve it, then trace the outcome only on
        # TRANSITIONS: a mapper stuck degraded/waiting for hours would
        # otherwise emit an identical (ERROR) span every poll tick
        outcome, error = None, None
        degraded = self.degraded()
        if degraded:
            outcome, error = "degraded_skip", degraded
            log.warning("mapper degraded — periodic run skipped (%s)", degraded)
        else:
            async with self._lock:
                if self.active():
                    outcome = "already_active"
                elif len(self.mgr.runs.store.active()) >= self.config.concurrency.global_max:
                    outcome = "concurrency_deferred"  # counts toward the global cap
                else:
                    await self.mgr.dispatch_mapper(dt, missions)
                    self._last_at = time.monotonic()
                    outcome = "dispatched"
        if outcome == "dispatched" or outcome != self._last_periodic_outcome:
            with tracer.start_as_current_span("mapper.periodic") as span:
                span.set_attribute("devcake.outcome", outcome)
                if error:
                    span.set_status(Status(StatusCode.ERROR, error[:200]))
        self._last_periodic_outcome = outcome

    async def run_now(self) -> Run:
        """Manual trigger: works regardless of the periodic toggle and of the
        degraded state — a human pressing the button IS the reset signal."""
        dt = self.dev_type()
        if dt is None:
            raise MapperUnconfigured(
                "relations_mapper.dev_type must name an existing Dev Type — "
                "set it on the Config page")
        if "forge" in self.mgr.breakers:
            raise MapperUnconfigured(
                "forge connection is not writable; fix the repository token first")
        async with self._lock:
            if self.active():
                raise MapperBusy("a relations-mapper run is already active")
            missions = await self.mgr.pmo.list_all(self.mgr.instance.team_key)
            return await self.mgr.dispatch_mapper(dt, missions)
