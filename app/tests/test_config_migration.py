"""Config schema v1 → v2 migration (plural pmos:/repos: instance lists) and
the exactly-one-instance constraint. The migration is forward-only: load_config
keeps a config.yaml.v1.bak next to the migrated file."""

import yaml
import pytest

import devcake.config as config_mod
from devcake.config import AppConfig, migrate_config, migrate_config_patch

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
  reviewer_token_env: GH_REVIEWER
adoption_mode: opt_in
auto_merge: true
"""


def test_migrate_v1_to_v2():
    data = yaml.safe_load(V1_YAML)
    out = migrate_config(data)
    assert out["schema_version"] == 2
    assert "pmo" not in out and "repo" not in out
    assert out["pmos"] == [{"id": "main", "system": "linear",
                            "api_key_env": "LINEAR_API_KEY", "team_key": "DEV"}]
    assert out["repos"][0]["id"] == "main"
    assert out["repos"][0]["url"] == "https://github.com/x/y"
    assert out["repos"][0]["reviewer_token_env"] == "GH_REVIEWER"
    assert out["auto_merge"] is True                       # siblings untouched
    cfg = AppConfig.model_validate(out)                    # validates cleanly
    assert cfg.pmo.team_key == "DEV"                       # property accessor
    assert cfg.repo.default_branch == "main"               # new field defaulted


def test_migrate_is_idempotent():
    once = migrate_config(yaml.safe_load(V1_YAML))
    assert migrate_config(dict(once)) == once


def test_migrate_empty_and_partial():
    # first-boot-ish v1 files with missing blocks migrate to defaults
    out = migrate_config({"schema_version": 1})
    cfg = AppConfig.model_validate(out)
    assert cfg.pmo.system == "linear" and cfg.repo.forge == "github"


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


def test_legacy_put_body_adapted_not_dropped():
    """pydantic ignores unknown keys, so without the patch adapter a stale
    client's {"pmo": {...}} PUT would silently lose the edit."""
    cfg = AppConfig.model_validate(migrate_config(yaml.safe_load(V1_YAML)))
    body = migrate_config_patch({"pmo": {"team_key": "OPS"}}, cfg)
    assert "pmo" not in body
    assert body["pmos"][0]["team_key"] == "OPS"
    assert body["pmos"][0]["api_key_env"] == "LINEAR_API_KEY"  # sibling kept
    # non-legacy bodies pass through untouched
    assert migrate_config_patch({"auto_merge": False}, cfg) == {"auto_merge": False}


def test_load_config_migrates_in_place_with_backup(tmp_path, monkeypatch):
    path = tmp_path / "config" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(V1_YAML)
    monkeypatch.setattr(config_mod, "CONFIG_PATH", path)
    cfg = config_mod.load_config()
    assert cfg.schema_version == 2 and cfg.pmo.team_key == "DEV"
    on_disk = yaml.safe_load(path.read_text())
    assert on_disk["schema_version"] == 2 and "pmo" not in on_disk
    bak = path.with_suffix(".yaml.v1.bak")
    assert bak.exists()
    assert yaml.safe_load(bak.read_text())["schema_version"] == 1
    # second boot: already v2, no re-migration, backup untouched
    cfg2 = config_mod.load_config()
    assert cfg2.pmo.team_key == "DEV"
