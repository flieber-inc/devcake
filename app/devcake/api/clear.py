"""Operator "start fresh" wipe — local run state, Dagu history, OpenObserve data.

Preserves config + secrets (and therefore PMO/forge state, which
lives outside DevCake). Documented consequence of wiping `/data/state` is
reset attempt counters (docs/10 §5, INV-1).
"""

from __future__ import annotations

import base64
import logging
import os
from pathlib import Path
from typing import Any

import httpx
from opentelemetry import trace

from ..activity import IN_FLIGHT
from ..adapters.dagu import DaguExecutor
from ..adapters.files import RunLogStore, RunStore
from ..adapters.redis import INGRESS, Messaging
from ..domain import failure_taxonomy
from ..telemetry import OO_ORG, OO_URL

log = logging.getLogger("devcake.clear")

# ProxyTracer (api/poll.py precedent): resolves the real provider per span
# start, so module scope needs no telemetry install.
tracer = trace.get_tracer("devcake")

DATA_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
STATE_DIR = DATA_DIR / "state"
AUDIT_PATH = STATE_DIR / "events.jsonl"


def _oo_auth_header() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()


async def stop_and_drain(store: RunStore, executor: DaguExecutor,
                         run_manager, timeout_s: float = 40.0) -> dict[str, Any]:
    """Stop every active run and WAIT for the containers to actually exit
    before anything is wiped (founder-scoped hardening, root cause 2026-07-18:
    clear_redis's `ACL DELUSER dev-*` raced Dagu's 30s SIGTERM grace — a
    stopping Dev re-authenticated as its deleted user and died exit 1 mid-
    teardown with `AuthenticationError: user is disabled`).

    Kill (not bare stop): RunManager.kill runs the full per-run teardown —
    executor stop, failure record to OpenObserve, terminal state. Finalizing
    runs have no container (never killed; their records are wiped right
    after). Then poll Dagu until every stopped run reports terminal, capped
    just past the SIGTERM grace so a wedged container can't hold the wipe
    hostage forever."""
    import asyncio
    import time as _time

    stopped: list[str] = []
    for snap in store.active():
        # re-read current state (audit D5 #7): the ingress consumer runs
        # concurrently, so a run may have flipped to finalizing while an
        # earlier kill was awaiting — killing it then would revert its
        # mission status against the live finalize. Skip anything no longer
        # dispatched/running. (The caller also holds poll + dispatch locks
        # for the full wipe — new launches cannot create ACL users mid-drain.)
        run = store.get(snap.run_id)
        if run is None or run.state not in ("dispatched", "running"):
            continue                                    # gone / finalizing / terminal
        try:
            await run_manager.kill(
                run, "failed", "operator clear-runs",
                error_class=failure_taxonomy.DEV_OPERATOR_STOP)
            stopped.append(run.run_id)
        except Exception:  # noqa: BLE001 — best-effort teardown: a kill failure is logged; the drain below still waits on Dagu's view
            log.exception("clear: kill failed for %s — continuing", run.run_id)
    deadline = _time.monotonic() + timeout_s
    live = list(stopped)
    while live and _time.monotonic() < deadline:
        still: list[str] = []
        for rid in live:
            try:
                status = await executor.status(rid)
            except Exception:  # noqa: BLE001 — drain probe is best-effort; an unreachable Dagu must not wedge the wipe (the cap bounds the wait)
                still.append(rid)
                continue
            detail = (status or {}).get("dagRunDetails") or {}
            label = str(detail.get("statusLabel", detail.get("status", ""))).lower()
            if status is not None and label in ("running", "queued", "started"):
                still.append(rid)
        live = still
        if live:
            await asyncio.sleep(2)

    # Force-remove pass (post-#32 residual B): soft drain timed out with
    # containers still live. App has no docker.sock — force means Dagu stop
    # (+ stop_all hammer) and a short re-poll, then wipe proceeds anyway so
    # ACL DELUSER does not wait forever. ok:false when undrained remain.
    force_attempted = False
    force_stops = 0
    if live:
        force_attempted = True
        log.error(
            "clear: %d container(s) still running after %.0fs soft drain — "
            "force-stop via Dagu then re-poll: %s",
            len(live), timeout_s, live,
        )
        for rid in list(live):
            try:
                await executor.stop(rid)
                force_stops += 1
            except Exception:  # noqa: BLE001 — force stop is best-effort per run; re-poll decides undrained
                log.exception("clear: force stop failed for %s", rid)
        stop_all = getattr(executor, "stop_all", None)
        if callable(stop_all):
            try:
                await stop_all()
            except Exception:  # noqa: BLE001 — stop_all hammer is best-effort; residual undrained reported below
                log.exception("clear: executor.stop_all failed during force pass")
        # short re-poll (capped; does not re-open the soft-drain budget)
        force_deadline = _time.monotonic() + min(15.0, max(4.0, timeout_s * 0.25))
        while live and _time.monotonic() < force_deadline:
            still = []
            for rid in live:
                try:
                    status = await executor.status(rid)
                except Exception:  # noqa: BLE001 — force re-poll best-effort; treat probe failure as still live
                    still.append(rid)
                    continue
                detail = (status or {}).get("dagRunDetails") or {}
                label = str(detail.get("statusLabel", detail.get("status", ""))).lower()
                if status is not None and label in ("running", "queued", "started"):
                    still.append(rid)
            live = still
            if live:
                await asyncio.sleep(1)
        if live:
            log.error(
                "clear: %d container(s) STILL live after force-remove — "
                "proceeding with wipe (ACL may race residual containers): %s",
                len(live), live,
            )
    return {
        "stopped": len(stopped),
        "undrained": live,
        "force_remove_attempted": force_attempted,
        "force_stops": force_stops,
    }


def clear_local_state(store: RunStore,
                      runlog: RunLogStore | None = None) -> dict[str, int]:
    """Wipe run files, run logs, the audit log, and the profiles breadcrumb.

    Config/secrets untouched. ``profiles.json`` is the ADR-0013 last-applied
    advisory sidecar (docs/10 layout) — clear-runs is the documented wipe.
    """
    runs_deleted = store.clear()
    runlogs_deleted = runlog.clear() if runlog is not None else 0
    audit_cleared = 0
    if AUDIT_PATH.exists():
        AUDIT_PATH.write_text("")
        audit_cleared = 1
    profiles_cleared = 0
    profiles_path = STATE_DIR / "profiles.json"
    if profiles_path.exists():
        try:
            profiles_path.unlink()
            profiles_cleared = 1
        except OSError:
            log.warning("clear: could not remove %s", profiles_path)
    return {
        "runs_deleted": runs_deleted,
        "runlogs_deleted": runlogs_deleted,
        "audit_cleared": audit_cleared,
        "profiles_cleared": profiles_cleared,
    }


async def clear_dagu(executor: DaguExecutor) -> dict[str, Any]:
    """Stop in-flight Devs, then delete every Dagu run record for dev-run."""
    stop_errors = await executor.stop_all()
    # brief settle so stop transitions finish before deletes (async stop)
    import asyncio
    await asyncio.sleep(1.5)

    ids = await executor.list_all_run_ids()
    deleted = 0
    failed: list[str] = []
    for rid in ids:
        ok = await executor.delete(rid)
        if ok:
            deleted += 1
        else:
            failed.append(rid)
    # second pass for anything that was still settling after stop
    if failed:
        await asyncio.sleep(2)
        still: list[str] = []
        for rid in failed:
            if await executor.delete(rid):
                deleted += 1
            else:
                still.append(rid)
        failed = still
    return {
        "stop_errors": stop_errors,
        "listed": len(ids),
        "deleted": deleted,
        "failed": failed,
    }


async def clear_openobserve() -> dict[str, Any]:
    """Delete log/trace/metric streams (data). Metadata streams and dashboards stay.

    Streams auto-recreate on next ingest (OTLP / fluent-bit).
    """
    headers = {"Authorization": _oo_auth_header()}
    deleted: list[str] = []
    errors: list[str] = []
    async with httpx.AsyncClient(timeout=30, headers=headers) as client:
        resp = await client.get(f"{OO_URL}/api/{OO_ORG}/streams")
        if resp.status_code >= 400:
            return {"deleted": [], "errors": [f"list streams: HTTP {resp.status_code}"]}
        streams = (resp.json() or {}).get("list") or []
        for s in streams:
            name = s.get("name") or ""
            stype = s.get("stream_type") or "logs"
            if stype == "metadata":
                continue  # internal index streams; not operator data
            dr = await client.delete(
                f"{OO_URL}/api/{OO_ORG}/streams/{name}",
                params={"type": stype, "delete_all": "true"},
            )
            if dr.status_code < 300:
                deleted.append(f"{name}:{stype}")
            else:
                errors.append(f"{name}:{stype} HTTP {dr.status_code}")
    return {"deleted": deleted, "errors": errors}


async def clear_redis(messaging: Messaging) -> dict[str, Any]:
    """Drop ingress backlog, per-run reply streams, and leftover per-run ACL users."""
    r = messaging.redis
    reply_deleted = 0
    users_deleted = 0
    errors: list[str] = []
    # reply streams + any other devcake:* keys except we recreate ingress group
    async for key in r.scan_iter(match="devcake:*", count=200):
        if key == INGRESS:
            continue
        try:
            await r.delete(key)
            reply_deleted += 1
        except Exception as e:  # noqa: BLE001 — clear-runs teardown is best-effort per key; failure logged, sweep continues
            log.warning("redis delete %s: %s", key, e)
            errors.append(f"delete {key}: {str(e)[:160]}")
    # trim ingress rather than drop it (keeps consumer group)
    try:
        await r.xtrim(INGRESS, maxlen=0, approximate=False)
        ingress_trimmed = True
    except Exception as e:  # noqa: BLE001 — best-effort teardown; failure logged and recorded as ingress_trimmed=False, sweep continues
        log.warning("redis xtrim ingress: %s", e)
        ingress_trimmed = False
    # revoke leftover per-run ACL users (dev-*) via the messaging chokepoint
    # so ACL SAVE + runtime-secret unregister stay coupled to DELUSER (docs/09)
    try:
        users_deleted = await messaging.revoke_leftover_run_users()
    except Exception as e:  # noqa: BLE001 — clear-runs teardown is best-effort per subsystem; failure logged, sweep continues
        log.warning("redis ACL cleanup: %s", e)
        errors.append(f"acl cleanup: {str(e)[:160]}")
    # in-process chunk reassembly via the Messaging chokepoint (no private reach-through)
    messaging.clear_chunk_assemblies()
    out: dict[str, Any] = {
        "keys_deleted": reply_deleted,
        "ingress_trimmed": ingress_trimmed,
        "acl_users_deleted": users_deleted,
    }
    if errors:
        out["errors"] = errors
    return out


async def clear_activity_repos(internal_forge) -> dict[str, Any]:
    """ADR-0014 D4 sweep: delete every activity-* repo. Runs after
    clear_dagu (Devs already stopped — nobody is mid-clone; an already-
    cloned workspace is unaffected by server-side deletion, and the next
    step's ensure_activity_repo re-creates create-or-adopt, so no live-run
    guard is needed). Per-repo failures are collected, never aborting."""
    if internal_forge is None:
        return {"deleted": 0, "errors": [], "skipped": "internal forge disabled"}
    deleted, errors = 0, []
    try:
        repos = await internal_forge.list_activity_repos()
    except Exception as e:
        log.exception("activity-repo listing failed")
        return {"deleted": 0, "errors": [f"list: {str(e)[:200]}"]}
    for r in repos:
        try:
            await internal_forge.delete_activity_repo(r.name)
            deleted += 1
        except Exception as e:
            log.exception("activity-repo delete failed: %s", r.name)
            errors.append(f"{r.name}: {str(e)[:200]}")
    return {"deleted": deleted, "errors": errors}


async def clear_all(
    store: RunStore,
    executor: DaguExecutor,
    messaging: Messaging,
    runlog: RunLogStore | None = None,
    internal_forge=None,
    run_manager=None,
    claims=None,
    config=None,
    repo_cache=None,
) -> dict[str, Any]:
    """Full operator wipe. Best-effort per subsystem; partial failures are
    reported. Order is load-bearing: stop-and-DRAIN first, wipe after — the
    ACL sweep must never race a container still inside its SIGTERM grace."""
    drain: dict[str, Any] = {"stopped": 0, "undrained": [],
                             "skipped": "no run_manager"}
    if run_manager is not None:
        try:
            drain = await stop_and_drain(store, executor, run_manager)
        except Exception as e:  # noqa: BLE001 — the wipe must proceed even if the drain phase itself fails; the failure is reported
            log.exception("stop_and_drain failed")
            drain = {"error": str(e)[:300], "stopped": 0, "undrained": []}
    local = clear_local_state(store, runlog)
    # ADR-0025 Hook E: the run records are gone, so every run-id-shaped
    # child of the workspace base goes too — unconditionally (undrained
    # containers keep writing into a lazily-detached mount that dies with
    # them). Runs inside the caller's dispatch_lock wrap, so no pre-create
    # can race the wipe.
    workspaces_removed = 0
    workspaces_error: str | None = None
    if run_manager is not None:
        try:
            workspaces_removed = run_manager.workspaces.wipe_all()
        except Exception as e:  # noqa: BLE001 — best-effort like every subsystem here; the sweep reclaims stragglers
            log.exception("workspace wipe failed")
            workspaces_error = str(e)[:300]
    dagu: dict[str, Any]
    oo: dict[str, Any]
    redis_info: dict[str, Any]
    try:
        dagu = await clear_dagu(executor)
    except Exception as e:
        log.exception("dagu clear failed")
        dagu = {"error": str(e)[:300], "deleted": 0, "listed": 0, "failed": []}
    try:
        oo = await clear_openobserve()
    except Exception as e:
        log.exception("openobserve clear failed")
        oo = {"error": str(e)[:300], "deleted": [], "errors": [str(e)[:200]]}
    try:
        redis_info = await clear_redis(messaging)
    except Exception as e:
        log.exception("redis clear failed")
        redis_info = {"error": str(e)[:300]}
    activity = await clear_activity_repos(internal_forge)
    claims_pruned: dict[str, Any] = {}
    if claims is not None and config is not None:
        try:
            from ..config import memory_bound_names
            from ..domain import claims as claims_mod
            # clear-all wipes EVERY board, and orphan claims from boards
            # deleted before the Clear have no owner left — prune every
            # claim file, not just currently-configured source names
            cards = sorted(memory_bound_names(config))
            claims_pruned = await claims_mod.prune_all(claims, cards)
        except Exception:  # noqa: BLE001 — wipe continues; claims prune is best-effort
            log.exception("claims prune during clear failed")
            claims_pruned = {"error": "claims prune failed"}
        if repo_cache is not None:
            # the prune pushed to those notebooks: the mirrors stay (they are
            # not run history) but their freshness does not — the first run
            # after a Clear must not mount the claims it just wiped
            for card, n in claims_pruned.items():
                if isinstance(n, int) and n > 0:
                    repo_cache.invalidate(card)
    log.warning(
        "operator clear-runs: local_runs=%s dagu_deleted=%s oo=%s "
        "activity_repos=%s",
        local.get("runs_deleted"), dagu.get("deleted"), oo.get("deleted"),
        activity.get("deleted"),
    )
    redis_failed = ("error" in redis_info
                    or redis_info.get("ingress_trimmed") is False
                    or bool(redis_info.get("errors")))
    ok = (not dagu.get("failed") and not oo.get("errors")
          and "error" not in dagu and "error" not in oo
          and not redis_failed
          and not activity.get("errors") and "error" not in drain
          and not drain.get("undrained")
          and "error" not in claims_pruned
          and workspaces_error is None)
    out: dict[str, Any] = {
        "ok": ok,
        "stopped": drain,
        "local": local,
        "workspaces_removed": workspaces_removed,
        "dagu": dagu,
        "openobserve": oo,
        "redis": redis_info,
        "activity_repos": activity,
        "claims_pruned": claims_pruned,
        "preserved": ["config", "secrets", "pmo", "operator repos",
                      "skill-store", "work repos (devcake-internal)",
                      "notebooks (notes stay; board claims pruned)",
                      "repo mirrors"],
    }
    if workspaces_error is not None:
        out["workspaces_error"] = workspaces_error
    return out


async def run_clear_runs(
    *,
    store: RunStore,
    executor: DaguExecutor,
    messaging: Messaging,
    runlog: RunLogStore | None,
    internal_forge,
    run_manager,
    claims,
    config,
    poll_lock,
    repo_cache=None,
    dispatch_lock,
    missions_cache,
    managers: dict,
    shared_backend_degraded: dict,
) -> dict[str, Any]:
    """HTTP clear-runs request body (ADR-0015): locks, wipe, advisory clears.

    Owns the orchestration formerly inlined in ``main.clear_runs`` so the
    route stays a ≤4-statement forward. Wipe semantics stay in ``clear_all``;
    lock order is load-bearing (poll_rt.lock → dispatch_lock).
    """
    with tracer.start_as_current_span("system.clear_runs") as span:
        # Serialize the wipe against EVERY dispatch path (audit D5 #2/#8 +
        # re-audit #0/#6). Two locks, acquired in a fixed order:
        #   • poll_rt.lock stops the periodic poll cycle (and force-poll) —
        #     the poll loop takes it at cycle start, so no cycle runs.
        #   • bootstrap.dispatch_lock is the true chokepoint: EVERY dispatch
        #     flavor (poll, oauth, steward "run now", hello) creates its ACL
        #     user + container inside RunBootstrap.launch under this lock.
        #     poll_rt.lock alone missed the three non-poll paths, so the
        #     ACL/SIGTERM race D3 targeted was still reachable — this closes it.
        # Order poll_rt.lock → dispatch_lock matches the poll loop's own order
        # (run_cycle holds poll_rt.lock, then launch takes dispatch_lock), so
        # there is no lock-ordering inversion / deadlock.
        # registered like the poll cycle it displaces: a drain plus the
        # deletes can outlast two poll intervals, and an unregistered lock
        # hold would read as a wedged loop on the activity bar
        with IN_FLIGHT.phase("system.clear_runs", "clear runs", expect_s=120,
                             state="waiting for the poll cycle") as ph:
            async with poll_lock, dispatch_lock:
                ph.set(state="clearing")
                result = await clear_all(
                    store, executor, messaging, runlog,
                    internal_forge=internal_forge,
                    run_manager=run_manager,
                    claims=claims, config=config, repo_cache=repo_cache)
        missions_cache.clear()
        for mgr in managers.values():
            mgr._grace.clear()
            mgr._grace_next.clear()
        # ADR-0018: the backend-degraded map IS run history, so "start fresh"
        # legitimately forgets it — and clearing it now avoids throttling with
        # zero evidence until the next poll cycle recomputes.
        shared_backend_degraded.clear()
        # auth breakers stay — they reflect live credential health, not run history
        span.set_attribute(
            "devcake.clear.runs_deleted",
            int((result.get("local") or {}).get("runs_deleted") or 0))
        span.set_attribute(
            "devcake.clear.dagu_deleted",
            int((result.get("dagu") or {}).get("deleted") or 0))
        span.set_attribute("devcake.clear.ok", bool(result.get("ok")))
        return result
