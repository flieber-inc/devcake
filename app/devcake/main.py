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

from .config import load_config
from .dagu import DAGU_URL, DaguExecutor, DuplicateRun
from .linear import LinearAdapter, PMOTransient
from .messaging import Messaging
from .pmo import ALL_LABELS, derive
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

config = load_config()
store = RunStore()
messaging = Messaging(REDIS_URL, REDIS_PASSWORD)
executor = DaguExecutor()
manager = RunManager(store, messaging, executor)
pmo = LinearAdapter(config.api_key)

# poll-cycle snapshot served by /api/v1/missions (advisory cache — INV-1)
missions_cache: list[dict] = []


async def poll_loop() -> None:
    """Poll cycle (docs/04 §1) — M2 scope: fetch + derive + cache; dispatch at M3."""
    cycle = 0
    while True:
        cycle += 1
        with tracer.start_as_current_span("poll.cycle") as span:
            span.set_attribute("devcake.poll.cycle", cycle)
            try:
                missions = await pmo.list_all(config.pmo.team_key)
                derived = [(m, derive(m, config.adoption_mode)) for m in missions]
                candidates = [m for m, d in derived if d.schedulable]
                missions_cache.clear()
                missions_cache.extend({
                    "key": m.key, "kind": m.pmo_kind, "title": m.title,
                    "status": m.status, "priority": m.priority,
                    "labels": sorted(m.labels), "mission_type": d.mission_type,
                    "schedulable": d.schedulable, "reason": d.reason,
                    "pmo_id": m.pmo_id,
                } for m, d in derived)
                span.set_attribute("devcake.missions.seen", len(missions))
                span.set_attribute("devcake.missions.candidates", len(candidates))
                span.set_attribute("devcake.missions.dispatched", 0)  # M3
                log.info("poll.cycle %d: %d missions, %d candidates",
                         cycle, len(missions), len(candidates))
            except PMOTransient as e:
                span.set_attribute("devcake.outcome", "PMO_TRANSIENT")
                log.warning("poll.cycle %d skipped: %s", cycle, e)
        await asyncio.sleep(config.poll_interval_seconds)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # startup reconciliation step 2 (docs/04 §6): ensure the nine managed labels
    try:
        await pmo.ensure_labels(config.pmo.team_key, ALL_LABELS)
        log.info("labels ensured in team %s", config.pmo.team_key)
    except Exception:
        log.exception("label bootstrap failed — poll loop will keep retrying reads")
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
    try:
        await pmo._team(config.pmo.team_key)
        pmo_ok = True
    except Exception:
        pmo_ok = False
    return {
        "app": True,
        "redis": redis_ok,
        "dagu": dagu_ok,
        "openobserve": oo_ok,
        "pmo": pmo_ok,
        "forge": None,        # wired at M4
        "config_valid": True,
        "circuit_breakers": {},
    }


@app.get("/api/v1/missions")
async def list_missions():
    """Current derived Missions (poll-cycle snapshot) — M2 exit criterion."""
    return {"team": config.pmo.team_key, "adoption_mode": config.adoption_mode,
            "missions": missions_cache}


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
