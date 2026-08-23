"""Path-injection hardening on prompt-template DELETE (CAKE-135).

Save paths already gate names with _NAME_RE; delete paths must refuse
traversal names / invalid Dev Type names and must not unlink outside the
intended template subdirectory.
"""

import asyncio

import pytest
from fastapi import HTTPException


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _tpl(monkeypatch, tmp_path):
    import devcake.config as config_mod
    monkeypatch.setattr(config_mod, "CONFIG_PATH",
                        tmp_path / "config" / "config.yaml")
    from devcake.prompts import templates
    return templates


def test_delete_devtype_prompt_refuses_parent_dev_type_escape(monkeypatch, tmp_path):
    """dev_type='..' must not resolve to /data/config/{name}.yaml and unlink.

    The escape only resolves when the templates root exists (POSIX needs the
    intermediate dir to walk `..`); create it so the pre-fix path would
    reach the sentinel.
    """
    t = _tpl(monkeypatch, tmp_path)
    config_dir = tmp_path / "config"
    (config_dir / "devtype_prompt_templates").mkdir(parents=True)
    sentinel = config_dir / "config.yaml"
    sentinel.write_text("schema_version: 1\nkeep: me\n")

    with pytest.raises(ValueError):
        t.delete_devtype_prompt("..", "config")

    assert sentinel.exists()
    assert "keep: me" in sentinel.read_text()


def test_delete_devtype_prompt_refuses_slash_and_dotdot_name(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    root = tmp_path / "config" / "devtype_prompt_templates"
    (root / "judgment").mkdir(parents=True)
    sentinel = root / "evil.yaml"
    sentinel.write_text("sentinel\n")
    # name with slash / .. would escape the per-dev-type dir
    for bad in ("../evil", "a/b"):
        with pytest.raises(ValueError):
            t.delete_devtype_prompt("judgment", bad)
    assert sentinel.exists()


def test_delete_template_refuses_slash_and_dotdot_name(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    base = tmp_path / "config" / "prompt_templates"
    sentinel = base / "evil.yaml"
    sentinel.write_text("sentinel\n")
    for bad in ("../evil", "a/b"):
        with pytest.raises(ValueError):
            t.delete_template("EXECUTE", bad)
    assert sentinel.exists()
    # builtins under EXECUTE must still be present
    assert (base / "EXECUTE" / "Development.yaml").exists()

def test_delete_template_happy_path(monkeypatch, tmp_path):
    t = _tpl(monkeypatch, tmp_path)
    t.seed_default_templates()
    t.save_template("EXECUTE", "terse", "just {key} on {branch}")
    path = (tmp_path / "config" / "prompt_templates" / "EXECUTE" / "terse.yaml")
    assert path.exists()
    t.delete_template("EXECUTE", "terse")
    assert not path.exists()


def test_delete_devtype_prompt_happy_path(monkeypatch, tmp_path):
    from devcake.config import DevType
    t = _tpl(monkeypatch, tmp_path)
    dts = {"judgment": DevType(name="judgment", harness_template="claude-code",
                               identifying_prompt="You are judgment.")}
    t.seed_devtype_prompts(dts)
    t.save_devtype_prompt("judgment", "custom", "Custom identifying text.")
    path = (tmp_path / "config" / "devtype_prompt_templates" / "judgment"
            / "custom.yaml")
    assert path.exists()
    t.delete_devtype_prompt("judgment", "custom")
    assert not path.exists()


def test_api_delete_devtype_prompt_unknown_dev_type_404(monkeypatch, tmp_path):
    """API DELETE must mirror PUT: unknown / traversal-shaped dev_type → 404."""
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    dts = {"judgment": DevType(name="judgment", harness_template="claude-code")}
    cfg = AppConfig()

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.delete_devtype_prompt(
            "ghost-dev", "custom", config=cfg, dev_types=dts))
    assert e.value.status_code == 404

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.delete_devtype_prompt(
            "..", "config", config=cfg, dev_types=dts))
    assert e.value.status_code == 404


def test_api_delete_devtype_prompt_bad_name_422(monkeypatch, tmp_path):
    from devcake.api import devtypes_service
    from devcake.config import AppConfig, DevType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    dts = {"judgment": DevType(name="judgment", harness_template="claude-code")}
    cfg = AppConfig()

    with pytest.raises(HTTPException) as e:
        run_coro(devtypes_service.delete_devtype_prompt(
            "judgment", "../evil", config=cfg, dev_types=dts))
    assert e.value.status_code == 422
