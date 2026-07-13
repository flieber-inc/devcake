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
