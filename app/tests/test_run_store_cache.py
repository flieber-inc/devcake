"""RunStore.all() mtime-keyed parse cache (founder decision 2026-08-02,
chosen over SQLite/Redis): the app is ONE process, so a file whose
(mtime_ns, size) is unchanged since the last parse cannot have changed
underneath us — save() always lands via os.replace (new inode, fresh
mtime). Object identity across calls IS the no-reparse proof; a changed,
deleted, or corrupt file must still behave exactly as before."""

from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run


def _run(i, **over):
    base = dict(run_id=f"A-{i}-1-EXECUTE-{'Z' * 6}", mission_key=f"A-{i}",
                mission_type="EXECUTE", dev_type="senior-dev", seq=1,
                state="running")
    base.update(over)
    return Run(**base)


def test_unchanged_files_are_not_reparsed(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.save(_run(1))
    first = store.all()
    second = store.all()
    assert second[0] is first[0]          # same object ⇒ no second parse

    # a save re-lands the file via os.replace → next all() re-parses it
    store.save(_run(1, state="finished"))
    third = store.all()
    assert third[0] is not first[0]
    assert third[0].state == "finished"

    # the cache is per-instance advisory state — a fresh store re-parses
    assert RunStore(tmp_path / "runs").all()[0] is not third[0]


def test_deleted_files_leave_the_cache(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.save(_run(1))
    store.save(_run(2))
    assert len(store.all()) == 2
    (tmp_path / "runs" / f"{_run(2).run_id}.json").unlink()
    assert [r.run_id for r in store.all()] == [_run(1).run_id]
    # clear() unlinks everything — nothing may resurrect from the cache
    store.clear()
    assert store.all() == []


def test_corrupt_files_stay_skipped_on_every_call(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.save(_run(1))
    (tmp_path / "runs" / "junk.json").write_text("{not json")
    assert [r.run_id for r in store.all()] == [_run(1).run_id]
    assert [r.run_id for r in store.all()] == [_run(1).run_id]
