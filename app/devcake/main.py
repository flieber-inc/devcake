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

from .config import (AppConfig, Assignment, DevType, delete_dev_type, load_config,
                     load_dev_types, save_config, save_dev_type)
from .oauth import OAuthManager
from .dagu import DAGU_URL, DaguExecutor, DuplicateRun
from .linear import LinearAdapter, PMOTransient
from .messaging import Messaging
from .missions import MissionManager
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
dev_types = load_dev_types()
store = RunStore()
messaging = Messaging(REDIS_URL, REDIS_PASSWORD)
executor = DaguExecutor()
manager = RunManager(store, messaging, executor)
pmo = LinearAdapter(config.api_key)
mission_mgr = MissionManager(config, dev_types, pmo, manager, messaging)
manager.mission_mgr = mission_mgr
oauth_mgr = OAuthManager(manager, messaging, dev_types)
manager.oauth_mgr = oauth_mgr

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
                await mission_mgr.sweeps(missions)   # merge + tracking sweeps (docs/04 §1)
                dispatched = await mission_mgr.schedule(missions)
                mission_mgr.rotate_grace()
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
                span.set_attribute("devcake.missions.dispatched", dispatched)
                log.info("poll.cycle %d: %d missions, %d candidates, %d dispatched",
                         cycle, len(missions), len(candidates), dispatched)
            except PMOTransient as e:
                span.set_attribute("devcake.outcome", "PMO_TRANSIENT")
                log.warning("poll.cycle %d skipped: %s", cycle, e)
            except Exception:
                # a poll cycle must NEVER kill the loop — log and try again next tick
                span.set_attribute("devcake.outcome", "cycle_error")
                log.exception("poll.cycle %d failed", cycle)
        await asyncio.sleep(config.poll_interval_seconds)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    # startup reconciliation (docs/04 §6)
    try:
        await pmo.ensure_labels(config.pmo.team_key, ALL_LABELS)          # step 2
        log.info("labels ensured in team %s", config.pmo.team_key)
    except Exception:
        log.exception("label bootstrap failed — poll loop will keep retrying reads")
    for r in store.active():                                              # step 3
        try:
            status = await executor.status(r.run_id)
            st = str(((status or {}).get("dagRunDetails") or {}).get("status", "")).lower()
            label = str(((status or {}).get("dagRunDetails") or {}).get("statusLabel", "")).lower()
            if status is None or st in ("failed", "aborted", "error")                     or label in ("failed", "aborted", "error", "cancelled"):
                await manager.kill(r, "orphaned", "reconciliation: dagu run not alive")
            else:
                log.info("reconciliation: adopted in-flight run %s (dagu: %s)",
                         r.run_id, label or st or "running")
        except Exception:
            log.exception("reconciliation failed for %s", r.run_id)
    try:
        await messaging.reclaim_pending(manager.handle, manager.verify_auth)  # step 4
    except Exception:
        log.exception("pending-entry reclaim failed")
    def _log_task_death(t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception():
            log.error("background task %s DIED", t.get_name(), exc_info=t.exception())

    tasks = [
        asyncio.create_task(poll_loop(), name="poll_loop"),
        asyncio.create_task(messaging.consume_forever(manager.handle, manager.verify_auth),
                            name="ingress_consumer"),
        asyncio.create_task(watchdog_loop(manager), name="watchdog"),
    ]
    for t in tasks:
        t.add_done_callback(_log_task_death)
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
        "circuit_breakers": mission_mgr.breakers,
    }


@app.get("/api/v1/missions")
async def list_missions():
    """Current derived Missions (poll-cycle snapshot) — M2 exit criterion."""
    return {"team": config.pmo.team_key, "adoption_mode": config.adoption_mode,
            "missions": missions_cache}


@app.get("/api/v1/runs")
async def list_runs(limit: int = 25, offset: int = 0, mission_key: str | None = None):
    runs = sorted(store.all(), key=lambda r: r.created_at, reverse=True)
    if mission_key:
        needle = mission_key.strip().upper()
        runs = [r for r in runs if needle in r.mission_key.upper()
                or needle in r.run_id.upper()]
    total = len(runs)
    page = [r.model_dump(include={"run_id", "mission_key", "mission_type", "dev_type",
                                  "seq", "state", "created_at", "started_at",
                                  "ended_at", "error"})
            for r in runs[offset:offset + limit]]
    return {"total": total, "offset": offset, "limit": limit, "runs": page}


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


@app.post("/api/v1/system/clear-runs")
async def clear_runs():
    """Operator wipe: local run state + Dagu history + OpenObserve data.

    Config, secrets, and everything in the PMO/forge are untouched (INV-1).
    In-flight Devs are stopped first. See docs/11 §3 and docs/10 §5.
    """
    from .clear import clear_all
    with tracer.start_as_current_span("system.clear_runs") as span:
        result = await clear_all(store, executor, messaging)
        missions_cache.clear()
        mission_mgr._grace.clear()
        mission_mgr._grace_next.clear()
        # auth breakers stay — they reflect live credential health, not run history
        span.set_attribute("devcake.clear.runs_deleted",
                           int((result.get("local") or {}).get("runs_deleted") or 0))
        span.set_attribute("devcake.clear.dagu_deleted",
                           int((result.get("dagu") or {}).get("deleted") or 0))
        span.set_attribute("devcake.clear.ok", bool(result.get("ok")))
        return result


# ── Config CRUD (docs/11 §1; writes validate once here, hot-apply next cycle) ──

@app.get("/api/v1/config")
async def get_config():
    data = config.model_dump()
    return data


@app.put("/api/v1/config")
async def put_config(body: dict):
    global config
    try:
        merged = AppConfig.model_validate({**config.model_dump(), **body})
    except Exception as e:
        raise HTTPException(422, str(e))
    for field in merged.model_fields:
        setattr(config, field, getattr(merged, field))
    save_config(config)
    mission_mgr.reload_forge()
    return config.model_dump()


@app.get("/api/v1/dev-types")
async def list_dev_types():
    return [d.model_dump() for d in dev_types.values()]


@app.post("/api/v1/dev-types")
@app.put("/api/v1/dev-types/{name}")
async def upsert_dev_type(body: dict, name: str | None = None):
    try:
        dt = DevType.model_validate(body if name is None else {**body, "name": name})
    except Exception as e:
        raise HTTPException(422, str(e))
    dev_types[dt.name] = dt
    save_dev_type(dt)
    return dt.model_dump()


@app.delete("/api/v1/dev-types/{name}")
async def remove_dev_type(name: str):
    if any(a.dev_type == name for a in config.assignments.values()):
        raise HTTPException(409, f"{name} is assigned to a mission type")
    dev_types.pop(name, None)
    delete_dev_type(name)
    return {"deleted": name}


@app.post("/api/v1/dev-types/{name}/credentials")
async def upload_credentials(name: str, body: dict):
    """{"filename": "...", "content": "..."} → /data/secrets/{name}/ (0600)."""
    if name not in dev_types:
        raise HTTPException(404)
    from pathlib import Path
    target = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets" / name
    target.mkdir(parents=True, exist_ok=True)
    fname = os.path.basename(body.get("filename") or "creds.json")
    p = target / fname
    p.write_text(body.get("content") or "")
    p.chmod(0o600)
    mission_mgr.breakers.pop(name, None)   # fresh credential clears the breaker
    return {"stored": f"{name}/{fname}"}


@app.get("/api/v1/assignments")
async def get_assignments():
    return {k: v.model_dump() for k, v in config.assignments.items()}


@app.put("/api/v1/assignments")
async def put_assignments(body: dict):
    try:
        new = {k: Assignment.model_validate(v) for k, v in body.items()}
    except Exception as e:
        raise HTTPException(422, str(e))
    missing = {"ONBOARD", "PLAN", "EXECUTE", "REVIEW"} - set(new)
    if missing:
        raise HTTPException(422, f"unassigned mission types: {sorted(missing)}")
    unknown = {a.dev_type for a in new.values()} - set(dev_types)
    if unknown:
        raise HTTPException(422, f"unknown dev types: {sorted(unknown)}")
    config.assignments = new
    save_config(config)
    return {k: v.model_dump() for k, v in config.assignments.items()}


@app.post("/api/v1/connections/pmo/test")
async def test_pmo():
    try:
        team = await pmo._team(config.pmo.team_key)
        missions = await pmo.list_all(config.pmo.team_key)
        return {"ok": True, "team": config.pmo.team_key,
                "labels": len([l for l in team["labels"]["nodes"]
                               if l["name"].startswith("DEVCAKE")]),
                "missions_visible": len(missions)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/v1/connections/forge/test")
async def test_forge():
    try:
        import httpx as _hx
        f = mission_mgr.forge
        pr = await f.get_pr_by_branch("devcake/__connection_test__")
        reviewer = bool(getattr(f, "reviewer_token", None))
        return {"ok": True, "forge": config.repo.forge, "repo": config.repo.url,
                "reviewer_token_configured": reviewer, "probe_pr": pr is None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


# ── GUI OAuth helpers (docs/16 M6) ───────────────────────────────────────────

@app.post("/api/v1/oauth/{harness}/start")
async def oauth_start(harness: str):
    try:
        return await oauth_mgr.start(harness)
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/v1/oauth/status/{run_id}")
async def oauth_status(run_id: str):
    s = oauth_mgr.status(run_id)
    if s is None:
        raise HTTPException(404)
    return s


@app.post("/api/v1/debug/dispatch-hello")
async def dispatch_hello(sleep: int = 3, payload_kb: int = 1,
                         timeout_seconds: int | None = None):
    """M1 debug endpoint (docs/16 M1). Removed when real dispatch lands at M3."""
    try:
        run = await manager.dispatch_hello(sleep, payload_kb, timeout_seconds)
    except DuplicateRun as e:
        raise HTTPException(409, f"duplicate dagRunId {e}")
    return {"run_id": run.run_id, "state": run.state}
