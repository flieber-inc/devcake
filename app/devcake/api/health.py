"""Health probes + the /api/v1/health payload builder (docs/11, docs/12).

Application-service module per ADR-0015 Decision 3: explicit parameters,
never imports main.py. Probe contract (docs/15 §7): any probe failure maps to
False/None/ok:False — /health must never 500.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time

import httpx
import redis.asyncio as aioredis

from .. import security
from ..adapters import budget as pmo_budget
from ..adapters.dagu import DAGU_URL
from ..bake_status import annotate_liveness, read_bake_status
from ..staffing import app_digest, receipt_summary
from ..domain.claims import claims_depth, claims_queue_capped
from ..domain.forge_runtime import PROBE_CONCURRENCY
from ..prompts import templates as prompt_templates
from ..telemetry import OO_URL

log = logging.getLogger("devcake.health")


def _harness_pins(dev_types, receipt_store, bake_status) -> dict:
    return receipt_summary(
        dev_types or {}, digest=app_digest(), store=receipt_store,
        bake_status=bake_status)


async def _check_redis() -> bool:
    from ..adapters.redis import redis_connect_env
    try:
        url, password = redis_connect_env()
        r = aioredis.from_url(url, password=password or None, socket_timeout=3)
        try:
            return bool(await r.ping())
        finally:
            await r.aclose()
    except Exception:  # noqa: BLE001 — probe contract: any failure → False; /health must never 500
        return False


# branch-protection probe (A2, docs/14): cached — /health is polled every 10 s
# by the SPA, the forge API needs at most one look every few minutes.
# `refreshing` is a single-flight guard (2026-08-12 review): stamping `ts`
# AFTER the probe gives self-healing (a cancelled/failed refresh retries
# next call), but WITHOUT this flag every concurrent /health caller during
# the slow O(N repos) probe launched its OWN gather — the exact forge storm
# of the 2026-08-01 boot incident this cache exists to prevent. The flag
# check+set is synchronous (no await between), so only the first caller
# refreshes; the rest serve the last value immediately (no added latency).
_protection_cache: dict = {"ts": 0.0, "value": {}, "refreshing": False}
_PROTECTION_PROBE_TIMEOUT = 5   # a hung forge must not stall the refresh

# per-instance PMO probe cache (2026-08-12 audit F3): same rationale — the
# vendor needs at most one look a minute, not one per SPA poll. Keyed by
# (instance, team_key) so a team repoint reprobes immediately.
_PMO_PROBE_TTL = 60
_PMO_PROBE_TIMEOUT = 5   # one sick PMO must not stall /health (audit F3)
_pmo_probe_cache: dict = {}


def reset_health_caches() -> None:
    """THE config-reload hook (one reset path, ADR-0034): connections or
    repos may have changed — every cached probe reprobes on the next
    /health. The admin 'Test' endpoint stays live and uncached."""
    _protection_cache["ts"] = 0.0
    _pmo_probe_cache.clear()



async def _branch_protection(forge_runtime) -> dict:
    """{repo_name: BranchProtection|None} across every configured repo.
    Bounded-parallel like ForgeRuntime.refresh_all — sequential, this walk
    stalled the first rich /health call for O(N repos) of probe I/O (same
    defect class as the 2026-08-01 boot incident)."""
    stale = (_protection_cache["ts"] == 0
             or time.monotonic() - _protection_cache["ts"] > 300)
    # fresh, or another caller is already refreshing → serve the last value
    # (single-flight; the check+set below is await-free, so only one refreshes)
    if not stale or _protection_cache["refreshing"]:
        return _protection_cache["value"]
    _protection_cache["refreshing"] = True
    try:
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        out: dict = {}

        async def _probe(name: str, f) -> None:
            # internal mission repos (ForgeRuntime.internal) are not operator
            # work cards — nobody watches the internal Gitea and apply-
            # protection walks config.repos only — so the advisory would be
            # unactionable noise; skipped before the row is even consulted
            if name in forge_runtime.internal:
                return
            try:
                inst = forge_runtime.instance(name)
                # reference-only repos: DevCake never pushes or merges there, so
                # the unprotected-default-branch advisory would be pure noise
                if inst is not None and inst.reference_only:
                    return
                async with sem:
                    async with asyncio.timeout(_PROTECTION_PROBE_TIMEOUT):
                        prot = await f.default_branch_protection(
                            inst.default_branch if inst else "main")
                    out[name] = prot.model_dump() if prot else None
            except Exception:  # noqa: BLE001 — probe contract: any per-repo failure (row lookup, timeout, forge) → None (advisory omitted); /health must never 500
                out[name] = None

        await asyncio.gather(*(_probe(n, f)
                               for n, f in list(forge_runtime.forges.items())))
        _protection_cache["value"] = out
        _protection_cache["ts"] = time.monotonic()   # ts-after: a cancelled refresh retries next call
    finally:
        # cleared even on cancellation, so a killed refresh never latches the
        # single-flight guard shut
        _protection_cache["refreshing"] = False
    return _protection_cache["value"]


async def _check_http(url: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            return (await client.get(url)).status_code < 500
    except Exception:  # noqa: BLE001 — probe contract: any failure → False; /health must never 500
        return False


# 60s-cached probe: does the OO service account actually authenticate?
# Boot auto-provisions the user (telemetry.oo_provision); this catches
# mid-runtime OO wipe / credential drift so ops see a red ingest path
# without waiting for a restart.
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
                  f"authenticate; restart app (boot re-provisions) or check "
                  f"OO_INGEST_* / OO_ROOT_*"}
    except Exception as e:  # noqa: BLE001 — probe contract: any failure → ok:False + detail; /health must never 500
        result = {"ok": False, "detail": f"probe failed: {str(e)[:150]}"}
    _oo_ingest_cache.update(ts=now, result=result)
    return result


def unused_repo_names(config, dev_types=None) -> list[str]:
    """Configured repo adapters selected by NO PMO instance (neither work,
    reference, nor memory) and by no Dev Type memory_repos binding."""
    selected: set[str] = set()
    for pmo in config.pmos:
        selected.update(pmo.repos)
        selected.update(pmo.reference_repos)
        selected.update(pmo.memory_repos or [])
    for dt in (dev_types or {}).values():
        selected.update(getattr(dt, "memory_repos", None) or [])
    # `<source>/<skill>` prefixes name dedicated skill_sources
    # (2026-08-14 ruling), never repo cards — no skills branch here. But a
    # repo card BACKING a skill source (ADR-0039) is in use: flagging it
    # unused would nudge a removal the config validator then refuses.
    for src in getattr(config, "skill_sources", None) or []:
        if getattr(src, "backed_by", ""):
            selected.add(src.backed_by)
    return sorted(r.name for r in config.repos if r.name not in selected)


# demand above this share of a vendor quota earns an advisory (headroom for
# dispatch/finalize bursts the poll cannot foresee)
BUDGET_WARN_SHARE = 0.8


def _budget_warnings(budgets: dict, poll_interval_seconds: int) -> dict[str, str]:
    """Operator advisories derived from the request budgets (ADR-0040):
    measured demand against the vendor's limit → the poll interval that
    would fit under BUDGET_WARN_SHARE, and a note when the quota is being
    spent by something other than this DevCake (another deployment, a
    person's tooling on the same key)."""
    out: dict[str, str] = {}
    for label, b in (budgets or {}).items():
        limit = b.get("limit")
        demand = sum(v for v in (b.get("demand_per_hour") or {}).values()
                     if isinstance(v, int))
        parts: list[str] = []
        if limit and demand > limit * BUDGET_WARN_SHARE:
            floor = max(1, int(math.ceil(
                poll_interval_seconds * demand / (limit * BUDGET_WARN_SHARE))))
            parts.append(
                f"measured ~{demand} requests/hour against {limit}/hour shared "
                f"by {', '.join(b.get('instances') or []) or 'this instance'}; "
                f"raise the poll interval to at least {floor} s or spread the "
                f"instances over more credentials")
        foreign = b.get("foreign_spend") or 0
        if limit and foreign > limit * 0.1:
            parts.append(
                f"~{foreign} requests this window were spent by another "
                f"consumer of the same credential (another deployment or a "
                f"person's tooling) — the budget is shared")
        if parts:
            out[label] = "; ".join(parts)
    return out


async def build_health_payload(*, config, dev_types, managers, stewards,
                               forge_runtime, shared_breakers, store,
                               internal_forge, poll_rt,
                               backend_degraded: dict | None = None,
                               repo_cache=None, workspaces=None,
                               cron=None,
                               receipt_store=None) -> dict:
    """The /api/v1/health body (docs/11 §0). All deps explicit — unit-testable
    with fakes; the route in main.py is a one-line forward."""
    redis_ok, dagu_ok, oo_ok = await asyncio.gather(
        _check_redis(),
        _check_http(f"{DAGU_URL}/api/v1/health"),
        _check_http(f"{OO_URL}/healthz"),
    )
    # per-instance PMO health (schema v3): unconfigured instances show grey
    # (ok: None), never red; the scalar `pmo` aggregate keeps the SPA's
    # health dot working (true = every configured instance probes ok).
    # Cached (60 s TTL) + CONCURRENT with a per-probe timeout (audit F3):
    # the old sequential uncached loop let one sick PMO stall /health ~30 s
    # while the SPA's 10 s cadence piled up overlapping requests.
    async def _probe_one(inst) -> tuple[str, dict]:
        if not inst.configured:
            return inst.name, {
                "ok": None, "configured": False, "team": "",
                "intake_paused": bool(inst.intake_paused),
            }
        key = (inst.name, inst.team_key)
        cached = _pmo_probe_cache.get(key)
        now = time.monotonic()
        if cached is not None and now - cached["ts"] < _PMO_PROBE_TTL:
            row = cached
        else:
            mgr = managers.get(inst.name)
            detail = ""
            rel = att = None
            try:
                async with asyncio.timeout(_PMO_PROBE_TIMEOUT):
                    h = (await mgr.pmo.health_probe(inst.team_key)
                         ) if mgr else None
                    ok = bool(h and h.ok)
                    detail = getattr(h, "detail", "") or "" if h else ""
                    try:
                        caps = mgr.pmo.capabilities() if mgr else None
                        if caps is not None:
                            rel = bool(caps.relations_supported)
                            att = bool(getattr(
                                caps, "attachments_supported", True))
                    except Exception:  # noqa: BLE001 — caps are advisory on /health
                        pass
            except Exception as e:  # noqa: BLE001 — probe contract: any failure (incl. the 5s timeout) → ok:False + detail; /health must never 500
                ok = False
                detail = f"probe failed: {str(e)[:150]}"
                log.warning("pmo probe %s failed: %s", inst.name, e)
            row = {"ts": now, "ok": ok, "detail": detail,
                   "relations_supported": rel, "attachments_supported": att}
            _pmo_probe_cache[key] = row
        out = {
            "ok": row["ok"], "configured": True, "team": inst.team_key,
            "intake_paused": bool(inst.intake_paused),
        }
        if row.get("detail"):
            out["detail"] = row["detail"]
        if row.get("relations_supported") is not None:
            out["relations_supported"] = row["relations_supported"]
        if row.get("attachments_supported") is not None:
            out["attachments_supported"] = row["attachments_supported"]
        return inst.name, out

    pmo_instances: dict[str, dict] = dict(
        await asyncio.gather(*(_probe_one(i) for i in config.pmos)))
    budgets = pmo_budget.snapshot_all()
    configured_ok = [v["ok"] for v in pmo_instances.values() if v["configured"]]
    prefixed = len(managers) > 1   # advisory text carries the instance when N>1

    def _merged(attr: str) -> dict:
        out: dict = {}
        for name, mgr in managers.items():
            for k, v in getattr(mgr, attr).items():
                key = f"{name}:{k}" if prefixed else k
                out[key] = (f"[{name}] {v}" if prefixed and isinstance(v, str)
                            else v)
        return out

    bake = annotate_liveness(read_bake_status())
    return {
        "app": True,
        "redis": redis_ok,
        "dagu": dagu_ok,
        "openobserve": oo_ok,
        "oo_ingest": await _oo_ingest_check(),
        "pmo": bool(configured_ok) and all(configured_ok),
        "pmo_instances": pmo_instances,
        "forge": forge_runtime.health,
        # the initial full sweep now rides the poll task (2026-08-01) — this
        # tells an empty/partial `forge` map apart from a completed sweep
        "forge_probe": {
            "complete": forge_runtime.last_full_probe_at is not None,
            "completed_at": (forge_runtime.last_full_probe_at.isoformat()
                             if forge_runtime.last_full_probe_at else None),
            "probed": len(forge_runtime.health),
            "configured": len(forge_runtime.forges),
        },
        # dev-type breakers + per-repo forge breakers, one map for the SPA
        "circuit_breakers": {**shared_breakers,
                             **{f"repo:{k}": v
                                for k, v in forge_runtime.breakers.items()}},
        "intake_paused": config.intake_paused,
        # poll cadence surface for the Missions "Last polled Ns ago · next in
        # ~Ns" indicator — INV-1 made visible, not hidden behind a spinner
        "last_poll_at": (poll_rt.last_poll_at.isoformat()
                         if poll_rt.last_poll_at else None),
        "poll_interval_seconds": config.poll_interval_seconds,
        # ADR-0040: per-credential request budgets (vendor quota as observed
        # on the wire) + the advisory computed from measured demand
        "pmo_budget": budgets,
        "pmo_budget_warnings": _budget_warnings(
            budgets, config.poll_interval_seconds),
        "active_runs": len(store.active()),
        "forge_protection": await _branch_protection(forge_runtime),
        "anomalies": _merged("anomalies"),
        "merge_handoffs": _merged("merge_handoffs"),
        "needs_human": _merged("needs_human"),
        "dependency_cycles": [
            ([f"{name}:{k}" for k in cyc] if prefixed else cyc)
            for name, mgr in managers.items() for cyc in mgr.cycles],
        "blocked_reasons": _merged("blocked_reasons"),
        # instances whose poll segment failed with a PERMANENT error (audit
        # A1) — the other instances keep polling; this names the sick one
        "poll_degraded": dict(poll_rt.poll_degraded),
        # dev_type → reason (ADR-0018). Deliberately NOT merged into
        # circuit_breakers: the SPA renders that whole map as ONE alert whose
        # remediation is chosen by a `repo:` prefix test, so a third namespace
        # there would tell the operator to refresh a credential that is fine —
        # and services.js would paint the Dev card red for what is a
        # self-healing throttle. Shaped like poll_degraded, placed by
        # steward_degraded (which is a plain string, not a map).
        "dev_backend_degraded": dict(backend_degraded or {}),
        "internal_forge": (await internal_forge.health()
                           if internal_forge is not None else None),
        "steward_degraded": " · ".join(
            f"[{name}] {msg}" if prefixed else str(msg)
            for name, mp in stewards.items() if (msg := mp.degraded())) or None,
        "cron_degraded": sorted(cron.degraded) if cron is not None else [],
        "memory_curator_no_board": (
            sorted(cron.no_board) if cron is not None else []),
        "claims_queue_capped": sorted(claims_queue_capped),
        "claims_depth": dict(claims_depth),
        # active templates that no longer resolve (fallback-to-default in
        # effect) — the SPA derives a dismissable alert per entry (v0.1.1)
        "prompt_template_warnings": (
            prompt_templates.template_warnings(config)
            + prompt_templates.devtype_prompt_warnings(config, dev_types)),
        "security_warnings": security.security_warnings(config),
        # adapters no PMO selects — SPA derives a dismissable hygiene alert
        # and Repositories → ⋯ offers bulk removal (2026-08-01 incident)
        "unused_repos": {
            "count": len(names := unused_repo_names(config, dev_types)),
            "names": names,
            "configured": len(config.repos),
        },
        # ADR-0024: per-mirror sync state + the volume probe. A failing
        # mirror freezes dispatch for every mission touching it (fail-closed
        # precondition), so the SPA derives a NON-dismissable alert.
        "repo_mirror": {
            "lfs": config.repo_mirror.lfs,
            "sync_max_age_seconds": config.repo_mirror.sync_max_age_seconds,
            "volume_error": getattr(repo_cache, "volume_error", None),
            "mirrors": repo_cache.health_map() if repo_cache else {},
            "disk": repo_cache.disk_stats() if repo_cache else None,
        },
        # ADR-0025: per-run workspace base (host bind). leaked > 0 means the
        # cleanup hooks AND the sweep are losing to something; disk
        # exhaustion here otherwise presents as DEV_FORGE retry churn.
        "workspaces": {
            "volume_error": getattr(workspaces, "volume_error", None),
            "leaked": workspaces.leaked_count(store) if workspaces else 0,
            "disk": workspaces.disk_stats() if workspaces else None,
        },
        "harness_pins": _harness_pins(dev_types, receipt_store, bake),
        "bake_status": bake,
    }
