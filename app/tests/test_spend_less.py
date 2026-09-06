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


def test_memo_without_max_age_never_rescans_on_age_alone():
    """A vendor whose `updated_at` moves on every comment needs no safety
    rescan: the changed mission already misses the memo."""
    clock = Clock()
    memo = FeedScanMemo(clock=clock, max_age=None)
    m = issue("s1")
    memo.put("discovery", m, "scan-1", memo.generation(m.pmo_id))
    clock.t += timedelta(hours=6)                        # age alone: still good
    assert memo.get("discovery", m) == "scan-1"
    m.updated_at = now() + timedelta(seconds=1)          # the mission changed
    assert memo.get("discovery", m) is None


def test_memo_for_pmo_reads_the_capability():
    from devcake.domain.orchestrator.feed_memo import MAX_AGE
    from fakes import fake_pmo_capabilities
    tracks = SimpleNamespace(capabilities=lambda: fake_pmo_capabilities(
        updated_at_tracks_comments=True))
    rescans = SimpleNamespace(capabilities=lambda: fake_pmo_capabilities(
        updated_at_tracks_comments=False))
    assert FeedScanMemo.for_pmo(tracks).max_age is None
    assert FeedScanMemo.for_pmo(rescans).max_age == MAX_AGE
    # no self-description at all (a bare fake): the conservative rescan
    assert FeedScanMemo.for_pmo(object()).max_age == MAX_AGE


def test_manager_memo_follows_the_adapter(tmp_path):
    mgr, fake, store = make_mgr(tmp_path, issue("s1"))   # Linear-shaped fake
    assert mgr.feed_memo.max_age is None


class FeedPMO:
    def __init__(self, truncated=False, entries=None):
        self.reads = 0
        self.truncated = truncated
        self.entries = list(entries or [])

    async def get_activity(self, ref, full=False):
        self.reads += 1
        return SimpleNamespace(entries=list(self.entries),
                               truncated=self.truncated)


def _scan_mgr(truncated=False, entries=None):
    return SimpleNamespace(pmo=FeedPMO(truncated, entries),
                           feed_memo=FeedScanMemo(), cycle_stats={})


# ── the feed-changes witness ─────────────────────────────────────────────────

def _delta(*rows, truncated=False):
    from devcake.domain.model import FeedChange, FeedDelta
    changes = [FeedChange(pmo_id=p, entry_id=e, created_at=t, changed_at=t)
               for p, e, t in rows]
    return FeedDelta(changes=changes,
                     newest=max((c.changed_at for c in changes), default=None),
                     truncated=truncated)


def test_reconcile_keeps_a_scan_whose_mission_changed_but_feed_did_not():
    """A label, status, or relation edit moves `updated_at`; the witness
    shows no feed row newer than the scan, so the scan is re-stamped."""
    memo = FeedScanMemo(max_age=None)
    m = issue("s1")
    t0 = now()
    memo.put("discovery", m, "scan", memo.generation(m.pmo_id), feed_until=t0)
    m.updated_at = t0 + timedelta(minutes=1)              # a label edit
    assert memo.get("discovery", m) is None
    assert memo.reconcile([m], _delta()) == 1
    assert memo.get("discovery", m) == "scan"


def test_reconcile_never_refreshes_the_safety_rescan():
    """A vendor that keeps the safety rescan keeps it: the witness cannot
    list what the vendor never reports (a removed comment), so the scan's
    age is not refreshed by a complete witness."""
    clock = Clock()
    memo = FeedScanMemo(clock=clock, max_age=timedelta(minutes=5))
    m = issue("s1")
    memo.put("discovery", m, "scan", memo.generation(m.pmo_id), feed_until=now())
    clock.t += timedelta(minutes=6)
    memo.reconcile([m], _delta())
    assert memo.get("discovery", m) is None


def test_witness_floor_stops_an_edit_from_popping_the_scan_every_cycle():
    """An edit (or a removal) carries a change time no entry's creation time
    reaches; the witness reports it again on every overlap re-delivery. The
    change time becomes the mission's floor, so the rescan after the pop
    records a `feed_until` the re-delivery no longer exceeds."""
    memo = FeedScanMemo(max_age=None)
    m = issue("s1")
    t0 = now()
    edit = t0 + timedelta(minutes=1)
    memo.put("discovery", m, "scan", memo.generation(m.pmo_id), feed_until=t0)
    memo.reconcile([m], _delta(("s1", "c1", edit)))       # the edit: popped
    assert memo.get("discovery", m) is None
    floor = memo.witnessed(m.pmo_id)                       # captured before the rescan
    assert floor == edit
    memo.put("discovery", m, "rescan", memo.generation(m.pmo_id),
             feed_until=t0, floor=floor)                    # creation times unchanged
    for _ in range(3):                                     # overlap re-deliveries
        memo.reconcile([m], _delta(("s1", "c1", edit)))
        assert memo.get("discovery", m) == "rescan"
    memo.reconcile([m], _delta(("s1", "c1", edit + timedelta(seconds=1))))
    assert memo.get("discovery", m) is None                # a second edit: once more


def test_reconcile_drops_a_scan_when_the_feed_changed_after_it():
    memo = FeedScanMemo(max_age=None)
    m = issue("s1")
    t0 = now()
    memo.put("discovery", m, "scan", memo.generation(m.pmo_id), feed_until=t0)
    # an overlap re-delivery of a row the scan already folded: harmless
    memo.reconcile([m], _delta(("s1", "c1", t0 - timedelta(seconds=30))))
    assert memo.get("discovery", m) == "scan"
    # a row newer than the scan (a comment, an edit, a removal): stale
    memo.reconcile([m], _delta(("s1", "c2", t0 + timedelta(seconds=1))))
    assert memo.get("discovery", m) is None and len(memo) == 0


def test_reconcile_truncated_witness_drops_everything_and_still_advances():
    memo = FeedScanMemo(max_age=None)
    m1, m2 = issue("s1"), issue("s2")
    t0 = now()
    for m in (m1, m2):
        memo.put("discovery", m, "scan", memo.generation(m.pmo_id),
                 feed_until=t0)
    delta = _delta(("s1", "c9", t0 + timedelta(minutes=1)), truncated=True)
    memo.advance(delta)
    assert memo.reconcile([m1, m2], delta) == 0 and len(memo) == 0
    assert memo.watermark == t0 + timedelta(minutes=1)


def test_reconcile_skips_never_scanned_and_post_own_write_entries():
    memo = FeedScanMemo(max_age=None)
    m = issue("s1")
    t0 = now()
    assert memo.reconcile([m], _delta()) == 0                # nothing memoized
    memo.put("discovery", m, "scan", memo.generation(m.pmo_id), feed_until=t0)
    memo.forget(m.pmo_id)                                    # our own write
    m.updated_at = t0 + timedelta(minutes=1)
    assert memo.reconcile([m], _delta()) == 0
    assert memo.get("discovery", m) is None


def test_watermark_anchors_and_advances_on_vendor_timestamps_only():
    memo = FeedScanMemo(max_age=None)
    t0 = now()
    memo.anchor(t0)
    memo.anchor(t0 + timedelta(hours=1))                     # first anchor wins
    assert memo.watermark == t0
    memo.anchor(t0 + timedelta(hours=1), follow=True)        # nothing memoized: follow
    memo.anchor(t0, follow=True)                             # never backwards
    assert memo.watermark == t0 + timedelta(hours=1)
    memo.watermark = t0
    memo.advance(_delta(("s1", "c1", t0 + timedelta(minutes=2))))
    memo.advance(_delta(("s1", "c0", t0 - timedelta(minutes=2))))   # never back
    memo.advance(_delta())                                   # empty: unchanged
    assert memo.watermark == t0 + timedelta(minutes=2)
    memo.clear()
    assert memo.watermark is None and memo.delta_error is None


def test_scans_record_the_newest_entry_time():
    t = now() - timedelta(minutes=3)
    mgr = _scan_mgr(entries=[SimpleNamespace(ts=t - timedelta(minutes=1), body=""),
                             SimpleNamespace(ts=t, body="")])
    m = issue("s1")
    run_coro(discovery.scan_source(mgr, m))
    assert mgr.feed_memo._entries[("discovery", "s1")].feed_until == t


def _witness_board(tmp_path, *, feed_delta=True):
    """A labelled mission on a manager whose fake PMO answers the witness;
    the first sweep pays the full read and memoizes it."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE", LABEL_DISCOVERY})
    mgr, fake, store = make_mgr(tmp_path, m)
    mgr.instance.discovery_routing = True
    fake.feed_delta = feed_delta
    fake.activity_entries = [_entry("e1", "hello", ts=m.updated_at)]
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.get_activity_calls == 1 and getattr(fake, "feed_delta_calls", 0) == 0
    mgr.cycle_stats = {}
    return m, mgr, fake


def test_sweeps_pay_no_feed_read_for_a_changed_mission_the_witness_left_untouched(tmp_path):
    m, mgr, fake = _witness_board(tmp_path)
    anchored = mgr.feed_memo.watermark
    m.updated_at = m.updated_at + timedelta(minutes=1)     # a label edit
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.feed_delta_calls == 1
    assert fake.feed_delta_since == anchored - sweeps.FEED_DELTA_OVERLAP
    assert fake.get_activity_calls == 1                     # no feed read
    assert mgr.cycle_stats == {"feed_delta_reads": 1, "feed_scan_memo_kept": 1,
                               "feed_scan_memo_hits": 1}


def test_sweeps_read_a_feed_the_witness_reports_changed(tmp_path):
    from devcake.domain.model import FeedChange
    m, mgr, fake = _witness_board(tmp_path)
    later = m.updated_at + timedelta(minutes=1)
    fake.feed_changes = [FeedChange(pmo_id=m.pmo_id, entry_id="e2",
                                    created_at=later, changed_at=later)]
    m.updated_at = later
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.get_activity_calls == 2                     # the feed changed
    assert mgr.feed_memo.watermark == later


def test_sweeps_read_an_edited_feed_once_not_every_cycle(tmp_path):
    """The livelock the witness must not have: an edit is re-delivered on
    every overlap read, and the rescan's entries keep their creation
    times; the floor makes the second and later deliveries a memo hit."""
    from devcake.domain.model import FeedChange
    m, mgr, fake = _witness_board(tmp_path)
    edit = m.updated_at + timedelta(minutes=1)
    fake.feed_changes = [FeedChange(pmo_id=m.pmo_id, entry_id="e1",
                                    created_at=m.updated_at, changed_at=edit)]
    reads = []
    for _ in range(4):                                      # quiet board, same row
        before = fake.get_activity_calls
        run_coro(sweeps.sweeps(mgr, [m]))
        reads.append(fake.get_activity_calls - before)
    assert reads == [1, 0, 0, 0]


def test_sweeps_truncated_witness_rereads_and_advances(tmp_path):
    m, mgr, fake = _witness_board(tmp_path)
    fake.feed_delta_truncated = True
    later = m.updated_at + timedelta(minutes=1)
    from devcake.domain.model import FeedChange
    fake.feed_changes = [FeedChange(pmo_id="someone-else", entry_id="x",
                                    created_at=later, changed_at=later)]
    run_coro(sweeps.sweeps(mgr, [m]))                       # mission unchanged
    assert fake.get_activity_calls == 2                     # dropped all: re-read
    assert mgr.feed_memo.watermark == later                 # and advanced


def test_witness_watermark_follows_the_board_while_nothing_is_memoized(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})   # no label
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.feed_delta = True
    run_coro(sweeps.sweeps(mgr, [m]))
    first = mgr.feed_memo.watermark
    m.updated_at = m.updated_at + timedelta(hours=3)          # an idle stretch
    run_coro(sweeps.sweeps(mgr, [m]))
    assert getattr(fake, "feed_delta_calls", 0) == 0          # nothing to witness
    assert mgr.feed_memo.watermark == m.updated_at > first


def test_sweeps_fall_back_to_per_mission_reads_when_the_witness_is_refused(tmp_path):
    from devcake.ports.pmo import PMOBudgetExceeded
    m, mgr, fake = _witness_board(tmp_path)
    anchored = mgr.feed_memo.watermark
    fake.feed_delta_exc = PMOBudgetExceeded("reserved for critical calls")
    m.updated_at = m.updated_at + timedelta(minutes=1)
    run_coro(sweeps.sweeps(mgr, [m]))                       # nothing escapes
    assert fake.feed_delta_calls == 1 and fake.get_activity_calls == 2
    assert mgr.feed_memo.watermark == anchored and mgr.feed_memo.delta_error is None


def test_sweeps_skip_the_witness_without_the_capability(tmp_path):
    m, mgr, fake = _witness_board(tmp_path, feed_delta=False)
    m.updated_at = m.updated_at + timedelta(minutes=1)
    run_coro(sweeps.sweeps(mgr, [m]))
    assert getattr(fake, "feed_delta_calls", 0) == 0
    assert fake.get_activity_calls == 2                     # today's rule


def test_a_permanent_witness_error_latches_off_until_a_save(tmp_path):
    m, mgr, fake = _witness_board(tmp_path)
    fake.feed_delta_exc = RuntimeError("unknown field")
    m.updated_at = m.updated_at + timedelta(minutes=1)
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.feed_delta_calls == 1 and mgr.feed_memo.delta_error
    m.updated_at = m.updated_at + timedelta(minutes=1)
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.feed_delta_calls == 1                       # latched
    mgr.feed_memo.delta_error = None                         # what a Save does
    fake.feed_delta_exc = None
    m.updated_at = m.updated_at + timedelta(minutes=1)
    run_coro(sweeps.sweeps(mgr, [m]))
    assert fake.feed_delta_calls == 2


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
