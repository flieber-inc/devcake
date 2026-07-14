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

import logging
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    from ..config import RepoInstance
    from ..ports.forge import ForgePort

log = logging.getLogger("devcake.forge_runtime")
tracer = trace.get_tracer("devcake")


class ForgeRuntime:
    def __init__(self):
        self.forges: dict[str, "ForgePort"] = {}
        self.instances: dict[str, "RepoInstance"] = {}
        self.health: dict[str, dict] = {}       # advisory; /health
        self.breakers: dict[str, str] = {}      # repo name → reason (latched)

    def rebuild(self, repos: list["RepoInstance"], make_forge) -> None:
        """Reconcile with config (boot + hot reload). Unconfigured entries
        (empty url) build no adapter; removed repos drop health/breakers."""
        live: dict[str, "ForgePort"] = {}
        insts: dict[str, "RepoInstance"] = {}
        for inst in repos:
            if not inst.configured:
                continue
            live[inst.name] = make_forge(inst)
            insts[inst.name] = inst
        self.forges, self.instances = live, insts
        for name in list(self.health):
            if name not in live:
                del self.health[name]
        for name in list(self.breakers):
            if name not in live:
                del self.breakers[name]

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
        OK probe clears the latch (docs/15 §4)."""
        self.health[name] = data
        if data.get("ok"):
            if self.breakers.pop(name, None) is not None:
                log.info("forge breaker CLEARED for repo %s", name)
        elif data.get("transient"):
            log.warning("forge probe transient failure on repo %s (breaker "
                        "untouched): %s", name, data.get("detail"))
            # span-mirrored so the FORGE_TRANSIENT >15m alert has a signal
            # (ISSUES #23) — the log line never reaches the traces stream
            with tracer.start_as_current_span("forge.probe_transient") as span:
                span.set_attribute("devcake.repo", name)
                span.set_attribute("devcake.reason",
                                   str(data.get("detail") or "")[:500])
        else:
            self.latch(name, data.get("detail") or "repository is not writable")

    def latch(self, name: str, reason: str) -> None:
        """Manually latch a repo breaker (Dev-side DEV_FORGE_AUTH — the
        structured clone-credential classification, docs/15 §4)."""
        if name not in self.breakers:
            log.error("forge breaker LATCHED for repo %s: %s", name, reason)
            # same span the OO alert battery queries for dev-type breakers
            with tracer.start_as_current_span("breaker.trip") as span:
                span.set_attribute("devcake.breaker", f"repo:{name}")
                span.set_attribute("devcake.reason", reason[:500])
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
            except Exception as e:
                # a probe that could not run says nothing about the credential
                data = {"ok": False, "repository": inst.url if inst else "",
                        "can_push": False, "transient": True,
                        "detail": f"forge probe failed: {str(e)[:200]}"}
        self.apply_health(name, data)
        return data

    async def refresh_all(self) -> dict[str, dict]:
        for name in list(self.forges):
            await self.refresh_health(name)
        return dict(self.health)
