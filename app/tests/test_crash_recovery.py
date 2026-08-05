"""Crash-recovery spine (ISSUES #1–3, #6, #26): redelivery, kill teardown,
watchdog timeout, and merge already-merged honesty."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from devcake.config import PMOInstance
from devcake.domain.orchestrator import transitions
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


def test_kill_does_not_resurrect_a_concurrently_wiped_record(tmp_path):
    """Re-audit #31 #1/#2: a kill whose record was deleted by a concurrent
    clear-runs wipe (while kill was awaiting teardown) must NOT store.save it
    back — that recreated a phantom terminal run after 'start fresh'. The
    get()+save() guard in _kill_inner is atomic (no await between), so a gone
    record stays gone for EVERY killer path. Wipe generation also drops saves
    whose store_gen predates the clear."""
    store = RunStore(tmp_path / "runs")
    mgr = RunManager(store, FakeMessaging(), FakeExecutor())
    run = _make_run(store, state="running", run_id="W-1")
    # simulate the concurrent wipe: the record is gone by the time kill's
    # teardown reaches its final save
    store.clear()
    assert store.get("W-1") is None
    run_coro(mgr.kill(run, "timed_out", "watchdog timeout"))
    assert store.get("W-1") is None                    # not resurrected
    # a normal kill (record present) still persists the terminal state
    live = _make_run(store, state="running", run_id="W-2",
                     store_gen=store.wipe_generation)
    run_coro(mgr.kill(live, "failed", "operator"))
    assert store.get("W-2").state == "failed"


def test_kill_interleaved_with_clear_during_executor_stop(tmp_path):
    """Issue F: clear bumps wipe_generation while kill awaits executor.stop;
    the final save must not resurrect the record."""
    store = RunStore(tmp_path / "runs")
    gate = asyncio.Event()

    class GatedExecutor(FakeExecutor):
        async def stop(self, rid):
            self.stopped.append(rid)
            await gate.wait()
            return True

    mgr = RunManager(store, FakeMessaging(), GatedExecutor())
    run = _make_run(store, state="running", run_id="W-GATE")
    run.store_gen = 0
    store.save(run)

    async def scenario():
        task = asyncio.ensure_future(
            mgr.kill(run, "failed", "operator stop"))
        await asyncio.sleep(0)                # let kill reach gated stop
        store.clear()                         # concurrent wipe mid-kill
        assert store.get("W-GATE") is None
        gate.set()
        await task
        assert store.get("W-GATE") is None

    asyncio.new_event_loop().run_until_complete(scenario())


def test_finalize_does_not_resurrect_or_post_after_clear(tmp_path):
    """Residual A: in-flight finalize after clear must not recreate the run
    file or drive further PMO side effects."""
    from devcake.domain.orchestrator import finalize as fin_mod

    store = RunStore(tmp_path / "runs")
    mgr = RunManager(store, FakeMessaging(), FakeExecutor())
    posts: list[str] = []
    run = _make_run(store, state="running", run_id="F-1",
                    mission_pmo_id="pmo-1", store_gen=0)
    store.clear()
    assert store.get("F-1") is None

    class M:
        pass

    m = M()
    m.runs = mgr
    m.messaging = mgr.messaging

    async def _feed(pmo_id, kind, md, externalize=True):
        posts.append("feed")

    m._feed = _feed

    # Call the public finalize seam with the pre-wipe in-memory Run (the
    # race shape: clear unlinked the file while finalize still holds `run`).
    run_coro(fin_mod.finalize(m, run, {
        "result": {"outcome": "executed"},
        "transcript_md": "hello",
        "token_report": {"total_tokens": 1},
    }))
    assert store.get("F-1") is None
    assert posts == []


def test_started_does_not_resurrect_a_killed_run(tmp_path):
    """Audit D5 #10: a run.started arriving AFTER the run was killed (its
    container was just booting when stop-all fired) must NOT flip the record
    back to 'running' — it would re-enter store.active() and hold the
    mission's in-flight slot until the watchdog grace expires."""
    store = RunStore(tmp_path / "runs")
    mgr = RunManager(store, FakeMessaging(), FakeExecutor())
    for state in ("failed", "timed_out", "orphaned", "finished"):
        run = _make_run(store, state=state, run_id=f"S-{state}", started_at=None)
        run_coro(mgr.handle(run.run_id, "run.started", {}))
        assert store.get(run.run_id).state == state       # unchanged
        assert store.get(run.run_id).started_at is None
    # a genuinely dispatched run still starts normally
    live = _make_run(store, state="dispatched", run_id="S-live", started_at=None)
    run_coro(mgr.handle(live.run_id, "run.started", {}))
    assert store.get(live.run_id).state == "running"


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


def test_kill_aborts_when_finalize_claims_the_run_mid_kill(tmp_path):
    """2026-08 evaluation TOCTOU: _kill_inner's teardown awaits are yield
    points where finalize can claim or finish the run. The save must abort
    (the mover's terminal truth wins over a stale kill verdict), the passed
    record object must stay unmutated (it may be the store's shared parse
    cache), and restore_after_failure must NOT fire — the mover owns the
    mission transition."""
    store = RunStore(tmp_path / "runs")
    mgr = RunManager(store, FakeMessaging(), FakeExecutor())
    run = _make_run(store, state="running")

    async def flip_mid_kill(run_, new_state, reason):
        fresh = store.get(run.run_id)
        fresh.state = "finished"          # finalize completed during teardown
        store.save(fresh)

    mgr._ship_failure = flip_mid_kill  # type: ignore[method-assign]
    restored = []

    class SpyFinalizer:
        async def restore_after_failure(self, r):
            restored.append(r.run_id)

    mgr.finalizer = SpyFinalizer()
    run_coro(mgr.kill(run, "timed_out", "watchdog: timeout"))
    assert store.get(run.run_id).state == "finished"   # never overwritten
    assert run.state == "running"                      # snapshot unmutated
    assert restored == []                              # mover owns the mission


def test_watchdog_liveness_kill_disarmed_by_midprobe_finalize(tmp_path,
                                                              monkeypatch):
    """The heartbeat/startup branch awaits executor.status() before killing —
    a run that finalize claimed during that probe must be left alone (the
    same guard the stalled-finalize branch has always had)."""
    from devcake.domain import watchdog as wd

    store = RunStore(tmp_path / "runs")
    executor = FakeExecutor()               # status() → None ⇒ "dagu run dead"
    mgr = RunManager(store, FakeMessaging(), executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="running",
        created_at=utcnow() - timedelta(minutes=20),
        started_at=utcnow() - timedelta(minutes=20),
        last_heartbeat=utcnow() - timedelta(minutes=10),   # stale
        timeout_seconds=3600,                              # wall-clock not hit
    )

    async def status_flips_to_finalizing(rid):
        fresh = store.get(run.run_id)
        fresh.state = "finalizing"        # artifacts landed during the probe
        store.save(fresh)
        return None

    executor.status = status_flips_to_finalizing  # type: ignore[method-assign]

    async def one_cycle(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(wd.asyncio, "sleep", one_cycle)
    with pytest.raises(asyncio.CancelledError):
        run_coro(wd.watchdog_loop(mgr))
    assert store.get(run.run_id).state == "finalizing"     # not killed


def test_watchdog_liveness_kill_disarmed_by_fresh_heartbeat(tmp_path,
                                                            monkeypatch):
    """A first heartbeat that lands during the status() probe re-arms
    liveness: the fresh re-read must re-derive the verdict, not kill on the
    stale snapshot."""
    from devcake.domain import watchdog as wd

    store = RunStore(tmp_path / "runs")
    executor = FakeExecutor()
    mgr = RunManager(store, FakeMessaging(), executor)
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run = _make_run(
        store, state="running",
        created_at=utcnow() - timedelta(minutes=20),
        started_at=utcnow() - timedelta(minutes=20),
        last_heartbeat=utcnow() - timedelta(minutes=10),   # stale in snapshot
        timeout_seconds=3600,
    )

    async def status_and_heartbeat(rid):
        fresh = store.get(run.run_id)
        fresh.last_heartbeat = utcnow()   # Dev was alive all along
        store.save(fresh)
        return None

    executor.status = status_and_heartbeat  # type: ignore[method-assign]

    async def one_cycle(_):
        raise asyncio.CancelledError()

    monkeypatch.setattr(wd.asyncio, "sleep", one_cycle)
    with pytest.raises(asyncio.CancelledError):
        run_coro(wd.watchdog_loop(mgr))
    assert store.get(run.run_id).state == "running"        # not killed


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
            run.error_class = "DEV_FORGE"      # the port mutates (ADR-0018)
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


def test_recon_restamps_store_gen_for_adopted_and_finalizing(tmp_path):
    """PR #34 review follow-up: wipe_generation resets to 0 on process start,
    but run files may still carry store_gen from a prior process. Reconcile
    must restamp kept-alive runs so the first clear in THIS process treats
    them as pre-wipe (no PMO post / no resurrect after clear)."""
    from devcake.domain.orchestrator import finalize as fin_mod
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    assert store.wipe_generation == 0

    # Simulate runs written by a prior process that had cleared twice.
    live = _make_run(store, state="running", run_id="LIVE-OLD",
                     store_gen=2, mission_pmo_id="pmo-1")
    fin = _make_run(store, state="finalizing", run_id="FIN-OLD",
                    store_gen=5, mission_pmo_id="pmo-2")
    assert store.get(live.run_id).store_gen == 2
    assert store.get(fin.run_id).store_gen == 5

    class Executor(FakeExecutor):
        async def status(self, rid):
            return {"dagRunDetails": {"status": "running",
                                      "statusLabel": "running"}}

    messaging = FakeMessaging()

    async def reclaim(handler, verify_auth):
        pass

    messaging.reclaim_pending = reclaim
    mgr = RunManager(store, messaging, Executor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    run_coro(reconcile_runs(mgr))

    # Born into this process's generation (0 until first clear).
    assert store.get(live.run_id).store_gen == 0
    assert store.get(fin.run_id).store_gen == 0

    # First clear in this process → wipe_generation = 1; pre-wipe catches
    # restamped runs so finalize cannot resurrect or post.
    posts: list[str] = []
    store.clear()
    assert store.wipe_generation == 1
    assert store.get(live.run_id) is None

    class M:
        pass

    m = M()
    m.runs = mgr
    m.messaging = mgr.messaging

    async def _feed(pmo_id, kind, md, externalize=True):
        posts.append("feed")

    m._feed = _feed
    # In-memory object still holds the restamped store_gen=0 from reconcile.
    live.store_gen = 0
    run_coro(fin_mod.finalize(m, live, {
        "result": {"outcome": "executed"},
        "transcript_md": "should not land",
        "token_report": {"total_tokens": 1},
    }))
    assert store.get(live.run_id) is None
    assert posts == []


def test_recon_enriches_exit14_mcp_setup(tmp_path):
    """Same enrichment for the other classified pre-harness exit: a dead run
    whose step errors carry exit status 14 (MCP setup failed while the app
    was down) gets the DEV_MCP_SETUP error instead of the generic orphan
    reason."""
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()

    class Executor(FakeExecutor):
        async def status(self, rid):
            return {"dagRunDetails": {"status": "failed",
                                      "statusLabel": "failed"}}

        async def node_errors(self, rid):
            return [{"step": "run_dev", "status": "failed",
                     "error": "exit status 14: mcp setup failed"}]

    mgr = RunManager(store, messaging, Executor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]

    class MM:
        def dev_failure_error(self, run, payload):
            assert payload["exit_code"] == 14
            run.error_class = "DEV_MCP_SETUP"  # the port mutates (ADR-0018)
            return "DEV_MCP_SETUP: claude mcp add …: exit 1"

    dead = _make_run(store, state="running", run_id="DEAD-14")

    async def reclaim(handler, verify_auth):
        pass

    messaging.reclaim_pending = reclaim
    mgr.set_finalizer(MM())
    run_coro(reconcile_runs(mgr))
    saved = store.get(dead.run_id)
    assert saved.state == "orphaned"
    assert saved.error == "DEV_MCP_SETUP: claude mcp add …: exit 1"


@pytest.mark.parametrize("code,recovered", [
    (10, True), (11, True), (20, True), (12, False)])
def test_recon_enrichment_regex_membership(tmp_path, code, recovered):
    """ADR-0027 gap-closure pin, taken BEFORE the regex became a table
    derivation: the informational exits 10/11/20 ARE recovered from Dagu's
    post-mortem string (AUD-015), and exit 12 is REFUSED — dev_failure_error
    latches the dev-type auth breaker for 12, and a stale orphan post-mortem
    must never trip a breaker from reconcile. The 13/14 arms are pinned by
    the two tests above; membership itself was previously untested."""
    from devcake.domain.reconcile import reconcile_runs

    store = RunStore(tmp_path / "runs")
    messaging = FakeMessaging()

    class Executor(FakeExecutor):
        async def status(self, rid):
            return {"dagRunDetails": {"status": "failed",
                                      "statusLabel": "failed"}}

        async def node_errors(self, rid):
            return [{"step": "run_dev", "status": "failed",
                     "error": f"exit status {code}: boom"}]

    mgr = RunManager(store, messaging, Executor())
    mgr._ship_failure = AsyncMock()  # type: ignore[method-assign]
    seen = []

    class MM:
        def dev_failure_error(self, run, payload):
            seen.append(payload["exit_code"])
            return f"enriched exit {payload['exit_code']}"

    dead = _make_run(store, state="running", run_id=f"DEAD-{code}")

    async def reclaim(handler, verify_auth):
        pass

    messaging.reclaim_pending = reclaim
    mgr.set_finalizer(MM())
    run_coro(reconcile_runs(mgr))
    saved = store.get(dead.run_id)
    assert saved.state == "orphaned"
    assert (seen == [code]) is recovered, (
        f"exit {code}: enrichment {'expected' if recovered else 'FORBIDDEN'}")


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

    from fakes import make_mission_manager
    mgr = make_mission_manager(
        runs=type("R", (), {"store": store})(),
        noop_audit=False,
    )
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
    runs = type("Runs", (), {"store": store})()

    from fakes import make_mission_manager
    from types import SimpleNamespace
    mgr = make_mission_manager(
        pmo=FakePMO(),
        forge=SimpleNamespace(descriptor=SimpleNamespace(pr_noun="pull request")),
        config=AppConfig(),
        dev_types={},
        runs=runs,
        messaging=FakeMessaging(),
    )

    run = Run(
        run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1", mission_pmo_id="p1",
        mission_type="EXECUTE", dev_type="main-dev", seq=1,
        state="finalizing", stage_label_at_dispatch=LABEL_EXECUTE,
    )
    store.save(run)
    result = {"outcome": "human_needed", "summary": "stuck on secrets"}
    run_coro(transitions.transition(mgr, run, result, None))
    assert sum(1 for c in comments if "needs a human" in c.lower()
               or "DevCake needs a human" in c) == 1
    # redelivery: checkpoint skips baton
    run_coro(transitions.transition(mgr, run, result, None))
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
    runs = type("Runs", (), {"store": store})()

    from fakes import make_mission_manager
    from types import SimpleNamespace
    mgr = make_mission_manager(
        pmo=FakePMO(),
        forge=SimpleNamespace(descriptor=SimpleNamespace(pr_noun="pull request")),
        config=AppConfig(),
        dev_types={},
        runs=runs,
        messaging=FakeMessaging(),
    )
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
    run_coro(transitions.transition(mgr, run, result, None))
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
    run_coro(transitions.transition(mgr, fresh, result, None))
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
    run_coro(transitions.transition(mgr, canceled, result, None))
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
