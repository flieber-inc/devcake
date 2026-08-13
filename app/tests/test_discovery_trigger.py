"""ADR-0033 routing trigger: the StewardService discovery lane (per-instance
single-flight, event kick + poll-segment drive), the per-PMO toggle gate,
and the label-gated sweep (re-seed / self-heal / unroutable terminal)."""
import asyncio

from devcake.config import AppConfig, DevType
from devcake.domain.model import Activity, ActivityEntry
from devcake.domain.orchestrator import discovery
from devcake.domain.orchestrator.markers import discovery_marker
from devcake.domain.steward_service import StewardService

from test_steward import NOW, RoutePMO, _ae, _src_run, m, make_mgr


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _svc_setup(tmp_path, *, src_feed_bodies=(), routing=True):
    src = m("src", "T-S", status="in_progress")
    other = m("oth", "T-O")
    pmo = RoutePMO([src, other])
    pmo.feeds["src"] = Activity(
        mission=src,
        entries=[_ae(b) for b in (src_feed_bodies
                                  or (discovery_marker(2, 1),))],
        truncated=False)
    mgr = make_mgr(tmp_path, pmo)
    mgr.instance.discovery_routing = routing
    mgr.steward_repo = lambda: "home"
    _src_run(mgr.runs.store, mgr, seq=2, n_entries=1)
    dt = DevType(name="steward", harness_template="claude-code")
    svc = StewardService(AppConfig(), {"steward": dt}, mgr)
    calls = []

    async def fake_dispatch(dt_, fam, pending):
        calls.append((sorted(fam.by_id), dict(pending)))
        return object()   # a dispatched run
    mgr.dispatch_steward_discovery = fake_dispatch
    return pmo, mgr, svc, calls, [src, other]


def test_dispatches_one_family_and_clears_served(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert len(calls) == 1
    fam_ids, pending = calls[0]
    assert "src" in fam_ids
    assert pending == {"src": [(2, 1)]}
    assert mgr._discoveries_pending == set()


def test_toggle_off_never_dispatches_or_reads_feeds(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path, routing=False)
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == [] and pmo.activity_calls == []
    assert mgr._discoveries_pending == {"src"}     # retained for toggle-on


def test_active_steward_defers_and_retains_pending(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    svc.active = lambda: True                      # one-STEWARD slot taken
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == []
    assert mgr._discoveries_pending == {"src"}     # next cycle re-drives


def test_receipted_source_is_discarded_without_dispatch(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path, src_feed_bodies=(
        discovery_marker(2, 1),
        "`devcake:discovery-routed:v1 step=2 to=T-O`"))
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == []
    assert mgr._discoveries_pending == set()


def test_off_snapshot_pending_ids_are_dropped(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("ghost")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == [] and mgr._discoveries_pending == set()


# ── the label-gated sweep arm ────────────────────────────────────────────────

def test_sweep_reseeds_pending_from_the_board(tmp_path):
    pmo, mgr, svc, calls, (src, other) = _svc_setup(tmp_path)
    src.labels = src.labels | {"DEVCAKE-DISCOVERY"}
    run_coro(discovery.discovery_sweep(mgr, src))
    assert mgr._discoveries_pending == {"src"}
    # unlabeled missions never get a feed read
    n_before = len(pmo.activity_calls)
    run_coro(discovery.discovery_sweep(mgr, other))
    assert len(pmo.activity_calls) == n_before


def test_sweep_self_heals_receipted_label(tmp_path):
    pmo, mgr, svc, calls, (src, _o) = _svc_setup(tmp_path, src_feed_bodies=(
        discovery_marker(2, 1),
        "`devcake:discovery-routed:v1 step=2 to=T-O`"))
    src.labels = src.labels | {"DEVCAKE-DISCOVERY"}
    mgr._discoveries_pending.add("src")
    run_coro(discovery.discovery_sweep(mgr, src))
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps
    assert mgr._discoveries_pending == set()


def test_sweep_terminates_unroutable_batches(tmp_path):
    # run record cleared (clear-runs): verbatim transport impossible — the
    # sweep posts the sentinel'd disposition (`to=-`) and drops the gate
    pmo, mgr, svc, calls, (src, _o) = _svc_setup(tmp_path, src_feed_bodies=(
        discovery_marker(7, 1),))                  # no run for step 7
    src.labels = src.labels | {"DEVCAKE-DISCOVERY"}
    run_coro(discovery.discovery_sweep(mgr, src))
    src_posts = [md for pid, md in pmo.comments if pid == "src"]
    assert any("`devcake:discovery-routed:v1 step=7 to=-`" in md
               and "Unroutable" in md for md in src_posts)
    assert ("src", {"DEVCAKE-DISCOVERY"}, set()) in pmo.swaps
    assert mgr._discoveries_pending == set()


def test_sweep_toggle_off_leaves_board_untouched(tmp_path):
    pmo, mgr, svc, calls, (src, _o) = _svc_setup(tmp_path, routing=False)
    src.labels = src.labels | {"DEVCAKE-DISCOVERY"}
    run_coro(discovery.discovery_sweep(mgr, src))
    assert pmo.activity_calls == [] and pmo.swaps == []
    assert mgr._discoveries_pending == set()


def test_harvest_notify_seam_is_best_effort(tmp_path):
    # the composition root injects kick_discovery; harvest calls it and a
    # raising callable never touches the close
    from test_transitions import make_mgr as t_make_mgr, mission
    mm = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr2, fake, store = t_make_mgr(tmp_path / "n", mm)
    kicked = []
    mgr2.discovery_notify = lambda: kicked.append(1)
    from devcake.domain.run import Run
    r = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
            mission_pmo_id="p1", mission_type="EXECUTE",
            dev_type="senior-dev", seq=1,
            stage_label_at_dispatch="DEVCAKE-EXECUTE")
    store.save(r)
    entry = {"finding": "f", "evidence": "e", "scope": "s"}
    n = run_coro(discovery.harvest(mgr2, r, {"discoveries": [entry]}))
    assert n == 1 and kicked == [1]

    def _boom():
        raise RuntimeError("kick failed")
    mgr2.discovery_notify = _boom
    r2 = Run(run_id="T-1-2-EXECUTE-AAAAAA", mission_key="T-1",
             mission_pmo_id="p1", mission_type="EXECUTE",
             dev_type="senior-dev", seq=2,
             stage_label_at_dispatch="DEVCAKE-EXECUTE")
    store.save(r2)
    n = run_coro(discovery.harvest(mgr2, r2, {"discoveries": [entry]}))
    assert n == 1                                  # close never wedged
