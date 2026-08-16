"""Poll machinery (docs/04 §1): PollRuntime owns the cross-instance poll
cycle — durable ownership claims, per-instance segments, the missions cache,
and the periodic loop.

Constructed ONCE in the composition root with LIVE references (the managers
dict, config object, forge runtime) and never rebuilt on config reload —
hot-swapped instances appear because `build_managers()` mutates the shared
dict in place (ADR-0015 Decision 2). `missions_cache` keeps one stable list
identity for the same reason (`/api/v1/missions` serves it by reference).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ..adapters.files.owner_store import OwnerStore
from ..config import intake_blocks_dispatch
from ..domain import backend_health
from ..domain.model import ALL_LABELS, derive
from ..domain.orchestrator import MissionManager
from ..ports.pmo import PMOTransient

log = logging.getLogger("devcake")
tracer = trace.get_tracer("devcake")

# Budget for a forge-health sweep, shared by the boot sweep and the in-cycle
# re-probe (AUD-007) so neither can hold the poll lock unbounded.
FORGE_SWEEP_BUDGET_S = 60


def _claim_missions(mgr: MissionManager, fetched: list,
                    owner: dict[str, str]) -> list:
    """Cross-instance dedupe on the RAW pmo_id: a Linear project can be
    accessible to two teams, so two instances in one workspace would both
    adopt it — duplicate decomposition, label fights. The first instance to
    see it claims it (durably — see PollRuntime.mission_owner); others surface
    an anomaly and skip. Pure function (hermetically tested)."""
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


class PollRuntime:
    """The poll loop's state and drivers, extracted from the composition root.

    PERSISTENT cross-instance ownership (v0.1 plan H1 + review finding):
    pmo_id → owning instance name. Persistence is load-bearing — a per-cycle
    rebuild would flip ownership of a shared mission the moment its owner has
    one PMOTransient cycle, double-dispatching it. Released only when the
    OWNER successfully polls and no longer sees the mission, or leaves config
    (and never while a run for the mission is still active — audit A15).
    Durable across restarts via OwnerStore."""

    def __init__(self, *, config, managers: dict[str, MissionManager],
                 stewards: dict, store, forge_runtime, refresh_forge_health,
                 managers_in_config_order, owner_store: OwnerStore | None = None,
                 backend_degraded: dict[str, str] | None = None,
                 repo_cache=None, cron=None):
        self.config = config
        # ADR-0024: warm-up owner. None only in tests (loop() guards).
        self.repo_cache = repo_cache
        self.managers = managers                    # live reference
        self.stewards = stewards                      # live reference
        self.cron = cron
        self.store = store
        self.forge_runtime = forge_runtime
        self.refresh_forge_health = refresh_forge_health
        self.managers_in_config_order = managers_in_config_order
        self.owner_store = owner_store if owner_store is not None else OwnerStore()
        self.mission_owner: dict[str, str] = self.owner_store.load()
        # instance → last poll-segment error (audit A1): a PERMANENT PMO
        # failure (revoked key, deleted team) skips only that instance's
        # segment; surfaced in /health as `poll_degraded`. Cleared on green.
        self.poll_degraded: dict[str, str] = {}
        # dev_type → reason (ADR-0018). THIS class is the sole writer; every
        # MissionManager holds the same dict object and only reads it, so the
        # refresh below must mutate IN PLACE and never rebind.
        self.backend_degraded: dict[str, str] = (
            backend_degraded if backend_degraded is not None else {})
        # Wall-clock UTC of the last poll cycle that finished (periodic OR
        # manual) — /health `last_poll_at`; a stale timestamp is a signal.
        self.last_poll_at: datetime | None = None
        # Shared lock + cycle counter for the periodic loop and the manual
        # POST /api/v1/poll/run trigger: at most one cycle in flight
        # (concurrent list_all + missions_cache mutation would race).
        self.lock: asyncio.Lock = asyncio.Lock()
        self.cycle_counter: int = 0
        # poll-cycle snapshot served by /api/v1/missions (advisory — INV-1)
        self.missions_cache: list[dict] = []
        # Host baker heartbeat (None = not observed yet). Transition spans
        # fire from this, not from /health — health only reads.
        self.baker_alive: bool | None = None

    def next_cycle_id(self) -> int:
        """Advance the shared cycle counter and return the new id."""
        self.cycle_counter += 1
        return self.cycle_counter

    def release_stale_ownership(self, polled_ok: dict[str, set]) -> None:
        """Drop ownership entries whose owner left config, or whose owner
        successfully polled this cycle and no longer sees the mission (done +
        aged out of list_all, or deleted). A FAILED poll releases nothing, and
        neither does an ACTIVE run for the mission (audit A15: releasing while
        the ex-owner's run still executes lets another instance re-dispatch the
        same work — the in-flight guard only sees its own pmo_ref)."""
        in_flight = {r.mission_pmo_id for r in self.store.active()
                     if r.mission_pmo_id}
        for pmo_id, name in list(self.mission_owner.items()):
            if pmo_id in in_flight:
                continue
            if name not in self.managers:
                del self.mission_owner[pmo_id]
            elif name in polled_ok and pmo_id not in polled_ok[name]:
                del self.mission_owner[pmo_id]

    async def poll_instance(self, mgr: MissionManager,
                            cache_rows: list[dict]) -> tuple[int, int, int, set]:
        """One instance's poll segment: fetch + cross-instance dedupe + derive +
        gate + sweeps + schedule + cache rows. Returns (seen, candidates,
        dispatched, fetched_ids). Raises PMOTransient for the caller's
        per-instance skip."""
        if not mgr.labels_ready:
            # per-config-generation once-latch (audit F3): heal the managed
            # labels here — not in the read-only /health probe. Failure is
            # logged and retried next cycle; reads below tolerate missing
            # labels (boot already ensures once, main.py lifespan).
            try:
                await mgr.pmo.ensure_labels(mgr.instance.team_key, ALL_LABELS)
                mgr.labels_ready = True
            except Exception:  # noqa: BLE001 — label healing must never take the segment down; the poll's reads work without it
                log.exception("label ensure failed for %s — retrying next "
                              "cycle", mgr.instance_name)
        fetched = await mgr.pmo.list_all(mgr.instance.team_key)
        fetched_ids = {m.pmo_id for m in fetched}
        missions = _claim_missions(mgr, fetched, self.mission_owner)
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
            except Exception as e:  # noqa: BLE001 — poll guard: a forge outage gates THIS mission only (reason recorded + logged); it must never abort the whole cycle
                m.repo, m.repo_reason = None, (
                    f"internal forge unreachable — mission gated: {str(e)[:150]}")
                log.warning("repo resolution failed for %s: %s", m.key, e)
        derived = [(m, derive(m, self.config.adoption_mode)) for m in missions]
        # the gate is a poll artifact, computed EVERY cycle — pause freezes
        # dispatch, never information (docs/04 §2)
        gate = await mgr.gate_map(missions)
        await mgr.sweeps(missions)   # merge + tracking sweeps (docs/04 §1)
        # intake pause (docs/11): no NEW dispatches — in-flight runs still
        # finalize (ingress consumer) and sweeps above keep running. Global
        # master OR this instance's own switch freezes dispatch for the segment.
        if intake_blocks_dispatch(self.config, mgr.instance):
            dispatched = 0
        else:
            dispatched = await mgr.schedule(missions, gate)
            # .get: build_managers may drop the instance mid-cycle (hot reload) —
            # a KeyError here would abort the WHOLE poll cycle (review finding)
            mp = self.stewards.get(mgr.instance_name)
            if mp is not None:
                await mp.maybe_dispatch(missions)
                # ADR-0033: the discovery lane rides the same segment (and
                # the same intake guard — pause freezes all new dispatches)
                await mp.maybe_dispatch_discovery(missions)
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
            # surfaced for the Missions board card (Linear link + Done-column sort)
            "url": m.url,
            "updated_at": m.updated_at,
        } for m, d in derived)
        return (len(missions), sum(1 for _, d in derived if d.schedulable),
                dispatched, fetched_ids)

    async def run_cycle(self, cycle: int) -> None:
        """One poll cycle (docs/04 §1): per configured instance — fetch + dedupe
        + derive + gate + dispatch + sweeps — then the merged cache. A failing
        instance (PMOTransient or a PERMANENT error like a revoked key — audit
        A1) skips only ITS segment, never the whole cycle; permanent failures
        surface in /health `poll_degraded` until a green segment clears them."""
        with tracer.start_as_current_span("poll.cycle") as span:
            span.set_attribute("devcake.poll.cycle", cycle)
            try:
                # a latched repo re-probes every cycle so a transient failure
                # (or a rotated-back token) self-heals without an operator.
                # An unset last_full_probe_at means no full sweep has ever
                # completed (loop()'s initial sweep still pending, or its
                # budget expired mid-catalog) — retry until one lands, or
                # /health `forge_probe` would report "pending" forever.
                if (self.forge_runtime.breakers
                        or self.forge_runtime.last_full_probe_at is None):
                    # AUD-007: the same 60 s budget as the boot sweep. Without
                    # it, a large or sick catalog re-probes the WHOLE set every
                    # cycle under the poll lock — and a partial first sweep
                    # leaves last_full_probe_at unset, so this fires unbounded
                    # forever, serializing force-poll / clear-runs behind a
                    # multi-minute sweep. A timeout leaves it "pending" and the
                    # next cycle retries — never a stuck lock.
                    try:
                        async with asyncio.timeout(FORGE_SWEEP_BUDGET_S):
                            await self.refresh_forge_health()
                    except TimeoutError:
                        log.warning("in-cycle forge sweep exceeded its %ds "
                                    "budget — %d/%d probed; retrying next cycle",
                                    FORGE_SWEEP_BUDGET_S,
                                    len(self.forge_runtime.health),
                                    len(self.forge_runtime.forges))
                # ADR-0018 — recompute the backend-degraded map. Placement is
                # load-bearing in two ways, and BOTH fail silently if broken:
                #   * UNCONDITIONAL, at this indent. One level deeper it would
                #     sit inside the `if self.forge_runtime.breakers:` above and
                #     never run in the common case (no forge breaker latched),
                #     leaving the map empty and the brake permanently disarmed.
                #   * BEFORE the instance loop below, which is what calls
                #     poll_instance → schedule(). After it, the throttle would
                #     lag a full poll interval — long enough for the fleet to be
                #     re-dispatched at full concurrency into a broken backend.
                # `known` is the LIVE dev-type registry, unioned from the
                # managers this runtime already holds (each carries the config's
                # `dev_types`) — no `api.main` import, ADR-0015. Without it a
                # renamed or deleted dev type keeps a permanent, unclearable
                # entry (no new runs ⇒ no two greens) while the renamed type
                # dispatches at full concurrency on no evidence.
                known = {name for mgr in self.managers.values()
                         for name in mgr.dev_types}
                for dev_type in backend_health.refresh_degraded(
                        self.store.all(), self.backend_degraded, known,
                        classes=backend_health.fault_classes(
                            self.config.brake_on_bad_output)):
                    with tracer.start_as_current_span("dev.backend_degraded") as bspan:
                        bspan.set_attribute("devcake.dev_type", dev_type)
                        bspan.set_attribute("devcake.reason",
                                            self.backend_degraded[dev_type][:500])
                        bspan.set_status(Status(StatusCode.ERROR,
                                               "model backend degraded"))
                    log.warning("backend degraded for dev type %s: %s",
                                dev_type, self.backend_degraded[dev_type])
                cache_rows: list[dict] = []
                seen, cand, disp = 0, 0, 0
                polled_ok: dict[str, set] = {}       # instance → fetched pmo_ids
                owner_before = dict(self.mission_owner)  # persisted iff changed
                for mgr in self.managers_in_config_order():
                    with tracer.start_as_current_span("poll.instance") as ispan:
                        ispan.set_attribute("devcake.instance", mgr.instance_name)
                        try:
                            s, c, d, ids = await self.poll_instance(mgr, cache_rows)
                            polled_ok[mgr.instance_name] = ids
                            seen, cand, disp = seen + s, cand + c, disp + d
                            self.poll_degraded.pop(mgr.instance_name, None)
                        except PMOTransient as e:
                            # transient PMO trouble skips only THIS instance's
                            # segment — the others still poll this cycle. Not
                            # marked degraded: transient is expected weather.
                            ispan.set_attribute("devcake.outcome", "PMO_TRANSIENT")
                            log.warning("poll.cycle %d: instance %s skipped: %s",
                                        cycle, mgr.instance_name, e)
                        except Exception as e:  # noqa: BLE001 — poll loop guard: a permanent per-instance failure must not starve the remaining instances (audit A1); surfaced via poll_degraded
                            ispan.set_attribute("devcake.outcome", "INSTANCE_ERROR")
                            self.poll_degraded[mgr.instance_name] = (
                                f"{type(e).__name__}: {str(e)[:200]}")
                            log.exception("poll.cycle %d: instance %s FAILED — "
                                          "segment skipped", cycle,
                                          mgr.instance_name)
                self.release_stale_ownership(polled_ok)
                if self.mission_owner != owner_before:   # durable claim (A15)
                    self.owner_store.save(self.mission_owner)
                # a skipped instance keeps its LAST snapshot in the cache
                # (v0 behavior: PMO trouble never blanks the view)
                cache_rows.extend(
                    row for row in self.missions_cache
                    if row["instance"] in self.managers
                    and row["instance"] not in polled_ok)
                # cross-instance blocker keys (ADR-0009 amendment): each
                # segment maps blocked_by through ITS OWN id_to_key, so an id
                # owned by a PEER instance stays a raw vendor id until this
                # merged post-pass. Zero network; an id in NO instance's
                # snapshot (done + aged out) legitimately stays raw —
                # advisory display only, the gate has its own resolution.
                key_of = {row["pmo_id"]: row["key"]
                          for row in cache_rows if row.get("pmo_id")}
                for row in cache_rows:
                    if row.get("blocked_by"):
                        row["blocked_by"] = [key_of.get(b, b)
                                             for b in row["blocked_by"]]
                self.missions_cache[:] = cache_rows
                if self.cron is not None:
                    try:
                        await self.cron.maybe_fire()
                    except Exception:  # noqa: BLE001 — cron must not kill the poll cycle
                        log.exception("cron.maybe_fire failed")
                span.set_attribute("devcake.missions.seen", seen)
                span.set_attribute("devcake.missions.candidates", cand)
                span.set_attribute("devcake.missions.dispatched", disp)
                log.info("poll.cycle %d: %d missions, %d candidates, "
                         "%d dispatched (%d instances)", cycle, seen, cand,
                         disp, len(self.managers))
            except Exception:  # noqa: BLE001 — cycle guard: a poll cycle must NEVER kill the loop; logged, next tick retries
                span.set_attribute("devcake.outcome", "cycle_error")
                log.exception("poll.cycle %d failed", cycle)
            finally:
                # stamped even on cycle_error — a partial cycle IS a poll
                # attempt; /health surfaces it as `last_poll_at` (docs/11 §0)
                self.last_poll_at = datetime.now(timezone.utc)
                try:
                    await self._observe_baker()
                except Exception:  # noqa: BLE001 — baker observe must not kill the poll cycle
                    log.exception("baker observe failed")

    async def _observe_baker(self) -> None:
        """Ship baker jsonl via push_oo_log; span only on alive/dead edges.

        The baker is a host process. This is the app-side chokepoint — same
        stream shape as run_failures, same tracer as poll.cycle.
        """
        from ..bake_status import (
            annotate_liveness, baker_transition, drain_baker_log,
            read_bake_status,
        )
        from ..telemetry import push_oo_log

        status = annotate_liveness(read_bake_status())
        alive = bool(status.get("baker_alive"))
        trans = baker_transition(self.baker_alive, alive)
        if trans:
            with tracer.start_as_current_span(f"baker.{trans}") as span:
                if trans == "dead":
                    span.set_status(Status(StatusCode.ERROR))
                span.set_attribute(
                    "devcake.baker.detail", status.get("baker_detail") or "")
                span.set_attribute(
                    "devcake.baker.state", status.get("state") or "")
        self.baker_alive = alive
        for rec in drain_baker_log():
            await push_oo_log("baker", rec)

    async def loop(self) -> None:
        """Periodic poll driver. Shares `lock` and the cycle counter with
        `POST /api/v1/poll/run` — at most one cycle in flight.

        The initial full forge sweep runs here, NOT in lifespan (incident
        2026-08-01: 319 sequential probes held the listen socket ~95s+ and
        failed the compose healthcheck), but still before the first cycle:
        schedule() gates on latched breakers, so a definitively bad
        credential must latch before cycle 1 can burn an attempt on it.
        Budget is 60s because a manual poll queued on this lock must resolve
        inside the admin proxy's 60s window; on expiry the probes that did
        land already updated health incrementally, last_full_probe_at stays
        unset, and run_cycle retries the sweep next tick."""
        async with self.lock:
            try:
                async with asyncio.timeout(FORGE_SWEEP_BUDGET_S):
                    await self.refresh_forge_health()
            except TimeoutError:
                log.warning(
                    "initial forge sweep exceeded its 60s budget — "
                    "%d/%d repos probed; retrying on the next poll cycle",
                    len(self.forge_runtime.health),
                    len(self.forge_runtime.forges))
        # ADR-0024: mirror warm-up — background, UNBOUNDED, never under the
        # poll lock and never awaited (a cold 27-repo deployment clones for
        # minutes); a dispatch needing repo X coalesces onto the in-flight
        # sync via RepoCache's per-name lock.
        if self.repo_cache is not None:
            warm = asyncio.create_task(self.repo_cache.warm_all(),
                                       name="mirror_warmup")
            warm.add_done_callback(
                lambda t: t.cancelled() or t.exception() is None or log.error(
                    "mirror warm-up died: %r", t.exception()))
        while True:
            async with self.lock:
                await self.run_cycle(self.next_cycle_id())
            await asyncio.sleep(self.config.poll_interval_seconds)
