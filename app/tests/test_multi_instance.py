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
    from devcake.config import PMOInstance as PI
    cfg = AppConfig()                     # empty first boot (schema v4)
    assert cfg.pmos == [] and cfg.repos == []
    # an unconfigured (empty team_key) instance is valid but idle
    assert PI(name="linear").configured is False
    assert PI(name="linear", team_key="DEV").configured is True


def test_dispatch_stamps_the_dispatching_instances_pmo_ref():
    """Review finding: pmo_ref must come from the DISPATCHING manager, never
    config.pmos[0] — a wrong stamp routes finalize to the wrong workspace
    and blinds the in-flight guard (duplicate-run storm)."""
    import inspect
    from devcake.domain.orchestrator import dispatch as dispatch_mod
    src = inspect.getsource(dispatch_mod.dispatch)
    assert "pmo_ref=self.instance_name" in src
    assert "pmo_ref=self.config.pmos[0]" not in src
    from devcake.domain.orchestrator import mapper as mapper_mod
    src = inspect.getsource(mapper_mod.dispatch_mapper)
    assert "pmo_ref=self.instance_name" in src


def test_ownership_survives_a_transient_cycle(tmp_path, monkeypatch):
    """Review finding: ownership of a shared mission must NOT flip to the
    second instance just because the owner had one PMOTransient cycle."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api.main import _claim_missions, _release_stale_ownership
    from devcake.api import main as app_main
    a, b = _mgr("linteama"), _mgr("linteamb")
    owner: dict[str, str] = {"proj-1": "linteama"}   # A claimed earlier
    # A's cycle fails (PMOTransient) → A contributes nothing to polled_ok;
    # B still must NOT claim proj-1
    got_b = _claim_missions(b, [_mission("proj-1", "PRJ-s", "linteamb")], owner)
    assert got_b == [] and owner["proj-1"] == "linteama"
    # release only when the OWNER successfully polls without the mission
    monkeypatch.setattr(app_main, "managers", {"linteama": a, "linteamb": b})
    monkeypatch.setattr(app_main, "_mission_owner", owner)
    app_main._release_stale_ownership({"linteamb": {"proj-1"}})   # B ok, A failed
    assert owner.get("proj-1") == "linteama"                      # kept
    app_main._release_stale_ownership({"linteama": set()})        # A ok, gone
    assert "proj-1" not in owner


def test_permanent_pmo_error_starves_no_other_instance(tmp_path, monkeypatch):
    """Audit A1: a NON-transient PMO failure (revoked key → RuntimeError, not
    PMOTransient) on instance A must skip only A's segment — B still polls,
    A's cache rows are retained, and /health surfaces the degradation. A
    green segment clears the degraded flag."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import main as app_main
    a, b = _mgr("linteama"), _mgr("linteamb")
    monkeypatch.setattr(app_main, "managers", {"linteama": a, "linteamb": b})
    monkeypatch.setattr(app_main, "_managers_in_config_order", lambda: [a, b])
    stale_row = {"instance": "linteama", "key": "T-9"}
    monkeypatch.setattr(app_main, "missions_cache", [stale_row])
    app_main._poll_degraded.clear()

    polled: list[str] = []

    async def broken_poll(mgr, cache_rows):
        if mgr.instance_name == "linteama":
            raise RuntimeError("linear graphql: authentication failed")
        polled.append(mgr.instance_name)
        cache_rows.append({"instance": mgr.instance_name, "key": "T-1"})
        return (1, 0, 0, set())

    monkeypatch.setattr(app_main, "_poll_instance", broken_poll)
    run_coro(app_main.run_poll_cycle(1))
    assert polled == ["linteamb"]                     # B was never starved
    assert "RuntimeError" in app_main._poll_degraded["linteama"]
    # A keeps its last snapshot (v0 behavior extended to permanent errors)
    assert stale_row in app_main.missions_cache

    async def ok_poll(mgr, cache_rows):
        polled.append(mgr.instance_name)
        return (0, 0, 0, set())

    monkeypatch.setattr(app_main, "_poll_instance", ok_poll)
    run_coro(app_main.run_poll_cycle(2))
    assert app_main._poll_degraded == {}              # green cycle clears it


def test_dispatch_gates_on_pmo_read_failure(tmp_path):
    """Audit A1 (dispatch half): pmo.get raising at the live re-read must
    gate the mission with a visible reason, never escape into the segment."""
    from test_transitions import FakePMO, make_mgr, mission
    from devcake.domain.model import MissionType

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _store = make_mgr(tmp_path, m)

    async def broken_get(ref):
        raise RuntimeError("linear graphql: team not found")

    fake.get = broken_get
    dev = mgr.dev_types["senior-dev"]
    out = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert out is None
    assert "PMO read failed" in mgr.blocked_reasons[m.pmo_id]


def test_dispatch_gates_on_run_id_overflow(tmp_path):
    """Audit A15c: a forged `9…9_EXECUTE.md` feed marker inflates seq past
    the 64-char run-id budget — the mission gates with a fix-the-marker
    reason instead of wedging the poll segment."""
    from test_transitions import make_mgr, mission
    from devcake.domain.model import ActivityEntry, MissionType
    from datetime import datetime, timezone

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _store = make_mgr(tmp_path, m, forge=object())
    mgr.internal_forge = None
    mgr.instance = PMOInstance(name="linear", team_key="DEV",
                               default_repo="main")
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="troll", kind="comment",
        body="see `" + "9" * 39 + "_EXECUTE.md` for details")]
    dev = mgr.dev_types["senior-dev"]
    out = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert out is None
    reason = mgr.blocked_reasons[m.pmo_id]
    assert "64" in reason and "marker" in reason


def test_run_branch_legacy_fallback():
    """Pre-v3 records (pmo_ref ''/'main', no stored branch) must resolve to
    the UNPREFIXED branch their Devs actually pushed."""
    from devcake.ports.forge import run_branch
    legacy = Run(run_id="T-9-1-EXECUTE-AAAAAA", mission_key="T-9",
                 mission_type="EXECUTE", dev_type="d", seq=1, pmo_ref="main")
    assert run_branch(legacy) == "devcake/T-9"
    modern = Run(run_id="LINEAR-T-9-2-EXECUTE-BBBBBB", mission_key="T-9",
                 mission_type="EXECUTE", dev_type="d", seq=2,
                 pmo_ref="linear", branch="devcake/LINEAR-T-9")
    assert run_branch(modern) == "devcake/LINEAR-T-9"
    derived = Run(run_id="LINEAR-T-9-3-EXECUTE-CCCCCC", mission_key="T-9",
                  mission_type="EXECUTE", dev_type="d", seq=3, pmo_ref="linear")
    assert run_branch(derived) == "devcake/LINEAR-T-9"
