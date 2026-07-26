"""Per-PMO intake pause (under the global master switch).

Global `AppConfig.intake_paused` still freezes every instance. Each
`PMOInstance.intake_paused` freezes only that instance's NEW dispatches —
sweeps / finalization continue either way.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from devcake.api import config_service
from devcake.api.poll import intake_blocks_dispatch
from devcake.config import AppConfig, PMOInstance


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_pmo_instance_defaults_intake_open():
    assert PMOInstance(name="linear", team_key="T").intake_paused is False


def test_global_pause_blocks_every_instance():
    cfg = AppConfig(intake_paused=True)
    inst = PMOInstance(name="a", team_key="A", intake_paused=False)
    assert intake_blocks_dispatch(cfg, inst) is True


def test_instance_pause_blocks_only_when_global_is_open():
    cfg = AppConfig(intake_paused=False)
    paused = PMOInstance(name="a", team_key="A", intake_paused=True)
    open_ = PMOInstance(name="b", team_key="B", intake_paused=False)
    assert intake_blocks_dispatch(cfg, paused) is True
    assert intake_blocks_dispatch(cfg, open_) is False


def test_global_and_instance_both_paused_still_blocked():
    cfg = AppConfig(intake_paused=True)
    inst = PMOInstance(name="a", team_key="A", intake_paused=True)
    assert intake_blocks_dispatch(cfg, inst) is True


def test_config_roundtrip_keeps_per_pmo_intake():
    cfg = AppConfig(pmos=[
        PMOInstance(name="alpha", team_key="A", intake_paused=True),
        PMOInstance(name="beta", team_key="B"),
    ])
    dumped = cfg.model_dump()
    restored = AppConfig.model_validate(dumped)
    assert restored.pmos[0].intake_paused is True
    assert restored.pmos[1].intake_paused is False


def test_set_pmo_intake_mutates_in_place_without_list_replace(tmp_path, monkeypatch):
    """Narrow path: flip one bool on the live object; never rewrite pmos."""
    cfg = AppConfig(pmos=[
        PMOInstance(name="alpha", team_key="A"),
        PMOInstance(name="beta", team_key="B", intake_paused=True),
    ])
    alpha_id = id(cfg.pmos[0])
    beta_id = id(cfg.pmos[1])
    saved = []

    def fake_save(c):
        saved.append(c.model_dump())

    monkeypatch.setattr(config_service, "save_config", fake_save)
    out = config_service.set_pmo_intake(name="alpha", paused=True, config=cfg)
    assert out == {"name": "alpha", "intake_paused": True}
    assert cfg.pmos[0].intake_paused is True
    assert cfg.pmos[1].intake_paused is True          # untouched
    assert id(cfg.pmos[0]) == alpha_id and id(cfg.pmos[1]) == beta_id
    assert len(saved) == 1


def test_set_pmo_intake_unknown_name_404(monkeypatch):
    cfg = AppConfig(pmos=[PMOInstance(name="alpha", team_key="A")])
    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    with pytest.raises(HTTPException) as ei:
        config_service.set_pmo_intake(name="nope", paused=True, config=cfg)
    assert ei.value.status_code == 404


def test_set_pmo_intake_rejects_non_bool(monkeypatch):
    cfg = AppConfig(pmos=[PMOInstance(name="alpha", team_key="A")])
    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    with pytest.raises(HTTPException) as ei:
        config_service.set_pmo_intake(name="alpha", paused="yes", config=cfg)
    assert ei.value.status_code == 422


def test_poll_instance_skips_schedule_when_instance_paused(tmp_path):
    """A paused instance must not schedule or mapper-dispatch; sweeps still run.
    A sibling open instance is unaffected (tested at the predicate layer above).
    """
    from fakes import make_mission_manager
    from devcake.api.poll import PollRuntime
    from devcake.adapters.files.owner_store import OwnerStore

    paused_inst = PMOInstance(name="alpha", team_key="A", intake_paused=True)
    mgr = make_mission_manager(instance=paused_inst, noop_audit=True)
    sweeps = AsyncMock()
    schedule = AsyncMock(return_value=3)
    gate_map = AsyncMock(return_value={})
    mgr.sweeps = sweeps
    mgr.schedule = schedule
    mgr.gate_map = gate_map
    mgr.resolve_repo_live = AsyncMock(return_value=(None, ""))
    mgr.pmo = SimpleNamespace(list_all=AsyncMock(return_value=[]))
    mgr.runs = SimpleNamespace(store=SimpleNamespace(all=lambda: []))
    mgr.instance_name = "alpha"
    mgr.anomalies = {}

    mapper = SimpleNamespace(maybe_dispatch=AsyncMock())

    async def _noop():
        return {}

    rt = PollRuntime(
        config=AppConfig(intake_paused=False),
        managers={"alpha": mgr},
        mappers={"alpha": mapper},
        store=SimpleNamespace(active=lambda: [], all=lambda: []),
        forge_runtime=SimpleNamespace(breakers={}),
        refresh_forge_health=_noop,
        managers_in_config_order=lambda: [mgr],
        owner_store=OwnerStore(tmp_path / "state" / "mission_owner.json"),
    )
    seen, cand, disp, _ids = run_coro(rt.poll_instance(mgr, []))
    assert disp == 0
    sweeps.assert_awaited_once()
    schedule.assert_not_awaited()
    mapper.maybe_dispatch.assert_not_awaited()
    gate_map.assert_awaited_once()


def test_poll_instance_schedules_when_open(tmp_path):
    from fakes import make_mission_manager
    from devcake.api.poll import PollRuntime
    from devcake.adapters.files.owner_store import OwnerStore

    inst = PMOInstance(name="alpha", team_key="A", intake_paused=False)
    mgr = make_mission_manager(instance=inst, noop_audit=True)
    mgr.sweeps = AsyncMock()
    mgr.schedule = AsyncMock(return_value=2)
    mgr.gate_map = AsyncMock(return_value={})
    mgr.resolve_repo_live = AsyncMock(return_value=(None, ""))
    mgr.pmo = SimpleNamespace(list_all=AsyncMock(return_value=[]))
    mgr.runs = SimpleNamespace(store=SimpleNamespace(all=lambda: []))
    mgr.instance_name = "alpha"
    mgr.anomalies = {}

    async def _noop():
        return {}

    rt = PollRuntime(
        config=AppConfig(intake_paused=False),
        managers={"alpha": mgr},
        mappers={},
        store=SimpleNamespace(active=lambda: [], all=lambda: []),
        forge_runtime=SimpleNamespace(breakers={}),
        refresh_forge_health=_noop,
        managers_in_config_order=lambda: [mgr],
        owner_store=OwnerStore(tmp_path / "state" / "mission_owner.json"),
    )
    _s, _c, disp, _i = run_coro(rt.poll_instance(mgr, []))
    assert disp == 2
    mgr.schedule.assert_awaited_once()


def test_global_pause_skips_schedule_even_if_instance_open(tmp_path):
    from fakes import make_mission_manager
    from devcake.api.poll import PollRuntime
    from devcake.adapters.files.owner_store import OwnerStore

    inst = PMOInstance(name="alpha", team_key="A", intake_paused=False)
    mgr = make_mission_manager(instance=inst, noop_audit=True)
    mgr.sweeps = AsyncMock()
    mgr.schedule = AsyncMock(return_value=9)
    mgr.gate_map = AsyncMock(return_value={})
    mgr.resolve_repo_live = AsyncMock(return_value=(None, ""))
    mgr.pmo = SimpleNamespace(list_all=AsyncMock(return_value=[]))
    mgr.runs = SimpleNamespace(store=SimpleNamespace(all=lambda: []))
    mgr.instance_name = "alpha"
    mgr.anomalies = {}

    async def _noop():
        return {}

    rt = PollRuntime(
        config=AppConfig(intake_paused=True),
        managers={"alpha": mgr},
        mappers={},
        store=SimpleNamespace(active=lambda: [], all=lambda: []),
        forge_runtime=SimpleNamespace(breakers={}),
        refresh_forge_health=_noop,
        managers_in_config_order=lambda: [mgr],
        owner_store=OwnerStore(tmp_path / "state" / "mission_owner.json"),
    )
    _s, _c, disp, _i = run_coro(rt.poll_instance(mgr, []))
    assert disp == 0
    mgr.schedule.assert_not_awaited()
    mgr.sweeps.assert_awaited_once()
