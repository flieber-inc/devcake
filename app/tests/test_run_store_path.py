"""RunStore path discipline: get/delete/save must refuse ids that fail
RUN_ID_RE so URL-supplied run_id values cannot escape the runs root
(CAKE-137 / CodeQL path traversal)."""

from __future__ import annotations

import logging

from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run


def _valid_run(run_id: str = "R-1-1-EXECUTE-AAAAAA", **over) -> Run:
    base = dict(
        run_id=run_id,
        mission_key="R-1",
        mission_type="EXECUTE",
        dev_type="senior-dev",
        seq=1,
        state="running",
    )
    base.update(over)
    return Run(**base)


def test_get_traversal_and_garbage_ids_return_none(tmp_path):
    store = RunStore(tmp_path / "runs")
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    bait = secrets / "foo.json"
    bait.write_text('{"schema_version": 2, "run_id": "bait"}')

    assert store.get("../secrets/foo") is None
    assert store.get("..") is None
    assert store.get("../escape") is None
    assert store.get("") is None
    assert store.get("short") is None
    assert store.get("a/b") is None

    assert bait.exists()
    assert bait.read_text() == '{"schema_version": 2, "run_id": "bait"}'
    assert list(store.root.glob("*.json")) == []


def test_delete_traversal_does_not_unlink_outside_root(tmp_path):
    store = RunStore(tmp_path / "runs")
    victim = tmp_path / "victim.json"
    victim.write_text("do-not-delete")

    store.delete("../victim")
    store.delete("..")
    store.delete("../secrets/foo")
    store.delete("")
    store.delete("a/b")

    assert victim.exists()
    assert victim.read_text() == "do-not-delete"
    assert list(store.root.glob("*.json")) == []


def test_valid_run_id_round_trips_save_get_delete(tmp_path):
    store = RunStore(tmp_path / "runs")
    run = _valid_run("R-1-1-EXECUTE-AAAAAA")

    store.save(run)
    got = store.get(run.run_id)
    assert got is not None
    assert got.run_id == run.run_id
    assert got.state == "running"
    assert (store.root / f"{run.run_id}.json").is_file()

    store.delete(run.run_id)
    assert store.get(run.run_id) is None
    assert not (store.root / f"{run.run_id}.json").exists()


def test_save_invalid_run_id_refuses_without_writing_outside_root(
        tmp_path, caplog):
    store = RunStore(tmp_path / "runs")
    outside = tmp_path / "secrets"
    outside.mkdir()

    bad = _valid_run(run_id="../secrets/foo")
    with caplog.at_level(logging.INFO):
        store.save(bad)

    assert not (outside / "foo.json").exists()
    assert list(store.root.glob("*.json")) == []
    assert any("invalid run id" in rec.message.lower()
               or "drop save" in rec.message.lower()
               for rec in caplog.records)
