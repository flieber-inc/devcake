"""DevCake main app: wiring, lifespan (startup reconciliation), and the API.

Loops: poll cycle (derive + dispatch + sweeps), ingress consumer, watchdog.
Endpoints: health, config, connections, missions, runs, credentials/OAuth,
and the hello debug dispatch (permanent CI fixture — scripts/ci_suite.sh).
"""

import asyncio
import base64
import contextlib
import logging
import os
import re
import time
from datetime import datetime, timezone

import httpx
import yaml
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
from pydantic import BaseModel
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from ..adapters.dagu import DAGU_URL, DaguExecutor, DuplicateRun
from ..adapters.files import RunLogStore, RunStore
from ..adapters.redis import Messaging
from ..adapters.registry import make_forge, make_internal_forge, make_pmo
from .. import profiles as profiles_store
from .. import secrets as secrets_store
from .. import security
from ..settings_bundle import (BundleError, MAX_BUNDLE_BYTES, SETUP_ENV_VARS,
                               apply_bundle, audit_event, diff_bundle,
                               dry_run_adapters, generate_env_file,
                               protect_bundle, serialize_current,
                               unprotect_bundle, validate_bundle,
                               validate_config_semantics)
from ..settings_crypto import MIN_PASSPHRASE_LEN, DecryptError
from ..config import (HARNESS_VAR_PATTERN, AppConfig, Assignment, DevType,
                      _INSTANCE_NAME_RE, deep_merge, delete_dev_type,
                      load_config, load_dev_types, reject_stale_patch,
                      save_config, save_dev_type)
from ..domain.blocker_locator import BlockerLocator
from ..domain.model import ALL_LABELS, derive
from ..domain.oauth import OAuthManager
from ..domain.orchestrator import (FinalizerRouter, MapperBusy, MapperService,
                                   MapperUnconfigured, MissionManager)
from ..domain.forge_runtime import ForgeRuntime
from ..domain.reconcile import reconcile_runs
from ..domain.repo_mirror import RepoCache
from ..domain.workspaces import WorkspaceStore
from ..domain.runs import RunManager
from ..domain.skills import SkillService, SkillStoreError
from ..domain.watchdog import watchdog_loop
from ..harness import HARNESSES, dev_type_status
from ..ports.forge import mission_branch
from ..prompts import templates as prompt_templates
from ..ports.pmo import PMOTransient
from ..telemetry import OO_URL, setup_telemetry
from ..telemetry.oo_provision import ensure_oo_ingest_user_at_boot
from .auth import credentials_configured, enforce_control_plane_auth
from . import (connections_service, devtypes_service, internal_repos_service,
               profiles_service, settings_transfer)
from .config_service import apply_config_patch, set_pmo_intake
from .health import build_health_payload, reset_protection_cache
from .poll import PollRuntime

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("devcake")

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")

tracer = setup_telemetry()

config = load_config()
dev_types = load_dev_types()
prompt_templates.seed_default_templates()   # /data defaults (v0.1.1)
prompt_templates.seed_devtype_prompts(dev_types)   # per-dev-type (2026-07-15)
store = RunStore()
messaging = Messaging(REDIS_URL, REDIS_PASSWORD)
executor = DaguExecutor()
# ADR-0025: per-run workspace tree on the host bind (compose mounts
# $DEVCAKE_WS_HOST here rw; the dev-run DAG binds per-run subdirs)
workspaces = WorkspaceStore("/workspaces")
manager = RunManager(store, messaging, executor, workspaces=workspaces)

# ── multi-instance wiring (schema v3, ADR-0009; repos plural per M10) ───────
# One MissionManager per CONFIGURED PMO instance; shared state is injected:
# the RunManager/store (global concurrency), ONE dev-type breakers dict
# (credentials are DevCake-global), and ONE ForgeRuntime (repos belong to
# the deployment — missions from any instance can route to any repo).
shared_breakers: dict[str, str] = {}
# dev_type → reason (ADR-0018). Same injected-shared-dict idiom as
# shared_breakers, but a DIFFERENT thing: it throttles a dev type to one probe
# run when its model backend looks sick, and clears itself when runs succeed.
# PollRuntime is the sole writer; managers only read it.
shared_backend_degraded: dict[str, str] = {}
managers: dict[str, MissionManager] = {}
mappers: dict[str, MapperService] = {}
forge_runtime = ForgeRuntime()
# ADR-0024: the ONE deployment-wide source mirror (repos are deployment-
# global, so the mirror is too — same shared-runtime idiom as forge_runtime)
repo_cache = RepoCache(config, forge_runtime)
# the bundled internal fallback forge (M11): provisioner is admin-credentialed
# (GITEA_ADMIN_*); None disables the zero-repo un-gating when Gitea is absent
internal_forge = make_internal_forge() if os.environ.get("GITEA_ADMIN_PASSWORD") else None
# skill store (docs/16 skill store v1): store-first reads via the internal
# forge; bundled copies keep built-in skills working forge-less
skill_service = SkillService(internal_forge)


def _mission_owner_of(bid: str) -> str | None:
    """PollRuntime's durable claim map, read at call time — `poll_rt` is
    defined below (module-global late binding: blocker resolution only runs
    inside poll/dispatch, long after startup)."""
    return poll_rt.mission_owner.get(bid)


# Deployment-wide blocker resolution (ADR-0009 amendment): ONE locator over
# the LIVE managers dict, so a native blocked_by edge to a peer instance's
# mission gates and inherits exactly like a local one (Linear v1; read-only —
# never claims, never writes). Same live-reference pattern as FinalizerRouter.
blocker_locator = BlockerLocator(managers, _mission_owner_of)


def build_managers() -> None:
    """(Re)build the manager set IN PLACE to match config.pmos: existing
    managers keep their advisory state (grace, anomalies, merge windows) and
    get repointed adapters; removed instances drop theirs — never leaked."""
    live = {i.name: i for i in config.pmos if i.configured}
    for name in [n for n in managers if n not in live]:
        managers.pop(name)
        mappers.pop(name, None)
    for name, inst in live.items():
        p = make_pmo(inst)
        if name in managers:
            mgr = managers[name]
            mgr.pmo, mgr.forges, mgr.config = p, forge_runtime, config
            mgr.instance, mgr.instance_name = inst, name
            mgr.internal_forge = internal_forge
            mgr.skills = skill_service
            mgr.blocker_locator = blocker_locator
            mgr.repo_cache = repo_cache
        else:
            managers[name] = MissionManager(
                config, dev_types, p, forge_runtime, manager, messaging,
                instance=inst, breakers=shared_breakers,
                internal_forge=internal_forge, skills=skill_service,
                backend_degraded=shared_backend_degraded,
                blocker_locator=blocker_locator, repo_cache=repo_cache)
            mappers[name] = MapperService(config, dev_types, managers[name])


def _managers_in_config_order() -> list[MissionManager]:
    return [managers[i.name] for i in config.pmos if i.name in managers]


forge_runtime.rebuild(config.repos, make_forge)
build_managers()
router = FinalizerRouter(managers, store, messaging)
manager.set_finalizer(router)  # RunFinalizer seam: routes on run.pmo_ref


async def refresh_forge_health() -> dict[str, dict]:
    """Probe every configured repo; the runtime latches/clears per repo."""
    return await forge_runtime.refresh_all()


def _log_task_death(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception():
        log.error("background task %s DIED", t.get_name(), exc_info=t.exception())


def reload_connections() -> None:
    """Hot-reload adapters after a config PUT: rebuild the forge, reconcile
    the manager set, and re-ensure the managed labels per instance —
    bootstrap is otherwise startup-only, so a hot-swapped team_key would run
    unlabeled until restart."""
    forge_runtime.rebuild(config.repos, make_forge)
    build_managers()
    reset_protection_cache()               # repos may have changed — reprobe
    # the adapter set feeds secret_env_vars (descriptor env lists), so the
    # redaction scan cache must rebuild with it (2026-08 F17)
    security.invalidate_secret_scan()

    async def _ensure():
        for mgr in list(managers.values()):
            try:
                await mgr.pmo.ensure_labels(mgr.instance.team_key, ALL_LABELS)
            except Exception:
                log.exception("ensure_labels after config reload failed for "
                              "instance %s — ensured on next restart",
                              mgr.instance_name)
        await refresh_forge_health()
    asyncio.create_task(_ensure(), name="ensure_labels_reload") \
        .add_done_callback(_log_task_death)
oauth_mgr = OAuthManager(manager, messaging, dev_types,
                         breakers=shared_breakers)
manager.oauth_mgr = oauth_mgr
runlog = RunLogStore()
manager.runlog = runlog

# The poll machinery (ownership claims, cycle driver, missions cache, loop)
# lives in api/poll.py — constructed ONCE with live references and never
# rebuilt on config reload (ADR-0015 Decision 2). `poll_rt.missions_cache`
# keeps one stable list identity; /api/v1/missions serves it by reference.
poll_rt = PollRuntime(
    config=config, managers=managers, mappers=mappers, store=store,
    forge_runtime=forge_runtime, refresh_forge_health=refresh_forge_health,
    managers_in_config_order=_managers_in_config_order,
    backend_degraded=shared_backend_degraded, repo_cache=repo_cache)


def _refuse_insecure_passwords() -> None:
    """Refuse empty/default infra passwords unless DEVCAKE_ALLOW_INSECURE=1
    (ISSUES #18)."""
    if os.environ.get("DEVCAKE_ALLOW_INSECURE", "").strip() in ("1", "true", "yes"):
        log.warning("DEVCAKE_ALLOW_INSECURE set — skipping password strength checks")
        return
    weak = {"", "change-me", "change-me-too", "change-me-as-well", "password", "admin"}
    checks = {
        "ADMIN_PASSWORD": os.environ.get("ADMIN_PASSWORD", ""),
        "REDIS_PASSWORD": os.environ.get("REDIS_PASSWORD", ""),
        "DAGU_PASSWORD": os.environ.get("DAGU_PASSWORD", ""),
        "OO_ROOT_PASSWORD": os.environ.get("OO_ROOT_PASSWORD", ""),
        # required since M8: the collector, fluentbit, and push_oo_log all
        # authenticate with the OO service account (ISSUES #13)
        "OO_INGEST_PASSWORD": os.environ.get("OO_INGEST_PASSWORD", ""),
        # the internal fallback forge's admin (M11) — the sharpest credential
        # on the runtime network; weak/empty must refuse boot like the rest
        "GITEA_ADMIN_PASSWORD": os.environ.get("GITEA_ADMIN_PASSWORD", ""),
    }
    bad = [name for name, val in checks.items() if (val or "").strip() in weak]
    if bad:
        raise RuntimeError(
            f"refusing to start with empty/default passwords for: {', '.join(bad)}. "
            f"Set strong values in .env, or DEVCAKE_ALLOW_INSECURE=1 for local sandbox only.")
    # ADMIN_PASSWORD gets a LENGTH floor on top of the deny-list (2026-08
    # evaluation, docs/14 §11 backlog item): admin basic auth is
    # host-root-equivalent — one authenticated settings-export request
    # returns DAGU_PASSWORD, and dagu holds docker.sock — yet a 3-char
    # password booted fine. 12 is a floor, not advice; the deny-list alone
    # cannot express it. DELIBERATE BREAKING CHANGE for short-password
    # deployments: fail-loudly at boot with the exact remedy (pre-v1
    # doctrine, release-noted).
    if len(os.environ.get("ADMIN_PASSWORD", "").strip()) < 12:
        raise RuntimeError(
            "ADMIN_PASSWORD must be at least 12 characters — it is "
            "host-root-equivalent (settings export carries the Dagu "
            "credential, and Dagu holds docker.sock — docs/14 §3). Set a "
            "longer value in .env, or DEVCAKE_ALLOW_INSECURE=1 for local "
            "sandbox only.")
    # the password check above can't catch a blank USER half of the OO
    # service account — an empty email encodes ':password' and 401s every
    # telemetry write silently (collector, fluentbit, log-push)
    if not os.environ.get("OO_INGEST_EMAIL", "").strip():
        raise RuntimeError(
            "OO_INGEST_EMAIL must be set (the OO service account — ISSUES #13). "
            "Set it in .env alongside OO_INGEST_PASSWORD; app boot creates the "
            "user in OpenObserve (scripts/provision_oo.py still provisions "
            "dashboard/alerts).")


def _security_warnings() -> list[dict]:
    """Credential-posture warnings — body lives in security.security_warnings
    so the copy is testable without this module's singletons (F1 tripwire)."""
    return security.security_warnings(config)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    if not credentials_configured():
        raise RuntimeError("ADMIN_USER and ADMIN_PASSWORD must both be configured")
    _refuse_insecure_passwords()
    secrets_store.register_all()   # redaction coverage for GUI-stored secrets (M12)
    log.info("boot: schema_version=%s dagu pull_policy=missing image tags "
             "devcake/dev-*:%s — re-run `docker buildx bake all` lockstep "
             "with app upgrades", config.schema_version,
             os.environ.get("DEVCAKE_TAG", "latest"))
    for warn in _security_warnings():   # loud at boot; dismissable in the SPA
        log.warning("%s — %s", warn["title"], warn["body"])
    # OpenObserve ingest service account (ISSUES #13): non-negotiable.
    # Compose only seeds root; boot creates/resyncs OO_INGEST_* so telemetry
    # never 401s after a fresh volume. Retries while OO is still starting.
    status = await ensure_oo_ingest_user_at_boot()
    log.info("openobserve: ingest user %s", status)
    # corrupt run records must never wedge boot; a quarantined record is
    # FORGOTTEN, so best-effort teardown of anything it may have left live
    # (container, per-run ACL user, reply stream) — the run id is the handle
    for run_id in store.quarantine_unreadable():
        with contextlib.suppress(Exception):
            await executor.stop(run_id)
        with contextlib.suppress(Exception):
            await messaging.delete_run_user(run_id)
        with contextlib.suppress(Exception):
            await messaging.delete_reply_stream(run_id)
        # ADR-0025 Hook F: a quarantined record is FORGOTTEN — its dir would
        # otherwise only fall to the sweep's record-absent predicate later
        with contextlib.suppress(Exception):
            workspaces.cleanup(run_id)
    # internal fallback forge (M11): provision org + service accounts. Best
    # effort — a Gitea outage degrades only zero-repo missions, never boot
    if internal_forge is not None:
        try:
            await internal_forge.ensure_service_accounts()
            log.info("internal forge: service accounts ensured")
        except Exception:
            log.exception("internal forge provisioning failed — zero-repo "
                          "missions will retry once Gitea is reachable")
        try:
            await internal_forge.ensure_skill_store(skill_service.builtin_seed())
            log.info("internal forge: skill store ensured")
        except Exception:
            log.exception("skill store seeding failed — skills fall back to "
                          "bundled copies (POST /api/v1/skills/sync re-seeds)")
    # startup reconciliation (docs/04 §6) — labels per configured instance
    for mgr in list(managers.values()):
        try:
            await mgr.pmo.ensure_labels(mgr.instance.team_key, ALL_LABELS)   # step 2
            log.info("labels ensured in team %s (instance %s)",
                     mgr.instance.team_key, mgr.instance_name)
        except Exception:
            log.exception("label bootstrap failed for instance %s — poll loop "
                          "will keep retrying reads", mgr.instance_name)
    # forge sweep deliberately NOT awaited here (incident 2026-08-01: it held
    # the listen socket O(N repos) and failed the compose healthcheck) — the
    # poll task runs it before its first cycle; /health `forge_probe` tracks it
    await reconcile_runs(manager)
    # ADR-0024: synchronous writability probe only (no network, no sync) —
    # mirror warm-up belongs to the poll task, NEVER awaited at boot
    repo_cache.verify_writable()
    # ADR-0025: same probe for the workspace base — first-touched-by-dockerd
    # (or a uid≠1000 mkdir) means root-owned and every dispatch would fail
    workspaces.verify_writable()
    tasks = [
        asyncio.create_task(poll_rt.loop(), name="poll_loop"),
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


app = FastAPI(
    title="DevCake",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.middleware("http")(enforce_control_plane_auth)
FastAPIInstrumentor.instrument_app(app)


@app.get("/api/v1/health")
async def health():
    return await build_health_payload(
        config=config, dev_types=dev_types, managers=managers,
        mappers=mappers, forge_runtime=forge_runtime,
        shared_breakers=shared_breakers, store=store,
        internal_forge=internal_forge, poll_rt=poll_rt,
        backend_degraded=shared_backend_degraded, repo_cache=repo_cache,
        workspaces=workspaces)


@app.get("/api/v1/health/live")
async def liveness():
    return {"app": True}


@app.get("/api/v1/missions")
async def list_missions():
    """Current derived Missions (poll-cycle snapshot; advisory cache — INV-1)."""
    # rows carry per-instance provenance ("instance"/"team" fields);
    # `teams` maps every configured instance for group-by consumers
    return {"teams": {i.name: i.team_key for i in config.pmos if i.configured},
            "adoption_mode": config.adoption_mode,
            "missions": poll_rt.missions_cache}


@app.get("/api/v1/runs")
async def list_runs(limit: int = 25, offset: int = 0,
                    mission_key: str | None = None, pmo_ref: str | None = None,
                    created_from: str | None = None,
                    created_to: str | None = None, sort: str | None = None,
                    dir: str | None = None, group_by: str | None = None):
    from .runs_service import list_runs_response
    return list_runs_response(store, config.cost_inputs, limit=limit,
                              offset=offset, mission_key=mission_key,
                              pmo_ref=pmo_ref, created_from=created_from,
                              created_to=created_to, sort=sort,
                              direction=dir, group_by=group_by)


@app.get("/api/v1/runs/{run_id}")
async def get_run(run_id: str):
    from .runs_service import run_detail
    run = store.get(run_id)
    if run is None:
        raise HTTPException(404)
    return run_detail(run, config.cost_inputs)


TERMINAL_STATES = {"finished", "failed", "timed_out", "orphaned"}


# ── Missions actions (docs/05 §1): the admin UI writes through here so
# every action lands in the PMO — INV-1 preserved. Handlers stay thin;
# the application service in `mission_actions.py` owns validation, label
# math (OCP), and HTTPException status codes.

class _MissionActionBody(BaseModel):
    action: str


class _SteeringBody(BaseModel):
    body: str


@app.post("/api/v1/missions/{pmo_id}/actions")
async def mission_action(pmo_id: str, body: _MissionActionBody):
    """Retry / park / unpark / resume via label swap. Returns projected labels."""
    from .mission_actions import label_action
    return await label_action(pmo_id, body.action,
                              missions_cache=poll_rt.missions_cache, managers=managers)


@app.post("/api/v1/missions/{pmo_id}/comment")
async def mission_comment(pmo_id: str, body: _SteeringBody):
    """Post an operator steering comment. NOT signed with the DevCake sentinel —
    the next poll classifies it as HUMAN and resets the attempt counter."""
    from .mission_actions import post_steering
    return await post_steering(pmo_id, body.body,
                               missions_cache=poll_rt.missions_cache, managers=managers)


@app.post("/api/v1/poll/run")
async def force_poll_route():
    """Force a poll cycle now (docs/00 §4, INV-1).

    Missions are born in the PMO — DevCake observes. This endpoint lets the
    operator close the 30-second feedback loop after a label edit in Linear
    (or a retry/park issued from the board) without waiting for the next tick.

    409 when a poll cycle is already in flight (periodic OR another manual
    trigger) — the operator retries; the next automatic cycle is imminent
    anyway. Mirrors `POST /api/v1/relations-mapper/run` (docs/11 §1).
    """
    from .mission_actions import force_poll_now
    return await force_poll_now(
        lock=poll_rt.lock,
        next_cycle_id=poll_rt.next_cycle_id,
        run_cycle=poll_rt.run_cycle)


@app.post("/api/v1/runs/{run_id}/stop")
async def stop_run_route(run_id: str):
    """Kill an in-flight run (counts as a failed attempt — UI copy says so)."""
    from .mission_actions import stop_run
    return await stop_run(run_id, run_manager=manager, run_store=store)


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


@app.post("/api/v1/system/stop-runs")
async def stop_runs_route():
    """Stop every in-flight run without wiping anything (Clear-Runs
    hardening): finalizing runs are skipped and named in the response."""
    from .mission_actions import stop_all_runs
    return await stop_all_runs(run_manager=manager, run_store=store)


@app.post("/api/v1/system/clear-runs")
async def clear_runs():
    """Operator wipe: local run state + Dagu history + OpenObserve data.

    Config, secrets, and everything in the PMO/forge are untouched (INV-1).
    In-flight Devs are stopped first. See docs/11 §3 and docs/10 §5.
    """
    from .clear import clear_all
    with tracer.start_as_current_span("system.clear_runs") as span:
        # Serialize the wipe against EVERY dispatch path (audit D5 #2/#8 +
        # re-audit #0/#6). Two locks, acquired in a fixed order:
        #   • poll_rt.lock stops the periodic poll cycle (and force-poll) —
        #     the poll loop takes it at cycle start, so no cycle runs.
        #   • bootstrap.dispatch_lock is the true chokepoint: EVERY dispatch
        #     flavor (poll, oauth, mapper "run now", hello) creates its ACL
        #     user + container inside RunBootstrap.launch under this lock.
        #     poll_rt.lock alone missed the three non-poll paths, so the
        #     ACL/SIGTERM race D3 targeted was still reachable — this closes it.
        # Order poll_rt.lock → dispatch_lock matches the poll loop's own order
        # (run_cycle holds poll_rt.lock, then launch takes dispatch_lock), so
        # there is no lock-ordering inversion / deadlock.
        async with poll_rt.lock, manager.bootstrap.dispatch_lock:
            result = await clear_all(store, executor, messaging, runlog,
                                     internal_forge=internal_forge,
                                     run_manager=manager)
        poll_rt.missions_cache.clear()
        for mgr in managers.values():
            mgr._grace.clear()
            mgr._grace_next.clear()
        # ADR-0018: the backend-degraded map IS run history, so "start fresh"
        # legitimately forgets it — and clearing it now avoids throttling with
        # zero evidence until the next poll cycle recomputes.
        shared_backend_degraded.clear()
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
    return await apply_config_patch(body, config=config, dev_types=dev_types,
                                    managers=managers, reload=reload_connections,
                                    repo_cache=repo_cache)


@app.put("/api/v1/config/pmos/{name}/intake")
async def put_pmo_intake(name: str, body: dict):
    """Flip one PMO's intake switch without rewriting the pmos list (docs/11)."""
    return set_pmo_intake(name=name, paused=body.get("paused"), config=config)


# ── config profiles (ADR-0013): named snapshots — bodies in profiles_service ─

@app.get("/api/v1/profiles")
async def list_profiles():
    return profiles_service.list_profiles_rows(config, dev_types)


@app.get("/api/v1/profiles/{name}")
async def get_profile(name: str):
    return profiles_service.get_profile_payload(name, config, dev_types)


@app.post("/api/v1/profiles")
async def save_profile(body: dict):
    return profiles_service.save_profile_body(body, config, dev_types)


@app.post("/api/v1/profiles/{name}/apply")
async def apply_profile(name: str):
    return profiles_service.apply_profile_body(
        name, config=config, dev_types=dev_types, store=store,
        reload=reload_connections, shared_breakers=shared_breakers,
        forge_breakers=forge_runtime.breakers, managers=managers)


@app.post("/api/v1/profiles/{name}/rename")
async def rename_profile(name: str, body: dict):
    return profiles_service.rename_profile_body(name, body)


@app.delete("/api/v1/profiles/{name}")
async def delete_profile(name: str):
    return profiles_service.delete_profile_body(name)


# ── settings transfer (ADR-0013): export/import — bodies in settings_transfer ─

@app.post("/api/v1/settings/export")
async def export_settings(body: dict):
    return await settings_transfer.export_settings_body(
        body, config=config, dev_types=dev_types, skill_service=skill_service)


@app.get("/api/v1/settings/export/summary")
async def export_summary():
    return await settings_transfer.export_summary_body(skill_service=skill_service)


@app.post("/api/v1/settings/import/preview")
async def import_preview(body: dict):
    return settings_transfer.import_preview_body(body, config=config,
                                                 dev_types=dev_types)


@app.post("/api/v1/settings/import")
async def import_settings(body: dict):
    return await settings_transfer.import_settings_body(
        body, skill_service=skill_service)


@app.post("/api/v1/settings/import/env")
async def import_env(body: dict):
    return settings_transfer.import_env_body(body)


# ── per-Mission-Type prompt templates (v0.1.1) + Dev Types — bodies in
# devtypes_service; forwards pass the singletons at call time ────────────────

@app.get("/api/v1/prompt-templates")
async def get_prompt_templates():
    return await devtypes_service.get_prompt_templates(config=config,
                                                       dev_types=dev_types)


@app.put("/api/v1/prompt-templates/{mission_type}/{name}")
async def put_prompt_template(mission_type: str, name: str, body: dict):
    return await devtypes_service.put_prompt_template(mission_type, name, body)


@app.delete("/api/v1/prompt-templates/{mission_type}/{name}")
async def delete_prompt_template(mission_type: str, name: str):
    return await devtypes_service.delete_prompt_template(mission_type, name,
                                                         config=config)


@app.put("/api/v1/devtype-prompts/{dev_type}/{name}")
async def put_devtype_prompt(dev_type: str, name: str, body: dict):
    return await devtypes_service.put_devtype_prompt(dev_type, name, body,
                                                     dev_types=dev_types)


@app.delete("/api/v1/devtype-prompts/{dev_type}/{name}")
async def delete_devtype_prompt(dev_type: str, name: str):
    return await devtypes_service.delete_devtype_prompt(dev_type, name,
                                                        config=config)


@app.get("/api/v1/harnesses")
async def list_harnesses():
    return await devtypes_service.list_harnesses()


@app.get("/api/v1/dev-types")
async def list_dev_types():
    return await devtypes_service.list_dev_types(dev_types=dev_types)


@app.post("/api/v1/dev-types")
@app.put("/api/v1/dev-types/{name}")
async def upsert_dev_type(body: dict, name: str | None = None):
    return await devtypes_service.upsert_dev_type(body, name,
                                                  dev_types=dev_types)


@app.post("/api/v1/dev-types/{name}/rename")
async def rename_dev_type(name: str, body: dict):
    return await devtypes_service.rename_dev_type(
        name, body, config=config, dev_types=dev_types,
        shared_breakers=shared_breakers)


@app.delete("/api/v1/dev-types/{name}")
async def remove_dev_type(name: str):
    return await devtypes_service.remove_dev_type(name, config=config,
                                                  dev_types=dev_types)


@app.post("/api/v1/dev-types/{name}/credentials")
async def upload_credentials(name: str, body: dict):
    return await devtypes_service.upload_credentials(
        name, body, dev_types=dev_types, shared_breakers=shared_breakers)


@app.get("/api/v1/assignments")
async def get_assignments():
    return await devtypes_service.get_assignments(config=config)


@app.put("/api/v1/assignments")
async def put_assignments(body: dict):
    return await devtypes_service.put_assignments(body, config=config,
                                                  dev_types=dev_types)


# ── GUI-stored secrets (M12, F5) + connection tests — bodies (and the
# secret-ref/harness-var validators) in connections_service ──────────────────

@app.put("/api/v1/secrets/{scope}/{instance}/{field}")
async def put_secret(scope: str, instance: str, field: str, body: dict):
    return await connections_service.put_secret(
        scope, instance, field, body,
        forge_runtime=forge_runtime, reload=reload_connections)


@app.delete("/api/v1/secrets/{scope}/{instance}/{field}")
async def delete_secret(scope: str, instance: str, field: str):
    return await connections_service.delete_secret(
        scope, instance, field,
        forge_runtime=forge_runtime, reload=reload_connections)


@app.put("/api/v1/harness-secrets/{var}")
async def put_harness_secret(var: str, body: dict):
    return await connections_service.put_harness_secret(
        var, body, dev_types=dev_types, shared_breakers=shared_breakers)


@app.delete("/api/v1/harness-secrets/{var}")
async def delete_harness_secret(var: str):
    return await connections_service.delete_harness_secret(var)


@app.get("/api/v1/secrets-check")
async def secrets_check(conn: str = "", harness: str = ""):
    return await connections_service.secrets_check(conn, harness)


@app.get("/api/v1/secrets/inventory")
async def secrets_inventory():
    """Presence-only catalog of clearable secrets (never values)."""
    return await connections_service.secrets_inventory()


@app.post("/api/v1/secrets/clear")
async def clear_secrets(body: dict):
    """Delete operator-selected secrets (harness / connections / credential files)."""
    return await connections_service.clear_secrets(
        body, forge_runtime=forge_runtime, reload=reload_connections,
        config=config, shared_breakers=shared_breakers, dev_types=dev_types)


@app.get("/api/v1/connections/registry")
async def connections_registry():
    return await connections_service.connections_registry()


@app.post("/api/v1/connections/pmo/{name}/test")
async def test_pmo(name: str):
    return await connections_service.test_pmo(name, config=config,
                                              managers=managers)


@app.post("/api/v1/connections/forge/{name}/test")
async def test_forge(name: str):
    return await connections_service.test_forge(name, config=config,
                                                forge_runtime=forge_runtime)


# ── internal-forge repos + skill store (M11, docs/16) — bodies in
# internal_repos_service ─────────────────────────────────────────────────────

@app.get("/api/v1/internal-repos")
async def list_internal_repos():
    return await internal_repos_service.list_internal_repos(
        internal_forge=internal_forge)


@app.post("/api/v1/internal-repos/create")
async def create_internal_repo(body: dict):
    return await internal_repos_service.create_internal_repo(
        body, internal_forge=internal_forge)


@app.get("/api/v1/skills")
async def list_skills():
    return await internal_repos_service.list_skills(skill_service=skill_service)


@app.get("/api/v1/skills/{name}")
async def get_skill(name: str):
    return await internal_repos_service.get_skill(
        name, skill_service=skill_service)


@app.post("/api/v1/skills")
async def create_skill(body: dict):
    return await internal_repos_service.create_skill(
        body, skill_service=skill_service)


@app.post("/api/v1/skills/import")
async def import_skill(body: dict):
    return await internal_repos_service.import_skill(
        body, skill_service=skill_service)


@app.delete("/api/v1/skills/{name}")
async def delete_skill_endpoint(name: str):
    return await internal_repos_service.delete_skill_endpoint(
        name, skill_service=skill_service)


@app.post("/api/v1/skills/sync")
async def sync_skills():
    return await internal_repos_service.sync_skills(
        internal_forge=internal_forge, skill_service=skill_service)


@app.delete("/api/v1/internal-repos/{name}")
async def delete_internal_repo(name: str):
    return await internal_repos_service.delete_internal_repo(
        name, internal_forge=internal_forge, store=store,
        forge_runtime=forge_runtime)


@app.post("/api/v1/relations-mapper/run")
async def run_mapper(instance: str | None = None):
    """Manual trigger (docs/11): works regardless of the enabled toggle — the
    toggle governs only the periodic service. Requires a valid dev_type.
    ?instance= selects the PMO instance; default = the first configured."""
    names = [i.name for i in config.pmos if i.name in mappers]
    if not names:
        raise HTTPException(422, "no configured PMO instance")
    target = instance or names[0]
    if target not in mappers:
        raise HTTPException(404, f"no PMO instance named {target!r}")
    try:
        run = await mappers[target].run_now()
    except MapperUnconfigured as e:
        raise HTTPException(422, str(e))
    except MapperBusy as e:
        raise HTTPException(409, str(e))
    return {"run_id": run.run_id, "state": run.state, "instance": target}


# ── GUI OAuth helpers (docs/11 §2) ───────────────────────────────────────────

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
    """Dispatches the hello stub Dev through the full pipeline (Dagu → container
    → Redis → finalize). Permanent debug/CI fixture — scripts/ci_suite.sh."""
    try:
        run = await manager.dispatch_hello(sleep, payload_kb, timeout_seconds)
    except DuplicateRun as e:
        raise HTTPException(409, f"duplicate dagRunId {e}")
    return {"run_id": run.run_id, "state": run.state}
