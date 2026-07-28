"""BlockerLocator: deployment-wide blocker Mission resolution (hermetic).

The locator is the ONE seam that widens where a `blocked_by` id is looked up
(ADR-0009 amendment): local snapshot → owner map → same-system peer scan
(global-id vendors only) → local adapter fallback → None. Attribution
(`accepted_pmo_refs`) is asserted throughout — it is what keeps
resolve_blocker_work's widened run index safe on colliding-id vendors.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from devcake.domain.blocker_locator import BlockerLocator
from devcake.domain.model import Mission

NOW = datetime.now(timezone.utc)


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _mission(pmo_id, key, status="done", instance=""):
    return Mission(
        pmo_id=pmo_id, pmo_kind="issue", instance=instance, key=key,
        title=key, status=status, labels={"DEVCAKE"}, updated_at=NOW)


class CountingPMO:
    def __init__(self, missions=None, fail=False):
        self.missions = missions or {}
        self.fail = fail
        self.gets: list[str] = []

    async def get(self, ref):
        self.gets.append(ref.pmo_id)
        if self.fail:
            raise RuntimeError("pmo down")
        m = self.missions.get(ref.pmo_id)
        if m is None:
            raise RuntimeError(f"missing {ref.pmo_id}")
        return m


def _mgr(name, system="linear", missions=None, fail=False):
    return SimpleNamespace(
        instance=SimpleNamespace(name=name, system=system),
        instance_name=name,
        pmo=CountingPMO(missions, fail=fail))


def _locator(managers, owner=None):
    owner = owner or {}
    return BlockerLocator(managers, owner.get)


def test_hit_in_snapshot_no_peer_io():
    a = _mission("a", "CS-1", instance="eng")
    eng, cs = _mgr("eng"), _mgr("cs")
    loc = _locator({"cs": cs, "eng": eng})
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={"a": a}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"", "main", "eng"})
    assert cs.pmo.gets == [] and eng.pmo.gets == []


def test_owner_map_resolves_via_peer_adapter():
    """A's API key reads A — the local (eng) adapter is never asked."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"cs"})
    assert eng.pmo.gets == []


def test_owner_released_peer_scan_is_primary_path():
    """release_stale_ownership frees done+aged-out entries — the flagship
    scenario arrives with an EMPTY owner map and must still resolve."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng})       # no owner entry at all
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"cs"})


def test_owner_points_at_missing_manager_falls_through():
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "gone"})
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"cs"})


def test_different_system_peer_never_called():
    eng = _mgr("eng", system="linear")
    board = _mgr("board", system="gitea_issues", missions={
        "a": _mission("a", "#3", instance="board")})
    loc = _locator({"eng": eng, "board": board})
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert r is None
    assert board.pmo.gets == []


def test_colliding_id_system_never_scans_peers():
    """gitea_issues pmo_ids are per-repo issue NUMBERS — a same-system peer
    holding '3' is a DIFFERENT mission. Hard-refused, not best-effort."""
    g1 = _mgr("g1", system="gitea_issues")
    g2 = _mgr("g2", system="gitea_issues", missions={
        "3": _mission("3", "#3", instance="g2")})
    loc = _locator({"g1": g1, "g2": g2})
    r = run_coro(loc.resolve("3", local_mgr=g1, by_id={}, memo={}))
    assert r is None
    assert g2.pmo.gets == []


def test_colliding_id_system_local_fallback_keeps_local_attribution():
    """A gitea instance's own aged-out blocker still resolves through its own
    adapter, with attribution unchanged from today's `_run_is_ours` set."""
    a = _mission("3", "#3", instance="g1")
    g1 = _mgr("g1", system="gitea_issues", missions={"3": a})
    g2 = _mgr("g2", system="gitea_issues")
    loc = _locator({"g1": g1, "g2": g2})
    r = run_coro(loc.resolve("3", local_mgr=g1, by_id={}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"", "main", "g1"})


def test_local_fallback_on_foreign_id_accepts_all_same_system():
    """Same-workspace Linear keys can resolve a peer's id through the LOCAL
    adapter (true owner unknown, Mission stamped locally — adapters are
    instance-bound). Safe to accept any same-system instance's runs only
    because Linear ids cannot collide."""
    a = _mission("a", "CS-1", instance="eng")     # local adapter's stamp
    eng = _mgr("eng", missions={"a": a})
    cs = _mgr("cs")                               # peer cannot resolve it
    other = _mgr("board", system="gitea_issues")
    loc = _locator({"cs": cs, "eng": eng, "board": other})
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"", "main", "eng", "cs"})


def test_all_miss_returns_none_fail_safe():
    eng, cs = _mgr("eng", fail=True), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    memo = {}
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo=memo))
    assert r is None
    assert memo["a"] is None                      # memoized as unreadable


def test_memo_prevents_second_get():
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng})
    memo = {}
    r1 = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo=memo))
    gets_after_first = len(cs.pmo.gets) + len(eng.pmo.gets)
    r2 = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo=memo))
    assert r2 is r1
    assert len(cs.pmo.gets) + len(eng.pmo.gets) == gets_after_first


def test_memo_negative_result_not_retried():
    eng, cs = _mgr("eng", fail=True), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng})
    memo = {}
    run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo=memo))
    gets_after_first = len(cs.pmo.gets) + len(eng.pmo.gets)
    r = run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo=memo))
    assert r is None
    assert len(cs.pmo.gets) + len(eng.pmo.gets) == gets_after_first


def test_owner_map_peer_tried_once_not_twice():
    """When the owner-map peer fails, the scan must not re-ask the same
    peer — one get per manager per resolve."""
    eng, cs = _mgr("eng"), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    run_coro(loc.resolve("a", local_mgr=eng, by_id={}, memo={}))
    assert cs.pmo.gets == ["a"]
