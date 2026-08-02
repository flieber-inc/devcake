"""/api/v1/health payload additions for the boot-sweep rework (incident
2026-08-01): `forge_probe` distinguishes "initial sweep pending" from "done"
so the SPA/operator can tell an empty forge map from an unprobed one, and
_branch_protection must probe bounded-parallel — it walks every work repo
and, sequential, stalled the first rich /health call the same way the boot
sweep stalled lifespan."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.api import health as health_mod
from devcake.config import AppConfig


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _forge_runtime(*, last_full_probe_at=None, health=None, forges=None):
    return SimpleNamespace(
        health=health if health is not None else {},
        breakers={},
        forges=forges if forges is not None else {},
        last_full_probe_at=last_full_probe_at,
        instance=lambda name: None,
    )


def _payload(fr, monkeypatch):
    async def _true(*a, **k):
        return True

    async def _ingest():
        return {"ok": True, "detail": ""}

    monkeypatch.setattr(health_mod, "_check_redis", _true)
    monkeypatch.setattr(health_mod, "_check_http", _true)
    monkeypatch.setattr(health_mod, "_oo_ingest_check", _ingest)
    health_mod.reset_protection_cache()
    return run_coro(health_mod.build_health_payload(
        config=AppConfig(), dev_types={}, managers={}, mappers={},
        forge_runtime=fr, shared_breakers={},
        store=SimpleNamespace(active=lambda: []),
        internal_forge=None,
        poll_rt=SimpleNamespace(last_poll_at=None, poll_degraded={})))


def test_forge_probe_pending_then_complete(monkeypatch):
    fr = _forge_runtime(forges={"a": object(), "b": object()},
                        health={"a": {"ok": True}})
    got = _payload(fr, monkeypatch)["forge_probe"]
    assert got == {"complete": False, "completed_at": None,
                   "probed": 1, "configured": 2}

    done = datetime.now(timezone.utc)
    fr.last_full_probe_at = done
    fr.health["b"] = {"ok": True}
    got = _payload(fr, monkeypatch)["forge_probe"]
    assert got == {"complete": True, "completed_at": done.isoformat(),
                   "probed": 2, "configured": 2}


def test_branch_protection_probes_concurrently(monkeypatch):
    started = {"n": 0}
    all_started = asyncio.Event()

    class _Prot:
        def model_dump(self):
            return {"rendezvous": True}

    class _Forge:
        async def default_branch_protection(self, branch):
            started["n"] += 1
            if started["n"] == 3:
                all_started.set()
            # sequential execution times out here → the except path stores
            # None; only genuine parallelism lets all three return _Prot
            await asyncio.wait_for(all_started.wait(), timeout=2)
            return _Prot()

    inst = SimpleNamespace(reference_only=False, default_branch="main")
    fr = SimpleNamespace(forges={f"r{i}": _Forge() for i in range(3)},
                         instance=lambda name: inst)
    health_mod.reset_protection_cache()
    out = run_coro(asyncio.wait_for(health_mod._branch_protection(fr), timeout=5))
    assert out == {f"r{i}": {"rendezvous": True} for i in range(3)}
    health_mod.reset_protection_cache()   # don't leak the fake into others
