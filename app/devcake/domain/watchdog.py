"""Watchdog: timeout + liveness over active runs (docs/04 §5). No docker.sock —
kills go through Dagu's stop endpoint (verified: SIGTERM→SIGKILL→removal)."""

import asyncio
import logging
import os
import time
from datetime import timedelta

from .run import utcnow
from .runs import RunManager

log = logging.getLogger("devcake.watchdog")

CHECK_INTERVAL = 10
HEARTBEAT_GRACE = timedelta(
    seconds=int(os.environ.get("DEVCAKE_HEARTBEAT_GRACE_SECONDS", "300")))
STARTUP_GRACE = timedelta(seconds=90)  # dispatched runs must start within this
# extra wall-clock allowance past timeout_seconds before a finalizing run is
# even considered stalled — generous so crash+reclaim resumes are never raced
FINALIZE_STALL_GRACE_SECONDS = int(
    os.environ.get("DEVCAKE_FINALIZE_STALL_SECONDS", "3600"))
# the stall probe walks the whole ingress stream — once a minute is plenty for
# a condition with an hour-scale deadline; the 10 s loop stays cheap
STALL_CHECK_INTERVAL = 60


async def watchdog_loop(mgr: RunManager) -> None:
    last_stall_check = -float("inf")
    last_ws_sweep = -float("inf")
    while True:
        # ADR-0025 Hook G (periodic half): the sweep is the reclamation
        # guarantee behind every best-effort cleanup hook — kill races,
        # partial rmtrees, daemon-created husks. Cheap (one scandir + store
        # lookups), so the stall-check cadence is plenty.
        if time.monotonic() - last_ws_sweep >= STALL_CHECK_INTERVAL:
            last_ws_sweep = time.monotonic()
            try:
                mgr.workspaces.sweep(mgr.store)
            except Exception:
                log.exception("workspace sweep failed")
        try:
            stalled_finalizing = []
            for run in mgr.store.active():
                # finalizing = app-side PMO/forge work after the Dev has exited.
                # Never wall-clock-kill it while its artifacts entry can still
                # be redelivered: that would strand mid-finalize work
                # (especially after crash+reclaim), and artifact redelivery is
                # a no-op once timed_out. But once the entry is dead-lettered
                # (poison) or lost, nothing can ever finish the run — it would
                # block its mission and hold a concurrency slot forever — so
                # past a generous deadline it is failed without re-entering
                # finalize (restore_after_failure hands the mission back).
                if run.state == "finalizing":
                    age = (utcnow() - run.created_at).total_seconds()
                    if age > run.timeout_seconds + FINALIZE_STALL_GRACE_SECONDS:
                        stalled_finalizing.append(run)
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
            if stalled_finalizing and \
                    time.monotonic() - last_stall_check >= STALL_CHECK_INTERVAL:
                last_stall_check = time.monotonic()
                # fail-closed on the check itself: if redis is unreachable we
                # cannot prove the entry is gone, so leave the runs alone
                unresolved = await mgr.messaging.unresolved_run_ids()
                for run in stalled_finalizing:
                    if run.run_id in unresolved:
                        continue
                    # TOCTOU guard: finalize may have completed while we
                    # walked the stream — kill only a run that is STILL
                    # finalizing per the store, never a stale snapshot
                    fresh = mgr.store.get(run.run_id)
                    if fresh is None or fresh.state != "finalizing":
                        continue
                    await mgr.kill(
                        fresh, "failed",
                        "finalize stalled past deadline and its artifacts "
                        "entry is no longer on the ingress stream "
                        "(dead-lettered or lost) — nothing can resume it")
        except Exception:
            log.exception("watchdog cycle failed")
        await asyncio.sleep(CHECK_INTERVAL)
