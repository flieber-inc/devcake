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
        config=AppConfig(), managers={}, mappers={},
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
