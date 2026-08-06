"""Relations Steward cadence service (ADR-0007). Split from the orchestrator
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
from ..harness import missing_referenced_secret_env
from .model import Mission
from .run import Run

if TYPE_CHECKING:
    from .orchestrator import MissionManager

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


class StewardBusy(Exception):
    """A steward run is already active."""


class StewardUnconfigured(Exception):
    """steward.dev_type does not name an existing Dev Type."""


class StewardService:
    """Cadence + concurrency for STEWARD runs. One lock closes the manual-vs-
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
        rm = self.config.steward
        return self.dev_types.get(rm.dev_type) if rm.dev_type else None

    def active(self) -> bool:
        return any(r.mission_type == "STEWARD"
                   for r in self.mgr.runs.store.active())

    def degraded(self) -> str | None:
        """The 3 most recent STEWARD runs all dead ⇒ the periodic service backs
        off (docs/15). Run now stays available; a success clears the condition."""
        recent = sorted((r for r in self.mgr.runs.store.all()
                         if r.mission_type == "STEWARD"),
                        key=lambda r: r.created_at, reverse=True)[:3]
        if len(recent) == 3 and all(r.state in ("failed", "timed_out", "orphaned")
                                    for r in recent):
            return recent[0].error or "3 consecutive steward failures"
        return None

    async def maybe_dispatch(self, missions: list[Mission]) -> None:
        """The interval path, called once per poll cycle (never while paused)."""
        rm = self.config.steward
        dt = self.dev_type()
        repo = self.mgr.steward_repo()
        if not rm.enabled or dt is None or repo is None \
                or repo in self.mgr.forges.breakers:   # no/broken repo → idle
            return
        if time.monotonic() - self._last_at < rm.interval_minutes * 60:
            self._last_periodic_outcome = None
            return
        # a periodic run is DUE — resolve it, then trace the outcome only on
        # TRANSITIONS: a steward stuck degraded/waiting for hours would
        # otherwise emit an identical (ERROR) span every poll tick
        outcome, error = None, None
        degraded = self.degraded()
        mirror_ok, mirror_why = await self.mgr.repo_cache.ensure_fresh([repo])
        if not mirror_ok:
            # ADR-0024 fail-closed precondition — same semantics as mission
            # dispatch: skip this cycle, retry next; NEVER raises into the
            # poll segment (an exception here would mark the instance
            # poll_degraded)
            outcome = "mirror_stale"
            error = f"repository mirror not fresh: {mirror_why.get(repo, '')}"
            log.warning("steward periodic run skipped — %s", error)
        elif degraded:
            outcome, error = "degraded_skip", degraded
            log.warning("steward degraded — periodic run skipped (%s)", degraded)
        elif (missing := missing_referenced_secret_env(dt)):
            # same gate as mission dispatch: a referenced-but-unstored
            # secret env var would exit 14 in-container (docs/14 §8)
            outcome = "secret_env_gate"
            error = (f"secret env {', '.join(missing)} referenced by "
                     "mcp_setup_commands but not stored")
            log.warning("steward periodic run skipped — %s", error)
        else:
            async with self._lock:
                if self.active():
                    outcome = "already_active"
                elif len(self.mgr.runs.store.active()) >= self.config.concurrency.global_max:
                    outcome = "concurrency_deferred"  # counts toward the global cap
                else:
                    await self.mgr.dispatch_steward(dt, missions)
                    self._last_at = time.monotonic()
                    outcome = "dispatched"
        if outcome == "dispatched" or outcome != self._last_periodic_outcome:
            with tracer.start_as_current_span("steward.periodic") as span:
                span.set_attribute("devcake.outcome", outcome)
                if error:
                    span.set_status(Status(StatusCode.ERROR, error[:200]))
        self._last_periodic_outcome = outcome

    async def run_now(self) -> Run:
        """Manual trigger: works regardless of the periodic toggle and of the
        degraded state — a human pressing the button IS the reset signal."""
        dt = self.dev_type()
        if dt is None:
            raise StewardUnconfigured(
                "steward.dev_type must name an existing Dev Type — "
                "set it on the Config page")
        if (missing := missing_referenced_secret_env(dt)):
            raise StewardUnconfigured(
                f"dev type {dt.name}: secret env {', '.join(missing)} is "
                "referenced by mcp_setup_commands but has no stored value — "
                "paste it on the admin Config page")
        repo = self.mgr.steward_repo()
        if repo is None:
            raise StewardUnconfigured(
                "no repository configured — steward runs need the forge "
                "dialect in their run spec")
        if repo in self.mgr.forges.breakers:
            raise StewardUnconfigured(
                f"repo '{repo}' is not writable; fix its token first")
        mirror_ok, mirror_why = await self.mgr.repo_cache.ensure_fresh([repo])
        if not mirror_ok:
            raise StewardUnconfigured(
                f"repository mirror for '{repo}' is not fresh: "
                f"{mirror_why.get(repo, 'sync failed')}")
        async with self._lock:
            if self.active():
                raise StewardBusy("a steward run is already active")
            missions = await self.mgr.pmo.list_all(self.mgr.instance.team_key)
            return await self.mgr.dispatch_steward(dt, missions)
