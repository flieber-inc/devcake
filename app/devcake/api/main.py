"""DevCake main app — M1 scope: dispatch mechanics with the stub Dev (docs/16 M1).

Loops: stub poll cycle (M0), ingress consumer, watchdog. Endpoints: health,
run history, and the M1 debug dispatch.
"""

import asyncio
import contextlib
import logging
import os
import time

import httpx
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from ..adapters.dagu import DAGU_URL, DaguExecutor, DuplicateRun
from ..adapters.files import RunLogStore, RunStore
from ..adapters.github import GitHubForge
from ..adapters.gitlab import GitLabForge
from ..adapters.linear import LinearAdapter
from ..adapters.redis import Messaging
from ..config import (AppConfig, Assignment, DevType, deep_merge, delete_dev_type,
                      load_config, load_dev_types, migrate_config_patch,
                      save_config, save_dev_type)
from ..domain.model import ALL_LABELS, derive
from ..domain.oauth import OAuthManager
from ..domain.orchestrator import (MapperBusy, MapperService, MapperUnconfigured,
                                   MissionManager)
from ..domain.runs import RunManager
from ..domain.watchdog import watchdog_loop
from ..harness import HARNESSES, dev_type_status
from ..ports.pmo import PMOTransient
from ..telemetry import OO_URL, setup_telemetry

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
def _make_forge(cfg: AppConfig):
    """Adapter construction lives in the api layer — the domain receives the
    forge fully built and never imports adapter code (docs/01 §3)."""
    reviewer = os.environ.get(cfg.repo.reviewer_token_env or "") or None
    cls = GitLabForge if cfg.repo.forge == "gitlab" else GitHubForge
    return cls(cfg.repo.url, cfg.repo.token, reviewer)


pmo = LinearAdapter(config.api_key)
forge = _make_forge(config)
mission_mgr = MissionManager(config, dev_types, pmo, forge, manager, messaging)
manager.mission_mgr = mission_mgr
oauth_mgr = OAuthManager(manager, messaging, dev_types)
manager.oauth_mgr = oauth_mgr
runlog = RunLogStore()
manager.runlog = runlog

# poll-cycle snapshot served by /api/v1/missions (advisory cache — INV-1)
missions_cache: list[dict] = []

mapper = MapperService(config, dev_types, mission_mgr)


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
                # the gate is a poll artifact, computed EVERY cycle — pause
                # freezes dispatch, never information (docs/04 §2)
                gate = await mission_mgr.gate_map(missions)
                await mission_mgr.sweeps(missions)   # merge + tracking sweeps (docs/04 §1)
                # intake pause (docs/11): no NEW dispatches — in-flight runs still
                # finalize (ingress consumer) and sweeps above keep running
                if config.intake_paused:
                    dispatched = 0
                else:
                    dispatched = await mission_mgr.schedule(missions, gate)
                    await mapper.maybe_dispatch(missions)
                mission_mgr.rotate_grace()
                # anomalies are advisory and per-mission: prune once terminal
                terminal = {m.pmo_id for m in missions
                            if m.status in ("done", "canceled")}
                for k in list(mission_mgr.anomalies):
                    if k in terminal:
                        del mission_mgr.anomalies[k]
                missions_cache.clear()
                id_to_key = {m.pmo_id: m.key for m in missions}
                missions_cache.extend({
                    "key": m.key, "kind": m.pmo_kind, "title": m.title,
                    "status": m.status, "priority": m.priority,
                    "labels": sorted(m.labels), "mission_type": d.mission_type,
                    "schedulable": d.schedulable,
                    # the blocked-by gate is a scheduler concern, not a derivation
                    # row — surface it here so the admin panel shows why (ADR-0007)
                    "reason": gate.get(m.pmo_id, d.reason),
                    "blocked_by": [id_to_key.get(b, b) for b in m.blocked_by],
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


# branch-protection probe (A2, docs/14): cached — /health is polled every 10 s
# by the SPA, the forge API needs at most one look every few minutes
_protection_cache: dict = {"ts": 0.0, "value": None}


async def _branch_protection():
    if time.monotonic() - _protection_cache["ts"] > 300 or _protection_cache["ts"] == 0:
        _protection_cache["ts"] = time.monotonic()
        try:
            _protection_cache["value"] = (
                await mission_mgr.forge.default_branch_protection()
                if config.repo.url else None)
        except Exception:
            _protection_cache["value"] = None
    return _protection_cache["value"]


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
        pmo_ok = (await pmo.health_probe(config.pmo.team_key)).ok
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
        "intake_paused": config.intake_paused,
        "active_runs": len(store.active()),
        "forge_protection": await _branch_protection(),
        "anomalies": mission_mgr.anomalies,
        "merge_handoffs": mission_mgr.merge_handoffs,
        "needs_human": mission_mgr.needs_human,
        "dependency_cycles": mission_mgr.cycles,
        "mapper_degraded": mapper.degraded(),
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
                                  "ended_at", "error", "verdict"})
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


TERMINAL_STATES = {"finished", "failed", "timed_out", "orphaned"}


@app.get("/api/v1/runs/{run_id}/log")
async def get_run_log(run_id: str, tail: int | None = None):
    """Condensed harness output relayed live by the Dev (docs/11 §2a)."""
    if store.get(run_id) is None:
        raise HTTPException(404)
    _seq, text = runlog.read(run_id, tail)
    return PlainTextResponse(text)


@app.get("/api/v1/runs/{run_id}/log/stream")
async def stream_run_log(run_id: str):
    """SSE follow: replays the stored log, then live lines until the run ends.
    X-Accel-Buffering disables nginx proxy buffering for this response."""
    if store.get(run_id) is None:
        raise HTTPException(404)

    def is_terminal() -> bool:
        r = store.get(run_id)
        return r is None or r.state in TERMINAL_STATES

    return StreamingResponse(
        runlog.stream(run_id, is_terminal),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/v1/system/clear-runs")
async def clear_runs():
    """Operator wipe: local run state + Dagu history + OpenObserve data.

    Config, secrets, and everything in the PMO/forge are untouched (INV-1).
    In-flight Devs are stopped first. See docs/11 §3 and docs/10 §5.
    """
    from .clear import clear_all
    with tracer.start_as_current_span("system.clear_runs") as span:
        result = await clear_all(store, executor, messaging, runlog)
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
        body = migrate_config_patch(body, config)
        merged = AppConfig.model_validate(deep_merge(config.model_dump(), body))
    except Exception as e:
        raise HTTPException(422, str(e))
    rm = merged.relations_mapper
    if rm.enabled and (not rm.dev_type or rm.dev_type not in dev_types):
        raise HTTPException(422, "relations_mapper.dev_type must name an existing "
                                 "Dev Type when the mapper is enabled")
    if rm.interval_minutes < 1:
        raise HTTPException(422, "relations_mapper.interval_minutes must be ≥ 1")
    for field in merged.model_fields:
        setattr(config, field, getattr(merged, field))
    save_config(config)
    mission_mgr.forge = _make_forge(config)  # hot reload after repo changes
    return config.model_dump()


@app.get("/api/v1/harnesses")
async def list_harnesses():
    """The harness registry — image + credential requirements per
    harness_template. Read-only; the admin Dev Type card derives its display
    (including previews of unsaved harness switches) from this."""
    return {name: {"docker_image": h.image,
                   "credential_env": h.credential_env,
                   "credential_files": [cf.model_dump() for cf in h.credential_files],
                   "oauth_available": h.oauth is not None}
            for name, h in HARNESSES.items()}


@app.get("/api/v1/dev-types")
async def list_dev_types():
    return [dev_type_status(d) for d in dev_types.values()]


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
    if config.relations_mapper.dev_type == name:
        raise HTTPException(409, f"{name} is the Relations Mapper's Dev Type — "
                                 "repoint or disable the mapper first")
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


@app.get("/api/v1/env-check")
async def env_check(names: str = ""):
    """Set/unset status (never values) for comma-separated env var names —
    powers the admin Config tab's inline ✓/✗ next to *_env fields."""
    return {n: bool(os.environ.get(n, "").strip()) for n in names.split(",") if n}


@app.post("/api/v1/connections/pmo/test")
async def test_pmo():
    if not config.api_key:
        return {"ok": False, "error": f"env var {config.pmo.api_key_env} is empty or "
                                      "unset in DevCake's environment — put the API key "
                                      "in .env and restart"}
    try:
        h = await pmo.health_probe(config.pmo.team_key)
        missions = await pmo.list_all(config.pmo.team_key)
        return {"ok": h.ok, "team": h.workspace or config.pmo.team_key,
                "labels": h.managed_labels_present,
                "labels_expected": h.managed_labels_expected,
                "missions_visible": len(missions)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/v1/connections/forge/test")
async def test_forge():
    if not config.repo.token:
        return {"ok": False, "error": f"env var {config.repo.token_env} is empty or "
                                      "unset in DevCake's environment — the field wants "
                                      "the env var NAME; the token itself goes in .env"}
    try:
        import httpx as _hx
        f = mission_mgr.forge
        pr = await f.get_pr_by_branch("devcake/__connection_test__")
        reviewer = bool(getattr(f, "reviewer_token", None))
        protection = await f.default_branch_protection()
        return {"ok": True, "forge": config.repo.forge, "repo": config.repo.url,
                "reviewer_token_configured": reviewer, "probe_pr": pr is None,
                "branch_protection": protection}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/v1/relations-mapper/run")
async def run_mapper():
    """Manual trigger (docs/11): works regardless of the enabled toggle — the
    toggle governs only the periodic service. Requires a valid dev_type."""
    try:
        run = await mapper.run_now()
    except MapperUnconfigured as e:
        raise HTTPException(422, str(e))
    except MapperBusy as e:
        raise HTTPException(409, str(e))
    return {"run_id": run.run_id, "state": run.state}


# ── GUI OAuth helpers (docs/16 M6) ───────────────────────────────────────────

@app.post("/api/v1/oauth/dev-types/{name}/start")
async def oauth_start(name: str):
    """Per-dev-type device-code login: the credential lands in THIS Dev Type's
    /data/secrets dir (two Dev Types on one harness = two accounts)."""
    try:
        return await oauth_mgr.start(name)
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
