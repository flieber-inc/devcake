"""Cron HTTP application service (PLAN_MEMORY §6.4)."""

from __future__ import annotations

from fastapi import HTTPException

from ..domain.cron_service import CronBusy, CronUnconfigured
from ..ports.pmo import PMOTransient


async def list_crons(*, config):
    return {"crons": [c.model_dump() for c in config.crons]}


async def run_cron(job_id: str, *, cron) -> dict:
    if cron is None:
        raise HTTPException(503, "cron service is not available")
    try:
        created = await cron.fire(job_id, automatic=False)
    except CronUnconfigured as e:
        raise HTTPException(422, str(e))
    except CronBusy as e:
        raise HTTPException(409, str(e))
    except PMOTransient as e:
        # the tracker was busy or its request budget thin — same 502 family as
        # the mission actions, never a 500 with a traceback
        raise HTTPException(502, f"pmo transient error while creating the ticket: {e}")
    return {"created": [{"pmo": c["pmo"], "key": c["key"]} for c in created]}
