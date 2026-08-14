"""The service graph: every live object the app is made of (ADR-0028).

`build_services()` is THE composition root — the module-scope body that
api/main.py used to execute at import time, moved behind an explicit
factory call. Importing api.main (or this module) builds NOTHING: no /data
reads-that-write, no adapter/client construction, no threads. The lifespan
calls `build_services()` once; tests install a `fakes.make_services(...)`
instead and never touch the real factory (it reads /data and writes
config.yaml).

Two invariants carried over from ADR-0015 (identity is the contract):

* Hot reload MUTATES in place, never rebinds — `apply_config_patch` setattrs
  fields on the stable `config` object, `build_managers` reconciles the
  `managers`/`stewards` dicts in place, and the shared dicts
  (`shared_breakers`, `shared_backend_degraded`) keep one identity that
  every MissionManager holds. Nothing outside build_services() may replace
  these objects.
* `poll_rt` is constructed ONCE with live references and never rebuilt on
  config reload (ADR-0015 Decision 2). Building it BEFORE the
  BlockerLocator dissolves the old `_mission_owner_of` module-global
  late-binding hack — the locator's owner lookup closes over the already-
  constructed runtime.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from ..adapters.claims_writer import make_claims_writer
from ..adapters.dagu import DaguExecutor
from ..adapters.files import RunLogStore, RunStore
from ..adapters.redis import Messaging
from ..adapters.registry import make_forge, make_internal_forge, make_pmo
from .. import security
from ..config import AppConfig, DevType, load_config, load_dev_types
from ..domain.blocker_locator import BlockerLocator
from ..domain.model import ALL_LABELS
from ..domain.oauth import OAuthManager
from ..domain.orchestrator import FinalizerRouter, StewardService, MissionManager
from ..domain.forge_runtime import ForgeRuntime
from ..domain.repo_mirror import RepoCache
from ..domain.workspaces import WorkspaceStore
from ..domain.runs import RunManager
from ..domain.cron_service import CronService
from ..domain.skills import SkillService
from .health import reset_health_caches
from .poll import PollRuntime

log = logging.getLogger("devcake")


def _log_task_death(t: asyncio.Task) -> None:
    if not t.cancelled() and t.exception():
        log.error("background task %s DIED", t.get_name(), exc_info=t.exception())


@dataclass
class Services:
    """The live object graph. Deliberately NOT frozen and NOT slotted:
    tests monkeypatch per instance (a stub `reload_connections` becomes an
    instance attribute shadowing the method — the sanctioned seam)."""

    config: AppConfig
    dev_types: dict[str, DevType]
    store: RunStore
    runlog: RunLogStore
    messaging: Messaging
    executor: DaguExecutor
    workspaces: WorkspaceStore
    manager: RunManager
    shared_breakers: dict[str, str]
    shared_backend_degraded: dict[str, str]
    managers: dict[str, MissionManager]
    stewards: dict[str, StewardService]
    forge_runtime: ForgeRuntime
    repo_cache: RepoCache
    internal_forge: Any
    skill_service: SkillService
    # wired by build_services() after construction (poll_rt must exist
    # before the locator; the router/oauth manager hold the finished graph)
    poll_rt: Optional[PollRuntime] = None
    blocker_locator: Optional[BlockerLocator] = None
    finalizer: Optional[FinalizerRouter] = None
    oauth_mgr: Optional[OAuthManager] = None
    cron: Any = None
    claims: Any = None

    def build_managers(self) -> None:
        """(Re)build the manager set IN PLACE to match config.pmos: existing
        managers keep their advisory state (grace, anomalies, merge windows)
        and get repointed adapters; removed instances drop theirs — never
        leaked."""
        live = {i.name: i for i in self.config.pmos if i.configured}
        for name in [n for n in self.managers if n not in live]:
            self.managers.pop(name)
            self.stewards.pop(name, None)
        for name, inst in live.items():
            p = make_pmo(inst)
            if name in self.managers:
                mgr = self.managers[name]
                mgr.pmo, mgr.forges, mgr.config = p, self.forge_runtime, self.config
                mgr.labels_ready = False   # repoint may change team_key (F3 latch)
                mgr.instance, mgr.instance_name = inst, name
                mgr.internal_forge = self.internal_forge
                mgr.skills = self.skill_service
                mgr.blocker_locator = self.blocker_locator
                mgr.repo_cache = self.repo_cache
            else:
                self.managers[name] = MissionManager(
                    self.config, self.dev_types, p, self.forge_runtime,
                    self.manager, self.messaging,
                    instance=inst, breakers=self.shared_breakers,
                    internal_forge=self.internal_forge,
                    skills=self.skill_service,
                    backend_degraded=self.shared_backend_degraded,
                    blocker_locator=self.blocker_locator,
                    repo_cache=self.repo_cache)
                self.stewards[name] = StewardService(
                    self.config, self.dev_types, self.managers[name])
                # ADR-0033: harvest's event trigger for the discovery lane —
                # injected here so domain never imports upward (the
                # breakers-injection precedent)
                self.managers[name].discovery_notify = \
                    self.stewards[name].kick_discovery
            self.managers[name].claims = self.claims

    def managers_in_config_order(self) -> list[MissionManager]:
        return [self.managers[i.name] for i in self.config.pmos
                if i.name in self.managers]

    async def refresh_forge_health(self) -> dict[str, dict]:
        """Probe every configured repo; the runtime latches/clears per repo."""
        return await self.forge_runtime.refresh_all()

    def reload_connections(self) -> None:
        """Hot-reload adapters after a config PUT: rebuild the forge,
        reconcile the manager set, and re-ensure the managed labels per
        instance — bootstrap is otherwise startup-only, so a hot-swapped
        team_key would run unlabeled until restart."""
        self.forge_runtime.rebuild(self.config.repos, make_forge)
        self.build_managers()
        reset_health_caches()              # connections/repos may have changed — reprobe
        # the adapter set feeds secret_env_vars (descriptor env lists), so the
        # redaction scan cache must rebuild with it (2026-08 F17)
        security.invalidate_secret_scan()

        async def _ensure():
            # ADR-0030: opportunistic board repair — a config PUT or secret
            # write is a natural moment to heal a board that was missing at
            # boot (Gitea down). No-op when already healthy, so the reload
            # it can itself trigger terminates immediately.
            try:
                from .default_board import ensure_default_board
                await ensure_default_board(self)
            except Exception:
                log.exception("default board re-ensure failed — retried at "
                              "the next reload or boot")
            for mgr in list(self.managers.values()):
                try:
                    await mgr.pmo.ensure_labels(mgr.instance.team_key,
                                                ALL_LABELS)
                except Exception:
                    log.exception(
                        "ensure_labels after config reload failed for "
                        "instance %s — ensured on next restart",
                        mgr.instance_name)
            await self.refresh_forge_health()
        asyncio.create_task(_ensure(), name="ensure_labels_reload") \
            .add_done_callback(_log_task_death)


def build_services() -> Services:
    """Construct the whole graph — the moved api/main.py module body.

    Only the lifespan calls this (once, at startup): it reads /data, WRITES
    config.yaml (`load_config`'s normalize-on-load), and constructs live
    adapters. A corrupt config.yaml therefore crashes at STARTUP now, not at
    import — same fail-loudly, new log position (ADR-0028 release note).
    Tests wire `fakes.make_services(...)` instead.
    """
    config = load_config()
    dev_types = load_dev_types()
    store = RunStore()
    runlog = RunLogStore()
    # REDIS_* read HERE — no longer frozen at import time
    messaging = Messaging(os.environ.get("REDIS_URL", "redis://redis:6379/0"),
                          os.environ.get("REDIS_PASSWORD", ""))
    executor = DaguExecutor()
    # ADR-0025: per-run workspace tree on the host bind (compose mounts
    # $DEVCAKE_WS_HOST here rw; the dev-run DAG binds per-run subdirs)
    workspaces = WorkspaceStore("/workspaces")
    manager = RunManager(store, messaging, executor, workspaces=workspaces,
                         config=config)

    # ── multi-instance wiring (schema v3, ADR-0009; repos plural per M10) ──
    # One MissionManager per CONFIGURED PMO instance; shared state is
    # injected: the RunManager/store (global concurrency), ONE dev-type
    # breakers dict (credentials are DevCake-global), and ONE ForgeRuntime
    # (repos belong to the deployment — missions from any instance can route
    # to any repo). shared_backend_degraded is the same injected-shared-dict
    # idiom but a DIFFERENT thing: it throttles a dev type to one probe run
    # when its model backend looks sick (ADR-0018). PollRuntime is the sole
    # writer; managers only read it.
    forge_runtime = ForgeRuntime()
    # ADR-0024: the ONE deployment-wide source mirror (repos are
    # deployment-global, so the mirror is too — same shared-runtime idiom)
    repo_cache = RepoCache(config, forge_runtime)
    # the bundled internal fallback forge (M11): provisioner is admin-
    # credentialed (GITEA_ADMIN_*); None disables the zero-repo un-gating
    # when Gitea is absent
    internal_forge = (make_internal_forge()
                      if os.environ.get("GITEA_ADMIN_PASSWORD") else None)
    # skill store (docs/16 skill store v1): store-first reads via the
    # internal forge; bundled copies keep built-in skills working forge-less;
    # `<card>/<skill>` external skills read the ADR-0024 mirror (ADR-0016
    # addendum) — same shared config identity, no second cache
    s = Services(
        config=config, dev_types=dev_types, store=store, runlog=runlog,
        messaging=messaging, executor=executor, workspaces=workspaces,
        manager=manager, shared_breakers={}, shared_backend_degraded={},
        managers={}, stewards={}, forge_runtime=forge_runtime,
        repo_cache=repo_cache, internal_forge=internal_forge,
        skill_service=SkillService(internal_forge, repo_cache=repo_cache,
                                   config=config),
        claims=make_claims_writer(config, internal_forge, repo_cache))
    s.cron = CronService(config, s.managers, claims=s.claims,
                         dev_types=s.dev_types)

    # The poll machinery (ownership claims, cycle driver, missions cache,
    # loop) lives in api/poll.py — constructed ONCE with live references and
    # never rebuilt on config reload (ADR-0015 Decision 2).
    # `poll_rt.missions_cache` keeps one stable list identity;
    # /api/v1/missions serves it by reference.
    s.poll_rt = PollRuntime(
        config=config, managers=s.managers, stewards=s.stewards, store=store,
        forge_runtime=s.forge_runtime,
        refresh_forge_health=s.refresh_forge_health,
        managers_in_config_order=s.managers_in_config_order,
        backend_degraded=s.shared_backend_degraded, repo_cache=s.repo_cache,
        cron=s.cron)
    poll_rt = s.poll_rt
    # Deployment-wide blocker resolution (ADR-0009 amendment): ONE locator
    # over the LIVE managers dict, so a native blocked_by edge to a peer
    # instance's mission gates and inherits exactly like a local one (Linear
    # v1; read-only — never claims, never writes). poll_rt already exists,
    # so its durable claim map is read directly — no late binding.
    s.blocker_locator = BlockerLocator(
        s.managers, lambda bid: poll_rt.mission_owner.get(bid))

    s.forge_runtime.rebuild(config.repos, make_forge)
    s.build_managers()
    s.finalizer = FinalizerRouter(s.managers, store, messaging)
    manager.set_finalizer(s.finalizer)  # RunFinalizer seam: routes on run.pmo_ref
    s.oauth_mgr = OAuthManager(manager, messaging, dev_types,
                               breakers=s.shared_breakers)
    manager.oauth_mgr = s.oauth_mgr
    manager.runlog = runlog
    return s
