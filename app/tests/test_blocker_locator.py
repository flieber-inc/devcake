"""BlockerLocator: deployment-wide blocker Mission resolution (hermetic).

The locator is the ONE seam that widens where a `blocked_by` id is looked up
(ADR-0009 amendment). Callers resolve snapshot hits against their own by_id
first; for off-snapshot ids: owner map → LOCAL adapter → same-system peer
scan (global-id vendors only) → None, in one batched walk per set
(`resolve_many`; adapters with `batch_get` answer in one read). Attribution
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
    def __init__(self, missions=None, fail=False, global_ids=True,
                 raise_caps=False):
        self.missions = missions or {}
        self.fail = fail
        self.global_ids = global_ids
        self.raise_caps = raise_caps
        self.gets: list[str] = []

    def capabilities(self):
        if self.raise_caps:
            raise RuntimeError("capabilities probe failed")
        from fakes import fake_pmo_capabilities
        return fake_pmo_capabilities(global_ids=self.global_ids)

    async def get(self, ref):
        self.gets.append(ref.pmo_id)
        if self.fail:
            raise RuntimeError("pmo down")
        m = self.missions.get(ref.pmo_id)
        if m is None:
            raise RuntimeError(f"missing {ref.pmo_id}")
        return m


def _mgr(name, system="linear", missions=None, fail=False, *, global_ids=None):
    # Peer allow/deny rides PMOCapabilities.global_ids (F10), not the system
    # name. Default True for the linear-shaped fixture only so existing peer
    # tests stay short; colliding-id fixtures pass global_ids=False explicitly.
    if global_ids is None:
        global_ids = True
    return SimpleNamespace(
        instance=SimpleNamespace(name=name, system=system),
        instance_name=name,
        pmo=CountingPMO(missions, fail=fail, global_ids=global_ids))


def _locator(managers, owner=None):
    owner = owner or {}
    return BlockerLocator(managers, owner.get)


# Peer-resolved attribution always carries LEGACY_PMO_REFS so pre-schema-v3
# runs (pmo_ref ""/"main") on that peer's history still mount (mirrors local).
from devcake.domain.run import LEGACY_PMO_REFS

_PEER_CS_REFS = LEGACY_PMO_REFS | frozenset({"cs"})


def test_owner_map_resolves_via_peer_adapter():
    """A's API key reads A — the local (eng) adapter is never asked."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == _PEER_CS_REFS
    assert eng.pmo.gets == []


def test_owner_released_peer_scan_is_primary_path():
    """release_stale_ownership frees done+aged-out entries — the flagship
    scenario arrives with an EMPTY owner map and must still resolve."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng})       # no owner entry at all
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == _PEER_CS_REFS


def test_owner_points_at_missing_manager_falls_through():
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "gone"})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == _PEER_CS_REFS


def test_different_system_peer_never_called():
    eng = _mgr("eng", system="linear")
    board = _mgr("board", system="gitea_issues", missions={
        "a": _mission("a", "#3", instance="board")})
    loc = _locator({"eng": eng, "board": board})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r is None
    assert board.pmo.gets == []


def test_colliding_id_system_never_scans_peers():
    """gitea_issues pmo_ids are per-repo issue NUMBERS — a same-system peer
    holding '3' is a DIFFERENT mission. Hard-refused, not best-effort."""
    g1 = _mgr("g1", system="gitea_issues", global_ids=False)
    g2 = _mgr("g2", system="gitea_issues", global_ids=False, missions={
        "3": _mission("3", "#3", instance="g2")})
    loc = _locator({"g1": g1, "g2": g2})
    r = run_coro(loc.resolve("3", local_mgr=g1, memo={}))
    assert r is None
    assert g2.pmo.gets == []


def test_colliding_id_system_local_fallback_keeps_local_attribution():
    """A gitea instance's own aged-out blocker still resolves through its own
    adapter, with attribution unchanged from today's `_run_is_ours` set."""
    a = _mission("3", "#3", instance="g1")
    g1 = _mgr("g1", system="gitea_issues", global_ids=False, missions={"3": a})
    g2 = _mgr("g2", system="gitea_issues", global_ids=False)
    loc = _locator({"g1": g1, "g2": g2})
    r = run_coro(loc.resolve("3", local_mgr=g1, memo={}))
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
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a
    assert r.accepted_pmo_refs == frozenset({"", "main", "eng", "cs"})


def test_all_miss_returns_none_fail_safe():
    eng, cs = _mgr("eng", fail=True), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    memo = {}
    r = run_coro(loc.resolve("a", local_mgr=eng, memo=memo))
    assert r is None
    assert memo["a"] is None                      # memoized as unreadable


def test_memo_prevents_second_get():
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng})
    memo = {}
    r1 = run_coro(loc.resolve("a", local_mgr=eng, memo=memo))
    gets_after_first = len(cs.pmo.gets) + len(eng.pmo.gets)
    r2 = run_coro(loc.resolve("a", local_mgr=eng, memo=memo))
    assert r2 is r1
    assert len(cs.pmo.gets) + len(eng.pmo.gets) == gets_after_first


def test_memo_negative_result_not_retried():
    eng, cs = _mgr("eng", fail=True), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng})
    memo = {}
    run_coro(loc.resolve("a", local_mgr=eng, memo=memo))
    gets_after_first = len(cs.pmo.gets) + len(eng.pmo.gets)
    r = run_coro(loc.resolve("a", local_mgr=eng, memo=memo))
    assert r is None
    assert len(cs.pmo.gets) + len(eng.pmo.gets) == gets_after_first


def test_owner_map_peer_tried_once_not_twice():
    """When the owner-map peer fails, the scan must not re-ask the same
    peer — one get per manager per resolve."""
    eng, cs = _mgr("eng"), _mgr("cs", fail=True)
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert cs.pmo.gets == ["a"]


def test_slow_peer_counts_as_miss(monkeypatch):
    """Peer gets run inside the LOCAL instance's poll segment — a hanging
    peer adapter burns at most PEER_GET_TIMEOUT_S, then falls through (here:
    to the local adapter, which resolves). Latency must never couple a sick
    peer's client timeout into every local cycle."""
    from devcake.domain import blocker_locator as bl
    monkeypatch.setattr(bl, "PEER_GET_TIMEOUT_S", 0.05)

    class SlowPMO:
        def __init__(self):
            self.gets = []

        def capabilities(self):
            from fakes import fake_pmo_capabilities
            return fake_pmo_capabilities(global_ids=True)

        async def get(self, ref):
            self.gets.append(ref.pmo_id)
            await asyncio.sleep(5)

    a = _mission("a", "OPS-1", instance="ops")
    eng = _mgr("eng")                                # local cannot read it
    ops = _mgr("ops", missions={"a": a})
    cs = SimpleNamespace(
        instance=SimpleNamespace(name="cs", system="linear"),
        instance_name="cs", pmo=SlowPMO())
    loc = _locator({"cs": cs, "eng": eng, "ops": ops}, owner={"a": "cs"})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a          # fell through to the next peer
    assert cs.pmo.gets == ["a"]    # peer tried once, timed out, moved on


def test_peer_attribution_includes_legacy_pmo_refs():
    """Pre-schema-v3 runs on a peer instance carry pmo_ref ""/"main". Peer
    resolution must accept those stamps or multi-PMO upgrade orphans the
    peer's pre-v3 work trees at resolve_blocker_work."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r is not None
    assert LEGACY_PMO_REFS <= r.accepted_pmo_refs
    assert "cs" in r.accepted_pmo_refs


def test_global_ids_capability_not_system_name():
    """F10: peer allow/deny is PMOCapabilities.global_ids, not the vendor
    system string. A non-linear system name that declares global_ids peers;
    a linear-shaped name with global_ids=False never does."""
    # global_ids=True under an arbitrary system name → peer scan legal
    peer_m = _mission("uuid", "X-1", instance="peer")
    local = _mgr("local", system="acme_board", global_ids=True)
    peer = _mgr("peer", system="acme_board", global_ids=True,
                missions={"uuid": peer_m})
    r = run_coro(_locator({"local": local, "peer": peer}).resolve(
        "uuid", local_mgr=local, memo={}))
    assert r is not None and r.mission is peer_m
    assert peer.pmo.gets == ["uuid"]
    assert local.pmo.gets == ["uuid"]        # asked first, missed, scan ran

    # global_ids=False under a linear system name → peers hard-refused
    foreign = _mission("uuid", "CS-1", instance="cs")
    eng = _mgr("eng", system="linear", global_ids=False)
    cs = _mgr("cs", system="linear", global_ids=False,
              missions={"uuid": foreign})
    r2 = run_coro(_locator({"eng": eng, "cs": cs}).resolve(
        "uuid", local_mgr=eng, memo={}))
    assert r2 is None
    assert cs.pmo.gets == []


def test_capabilities_probe_failure_skips_peers_fail_safe():
    """A capabilities() raise fails CLOSED for peer resolution (never crash
    the locator) — a peer-only blocker then returns None so the gate stays
    open (ADR-0007), not hard-blocked on a probe blip."""
    peer_m = _mission("a", "CS-1", instance="cs")
    eng = _mgr("eng")
    eng.pmo = CountingPMO(raise_caps=True)
    cs = _mgr("cs", missions={"a": peer_m})
    r = run_coro(_locator({"cs": cs, "eng": eng}, owner={"a": "cs"}).resolve(
        "a", local_mgr=eng, memo={}))
    assert r is None
    assert cs.pmo.gets == []          # peer never consulted
    assert eng.pmo.gets == ["a"]      # local fallback tried and missed


def test_get_uses_issue_kind_mission_ref():
    """Native blocked_by edges are issue ids across adapters — the locator
    always queries kind 'issue' (projects normalize blocked_by=[])."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng"), _mgr("cs", missions={"a": a})
    # Record full MissionRef, not just pmo_id
    refs: list = []
    orig_get = cs.pmo.get

    async def tracking_get(ref):
        refs.append(ref)
        return await orig_get(ref)

    cs.pmo.get = tracking_get
    run_coro(_locator({"cs": cs, "eng": eng}, owner={"a": "cs"}).resolve(
        "a", local_mgr=eng, memo={}))
    assert len(refs) == 1
    assert refs[0].pmo_id == "a"
    assert refs[0].kind == "issue"


# ── batch form: one walk per blocker set, local before the peer scan ────────

class BatchPMO(CountingPMO):
    """A batch_get adapter: `get_many` answers every readable id in one call
    and records the batches; `get` records single reads (which the locator
    must never fall back to)."""
    def __init__(self, missions=None, fail_batch=False):
        super().__init__(missions)
        self.fail_batch = fail_batch
        self.batches: list[list[str]] = []

    def capabilities(self):
        from fakes import fake_pmo_capabilities
        return fake_pmo_capabilities(global_ids=True, batch_get=True)

    async def get_many(self, refs):
        ids = [r.pmo_id for r in refs]
        self.batches.append(ids)
        if self.fail_batch:
            raise RuntimeError("pmo down")
        assert all(r.kind == "issue" for r in refs)
        return {i: self.missions[i] for i in ids if i in self.missions}


def _batch_mgr(name, missions=None, fail_batch=False):
    return SimpleNamespace(
        instance=SimpleNamespace(name=name, system="linear"),
        instance_name=name, pmo=BatchPMO(missions, fail_batch=fail_batch))


def test_resolve_many_reads_the_whole_set_in_one_batch():
    """Three hundred blockers on the local board: ONE get_many call, zero
    single gets, every answer memoized (the flagship cost case — the
    unbatched walk was three reads per blocker)."""
    ms = {f"b{i}": _mission(f"b{i}", f"T-{i}", instance="eng") for i in range(300)}
    eng = _batch_mgr("eng", ms)
    cs = _batch_mgr("cs")
    loc = _locator({"eng": eng, "cs": cs})
    memo = {}
    got = run_coro(loc.resolve_many(list(ms), local_mgr=eng, memo=memo))
    assert all(got[b].mission is ms[b] for b in ms)
    assert len(eng.pmo.batches) == 1 and len(eng.pmo.batches[0]) == 300
    assert eng.pmo.gets == [] and cs.pmo.batches == [] and cs.pmo.gets == []
    assert set(memo) == set(ms)


def test_local_precedes_the_peer_scan():
    """No owner entry, the id readable locally: the peers are never asked
    (they used to be, first, for every done+aged-out blocker)."""
    a = _mission("a", "ENG-1", instance="eng")
    eng, cs = _mgr("eng", missions={"a": a}), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.mission is a
    assert cs.pmo.gets == []
    assert r.accepted_pmo_refs == frozenset({"", "main", "eng", "cs"})


def test_owner_map_still_precedes_local():
    """An id the owner map attributes to a peer reads through THAT peer's
    key first — the local adapter is not asked."""
    a = _mission("a", "CS-1", instance="cs")
    eng, cs = _mgr("eng", missions={"a": a}), _mgr("cs", missions={"a": a})
    loc = _locator({"cs": cs, "eng": eng}, owner={"a": "cs"})
    r = run_coro(loc.resolve("a", local_mgr=eng, memo={}))
    assert r.accepted_pmo_refs == _PEER_CS_REFS
    assert eng.pmo.gets == []


def test_peer_scan_asks_only_for_the_misses():
    local = _mission("l", "ENG-1", instance="eng")
    foreign = _mission("f", "CS-1", instance="cs")
    eng = _mgr("eng", missions={"l": local})
    cs = _mgr("cs", missions={"f": foreign})
    loc = _locator({"eng": eng, "cs": cs})
    memo = {}
    got = run_coro(loc.resolve_many(["l", "f", "gone"], local_mgr=eng, memo=memo))
    assert got["l"].mission is local and got["f"].mission is foreign
    assert got["gone"] is None
    assert eng.pmo.gets == ["l", "f", "gone"]        # local: the whole set
    assert cs.pmo.gets == ["f", "gone"]              # peer: only the misses
    assert memo == got                               # negatives memoized too


def test_batch_failure_is_a_miss_never_a_per_id_retry():
    """A refused batch (rate limit, outage) must not degrade into one read
    per id — that storm is what the batch form ends. The peers still get
    their (batched) turn; unresolved ids stay open."""
    ms = {f"b{i}": _mission(f"b{i}", f"T-{i}") for i in range(50)}
    eng = _batch_mgr("eng", ms, fail_batch=True)
    cs = _batch_mgr("cs", {"b1": ms["b1"]})
    loc = _locator({"eng": eng, "cs": cs})
    got = run_coro(loc.resolve_many(list(ms), local_mgr=eng, memo={}))
    assert eng.pmo.gets == [] and len(eng.pmo.batches) == 1
    assert got["b1"].mission is ms["b1"] and got["b2"] is None
    assert len(cs.pmo.batches) == 1 and len(cs.pmo.batches[0]) == 50


def test_resolve_many_serves_memo_hits_without_the_wire():
    a = _mission("a", "ENG-1", instance="eng")
    eng = _mgr("eng", missions={"a": a})
    loc = _locator({"eng": eng})
    memo = {}
    run_coro(loc.resolve_many(["a"], local_mgr=eng, memo=memo))
    run_coro(loc.resolve_many(["a", "a"], local_mgr=eng, memo=memo))
    assert eng.pmo.gets == ["a"]
