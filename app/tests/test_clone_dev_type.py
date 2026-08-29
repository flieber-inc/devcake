"""Clone Dev Type — public seam clone_dev_type (CAKE-149).

Copies YAML fields + prompt-template dir (+ active selection); never copies
secrets; never remaps assignments / steward / breakers.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _cfg(dev_type: str = "judgment"):
    from devcake.config import AppConfig, Assignment
    return AppConfig(assignments={
        mt: Assignment(dev_type=dev_type)
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})


def _patch_persist(monkeypatch, service):
    monkeypatch.setattr(service, "save_config", lambda c: None)
    monkeypatch.setattr(service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(service, "publish_keep_set", lambda dts: None)


def test_clone_dev_type_copies_yaml_fields_leaves_source(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    saved: list = []
    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "save_dev_type",
                        lambda d: saved.append(d.name))
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)
    monkeypatch.setattr(devtypes_service.prompt_templates, "seed_devtype_prompts",
                        lambda dts: None)

    src = DevType(
        name="senior",
        harness_template="codex",
        identifying_prompt="I am senior.",
        model="gpt-5",
        skills=["pr-hygiene"],
        skills_required=["pr-hygiene"],
        secret_env=["DD_API_KEY"],
        memory_repos=["notebook"],
        max_concurrency=3,
        dev_entrypoint="echo hi",
        override_harness_adapter=True,
        backend_base_url="https://example.invalid/v1",
        cli_version="1.2.3",
    )
    dts = {"senior": src}
    cfg = _cfg("judgment")

    out = run_coro(devtypes_service.clone_dev_type(
        "senior", {"new_name": "senior-clone"}, config=cfg, dev_types=dts))
    assert out == {"cloned": True, "name": "senior-clone"}
    assert "senior" in dts and "senior-clone" in dts
    clone = dts["senior-clone"]
    assert clone.name == "senior-clone"
    assert clone.harness_template == "codex"
    assert clone.identifying_prompt == "I am senior."
    assert clone.model == "gpt-5"
    assert clone.skills == ["pr-hygiene"]
    assert clone.skills_required == ["pr-hygiene"]
    assert clone.secret_env == ["DD_API_KEY"]
    assert clone.memory_repos == ["notebook"]
    assert clone.max_concurrency == 3
    assert clone.dev_entrypoint == "echo hi"
    assert clone.override_harness_adapter is True
    assert clone.backend_base_url == "https://example.invalid/v1"
    assert clone.cli_version == "1.2.3"
    assert dts["senior"].name == "senior"
    assert dts["senior"].identifying_prompt == "I am senior."
    assert "senior-clone" in saved


def test_clone_dev_type_copies_prompt_dir_not_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    _patch_persist(monkeypatch, devtypes_service)

    dts = {"senior": DevType(name="senior", harness_template="codex",
                             identifying_prompt="I am senior.")}
    cfg = _cfg("judgment")

    templates = tmp_path / "config" / "devtype_prompt_templates" / "senior"
    templates.mkdir(parents=True)
    (templates / "Development.yaml").write_text(
        "schema_version: 1\ndev_type: senior\nname: Development\n"
        "template: I am senior.\n")
    secrets = tmp_path / "secrets" / "senior"
    secrets.mkdir(parents=True)
    (secrets / "creds.json").write_text('{"token":"secret"}\n')

    out = run_coro(devtypes_service.clone_dev_type(
        "senior", {"new_name": "junior"}, config=cfg, dev_types=dts))
    assert out["cloned"] and out["name"] == "junior"

    clone_tpl = (tmp_path / "config" / "devtype_prompt_templates" / "junior"
                 / "Development.yaml")
    assert clone_tpl.exists()
    text = clone_tpl.read_text()
    assert "I am senior." in text
    assert "dev_type: junior" in text
    # source templates remain
    assert (templates / "Development.yaml").exists()
    # secrets must NOT be copied
    assert not (tmp_path / "secrets" / "junior").exists()
    assert (secrets / "creds.json").read_text() == '{"token":"secret"}\n'


def test_clone_dev_type_copies_active_prompt_leaves_assignments(monkeypatch,
                                                               tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType, Steward

    saved_cfg: list = []
    monkeypatch.setattr(devtypes_service, "save_config",
                        lambda c: saved_cfg.append(
                            dict(c.active_devtype_prompts)))
    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)
    monkeypatch.setattr(devtypes_service.prompt_templates, "seed_devtype_prompts",
                        lambda dts: None)

    dts = {
        "senior": DevType(name="senior", harness_template="codex"),
        "judgment": DevType(name="judgment", harness_template="claude-code"),
    }
    cfg = _cfg("judgment")
    cfg.steward = Steward(enabled=True, dev_type="judgment")
    cfg.active_devtype_prompts = {"senior": "Customer Success",
                                  "judgment": "Development"}
    breakers = {"senior": {"open": True}}

    run_coro(devtypes_service.clone_dev_type(
        "senior", {"new_name": "clone-a"}, config=cfg, dev_types=dts,
        shared_breakers=breakers))

    assert cfg.active_devtype_prompts["clone-a"] == "Customer Success"
    assert cfg.active_devtype_prompts["senior"] == "Customer Success"
    assert cfg.assignments["EXECUTE"].dev_type == "judgment"
    assert cfg.steward.dev_type == "judgment"
    assert breakers == {"senior": {"open": True}}
    assert saved_cfg and saved_cfg[-1]["clone-a"] == "Customer Success"


def test_clone_dev_type_seeds_when_source_has_no_prompt_dir(monkeypatch,
                                                           tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake import config as config_mod
    from devcake.api import devtypes_service
    from devcake.config import DevType

    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    _patch_persist(monkeypatch, devtypes_service)

    seeded: list = []
    real_seed = devtypes_service.prompt_templates.seed_devtype_prompts

    def _track(dts):
        seeded.append(list(dts.keys()))
        real_seed(dts)

    monkeypatch.setattr(devtypes_service.prompt_templates, "seed_devtype_prompts",
                        _track)

    dts = {"bare": DevType(name="bare", harness_template="codex",
                           identifying_prompt="bare prompt")}
    cfg = _cfg("judgment")
    (tmp_path / "config").mkdir(parents=True, exist_ok=True)

    run_coro(devtypes_service.clone_dev_type(
        "bare", {"new_name": "bare-clone"}, config=cfg, dev_types=dts))
    assert seeded == [["bare-clone"]]
    seeded_path = (tmp_path / "config" / "devtype_prompt_templates"
                   / "bare-clone" / "Development.yaml")
    assert seeded_path.exists()
    assert "bare prompt" in seeded_path.read_text()


@pytest.mark.parametrize("new_name,code", [
    ("senior", 409),
    ("a/b", 422),
    ("../x", 422),
    ("bad:name", 422),
    ("", 422),
])
def test_clone_dev_type_refuses_bad_or_collision(monkeypatch, tmp_path,
                                                 new_name, code):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    _patch_persist(monkeypatch, devtypes_service)
    dts = {
        "senior": DevType(name="senior", harness_template="codex"),
        "other": DevType(name="other", harness_template="codex"),
    }
    cfg = _cfg("other")
    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.clone_dev_type(
            "senior", {"new_name": new_name}, config=cfg, dev_types=dts))
    assert e.value.status_code == code
    assert "senior" in dts and "other" in dts
    assert "senior-clone" not in dts


def test_clone_dev_type_404_missing_source(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service

    _patch_persist(monkeypatch, devtypes_service)
    dts = {}
    cfg = _cfg("judgment")
    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.clone_dev_type(
            "ghost", {"new_name": "new"}, config=cfg, dev_types=dts))
    assert e.value.status_code == 404
