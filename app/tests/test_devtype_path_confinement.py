"""Path confinement on Dev Type rename/remove/YAML-delete (CAKE-138).

Charset re-check + pathsafety.confined before shutil.move / rmtree / unlink
so forged names cannot leave /data/secrets, /data/config/devtype_prompt_templates,
or CONFIG_PATH.parent / "dev_types".
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import HTTPException


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _cfg_assignments(dev_type: str = "judgment"):
    from devcake.config import AppConfig, Assignment
    return AppConfig(assignments={
        mt: Assignment(dev_type=dev_type)
        for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")})


# ── config.delete_dev_type ───────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ("..", "../x", "a/b", "../config"))
def test_delete_dev_type_refuses_traversal_names(monkeypatch, tmp_path, bad):
    """Bad names must raise ValueError and must not unlink a planted sibling."""
    import devcake.config as config_mod

    config_dir = tmp_path / "config"
    dt_dir = config_dir / "dev_types"
    dt_dir.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_dir / "config.yaml")

    sentinel = config_dir / "config.yaml"
    sentinel.write_text("schema_version: 1\nkeep: me\n")
    # Intermediate jail root already exists (dt_dir) — CAKE-135: without it,
    # unresolved `..` never reaches the sibling and the test is a false green.
    with pytest.raises(ValueError):
        config_mod.delete_dev_type(bad)

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


def test_delete_dev_type_happy_path_unlinks_yaml(monkeypatch, tmp_path):
    import devcake.config as config_mod

    config_dir = tmp_path / "config"
    dt_dir = config_dir / "dev_types"
    dt_dir.mkdir(parents=True)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", config_dir / "config.yaml")
    target = dt_dir / "junior-dev.yaml"
    target.write_text("name: junior-dev\n")

    config_mod.delete_dev_type("junior-dev")
    assert not target.exists()


# ── rename_dev_type / remove_dev_type refusal ────────────────────────────────

def test_rename_dev_type_refuses_forged_traversal_name(monkeypatch, tmp_path):
    """Inject a traversal-shaped key into dev_types (bypassing create-time
    DevType.name validation). Rename must 422 and leave a sibling sentinel."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)

    secrets_root = tmp_path / "secrets"
    templates_root = tmp_path / "config" / "devtype_prompt_templates"
    secrets_root.mkdir(parents=True)
    templates_root.mkdir(parents=True)
    sentinel = tmp_path / "outside-secret"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("do-not-touch\n")

    forged = "../outside-secret"
    # Membership bypass: key is the path component rename uses.
    dts = {forged: DevType(name="legit", harness_template="codex",
                           identifying_prompt="x")}
    cfg = _cfg_assignments("judgment")
    # Point no assignment at the forged key so 409 cannot mask the charset check.
    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.rename_dev_type(
            forged, {"new_name": "safe-new"}, config=cfg, dev_types=dts,
            shared_breakers={}))
    assert e.value.status_code == 422
    assert (sentinel / "keep.txt").read_text() == "do-not-touch\n"
    assert forged in dts  # pop must not have happened


def test_remove_dev_type_refuses_forged_traversal_name(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)

    secrets_root = tmp_path / "secrets"
    templates_root = tmp_path / "config" / "devtype_prompt_templates"
    secrets_root.mkdir(parents=True)
    templates_root.mkdir(parents=True)
    sentinel = tmp_path / "outside-secret"
    sentinel.mkdir()
    (sentinel / "keep.txt").write_text("do-not-touch\n")

    forged = "../outside-secret"
    dts = {
        forged: DevType(name="legit", harness_template="codex"),
        "judgment": DevType(name="judgment", harness_template="claude-code"),
    }
    cfg = _cfg_assignments("judgment")

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.remove_dev_type(
            forged, config=cfg, dev_types=dts))
    assert e.value.status_code == 422
    assert (sentinel / "keep.txt").read_text() == "do-not-touch\n"
    assert forged in dts


def test_rename_dev_type_refuses_bad_new_name_with_slash(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)

    dts = {"olddev": DevType(name="olddev", harness_template="codex")}
    cfg = _cfg_assignments("olddev")

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.rename_dev_type(
            "olddev", {"new_name": "a/b"}, config=cfg, dev_types=dts,
            shared_breakers={}))
    assert e.value.status_code == 422
    assert "olddev" in dts


# ── happy path: rename moves both sidecar dirs ───────────────────────────────

def test_rename_dev_type_moves_secrets_and_templates(monkeypatch, tmp_path):
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import devtypes_service
    from devcake.config import DevType

    monkeypatch.setattr(devtypes_service, "save_config", lambda c: None)
    monkeypatch.setattr(devtypes_service, "save_dev_type", lambda d: None)
    monkeypatch.setattr(devtypes_service, "delete_dev_type", lambda n: None)
    monkeypatch.setattr(devtypes_service, "publish_keep_set", lambda dts: None)

    dt = DevType(name="olddev", harness_template="codex",
                 identifying_prompt="I am old.")
    dts = {"olddev": dt}
    cfg = _cfg_assignments("olddev")
    cfg.active_devtype_prompts = {"olddev": "Customer Success"}

    templates = tmp_path / "config" / "devtype_prompt_templates" / "olddev"
    templates.mkdir(parents=True)
    (templates / "Development.yaml").write_text(
        "name: Development\ntemplate: I am old.\n")
    secrets = tmp_path / "secrets" / "olddev"
    secrets.mkdir(parents=True)
    (secrets / "creds.json").write_text("{}")

    out = run_coro(devtypes_service.rename_dev_type(
        "olddev", {"new_name": "newdev"}, config=cfg, dev_types=dts,
        shared_breakers={}))
    assert out["renamed"] and "newdev" in dts and "olddev" not in dts
    assert cfg.assignments["EXECUTE"].dev_type == "newdev"
    assert cfg.active_devtype_prompts == {"newdev": "Customer Success"}
    assert (tmp_path / "config" / "devtype_prompt_templates" / "newdev"
            / "Development.yaml").exists()
    assert (tmp_path / "secrets" / "newdev" / "creds.json").exists()
    assert not (tmp_path / "secrets" / "olddev").exists()
    assert not (tmp_path / "config" / "devtype_prompt_templates" / "olddev").exists()
