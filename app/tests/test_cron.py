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


def test_memory_curator_no_board_is_a_standing_warning():
    product = PMOInstance(name="eng", team_key="A", repos=["webapp"],
                          memory_repos=["nb"])
    cfg = _cfg(product)
    svc = CronService(cfg, {"eng": _mgr("eng", FakePMO(), product)})
    run_coro(svc.fire(MEMORY_CURATOR_CRON_ID, automatic=False))
    assert "nb" in svc.no_board


def test_unknown_id_raises():
    cfg = _cfg(PMOInstance(name="eng", team_key="T"))
    svc = CronService(cfg, {})
    with pytest.raises(CronUnconfigured):
        run_coro(svc.fire("nosuch", automatic=False))
