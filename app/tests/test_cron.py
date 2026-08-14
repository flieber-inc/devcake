"""Cron module (PLAN_MEMORY §6) — public seam: CronService.fire."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from devcake.config import (AppConfig, CronJob, MEMORY_CURATOR_CRON_ID,
                            PMOInstance, RepoInstance, memory_curator_seed)
from devcake.domain.cron_service import (CRON_MARKER, CronBusy, CronService,
                                         CronUnconfigured, cron_marker)
from devcake.domain.model import (LABEL_EXECUTE, LABEL_OPTIN, LABEL_PLAN,
                                  Mission)


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


class FakePMO:
    def __init__(self, team="ORG/board"):
        self.team = team
        self.created: list[tuple] = []
        self.missions: list[Mission] = []

    async def list_all(self, team_ref):
        return list(self.missions)

    async def ensure_labels(self, team_ref, names):
        self.ensured = set(names)

    async def create_mission(self, team_ref, title, description, priority,
                             label_names, parent_ref=None):
        key = f"T-{len(self.created) + 1}"
        pid = f"id-{key}"
        self.created.append((title, description, set(label_names), key))
        self.missions.append(Mission(
            pmo_id=pid, pmo_kind="issue", key=key, title=title,
            description=description, status="backlog",
            labels=set(label_names),
            updated_at=datetime.now(timezone.utc)))
        return key, pid


def _cfg(*pmos, crons=None, memory=None):
    repos = [RepoInstance(name=n, url=f"https://github.com/acme/{n}")
             for n in ("webapp", "nb")]
    rows = list(pmos)
    jobs = list(crons) if crons is not None else [memory_curator_seed()]
    return AppConfig(pmos=rows, repos=repos, crons=jobs)


def _mgr(name, pmo, inst):
    return SimpleNamespace(
        instance_name=name, instance=inst, pmo=pmo,
        config=None)


def test_generic_execute_labels_and_onboard_has_no_stage_label():
    inst = PMOInstance(name="eng", team_key="T")
    pmo = FakePMO()
    cfg = _cfg(inst, crons=[
        memory_curator_seed(),
        CronJob(id="nightly", name="N", entry_stage="EXECUTE",
                description_template="do {timestamp}", pmo="eng"),
        CronJob(id="onboard", name="O", entry_stage="ONBOARD",
                description_template="intake", pmo="eng"),
    ])
    svc = CronService(cfg, {"eng": _mgr("eng", pmo, inst)})
    got = run_coro(svc.fire("nightly", automatic=False))
    assert got[0]["key"] == "T-1"
    title, body, labels, _ = pmo.created[0]
    assert title.startswith("[cron:nightly]")
    assert LABEL_OPTIN in labels and LABEL_EXECUTE in labels
    assert cron_marker("nightly") in body
    assert "{timestamp}" not in body
    pmo.created.clear()
    pmo.missions.clear()
    run_coro(svc.fire("onboard", automatic=False))
    _, _, labels, _ = pmo.created[0]
    assert labels == {LABEL_OPTIN}
    assert "DEVCAKE-ONBOARD" not in labels


def test_single_flight_skips_when_marker_still_open():
    inst = PMOInstance(name="eng", team_key="T")
    pmo = FakePMO()
    cfg = _cfg(inst, crons=[
        memory_curator_seed(),
        CronJob(id="nightly", name="N", entry_stage="PLAN",
                description_template="x", pmo="eng"),
    ])
    svc = CronService(cfg, {"eng": _mgr("eng", pmo, inst)})
    run_coro(svc.fire("nightly", automatic=False))
    with pytest.raises(CronBusy):
        run_coro(svc.fire("nightly", automatic=False))


def test_pause_skips_generic():
    inst = PMOInstance(name="eng", team_key="T", intake_paused=True)
    pmo = FakePMO()
    cfg = _cfg(inst, crons=[
        memory_curator_seed(),
        CronJob(id="nightly", name="N", entry_stage="PLAN",
                description_template="x", pmo="eng"),
    ])
    svc = CronService(cfg, {"eng": _mgr("eng", pmo, inst)})
    assert run_coro(svc.fire("nightly", automatic=False)) == []
    assert pmo.created == []


def test_memory_curator_skips_empty_automatic_run_now_does_not():
    from devcake.domain import claims as claims_mod
    claims_mod.claims_depth["nb"] = 0
    product = PMOInstance(name="eng", team_key="A", repos=["webapp"],
                          memory_repos=["nb"])
    curator = PMOInstance(name="cur", team_key="B", repos=["nb"])
    pmo = FakePMO()
    cfg = _cfg(product, curator)
    svc = CronService(cfg, {
        "eng": _mgr("eng", FakePMO(), product),
        "cur": _mgr("cur", pmo, curator),
    })
    assert run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=True)) == []
    assert pmo.created == []
    got = run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=False))
    assert got[0]["pmo"] == "cur"
    _, body, labels, _ = pmo.created[0]
    assert LABEL_EXECUTE in labels
    assert cron_marker(MEMORY_CURATOR_CRON_ID) in body


class FakeClaims:
    """ClaimsNotebooks fake for the depth path — listing only."""

    def __init__(self, names):
        self.names = names
        self.listed: list[str] = []

    async def list_json_names(self, card):
        self.listed.append(card)
        return self.names

    def can_write(self, card):
        return True


def test_automatic_skip_confirms_depth_by_listing_not_stale_cache():
    """R2: a drained notebook leaves a stale >0 cache; the automatic fire
    must list `.claims/` and skip, not burn a Curator run every interval."""
    from devcake.domain import claims as claims_mod
    product = PMOInstance(name="eng", team_key="A", repos=["webapp"],
                          memory_repos=["nb"])
    curator = PMOInstance(name="cur", team_key="B", repos=["nb"])
    pmo = FakePMO()
    cfg = _cfg(product, curator)
    claims = FakeClaims([])            # the drain emptied the queue
    claims_mod.claims_depth["nb"] = 5  # stale — cache never saw the drain
    try:
        svc = CronService(cfg, {
            "eng": _mgr("eng", FakePMO(), product),
            "cur": _mgr("cur", pmo, curator),
        }, claims=claims)
        assert run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=True)) == []
        assert pmo.created == [] and claims.listed == ["nb"]
        # the reverse staleness: cache says 0, listing finds work → fire
        claims.names = ["aa11bb22cc33dd44.json"]
        assert claims_mod.claims_depth["nb"] == 0  # refreshed by the skip
        got = run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=True))
        assert got and pmo.created
        # listing failure falls back to the cache, not a crash
        claims.names = None
        run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=True))
    finally:
        claims_mod.claims_depth.pop("nb", None)


def test_dev_type_bound_notebook_is_curated_and_warned():
    """R3: set M includes Dev Type memory_repos — a domain-bound-only
    notebook gets Curator tickets, and a no-board warning when unstaffed."""
    from devcake.domain import claims as claims_mod
    product = PMOInstance(name="eng", team_key="A", repos=["webapp"])
    curator = PMOInstance(name="cur", team_key="B", repos=["nb"])
    pmo = FakePMO()
    cfg = _cfg(product, curator)
    dts = {"coder": SimpleNamespace(memory_repos=["nb"])}
    claims_mod.claims_depth.pop("nb", None)
    svc = CronService(cfg, {
        "eng": _mgr("eng", FakePMO(), product),
        "cur": _mgr("cur", pmo, curator),
    }, dev_types=dts)
    got = run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=False))
    assert got[0]["pmo"] == "cur" and pmo.created
    # unstaffed: no Curator board for the domain-bound card → warning
    cfg2 = _cfg(product)
    svc2 = CronService(cfg2, {"eng": _mgr("eng", FakePMO(), product)},
                       dev_types=dts)
    run_coro(svc2.fire(MEMORY_CURATOR_CRON_ID, automatic=False))
    assert "nb" in svc2.no_board


def test_memory_curator_no_board_is_a_standing_warning():
    product = PMOInstance(name="eng", team_key="A", repos=["webapp"],
                          memory_repos=["nb"])
    cfg = _cfg(product)
    svc = CronService(cfg, {"eng": _mgr("eng", FakePMO(), product)})
    run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=False))
    assert "nb" in svc.no_board


class BoomPMO(FakePMO):
    """create_mission fails until `healthy` is flipped — forge-outage fake."""

    def __init__(self):
        super().__init__()
        self.calls = 0
        self.healthy = False

    async def create_mission(self, *a, **k):
        self.calls += 1
        if not self.healthy:
            raise RuntimeError("forge down")
        return await super().create_mission(*a, **k)


def _nightly_cfg(inst):
    return _cfg(inst, crons=[
        memory_curator_seed(),
        CronJob(id="nightly", name="N", entry_stage="EXECUTE",
                description_template="x", pmo="eng", enabled=True,
                interval_minutes=1),
    ])


def test_degradation_is_ledger_derived_restart_safe_and_cleared_by_run_now(
        tmp_path):
    """R4: 3 failed automatic fires degrade; the state survives a service
    rebuild (file ledger); a successful Run now clears it."""
    from devcake.adapters.files.cron_store import CronStore
    inst = PMOInstance(name="eng", team_key="T")
    pmo = BoomPMO()
    cfg = _nightly_cfg(inst)
    store = CronStore(tmp_path / "cron.json")
    svc = CronService(cfg, {"eng": _mgr("eng", pmo, inst)}, store=store)
    for _ in range(3):
        run_coro(svc.maybe_fire())
        # age the window out so each pass is a fresh attempt
        store._state["nightly"].pop("last_fire_at", None)
    assert pmo.calls == 3 and "nightly" in svc.degraded
    run_coro(svc.maybe_fire())          # degraded rows stop firing
    assert pmo.calls == 3
    svc2 = CronService(cfg, {"eng": _mgr("eng", pmo, inst)},
                       store=CronStore(tmp_path / "cron.json"))
    assert "nightly" in svc2.degraded   # rehydrated from disk
    pmo.healthy = True
    got = run_coro(svc2.fire("nightly", automatic=False))
    assert got and svc2.degraded == set()


def test_elapsed_interval_gates_automatic_fires(tmp_path):
    """R4: schedule = elapsed time since the persisted last_fire_at —
    fresh row fires immediately, then not again inside the window."""
    from datetime import timedelta
    from devcake.adapters.files.cron_store import CronStore
    inst = PMOInstance(name="eng", team_key="T")
    pmo = FakePMO()
    cfg = _nightly_cfg(inst)
    store = CronStore(tmp_path / "cron.json")
    svc = CronService(cfg, {"eng": _mgr("eng", pmo, inst)}, store=store)
    run_coro(svc.maybe_fire())
    assert len(pmo.created) == 1        # no stamp yet → due now
    run_coro(svc.maybe_fire())
    assert len(pmo.created) == 1        # inside the window → skip
    old = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    store._state["nightly"]["last_fire_at"] = old
    pmo.missions.clear()                # let single-flight pass
    run_coro(svc.maybe_fire())
    assert len(pmo.created) == 2        # window elapsed → fires again


def test_unknown_id_raises():
    cfg = _cfg(PMOInstance(name="eng", team_key="T"))
    svc = CronService(cfg, {})
    with pytest.raises(CronUnconfigured):
        run_coro(svc.fire("nosuch", automatic=False))
