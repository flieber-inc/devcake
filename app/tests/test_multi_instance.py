"""Multi-PMO wiring (schema v3, docs/16 M9): the FinalizerRouter's clean
failure on vanished instances, cross-instance dedupe, shared-vs-separate
manager state, and unconfigured-idle semantics."""

import asyncio
from datetime import datetime, timezone

import pytest

from devcake.config import AppConfig, PMOInstance
from devcake.domain.model import Mission
from devcake.domain.orchestrator import FinalizerRouter, MissionManager
from devcake.domain.run import Run
from devcake.adapters.files.run_store import RunStore


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _mgr(name: str, breakers=None) -> MissionManager:
    m = MissionManager.__new__(MissionManager)
    m.instance = PMOInstance(name=name, team_key=name.upper())
    m.instance_name = name
    m.anomalies = {}
    m.breakers = breakers if breakers is not None else {}
    return m


def _mission(pmo_id: str, key: str, instance: str) -> Mission:
    return Mission(pmo_id=pmo_id, pmo_kind="issue", instance=instance,
                   key=key, title="t", status="backlog",
                   updated_at=datetime.now(timezone.utc))


# ── FinalizerRouter ──────────────────────────────────────────────────────────

def test_router_unknown_instance_fails_run_cleanly(tmp_path):
    """A run whose instance vanished from config must fail with a persisted,
    explanatory error — never crash the ingress consumer (plan finding)."""
    store = RunStore(tmp_path / "runs")
    router = FinalizerRouter({}, store)
    run = Run(run_id="GONE-T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="d", seq=1,
              pmo_ref="gone", state="finalizing")
    store.save(run)
    run_coro(router.finalize(run, {}))          # must not raise
    saved = store.get(run.run_id)
    assert saved.state == "failed"
    assert "no longer configured" in saved.error
    # runspec + activity degrade instead of raising
    assert router.runspec_secret_payload(run) is None
    assert run_coro(router.activity_payload(run)) == {"activity_md": "",
                                                      "attachments": []}
    assert "no longer configured" in router.dev_failure_error(run, {})


def test_router_routes_on_pmo_ref_and_legacy_to_sole_manager(tmp_path):
    store = RunStore(tmp_path / "runs")
    calls = []

    class FakeMgr:
        def __init__(self, name):
            self.name = name

        async def finalize(self, run, payload):
            calls.append((self.name, run.run_id))

    managers = {"linteama": FakeMgr("linteama"), "linteamb": FakeMgr("linteamb")}
    router = FinalizerRouter(managers, store)
    run_a = Run(run_id="LINTEAMA-T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
                mission_type="EXECUTE", dev_type="d", seq=1, pmo_ref="linteama")
    run_coro(router.finalize(run_a, {}))
    assert calls == [("linteama", run_a.run_id)]

    # legacy (pre-v3) records route to the sole manager ONLY when exactly one
    legacy = Run(run_id="T-1-2-EXECUTE-BBBBBB", mission_key="T-1",
                 mission_type="EXECUTE", dev_type="d", seq=2, pmo_ref="main",
                 state="finalizing")
    store.save(legacy)
    run_coro(router.finalize(legacy, {}))       # two managers → ambiguous → fail
    assert store.get(legacy.run_id).state == "failed"

    sole = FinalizerRouter({"only": FakeMgr("only")}, store)
    legacy2 = Run(run_id="T-1-3-EXECUTE-CCCCCC", mission_key="T-1",
                  mission_type="EXECUTE", dev_type="d", seq=3, pmo_ref="main")
    run_coro(sole.finalize(legacy2, {}))
    assert ("only", legacy2.run_id) in calls


# ── cross-instance dedupe (plan H1) ──────────────────────────────────────────

def test_shared_mission_claimed_once_with_anomaly(tmp_path, monkeypatch):
    # api.main has import-time singletons (config load, adapters) — point its
    # data dir at tmp so the first import in this process is hermetic
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api.main import _claim_missions
    a, b = _mgr("linteama"), _mgr("linteamb")
    shared = _mission("proj-uuid-1", "PRJ-shared", "linteama")
    only_b = _mission("issue-uuid-2", "DEV-9", "linteamb")
    owner: dict[str, str] = {}
    got_a = _claim_missions(a, [shared], owner)
    got_b = _claim_missions(b, [_mission("proj-uuid-1", "PRJ-shared", "linteamb"),
                                only_b], owner)
    assert [m.key for m in got_a] == ["PRJ-shared"]
    assert [m.key for m in got_b] == ["DEV-9"]              # shared one skipped
    assert "proj-uuid-1" in b.anomalies and "linteama" in b.anomalies["proj-uuid-1"]
    assert not a.anomalies


# ── shared vs separate manager state ────────────────────────────────────────

def test_dev_breakers_shared_advisory_separate():
    shared: dict[str, str] = {}
    a, b = _mgr("linteama", shared), _mgr("linteamb", shared)
    a.breakers["main-dev"] = "DEV_AUTH"
    assert b.breakers["main-dev"] == "DEV_AUTH"     # same dict object
    a.anomalies["x"] = "anomaly"
    assert not b.anomalies                          # advisory state separate


# ── unconfigured-idle semantics (schema v3) ─────────────────────────────────

def test_unconfigured_instance_is_valid_but_idle():
    cfg = AppConfig()                     # seeded unconfigured instance
    assert cfg.pmos[0].configured is False
    assert cfg.repos[0].configured is False
    # a configured one flips the property
    cfg2 = AppConfig(pmos=[PMOInstance(name="linear", team_key="DEV")])
    assert cfg2.pmos[0].configured is True
