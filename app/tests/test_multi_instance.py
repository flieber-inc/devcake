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
    from fakes import make_mission_manager
    return make_mission_manager(
        instance=PMOInstance(name=name, team_key=name.upper()),
        breakers=breakers if breakers is not None else {},
        noop_audit=False,
    )


def _mission(pmo_id: str, key: str, instance: str) -> Mission:
    return Mission(pmo_id=pmo_id, pmo_kind="issue", instance=instance,
                   key=key, title="t", status="backlog",
                   updated_at=datetime.now(timezone.utc))


# ── FinalizerRouter ──────────────────────────────────────────────────────────

class RecordingMessaging:
    def __init__(self):
        self.deleted_users: list[str] = []
        self.deleted_streams: list[str] = []

    async def delete_run_user(self, rid):
        self.deleted_users.append(rid)

    async def delete_reply_stream(self, rid):
        self.deleted_streams.append(rid)


def test_router_unknown_instance_fails_run_cleanly(tmp_path):
    """A run whose instance vanished from config must fail with a persisted,
    explanatory error — never crash the ingress consumer (plan finding) —
    AND tear down the run's Redis ACL user + reply stream like every other
    terminal path (audit A8: they leaked forever, invisible to startup
    reconciliation which only inspects active runs)."""
    store = RunStore(tmp_path / "runs")
    messaging = RecordingMessaging()
    router = FinalizerRouter({}, store, messaging)
    run = Run(run_id="GONE-T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
              mission_type="EXECUTE", dev_type="d", seq=1,
              pmo_ref="gone", state="finalizing")
    store.save(run)
    run_coro(router.finalize(run, {}))          # must not raise
    saved = store.get(run.run_id)
    assert saved.state == "failed"
    assert "no longer configured" in saved.error
    assert messaging.deleted_users == [run.run_id]
    assert messaging.deleted_streams == [run.run_id]
    # runspec + activity degrade instead of raising
    assert router.runspec_secret_payload(run) is None
    assert run_coro(router.activity_payload(run)) == {"mission_md": "",
                                                      "activity_md": "",
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
    router = FinalizerRouter(managers, store, RecordingMessaging())
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

    sole = FinalizerRouter({"only": FakeMgr("only")}, store, RecordingMessaging())
    legacy2 = Run(run_id="T-1-3-EXECUTE-CCCCCC", mission_key="T-1",
                  mission_type="EXECUTE", dev_type="d", seq=3, pmo_ref="main")
    run_coro(sole.finalize(legacy2, {}))
    assert ("only", legacy2.run_id) in calls


# ── cross-instance dedupe (plan H1) ──────────────────────────────────────────

def test_shared_mission_claimed_once_with_anomaly(tmp_path, monkeypatch):
    # api.main has import-time singletons (config load, adapters) — point its
    # data dir at tmp so the first import in this process is hermetic
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api.poll import _claim_missions
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
    assert "pmo_ref=mgr.instance_name" in src
    assert "pmo_ref=self.config.pmos[0]" not in src
    from devcake.domain.orchestrator import steward as steward_mod
    src = inspect.getsource(steward_mod.dispatch_steward)
    assert "pmo_ref=mgr.instance_name" in src


def _rt(tmp_path, managers=None, store=None, order=None):
    """A PollRuntime over fakes (ADR-0015 C4: the poll machinery's test seam
    is the runtime object itself, not monkeypatched module globals)."""
    from types import SimpleNamespace

    from devcake.adapters.files.owner_store import OwnerStore
    from devcake.api.poll import PollRuntime
    from devcake.config import AppConfig

    managers = managers if managers is not None else {}

    async def _noop():
        return {}

    return PollRuntime(
        config=AppConfig(), managers=managers, stewards={},
        store=store if store is not None else SimpleNamespace(active=lambda: [], all=lambda: []),
        forge_runtime=SimpleNamespace(breakers={},
                                      last_full_probe_at=datetime.now(timezone.utc)),
        refresh_forge_health=_noop,
        managers_in_config_order=(order or (lambda: list(managers.values()))),
        owner_store=OwnerStore(tmp_path / "state" / "mission_owner.json"))


def test_ownership_survives_a_transient_cycle(tmp_path):
    """Review finding: ownership of a shared mission must NOT flip to the
    second instance just because the owner had one PMOTransient cycle."""
    from devcake.api.poll import _claim_missions
    a, b = _mgr("linteama"), _mgr("linteamb")
    owner: dict[str, str] = {"proj-1": "linteama"}   # A claimed earlier
    # A's cycle fails (PMOTransient) → A contributes nothing to polled_ok;
    # B still must NOT claim proj-1
    got_b = _claim_missions(b, [_mission("proj-1", "PRJ-s", "linteamb")], owner)
    assert got_b == [] and owner["proj-1"] == "linteama"
    # release only when the OWNER successfully polls without the mission
    rt = _rt(tmp_path, managers={"linteama": a, "linteamb": b})
    rt.mission_owner = owner
    rt.release_stale_ownership({"linteamb": {"proj-1"}})   # B ok, A failed
    assert owner.get("proj-1") == "linteama"               # kept
    rt.release_stale_ownership({"linteama": set()})        # A ok, gone
    assert "proj-1" not in owner


def test_owner_store_roundtrip_and_corrupt_file(tmp_path):
    """Audit A15: ownership was process-lifetime only — a restart reopened
    the duplicate-dispatch window ('persistent' was oversold). Now a real
    file under /data/state."""
    from devcake.adapters.files.owner_store import OwnerStore
    path = tmp_path / "state" / "mission_owner.json"
    s = OwnerStore(path)
    assert s.load() == {}
    s.save({"proj-1": "linteama"})
    assert OwnerStore(path).load() == {"proj-1": "linteama"}   # restart survival
    path.write_text("{corrupt")
    assert OwnerStore(path).load() == {}                       # never wedges boot


def test_poll_cycle_persists_ownership_changes(tmp_path):
    from devcake.adapters.files.owner_store import OwnerStore
    from devcake.api.poll import _claim_missions
    a = _mgr("linteama")
    rt = _rt(tmp_path, managers={"linteama": a})

    async def claiming_poll(mgr, cache_rows):
        got = _claim_missions(
            mgr, [_mission("proj-9", "PRJ-9", mgr.instance_name)],
            rt.mission_owner)
        return (len(got), 0, 0, {"proj-9"})

    rt.poll_instance = claiming_poll   # instance-attr override, like fakes._audit
    run_coro(rt.run_cycle(1))
    store_path = tmp_path / "state" / "mission_owner.json"
    assert OwnerStore(store_path).load() == {"proj-9": "linteama"}


def test_release_deferred_while_ex_owner_run_still_active(tmp_path, monkeypatch):
    """Audit A15: releasing a shared mission the moment its owner no longer
    sees it (or left config) lets the surviving instance re-dispatch while
    the ex-owner's run is still executing — duplicate work."""
    a, b = _mgr("linteama"), _mgr("linteamb")

    class FakeStore:
        def __init__(self, runs):
            self.runs = runs

        def active(self):
            return self.runs

    live = Run(run_id="LINTEAMA-PRJ-1-1-EXECUTE-AAAAAA", mission_key="PRJ-1",
               mission_type="EXECUTE", dev_type="d", seq=1,
               pmo_ref="linteama", state="running")
    live.mission_pmo_id = "proj-1"
    owner = {"proj-1": "linteama"}
    rt = _rt(tmp_path, managers={"linteama": a, "linteamb": b},
             store=FakeStore([live]))
    rt.mission_owner = owner
    # owner polled green and no longer sees the mission — but its run lives
    rt.release_stale_ownership({"linteama": set()})
    assert owner == {"proj-1": "linteama"}
    # run finished → release proceeds
    rt.store = FakeStore([])
    rt.release_stale_ownership({"linteama": set()})
    assert owner == {}


def test_permanent_pmo_error_starves_no_other_instance(tmp_path, monkeypatch):
    """Audit A1: a NON-transient PMO failure (revoked key → RuntimeError, not
    PMOTransient) on instance A must skip only A's segment — B still polls,
    A's cache rows are retained, and /health surfaces the degradation. A
    green segment clears the degraded flag."""
    a, b = _mgr("linteama"), _mgr("linteamb")
    rt = _rt(tmp_path, managers={"linteama": a, "linteamb": b},
             order=lambda: [a, b])
    stale_row = {"instance": "linteama", "key": "T-9"}
    rt.missions_cache[:] = [stale_row]

    polled: list[str] = []

    async def broken_poll(mgr, cache_rows):
        if mgr.instance_name == "linteama":
            raise RuntimeError("linear graphql: authentication failed")
        polled.append(mgr.instance_name)
        cache_rows.append({"instance": mgr.instance_name, "key": "T-1"})
        return (1, 0, 0, set())

    rt.poll_instance = broken_poll
    run_coro(rt.run_cycle(1))
    assert polled == ["linteamb"]                     # B was never starved
    assert "RuntimeError" in rt.poll_degraded["linteama"]
    # A keeps its last snapshot (v0 behavior extended to permanent errors)
    assert stale_row in rt.missions_cache

    async def ok_poll(mgr, cache_rows):
        polled.append(mgr.instance_name)
        return (0, 0, 0, set())

    rt.poll_instance = ok_poll
    run_coro(rt.run_cycle(2))
    assert rt.poll_degraded == {}                     # green cycle clears it


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


def test_dispatch_gates_on_missing_referenced_secret_env(tmp_path, monkeypatch):
    """Founder decision 2026-07-16: a declared secret_env var with NO stored
    value that IS referenced ($VAR/${VAR}) by an mcp_setup_command refuses
    dispatch deterministically — the command would run with an empty
    expansion and die as exit 14 inside the container, burning an attempt
    with the root cause buried in one warning line."""
    from test_transitions import make_mgr, mission
    from devcake import secrets as secrets_store
    from devcake.config import DevType
    from devcake.domain.model import MissionType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    gated = DevType(name="senior-dev", harness_template="claude-code",
                    secret_env=["DD_API_KEY"],
                    mcp_setup_commands=["claude mcp add logs "
                                        "-e K=$DD_API_KEY -- x"])
    out = run_coro(mgr.dispatch(m, MissionType.EXECUTE, gated))
    assert out is None
    reason = mgr.blocked_reasons[m.pmo_id]
    assert "DD_API_KEY" in reason and "mcp_setup_commands" in reason

    # pasting the value un-gates on the next poll cycle: the secret-env
    # gate no longer fires (any later refusal is a different reason)
    secrets_store.write_harness_secret("DD_API_KEY", "dd-key-0123456789ab")
    mgr.blocked_reasons.pop(m.pmo_id, None)
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, gated))
    assert "DD_API_KEY" not in mgr.blocked_reasons.get(m.pmo_id, "")


def test_dispatch_proceeds_when_missing_secret_unreferenced(tmp_path, monkeypatch):
    """Declared-but-unreferenced missing values keep warn-and-proceed — a
    log credential must not block missions when no setup command needs it."""
    from test_transitions import make_mgr, mission
    from devcake.config import DevType
    from devcake.domain.model import MissionType

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    dt = DevType(name="senior-dev", harness_template="claude-code",
                 secret_env=["DD_API_KEY"],
                 mcp_setup_commands=["claude mcp add probe -- x"])
    run_coro(mgr.dispatch(m, MissionType.EXECUTE, dt))
    assert "secret env" not in mgr.blocked_reasons.get(m.pmo_id, "")


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
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="troll", kind="comment",
        body="see `" + "9" * 39 + "_EXECUTE.md` for details")]
    dev = mgr.dev_types["senior-dev"]
    out = run_coro(mgr.dispatch(m, MissionType.EXECUTE, dev))
    assert out is None
    reason = mgr.blocked_reasons[m.pmo_id]
    assert "64" in reason and "marker" in reason


def test_config_put_survives_secret_cleanup_failure(tmp_path, monkeypatch):
    """Audit A21: the config change is APPLIED once reload succeeds — a
    failure deleting a removed instance's stored secrets must log, not 500
    an already-applied change."""
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    from devcake.api import main as app_main
    from devcake.api import config_service
    from devcake.config import AppConfig, RepoInstance
    from fakes import make_services
    from types import SimpleNamespace

    def boom(scope, name):
        raise RuntimeError("disk error")

    # save_config is called by config_service after the C5 split — patch it
    # THERE, not on app_main (audit D5 #3: the app_main patch is dead, so the
    # PUT persisted repos:[] to the real config path).
    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    monkeypatch.setattr(app_main.secrets_store, "delete_connection_instance", boom)
    # ADR-0028: a fresh per-test graph — no module-global repos to restore.
    # Dev types must cover the default assignments: the PUT validates the
    # reference unconditionally since SEC-3 (boot always seeds these).
    from devcake.config import DEFAULT_DEV_TYPES
    monkeypatch.setattr(app_main, "services", make_services(
        config=AppConfig(repos=[RepoInstance(name="gone",
                                             url="https://github.com/o/r")]),
        dev_types={dt.name: dt for dt in DEFAULT_DEV_TYPES},
        managers={}, repo_cache=None,
        poll_rt=SimpleNamespace(lock=asyncio.Lock()),
        reload_connections=lambda: None))
    out = run_coro(app_main.put_config({"repos": []}))   # must not raise
    assert out["repos"] == []


def test_hello_and_oauth_runs_stamp_sys_pmo_ref():
    """Audit A29: HELLO/OAUTH runs carried the field default pmo_ref='main'
    — indistinguishable from legacy pre-v3 mission records by ref. Source-
    pinned (the dispatch-stamp precedent above): both construction sites
    must stamp the sys pseudo-instance explicitly."""
    import inspect
    from devcake.domain import oauth as oauth_mod
    from devcake.domain import runs as runs_mod
    assert 'pmo_ref="sys"' in inspect.getsource(runs_mod.RunManager.dispatch_hello)
    # oauth's Run construction lives in start()'s traced inner — pin the
    # module (it has exactly one Run construction site)
    assert 'pmo_ref="sys"' in inspect.getsource(oauth_mod)


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


# ── cross-instance blocker resolution wiring (ADR-0009 amendment) ────────────

def test_locator_sees_owner_after_claim(tmp_path):
    """The composition shape from api.main, hermetically: ONE locator over
    the live managers dict, owner getter reading the runtime's durable claim
    map. A mission claimed by cs resolves for eng via cs's OWN adapter, with
    cs attribution."""
    from devcake.api.poll import _claim_missions
    from devcake.domain.blocker_locator import BlockerLocator

    class _PMO:
        def __init__(self, missions):
            self.missions = missions

        def capabilities(self):
            from fakes import fake_pmo_capabilities
            return fake_pmo_capabilities()   # global_ids=True (linear-shaped)

        async def get(self, ref):
            m = self.missions.get(ref.pmo_id)
            if m is None:
                raise RuntimeError(f"missing {ref.pmo_id}")
            return m

    cs, eng = _mgr("cs"), _mgr("eng")
    cs_m = _mission("uuid-a", "CS-1", "cs")
    cs.pmo, eng.pmo = _PMO({"uuid-a": cs_m}), _PMO({})
    managers = {"cs": cs, "eng": eng}
    rt = _rt(tmp_path, managers=managers)
    locator = BlockerLocator(managers, lambda bid: rt.mission_owner.get(bid))
    cs.blocker_locator = locator
    eng.blocker_locator = locator
    _claim_missions(cs, [cs_m], rt.mission_owner)
    assert rt.mission_owner["uuid-a"] == "cs"
    r = run_coro(eng.blocker_locator.resolve(
        "uuid-a", local_mgr=eng, memo={}))
    assert r.mission is cs_m
    assert r.accepted_pmo_refs == frozenset({"cs"})


def test_main_wires_one_shared_locator():
    """build_managers must hand the ONE shared locator to BOTH branches
    (create + reconcile) — a missed branch is the silent 'half multi-PMO'
    wiring bug the required-dependency rule exists to prevent. Source-pinned
    against api/services.py (ADR-0028: the composition root moved there;
    importing it stays side-effect-free)."""
    from pathlib import Path
    import devcake
    src = (Path(devcake.__file__).parent / "api" / "services.py").read_text()
    assert "s.blocker_locator = BlockerLocator(" in src
    assert "mgr.blocker_locator = self.blocker_locator" in src   # reconcile
    assert "blocker_locator=self.blocker_locator" in src         # create


def test_merged_cache_resolves_foreign_blocker_keys(tmp_path):
    """A blocked_by id owned by a PEER instance displays as that instance's
    key after the merged post-pass; an id in NO instance's snapshot stays a
    raw vendor id (done + aged out — advisory feed, documented)."""
    a, b = _mgr("cs"), _mgr("eng")
    rt = _rt(tmp_path, managers={"cs": a, "eng": b}, order=lambda: [a, b])

    async def seg(mgr, cache_rows):
        if mgr.instance_name == "cs":
            cache_rows.append({"instance": "cs", "key": "CS-1",
                               "pmo_id": "uuid-a", "blocked_by": []})
            return (1, 0, 0, {"uuid-a"})
        cache_rows.append({"instance": "eng", "key": "ENG-1",
                           "pmo_id": "uuid-b",
                           "blocked_by": ["uuid-a", "uuid-gone"]})
        return (1, 0, 0, {"uuid-b"})

    rt.poll_instance = seg
    run_coro(rt.run_cycle(1))
    row = next(r for r in rt.missions_cache if r["key"] == "ENG-1")
    assert row["blocked_by"] == ["CS-1", "uuid-gone"]
