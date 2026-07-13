"""ADR-0007: the blocked-by scheduler gate — a mission with an open blocker is
never dispatched; the gate reads live PMO state only (no local bookkeeping)."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.config import AppConfig, DevType
from devcake.domain.orchestrator import MissionManager
from devcake.domain.model import Mission
from devcake.adapters.files.run_store import RunStore

NOW = datetime.now(timezone.utc)


def m(pmo_id, key, status="backlog", labels=("DEVCAKE",), blocked_by=()):
    return Mission(pmo_id=pmo_id, pmo_kind="issue", key=key, title=key,
                   status=status, labels=set(labels), updated_at=NOW,
                   blocked_by=list(blocked_by))


class DepPMO:
    def __init__(self, by_id=None, fail_ids=()):
        self.by_id = by_id or {}
        self.fail_ids = set(fail_ids)
        self.live_fetches = []

    async def get_mission(self, pmo_id):
        self.live_fetches.append(pmo_id)
        if pmo_id in self.fail_ids:
            raise RuntimeError("pmo unreachable")
        return self.by_id[pmo_id]


def make_mgr(tmp_path, pmo):
    mgr = MissionManager.__new__(MissionManager)
    mgr.config = AppConfig()
    mgr.dev_types = {
        "senior-dev": DevType(name="senior-dev", harness_template="claude-code"),
        "main-dev": DevType(name="main-dev", harness_template="grok-build"),
    }
    mgr.pmo = pmo
    mgr.runs = SimpleNamespace(store=RunStore(tmp_path / "runs"))
    mgr._grace, mgr._grace_next, mgr.breakers = set(), set(), {}
    mgr.blocked_reasons = {}
    mgr._audit = lambda *a, **k: None
    dispatched = []

    async def fake_dispatch(mission, mtype, dev_type):
        dispatched.append(mission.key)
        return None

    mgr.dispatch = fake_dispatch
    return mgr, dispatched


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_open_blocker_skips_candidate(tmp_path):
    blocker = m("b1", "T-1")                       # backlog = open
    blocked = m("b2", "T-2", blocked_by=["b1"])
    mgr, dispatched = make_mgr(tmp_path, DepPMO())
    run_coro(mgr.schedule([blocker, blocked]))
    assert "T-2" not in dispatched
    assert "T-1" in dispatched                     # the blocker itself runs
    assert "T-1" in mgr.blocked_reasons["b2"]


def test_done_or_canceled_blocker_unblocks(tmp_path):
    for terminal in ("done", "canceled"):
        blocker = m("b1", "T-1", status=terminal)
        blocked = m("b2", "T-2", blocked_by=["b1"])
        mgr, dispatched = make_mgr(tmp_path, DepPMO())
        run_coro(mgr.schedule([blocker, blocked]))
        assert dispatched == ["T-2"]
        assert mgr.blocked_reasons == {}


def test_failed_blocker_still_blocks_and_is_surfaced(tmp_path):
    blocker = m("b1", "T-1", labels={"DEVCAKE", "DEVCAKE-FAILED"})
    blocked = m("b2", "T-2", blocked_by=["b1"])
    mgr, dispatched = make_mgr(tmp_path, DepPMO())
    run_coro(mgr.schedule([blocker, blocked]))
    assert dispatched == []                        # FAILED blocker isn't scheduled either
    assert "DEVCAKE-FAILED" in mgr.blocked_reasons["b2"]   # deadlock made visible


def test_unreadable_off_snapshot_blocker_fails_safe(tmp_path):
    blocked = m("b2", "T-2", blocked_by=["ghost"])
    mgr, dispatched = make_mgr(tmp_path, DepPMO(fail_ids={"ghost"}))
    run_coro(mgr.schedule([blocked]))
    assert dispatched == []
    assert "unreadable" in mgr.blocked_reasons["b2"]


def test_off_snapshot_blocker_fetched_once_per_cycle(tmp_path):
    ghost = m("g1", "T-0")                         # open, lives outside the snapshot
    pmo = DepPMO(by_id={"g1": ghost})
    a = m("b2", "T-2", blocked_by=["g1"])
    b = m("b3", "T-3", blocked_by=["g1"])
    mgr, dispatched = make_mgr(tmp_path, pmo)
    run_coro(mgr.schedule([a, b]))
    assert dispatched == []
    assert pmo.live_fetches == ["g1"]              # memoized within the cycle


def test_find_cycles():
    from devcake.domain.model import find_cycles
    assert find_cycles({"a": {"b"}, "b": set()}) == []
    assert find_cycles({"a": {"a"}}) == [["a"]]                 # self-loop
    two = find_cycles({"a": {"b"}, "b": {"a"}})
    assert len(two) == 1 and set(two[0]) == {"a", "b"}
    three = find_cycles({"a": {"b"}, "b": {"c"}, "c": {"a"}, "d": {"a"}})
    assert len(three) == 1 and set(three[0]) == {"a", "b", "c"}
    # edge pointing outside the snapshot cannot close a cycle
    assert find_cycles({"a": {"ghost"}}) == []


def test_gate_map_names_cycles_as_unsatisfiable(tmp_path):
    a = m("ia", "T-1", blocked_by=["ib"])
    b = m("ib", "T-2", blocked_by=["ia"])
    mgr, dispatched = make_mgr(tmp_path, DepPMO())
    gate = run_coro(mgr.gate_map([a, b]))
    assert "dependency cycle" in gate["ia"] and "will never unblock" in gate["ia"]
    assert "T-1" in gate["ib"] and "T-2" in gate["ib"]          # the loop is named
    assert mgr.cycles and set(mgr.cycles[0]) == {"T-1", "T-2"}  # → /health banner
    run_coro(mgr.schedule([a, b], gate))
    assert dispatched == []


def test_gate_map_is_always_fresh(tmp_path):
    # the gate is a poll artifact: while intake is paused the loop still calls
    # gate_map, so a relation deleted in Linear clears the reason next cycle
    blocker = m("b1", "T-1")
    blocked = m("b2", "T-2", blocked_by=["b1"])
    mgr, _ = make_mgr(tmp_path, DepPMO())
    gate = run_coro(mgr.gate_map([blocker, blocked]))
    assert "b2" in gate
    blocker_done = m("b1", "T-1", status="done")
    gate2 = run_coro(mgr.gate_map([blocker_done, blocked]))
    assert gate2 == {} and mgr.blocked_reasons == {}


def test_dispatch_recheck_aborts_on_live_blocker(tmp_path):
    from devcake.domain.model import MissionType
    live = m("b2", "T-2", blocked_by=["b1"])
    pmo = DepPMO(by_id={"b2": live, "b1": m("b1", "T-1")})   # blocker still open
    mgr, _ = make_mgr(tmp_path, pmo)
    del mgr.dispatch                                # use the real dispatch
    dt = mgr.dev_types["senior-dev"]
    run = run_coro(mgr.dispatch(live, MissionType.ONBOARD, dt))
    assert run is None                              # aborted before any side effect
