"""Watchdog: timeout + liveness over active runs (docs/04 §5). No docker.sock —
kills go through Dagu's stop endpoint (verified: SIGTERM→SIGKILL→removal)."""

import asyncio
import logging
import os
from datetime import timedelta

from .run import utcnow
from .runs import RunManager

log = logging.getLogger("devcake.watchdog")

CHECK_INTERVAL = 10
HEARTBEAT_GRACE = timedelta(
    seconds=int(os.environ.get("DEVCAKE_HEARTBEAT_GRACE_SECONDS", "300")))
STARTUP_GRACE = timedelta(seconds=90)  # dispatched runs must start within this


async def watchdog_loop(mgr: RunManager) -> None:
    while True:
        try:
            for run in mgr.store.active():
                # finalizing = app-side PMO/forge work after the Dev has exited.
                # Never wall-clock-kill it: that would strand mid-finalize work
                # (especially after crash+reclaim), and artifact redelivery is a
                # no-op once timed_out. Finalize has its own failure paths.
                if run.state == "finalizing":
                    continue
                age = (utcnow() - run.created_at).total_seconds()
                if age > run.timeout_seconds:
                    await mgr.kill(run, "timed_out", f"exceeded {run.timeout_seconds}s")
                    continue
                # reference = last heartbeat, else run start: a Dev killed before its
                # first heartbeat must not be invisible until the wall-clock timeout
                beat_ref = run.last_heartbeat or run.started_at
                stale_running = (run.state == "running" and beat_ref
                                 and utcnow() - beat_ref > HEARTBEAT_GRACE)
                dead_before_start = (run.state == "dispatched"
                                     and utcnow() - run.created_at > STARTUP_GRACE)
                if stale_running or dead_before_start:
                    status = await mgr.executor.status(run.run_id)
                    detail = ((status or {}).get("dagRunDetails") or {})
                    label = str(detail.get("statusLabel", detail.get("status", ""))).lower()
                    if status is None or any(t in label for t in
                                             ("failed", "aborted", "error", "cancel")):
                        await mgr.kill(run, "failed",
                                       "dagu run dead (%s)" %
                                       ("no heartbeat" if stale_running else "never started"))
        except Exception:
            log.exception("watchdog cycle failed")
        await asyncio.sleep(CHECK_INTERVAL)
