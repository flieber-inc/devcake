"""CAKE-164: empty first-boot roster + first-setup wizard.

Public seams: load_dev_types (no seed top-up), AppConfig.assignments default
(unstaffed), validate_config_semantics (empty map OK), assignment_for
(clear miss), POST /dev-types/first-setup (create + wire + pin).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# ── Slice 1: empty seed + unstaffed boot ─────────────────────────────────────


def test_load_dev_types_writes_nothing_on_empty_dir(tmp_path, monkeypatch):
    """Fresh volume: no judgment.yaml / implementer.yaml / steward.yaml top-up."""
    from devcake import config as config_mod

    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    out = config_mod.load_dev_types()
    assert out == {}
    dt_dir = tmp_path / "dev_types"
    assert dt_dir.is_dir()
    assert list(dt_dir.glob("*.yaml")) == []


def test_appconfig_default_assignments_are_unstaffed():
    from devcake.config import AppConfig, DEFAULT_ASSIGNMENTS

    assert DEFAULT_ASSIGNMENTS == {}
    assert AppConfig().assignments == {}


def test_validate_config_semantics_allows_empty_roster_and_assignments(
        monkeypatch, tmp_path):
    from devcake import config as config_mod
    from devcake import settings_bundle as sb

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    cfg = config_mod.AppConfig()
    assert cfg.assignments == {}
    sb.validate_config_semantics(cfg, set(), lambda mt, name: True)


def test_validate_config_semantics_still_refuses_unknown_dev_type():
    from devcake.config import AppConfig, Assignment
    from devcake import settings_bundle as sb

    cfg = AppConfig(assignments={
        mt: Assignment(dev_type="ghost")
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")
    })
    with pytest.raises(sb.BundleError, match="ghost"):
        sb.validate_config_semantics(cfg, set(), lambda mt, name: True)


def test_assignment_for_raises_clear_error_when_unstaffed():
    from devcake.config import (AppConfig, AssignmentUnstaffed, PMOInstance,
                                assignment_for)

    cfg = AppConfig()
    inst = PMOInstance(name="linear", team_key="DEV")
    with pytest.raises(AssignmentUnstaffed, match="no assignment|unstaffed|first setup"):
        assignment_for(cfg, inst, "EXECUTE")


def test_validate_assignment_map_empty_ok_partial_still_refused():
    from devcake.config import Assignment, validate_assignment_map

    validate_assignment_map({}, require_complete=True, context="assignments")
    with pytest.raises(ValueError, match="unassigned mission types"):
        validate_assignment_map(
            {"ONBOARD": Assignment(dev_type="judge")},
            require_complete=True, context="assignments")


# ── Slice 2: first-setup API ─────────────────────────────────────────────────


def test_first_setup_creates_three_types_pins_and_wires(
        tmp_path, monkeypatch):
    """Empty roster → three Dev Types, concrete CLI pins, assignments + steward."""
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig
    from devcake.house_pins import HOUSE_PINS

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    (tmp_path / "config").mkdir(parents=True)

    class Planted:
        def latest(self, template: str) -> str:
            return {"claude-code": "2.1.250", "grok-build": "0.2.200"
                    }.get(template, "1.2.3")

    cfg = AppConfig()
    dts: dict = {}
    published: list = []
    monkeypatch.setattr(devtypes_service, "publish_keep_set",
                        lambda d: published.append(sorted(d)))

    body = {
        "roles": {
            "judge": {"harness_template": "claude-code", "model": ""},
            "executor": {"harness_template": "grok-build", "model": ""},
            "steward": {"harness_template": "claude-code", "model": ""},
        }
    }
    out = _run(devtypes_service.first_setup(
        body, config=cfg, dev_types=dts, version_source=Planted()))

    assert set(dts) == {"executor", "judge", "steward"}
    assert dts["judge"].cli_version == "2.1.250"
    assert dts["executor"].cli_version == "0.2.200"
    assert dts["steward"].cli_version == "2.1.250"
    assert all(dt.cli_version != "latest" for dt in dts.values())
    assert all(not (dt.model or "").strip() for dt in dts.values())
    assert "claude-fable" not in str(dts["judge"].model)
    assert "claude-opus" not in str(dts["steward"].model)

    assert cfg.assignments["EXECUTE"].dev_type == "executor"
    assert cfg.assignments["ONBOARD"].dev_type == "judge"
    assert cfg.assignments["PLAN"].dev_type == "judge"
    assert cfg.assignments["REVIEW"].dev_type == "judge"
    assert cfg.assignments["ONBOARD"].extra_cli_args == "--max-turns 15"
    assert cfg.steward.dev_type == "steward"

    assert "Judge" in dts["judge"].identifying_prompt
    assert "Judgment" not in dts["judge"].identifying_prompt
    assert "Executor" in dts["executor"].identifying_prompt
    assert "Implementer" not in dts["executor"].identifying_prompt
    assert "Steward" in dts["steward"].identifying_prompt

    # YAML + prompt templates on disk
    for name in ("executor", "judge", "steward"):
        assert (tmp_path / "config" / "dev_types" / f"{name}.yaml").exists()
        assert (tmp_path / "config" / "devtype_prompt_templates" / name
                / "Development.yaml").exists()
        assert (tmp_path / "config" / "devtype_prompt_templates" / name
                / "Customer Success.yaml").exists()

    assert published and published[-1] == ["executor", "judge", "steward"]
    assert out["created"] == ["executor", "judge", "steward"]
    # house pins remain available as offline fallback reference
    assert "claude-code" in HOUSE_PINS


def test_first_setup_offline_falls_back_to_house_pin(tmp_path, monkeypatch):
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig
    from devcake.house_pins import HOUSE_PINS

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    (tmp_path / "config").mkdir(parents=True)

    class Dead:
        def latest(self, template: str) -> str:
            raise ConnectionError("registry unreachable")

    cfg = AppConfig()
    dts: dict = {}
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda d: None)

    _run(devtypes_service.first_setup(
        {"roles": {
            "judge": {"harness_template": "claude-code"},
            "executor": {"harness_template": "grok-build"},
            "steward": {"harness_template": "claude-code"},
        }},
        config=cfg, dev_types=dts, version_source=Dead()))

    assert dts["judge"].cli_version == HOUSE_PINS["claude-code"]
    assert dts["executor"].cli_version == HOUSE_PINS["grok-build"]
    assert "latest" not in {dts[n].cli_version for n in dts}


def test_first_setup_refuses_nonempty_roster(tmp_path, monkeypatch):
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")

    cfg = AppConfig()
    dts = {"judgment": DevType(name="judgment", harness_template="claude-code")}
    with pytest.raises(HTTPException) as exc:
        _run(devtypes_service.first_setup(
            {"roles": {
                "judge": {"harness_template": "claude-code"},
                "executor": {"harness_template": "grok-build"},
                "steward": {"harness_template": "claude-code"},
            }},
            config=cfg, dev_types=dts, version_source=None))
    assert exc.value.status_code == 409
    assert set(dts) == {"judgment"}  # untouched


def test_first_setup_refuses_name_collision_without_partial_write(
        tmp_path, monkeypatch):
    """If any of the three target names already exist on disk/memory, refuse
    before creating the others."""
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    (tmp_path / "config" / "dev_types").mkdir(parents=True)
    # Simulate a mid-state: only steward exists (operator-created name)
    # but we treat any of the three as collision when roster is otherwise
    # being set up — actually plan says: precondition is roster empty.
    # Separate case: roster empty in memory but one name already on disk?
    # first_setup checks in-memory roster empty AND none of the three names.
    cfg = AppConfig()
    dts = {"steward": DevType(name="steward", harness_template="claude-code")}
    with pytest.raises(HTTPException) as exc:
        _run(devtypes_service.first_setup(
            {"roles": {
                "judge": {"harness_template": "claude-code"},
                "executor": {"harness_template": "grok-build"},
                "steward": {"harness_template": "claude-code"},
            }},
            config=cfg, dev_types=dts, version_source=None))
    assert exc.value.status_code == 409
    assert "executor" not in dts
    assert "judge" not in dts
