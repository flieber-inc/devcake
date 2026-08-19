"""ForgeRuntime — the per-repo forge adapters, health, and breakers (M10).

Repos belong to the DevCake deployment, not to a PMO instance: missions from
any PMO instance can route to any configured repo, so ONE runtime is built in
api/main and injected into every MissionManager.

The resolution-failure contract (v0.1 plan finding H2) lives here: `get()`
returns None for a vanished repo and every call-site CLASS handles it
explicitly — sweeps skip with a visible reason, finalize paths fail the run
cleanly, dispatch refuses — a mission parked on DEVCAKE-MERGE whose repo the
operator removed must neither crash nor silently wedge.

Breakers latch per repo name ("repo:<name>"): a definitive credential
failure on repo A never stops repo B's missions (docs/15 §4).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

if TYPE_CHECKING:
    from ..config import RepoInstance
    from ..ports.forge import ForgePort

log = logging.getLogger("devcake.forge_runtime")
tracer = trace.get_tracer("devcake")

# Cap on in-flight health probes. Single GETs against (usually) one forge
# host sharing a token: high enough that a large catalog sweeps in seconds
# (incident 2026-08-01: 319 repos × ~300ms sequential ≈ 95s+), low enough
# to stay clear of secondary rate limits.
PROBE_CONCURRENCY = 8


class ForgeRuntime:
    def __init__(self):
        self.forges: dict[str, "ForgePort"] = {}
        self.instances: dict[str, "RepoInstance"] = {}
        self.health: dict[str, dict] = {}       # advisory; /health
        self.breakers: dict[str, str] = {}      # repo name → reason (latched)
        # repo names created on the internal fallback forge (M11): these are
        # NOT config repos — registered dynamically per mission, and flagged
        # so the merge → zip-to-PMO delivery hook knows to fire
        self.internal: set[str] = set()
        # when the last FULL refresh_all completed; None = no full sweep yet
        # (boot probe pending, or its 60s budget expired mid-sweep) — /health
        # surfaces this and the poll loop retries the sweep while unset
        self.last_full_probe_at: datetime | None = None

    def rebuild(self, repos: list["RepoInstance"], make_forge) -> None:
        """Reconcile with config (boot + hot reload). Unconfigured entries
        (empty url) build no adapter; removed CONFIG repos drop their
        health/breakers. Dynamically registered internal-forge entries are
        carried over untouched (audit A3): every config PUT and secret
        PUT/DELETE lands here, and wiping them failed in-flight zero-repo
        runspecs (Dev exit 20, burned attempt) and REVIEW finalizes for up
        to a poll cycle. Internal entries leave ONLY via the admin delete
        endpoint (`unregister`), which pops forges/instances/internal plus
        health and breakers so Clear does not leave a latched ghost."""
        live: dict[str, "ForgePort"] = {}
        insts: dict[str, "RepoInstance"] = {}
        for inst in repos:
            if not inst.configured:
                continue
            live[inst.name] = make_forge(inst)
            insts[inst.name] = inst
        for name in self.internal:
            if name in self.forges and name not in live:
                live[name] = self.forges[name]
                insts[name] = self.instances[name]
        # F16: the outgoing adapters hold pooled keep-alive clients — close
        # them (fire-and-forget) instead of leaking sockets on every config
        # PUT / secret write. Carried-over internal entries are the SAME
        # objects in `live`, so only truly replaced adapters close.
        outgoing = [f for n, f in self.forges.items()
                    if live.get(n) is not f]
        if outgoing:
            from ..adapters.http import aclose_adapters
            aclose_adapters(outgoing)
        self.forges, self.instances = live, insts
        for name in list(self.health):
            if name not in live:
                del self.health[name]
        for name in list(self.breakers):
            if name not in live:
                del self.breakers[name]

    def register_internal(self, name: str, inst, forge) -> None:
        """Register (idempotently) an auto-created internal-forge repo under
        its own name. Called when a mission routes internal — the entry then
        survives config/secret rebuilds (see rebuild); only a process restart
        drops it, and resolve_repo_live re-registers from stored creds on the
        first cycle. The app-side adapter uses the devcake-app SERVICE token
        (write:issue for PR comments + merge); the mission's write/read Dev
        token pair flows through the runspec by repo_ref."""
        self.instances[name] = inst
        self.forges[name] = forge
        self.internal.add(name)

    def unregister(self, name: str) -> None:
        """Drop a repo from every runtime map (admin Clear of an internal
        forge repo). Idempotent — missing names are ignored. Must pop
        health and breakers too: rebuild only drops those for names not in
        `live`, and internals are carried across rebuilds, so a Clear that
        only popped forges/instances/internal left latched breakers and
        stale health until process restart."""
        self.forges.pop(name, None)
        self.instances.pop(name, None)
        self.internal.discard(name)
        self.health.pop(name, None)
        self.breakers.pop(name, None)

    def get(self, name: str) -> "ForgePort | None":
        """None = the repo is not (or no longer) configured — the caller's
        class-specific failure contract applies (module docstring)."""
        return self.forges.get(name)

    def instance(self, name: str) -> "RepoInstance | None":
        return self.instances.get(name)

    def apply_health(self, name: str, data: dict) -> None:
        """Single writer for per-repo health + breaker latching (moved from
        finalize.apply_forge_health): latch only on definitive (non-transient)
        failures; a transient probe failure neither latches nor clears; any
        OK probe clears the latch (docs/15 §4). No-op for unregistered names
        (audit A29): a probe completing after a rebuild/delete removed the
        repo must not resurrect its health entry — refresh_all walks only
        registered repos, so it would sit stale until the next config PUT."""
        if name not in self.forges:
            log.info("health probe for unregistered repo %s dropped", name)
            return
        self.health[name] = data
        if data.get("ok"):
            if self.breakers.pop(name, None) is not None:
                log.info("forge breaker CLEARED for repo %s", name)
        elif data.get("transient"):
            log.warning("forge probe transient failure on repo %s (breaker "
                        "untouched): %s", name, data.get("detail"))
            # span-mirrored so the FORGE_TRANSIENT >15m alert has a signal —
            # the log line never reaches the traces stream
            from ..security import redact
            with tracer.start_as_current_span("forge.probe_transient") as span:
                span.set_attribute("devcake.repo", name)
                span.set_attribute("devcake.reason",
                                   redact(str(data.get("detail") or ""))[:500])
        else:
            self.latch(name, data.get("detail") or "repository is not writable")

    def latch(self, name: str, reason: str) -> None:
        """Manually latch a repo breaker (Dev-side DEV_FORGE_AUTH — the
        structured clone-credential classification, docs/15 §4). No-op for
        unregistered names (audit A29 — see apply_health)."""
        if name not in self.forges:
            log.warning("breaker latch for unregistered repo %s dropped", name)
            return
        if name not in self.breakers:
            from ..security import redact
            log.error("forge breaker LATCHED for repo %s: %s", name, reason)
            # same span (and ERROR status — docs/12 §2) the OO alert battery
            # queries for dev-type breakers; reason redacted like every
            # exported string (adapter errors can embed credentials)
            with tracer.start_as_current_span("breaker.trip") as span:
                span.set_attribute("devcake.breaker", f"repo:{name}")
                span.set_attribute("devcake.reason", redact(reason)[:500])
                span.set_status(Status(StatusCode.ERROR, redact(reason)[:200]))
        self.breakers[name] = reason

    async def refresh_health(self, name: str) -> dict:
        forge = self.forges.get(name)
        inst = self.instances.get(name)
        if forge is None:
            data = {"ok": False, "repository": "", "can_push": False,
                    "transient": True, "detail": "repository not configured"}
        else:
            try:
                data = (await forge.health_probe()).model_dump()
            except Exception as e:  # noqa: BLE001 — probe contract: any failure → ok:False + transient detail, never raises into the poll loop
                # a probe that could not run says nothing about the credential
                data = {"ok": False, "repository": inst.url if inst else "",
                        "can_push": False, "transient": True,
                        "detail": f"forge probe failed: {str(e)[:200]}"}
        # reference-only repos (read token, no write token — founder decision
        # 2026-07-15): readable-but-not-writable is their EXPECTED healthy
        # state, not a latchable failure. A read token that cannot even READ
        # (can_read False) still latches normally.
        if (inst is not None and not data["ok"] and not data.get("transient")
                and data.get("can_read") and inst.reference_only):
            data = {**data, "ok": True,
                    "detail": "reference-only: read access verified (no write "
                              "token stored — never a work target)"}
        self.apply_health(name, data)
        return data

    async def refresh_all(self, *, limit: int = PROBE_CONCURRENCY) -> dict[str, dict]:
        """Probe every registered repo, at most `limit` in flight. The bound
        lives here so every caller gets it: the poll task's initial sweep,
        the per-cycle breaker re-probe, and the config-reload probe all walk
        the full catalog. Names are snapshotted at entry; refresh_health
        never raises (probe contract) and apply_health drops names that a
        concurrent rebuild unregistered, so overlapping sweeps are benign
        last-writer-wins per repo."""
        sem = asyncio.Semaphore(limit)

        async def _one(name: str) -> None:
            async with sem:
                await self.refresh_health(name)

        await asyncio.gather(*(_one(n) for n in list(self.forges)))
        self.last_full_probe_at = datetime.now(timezone.utc)
        return dict(self.health)
