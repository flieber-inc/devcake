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

    async def fake_dispatch(dt_, fam, pending, **kw):
        calls.append((sorted(fam.by_id), dict(pending), kw))
        return object()   # a dispatched run
    mgr.dispatch_steward_discovery = fake_dispatch
    return pmo, mgr, svc, calls, [src, other]


def test_dispatches_one_family_and_clears_served(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert len(calls) == 1
    fam_ids, pending, _kw = calls[0]
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


def test_truncated_source_is_discarded_without_dispatch(tmp_path):
    # the ceiling case belongs to the sweep (raise to humans + retire);
    # holding the id here would re-fetch a full feed every cycle for nothing
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    pmo.feeds["src"].truncated = True
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


def test_intake_pause_blocks_discovery_dispatch(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    svc.config.intake_paused = True
    mgr.config.intake_paused = True
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == []
    assert mgr._discoveries_pending == {"src"}


def test_discovery_and_relations_share_one_lock(tmp_path):
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    mgr._discoveries_pending.add("src")
    svc.config.steward.enabled = True
    svc._last_at = __import__("time").monotonic() - 10**6
    rel = []
    gate = asyncio.Event()

    async def slow_disc(dt_, fam, pending, **kw):
        calls.append("d")
        await gate.wait()
        return object()

    async def rel_dispatch(dt, missions_, **kw):
        rel.append("r")
        return object()

    mgr.dispatch_steward_discovery = slow_disc
    mgr.dispatch_steward = rel_dispatch

    async def both():
        t1 = asyncio.create_task(svc.maybe_dispatch_discovery(missions))
        await asyncio.sleep(0)
        t2 = asyncio.create_task(svc.maybe_dispatch(missions))
        await asyncio.sleep(0)
        assert rel == []          # still blocked on the shared lock
        gate.set()
        await t1
        await t2

    run_coro(both())
    assert "d" in calls


def test_discovery_forwards_context_stale_and_omit(tmp_path):
    """PLAN_MEMORY §3.5 / ADR-0033 gate honesty: the discovery lane shares
    `_context_gate` with relations and Run-now. Stale/omit sets must ride
    into dispatch so launch marks memory mounts and drops omitted cards —
    computing them then discarding them left discovery mounts dishonest
    relative to the gate that just ran."""
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    seen: list[dict] = []

    async def fake_dispatch(dt_, fam, pending, **kw):
        seen.append(kw)
        calls.append(1)
        return object()

    async def gate(dt, repo, extra=()):
        return True, {}, {"mem-notebook"}, {"skillcard"}

    mgr.dispatch_steward_discovery = fake_dispatch
    svc._context_gate = gate
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == [1]
    assert seen[0]["context_stale"] == {"mem-notebook"}
    assert seen[0]["context_omit"] == {"skillcard"}


def test_degraded_skips_discovery_and_retains_pending(tmp_path):
    """Shared degradation (3 consecutive dead STEWARD runs) backs off the
    discovery lane too — pending stays for the sweep/kick after a human
    Run-now success clears the condition. Mirrors relations' degraded_skip."""
    from devcake.domain.run import Run
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    for i, st in enumerate(("failed", "timed_out", "orphaned"), start=1):
        mgr.runs.store.save(Run(
            run_id=f"TEAM-{i}-STEWARD-XXXXXX", mission_key="TEAM",
            mission_type="STEWARD", dev_type="steward", seq=i, state=st,
            error=f"dead-{i}"))
    assert svc.degraded()
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert calls == []
    assert mgr._discoveries_pending == {"src"}


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


def test_gate_filters_mirror_ineligible_family_repos(tmp_path, monkeypatch):
    """The family's work repos legitimately include INTERNAL per-mission
    repos — valid clone extras, never mirrored (ADR-0024 §5). The gate must
    pass only mirror-ELIGIBLE names to ensure_fresh: an internal name would
    401 the unauthenticated mirror fetch and wedge the lane mirror_stale
    forever on zero-repo boards (graduation-smoke prep find, 2026-08-13)."""
    from devcake.domain.orchestrator import family_graph
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    missions[1].blocked_by = ["src"]          # sibling JOINS src's family
    missions[1].repo = "intmission1"          # the sibling's internal repo
    monkeypatch.setattr(family_graph, "blocker_read_credential",
                        lambda mgr_, name: ("internal", object()))
    # Positive control (audit find: the first cut of this test never linked
    # the missions into one family, so the asserts passed on the UNFIXED
    # code). The family wiring must genuinely surface intmission1 — only
    # then does the eligible() filter carry the assertions below.
    fam = family_graph.family_of(missions[0], missions)
    assert "intmission1" in [
        e["repo_ref"] for e in family_graph.family_work_repos(
            mgr, fam, exclude=frozenset({"home"}))]
    asked: list[list[str]] = []

    class Cache:
        def eligible(self, name):
            return name == "home"             # only the steward's card
        async def ensure_fresh(self, names):
            asked.append(sorted(names))
            assert "intmission1" not in names, \
                "internal repo reached the mirror gate"
            return True, {}

    mgr.repo_cache = Cache()
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert len(calls) == 1        # dispatched despite the internal sibling
    assert asked and all("intmission1" not in a for a in asked)


def test_gate_combines_eligible_filter_and_skill_card_union(tmp_path,
                                                            monkeypatch):
    """Audit B2: 147's eligible() filter and the ADR-0016 skill-card union
    edit the SAME gate block — this pins both halves in ONE ensure_fresh
    call: the internal family repo is filtered OUT while the skill card is
    unioned IN un-filtered (deliberate: an unconfigured skill card must
    gate loudly, fail-closed ruling)."""
    from devcake.domain.orchestrator import family_graph
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    missions[1].blocked_by = ["src"]          # sibling JOINS src's family
    missions[1].repo = "intmission1"          # the sibling's internal repo
    monkeypatch.setattr(family_graph, "blocker_read_credential",
                        lambda mgr_, name: ("internal", object()))
    svc.dev_types["steward"].skills = ["skillcard/tdd"]   # external skill
    asked: list[list[str]] = []

    class Cache:
        def eligible(self, name):
            return name == "home"             # only the steward's card
        async def ensure_fresh(self, names):
            asked.append(sorted(names))
            return True, {}

    mgr.repo_cache = Cache()
    mgr._discoveries_pending.add("src")
    run_coro(svc.maybe_dispatch_discovery(missions))
    assert len(calls) == 1
    assert asked and "skillcard" in asked[0]              # union half
    assert all("intmission1" not in a for a in asked)     # filter half


def test_context_gate_backed_skill_card_downgrades_in_open_mode(tmp_path):
    """ADR-0039: ensure_fresh keys a backed source's failure by its BACKING
    card — the steward gate must classify that key as a context card
    (toggle-governed stale/omit), exactly like dispatch's gate, never a
    hard defer."""
    pmo, mgr, svc, calls, missions = _svc_setup(tmp_path)
    svc.config.context_sourcing_strict = False
    dt = svc.dev_types["steward"]
    dt.skills = ["shelf/tdd"]

    class Cache:
        def mirror_name_of(self, name):
            return "work" if name == "shelf" else name

        async def ensure_fresh(self, names):
            assert "shelf" not in names          # resolved before the union
            bad = {n: "fetch: down" for n in names if n == "work"}
            return (not bad), bad

        def has_last_good(self, name):
            return name == "work"

    mgr.repo_cache = Cache()
    ok, why, stale, omit = run_coro(svc._context_gate(dt, "home"))
    assert ok and not why
    assert stale == {"work"} and omit == set()
