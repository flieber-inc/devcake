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
    (data / "config").mkdir()
    (data / "config" / "config.yaml").write_text("schema_version: 2\n")
    (data / "secrets").mkdir()
    (data / "secrets" / "keep.me").write_text("secret")

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(data))
    # re-import paths that were bound at module load — patch the module attributes
    import devcake.api.clear as clear_mod
    monkeypatch.setattr(clear_mod, "DATA_DIR", data)
    monkeypatch.setattr(clear_mod, "STATE_DIR", data / "state")
    monkeypatch.setattr(clear_mod, "AUDIT_PATH", audit)

    store = RunStore(root=runs)
    store.save(Run(
        run_id="T-1-1-ONBOARD-AAAAAA", mission_key="T-1",
        mission_type="ONBOARD", dev_type="senior-dev", seq=1,
    ))
    result = clear_local_state(store)
    assert result["runs_deleted"] == 1
    assert result["audit_cleared"] == 1
    assert store.all() == []
    assert audit.read_text() == ""
    assert (data / "config" / "config.yaml").exists()
    assert (data / "secrets" / "keep.me").read_text() == "secret"


def test_quarantine_unreadable_moves_corrupt_and_prev2_records(tmp_path: Path):
    """One bad record must never wedge startup (crash-loop regression); a
    pre-v2 record may carry credentials at rest and is quarantined wholesale
    (the v1 scrub was removed at v0 — docs/10 §5). Healthy records stay."""
    import json

    store = RunStore(root=tmp_path / "runs")
    corrupt = store.root / "T-9-1-ONBOARD-CORRUPT.json"
    corrupt.write_text('{"schema_version": 2, "run_id": "T-9')  # truncated write
    bad_model = store.root / "T-2-1-ONBOARD-BADMODEL.json"
    bad_model.write_text(json.dumps({
        "schema_version": 2, "run_id": "T-2-1-ONBOARD-BADMODEL",
        "mission_key": "T-2", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": "not-an-int-at-all",
        "state": "bogus-state",
    }))
    legacy = store.root / "T-1-1-ONBOARD-LEGACY.json"
    legacy.write_text(json.dumps({
        "schema_version": 1, "run_id": "T-1-1-ONBOARD-LEGACY",
        "mission_key": "T-1", "mission_type": "ONBOARD",
        "dev_type": "senior-dev", "seq": 1, "state": "finished",
        "redis_password": "relay-secret", "spec_env": {"PUBLIC": "yes"},
    }))
    store.save(Run(
        run_id="T-4-1-ONBOARD-OK", mission_key="T-4",
        mission_type="ONBOARD", dev_type="senior-dev", seq=1,
    ))

    quarantined = store.quarantine_unreadable()

    assert sorted(quarantined) == ["T-1-1-ONBOARD-LEGACY", "T-2-1-ONBOARD-BADMODEL",
                                   "T-9-1-ONBOARD-CORRUPT"]
    for name in (corrupt.name, bad_model.name, legacy.name):
        assert not (store.root / name).exists()
        assert (store.root / "quarantine" / name).exists()
    # a parseable quarantined record is scrubbed — no secret-at-rest, even here
    raw = json.loads((store.root / "quarantine" / legacy.name).read_text())
    assert "redis_password" not in raw
    # the truncated file couldn't be scrubbed; its bytes are preserved as-is
    assert (store.root / "quarantine" / corrupt.name).read_text().startswith('{"schema_version"')
    assert [r.run_id for r in store.all()] == ["T-4-1-ONBOARD-OK"]
    assert store.quarantine_unreadable() == []  # idempotent on a clean store


def test_clear_removes_quarantined_files(tmp_path: Path):
    store = RunStore(root=tmp_path / "runs")
    (store.root / "T-9-1-ONBOARD-CORRUPT.json").write_text("{broken")
    store.quarantine_unreadable()
    assert (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()
    assert store.clear() == 1
    assert not (store.root / "quarantine" / "T-9-1-ONBOARD-CORRUPT.json").exists()
