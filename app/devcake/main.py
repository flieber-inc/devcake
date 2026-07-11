"""DevCake main app — M0 scope: observability spine only (docs/16 M0).

Runs the stub poll loop (emits `poll.cycle` spans; no PMO, no scheduling — that
arrives at M2/M3) and serves /api/v1/health for the admin panel's health strip.
"""

import asyncio
import contextlib
import logging
import os

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .telemetry import OO_URL, setup_telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("devcake")

POLL_INTERVAL = int(os.environ.get("DEVCAKE_POLL_INTERVAL_SECONDS", "30"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")
DAGU_URL = os.environ.get("DAGU_URL", "http://dagu:8080")

tracer = setup_telemetry()


async def poll_loop() -> None:
    """M0 stub of the orchestrator poll cycle (docs/04 §1).

    Emits one `poll.cycle` span per interval with zeroed counts so the
    trace pipeline is proven end-to-end before any business logic exists.
    """
    cycle = 0
    while True:
        cycle += 1
        with tracer.start_as_current_span("poll.cycle") as span:
            span.set_attribute("devcake.poll.cycle", cycle)
            span.set_attribute("devcake.missions.seen", 0)
            span.set_attribute("devcake.missions.candidates", 0)
            span.set_attribute("devcake.missions.dispatched", 0)
            log.info("poll.cycle %d (stub — M0)", cycle)
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(poll_loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


app = FastAPI(title="DevCake", lifespan=lifespan)
FastAPIInstrumentor.instrument_app(app)


async def _check_redis() -> bool:
    try:
        r = aioredis.from_url(REDIS_URL, password=REDIS_PASSWORD or None, socket_timeout=3)
        try:
            return bool(await r.ping())
        finally:
            await r.aclose()
    except Exception:
        return False


async def _check_http(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.get(url)
            return resp.status_code < 500
    except Exception:
        return False


@app.get("/api/v1/health")
async def health():
    redis_ok, dagu_ok, oo_ok = await asyncio.gather(
        _check_redis(),
        _check_http(f"{DAGU_URL}/api/v1/health"),
        _check_http(f"{OO_URL}/healthz"),
    )
    return {
        "app": True,
        "redis": redis_ok,
        "dagu": dagu_ok,
        "openobserve": oo_ok,
        "pmo": None,          # wired at M2
        "forge": None,        # wired at M4
        "config_valid": True, # config model arrives at M2
        "circuit_breakers": {},
    }
