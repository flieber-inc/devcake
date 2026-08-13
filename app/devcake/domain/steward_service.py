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

from ..config import AppConfig, DevType, intake_blocks_dispatch
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
        # ADR-0033 discovery lane: event-kicked by harvest; re-driven by
        # the label sweep. Shares `_lock` with the relations cadence and
        # run_now — one STEWARD slot (addendum 11), not two locks around
        # the same active() check.
        self._last_discovery_outcome: str | None = None

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
                    run = await self.mgr.dispatch_steward(dt, missions)
                    if run is None:
                        outcome = "dispatch_skipped"
                    else:
                        self._last_at = time.monotonic()
                        outcome = "dispatched"
        if outcome == "dispatched" or outcome != self._last_periodic_outcome:
            with tracer.start_as_current_span("steward.periodic") as span:
                span.set_attribute("devcake.outcome", outcome)
                if error:
                    span.set_status(Status(StatusCode.ERROR, error[:200]))
        self._last_periodic_outcome = outcome

    async def maybe_dispatch_discovery(self, missions: list[Mission]) -> None:
        """The ADR-0033 discovery lane: drain ONE family's pending batches
        per call (single-flight; the advisory queue keeps the rest for the
        next kick/cycle). Shares active()/global_max/degraded() with the
        relations cadence — both are board-tending runs on the same live
        board, and deferral is the design (batching calm over fan-out)."""
        mgr = self.mgr
        if intake_blocks_dispatch(self.config, mgr.instance):
            return
        if not mgr.instance.discovery_routing:   # D11 — direct field access,
            return                               # fail-closed on rename
        dt = self.dev_type()
        repo = mgr.steward_repo()
        if dt is None or repo is None or repo in mgr.forges.breakers:
            return
        if not mgr._discoveries_pending:
            return
        from .orchestrator import discovery, family_graph
        outcome, error = None, None
        degraded = self.degraded()
        if degraded:
            outcome, error = "degraded_skip", degraded
        elif (missing := missing_referenced_secret_env(dt)):
            outcome = "secret_env_gate"
            error = (f"secret env {', '.join(missing)} referenced by "
                     "mcp_setup_commands but not stored")
        else:
            by_id = {m.pmo_id: m for m in missions}
            sources = [by_id[p] for p in sorted(mgr._discoveries_pending)
                       if p in by_id]
            if not sources:
                # off-snapshot ids: drop — the label sweep re-seeds real ones
                mgr._discoveries_pending.clear()
                return
            fam = family_graph.family_of(sources[0], missions)
            group = [s for s in sources if s.pmo_id in fam.by_id]
            pending: dict[str, list[tuple[int, int]]] = {}
            for s in group:
                state = await discovery.scan_source(mgr, s)
                if state.truncated:
                    # ceiling case — the sweep raises it to the humans and
                    # retires the gate; holding the id here would re-fetch
                    # a full feed every cycle for nothing
                    mgr._discoveries_pending.discard(s.pmo_id)
                    continue
                if state.pending:
                    pending[s.pmo_id] = state.pending
                else:                             # already receipted — done
                    mgr._discoveries_pending.discard(s.pmo_id)
            if not pending:
                return
            fam_repos = [e["repo_ref"] for e in family_graph.family_work_repos(
                mgr, fam, exclude=frozenset({repo}))]
            mirror_ok, mirror_why = await mgr.repo_cache.ensure_fresh(
                [repo] + fam_repos)
            if not mirror_ok:
                outcome = "mirror_stale"          # fail-closed, retry later
                error = f"family mirrors not fresh: {mirror_why}"
                log.warning("discovery steward skipped — %s", error)
            else:
                async with self._lock:
                    if self.active():
                        outcome = "already_active"
                    elif (len(mgr.runs.store.active())
                          >= self.config.concurrency.global_max):
                        outcome = "concurrency_deferred"
                    else:
                        run = await mgr.dispatch_steward_discovery(
                            dt, fam, pending)
                        if run is None:
                            outcome = "dispatch_skipped"   # workspace/empty
                        else:
                            # served — finalize receipts them; a crash is
                            # re-detected from the board by the sweep
                            for pid in pending:
                                mgr._discoveries_pending.discard(pid)
                            outcome = "dispatched"
        if outcome and (outcome == "dispatched"
                        or outcome != self._last_discovery_outcome):
            with tracer.start_as_current_span("steward.discovery") as span:
                span.set_attribute("devcake.outcome", outcome)
                if error:
                    span.set_status(Status(StatusCode.ERROR, error[:200]))
        self._last_discovery_outcome = outcome

    def kick_discovery(self) -> None:
        """Event trigger (harvest enqueued from the ingress task context):
        fire-and-forget one discovery pass with a fresh board fetch. Errors
        are swallowed — the poll-cycle sweep is the durable retry path."""
        async def _once():
            try:
                missions = await self.mgr.pmo.list_all(
                    self.mgr.instance.team_key)
                await self.maybe_dispatch_discovery(missions)
            except Exception:  # noqa: BLE001 — best-effort by design
                log.debug("discovery kick failed — the sweep re-drives",
                          exc_info=True)
        try:
            asyncio.create_task(_once())
        except RuntimeError:  # no running loop (sync test contexts)
            log.debug("discovery kick without a running loop — skipped")

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
            run = await self.mgr.dispatch_steward(dt, missions)
            if run is None:
                # AUD-001 skip (workspace base unusable) — surfaced as 422,
                # not an AttributeError 500 in the route (pre-existing bug)
                raise StewardUnconfigured(
                    "steward dispatch skipped — workspace base unusable; "
                    "see the app logs")
            return run
