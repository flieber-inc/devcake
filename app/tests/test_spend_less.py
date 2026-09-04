"""Spend less per cycle (ADR-0003 amendment, ADR-0033 addendum, ADR-0040):
the cycle's board snapshot serves enumeration reads, labeled feeds are
re-read only when something changed, and Linear's project-label registry
is cached."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from devcake.domain.model import LABEL_DISCOVERY, Mission
from devcake.domain.orchestrator import discovery, sweeps
from devcake.domain.orchestrator.markers import MERGE_RETRY_MARKER
from devcake.domain.orchestrator.board import BoardSnapshot, board_missions
from devcake.domain.orchestrator.feed import feed_written
from devcake.domain.orchestrator.feed_memo import FeedScanMemo
from test_freshness_gate import SENTINEL, _entry
from test_transitions import FakeForge, make_mgr, mission, run_coro


def now():
    return datetime.now(timezone.utc)


def issue(pmo_id, status="backlog", parent=None):
    return Mission(instance="linear", pmo_id=pmo_id, pmo_kind="issue",
                   key=f"T-{pmo_id}", title="t", status=status,
                   labels={"DEVCAKE"}, updated_at=now(), parent_ref=parent)


def tracking_project(tmp_path):
    proj = mission("in_progress", {"DEVCAKE-TRACKING"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    calls = []
    real = fake.children_of

    async def counting(ref):
        calls.append(ref)
        return await real(ref)
    fake.children_of = counting
    return proj, mgr, fake, calls


# ── board snapshot ───────────────────────────────────────────────────────────

def test_snapshot_children_are_issues_with_the_project_as_parent():
    proj = issue("p1")
    proj.pmo_kind = "project"
    snap = BoardSnapshot((proj, issue("c1", parent="p1"), issue("c2", parent="p1"),
                          issue("x", parent="other"), issue("orphan")), 7, now())
    assert [m.pmo_id for m in snap.children_of("p1")] == ["c1", "c2"]
    assert snap.children_of("nope") == []


def test_tracking_sweep_pays_no_read_while_a_snapshot_child_is_open(tmp_path):
    proj, mgr, fake, calls = tracking_project(tmp_path)
    mgr.snapshot = BoardSnapshot(
        (proj, issue("c1", "done", parent=proj.pmo_id),
         issue("c2", "backlog", parent=proj.pmo_id)), 1, now())
    fake.children = [issue("c1", "done"), issue("c2", "backlog")]
    run_coro(sweeps.tracking_sweep(mgr, proj))
    assert calls == []                              # nothing to decide yet
    assert proj.status == "in_progress"
    assert mgr.cycle_stats.get("tracking_children_live", 0) == 0


def test_tracking_sweep_confirms_live_when_the_snapshot_says_all_terminal(tmp_path):
    proj, mgr, fake, calls = tracking_project(tmp_path)
    mgr.snapshot = BoardSnapshot(
        (proj, issue("c1", "done", parent=proj.pmo_id)), 1, now())
    fake.children = [issue("c1", "done")]
    run_coro(sweeps.tracking_sweep(mgr, proj))
    assert len(calls) == 1                          # the live confirm
    assert proj.status == "done" and "DEVCAKE-TRACKING" not in proj.labels
    assert mgr.cycle_stats["tracking_children_live"] == 1


def test_tracking_sweep_never_completes_on_the_snapshot_alone(tmp_path):
    """The snapshot may be a subset (cross-team children): completion is
    decided on the live read, which here still shows an open child."""
    proj, mgr, fake, calls = tracking_project(tmp_path)
    mgr.snapshot = BoardSnapshot(
        (proj, issue("c1", "done", parent=proj.pmo_id)), 1, now())
    fake.children = [issue("c1", "done"), issue("far", "backlog")]
    run_coro(sweeps.tracking_sweep(mgr, proj))
    assert len(calls) == 1 and proj.status == "in_progress"


def test_tracking_sweep_reads_live_when_the_snapshot_knows_no_child(tmp_path):
    proj, mgr, fake, calls = tracking_project(tmp_path)
    mgr.snapshot = BoardSnapshot((proj,), 1, now())   # e.g. no parent_ref vendor
    fake.children = [issue("c1", "backlog")]
    run_coro(sweeps.tracking_sweep(mgr, proj))
    assert len(calls) == 1 and proj.status == "in_progress"


def test_board_missions_serves_a_fresh_snapshot_and_refetches_a_stale_one():
    reads = []

    class PMO:
        async def list_all(self, team):
            reads.append(team)
            return [issue("live")]
    mgr = SimpleNamespace(pmo=PMO(), instance=SimpleNamespace(team_key="T"),
                          cycle_stats={}, snapshot=None)
    mgr.snapshot = BoardSnapshot((issue("snap"),), 3, now())
    got = run_coro(board_missions(mgr, max_age=timedelta(seconds=30)))
    assert [m.pmo_id for m in got] == ["snap"] and reads == []
    mgr.snapshot = BoardSnapshot((issue("old"),), 2, now() - timedelta(minutes=5))
    got = run_coro(board_missions(mgr, max_age=timedelta(seconds=30)))
    assert [m.pmo_id for m in got] == ["live"] and reads == ["T"]
    assert mgr.cycle_stats == {"snapshot_hits": 1, "snapshot_misses": 1}


# ── feed-scan memo ───────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.t = now()

    def __call__(self):
        return self.t


def test_memo_hits_while_nothing_changed_and_misses_on_every_signal():
    clock = Clock()
    memo = FeedScanMemo(clock=clock, max_age=timedelta(minutes=5))
    m = issue("s1")
    gen = memo.generation(m.pmo_id)
    memo.put("discovery", m, "scan-1", gen)
    assert memo.get("discovery", m) == "scan-1"
    assert memo.get("merge", m) is None                 # per kind
    m.updated_at = now() + timedelta(seconds=1)          # the mission changed
    assert memo.get("discovery", m) is None
    memo.put("discovery", m, "scan-2", memo.generation(m.pmo_id))
    memo.forget(m.pmo_id)                                # we wrote to the feed
    assert memo.get("discovery", m) is None
    memo.put("discovery", m, "scan-3", memo.generation(m.pmo_id))
    clock.t += timedelta(minutes=6)                      # safety rescan
    assert memo.get("discovery", m) is None


def test_memo_discards_a_scan_that_started_before_our_own_write():
    memo = FeedScanMemo()
    m = issue("s1")
    gen = memo.generation(m.pmo_id)
    memo.forget(m.pmo_id)                                # write landed mid-scan
    memo.put("discovery", m, "stale", gen)
    assert memo.get("discovery", m) is None and len(memo) == 0


class FeedPMO:
    def __init__(self, truncated=False):
        self.reads = 0
        self.truncated = truncated

    async def get_activity(self, ref, full=False):
        self.reads += 1
        return SimpleNamespace(entries=[], truncated=self.truncated)


def _scan_mgr(truncated=False):
    return SimpleNamespace(pmo=FeedPMO(truncated), feed_memo=FeedScanMemo(),
                           cycle_stats={})


def test_scan_source_reuses_an_untruncated_scan_within_the_cycle_window():
    mgr = _scan_mgr()
    m = issue("s1")
    run_coro(discovery.scan_source(mgr, m))
    run_coro(discovery.scan_source(mgr, m))
    assert mgr.pmo.reads == 1
    assert mgr.cycle_stats == {"feed_scan_reads": 1, "feed_scan_memo_hits": 1}


def test_scan_source_never_memoizes_a_truncated_scan():
    mgr = _scan_mgr(truncated=True)
    m = issue("s1")
    run_coro(discovery.scan_source(mgr, m))
    run_coro(discovery.scan_source(mgr, m))
    assert mgr.pmo.reads == 2


def test_scan_source_memo_false_always_reads_live():
    mgr = _scan_mgr()
    m = issue("s1")
    run_coro(discovery.scan_source(mgr, m))
    run_coro(discovery.scan_source(mgr, m, memo=False))
    assert mgr.pmo.reads == 2


def test_our_own_feed_write_invalidates_the_scan():
    mgr = _scan_mgr()
    m = issue("s1")
    run_coro(discovery.scan_source(mgr, m))
    feed_written(mgr, m.pmo_id)
    run_coro(discovery.scan_source(mgr, m))
    assert mgr.pmo.reads == 2


def test_feed_chokepoint_invalidates_the_memo(tmp_path):
    """Every DevCake-authored comment passes through `_feed`, which forgets
    the mission's memoized scans."""
    m = mission()
    mgr, fake, _store = make_mgr(tmp_path, m)
    mgr.feed_memo.put("discovery", m, "scan", mgr.feed_memo.generation(m.pmo_id))
    run_coro(mgr._feed(m.pmo_id, "issue", "hello"))
    assert mgr.feed_memo.get("discovery", m) is None


# ── Linear project-label registry cache ──────────────────────────────────────

def test_project_label_registry_is_cached_and_invalidated():
    from devcake.adapters.linear.adapter import LinearAdapter
    walks = []

    def handler(req):
        body = req.read().decode()
        if "projectLabels(" in body:
            walks.append(1)
            return httpx.Response(200, json={"data": {"projectLabels": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": [{"id": "L1", "name": "DEVCAKE"}]}}})
        return httpx.Response(200, json={"data": {}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(handler))

    def run(c):
        return asyncio.new_event_loop().run_until_complete(c)
    assert run(pmo._all_project_labels()) == {"DEVCAKE": "L1"}
    assert run(pmo._all_project_labels()) == {"DEVCAKE": "L1"}
    assert len(walks) == 1                                  # cached
    run(pmo._all_project_labels(force=True))
    assert len(walks) == 2                                  # forced re-walk
    pmo._invalidate_team_cache()
    run(pmo._all_project_labels())
    assert len(walks) == 3                                  # invalidated


# ── review round: writes confirm live, failed posts invalidate, eviction ──────

def _sweep_mgr(memo_state, live_state, monkeypatch):
    calls = []

    async def fake_scan(mgr, m, *, memo=True):
        calls.append(memo)
        return memo_state if memo else live_state
    monkeypatch.setattr(discovery, "scan_source", fake_scan)
    swaps = []

    class PMO:
        async def swap_labels(self, ref, remove, add):
            swaps.append(set(remove))
    mgr = SimpleNamespace(
        instance=SimpleNamespace(discovery_routing=True), pmo=PMO(),
        _discoveries_pending=set(), feed_memo=FeedScanMemo(), cycle_stats={},
        runs=SimpleNamespace(store=SimpleNamespace(all=lambda: [])),
        _run_is_ours=lambda r: True, _audit=lambda *a, **k: None)
    return mgr, calls, swaps


def test_discovery_sweep_confirms_live_before_dropping_the_label(monkeypatch):
    """A memoized scan may say 'fully receipted'; the label is dropped only
    after a live scan agrees — here the live scan is truncated, so nothing
    is written and the next sweep takes the fresh path."""
    memo_state = discovery.SourceState(posted=[(1, 2)], receipted={(1, "x")})
    live_state = discovery.SourceState(posted=[(1, 2)], receipted=set(), truncated=True)
    mgr, calls, swaps = _sweep_mgr(memo_state, live_state, monkeypatch)
    m = issue("s1")
    m.labels = {"DEVCAKE", LABEL_DISCOVERY}
    run_coro(discovery.discovery_sweep(mgr, m))
    assert calls == [True, False] and swaps == []


def test_discovery_sweep_writes_when_the_live_scan_agrees(monkeypatch):
    state = discovery.SourceState(posted=[(1, 2)], receipted={(1, "x")})
    mgr, calls, swaps = _sweep_mgr(state, state, monkeypatch)
    m = issue("s1")
    m.labels = {"DEVCAKE", LABEL_DISCOVERY}
    run_coro(discovery.discovery_sweep(mgr, m))
    assert calls == [True, False] and swaps == [{LABEL_DISCOVERY}]


def test_discovery_sweep_reads_once_while_batches_are_pending_in_flight(monkeypatch):
    """No write imminent (a pending batch whose run is in flight): the
    memoized scan is enough — one read, no live confirm."""
    state = discovery.SourceState(posted=[(1, 2)], receipted=set())
    mgr, calls, swaps = _sweep_mgr(state, state, monkeypatch)
    run = SimpleNamespace(mission_pmo_id="s1", seq=1, mission_type="EXECUTE",
                          state="running", result=None)
    mgr.runs = SimpleNamespace(store=SimpleNamespace(all=lambda: [run]))
    monkeypatch.setattr(discovery, "HARVEST_TYPES", {"EXECUTE"})
    m = issue("s1")
    m.labels = {"DEVCAKE", LABEL_DISCOVERY}
    run_coro(discovery.discovery_sweep(mgr, m))
    assert calls == [True] and swaps == [] and "s1" in mgr._discoveries_pending


def test_a_failed_post_still_invalidates_the_memo(tmp_path):
    m = mission()
    mgr, fake, _store = make_mgr(tmp_path, m)
    mgr.feed_memo.put("discovery", m, "scan", mgr.feed_memo.generation(m.pmo_id))

    async def boom(ref, markdown):
        raise RuntimeError("read timeout after the vendor applied it")
    fake.post_feed = boom
    with pytest.raises(RuntimeError):
        run_coro(mgr._feed(m.pmo_id, "issue", "hello"))
    assert mgr.feed_memo.get("discovery", m) is None


def test_memo_retain_evicts_missions_that_left_the_board():
    memo = FeedScanMemo()
    a, b = issue("a"), issue("b")
    memo.put("discovery", a, "sa", memo.generation("a"))
    memo.put("discovery", b, "sb", memo.generation("b"))
    gen_b = memo.generation("b")
    memo.forget("b")
    memo.retain({"a"})
    assert memo.get("discovery", a) == "sa" and len(memo) == 1
    assert memo.generation("b") == gen_b + 1       # generations survive eviction
    memo.put("discovery", b, "stale", gen_b)         # a scan from before the write
    assert memo.get("discovery", b) is None


def test_merge_driver_memoizes_the_stamps_until_our_own_write(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    m.repo = "main"
    mgr, fake, _ = make_mgr(tmp_path, m, forge=FakeForge(mergeable_result=None))
    inst = mgr.forges.instance("main")
    inst.auto_merge = True
    inst.merge_retry_window_minutes = 30
    fake.activity_entries = [_entry(
        "r1", f"retrying {MERGE_RETRY_MARKER}\n\n" + SENTINEL,
        author="devcake", ts=now() - timedelta(minutes=5))]
    run_coro(sweeps.merge_sweep(mgr, m))
    run_coro(sweeps.merge_sweep(mgr, m))
    assert fake.get_activity_calls == 1
    assert mgr.cycle_stats["feed_scan_memo_hits"] == 1
    run_coro(mgr._feed(m.pmo_id, "issue", "our own comment"))
    run_coro(sweeps.merge_sweep(mgr, m))
    assert fake.get_activity_calls == 2


def test_ensure_labels_heals_a_project_label_deleted_inside_the_cache_window():
    from devcake.adapters.linear.adapter import LinearAdapter
    state = {"present": True}
    creates = []

    def handler(req):
        body = req.read().decode()
        if "projectLabels(" in body:
            nodes = [{"id": "L1", "name": "DEVCAKE"}] if state["present"] else []
            return httpx.Response(200, json={"data": {"projectLabels": {
                "pageInfo": {"hasNextPage": False, "endCursor": None},
                "nodes": nodes}}})
        if "projectLabelCreate" in body:
            creates.append(body)
            return httpx.Response(200, json={"data": {"projectLabelCreate": {"success": True}}})
        if "teams(" in body:
            return httpx.Response(200, json={"data": {"viewer": {"id": "u"}, "teams": {
                "nodes": [{"id": "t1", "key": "T", "states": {"nodes": []}}]}}})
        if "labels(first: 100" in body:
            return httpx.Response(200, json={"data": {"team": {"labels": {
                "nodes": [{"id": "I1", "name": "DEVCAKE"}],
                "pageInfo": {"hasNextPage": False, "endCursor": None}}}}})
        return httpx.Response(200, json={"data": {}})
    pmo = LinearAdapter("k", transport=httpx.MockTransport(handler))

    def run(c):
        return asyncio.new_event_loop().run_until_complete(c)
    run(pmo._all_project_labels())                  # warm the cache
    state["present"] = False                        # deleted on the vendor
    run(pmo.ensure_labels("T", {"DEVCAKE"}))
    assert len(creates) == 1                        # healed, cache not trusted
