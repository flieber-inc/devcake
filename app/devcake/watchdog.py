"""Watchdog: timeout + liveness over active runs (docs/04 §5). No docker.sock —
kills go through Dagu's stop endpoint (verified: SIGTERM→SIGKILL→removal)."""

import asyncio
import logging
from datetime import timedelta

from .runs import RunManager
from .state import utcnow

log = logging.getLogger("devcake.watchdog")

CHECK_INTERVAL = 10
HEARTBEAT_GRACE = timedelta(minutes=5)


async def watchdog_loop(mgr: RunManager) -> None:
    while True:
        try:
            for run in mgr.store.active():
                age = (utcnow() - run.created_at).total_seconds()
                if age > run.timeout_seconds:
                    await mgr.kill(run, "timed_out", f"exceeded {run.timeout_seconds}s")
                    continue
                if (
                    run.state == "running"
                    and run.last_heartbeat
                    and utcnow() - run.last_heartbeat > HEARTBEAT_GRACE
                ):
                    status = await mgr.executor.status(run.run_id)
                    st = str((status or {}).get("status", "")).lower()
                    if status is None or st in ("failed", "aborted", "error", "cancelled"):
                        await mgr.kill(run, "failed", "heartbeat lost; dagu run not running")
        except Exception:
            log.exception("watchdog cycle failed")
        await asyncio.sleep(CHECK_INTERVAL)
