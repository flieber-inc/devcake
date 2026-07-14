"""Config schema v2 (plural pmos:/repos: instance lists): the exactly-one
constraint, registry validation, and the loud refusal of v1-shaped input —
the v1→v2 auto-migration was removed at v0 crystallization."""

import yaml
import pytest

import devcake.config as config_mod
from devcake.config import AppConfig, reject_v1_patch

V1_YAML = """
schema_version: 1
pmo:
  system: linear
  api_key_env: LINEAR_API_KEY
  team_key: DEV
repo:
  forge: github
  url: https://github.com/x/y
  token_env: GITHUB_TOKEN
adoption_mode: opt_in
"""


def test_exactly_one_instance_enforced():
    base = AppConfig().model_dump()
    two = dict(base, pmos=base["pmos"] + [dict(base["pmos"][0], id="second")])
    with pytest.raises(Exception):
        AppConfig.model_validate(two)
    with pytest.raises(Exception):
        AppConfig.model_validate(dict(base, repos=[]))
    dupes = dict(base, repos=[base["repos"][0], dict(base["repos"][0])])
    with pytest.raises(Exception):
        AppConfig.model_validate(dupes)


def test_unknown_pmo_system_rejected():
    base = AppConfig().model_dump()
    base["pmos"][0]["system"] = "jira"          # not in the adapter registry
    with pytest.raises(Exception, match="unknown PMO system"):
        AppConfig.model_validate(base)


def test_make_pmo_dispatches_from_registry():
    from devcake.adapters.linear import LinearAdapter
    from devcake.adapters.registry import PMO_SYSTEMS, make_pmo
    cfg = AppConfig()
    assert isinstance(make_pmo(cfg.pmo), LinearAdapter)
    assert set(PMO_SYSTEMS) == {"linear"}
    info = PMO_SYSTEMS["linear"]
    assert info.api_key_env_default == "LINEAR_API_KEY"
    assert info.secret_env_vars and info.token_patterns and info.secret_shape_prefixes


def test_v1_put_body_rejected_not_dropped():
    """pydantic ignores unknown keys, so without the guard a stale client's
    {"pmo": {...}} PUT would silently lose the edit instead of failing."""
    with pytest.raises(ValueError, match="schema v1"):
        reject_v1_patch({"pmo": {"team_key": "OPS"}})
    with pytest.raises(ValueError, match="schema v1"):
        reject_v1_patch({"repo": {"url": "https://github.com/x/y"}})
    # v2 bodies pass through untouched
    reject_v1_patch({"auto_merge": False})
    reject_v1_patch({"pmos": [{"team_key": "OPS"}]})


def test_load_config_refuses_v1_file(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(V1_YAML)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    with pytest.raises(RuntimeError, match="v1 shape"):
        config_mod.load_config()
    # the refusal must not touch the file — the operator migrates it by hand
    assert yaml.safe_load(path.read_text())["schema_version"] == 1


def test_load_config_accepts_v2_without_version_field(tmp_path, monkeypatch):
    """Detection keys on the singular pmo:/repo: keys, never on an absent
    schema_version — an empty file or a hand-written v2 config must boot."""
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("pmos:\n- id: main\n  team_key: DEV\nrepos:\n- id: main\n")
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    assert config_mod.load_config().pmo.team_key == "DEV"

    path.write_text("")  # empty file → defaults, same as first boot
    assert config_mod.load_config().schema_version == 2
