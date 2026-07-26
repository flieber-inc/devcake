"""Per-PMO intake pause (under the global master switch).

Global `AppConfig.intake_paused` still freezes every instance. Each
`PMOInstance.intake_paused` freezes only that instance's NEW dispatches —
sweeps / finalization continue either way.
"""

from __future__ import annotations

from devcake.api.poll import intake_blocks_dispatch
from devcake.config import AppConfig, PMOInstance


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
