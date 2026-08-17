"""Post-approve auto-merge settle window + end-of-window freshness recheck."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from devcake.domain.model import ActivityEntry, MissionType
from devcake.domain.orchestrator import review, sweeps
from devcake.domain.orchestrator.markers import (MERGE_RETRY_MARKER,
                                                 MERGE_SETTLE_MARKER)
from devcake.domain.run import Run

from test_freshness_gate import SENTINEL, _entry, _gate_mgr, _review_run
from test_transitions import FakeForge, make_mgr, mission


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_config_merge_settle_default_zero():
    from devcake.config import RepoInstance
    r = RepoInstance(name="main", forge="github")
    assert r.merge_settle_minutes == 0


def test_finalize_settle_parks_without_merge(tmp_path):
    m, mgr, fake = _gate_mgr(
        tmp_path,
        [_entry("e1", "brief" + SENTINEL, author="devcake")],
        auto_merge=True)
    inst = mgr.forges.instance("main")
    inst.merge_settle_minutes = 15
    inst.auto_merge = True
    run = _review_run(watermark_id="e1")
    run.repo_ref = "main"
    run_coro(review.finalize_review(
        mgr, run, {"verdict": "approve", "report_md": "ok"}))
    forge = mgr.forges.get("main")
    assert forge.merges == []
    assert "DEVCAKE-MERGE" in m.labels
    assert "DEVCAKE-REVIEW" not in m.labels
    assert any(MERGE_SETTLE_MARKER in c for c in fake.comments)
    assert "review:merge_settle" in run.finalized_steps


def test_finalize_settle_zero_still_auto_merges(tmp_path):
    m, mgr, fake = _gate_mgr(
        tmp_path,
        [_entry("e1", "brief" + SENTINEL, author="devcake")],
        auto_merge=True)
    inst = mgr.forges.instance("main")
    inst.merge_settle_minutes = 0
    inst.auto_merge = True
    run = _review_run(watermark_id="e1")
    run.repo_ref = "main"
    run_coro(review.finalize_review(
        mgr, run, {"verdict": "approve", "report_md": "ok"}))
    forge = mgr.forges.get("main")
    assert forge.merges == [8]
    assert "done" in fake.statuses


def test_sweep_settle_before_window_does_not_merge(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    m.repo = "main"
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    inst = mgr.forges.instance("main")
    inst.auto_merge = True
    inst.merge_settle_minutes = 30
    now = datetime.now(timezone.utc)
    fake.activity_entries = [
        _entry("s1", f"settling {MERGE_SETTLE_MARKER}\n\n" + SENTINEL,
               author="devcake", ts=now - timedelta(minutes=5)),
    ]
    # finished REVIEW anchor (not needed while still in window)
    r = _review_run(watermark_id="s1")
    r.state = "finished"
    r.pmo_ref = "main"
    store.save(r)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert mgr.forges.get("main").merges == []
    assert "DEVCAKE-MERGE" in m.labels
    assert not any(MERGE_RETRY_MARKER in c for c in fake.comments)


def test_sweep_settle_elapsed_trips_on_discovery_in(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    m.repo = "main"
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    inst = mgr.forges.instance("main")
    inst.auto_merge = True
    inst.merge_settle_minutes = 10
    now = datetime.now(timezone.utc)
    fake.activity_entries = [
        _entry("wm", "approve note" + SENTINEL, author="devcake",
               ts=now - timedelta(minutes=40)),
        _entry("s1", f"settling {MERGE_SETTLE_MARKER}\n\n" + SENTINEL,
               author="devcake", ts=now - timedelta(minutes=20)),
        _entry("d1", "`devcake:discovery-in:v1 src=T-2 step=1`\n\n"
               "> late finding\n\n" + SENTINEL, author="devcake",
               ts=now - timedelta(minutes=5)),
    ]
    r = _review_run(watermark_id="wm")
    r.state = "finished"
    r.pmo_ref = "main"
    store.save(r)
    # get_activity in deferred path is shallow by default — recheck uses full=True
    run_coro(sweeps.merge_sweep(mgr, m))
    assert "DEVCAKE-REVIEW" in m.labels
    assert "DEVCAKE-MERGE" not in m.labels
    assert mgr.forges.get("main").merges == []
    assert any("freshness-rereview:1" in c for c in fake.comments)


def test_sweep_settle_elapsed_clean_opens_retry(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    m.repo = "main"
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    inst = mgr.forges.instance("main")
    inst.auto_merge = True
    inst.merge_settle_minutes = 10
    inst.merge_retry_window_minutes = 30
    now = datetime.now(timezone.utc)
    fake.activity_entries = [
        _entry("wm", "approve note" + SENTINEL, author="devcake",
               ts=now - timedelta(minutes=40)),
        _entry("s1", f"settling {MERGE_SETTLE_MARKER}\n\n" + SENTINEL,
               author="devcake", ts=now - timedelta(minutes=20)),
    ]
    r = _review_run(watermark_id="wm")
    r.state = "finished"
    r.pmo_ref = "main"
    store.save(r)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert "DEVCAKE-MERGE" in m.labels
    assert any(MERGE_RETRY_MARKER in c for c in fake.comments)
    assert mgr.forges.get("main").merges == []  # retry posted; merge next cycle
