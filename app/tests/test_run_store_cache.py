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


def test_clear_empties_parse_cache_immediately(tmp_path):
    """AUD-018: clear() must drop cached parses at once, not wait for the next
    all() to prune by absent-file — an intervening read must not see wiped
    runs from the cache."""
    store = RunStore(tmp_path / "runs")
    store.save(_run(1))
    store.all()                                   # populate the parse cache
    assert store._parse_cache                       # non-empty
    store.clear()
    assert store._parse_cache == {}                 # emptied by clear() itself


def test_corrupt_files_stay_skipped_on_every_call(tmp_path):
    store = RunStore(tmp_path / "runs")
    store.save(_run(1))
    (tmp_path / "runs" / "junk.json").write_text("{not json")
    assert [r.run_id for r in store.all()] == [_run(1).run_id]
    assert [r.run_id for r in store.all()] == [_run(1).run_id]


# ── the lost-update fence (2026-08-12 audit F8) ──────────────────────────────


def test_lost_update_fence_trips_loudly_but_preserves_last_writer(
        tmp_path, caplog):
    """get() and all() can hand two writers two different objects for the
    same run; the stale writer's save used to discard the other's fields
    SILENTLY. Semantics preserved (last writer wins), collision now loud."""
    import logging

    store = RunStore(tmp_path)
    r = _run("fence1")
    store.save(r)                       # rev 0 → 1

    a = store.get(r.run_id)             # two INDEPENDENT objects
    b = store.get(r.run_id)
    a.verdict = "from writer A"
    store.save(a)                       # rev 1 → 2, clean
    b.verdict = "from writer B"
    with caplog.at_level(logging.ERROR):
        store.save(b)                   # stale rev 1 vs disk 2 → fence
    assert any("lost-update fence tripped" in rec.message
               for rec in caplog.records)
    assert store.get(r.run_id).verdict == "from writer B"  # last writer wins


def test_same_object_resaves_never_trip_the_fence(tmp_path, caplog):
    """The sanctioned pattern — mutate-then-save the SAME object repeatedly
    (checkpoints, heartbeats) — must stay silent."""
    import logging

    store = RunStore(tmp_path)
    r = _run("fence2")
    with caplog.at_level(logging.ERROR):
        for i in range(5):
            r.verdict = f"step {i}"
            store.save(r)
    assert not any("lost-update fence" in rec.message
                   for rec in caplog.records)
    assert store.get(r.run_id).rev == 5


def test_legacy_record_without_rev_parses_and_fences_forward(tmp_path):
    import json

    store = RunStore(tmp_path)
    r = _run("legacy1")
    store.save(r)
    # simulate a pre-rev record on disk
    p = tmp_path / f"{r.run_id}.json"
    raw = json.loads(p.read_text())
    raw.pop("rev")
    p.write_text(json.dumps(raw))
    got = store.get(r.run_id)
    assert got.rev == 0
    got.verdict = "migrated forward"
    store.save(got)
    assert store.get(r.run_id).rev == 1
