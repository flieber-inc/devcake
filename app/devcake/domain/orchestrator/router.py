"""FinalizerRouter — the RunFinalizer adapter over N per-instance managers
(schema v3, docs/16 M9).

Every mission-run callback routes to its instance's MissionManager on
`run.pmo_ref`. A run whose instance vanished from config mid-flight fails
CLEANLY — persisted as failed with an explanatory error, never crashing the
ingress consumer (the poison/dead-letter path would otherwise eat the whole
stream). Legacy records (pre-v3 ""/"main" pmo_ref) route to the sole manager
when exactly one is configured.

HELLO/OAUTH runs never reach this router — they finalize inside RunManager.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

from ..run import Run, utcnow

if TYPE_CHECKING:
    from ...ports.messaging import MessagingPort
    from ...ports.state import StatePort
    from .manager import MissionManager

log = logging.getLogger("devcake.router")

_LEGACY_REFS = ("", "main")


class FinalizerRouter:
    def __init__(self, managers: dict[str, "MissionManager"], store: "StatePort",
                 messaging: "MessagingPort"):
        self._managers = managers   # LIVE reference — build_managers mutates it
        self._store = store
        self._messaging = messaging

    def _mgr(self, run: Run) -> "MissionManager | None":
        mgr = self._managers.get(run.pmo_ref)
        if mgr is None and run.pmo_ref in _LEGACY_REFS and len(self._managers) == 1:
            mgr = next(iter(self._managers.values()))
        return mgr

    def _orphan_error(self, run: Run) -> str:
        return (f"pmo instance {run.pmo_ref!r} is no longer configured — "
                f"run cannot finalize (configured: {sorted(self._managers)})")

    async def _fail(self, run: Run, op: str) -> None:
        log.error("%s for %s: %s", op, run.run_id, self._orphan_error(run))
        # tear down the run's Redis ACL user + reply stream like every other
        # terminal path — the run goes terminal here, so reconciliation
        # (which walks only ACTIVE runs) would never clean them (audit A8)
        with contextlib.suppress(Exception):
            await self._messaging.delete_run_user(run.run_id)
        with contextlib.suppress(Exception):
            await self._messaging.delete_reply_stream(run.run_id)
        run.state = "failed"
        run.error = self._orphan_error(run)
        # ADR-0018: this path does NOT funnel through RunManager._kill_inner, so
        # it stamps its own class. The state is "failed" but the condition is a
        # genuine orphan (the run's PMO instance is gone from config) — the
        # state/class mismatch is pre-existing and deliberate.
        run.error_class = "DEV_ORPHANED"
        run.ended_at = utcnow()
        self._store.save(run)

    async def finalize(self, run: Run, payload: dict) -> None:
        mgr = self._mgr(run)
        if mgr is None:
            await self._fail(run, "finalize")
            return
        await mgr.finalize(run, payload)

    async def finalize_mapper(self, run: Run, payload: dict) -> None:
        mgr = self._mgr(run)
        if mgr is None:
            await self._fail(run, "finalize_mapper")
            return
        await mgr.finalize_mapper(run, payload)

    async def restore_after_failure(self, run: Run) -> None:
        mgr = self._mgr(run)
        if mgr is None:
            # nothing to restore against — the failure is already recorded
            log.error("restore_after_failure skipped: %s", self._orphan_error(run))
            return
        await mgr.restore_after_failure(run)

    def runspec_secret_payload(self, run: Run) -> dict | None:
        mgr = self._mgr(run)
        return mgr.runspec_secret_payload(run) if mgr else None

    async def activity_payload(self, run: Run) -> dict:
        mgr = self._mgr(run)
        if mgr is None:
            return {"mission_md": "", "activity_md": "", "attachments": []}
        return await mgr.activity_payload(run.mission_pmo_id, run.pmo_kind)

    def dev_failure_error(self, run: Run, payload: dict) -> str:
        mgr = self._mgr(run)
        if mgr is None:
            return self._orphan_error(run)
        return mgr.dev_failure_error(run, payload)
