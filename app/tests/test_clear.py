"""Unit tests for the operator clear-runs wipe (local state side)."""

from datetime import datetime, timezone
from pathlib import Path

from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run
from devcake.api.clear import clear_local_state


def test_runstore_clear(tmp_path: Path):
    store = RunStore(root=tmp_path / "runs")
    for i in range(3):
        store.save(Run(
            run_id=f"T-{i}-1-ONBOARD-AAAAAA",
            mission_key=f"T-{i}",
            mission_type="ONBOARD",
            dev_type="senior-dev",
            seq=1,
            created_at=datetime.now(timezone.utc),
        ))
    assert len(store.all()) == 3
    n = store.clear()
    assert n == 3
    assert store.all() == []
    assert store.clear() == 0


def test_clear_local_state_preserves_nothing_but_wipes_audit(tmp_path: Path, monkeypatch):
    data = tmp_path / "data"
    runs = data / "state" / "runs"
    runs.mkdir(parents=True)
    audit = data / "state" / "events.jsonl"
    audit.write_text('{"ts":"x","pmo_id":"p","action":"devcake_failed"}\n')
    (data / "cache").mkdir()
    (data / "cache" / "snap.json").write_text("{}")
    (data / "config").mkdir()
    (data / "config" / "config.yaml").write_text("schema_version: 1\n")
    (data / "secrets").mkdir()
    (data / "secrets" / "keep.me").write_text("secret")

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(data))
    # re-import paths that were bound at module load — patch the module attributes
    import devcake.api.clear as clear_mod
    monkeypatch.setattr(clear_mod, "DATA_DIR", data)
    monkeypatch.setattr(clear_mod, "STATE_DIR", data / "state")
    monkeypatch.setattr(clear_mod, "AUDIT_PATH", audit)
    monkeypatch.setattr(clear_mod, "CACHE_DIR", data / "cache")

    store = RunStore(root=runs)
    store.save(Run(
        run_id="T-1-1-ONBOARD-AAAAAA", mission_key="T-1",
        mission_type="ONBOARD", dev_type="senior-dev", seq=1,
    ))
    result = clear_local_state(store)
    assert result["runs_deleted"] == 1
    assert result["audit_cleared"] == 1
    assert result["cache_files_deleted"] == 1
    assert store.all() == []
    assert audit.read_text() == ""
    assert (data / "config" / "config.yaml").exists()
    assert (data / "secrets" / "keep.me").read_text() == "secret"


def test_startup_migration_scrubs_legacy_run_credentials(tmp_path: Path):
    import json

    store = RunStore(root=tmp_path / "runs")
    path = store.root / "T-1-1-ONBOARD-LEGACY.json"
    path.write_text(json.dumps({
        "schema_version": 1,
        "run_id": "T-1-1-ONBOARD-LEGACY",
        "mission_key": "T-1",
        "mission_type": "ONBOARD",
        "dev_type": "senior-dev",
        "seq": 1,
        "state": "running",
        "redis_password": "relay-secret",
        "spec_env": {"PUBLIC": "yes", "DEVCAKE_FORGE_TOKEN": "forge-secret"},
        "spec_files": [{"path_hint": "~/.auth", "content": "oauth-secret"}],
    }))

    active = store.scrub_legacy_secrets()

    assert [run.run_id for run in active] == ["T-1-1-ONBOARD-LEGACY"]
    raw = json.loads(path.read_text())
    assert raw["schema_version"] == 2
    assert raw["spec_env"] == {"PUBLIC": "yes"}
    assert "redis_password" not in raw and "spec_files" not in raw
    assert not store.legacy_secrets_remaining()


def test_scrub_quarantines_unparseable_run_file(tmp_path: Path):
    """One corrupt record must never wedge startup (crash-loop regression)."""
    import json

    store = RunStore(root=tmp_path / "runs")
    corrupt = store.root / "T-9-1-ONBOARD-CORRUPT.json"
    corrupt.write_text('{"schema_version": 1, "run_id": "T-9')  # truncated write
    legacy = store.root / "T-1-1-ONBOARD-LEGACY.json"
    legacy.write_text(json.dumps({
        "schema_version": 1, "run_id": "T-1-1-ONBOARD-LEGACY",
        "mission_key": "T-1", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": 1, "state": "finished",
        "redis_password": "relay-secret", "spec_env": {"PUBLIC": "yes"},
    }))

    store.scrub_legacy_secrets()

    assert not corrupt.exists()
    assert (store.root / "quarantine" / corrupt.name).exists()
    assert store.legacy_secrets_remaining() == []
    assert [r.run_id for r in store.all()] == ["T-1-1-ONBOARD-LEGACY"]


def test_scrub_model_invalid_file_scrubbed_at_raw_level(tmp_path: Path):
    """A parseable record the model rejects still has its secrets removed."""
    import json

    store = RunStore(root=tmp_path / "runs")
    path = store.root / "T-2-1-ONBOARD-BADMODEL.json"
    path.write_text(json.dumps({
        "schema_version": 1, "run_id": "T-2-1-ONBOARD-BADMODEL",
        "mission_key": "T-2", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": "not-an-int-at-all",
        "state": "bogus-state",
        "redis_password": "relay-secret",
        "spec_env": {"PUBLIC": "yes", "DEVCAKE_FORGE_TOKEN": "forge-secret"},
    }))

    store.scrub_legacy_secrets()

    raw = json.loads(path.read_text())
    assert raw["schema_version"] == 2
    assert "redis_password" not in raw and "spec_files" not in raw
    assert raw["spec_env"] == {"PUBLIC": "yes"}
    assert store.legacy_secrets_remaining() == []


def test_legacy_secrets_remaining_names_files(tmp_path: Path):
    import json

    store = RunStore(root=tmp_path / "runs")
    (store.root / "T-3-1-ONBOARD-HOT.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "T-3-1-ONBOARD-HOT",
        "redis_password": "relay-secret",
    }))
    assert store.legacy_secrets_remaining() == ["T-3-1-ONBOARD-HOT.json"]


def test_clear_removes_quarantined_files(tmp_path: Path):
    store = RunStore(root=tmp_path / "runs")
    (store.root / "T-9-1-ONBOARD-CORRUPT.json").write_text("{broken")
    store.scrub_legacy_secrets()
    assert (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()
    assert store.clear() == 1
    assert not (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()
