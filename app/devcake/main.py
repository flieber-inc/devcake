"""DevCake main app — M1 scope: dispatch mechanics with the stub Dev (docs/16 M1).

Loops: stub poll cycle (M0), ingress consumer, watchdog. Endpoints: health,
run history, and the M1 debug dispatch.
"""

import asyncio
import contextlib
import logging
import os

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from .dagu import DAGU_URL, DaguExecutor, DuplicateRun
from .messaging import Messaging
from .runs import RunManager
from .state import RunStore
from .telemetry import OO_URL, setup_telemetry
from .watchdog import watchdog_loop

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("devcake")

POLL_INTERVAL = int(os.environ.get("DEVCAKE_POLL_INTERVAL_SECONDS", "30"))
REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

tracer = setup_telemetry()

store = RunStore()
messaging = Messaging(REDIS_URL, REDIS_PASSWORD)
executor = DaguExecutor()
manager = RunManager(store, messaging, executor)


async def poll_loop() -> None:
    """M0 stub of the orchestrator poll cycle (docs/04 §1); real derivation at M2."""
    cycle = 0
    while True:
        cycle += 1
        with tracer.start_as_current_span("poll.cycle") as span:
            span.set_attribute("devcake.poll.cycle", cycle)
            span.set_attribute("devcake.missions.seen", 0)
            span.set_attribute("devcake.missions.candidates", 0)
            span.set_attribute("devcake.missions.dispatched", 0)
        await asyncio.sleep(POLL_INTERVAL)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    tasks = [
        asyncio.create_task(poll_loop()),
        asyncio.create_task(messaging.consume_forever(manager.handle, manager.verify_auth)),
        asyncio.create_task(watchdog_loop(manager)),
    ]
    yield
    for t in tasks:
        t.cancel()
    for t in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await t


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
            return (await client.get(url)).status_code < 500
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
        "config_valid": True,
        "circuit_breakers": {},
    }


@app.get("/api/v1/runs")
async def list_runs(limit: int = 50):
    runs = sorted(store.all(), key=lambda r: r.created_at, reverse=True)[:limit]
    return [
        r.model_dump(include={"run_id", "mission_key", "mission_type", "dev_type",
                              "seq", "state", "created_at", "started_at", "ended_at", "error"})
        for r in runs
    ]


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str):
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404)
    data = run.model_dump()
    data.pop("redis_password", None)   # never serve credentials
    data["spec_env"] = {k: ("«redacted»" if "SECRET" in k or "OTLP_BASIC" in k else v)
                        for k, v in data["spec_env"].items()}
    return data


@app.post("/api/v1/debug/dispatch-hello")
async def dispatch_hello(sleep: int = 3, payload_kb: int = 1,
                         timeout_seconds: int | None = None):
    """M1 debug endpoint (docs/16 M1). Removed when real dispatch lands at M3."""
    try:
        run = await manager.dispatch_hello(sleep, payload_kb, timeout_seconds)
    except DuplicateRun as e:
        raise HTTPException(409, f"duplicate dagRunId {e}")
    return {"run_id": run.run_id, "state": run.state}
