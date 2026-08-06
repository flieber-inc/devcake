"""MAPPER→STEWARD rename (2026-08-06): the one-time migrations. Code carries
no aliases — persisted shapes (config YAML, dev-type files, settings bundles,
run records) migrate at their load seams instead; run_id strings stay
historical (immutable identifiers referenced by Dagu/OTel/relay)."""
import json

from devcake import config as config_mod
from devcake.config import AppConfig, load_dev_types
from devcake.adapters.files.run_store import RunStore


def test_raw_config_migrates_relations_mapper_key_and_dev_type_names():
    cfg = AppConfig.model_validate({
        "relations_mapper": {"enabled": True, "interval_minutes": 15,
                             "dev_type": "mapper"},
        "assignments": {"EXECUTE": {"dev_type": "mapper"}},
        "pmos": [{"name": "linear", "team_key": "DEV",
                  "assignments": {"REVIEW": {"dev_type": "mapper"}}}],
    })
    assert cfg.steward.enabled is True and cfg.steward.interval_minutes == 15
    assert cfg.steward.dev_type == "steward"
    assert cfg.assignments["EXECUTE"].dev_type == "steward"
    assert cfg.pmos[0].assignments["REVIEW"].dev_type == "steward"


def test_raw_config_migration_is_idempotent_and_respects_new_shape():
    cfg = AppConfig.model_validate({"steward": {"enabled": True,
                                                "dev_type": "custom"}})
    assert cfg.steward.enabled is True and cfg.steward.dev_type == "custom"
    # old key never clobbers an explicit new one
    cfg = AppConfig.model_validate({
        "steward": {"enabled": True},
        "relations_mapper": {"enabled": False}})
    assert cfg.steward.enabled is True


def test_dev_type_file_migration_preserves_customizations(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    dt_dir = tmp_path / "dev_types"
    dt_dir.mkdir(parents=True)
    (dt_dir / "mapper.yaml").write_text(
        "name: mapper\nharness_template: grok-build\nmodel: cheap-model\n")
    out = load_dev_types()
    assert "mapper" not in out and "steward" in out
    assert out["steward"].harness_template == "grok-build"   # customization kept
    assert not (dt_dir / "mapper.yaml").exists()
    # seeded default did NOT overwrite the migrated file
    assert out["steward"].model == "cheap-model"


def test_dev_type_file_migration_yields_to_existing_steward(tmp_path, monkeypatch):
    monkeypatch.setattr(config_mod, "CONFIG_PATH", tmp_path / "config.yaml")
    dt_dir = tmp_path / "dev_types"
    dt_dir.mkdir(parents=True)
    (dt_dir / "mapper.yaml").write_text(
        "name: mapper\nharness_template: grok-build\n")
    (dt_dir / "steward.yaml").write_text(
        "name: steward\nharness_template: codex\n")
    out = load_dev_types()
    assert out["steward"].harness_template == "codex"        # explicit wins
    assert "mapper" not in out
    assert not (dt_dir / "mapper.yaml").exists()             # retired anyway


def test_run_store_migrates_records_but_never_run_ids(tmp_path):
    store = RunStore(tmp_path)
    old = {"schema_version": 2, "run_id": "LINEAR-DEV-1-MAPPER-AAAAAA",
           "mission_key": "DEV", "mission_type": "MAPPER",
           "dev_type": "mapper", "seq": 1}
    keep = {"schema_version": 2, "run_id": "T-1-1-EXECUTE-AAAAAA",
            "mission_key": "T-1", "mission_type": "EXECUTE",
            "dev_type": "implementer", "seq": 1}
    (tmp_path / "LINEAR-DEV-1-MAPPER-AAAAAA.json").write_text(json.dumps(old))
    (tmp_path / "T-1-1-EXECUTE-AAAAAA.json").write_text(json.dumps(keep))
    assert store.migrate_steward_names() == 1
    runs = {r.run_id: r for r in store.all()}
    m = runs["LINEAR-DEV-1-MAPPER-AAAAAA"]                   # id: historical
    assert m.mission_type == "STEWARD" and m.dev_type == "steward"
    assert runs["T-1-1-EXECUTE-AAAAAA"].mission_type == "EXECUTE"
    assert store.migrate_steward_names() == 0                # idempotent
