"""Cron module — one verb: create a labeled ticket (PLAN_MEMORY §6).

Reserved `memory-curator` fans out to every Curator board (repos == [m]
for each memory-bound card). Generic rows fire at `row.pmo`.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..config import (AppConfig, CronJob, MEMORY_CURATOR_CRON_ID,
                      intake_blocks_dispatch, memory_bound_names)
from . import claims as claims_mod
from .model import LABEL_EXECUTE, LABEL_OPTIN, LABEL_PLAN, LABEL_REVIEW

if TYPE_CHECKING:
    from .orchestrator import MissionManager

log = logging.getLogger("devcake.cron")

_STAGE_LABEL = {
    "PLAN": LABEL_PLAN,
    "EXECUTE": LABEL_EXECUTE,
    "REVIEW": LABEL_REVIEW,
}
CRON_MARKER_RE = re.compile(r"`devcake:cron:v1 job=([a-z0-9_-]+)`")
CRON_MARKER = "`devcake:cron:v1 job={id}`"


def cron_marker(job_id: str) -> str:
    return CRON_MARKER.format(id=job_id)


def defang_template(text: str) -> str:
    """Templates must not smuggle backticks that would break the marker."""
    return (text or "").replace("`", "'")


class CronBusy(Exception):
    """An in-flight cron ticket already exists for this job (/ board)."""


class CronUnconfigured(Exception):
    """Unknown id, or a reserved row aimed at a product PMO."""


class CronService:
    """Single-process cadence + Run-now. One lock per job id (and, for
    memory-curator, per target board as well). Degradation is store-derived
    from recent fire outcomes — restart-safe like Steward."""

    def __init__(self, config: AppConfig, managers: dict[str, MissionManager],
                 claims=None, dev_types=None):
        self.config = config
        self.managers = managers
        self.claims = claims
        # live Dev Type map — set M must include domain-bound notebooks
        # (PLAN_MEMORY §6.3: "same set as I2"), not just instance lists
        self.dev_types = dev_types if dev_types is not None else {}
        self._locks: dict[str, asyncio.Lock] = {}
        # job_id → last N automatic outcomes ('created'/'skipped'/'failed')
        self._outcomes: dict[str, list[str]] = {}
        self.degraded: set[str] = set()
        self.no_board: set[str] = set()  # memory_curator_no_board:<m>
        self._last_auto_minute: dict[str, int] = {}

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def job(self, job_id: str) -> CronJob | None:
        return next((c for c in self.config.crons if c.id == job_id), None)

    def _record(self, job_id: str, outcome: str) -> None:
        hist = self._outcomes.setdefault(job_id, [])
        hist.append(outcome)
        if len(hist) > 3:
            del hist[:-3]
        if (len(hist) == 3
                and all(o == "failed" for o in hist)):
            self.degraded.add(job_id)
        elif outcome == "created" or outcome == "skipped":
            self.degraded.discard(job_id)

    async def maybe_fire(self) -> None:
        """Interval path — one pass per poll cycle."""
        now = datetime.now(timezone.utc)
        for row in list(self.config.crons):
            if not row.enabled:
                continue
            if row.id in self.degraded:
                continue
            # interval: fire when minute-of-epoch is a multiple, once
            # per window (watermark). Poll cadence is ~30s so a 60-min
            # job fires about once an hour.
            minute = int(now.timestamp()) // 60
            interval = max(row.interval_minutes, 1)
            if minute % interval != 0:
                continue
            if self._last_auto_minute.get(row.id) == minute:
                continue
            try:
                await self.fire(row.id, automatic=True)
                self._last_auto_minute[row.id] = minute
            except (CronBusy, CronUnconfigured):
                self._record(row.id, "skipped")
            except Exception:  # noqa: BLE001 — one row must not kill the cycle
                log.exception("cron %s automatic fire failed", row.id)
                self._record(row.id, "failed")

    async def fire(self, job_id: str, *, automatic: bool) -> list[dict]:
        """Create tickets. `automatic=False` is Run now (F4: no empty skip)."""
        row = self.job(job_id)
        if row is None:
            raise CronUnconfigured(f"unknown cron {job_id!r}")
        if job_id == MEMORY_CURATOR_CRON_ID:
            return await self._fire_memory_curator(row, automatic=automatic)
        return await self._fire_generic(row)

    async def _fire_generic(self, row: CronJob) -> list[dict]:
        if not row.pmo:
            raise CronUnconfigured(f"cron {row.id} has no target pmo")
        mgr = self.managers.get(row.pmo)
        if mgr is None:
            raise CronUnconfigured(f"cron {row.id}: no live manager for "
                                   f"pmo {row.pmo!r}")
        if intake_blocks_dispatch(self.config, mgr.instance):
            self._record(row.id, "skipped")
            return []
        async with self._lock(row.id):
            if await self._in_flight(mgr, row.id):
                raise CronBusy(f"cron {row.id} already in flight on {row.pmo}")
            created = await self._create_ticket(mgr, row)
            self._record(row.id, "created")
            return [created]

    async def _fire_memory_curator(self, row: CronJob, *,
                                   automatic: bool) -> list[dict]:
        M = memory_bound_names(self.config, self.dev_types)
        created: list[dict] = []
        self.no_board = {m for m in self.no_board if m in M}
        for m in sorted(M):
            boards = [p for p in self.config.pmos if list(p.repos) == [m]]
            if not boards:
                self.no_board.add(m)
                log.warning("memory_curator_no_board:%s", m)
                continue
            self.no_board.discard(m)
            if automatic:
                depth = await self._claims_depth(m)
                if depth == 0:
                    self._record(row.id, "skipped")
                    continue
            for inst in boards:
                mgr = self.managers.get(inst.name)
                if mgr is None:
                    continue
                if intake_blocks_dispatch(self.config, inst):
                    self._record(row.id, "skipped")
                    continue
                lock_key = f"{row.id}:{inst.name}"
                async with self._lock(lock_key):
                    if await self._in_flight(mgr, row.id):
                        self._record(row.id, "skipped")
                        continue
                    created.append(await self._create_ticket(
                        mgr, row, force_stage="EXECUTE"))
                    self._record(row.id, "created")
        return created

    async def _claims_depth(self, card: str) -> int:
        """Skip-if-empty must be confirmed by listing (PLAN_MEMORY §6.3) —
        the cache alone goes stale the moment a drain PR merges, and a
        trusted stale >0 would fire an empty Curator ticket every interval.
        refresh_depth falls back to the cache when the listing fails."""
        if self.claims is None:
            return claims_mod.claims_depth.get(card, 0)
        return await claims_mod.refresh_depth(self.claims, card)

    async def _in_flight(self, mgr, job_id: str) -> bool:
        """A previous cron-created ticket for this job still non-terminal."""
        try:
            missions = await mgr.pmo.list_all(mgr.instance.team_key)
        except Exception:  # noqa: BLE001 — treat unread as not in-flight
            log.exception("cron in-flight scan failed on %s", mgr.instance_name)
            return False
        marker = cron_marker(job_id)
        for m in missions:
            if m.status in ("done", "canceled"):
                continue
            body = getattr(m, "description", "") or ""
            if marker in body:
                return True
        return False

    async def _create_ticket(self, mgr, row: CronJob, *,
                             force_stage: str | None = None) -> dict:
        stage = force_stage or row.entry_stage
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        title = f"[cron:{row.id}] {today}"
        ts = datetime.now(timezone.utc).isoformat()
        body = defang_template(row.description_template).replace(
            "{timestamp}", ts)
        footer = cron_marker(row.id)
        if footer not in body:
            body = body.rstrip() + "\n\n" + footer
        labels = {LABEL_OPTIN}
        if stage != "ONBOARD":
            labels.add(_STAGE_LABEL[stage])
        await mgr.pmo.ensure_labels(mgr.instance.team_key, labels)
        key, pmo_id = await mgr.pmo.create_mission(
            mgr.instance.team_key, title, body, "normal", labels)
        log.info("cron %s created %s on %s", row.id, key, mgr.instance_name)
        return {"pmo": mgr.instance_name, "key": key, "pmo_id": pmo_id}


