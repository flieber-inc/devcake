"""L-1/L-2 (2026-08 evaluation): secret writes serialize against an
in-flight poll cycle, the config-PUT precedent (PR #104).

`reload_connections` swaps the adapter graph. The poll loop holds
`poll_rt.lock` for a whole cycle but suspends at awaits — a secret
PUT/DELETE/clear landing mid-cycle must not swap the graph (or delete the
credential) underneath the suspended cycle: it waits for the lock, so a
cycle observes either the pre-write world or the post-reload world, never
a half-swapped one. Same contract for the default board's opportunistic
repair, which reloads from a background task.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from devcake.api import connections_service
from devcake.config import AppConfig, MANAGED_BOARD_NAME


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _blocked_then_released(make_task, *, mutated, reloads):
    """Acquire the cycle lock, start the op, assert NOTHING happened while
    the cycle owns the lock, release, assert the op completed."""
    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        task = asyncio.create_task(make_task(lock))
        for _ in range(3):
            await asyncio.sleep(0)
        assert not task.done(), "op must wait for the in-flight poll cycle"
        assert mutated == [] and reloads == [], \
            "no store mutation and no reload while the cycle owns its lock"
        lock.release()
        return await task
    return run_coro(scenario())


def test_put_secret_waits_for_in_flight_poll_cycle(monkeypatch):
    mutated, reloads = [], []
    monkeypatch.setattr(connections_service.secrets_store,
                        "write_connection_secret",
                        lambda *a: mutated.append(a))
    monkeypatch.setattr(connections_service.secrets_store,
                        "connection_status",
                        lambda *a: {"present": True})
    out = _blocked_then_released(
        lambda lock: connections_service.put_secret(
            "pmo", "board", "api_key", {"value": "k"},
            forge_runtime=None, reload=lambda: reloads.append(1),
            cycle_lock=lock),
        mutated=mutated, reloads=reloads)
    assert out == {"present": True}
    assert len(mutated) == 1 and reloads == [1]


def test_delete_secret_waits_for_in_flight_poll_cycle(monkeypatch):
    mutated, reloads = [], []
    monkeypatch.setattr(connections_service.secrets_store,
                        "delete_connection_field",
                        lambda *a: mutated.append(a))
    out = _blocked_then_released(
        lambda lock: connections_service.delete_secret(
            "pmo", "board", "api_key",
            forge_runtime=None, reload=lambda: reloads.append(1),
            cycle_lock=lock),
        mutated=mutated, reloads=reloads)
    assert out == {"present": False}
    assert len(mutated) == 1 and reloads == [1]


def test_clear_secrets_waits_for_in_flight_poll_cycle(monkeypatch):
    mutated, reloads = [], []
    monkeypatch.setattr(connections_service.secrets_store,
                        "delete_connection_field",
                        lambda *a: mutated.append(a))
    monkeypatch.setattr("devcake.settings_bundle.audit_event",
                        lambda *a, **k: None)
    cfg = SimpleNamespace(intake_paused=False)
    out = _blocked_then_released(
        lambda lock: connections_service.clear_secrets(
            {"connections": [{"scope": "pmo", "instance": "board",
                              "field": "api_key"}]},
            forge_runtime=None, reload=lambda: reloads.append(1),
            config=cfg, shared_breakers={}, dev_types={}, cycle_lock=lock),
        mutated=mutated, reloads=reloads)
    assert out["ok"] is True
    assert len(mutated) == 1 and reloads == [1]


def test_default_board_repair_waits_for_in_flight_poll_cycle(tmp_path,
                                                             monkeypatch):
    """L-2: the opportunistic board repair mutates config + reloads from a
    background task — same serialization contract when `s.poll_rt` exists
    (test fakes without one keep running lock-free)."""
    from devcake.api.default_board import ensure_default_board
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    reloads = []
    forge = SimpleNamespace(ensure_pmo_board=None)

    async def ensure_pmo_board():
        return {"team_key": "devcake-pmo/missions",
                "api_base": "http://gitea:3000",
                "minted": False, "adopted": True}
    forge.ensure_pmo_board = ensure_pmo_board

    async def scenario():
        lock = asyncio.Lock()
        await lock.acquire()
        s = SimpleNamespace(
            config=AppConfig(pmos=[]), internal_forge=forge,
            reload_connections=lambda: reloads.append(1),
            poll_rt=SimpleNamespace(lock=lock))
        task = asyncio.create_task(ensure_default_board(s))
        for _ in range(5):
            await asyncio.sleep(0)
        assert not task.done(), "board repair must wait for the poll cycle"
        assert s.config.pmos == [] and reloads == []
        lock.release()
        await task
        assert [p.name for p in s.config.pmos] == [MANAGED_BOARD_NAME]
        assert reloads == [1]
    run_coro(scenario())
