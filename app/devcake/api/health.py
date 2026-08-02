"""Health probes + the /api/v1/health payload builder (docs/11, docs/12).

Application-service module per ADR-0015 Decision 3: explicit parameters,
never imports main.py. Probe contract (docs/15 §7): any probe failure maps to
False/None/ok:False — /health must never 500.
"""

from __future__ import annotations

import asyncio
import os
import time

import httpx
import redis.asyncio as aioredis

from .. import security
from ..adapters.dagu import DAGU_URL
from ..domain.forge_runtime import PROBE_CONCURRENCY
from ..prompts import templates as prompt_templates
from ..telemetry import OO_URL

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
REDIS_PASSWORD = os.environ.get("REDIS_PASSWORD", "")


async def _check_redis() -> bool:
    try:
        r = aioredis.from_url(REDIS_URL, password=REDIS_PASSWORD or None, socket_timeout=3)
        try:
            return bool(await r.ping())
        finally:
            await r.aclose()
    except Exception:  # noqa: BLE001 — probe contract: any failure → False; /health must never 500
        return False


# branch-protection probe (A2, docs/14): cached — /health is polled every 10 s
# by the SPA, the forge API needs at most one look every few minutes
_protection_cache: dict = {"ts": 0.0, "value": {}}


def reset_protection_cache() -> None:
    """Config reload hook: repos may have changed — reprobe on next /health."""
    _protection_cache["ts"] = 0.0


async def _branch_protection(forge_runtime) -> dict:
    """{repo_name: BranchProtection|None} across every configured repo.
    Bounded-parallel like ForgeRuntime.refresh_all — sequential, this walk
    stalled the first rich /health call for O(N repos) of probe I/O (same
    defect class as the 2026-08-01 boot incident)."""
    if time.monotonic() - _protection_cache["ts"] > 300 or _protection_cache["ts"] == 0:
        _protection_cache["ts"] = time.monotonic()
        sem = asyncio.Semaphore(PROBE_CONCURRENCY)
        out: dict = {}

        async def _probe(name: str, f) -> None:
            inst = forge_runtime.instance(name)
            # reference-only repos: DevCake never pushes or merges there, so
            # the unprotected-default-branch advisory would be pure noise
            if inst is not None and inst.reference_only:
                return
            async with sem:
                try:
                    prot = await f.default_branch_protection(
                        inst.default_branch if inst else "main")
                    out[name] = prot.model_dump() if prot else None
                except Exception:  # noqa: BLE001 — probe contract: failure → None (advisory omitted); /health must never 500
                    out[name] = None

        await asyncio.gather(*(_probe(n, f)
                               for n, f in list(forge_runtime.forges.items())))
        _protection_cache["value"] = out
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
# without waiting for a restart (ISSUES #13).
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
                  f"OO_INGEST_* / OO_ROOT_* (ISSUES #13)"}
    except Exception as e:  # noqa: BLE001 — probe contract: any failure → ok:False + detail; /health must never 500
        result = {"ok": False, "detail": f"probe failed: {str(e)[:150]}"}
    _oo_ingest_cache.update(ts=now, result=result)
    return result


async def build_health_payload(*, config, dev_types, managers, mappers,
                               forge_runtime, shared_breakers, store,
                               internal_forge, poll_rt,
                               backend_degraded: dict | None = None) -> dict:
    """The /api/v1/health body (docs/11 §0). All deps explicit — unit-testable
    with fakes; the route in main.py is a one-line forward."""
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
            pmo_instances[inst.name] = {
                "ok": None, "configured": False, "team": "",
                "intake_paused": bool(inst.intake_paused),
            }
            continue
        mgr = managers.get(inst.name)
        try:
            ok = bool(mgr) and (await mgr.pmo.health_probe(inst.team_key)).ok
        except Exception:  # noqa: BLE001 — probe contract: any failure → ok:False for this instance; /health must never 500
            ok = False
        pmo_instances[inst.name] = {
            "ok": ok, "configured": True, "team": inst.team_key,
            "intake_paused": bool(inst.intake_paused),
        }
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
        # mapper_degraded (which is a plain string, not a map).
        "dev_backend_degraded": dict(backend_degraded or {}),
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
        "security_warnings": security.security_warnings(config),
    }
