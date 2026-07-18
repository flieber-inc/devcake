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

import httpx
import yaml
import redis.asyncio as aioredis
from fastapi import FastAPI, HTTPException
from fastapi.responses import PlainTextResponse, StreamingResponse
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
from ..domain.model import ALL_LABELS, derive
from ..domain.oauth import OAuthManager
from ..domain.orchestrator import (FinalizerRouter, MapperBusy, MapperService,
                                   MapperUnconfigured, MissionManager)
from ..domain.forge_runtime import ForgeRuntime
from ..domain.reconcile import reconcile_runs
from ..domain.runs import RunManager
from ..domain.skills import SkillService, SkillStoreError
from ..domain.watchdog import watchdog_loop
from ..harness import HARNESSES, dev_type_status
from ..ports.forge import mission_branch
from ..prompts import templates as prompt_templates
from ..ports.pmo import PMOTransient
from ..telemetry import OO_URL, setup_telemetry
from .auth import credentials_configured, enforce_control_plane_auth

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
manager = RunManager(store, messaging, executor)

# ── multi-instance wiring (schema v3, ADR-0009; repos plural per M10) ───────
# One MissionManager per CONFIGURED PMO instance; shared state is injected:
# the RunManager/store (global concurrency), ONE dev-type breakers dict
# (credentials are DevCake-global), and ONE ForgeRuntime (repos belong to
# the deployment — missions from any instance can route to any repo).
shared_breakers: dict[str, str] = {}
managers: dict[str, MissionManager] = {}
mappers: dict[str, MapperService] = {}
forge_runtime = ForgeRuntime()
# the bundled internal fallback forge (M11): provisioner is admin-credentialed
# (GITEA_ADMIN_*); None disables the zero-repo un-gating when Gitea is absent
internal_forge = make_internal_forge() if os.environ.get("GITEA_ADMIN_PASSWORD") else None
# skill store (docs/16 skill store v1): store-first reads via the internal
# forge; bundled copies keep built-in skills working forge-less
skill_service = SkillService(internal_forge)


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
        else:
            managers[name] = MissionManager(
                config, dev_types, p, forge_runtime, manager, messaging,
                instance=inst, breakers=shared_breakers,
                internal_forge=internal_forge, skills=skill_service)
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
    _protection_cache["ts"] = 0.0          # repos may have changed — reprobe

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

# poll-cycle snapshot served by /api/v1/missions (advisory cache — INV-1)
missions_cache: list[dict] = []


# PERSISTENT cross-instance ownership (v0.1 plan H1 + review finding):
# pmo_id → owning instance name. Persistence is load-bearing — a per-cycle
# rebuild would flip ownership of a shared mission the moment its owner has
# one PMOTransient cycle, double-dispatching it. Released only when the
# OWNER successfully polls and no longer sees the mission, or leaves config
# (and never while a run for the mission is still active — audit A15).
# Durable across restarts via OwnerStore (audit A15: in-memory-only
# ownership reopened the duplicate-dispatch window on every restart).
from ..adapters.files.owner_store import OwnerStore  # noqa: E402

_owner_store = OwnerStore()
_mission_owner: dict[str, str] = _owner_store.load()

# instance → last poll-segment error (audit A1): a PERMANENT PMO failure
# (revoked key, deleted team) skips only that instance's segment; this map
# surfaces it in /health as `poll_degraded`. Cleared on a green segment.
_poll_degraded: dict[str, str] = {}


def _claim_missions(mgr: MissionManager, fetched: list,
                    owner: dict[str, str]) -> list:
    """Cross-instance dedupe on the RAW pmo_id: a Linear project can be
    accessible to two teams, so two instances in one workspace would both
    adopt it — duplicate decomposition, label fights. The first instance to
    see it claims it (durably — see _mission_owner); others surface an
    anomaly and skip. Pure function (hermetically tested)."""
    missions = []
    for m in fetched:
        prior = owner.get(m.pmo_id)
        if prior is not None and prior != mgr.instance_name:
            mgr.anomalies[m.pmo_id] = (
                f"{m.key} is also visible to instance '{prior}' — handled "
                f"there (shared project?); skipped here")
            continue
        owner[m.pmo_id] = mgr.instance_name
        missions.append(m)
    return missions


def _release_stale_ownership(polled_ok: dict[str, set]) -> None:
    """Drop ownership entries whose owner left config, or whose owner
    successfully polled this cycle and no longer sees the mission (done +
    aged out of list_all, or deleted). A FAILED poll releases nothing, and
    neither does an ACTIVE run for the mission (audit A15: releasing while
    the ex-owner's run still executes lets another instance re-dispatch the
    same work — the in-flight guard only sees its own pmo_ref)."""
    in_flight = {r.mission_pmo_id for r in store.active() if r.mission_pmo_id}
    for pmo_id, name in list(_mission_owner.items()):
        if pmo_id in in_flight:
            continue
        if name not in managers:
            del _mission_owner[pmo_id]
        elif name in polled_ok and pmo_id not in polled_ok[name]:
            del _mission_owner[pmo_id]


async def _poll_instance(mgr: MissionManager,
                         cache_rows: list[dict]) -> tuple[int, int, int, set]:
    """One instance's poll segment: fetch + cross-instance dedupe + derive +
    gate + sweeps + schedule + cache rows. Returns (seen, candidates,
    dispatched, fetched_ids). Raises PMOTransient for the caller's
    per-instance skip."""
    fetched = await mgr.pmo.list_all(mgr.instance.team_key)
    fetched_ids = {m.pmo_id for m in fetched}
    missions = _claim_missions(mgr, fetched, _mission_owner)
    run_snapshot = mgr.runs.store.all()   # one read per segment, not per mission
    for m in missions:
        # per-mission repo resolution (M10/M11): a transient poll artifact —
        # dispatch re-resolves live (sticky) before anything irreversible.
        # resolve_repo_live un-gates zero-repo missions onto the internal
        # fallback forge (provisions a per-mission repo at intake). A Gitea
        # outage must GATE the mission, never abort the whole poll cycle for
        # every instance (review finding #2) — the boot promise that an
        # outage "degrades only zero-repo missions" depends on this.
        try:
            m.repo, m.repo_reason = await mgr.resolve_repo_live(
                m, all_runs=run_snapshot)
        except Exception as e:
            m.repo, m.repo_reason = None, (
                f"internal forge unreachable — mission gated: {str(e)[:150]}")
            log.warning("repo resolution failed for %s: %s", m.key, e)
    derived = [(m, derive(m, config.adoption_mode)) for m in missions]
    # the gate is a poll artifact, computed EVERY cycle — pause freezes
    # dispatch, never information (docs/04 §2)
    gate = await mgr.gate_map(missions)
    await mgr.sweeps(missions)   # merge + tracking sweeps (docs/04 §1)
    # intake pause (docs/11): no NEW dispatches — in-flight runs still
    # finalize (ingress consumer) and sweeps above keep running
    if config.intake_paused:
        dispatched = 0
    else:
        dispatched = await mgr.schedule(missions, gate)
        # .get: build_managers may drop the instance mid-cycle (hot reload) —
        # a KeyError here would abort the WHOLE poll cycle (review finding)
        mp = mappers.get(mgr.instance_name)
        if mp is not None:
            await mp.maybe_dispatch(missions)
    mgr.rotate_grace()
    # anomalies are advisory and per-mission: prune on the FETCHED set (not
    # just claimed missions — a dedupe anomaly references a mission this
    # instance never claims) once terminal or no longer visible at all
    terminal = {m.pmo_id for m in fetched if m.status in ("done", "canceled")}
    for k in list(mgr.anomalies):
        if k in terminal or k not in fetched_ids:
            del mgr.anomalies[k]
    id_to_key = {m.pmo_id: m.key for m in missions}
    cache_rows.extend({
        "instance": mgr.instance_name,
        "team": mgr.instance.team_key,
        "key": m.key, "kind": m.pmo_kind, "title": m.title,
        "status": m.status, "priority": m.priority,
        "labels": sorted(m.labels), "mission_type": d.mission_type,
        "schedulable": d.schedulable,
        # the blocked-by gate is a scheduler concern, not a derivation row —
        # surfaced so the admin panel shows why (ADR-0007)
        "repo": m.repo,
        "reason": gate.get(m.pmo_id, m.repo_reason or d.reason),
        "blocked_by": [id_to_key.get(b, b) for b in m.blocked_by],
        "pmo_id": m.pmo_id,
    } for m, d in derived)
    return (len(missions), sum(1 for _, d in derived if d.schedulable),
            dispatched, fetched_ids)


async def run_poll_cycle(cycle: int) -> None:
    """One poll cycle (docs/04 §1): per configured instance — fetch + dedupe
    + derive + gate + dispatch + sweeps — then the merged cache. A failing
    instance (PMOTransient or a PERMANENT error like a revoked key — audit
    A1) skips only ITS segment, never the whole cycle; permanent failures
    surface in /health `poll_degraded` until a green segment clears them."""
    with tracer.start_as_current_span("poll.cycle") as span:
        span.set_attribute("devcake.poll.cycle", cycle)
        try:
            # a latched repo re-probes every cycle so a transient failure
            # (or a rotated-back token) self-heals without an operator
            if forge_runtime.breakers:
                await refresh_forge_health()
            cache_rows: list[dict] = []
            seen, cand, disp = 0, 0, 0
            polled_ok: dict[str, set] = {}       # instance → fetched pmo_ids
            owner_before = dict(_mission_owner)  # persisted iff changed below
            for mgr in _managers_in_config_order():
                with tracer.start_as_current_span("poll.instance") as ispan:
                    ispan.set_attribute("devcake.instance", mgr.instance_name)
                    try:
                        s, c, d, ids = await _poll_instance(mgr, cache_rows)
                        polled_ok[mgr.instance_name] = ids
                        seen, cand, disp = seen + s, cand + c, disp + d
                        _poll_degraded.pop(mgr.instance_name, None)
                    except PMOTransient as e:
                        # transient PMO trouble skips only THIS instance's
                        # segment — the others still poll this cycle. Not
                        # marked degraded: transient is expected weather.
                        ispan.set_attribute("devcake.outcome", "PMO_TRANSIENT")
                        log.warning("poll.cycle %d: instance %s skipped: %s",
                                    cycle, mgr.instance_name, e)
                    except Exception as e:
                        # a PERMANENT per-instance failure (revoked key,
                        # deleted team → RuntimeError) must not starve the
                        # remaining instances (audit A1)
                        ispan.set_attribute("devcake.outcome", "INSTANCE_ERROR")
                        _poll_degraded[mgr.instance_name] = (
                            f"{type(e).__name__}: {str(e)[:200]}")
                        log.exception("poll.cycle %d: instance %s FAILED — "
                                      "segment skipped", cycle, mgr.instance_name)
            _release_stale_ownership(polled_ok)
            if _mission_owner != owner_before:   # durable claim (audit A15)
                _owner_store.save(_mission_owner)
            # a skipped instance keeps its LAST snapshot in the cache
            # (v0 behavior: PMO trouble never blanks the view)
            cache_rows.extend(
                row for row in missions_cache
                if row["instance"] in managers
                and row["instance"] not in polled_ok)
            missions_cache[:] = cache_rows
            span.set_attribute("devcake.missions.seen", seen)
            span.set_attribute("devcake.missions.candidates", cand)
            span.set_attribute("devcake.missions.dispatched", disp)
            log.info("poll.cycle %d: %d missions, %d candidates, %d dispatched "
                     "(%d instances)", cycle, seen, cand, disp, len(managers))
        except Exception:
            # a poll cycle must NEVER kill the loop — log and try again next tick
            span.set_attribute("devcake.outcome", "cycle_error")
            log.exception("poll.cycle %d failed", cycle)


async def poll_loop() -> None:
    cycle = 0
    while True:
        cycle += 1
        await run_poll_cycle(cycle)
        await asyncio.sleep(config.poll_interval_seconds)


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
    # the password check above can't catch a blank USER half of the OO
    # service account — an empty email encodes ':password' and 401s every
    # telemetry write silently (collector, fluentbit, log-push)
    if not os.environ.get("OO_INGEST_EMAIL", "").strip():
        raise RuntimeError(
            "OO_INGEST_EMAIL must be set (the OO service account — ISSUES #13). "
            "Set it in .env alongside OO_INGEST_PASSWORD, then run "
            "scripts/provision_oo.py once.")


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
    await refresh_forge_health()
    await reconcile_runs(manager)
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


app = FastAPI(
    title="DevCake",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.middleware("http")(enforce_control_plane_auth)
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
_protection_cache: dict = {"ts": 0.0, "value": {}}


async def _branch_protection() -> dict:
    """{repo_name: BranchProtection|None} across every configured repo."""
    if time.monotonic() - _protection_cache["ts"] > 300 or _protection_cache["ts"] == 0:
        _protection_cache["ts"] = time.monotonic()
        out: dict = {}
        for name, f in forge_runtime.forges.items():
            inst = forge_runtime.instance(name)
            # reference-only repos: DevCake never pushes or merges there, so
            # the unprotected-default-branch advisory would be pure noise
            if inst is not None and inst.reference_only:
                continue
            try:
                prot = await f.default_branch_protection(
                    inst.default_branch if inst else "main")
                out[name] = prot.model_dump() if prot else None
            except Exception:
                out[name] = None
        _protection_cache["value"] = out
    return _protection_cache["value"]


async def _check_http(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            return (await client.get(url)).status_code < 500
    except Exception:
        return False


# 60s-cached probe: does the OO service account actually authenticate?
# Catches "compose up before provision_oo.py" / rotated-password drift —
# without it, telemetry 401s silently forever (ISSUES #13 review finding).
_oo_ingest_cache: dict = {"ts": 0.0, "result": None}


async def _oo_ingest_check() -> dict:
    now = time.monotonic()
    if _oo_ingest_cache["result"] is not None and now - _oo_ingest_cache["ts"] < 60:
        return _oo_ingest_cache["result"]
    from ..telemetry import OO_ORG, _basic_auth
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{OO_URL}/api/{OO_ORG}/streams",
                                 params={"type": "logs"},
                                 headers={"Authorization": f"Basic {_basic_auth()}"})
        result = {"ok": r.status_code == 200,
                  "detail": "" if r.status_code == 200 else
                  f"HTTP {r.status_code} — the OO service account cannot "
                  f"authenticate; run scripts/provision_oo.py (ISSUES #13)"}
    except Exception as e:
        result = {"ok": False, "detail": f"probe failed: {str(e)[:150]}"}
    _oo_ingest_cache.update(ts=now, result=result)
    return result


@app.get("/api/v1/health")
async def health():
    redis_ok, dagu_ok, oo_ok = await asyncio.gather(
        _check_redis(),
        _check_http(f"{DAGU_URL}/api/v1/health"),
        _check_http(f"{OO_URL}/healthz"),
    )
    # per-instance PMO health (schema v3): unconfigured instances show grey
    # (ok: None), never red; the scalar `pmo` aggregate keeps the SPA's
    # health dot working (true = every configured instance probes ok)
    pmo_instances: dict[str, dict] = {}
    for inst in config.pmos:
        if not inst.configured:
            pmo_instances[inst.name] = {"ok": None, "configured": False,
                                        "team": ""}
            continue
        mgr = managers.get(inst.name)
        try:
            ok = bool(mgr) and (await mgr.pmo.health_probe(inst.team_key)).ok
        except Exception:
            ok = False
        pmo_instances[inst.name] = {"ok": ok, "configured": True,
                                    "team": inst.team_key}
    configured_ok = [v["ok"] for v in pmo_instances.values() if v["configured"]]
    prefixed = len(managers) > 1   # advisory text carries the instance when N>1

    def _merged(attr: str) -> dict:
        out: dict = {}
        for name, mgr in managers.items():
            for k, v in getattr(mgr, attr).items():
                out[k] = f"[{name}] {v}" if prefixed and isinstance(v, str) else v
        return out

    return {
        "app": True,
        "redis": redis_ok,
        "dagu": dagu_ok,
        "openobserve": oo_ok,
        "oo_ingest": await _oo_ingest_check(),
        "pmo": bool(configured_ok) and all(configured_ok),
        "pmo_instances": pmo_instances,
        "forge": forge_runtime.health,
        # dev-type breakers + per-repo forge breakers, one map for the SPA
        "circuit_breakers": {**shared_breakers,
                             **{f"repo:{k}": v
                                for k, v in forge_runtime.breakers.items()}},
        "intake_paused": config.intake_paused,
        "active_runs": len(store.active()),
        "forge_protection": await _branch_protection(),
        "anomalies": _merged("anomalies"),
        "merge_handoffs": _merged("merge_handoffs"),
        "needs_human": _merged("needs_human"),
        "dependency_cycles": [
            ([f"{name}:{k}" for k in cyc] if prefixed else cyc)
            for name, mgr in managers.items() for cyc in mgr.cycles],
        "blocked_reasons": _merged("blocked_reasons"),
        # instances whose poll segment failed with a PERMANENT error (audit
        # A1) — the other instances keep polling; this names the sick one
        "poll_degraded": dict(_poll_degraded),
        "internal_forge": (await internal_forge.health()
                           if internal_forge is not None else None),
        "mapper_degraded": " · ".join(
            f"[{name}] {msg}" if prefixed else str(msg)
            for name, mp in mappers.items() if (msg := mp.degraded())) or None,
        # active templates that no longer resolve (fallback-to-default in
        # effect) — the SPA derives a dismissable alert per entry (v0.1.1)
        "prompt_template_warnings": (
            prompt_templates.template_warnings(config)
            + prompt_templates.devtype_prompt_warnings(config, dev_types)),
        "security_warnings": _security_warnings(),
    }


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
    return run.model_dump(include={
        "schema_version", "run_id", "mission_key", "mission_pmo_id", "pmo_kind",
        "pmo_ref", "repo_ref", "mission_type", "dev_type", "seq",
        "attempt_of_step", "stage_label_at_dispatch", "state", "created_at",
        "started_at", "ended_at", "last_heartbeat", "timeout_seconds",
        "finalized_steps", "artifact_bytes", "error",
        "verdict",
    })


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
        for mgr in managers.values():
            mgr._grace.clear()
            mgr._grace_next.clear()
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
        reject_stale_patch(body)
        merged = AppConfig.model_validate(deep_merge(config.model_dump(), body))
    except Exception as e:
        raise HTTPException(422, str(e))
    # cross-store semantics + dry-run adapter construction (ISSUES #11) live
    # in settings_bundle — ONE implementation shared with bundle apply
    # (ADR-0013); the PUT resolves templates against disk
    try:
        validate_config_semantics(
            merged, set(dev_types),
            template_exists=lambda mt, name:
                not prompt_templates.resolve_playbook(mt, name)[1])
        dry_run_adapters(merged)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    previous = config.model_dump()
    # a removed instance's stored secrets go with it — otherwise a later
    # instance reusing the name silently inherits the dead credential
    removed = [("pmo", p["name"]) for p in previous["pmos"]
               if p["name"] not in {i.name for i in merged.pmos}]
    removed += [("repo", r["name"]) for r in previous["repos"]
                if r["name"] not in {i.name for i in merged.repos}]
    for field in type(merged).model_fields:
        setattr(config, field, getattr(merged, field))
    save_config(config)
    try:
        reload_connections()                     # hot reload pmo + forge
    except Exception as e:
        log.exception("reload_connections failed — restoring previous config")
        restored = AppConfig.model_validate(previous)
        for field in type(restored).model_fields:
            setattr(config, field, getattr(restored, field))
        save_config(config)
        try:
            reload_connections()
        except Exception:
            log.exception("restore reload also failed")
        raise HTTPException(500, f"config reload failed; previous config restored: {e}")
    for scope, name in removed:                  # only once the new config took
        try:
            secrets_store.delete_connection_instance(scope, name)
        except Exception:
            # the config change is APPLIED at this point — a cleanup failure
            # must not 500 it (audit A21); the orphaned file is deletable
            # later and named here
            log.exception("could not delete stored secrets of removed "
                          "%s instance %r", scope, name)
    if not previous["auto_merge"] and config.auto_merge:
        # auto_merge flipped OFF→ON (founder request 2026-07-15): re-arm the
        # deferred-merge window for missions already parked at DEVCAKE-MERGE —
        # the next sweep posts a fresh window entry and drives their merges
        for mgr in managers.values():
            mgr.rearm_merge_windows = True
        log.info("auto_merge flipped ON — parked DEVCAKE-MERGE missions "
                 "re-armed for the deferred-merge sweep")
    return config.model_dump()


# ── config profiles (ADR-0013): named snapshots of settings + secrets ────────

def _require_no_active_runs(action: str) -> None:
    """World-swaps are blocked while runs are in flight (founder decision) —
    the internal-repo delete guard pattern, applied to whole-settings
    replacement."""
    n = len(store.active())
    if n:
        raise HTTPException(
            409, f"{n} run(s) active — wait for them to finish or clear "
                 f"runs before {action}")


def _snapshot_warnings(bundle: dict) -> list[str]:
    """A snapshot silently missing credentials its config expects is a trap
    at apply time — name the gaps at save time instead."""
    warns = []
    sec = bundle.get("secrets") or {}
    conns = sec.get("connections") or {}
    cfg = bundle.get("config") or {}
    for p in (cfg.get("app") or {}).get("pmos") or []:
        if p.get("team_key") and not (conns.get(f"pmo-{p['name']}") or {}).get("api_key"):
            warns.append(f"PMO {p['name']!r} is configured but has no stored "
                         "API key — the snapshot carries none")
    for r in (cfg.get("app") or {}).get("repos") or []:
        stored = conns.get(f"repo-{r['name']}") or {}
        if r.get("url") and not (stored.get("token") or stored.get("token_ro")):
            warns.append(f"repo {r['name']!r} is configured but has no stored "
                         "token — the snapshot carries none")
    return warns


@app.get("/api/v1/profiles")
async def list_profiles():
    """Profile rows for the admin table — counts and presence only. The
    last-applied breadcrumb + divergence boolean ride the matching row
    (dict compare + secret timestamps, never values — ADR-0011)."""
    rows = profiles_store.list_profiles()
    la = profiles_store.last_applied()
    for row in rows:
        row["last_applied_at"] = None
        row["diverged"] = None
        if la and la.get("name") == row["name"] and not row.get("broken"):
            row["last_applied_at"] = la.get("at")
            try:
                bundle = profiles_store.read_profile(row["name"])
                row["diverged"] = profiles_store.diverged_since(
                    bundle, la.get("at") or "", config, dev_types)
            except BundleError:
                row["diverged"] = None
    return {"profiles": rows}


@app.get("/api/v1/profiles/{name}")
async def get_profile(name: str):
    """Full section A + a secrets PRESENCE map + the apply-preview diff.
    Secret values never leave this endpoint."""
    try:
        bundle = profiles_store.read_profile(name)
        diff = diff_bundle(bundle, config, dev_types)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    presence = None
    if "secrets" in (bundle.get("sections") or []):
        sec = bundle.get("secrets") or {}
        presence = {"connections": {k: sorted(f) for k, f in
                                    (sec.get("connections") or {}).items()},
                    "harness": sorted(sec.get("harness") or {})}
    return {"name": bundle.get("name", name),
            "created_at": bundle.get("created_at"),
            "devcake_tag": bundle.get("devcake_tag", ""),
            "sections": bundle.get("sections") or [],
            "config": bundle.get("config"),
            "secrets_present": presence,
            "diff": diff}


@app.post("/api/v1/profiles")
async def save_profile(body: dict):
    """Save-current-as: snapshot the live settings (A) + secret values (B)
    under a name. 409 on collision unless overwrite — the UI chains an
    explicit overwrite confirm."""
    name = str(body.get("name") or "")
    bundle = serialize_current(config, dev_types,
                               include_config=True, include_secrets=True)
    try:
        profiles_store.save_profile(name, bundle,
                                    overwrite=bool(body.get("overwrite")))
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    warnings = _snapshot_warnings(bundle)
    sec = bundle.get("secrets") or {}
    audit_event("profile_saved",
                f"name={name} secrets="
                f"{sum(len(f) for f in (sec.get('connections') or {}).values()) + len(sec.get('harness') or {})}")
    return {"saved": True, "name": name, "warnings": warnings}


@app.post("/api/v1/profiles/{name}/apply")
async def apply_profile(name: str):
    """THE world-swap: replaces the sections the profile contains (a profile
    without secrets keeps the live ones). Blocked while runs are active;
    rollback-by-reapply on reload failure (settings_bundle)."""
    _require_no_active_runs("applying a profile")
    try:
        bundle = profiles_store.read_profile(name)
        result = apply_bundle(bundle, config=config, dev_types=dev_types,
                              reload=reload_connections)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    if "secrets" in result["applied"]:
        # fresh credentials clear latched auth state, same as the individual
        # secret PUT endpoints do
        shared_breakers.clear()
        forge_runtime.breakers.clear()
    profiles_store.record_applied(name)
    audit_event("profile_applied",
                f"name={name} sections={'+'.join(result['applied'])}")
    return {"profile": name, **result}


@app.post("/api/v1/profiles/{name}/rename")
async def rename_profile(name: str, body: dict):
    new = str(body.get("new_name") or "")
    try:
        profiles_store.rename_profile(name, new)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    audit_event("profile_renamed", f"{name} -> {new}")
    return {"renamed": True, "name": new}


@app.delete("/api/v1/profiles/{name}")
async def delete_profile(name: str):
    try:
        profiles_store.delete_profile(name)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    audit_event("profile_deleted", f"name={name}")
    return {"deleted": name}


# ── settings transfer (ADR-0013): export / import as a single file ───────────
# The ONE sanctioned secret-value egress: POST-only, encrypted by default,
# plaintext behind an explicit acknowledgment, every export audited. Import
# LANDS AS A PROFILE — applying stays the profiles endpoint's job, so there
# is exactly one world-swap path (and one runs-active guard) in the product.

class _PassphraseRequired(Exception):
    pass


def _open_uploaded_bundle(body: dict) -> dict:
    """content_b64 → dict, hardened: base64 + size cap + yaml.safe_load only,
    protected-envelope unwrap when a passphrase rides along."""
    try:
        raw = base64.b64decode(str(body.get("content_b64") or ""), validate=True)
    except Exception:
        raise HTTPException(422, "content_b64 must be valid base64")
    if len(raw) > MAX_BUNDLE_BYTES:
        raise HTTPException(422, f"bundle exceeds "
                                 f"{MAX_BUNDLE_BYTES // (1024 * 1024)} MB")
    try:
        doc = yaml.safe_load(raw.decode("utf-8"))
    except Exception:
        raise HTTPException(422, "not a readable YAML bundle")
    if not isinstance(doc, dict):
        raise HTTPException(422, "not a settings bundle")
    if "protected" in doc:
        passphrase = body.get("passphrase")
        if not passphrase:
            raise _PassphraseRequired()
        try:
            doc = unprotect_bundle(doc, str(passphrase))
        except DecryptError as e:
            raise HTTPException(422, str(e))
        except BundleError as e:
            raise HTTPException(e.status, str(e))
    return doc


def _bundle_filename() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("devcake-settings-%Y%m%d-%H%M.yaml")


@app.post("/api/v1/settings/export")
async def export_settings(body: dict):
    sections = body.get("sections") or {}
    inc_cfg = bool(sections.get("config"))
    inc_sec = bool(sections.get("secrets"))
    inc_env = bool(sections.get("setup_env"))
    if not (inc_cfg or inc_sec or inc_env):
        raise HTTPException(422, "select at least one section to export")
    enc = body.get("encryption") or {}
    passphrase = None
    if inc_sec or inc_env:
        mode = enc.get("mode")
        if mode == "passphrase":
            passphrase = str(enc.get("passphrase") or "")
            if len(passphrase) < MIN_PASSPHRASE_LEN:
                raise HTTPException(422, f"passphrase must be at least "
                                         f"{MIN_PASSPHRASE_LEN} characters")
        elif mode == "plaintext":
            if not enc.get("acknowledge_plaintext"):
                raise HTTPException(422, "plaintext secret export must be "
                                         "explicitly acknowledged")
        else:
            raise HTTPException(422, "exports containing secrets or setup "
                                     "values require an encryption choice")
    source = body.get("source") or "current"
    if source == "current":
        skill_payloads = None
        if inc_cfg and body.get("include_skills"):
            names = [s.name for s in (await skill_service.list_skills())[0]]
            skill_payloads, _warns = await skill_service.payload_for(names)
        bundle = serialize_current(
            config, dev_types,
            include_config=inc_cfg, include_secrets=inc_sec,
            include_credential_files=inc_sec and bool(body.get("include_credential_files")),
            include_setup_env=inc_env, skill_payloads=skill_payloads)
    else:
        name = str((source or {}).get("profile") or "")
        if inc_env:
            raise HTTPException(422, "profiles never hold setup values — "
                                     "export setup_env from current settings")
        try:
            stored = profiles_store.read_profile(name)
        except BundleError as e:
            raise HTTPException(e.status, str(e))
        for want, key in ((inc_cfg, "config"), (inc_sec, "secrets")):
            if want and key not in stored:
                raise HTTPException(422, f"profile {name!r} has no {key} section")
        bundle = {k: v for k, v in stored.items() if k != "name"}
        bundle["sections"] = [s for s in ("config", "secrets")
                              if (s == "config" and inc_cfg) or (s == "secrets" and inc_sec)]
        if not inc_cfg:
            bundle.pop("config", None)
        if not inc_sec:
            bundle.pop("secrets", None)
    if (inc_sec or inc_env) and passphrase is None:
        bundle["plaintext_secrets"] = True
    if passphrase is not None:
        bundle = protect_bundle(bundle, passphrase)
    audit_event("settings_exported",
                f"source={'current' if source == 'current' else source.get('profile')} "
                f"sections={'+'.join(bundle.get('sections') or [])} "
                f"encrypted={passphrase is not None}")
    return PlainTextResponse(
        yaml.safe_dump(bundle, sort_keys=False),
        media_type="application/yaml",
        headers={"Content-Disposition":
                 f'attachment; filename="{_bundle_filename()}"'})


@app.get("/api/v1/settings/export/summary")
async def export_summary():
    """Counts for the export dialog — never values, never names of values'
    contents."""
    conns = secrets_store.list_connection_secrets()
    by_scope: dict[str, int] = {"pmo": 0, "repo": 0}
    for key, fields in conns.items():
        scope = key.partition("-")[0]
        by_scope[scope] = by_scope.get(scope, 0) + len(fields)
    harness_n = len(secrets_store.list_harness_secrets())
    try:
        skills_n = len((await skill_service.list_skills())[0])
    except Exception:
        skills_n = 0
    return {"secrets": {"total": sum(by_scope.values()) + harness_n,
                        "by_scope": by_scope, "harness": harness_n,
                        "connections": len(conns)},
            "env_keys": [n for n, _ in SETUP_ENV_VARS
                         if os.environ.get(n) is not None],
            "skills": skills_n}


@app.post("/api/v1/settings/import/preview")
async def import_preview(body: dict):
    """Stateless preview: parse, decrypt if needed, diff vs current. No
    values in the response — presence, names, counts, warnings only."""
    try:
        bundle = _open_uploaded_bundle(body)
    except _PassphraseRequired:
        return {"needs_passphrase": True}
    try:
        diff = diff_bundle(bundle, config, dev_types)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    return {"sections_present": bundle.get("sections")
            or [k for k in ("config", "secrets", "setup_env") if k in bundle],
            "created_at": bundle.get("created_at"),
            "devcake_tag": bundle.get("devcake_tag", ""),
            "plaintext_secrets": bool(bundle.get("plaintext_secrets")),
            "has_skills": bool((bundle.get("skills") or {}).get("embedded")),
            "summary": diff, "warnings": diff["warnings"]}


@app.post("/api/v1/settings/import")
async def import_settings(body: dict):
    """Import LANDS AS A PROFILE (never applies): the risky transfer is
    decoupled from the risky world-swap. No runs guard here — nothing about
    the live world changes except (opt-in) additive skill-store writes."""
    try:
        bundle = _open_uploaded_bundle(body)
    except _PassphraseRequired:
        raise HTTPException(422, "bundle is encrypted — supply the passphrase")
    name = str(body.get("save_as") or "")
    try:
        parsed = validate_bundle(bundle)
        stored = {k: v for k, v in bundle.items()
                  if k not in ("setup_env", "skills", "plaintext_secrets")}
        stored["sections"] = [s for s in ("config", "secrets") if s in stored]
        if not stored["sections"]:
            raise BundleError(422, "bundle carries no config or secrets "
                                   "section to save as a profile")
        profiles_store.save_profile(name, stored,
                                    overwrite=bool(body.get("overwrite")))
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    warnings = list(parsed["warnings"])
    skills_imported: list[str] = []
    if body.get("import_skills"):
        for skill in (bundle.get("skills") or {}).get("embedded") or []:
            try:
                skill_name = skill_service.validate_import(skill.get("files") or [])
                await skill_service.save_skill(skill_name,
                                               skill.get("files") or [],
                                               overwrite=True)
                skills_imported.append(skill_name)
            except SkillStoreError as e:
                warnings.append(f"skill {skill.get('name')!r}: {e}")
    audit_event("settings_imported",
                f"save_as={name} sections={'+'.join(stored['sections'])} "
                f"skills={len(skills_imported)}")
    return {"saved_as": name, "sections": stored["sections"],
            "has_setup_env": "setup_env" in bundle,
            "skills_imported": skills_imported, "warnings": warnings}


@app.post("/api/v1/settings/import/env")
async def import_env(body: dict):
    """Section C → a generated .env download. No server state changes and no
    runs guard — the app cannot write the host's .env; the operator places
    the file and restarts the stack."""
    try:
        bundle = _open_uploaded_bundle(body)
    except _PassphraseRequired:
        raise HTTPException(422, "bundle is encrypted — supply the passphrase")
    try:
        parsed = validate_bundle(bundle)
    except BundleError as e:
        raise HTTPException(e.status, str(e))
    if parsed["setup_env"] is None:
        raise HTTPException(422, "bundle has no setup_env section")
    return PlainTextResponse(
        generate_env_file(parsed["setup_env"]),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="devcake.env"'})


# ── per-Mission-Type prompt templates (v0.1.1) ───────────────────────────────

@app.get("/api/v1/prompt-templates")
async def get_prompt_templates():
    """Every stored template per mission type (the built-in default first),
    the per-type variable allowlists (drives the SPA's hint chips), and the
    active selection."""
    from ..prompts import PLAYBOOK_VARS
    return {
        "variables": {mt: list(v) for mt, v in PLAYBOOK_VARS.items()},
        "templates": prompt_templates.list_templates(),
        "active": {mt: config.active_prompt_templates.get(mt, "Development")
                   for mt in PLAYBOOK_VARS},
        # dev-type identifying-prompt templates (2026-07-15): same workflow
        # names, per Dev Type; all editable (Development is seeded user data)
        "dev_types": prompt_templates.list_devtype_prompts(dev_types),
        "active_dev": {n: config.active_devtype_prompts.get(n, "Development")
                       for n in dev_types},
    }


@app.put("/api/v1/prompt-templates/{mission_type}/{name}")
async def put_prompt_template(mission_type: str, name: str, body: dict):
    text = body.get("template")
    if not isinstance(text, str):
        raise HTTPException(422, "body must carry a string 'template'")
    try:
        prompt_templates.save_template(mission_type, name, text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"mission_type": mission_type, "name": name, "saved": True}


@app.delete("/api/v1/prompt-templates/{mission_type}/{name}")
async def delete_prompt_template(mission_type: str, name: str):
    if config.active_prompt_templates.get(mission_type) == name:
        raise HTTPException(
            409, f"template {name!r} is the ACTIVE template for "
                 f"{mission_type} — switch back to 'default' first")
    try:
        prompt_templates.delete_template(mission_type, name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"deleted": True}


@app.put("/api/v1/devtype-prompts/{dev_type}/{name}")
async def put_devtype_prompt(dev_type: str, name: str, body: dict):
    if dev_type not in dev_types:
        raise HTTPException(404, f"no Dev Type named {dev_type!r}")
    text = body.get("template")
    if not isinstance(text, str):
        raise HTTPException(422, "body must carry a string 'template'")
    try:
        prompt_templates.save_devtype_prompt(dev_type, name, text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return {"dev_type": dev_type, "name": name, "saved": True}


@app.delete("/api/v1/devtype-prompts/{dev_type}/{name}")
async def delete_devtype_prompt(dev_type: str, name: str):
    active = config.active_devtype_prompts.get(dev_type, "Development")
    if active == name or (name == "Development" and active in ("Development",)):
        raise HTTPException(409, f"template {name!r} is ACTIVE for "
                                 f"{dev_type} — switch first")
    try:
        prompt_templates.delete_devtype_prompt(dev_type, name)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    return {"deleted": True}


@app.get("/api/v1/harnesses")
async def list_harnesses():
    """The harness registry — image + credential requirements per
    harness_template. Read-only; the admin Dev Type card derives its display
    (including previews of unsaved harness switches) from this."""
    return {name: {"docker_image": h.image,
                   "default_model": h.default_model,
                   "credential_env": h.credential_env,
                   "credential_files": [cf.model_dump() for cf in h.credential_files],
                   "oauth_available": h.oauth is not None,
                   "skills_dir": h.skills_dir}
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
    prompt_templates.seed_devtype_prompts({dt.name: dt})
    return dt.model_dump()


@app.post("/api/v1/dev-types/{name}/rename")
async def rename_dev_type(name: str, body: dict):
    """Rename a Dev Type in place (2026-07-15): moves its YAML, credential
    dir, and prompt-template dir, and remaps every reference (assignments,
    mapper, active prompt selection, breaker)."""
    import shutil
    from pathlib import Path as _P
    new = str(body.get("new_name") or "")
    if name not in dev_types:
        raise HTTPException(404, f"no Dev Type named {name!r}")
    if not re.fullmatch(r"^[A-Za-z0-9][A-Za-z0-9_-]*$", new) or ":" in new:
        raise HTTPException(422, "new_name must match ^[A-Za-z0-9][A-Za-z0-9_-]*$")
    if new in dev_types:
        raise HTTPException(409, f"a Dev Type named {new!r} already exists")
    dt = dev_types.pop(name).model_copy(update={"name": new})
    dev_types[new] = dt
    save_dev_type(dt)
    delete_dev_type(name)
    data = _P(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
    for sub in ("secrets", "config/devtype_prompt_templates"):
        src = data / sub / name
        if src.is_dir():
            shutil.move(str(src), str(data / sub / new))
    changed = False
    for mt, a in config.assignments.items():
        if a.dev_type == name:
            a.dev_type = new
            changed = True
    if config.relations_mapper.dev_type == name:
        config.relations_mapper.dev_type = new
        changed = True
    if name in config.active_devtype_prompts:
        config.active_devtype_prompts[new] = config.active_devtype_prompts.pop(name)
        changed = True
    if changed or True:
        save_config(config)
    if name in shared_breakers:
        shared_breakers[new] = shared_breakers.pop(name)
    return {"renamed": True, "name": new}


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
    shared_breakers.pop(name, None)   # fresh credential clears the breaker
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
    # Independent review is default config, not a hard invariant (ISSUES #19)
    warnings: list[str] = []
    ex = new.get("EXECUTE")
    rev = new.get("REVIEW")
    if ex and rev and ex.dev_type == rev.dev_type:
        msg = (f"EXECUTE and REVIEW share Dev Type {ex.dev_type!r} — "
               "independent AI review is not enforced")
        log.warning(msg)
        warnings.append(msg)
    config.assignments = new
    save_config(config)
    # warnings ride in their own field — mixing them into the mission-type
    # mapping handed clients a phantom "_warnings" mission type
    return {"assignments": {k: v.model_dump()
                            for k, v in config.assignments.items()},
            "warnings": warnings}


# ── GUI-stored secrets (M12, F5): write-only VALUES, never echoed back ───────

_SECRET_SCOPES = set(secrets_store.CONNECTION_FIELDS)
# per-scope field allowlist — ONE definition (secrets.CONNECTION_FIELDS,
# shared with settings_bundle); scope/instance/field all reach the filesystem
# as path components, so every entry point validates against it (audit A5/A9)
_SECRET_FIELDS = secrets_store.CONNECTION_FIELDS
_HARNESS_VAR_RE = re.compile(f"^{HARNESS_VAR_PATTERN}$")   # one definition: config.py


def _valid_secret_ref(scope: str, instance: str, field: str) -> bool:
    return (scope in _SECRET_FIELDS and field in _SECRET_FIELDS[scope]
            and re.fullmatch(_INSTANCE_NAME_RE, instance) is not None)


def _require_secret_ref(scope: str, instance: str, field: str) -> None:
    if scope not in _SECRET_SCOPES:
        raise HTTPException(404, f"unknown secret scope {scope!r}")
    if not _valid_secret_ref(scope, instance, field):
        raise HTTPException(
            422, f"invalid secret ref: instance must match {_INSTANCE_NAME_RE}"
                 f" and field ∈ {sorted(_SECRET_FIELDS[scope])}")


def _require_harness_var(var: str) -> None:
    if not _HARNESS_VAR_RE.fullmatch(var):
        raise HTTPException(422, "harness var must match ^[A-Z][A-Z0-9_]{0,63}$")


@app.put("/api/v1/secrets/{scope}/{instance}/{field}")
async def put_secret(scope: str, instance: str, field: str, body: dict):
    """Store a connection secret VALUE (never echoed). scope ∈ pmo|repo;
    instance is the config instance name; field ∈ api_key|token|token_ro|
    reviewer_token. Writing a repo/pmo secret clears any latched breaker."""
    _require_secret_ref(scope, instance, field)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    secrets_store.write_connection_secret(scope, instance, field, value)
    if scope == "repo":
        forge_runtime.breakers.pop(instance, None)
        _protection_cache["ts"] = 0.0
    # adapters capture credentials by VALUE at construction — a rotated
    # secret takes effect only through a rebuild, same as a config PUT
    reload_connections()
    return secrets_store.connection_status(scope, instance, field)


@app.delete("/api/v1/secrets/{scope}/{instance}/{field}")
async def delete_secret(scope: str, instance: str, field: str):
    _require_secret_ref(scope, instance, field)
    secrets_store.delete_connection_field(scope, instance, field)
    if scope == "repo":
        forge_runtime.breakers.pop(instance, None)
        _protection_cache["ts"] = 0.0
    reload_connections()
    return {"present": False}


@app.put("/api/v1/harness-secrets/{var}")
async def put_harness_secret(var: str, body: dict):
    """Store a harness/model key VALUE (e.g. ANTHROPIC_API_KEY)."""
    _require_harness_var(var)
    value = body.get("value")
    if not isinstance(value, str) or not value:
        raise HTTPException(422, "value must be a non-empty string")
    secrets_store.write_harness_secret(var, value)
    # fresh key clears the DEV_AUTH breaker of every dev type running a
    # harness that consumes this var (mirrors the credential-file path)
    for dt_name, dt in dev_types.items():
        if var in HARNESSES[dt.harness_template].credential_env:
            shared_breakers.pop(dt_name, None)
    return secrets_store.harness_status(var)


@app.delete("/api/v1/harness-secrets/{var}")
async def delete_harness_secret(var: str):
    """Revoke a stored harness/model key (audit A10) — previously a
    compromised key could only be overwritten, never removed, from the GUI.
    No reload needed: harness keys are read live at dispatch."""
    _require_harness_var(var)
    secrets_store.delete_harness_secret(var)
    return {"present": False}


@app.get("/api/v1/secrets-check")
async def secrets_check(conn: str = "", harness: str = ""):
    """Presence + updated_at (NEVER the value) for the ✓/✗ UI. `conn` is a
    comma list of scope:instance:field triples; `harness` a comma list of
    var names. Invalid refs are silently dropped — they previously reached
    the filesystem, an existence/mtime oracle for arbitrary *.json paths
    (audit A5)."""
    out: dict = {"conn": {}, "harness": {}}
    for triple in (t for t in conn.split(",") if t):
        parts = triple.split(":")
        if len(parts) == 3 and _valid_secret_ref(*parts):
            out["conn"][triple] = secrets_store.connection_status(*parts)
    for var in (v for v in harness.split(",") if v):
        if _HARNESS_VAR_RE.fullmatch(var):
            out["harness"][var] = secrets_store.harness_status(var)
    return out


@app.get("/api/v1/connections/registry")
async def connections_registry():
    """Available PMO systems and forges with display metadata — drives the
    admin Config page's selectors and paste guard, so adding an adapter never
    means editing the SPA (docs/11)."""
    from ..adapters.registry import PMO_SYSTEMS, forges
    forge_descriptors = forges()
    return {
        "pmo_systems": [{"id": s.id, "display_name": s.display_name}
                        for s in PMO_SYSTEMS.values()],
        "forges": [{"id": d.id, "display_name": d.display_name}
                   for d in forge_descriptors.values()],
        "secret_shape_prefixes": sorted(
            {p for s in PMO_SYSTEMS.values() for p in s.secret_shape_prefixes}
            | {p for d in forge_descriptors.values()
               for p in d.secret_shape_prefixes}),
        "managed_labels_expected": len(ALL_LABELS),
    }


@app.post("/api/v1/connections/pmo/{name}/test")
async def test_pmo(name: str):
    inst = next((i for i in config.pmos if i.name == name), None)
    if inst is None:
        raise HTTPException(404, f"no PMO instance named {name!r}")
    if not inst.configured:
        return {"ok": False, "error": "team key is empty — the instance is "
                                      "idle until one is set"}
    if not inst.api_key:
        return {"ok": False, "error": "API key not set — enter it on the "
                                      "Config page (it is stored securely, "
                                      "never in .env)"}
    mgr = managers.get(name)
    if mgr is None:
        return {"ok": False, "error": "instance not active — save the config "
                                      "first, then test"}
    try:
        h = await mgr.pmo.health_probe(inst.team_key)
        missions = await mgr.pmo.list_all(inst.team_key)
        return {"ok": h.ok, "instance": name,
                "team": h.workspace or inst.team_key,
                "labels": h.managed_labels_present,
                "labels_expected": h.managed_labels_expected,
                "missions_visible": len(missions)}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.post("/api/v1/connections/forge/{name}/test")
async def test_forge(name: str):
    inst = next((r for r in config.repos if r.name == name), None)
    if inst is None:
        raise HTTPException(404, f"no repo named {name!r}")
    if not inst.configured:
        return {"ok": False, "error": "repository URL is empty — the repo is "
                                      "idle until one is set"}
    f = forge_runtime.get(name)
    if f is None:
        return {"ok": False, "error": "repo not active — save the config "
                                      "first, then test"}
    # a read-only token alone is a valid, testable state (reference-only —
    # founder decision 2026-07-15); only ZERO stored tokens refuses
    if not inst.token and not inst.token_ro:
        return {"ok": False, "error": "no token stored — enter an Access "
                                      "token (work repo) or a Read-only token "
                                      "(reference-only) on this card"}
    try:
        health = await forge_runtime.refresh_health(name)
        if not health["ok"]:
            return health
        # reference-only: read access is the WHOLE contract — the PR-listing
        # and branch-protection probes need API scopes a read-only PAT may
        # lack, and DevCake never opens PRs here anyway
        if inst.reference_only:
            return {"ok": True, "repo_name": name, "forge": inst.forge,
                    "repo": inst.url, "can_push": False,
                    "reference_only": True,
                    "reviewer_token_configured": False, "probe_pr": None,
                    "branch_protection": None}
        # v4 allows a repo-only (0-pmo) config — probe with the SYS
        # pseudo-instance then (HELLO/OAUTH precedent, never a real branch)
        probe = config.pmos[0].name if config.pmos else "sys"
        pr = await f.get_pr_by_branch(mission_branch(probe, "__connection_test__"))
        reviewer = bool(getattr(f, "reviewer_token", None))
        protection = await f.default_branch_protection(inst.default_branch)
        return {"ok": True, "repo_name": name, "forge": inst.forge,
                "repo": inst.url, "can_push": health["can_push"],
                "reference_only": inst.reference_only,
                "reviewer_token_configured": reviewer, "probe_pr": pr is None,
                "branch_protection": protection.model_dump() if protection else None}
    except Exception as e:
        return {"ok": False, "error": str(e)[:300]}


@app.get("/api/v1/internal-repos")
async def list_internal_repos():
    """Read-only admin surface (M11, founder decision): the auto-created
    internal-forge repos. Empty list when the internal forge is disabled."""
    if internal_forge is None:
        return {"repos": [], "ui_url": None}
    try:
        repos = await internal_forge.list_repos()
    except Exception as e:
        raise HTTPException(502, f"internal forge unreachable: {str(e)[:200]}")
    return {"repos": [r.model_dump() for r in repos],
            "ui_url": os.environ.get("GITEA_UI_URL", "http://localhost:3300")}


@app.post("/api/v1/internal-repos/create")
async def create_internal_repo(body: dict):
    """Operator repo on the bundled Gitea (item 4): created in the separate
    devcake-repos org (never listed or swept by the per-mission surface
    above); the card's token set is minted and stored under repo:{name}, so
    saving a repo card with this name + the returned clone_url completes
    the setup."""
    if internal_forge is None:
        raise HTTPException(503, "internal forge is disabled "
                                 "(GITEA_ADMIN_PASSWORD unset)")
    name = str(body.get("name") or "")
    if not re.fullmatch(_INSTANCE_NAME_RE, name):
        raise HTTPException(422, f"name must match {_INSTANCE_NAME_RE} "
                                 f"(it doubles as the repo card name)")
    try:
        return await internal_forge.create_operator_repo(name)
    except ValueError as e:
        raise HTTPException(409, str(e))
    except Exception as e:
        raise HTTPException(502, f"internal forge: {str(e)[:200]}")


@app.get("/api/v1/skills")
async def list_skills():
    """Skill store catalog (v1): store-listed when the internal forge is up,
    bundled fallback otherwise — `store` tells the UI which it is (and where
    to edit)."""
    skills, store_status = await skill_service.list_skills()
    return {"skills": [s.model_dump() for s in skills], "store": store_status}


@app.post("/api/v1/skills")
async def create_skill(body: dict):
    """'Add skill' form (docs/11): name + trigger description + markdown
    body. Frontmatter is generated app-side — the operator never touches
    YAML. 409 on collision unless overwrite is set."""
    name = str(body.get("name") or "").strip()
    description = str(body.get("description") or "").strip()
    md = str(body.get("body") or "").strip()
    if not (name and description and md):
        raise HTTPException(422, "name, description and instructions are "
                                 "all required")
    try:
        await skill_service.save_skill(
            name, skill_service.compose_skill(name, description, md),
            overwrite=bool(body.get("overwrite")))
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, "name": name}


@app.post("/api/v1/skills/import")
async def import_skill(body: dict):
    """Import an uploaded skill: files = [{path, content_b64}] relative to
    the skill dir, one of them SKILL.md — the name comes from its
    frontmatter. 409 on collision unless overwrite is set."""
    files = body.get("files") or []
    try:
        name = skill_service.validate_import(files)
        await skill_service.save_skill(name, files,
                                       overwrite=bool(body.get("overwrite")))
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True, "name": name}


@app.delete("/api/v1/skills/{name}")
async def delete_skill_endpoint(name: str):
    """Remove an operator skill (built-ins refuse — they re-seed at boot)."""
    try:
        await skill_service.delete_skill(name)
    except SkillStoreError as e:
        raise HTTPException(e.status, str(e))
    return {"ok": True}


@app.post("/api/v1/skills/sync")
async def sync_skills():
    """Re-seed missing built-in skills without a restart — heals a first
    boot where Gitea came up after the app, and re-seeds after upgrades.
    Never overwrites operator edits (missing paths only)."""
    if internal_forge is None:
        raise HTTPException(503, "internal forge is disabled "
                                 "(GITEA_ADMIN_PASSWORD unset)")
    try:
        await internal_forge.ensure_skill_store(skill_service.builtin_seed())
    except Exception as e:
        raise HTTPException(502, f"internal forge: {str(e)[:200]}")
    return {"ok": True}


@app.delete("/api/v1/internal-repos/{name}")
async def delete_internal_repo(name: str):
    """Manual Clear (founder decision: retain-by-default, delete-on-demand).
    Refuses while a live run exists for the mission — its Dev still needs
    the repo. Deletes repo + machine user (revoking both tokens) + secret."""
    if internal_forge is None:
        raise HTTPException(404, "internal forge is not enabled")
    if any(r.repo_ref == name and r.state in ("dispatched", "running", "finalizing")
           for r in store.active()):
        raise HTTPException(409, "a live run is using this repo — wait for it "
                                 "to finish before clearing")
    try:
        await internal_forge.delete_repo(name)
    except Exception as e:
        raise HTTPException(502, f"delete failed: {str(e)[:200]}")
    forge_runtime.forges.pop(name, None)
    forge_runtime.instances.pop(name, None)
    forge_runtime.internal.discard(name)
    return {"deleted": name}


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
