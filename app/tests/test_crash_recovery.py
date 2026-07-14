"""Crash-recovery spine (ISSUES #1–3, #6, #26): redelivery, kill teardown,
watchdog timeout, and merge already-merged honesty."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

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

    mgr.mission_mgr = MM()

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

    mgr.mission_mgr = MM()
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


def test_recon_skips_finalizing():
    """Documented contract: finalizing runs are left for reclaim (ISSUES #2).
    The lifespan code is integration-heavy; assert the store.active() contract
    and the intended filter used by main.lifespan."""
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        store = RunStore(Path(d))
        _make_run(store, state="finalizing", run_id="F-1")
        _make_run(store, state="running", run_id="R-1")
        active = store.active()
        finalizing = [r for r in active if r.state == "finalizing"]
        others = [r for r in active if r.state != "finalizing"]
        assert len(finalizing) == 1
        assert len(others) == 1
        # recon should only kill `others`
        assert others[0].run_id == "R-1"


def test_watchdog_never_timeouts_finalizing(tmp_path, monkeypatch):
    """finalizing runs must not be wall-clock-killed (strand risk after reclaim)."""
    from devcake.domain import watchdog as wd

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()
    executor = FakeExecutor()
    mgr = RunManager(store, messaging, executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="finalizing",
        created_at=utcnow() - timedelta(hours=5),
        timeout_seconds=60,
    )
    cycles = {"n": 0}

    async def fake_sleep(_):
        cycles["n"] += 1
        if cycles["n"] >= 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(wd.asyncio, "sleep", fake_sleep)
    with pytest.raises(asyncio.CancelledError):
        run_coro(wd.watchdog_loop(mgr))
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
    mgr.config = AppConfig()
    mgr.dev_types = {}
    mgr.pmo = FakePMO()
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
