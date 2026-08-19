"""Relations Steward cadence service (ADR-0007). Split from the orchestrator
god object for maintainability.
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
        """The interval path, called once per poll cycle (never while paused).

        Self-guards intake pause (ADR-0034: the guard travels with the
        path, not only the poll caller — same as maybe_dispatch_discovery).
        """
        if intake_blocks_dispatch(self.config, self.mgr.instance):
            return
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
        mirror_ok, mirror_why, ctx_stale, ctx_omit = \
            await self._context_gate(dt, repo)
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
                    run = await self.mgr.dispatch_steward(
                        dt, missions, context_stale=ctx_stale,
                        context_omit=ctx_omit)
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
            # mirror-ELIGIBLE names only (the needed_for precedent): the
            # family's work repos legitimately include INTERNAL per-mission
            # repos — valid clone extras (direct, mission-scoped creds) but
            # never mirrored (ADR-0024 §5 security ruling). Passing one to
            # ensure_fresh would 401 the unauthenticated mirror fetch and
            # wedge the gate mirror_stale forever on zero-repo boards
            # (found by the graduation-smoke prep, 2026-08-13).
            fam_repos = [e["repo_ref"] for e in family_graph.family_work_repos(
                mgr, fam, exclude=frozenset({repo}))
                if mgr.repo_cache.eligible(e["repo_ref"])]
            # skill cards deliberately DO NOT ride the eligible() filter:
            # an unconfigured/internal skill card must gate LOUDLY
            # (fail-closed ruling), unlike family work repos which are
            # legitimate internal clone extras (audit B2: keep BOTH halves).
            mirror_ok, mirror_why, ctx_stale, ctx_omit = \
                await self._context_gate(dt, repo, extra=fam_repos)
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
                        # same gate honesty as relations / run_now: stale and
                        # omit ride into launch so mounts and mirror_repos
                        # match what `_context_gate` just decided
                        run = await mgr.dispatch_steward_discovery(
                            dt, fam, pending, context_stale=ctx_stale,
                            context_omit=ctx_omit)
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

    async def _context_gate(self, dt, repo: str, extra=()
                            ) -> tuple[bool, dict, set[str], set[str]]:
        """Mirror gate + PLAN_MEMORY §3.5: skill-source and memory cards
        are toggle-governed; work/family extras stay fail-closed. Returns
        (ok, why, stale, omit) — the stale/omit sets ride into the launch
        snapshot so the run record stays honest about its mounts."""
        from .repo_sourcing import (classify_context_failures, memory_mount_names,
                                    skill_source_cards,
                                    unresolvable_memory_cards)
        skill_cards = skill_source_cards(dt.skills)
        memory_cards = set(memory_mount_names(
            instance=self.mgr.instance, dev_type=dt, repo_ref=repo))
        needed_for = getattr(self.mgr.repo_cache, "needed_for", None)
        if callable(needed_for):
            needed = list(needed_for(
                work_repo=repo, mission_type="STEWARD",
                instance=self.mgr.instance, blocker_entries=None,
                dev_type=dt, config=self.config))
            needed = sorted(set(needed) | skill_cards | set(extra or ()))
        else:
            # test fakes that only implement ensure_fresh (ADR-0033 gate tests)
            needed = [repo] + list(extra or ()) + sorted(skill_cards | memory_cards)
        ok, why = await self.mgr.repo_cache.ensure_fresh(needed)
        stale: set[str] = set()
        omit: set[str] = set()
        if not ok:
            has_last = getattr(self.mgr.repo_cache, "has_last_good", None)
            defer, stale, omit = classify_context_failures(
                why, context_cards=memory_cards | skill_cards,
                strict=self.config.context_sourcing_strict,
                has_mirror=has_last if callable(has_last) else (lambda n: False))
            if defer:
                return False, defer, set(), set()
        # §3.5 second half: dangling / uncredentialed internal notebooks
        # never reach the mirror gate — resolve them here, same as dispatch
        eligible = getattr(self.mgr.repo_cache, "eligible", None)
        if callable(eligible):
            bad = unresolvable_memory_cards(
                self.config, memory_cards - omit, mirror_eligible=eligible)
            if bad:
                if self.config.context_sourcing_strict:
                    return False, bad, set(), set()
                omit |= set(bad)
        if stale or omit:
            log.warning("steward context gate: stale=%s omit=%s",
                        sorted(stale), sorted(omit))
        return True, {}, stale, omit

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
        degraded state — a human pressing the button IS the reset signal.

        Intake pause still freezes this path (docs/03 §4b, docs/11): pause
        means no NEW Dev runs (missions or steward), and "Run now" is not a
        back door around the master or per-instance switch.
        """
        if intake_blocks_dispatch(self.config, self.mgr.instance):
            # name which switch is on so the 422 is actionable
            if self.config.intake_paused:
                raise StewardUnconfigured(
                    "intake is paused (global master) — unpause Mission "
                    "intake in the sidebar before running the steward")
            raise StewardUnconfigured(
                f"intake is paused for PMO instance "
                f"{self.mgr.instance.name!r} — unpause it on the PMO page "
                f"before running the steward")
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
        mirror_ok, mirror_why, ctx_stale, ctx_omit = \
            await self._context_gate(dt, repo)
        if not mirror_ok:
            raise StewardUnconfigured(
                f"repository mirror for '{repo}' is not fresh: "
                f"{mirror_why.get(repo, 'sync failed')}")
        async with self._lock:
            if self.active():
                raise StewardBusy("a steward run is already active")
            missions = await self.mgr.pmo.list_all(self.mgr.instance.team_key)
            run = await self.mgr.dispatch_steward(
                dt, missions, context_stale=ctx_stale, context_omit=ctx_omit)
            if run is None:
                # AUD-001 skip (workspace base unusable) — surfaced as 422,
                # not an AttributeError 500 in the route (pre-existing bug)
                raise StewardUnconfigured(
                    "steward dispatch skipped — workspace base unusable; "
                    "see the app logs")
            return run
