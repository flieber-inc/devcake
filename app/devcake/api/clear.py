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

from ..adapters.dagu import DaguExecutor
from ..adapters.files import RunLogStore, RunStore
from ..adapters.redis import INGRESS, Messaging
from ..telemetry import OO_ORG, OO_URL

log = logging.getLogger("devcake.clear")

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
    for run in store.active():
        if run.state == "finalizing":
            continue
        try:
            await run_manager.kill(run, "failed", "operator clear-runs")
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
    if live:
        log.warning("clear: %d container(s) still reported running after "
                    "%.0fs drain — proceeding with the wipe: %s",
                    len(live), timeout_s, live)
    return {"stopped": len(stopped), "undrained": live}


def clear_local_state(store: RunStore,
                      runlog: RunLogStore | None = None) -> dict[str, int]:
    """Wipe run files, run logs, and the audit log. Config/secrets untouched."""
    runs_deleted = store.clear()
    runlogs_deleted = runlog.clear() if runlog is not None else 0
    audit_cleared = 0
    if AUDIT_PATH.exists():
        AUDIT_PATH.write_text("")
        audit_cleared = 1
    return {
        "runs_deleted": runs_deleted,
        "runlogs_deleted": runlogs_deleted,
        "audit_cleared": audit_cleared,
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
    # reply streams + any other devcake:* keys except we recreate ingress group
    async for key in r.scan_iter(match="devcake:*", count=200):
        if key == INGRESS:
            continue
        try:
            await r.delete(key)
            reply_deleted += 1
        except Exception as e:  # noqa: BLE001 — clear-runs teardown is best-effort per key; failure logged, sweep continues
            log.warning("redis delete %s: %s", key, e)
    # trim ingress rather than drop it (keeps consumer group)
    try:
        await r.xtrim(INGRESS, maxlen=0, approximate=False)
        ingress_trimmed = True
    except Exception as e:  # noqa: BLE001 — best-effort teardown; failure logged and recorded as ingress_trimmed=False, sweep continues
        log.warning("redis xtrim ingress: %s", e)
        ingress_trimmed = False
    # revoke any leftover per-run ACL users (dev-*)
    try:
        users = await r.execute_command("ACL", "USERS")
        for u in users or []:
            name = u.decode() if isinstance(u, (bytes, bytearray)) else str(u)
            if name.startswith("dev-"):
                await r.execute_command("ACL", "DELUSER", name)
                users_deleted += 1
    except Exception as e:  # noqa: BLE001 — clear-runs teardown is best-effort per subsystem; failure logged, sweep continues
        log.warning("redis ACL cleanup: %s", e)
    # in-process chunk reassembly
    messaging._chunks.clear()
    return {
        "keys_deleted": reply_deleted,
        "ingress_trimmed": ingress_trimmed,
        "acl_users_deleted": users_deleted,
    }


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
    log.warning(
        "operator clear-runs: local_runs=%s dagu_deleted=%s oo=%s "
        "activity_repos=%s",
        local.get("runs_deleted"), dagu.get("deleted"), oo.get("deleted"),
        activity.get("deleted"),
    )
    return {
        "ok": not dagu.get("failed") and not oo.get("errors") and "error" not in dagu
              and "error" not in oo and "error" not in redis_info
              and not activity.get("errors") and "error" not in drain
              and not drain.get("undrained"),
        "stopped": drain,
        "local": local,
        "dagu": dagu,
        "openobserve": oo,
        "redis": redis_info,
        "activity_repos": activity,
        "preserved": ["config", "secrets", "pmo", "operator repos",
                      "skill-store", "work repos (devcake-internal)"],
    }
