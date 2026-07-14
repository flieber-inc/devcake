"""Crash-recovery spine (ISSUES #1–3, #6, #26): redelivery, kill teardown,
watchdog timeout, and merge already-merged honesty."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from devcake.config import PMOInstance
from fakes import FakeForgeRuntime

from devcake.domain.run import Run, utcnow
from devcake.domain.runs import RunManager
from devcake.adapters.files.run_store import RunStore
from devcake.adapters.github.adapter import GitHubForge
from devcake.ports.forge import ForgeError, PullRequest


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _make_run(store: RunStore, state: str = "running", **kwargs) -> Run:
    defaults = dict(
        run_id="T-1-1-EXECUTE-ABCDEF",
        mission_key="T-1",
        mission_pmo_id="p1",
        mission_type="EXECUTE",
        dev_type="main-dev",
        seq=1,
        state=state,
        created_at=utcnow() - timedelta(minutes=5),
    )
    defaults.update(kwargs)
    run = Run(**defaults)
    store.save(run)
    return run


class FakeMessaging:
    def __init__(self):
        self.deleted_users = []
        self.deleted_streams = []
        self.unresolved = set()  # run_ids with entries still on the ingress stream

    async def unresolved_run_ids(self):
        return set(self.unresolved)

    async def delete_run_user(self, rid):
        self.deleted_users.append(rid)

    async def delete_reply_stream(self, rid):
        self.deleted_streams.append(rid)

    async def delete_runspec_result(self, rid):
        pass

    async def reply(self, *a, **k):
        pass


class FakeExecutor:
    def __init__(self, stop_raises=False):
        self.stop_raises = stop_raises
        self.stopped = []

    async def stop(self, rid):
        self.stopped.append(rid)
        if self.stop_raises:
            raise RuntimeError("network blip")
        return True

    async def status(self, rid):
        return None

    async def node_errors(self, rid):
        return []


def test_artifacts_redelivery_noop_on_all_terminal_states(tmp_path):
    """ISSUES #1: finished/failed/timed_out/orphaned must not re-enter finalize."""
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor()
    mgr = RunManager(store, messaging, executor)
    finalize_calls = []

    class MM:
        async def finalize(self, run, payload):
            finalize_calls.append((run.run_id, run.state))

        async def finalize_mapper(self, run, payload):
            finalize_calls.append(("mapper", run.run_id))

    mgr.set_finalizer(MM())

    for state in ("finished", "failed", "timed_out", "orphaned"):
        run = _make_run(store, state=state, run_id=f"R-{state}")
        run_coro(mgr.handle(run.run_id, "run.artifacts",
                            {"result": {"outcome": "executed"}}))
    assert finalize_calls == []


def test_artifacts_enters_finalize_from_running(tmp_path):
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    mgr = RunManager(store, messaging, FakeExecutor())
    calls = []

    class MM:
        async def finalize(self, run, payload):
            calls.append(run.state)

        async def finalize_mapper(self, run, payload):
            pass

    mgr.set_finalizer(MM())
    run = _make_run(store, state="running")
    run_coro(mgr.handle(run.run_id, "run.artifacts",
                        {"result": {"outcome": "executed"}}))
    assert calls == ["finalizing"]
    assert store.get(run.run_id).state == "finalizing"


def test_kill_teardown_when_stop_raises(tmp_path):
    """ISSUES #3: ACL + terminal state even if executor.stop raises."""
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor(stop_raises=True)
    mgr = RunManager(store, messaging, executor)
    # prevent OO shipping side effects
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(store, state="running")
    run_coro(mgr.kill(run, "timed_out", "watchdog: timeout"))
    assert messaging.deleted_users == [run.run_id]
    assert messaging.deleted_streams == [run.run_id]
    saved = store.get(run.run_id)
    assert saved.state == "timed_out"
    assert saved.ended_at is not None


def test_kill_teardown_when_ship_failure_raises(tmp_path):
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor()
    mgr = RunManager(store, messaging, executor)

    async def boom(*a, **k):
        raise RuntimeError("oo down")

    mgr._ship_failure = boom  # type: ignore[method-assign]
    run = _make_run(store, state="running")
    run_coro(mgr.kill(run, "failed", "dagu dead"))
    assert messaging.deleted_users == [run.run_id]
    assert store.get(run.run_id).state == "failed"


def test_watchdog_timeout_kills(tmp_path, monkeypatch):
    """ISSUES #26: watchdog_loop calls kill on aged runs."""
    from devcake.domain import watchdog as wd

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor()
    mgr = RunManager(store, messaging, executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="running",
        created_at=utcnow() - timedelta(hours=3),
        timeout_seconds=60,
    )
    # One cycle then stop
    cycles = {"n": 0}

    async def fake_sleep(_):
        cycles["n"] += 1
        if cycles["n"] >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run_coro(wd.watchdog_loop(mgr))
    assert store.get(run.run_id).state == "timed_out"


def test_github_merge_already_merged_is_success(monkeypatch):
    """ISSUES #6: merge() treats already-merged as success."""
    forge = GitHubForge("https://github.com/o/r", "tok")

    async def fake_req(method, path, **kwargs):
        if method == "PUT":
            raise ForgeError("already merged", status=405)
        if method == "GET" and path.startswith("/pulls/"):
            return {"merged": True, "state": "closed", "html_url": "https://x/1",
                    "number": 1}
        raise AssertionError(f"unexpected {method} {path}")

    forge._req = fake_req  # type: ignore[method-assign]
    run_coro(forge.merge(1))  # must not raise


def test_recon_orphans_dead_runs_but_leaves_finalizing_for_reclaim(tmp_path):
    """ISSUES #2/#26: reconciliation kills dead-Dagu runs to orphaned, leaves
    finalizing runs alone, adopts live ones, and reclaims AFTER the orphan
    pass — exercised through the real reconcile_runs."""
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor()  # status() returns None → dagu run dead
    mgr = RunManager(store, messaging, executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]

    finalizing = _make_run(store, state="finalizing", run_id="F-1")
    dead = _make_run(store, state="running", run_id="R-1")

    events = []
    orig_kill = mgr.kill

    async def spy_kill(run, new_state, reason):
        events.append(("kill", run.run_id, new_state))
        await orig_kill(run, new_state, reason)

    mgr.kill = spy_kill  # type: ignore[method-assign]

    async def reclaim(handler, verify_auth):
        events.append(("reclaim",))

    messaging.reclaim_pending = reclaim
    run_coro(reconcile_runs(mgr))

    assert store.get(dead.run_id).state == "orphaned"
    assert store.get(finalizing.run_id).state == "finalizing"  # left for reclaim
    # ordering contract: every orphan kill happens BEFORE reclaim
    assert events[-1] == ("reclaim",)
    assert ("kill", "R-1", "orphaned") in events


def test_recon_adopts_live_runs_and_enriches_exit13(tmp_path):
    """A live Dagu run is adopted untouched; a dead one whose step errors carry
    exit status 13 gets the enriched DEV_FORGE error on the run record."""
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()

    class Executor(FakeExecutor):
        async def status(self, rid):
            if rid == "LIVE-1":
                return {"dagRunDetails": {"status": "running",
                                          "statusLabel": "running"}}
            return {"dagRunDetails": {"status": "failed",
                                      "statusLabel": "failed"}}

        async def node_errors(self, rid):
            return [{"step": "run_dev", "status": "failed",
                     "error": "exit status 13: clone failed"}]

    executor = Executor()
    mgr = RunManager(store, messaging, executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]

    class MM:
        def dev_failure_error(self, run, payload):
            assert payload["exit_code"] == 13
            return "DEV_FORGE: clone failed"

    live = _make_run(store, state="running", run_id="LIVE-1")
    dead = _make_run(store, state="running", run_id="DEAD-1")

    async def reclaim(handler, verify_auth):
        pass

    messaging.reclaim_pending = reclaim
    mgr.set_finalizer(MM())
    run_coro(reconcile_runs(mgr))

    assert store.get(live.run_id).state == "running"     # adopted
    saved = store.get(dead.run_id)
    assert saved.state == "orphaned"
    assert saved.error == "DEV_FORGE: clone failed"      # exit-13 enrichment


def test_recon_reclaims_even_when_a_kill_blows_up(tmp_path):
    """A raising executor.status/kill must not stop reconciliation — the other
    runs are still processed and reclaim still happens."""
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()

    class Executor(FakeExecutor):
        async def status(self, rid):
            if rid == "BOOM-1":
                raise RuntimeError("dagu unreachable")
            return None

    executor = Executor()
    mgr = RunManager(store, messaging, executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    _make_run(store, state="running", run_id="BOOM-1")
    other = _make_run(store, state="running", run_id="OK-1")

    reclaimed = []

    async def reclaim(handler, verify_auth):
        reclaimed.append(True)

    messaging.reclaim_pending = reclaim
    run_coro(reconcile_runs(mgr))
    assert store.get(other.run_id).state == "orphaned"   # still processed
    assert reclaimed == [True]                           # reclaim still ran


def _one_watchdog_cycle(mgr, monkeypatch):
    from devcake.domain import watchdog as wd

    cycles = {"n": 0}

    async def fake_sleep(_):
        cycles["n"] += 1
        if cycles["n"] >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run_coro(wd.watchdog_loop(mgr))


def test_watchdog_never_timeouts_resumable_finalizing(tmp_path, monkeypatch):
    """finalizing runs whose artifacts entry can still be redelivered must not
    be wall-clock-killed, however old (strand risk after crash+reclaim)."""
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    mgr = RunManager(store, messaging, FakeExecutor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="finalizing",
        created_at=utcnow() - timedelta(hours=5),
        timeout_seconds=60,
    )
    messaging.unresolved = {run.run_id}  # entry still on the ingress stream
    _one_watchdog_cycle(mgr, monkeypatch)
    assert store.get(run.run_id).state == "finalizing"
    assert messaging.deleted_users == []


def test_watchdog_fails_wedged_finalizing(tmp_path, monkeypatch):
    """A finalizing run past the stall deadline whose artifacts entry is gone
    (poison dead-lettered / lost) can never be resumed — it must be failed so
    it stops blocking its mission and holding a concurrency slot."""
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    mgr = RunManager(store, messaging, FakeExecutor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="finalizing",
        created_at=utcnow() - timedelta(hours=5),
        timeout_seconds=60,
    )
    _one_watchdog_cycle(mgr, monkeypatch)  # messaging.unresolved is empty
    saved = store.get(run.run_id)
    assert saved.state == "failed"
    assert "finalize stalled" in saved.error
    assert messaging.deleted_users == [run.run_id]


def test_watchdog_leaves_fresh_finalizing_alone(tmp_path, monkeypatch):
    """Within the stall deadline, finalizing runs are untouched even if no
    ingress entry is visible (finalize may be executing right now)."""
    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    mgr = RunManager(store, messaging, FakeExecutor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="finalizing",
        created_at=utcnow() - timedelta(minutes=5),
        timeout_seconds=7200,
    )
    _one_watchdog_cycle(mgr, monkeypatch)
    assert store.get(run.run_id).state == "finalizing"
    assert messaging.deleted_users == []


def test_checkpoint_skips_without_unawaited_coro(tmp_path):
    """_checkpoint must accept a callable — redelivery must not spawn orphan coros."""
    from devcake.config import AppConfig, DevType
    from devcake.domain.orchestrator import MissionManager
    from devcake.domain.model import Mission
    from datetime import datetime, timezone

    store = RunStore(tmp_path / "runs")
    run = _make_run(store, state="finalizing")
    run.finalized_steps = ["already"]
    store.save(run)

    mgr = MissionManager.__new__(MissionManager)
    mgr.runs = type("R", (), {"store": store})()
    calls = []

    async def side():
        calls.append(1)

    run_coro(mgr._checkpoint(run, "already", side))
    assert calls == []  # skipped; side never invoked


def test_human_needed_baton_posted_once(tmp_path):
    """Redelivery after transition:human_needed must not re-feed the baton."""
    from datetime import datetime, timezone
    from devcake.config import AppConfig, DevType
    from devcake.domain.orchestrator import MissionManager
    from devcake.domain.model import Mission, LABEL_EXECUTE

    m = Mission(
        pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
        status="in_progress", labels={LABEL_EXECUTE},
        updated_at=datetime.now(timezone.utc),
    )
    comments = []

    class FakePMO:
        async def get(self, ref):
            return m

        async def post_feed(self, ref, markdown):
            comments.append(markdown)

        async def swap_labels(self, ref, remove, add):
            m.labels = (m.labels - set(remove)) | set(add)

        async def set_status(self, ref, status):
            m.status = status

    store = RunStore(tmp_path / "runs")

    class Runs:
        pass
    runs = Runs()
    runs.store = store

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = 'linear'
    mgr.instance = PMOInstance(name='linear', team_key='DEV')
    mgr.config = AppConfig()
    mgr.dev_types = {}
    mgr.pmo = FakePMO()
    from types import SimpleNamespace
    mgr.forges = FakeForgeRuntime(SimpleNamespace(descriptor=SimpleNamespace(pr_noun="pull request")))
    mgr.runs = runs
    mgr.messaging = FakeMessaging()
    mgr._grace = set()
    mgr._grace_next = set()
    mgr.breakers = {}
    mgr.merge_handoffs = {}
    mgr._merge_window_closed = set()
    mgr.needs_human = {}
    mgr._audit = lambda *a, **k: None

    run = Run(
        run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="main-dev", seq=1,
        state="finalizing", stage_label_at_dispatch=LABEL_EXECUTE,
    )
    store.save(run)
    result = {"outcome": "human_needed", "summary": "stuck on secrets"}
    run_coro(mgr._transition(run, result, None))
    assert sum(1 for c in comments if "needs a human" in c.lower()
               or "DevCake needs a human" in c) == 1
    # redelivery: checkpoint skips baton
    run_coro(mgr._transition(run, result, None))
    assert sum(1 for c in comments if "needs a human" in c.lower()
               or "DevCake needs a human" in c) == 1
    assert "transition:human_needed" in run.finalized_steps


def test_redelivery_own_label_swap_is_not_external_transition(tmp_path):
    """A crash between a checkpointed label swap and the coarse `transition`
    marker must resume the transition on redelivery — not misread DevCake's
    own EXECUTE→REVIEW swap as an external change and post a skip comment."""
    from datetime import datetime, timezone
    from unittest.mock import AsyncMock
    from devcake.config import AppConfig
    from devcake.domain.orchestrator import MissionManager
    from devcake.domain.model import Mission, LABEL_EXECUTE, LABEL_REVIEW

    # the previous delivery already swapped the stage label…
    m = Mission(
        pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
        status="in_progress", labels={LABEL_REVIEW},
        updated_at=datetime.now(timezone.utc),
    )
    comments = []

    class FakePMO:
        async def get(self, ref):
            return m

        async def post_feed(self, ref, markdown):
            comments.append(markdown)

        async def swap_labels(self, ref, remove, add):
            m.labels = (m.labels - set(remove)) | set(add)

        async def set_status(self, ref, status):
            m.status = status

    store = RunStore(tmp_path / "runs")

    class Runs:
        pass
    runs = Runs()
    runs.store = store

    mgr = MissionManager.__new__(MissionManager)
    mgr.instance_name = 'linear'
    mgr.instance = PMOInstance(name='linear', team_key='DEV')
    mgr.config = AppConfig()
    mgr.dev_types = {}
    mgr.pmo = FakePMO()
    from types import SimpleNamespace
    mgr.forges = FakeForgeRuntime(SimpleNamespace(descriptor=SimpleNamespace(pr_noun="pull request")))
    mgr.runs = runs
    mgr.messaging = FakeMessaging()
    mgr._grace = set()
    mgr._grace_next = set()
    mgr.breakers = {}
    mgr.merge_handoffs = {}
    mgr._merge_window_closed = set()
    mgr.needs_human = {}
    mgr._audit = lambda *a, **k: None
    mgr._flag_out_of_pipeline_merge = AsyncMock()

    run = Run(
        run_id="T-1-1-EXECUTE-BBBBBB", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="main-dev", seq=1,
        state="finalizing", stage_label_at_dispatch=LABEL_EXECUTE,
        # …and checkpointed it before the crash
        finalized_steps=["transition:executed:labels"],
    )
    store.save(run)
    result = {"outcome": "executed", "pr_url": "https://x/pr/1"}
    run_coro(mgr._transition(run, result, None))
    assert not any("changed externally" in c for c in comments)
    assert any("awaiting REVIEW" in c for c in comments)  # transition resumed
    assert "transition:executed" in run.finalized_steps
    # a genuinely external change (no own checkpoints) still skips
    comments.clear()
    m.labels = {LABEL_REVIEW}
    fresh = Run(
        run_id="T-1-2-EXECUTE-CCCCCC", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="main-dev", seq=2,
        state="finalizing", stage_label_at_dispatch=LABEL_EXECUTE,
    )
    store.save(fresh)
    run_coro(mgr._transition(fresh, result, None))
    assert any("changed externally" in c for c in comments)
    # non-swap checkpoints (feeds, pr comments) must NOT suppress the check:
    # a human canceling the mission between deliveries still halts the resume
    # (the over-broad prefix guard would have merged a canceled mission's PR)
    comments.clear()
    m.labels = set()          # human removed the stage label (cancel)
    canceled = Run(
        run_id="T-1-3-EXECUTE-DDDDDD", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="main-dev", seq=3,
        state="finalizing", stage_label_at_dispatch=LABEL_EXECUTE,
        finalized_steps=["transition:executed:feed"],  # feed is not a swap
    )
    store.save(canceled)
    run_coro(mgr._transition(canceled, result, None))
    assert any("changed externally" in c for c in comments)
    assert "transition:executed" not in canceled.finalized_steps


def test_watchdog_stall_kill_rechecks_store_state(tmp_path, monkeypatch):
    """TOCTOU guard: if finalize completes while the watchdog walks the
    ingress stream, the stalled snapshot must NOT be killed — a finished run
    would be clobbered to failed."""
    store = RunStore(tmp_path / "runs")

    class RacingMessaging(FakeMessaging):
        def __init__(self, store):
            super().__init__()
            self._store = store
            self.finish_during_walk = None

        async def unresolved_run_ids(self):
            if self.finish_during_walk:
                r = self._store.get(self.finish_during_walk)
                r.state = "finished"
                self._store.save(r)   # finalize wins the race mid-walk
            return set()

    messaging = RacingMessaging(store)
    mgr = RunManager(store, messaging, FakeExecutor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="finalizing",
        created_at=utcnow() - timedelta(hours=5),
        timeout_seconds=60,
    )
    messaging.finish_during_walk = run.run_id
    _one_watchdog_cycle(mgr, monkeypatch)
    assert store.get(run.run_id).state == "finished"   # not clobbered
    assert messaging.deleted_users == []
