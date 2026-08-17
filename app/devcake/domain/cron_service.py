"""Cron module — one verb: create a labeled ticket (ADR-0035, docs/04 §1.8).

Reserved `memory-curator` fans out to every Curator board (repos == [m]
for each memory-bound card). Generic rows fire at `row.pmo`.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ..config import (AppConfig, CronJob, MEMORY_CURATOR_CRON_ID,
                      intake_blocks_dispatch, memory_bound_names)
from ..ports.cron import CronStore
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


class _MemoryCronLedger:
    """In-memory CronStore for tests/default wiring. Production injects
    the file-backed adapter so the outcome ledger survives restarts
    (ADR-0035; docs/10 cron_outcomes.json)."""

    def __init__(self):
        self._state: dict[str, dict] = {}

    def record(self, job_id: str, outcome: str, *,
               fired_at: str | None = None) -> None:
        row = self._state.setdefault(job_id, {})
        row["outcomes"] = (list(row.get("outcomes") or []) + [outcome])[-3:]
        if fired_at:
            row["last_fire_at"] = fired_at

    def outcomes(self, job_id: str) -> list[str]:
        return list(self._state.get(job_id, {}).get("outcomes") or [])

    def last_fire_at(self, job_id: str) -> datetime | None:
        raw = self._state.get(job_id, {}).get("last_fire_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None


class CronService:
    """Single-process cadence + Run-now. One lock per job id (and, for
    memory-curator, per target board as well). Degradation and the fire
    schedule are derived from the injected CronStore — restart-safe when
    wired with the file-backed adapter (api/services.py)."""

    def __init__(self, config: AppConfig, managers: dict[str, MissionManager],
                 claims=None, dev_types=None, store: CronStore | None = None):
        self.config = config
        self.managers = managers
        self.claims = claims
        # live Dev Type map — set M must include domain-bound notebooks
        # (ADR-0035: same set as claims/memory mounts), not just instance lists
        self.dev_types = dev_types if dev_types is not None else {}
        self.store: CronStore = (
            store if store is not None else _MemoryCronLedger())
        self._locks: dict[str, asyncio.Lock] = {}
        self.no_board: set[str] = set()  # memory_curator_no_board:<m>

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def job(self, job_id: str) -> CronJob | None:
        return next((c for c in self.config.crons if c.id == job_id), None)

    @property
    def degraded(self) -> set[str]:
        """Jobs whose last 3 automatic fires all failed (no ticket AND no
        successful skip-reason). Ledger-derived — restart-safe; a Run-now
        that creates a ticket records 'created' and clears it."""
        out: set[str] = set()
        for row in self.config.crons:
            outs = self.store.outcomes(row.id)
            if len(outs) == 3 and all(o == "failed" for o in outs):
                out.add(row.id)
        return out

    async def maybe_fire(self) -> None:
        """Interval path — one pass per poll cycle. Elapsed-time
        semantics off the ledger's last_fire_at: one attempt per interval
        window, restart-safe, and a stalled poll cycle can't skip a whole
        window (the old minute-multiple watermark could). Exceptions on
        one row never kill the cycle (docs/04 §1.8)."""
        now = datetime.now(timezone.utc)
        degraded = self.degraded
        for row in list(self.config.crons):
            if not row.enabled or row.id in degraded:
                continue
            try:
                last = self.store.last_fire_at(row.id)
                interval = max(row.interval_minutes, 1)
                if last is not None and now - last < timedelta(minutes=interval):
                    continue
                stamp = now.isoformat()
                try:
                    created = await self.fire(row.id, automatic=True)
                    self.store.record(
                        row.id, "created" if created else "skipped",
                        fired_at=stamp)
                except (CronBusy, CronUnconfigured):
                    self.store.record(row.id, "skipped", fired_at=stamp)
                except Exception:  # noqa: BLE001 — one row must not kill the cycle
                    log.exception("cron %s automatic fire failed", row.id)
                    self.store.record(row.id, "failed", fired_at=stamp)
            except Exception:  # noqa: BLE001 — ledger/window faults stay isolated
                log.exception("cron %s automatic pass failed", row.id)

    async def fire(self, job_id: str, *, automatic: bool) -> list[dict]:
        """Create tickets. `automatic=False` is Run now (F4: no empty skip).
        Automatic outcomes are recorded by maybe_fire (one per window); a
        manual fire records only its success, which clears degradation."""
        row = self.job(job_id)
        if row is None:
            raise CronUnconfigured(f"unknown cron {job_id!r}")
        if job_id == MEMORY_CURATOR_CRON_ID:
            created = await self._fire_memory_curator(row, automatic=automatic)
        else:
            created = await self._fire_generic(row)
        if not automatic and created:
            self.store.record(row.id, "created")
        return created

    async def _fire_generic(self, row: CronJob) -> list[dict]:
        if not row.pmo:
            raise CronUnconfigured(f"cron {row.id} has no target pmo")
        mgr = self.managers.get(row.pmo)
        if mgr is None:
            raise CronUnconfigured(f"cron {row.id}: no live manager for "
                                   f"pmo {row.pmo!r}")
        if intake_blocks_dispatch(self.config, mgr.instance):
            return []
        async with self._lock(row.id):
            if await self._in_flight(mgr, row.id):
                raise CronBusy(f"cron {row.id} already in flight on {row.pmo}")
            return [await self._create_ticket(mgr, row)]

    async def _fire_memory_curator(self, row: CronJob, *,
                                   automatic: bool) -> list[dict]:
        M = memory_bound_names(self.config, self.dev_types)
        created: list[dict] = []
        errors = 0
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
                    continue
            for inst in boards:
                mgr = self.managers.get(inst.name)
                if mgr is None:
                    continue
                if intake_blocks_dispatch(self.config, inst):
                    continue
                lock_key = f"{row.id}:{inst.name}"
                async with self._lock(lock_key):
                    if await self._in_flight(mgr, row.id):
                        continue
                    try:
                        created.append(await self._create_ticket(
                            mgr, row, force_stage="EXECUTE"))
                    except Exception:  # noqa: BLE001 — one board must not stop the fan-out
                        log.exception("memory-curator ticket failed on %s",
                                      inst.name)
                        errors += 1
        if errors and not created:
            raise RuntimeError(
                f"memory-curator fire failed on {errors} board(s)")
        return created

    async def _claims_depth(self, card: str) -> int:
        """Skip-if-empty must be confirmed by listing (ADR-0035) — the
        cache alone goes stale the moment a drain PR merges, and a
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
