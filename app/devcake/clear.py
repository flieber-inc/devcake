"""Operator "start fresh" wipe — local run state, Dagu history, OpenObserve data.

Preserves config + secrets (and therefore Linear/GitHub/GitLab state, which
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

from .dagu import DaguExecutor
from .messaging import INGRESS, Messaging
from .state import RunStore
from .telemetry import OO_ORG, OO_URL

log = logging.getLogger("devcake.clear")

DATA_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
STATE_DIR = DATA_DIR / "state"
AUDIT_PATH = STATE_DIR / "events.jsonl"
CACHE_DIR = DATA_DIR / "cache"


def _oo_auth_header() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return "Basic " + base64.b64encode(f"{email}:{password}".encode()).decode()


def clear_local_state(store: RunStore) -> dict[str, int]:
    """Wipe run files, audit log, and rebuildable cache. Config/secrets untouched."""
    runs_deleted = store.clear()
    audit_cleared = 0
    if AUDIT_PATH.exists():
        AUDIT_PATH.write_text("")
        audit_cleared = 1
    cache_deleted = 0
    if CACHE_DIR.exists():
        for p in CACHE_DIR.rglob("*"):
            if p.is_file():
                try:
                    p.unlink()
                    cache_deleted += 1
                except OSError:
                    pass
    return {
        "runs_deleted": runs_deleted,
        "audit_cleared": audit_cleared,
        "cache_files_deleted": cache_deleted,
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
        except Exception as e:
            log.warning("redis delete %s: %s", key, e)
    # trim ingress rather than drop it (keeps consumer group)
    try:
        await r.xtrim(INGRESS, maxlen=0, approximate=False)
        ingress_trimmed = True
    except Exception as e:
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
    except Exception as e:
        log.warning("redis ACL cleanup: %s", e)
    # in-process chunk reassembly
    messaging._chunks.clear()
    messaging._chunk_totals.clear()
    return {
        "keys_deleted": reply_deleted,
        "ingress_trimmed": ingress_trimmed,
        "acl_users_deleted": users_deleted,
    }


async def clear_all(
    store: RunStore,
    executor: DaguExecutor,
    messaging: Messaging,
) -> dict[str, Any]:
    """Full operator wipe. Best-effort per subsystem; partial failures are reported."""
    local = clear_local_state(store)
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
    log.warning(
        "operator clear-runs: local_runs=%s dagu_deleted=%s oo=%s",
        local.get("runs_deleted"), dagu.get("deleted"), oo.get("deleted"),
    )
    return {
        "ok": not dagu.get("failed") and not oo.get("errors") and "error" not in dagu
              and "error" not in oo and "error" not in redis_info,
        "local": local,
        "dagu": dagu,
        "openobserve": oo,
        "redis": redis_info,
        "preserved": ["config", "secrets", "pmo", "forge"],
    }
