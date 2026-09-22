"""Boot-time forge sweep ordering (incident 2026-08-01): the initial full
probe left FastAPI lifespan and now rides the poll task — but it must still
complete BEFORE the first cycle, because schedule() gates dispatch on latched
breakers and cycle 1 would otherwise burn an attempt on a definitively bad
credential. A sweep that misses its budget leaves last_full_probe_at unset,
and run_cycle retries the full sweep until one completes."""

import asyncio
import contextlib
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.adapters.files.owner_store import OwnerStore
from devcake.api.poll import PollRuntime
from devcake.config import AppConfig


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _rt(tmp_path, *, forge_runtime, refresh):
    return PollRuntime(
        config=AppConfig(), managers={}, stewards={},
        store=SimpleNamespace(active=lambda: [], all=lambda: []),
        forge_runtime=forge_runtime,
        refresh_forge_health=refresh,
        managers_in_config_order=lambda: [],
        owner_store=OwnerStore(tmp_path / "state" / "mission_owner.json"))


def test_initial_probe_precedes_first_cycle(tmp_path):
    events = []

    async def probe():
        events.append("probe")
        return {}

    rt = _rt(tmp_path,
             forge_runtime=SimpleNamespace(breakers={}, last_full_probe_at=None,
                                           health={}, forges={}),
             refresh=probe)

    async def fake_cycle(cycle_id):
        events.append("cycle")

    rt.run_cycle = fake_cycle

    async def drive():
        task = asyncio.create_task(rt.loop())
        for _ in range(500):
            if "cycle" in events:
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    run_coro(drive())
    assert events[0] == "probe"
    assert "cycle" in events


def test_cycle_retries_sweep_until_first_full_probe_completes(tmp_path):
    calls = []

    async def probe():
        calls.append("probe")
        return {}

    fr = SimpleNamespace(breakers={}, last_full_probe_at=None)
    rt = _rt(tmp_path, forge_runtime=fr, refresh=probe)

    run_coro(rt.run_cycle(1))
    assert calls == ["probe"]          # no breakers, but sweep never completed

    fr.last_full_probe_at = datetime.now(timezone.utc)
    run_coro(rt.run_cycle(2))
    assert calls == ["probe"]          # sweep done + no breakers → no re-probe

    fr.breakers["repo:x"] = "401"
    run_coro(rt.run_cycle(3))
    assert calls == ["probe", "probe"]  # latched breaker still re-probes


def test_sweep_past_its_budget_is_not_cancelled_and_is_reused_next_cycle(
        tmp_path, monkeypatch, caplog):
    """ADR-0044: the sweep budget bounds the CYCLE, never the sweep. A sweep
    that misses its budget keeps running (a cancel mid TLS handshake leaks
    the socket) and the next cycle waits on the SAME sweep with a fresh
    budget instead of starting a second one."""
    import logging
    from devcake import deadline
    from devcake.api import poll as poll_mod
    monkeypatch.setattr(poll_mod, "FORGE_SWEEP_BUDGET_S", 0.05)
    release = asyncio.Event()
    calls = []
    seen = {}

    async def probe():
        calls.append("probe")
        try:
            await release.wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        return {}

    fr = SimpleNamespace(breakers={}, last_full_probe_at=None, health={}, forges={})
    rt = _rt(tmp_path, forge_runtime=fr, refresh=probe)

    async def drive():
        await rt.run_cycle(1)
        assert calls == ["probe"]
        await rt.run_cycle(2)
        assert calls == ["probe"], "cycle 2 must wait on the running sweep"
        release.set()
        await deadline.drain()
        fr.last_full_probe_at = datetime.now(timezone.utc)
        await rt.run_cycle(3)
        assert calls == ["probe"]          # sweep done + no breakers → no re-probe
        await deadline.drain()

    with caplog.at_level(logging.WARNING, logger="devcake"):
        run_coro(drive())
    assert seen == {}
    assert any("exceeded its" in r.getMessage() and "budget" in r.getMessage()
               for r in caplog.records)


def test_initial_sweep_in_loop_is_reused_by_the_first_cycle(tmp_path, monkeypatch):
    """The boot sweep that misses its budget is the one cycle 1 waits on."""
    from devcake import deadline
    from devcake.api import poll as poll_mod
    monkeypatch.setattr(poll_mod, "FORGE_SWEEP_BUDGET_S", 0.05)
    release = asyncio.Event()
    calls = []

    async def probe():
        calls.append("probe")
        await release.wait()
        return {}

    fr = SimpleNamespace(breakers={}, last_full_probe_at=None, health={}, forges={})
    rt = _rt(tmp_path, forge_runtime=fr, refresh=probe)
    cycles = []
    real_cycle = rt.run_cycle

    async def counting_cycle(cycle_id):
        cycles.append(cycle_id)
        await real_cycle(cycle_id)

    rt.run_cycle = counting_cycle

    async def drive():
        task = asyncio.create_task(rt.loop())
        for _ in range(500):
            if cycles:
                break
            await asyncio.sleep(0.01)
        release.set()
        await deadline.drain()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await deadline.drain()

    run_coro(drive())
    assert cycles, "the loop must reach cycle 1 past a slow boot sweep"
    assert calls == ["probe"], "cycle 1 reused the boot sweep instead of starting another"
