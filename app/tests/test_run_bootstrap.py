"""RunBootstrap seam (docs/04 §3.1): durable intent before trigger.

Every dispatch flavor must create the per-run ACL user, persist the Run with
an auth digest, then start the executor — never the reverse. Tests exercise
the public launch interface only, via in-memory port adapters.
"""

from __future__ import annotations

import asyncio

import pytest

from devcake.domain.run import Run, auth_digest
from devcake.domain.run_bootstrap import RunBootstrap


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


class InMemoryStore:
    """StatePort adapter for tests — dict-backed, no filesystem.

    Implements wipe_generation + save guard (docs/10) so launch/clear tests
    exercise the same contract as RunStore.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self.wipe_generation: int = 0

    def save(self, run: Run) -> None:
        gen = int(getattr(run, "store_gen", 0) or 0)
        if gen < self.wipe_generation:
            return
        self._runs[run.run_id] = run.model_copy(deep=True)

    def get(self, run_id: str) -> Run | None:
        r = self._runs.get(run_id)
        return r.model_copy(deep=True) if r else None

    def all(self) -> list[Run]:
        return [r.model_copy(deep=True) for r in self._runs.values()]

    def active(self) -> list[Run]:
        return [r for r in self.all()
                if r.state in ("dispatched", "running", "finalizing")]

    def clear(self) -> int:
        self.wipe_generation += 1
        n = len(self._runs)
        self._runs.clear()
        return n


class FakeMessaging:
    """MessagingPort adapter: fixed password so auth_digest is independently known."""

    PASSWORD = "test-run-password-known"

    async def create_run_user(self, run_id: str) -> str:
        self.last_run_id = run_id
        return self.PASSWORD

    async def delete_run_user(self, run_id: str) -> None:
        pass

    async def delete_reply_stream(self, run_id: str) -> None:
        pass

    async def reply(self, run_id: str, kind: str, payload: dict) -> None:
        pass


class FakeExecutor:
    """ExecutorPort adapter that fails if the run is not already durable."""

    def __init__(self, store: InMemoryStore) -> None:
        self.store = store
        self.starts: list[tuple[dict[str, str], str]] = []

    async def start(self, params: dict[str, str], dag_run_id: str) -> None:
        # The invariant under test: durable intent BEFORE the trigger.
        if self.store.get(dag_run_id) is None:
            raise AssertionError(
                f"executor.start called before store.save for {dag_run_id}"
            )
        self.starts.append((dict(params), dag_run_id))


def _hello_run(run_id: str = "HELLO-1-HELLO-AAAAAA") -> Run:
    return Run(
        run_id=run_id,
        mission_key="HELLO",
        mission_type="HELLO",
        dev_type="hello-stub",
        seq=1,
        timeout_seconds=180,
        traceparent="00-abc-def-01",
        spec_env={"DEVCAKE_MISSION_TYPE": "HELLO"},
    )


def test_launch_persists_run_before_executor_start():
    store = InMemoryStore()
    messaging = FakeMessaging()
    executor = FakeExecutor(store)
    bootstrap = RunBootstrap(store, messaging, executor)

    run = _hello_run()
    launched = run_coro(bootstrap.launch(run, image="devcake/dev-hello:latest"))

    assert len(executor.starts) == 1
    assert store.get(run.run_id) is not None
    assert launched.run_id == run.run_id


def test_launch_sets_auth_digest_from_acl_password():
    store = InMemoryStore()
    bootstrap = RunBootstrap(store, FakeMessaging(), FakeExecutor(store))

    run = _hello_run()
    launched = run_coro(bootstrap.launch(run, image="devcake/dev-hello:latest"))

    expected = auth_digest(FakeMessaging.PASSWORD)
    assert launched.auth_digest == expected
    assert store.get(run.run_id).auth_digest == expected


def test_launch_passes_image_and_redis_acl_to_executor():
    store = InMemoryStore()
    executor = FakeExecutor(store)
    bootstrap = RunBootstrap(store, FakeMessaging(), executor)

    run = _hello_run("HELLO-2-HELLO-BBBBBB")
    run_coro(bootstrap.launch(run, image="devcake/dev-hello:latest"))

    params, dag_run_id = executor.starts[0]
    assert dag_run_id == "HELLO-2-HELLO-BBBBBB"
    assert params["RUN_ID"] == "HELLO-2-HELLO-BBBBBB"
    assert params["IMAGE"] == "devcake/dev-hello:latest"
    assert params["REDIS_USER"] == "dev-HELLO-2-HELLO-BBBBBB"
    assert params["REDIS_PASSWORD"] == FakeMessaging.PASSWORD
    assert params["TRACEPARENT"] == "00-abc-def-01"


def test_launch_makes_run_retrievable_and_active():
    store = InMemoryStore()
    bootstrap = RunBootstrap(store, FakeMessaging(), FakeExecutor(store))

    run = _hello_run()
    run_coro(bootstrap.launch(run, image="devcake/dev-hello:latest"))

    saved = store.get(run.run_id)
    assert saved is not None
    assert saved.state == "dispatched"
    assert any(r.run_id == run.run_id for r in store.active())


def test_launch_serializes_on_dispatch_lock():
    """Re-audit #0/#6: clear-runs holds bootstrap.dispatch_lock across its wipe
    so NO dispatch flavor (poll/oauth/mapper/hello) can create an ACL user or
    start a container mid-wipe. Prove launch actually blocks on the lock."""
    async def scenario():
        store = InMemoryStore()
        messaging = FakeMessaging()
        bootstrap = RunBootstrap(store, messaging, FakeExecutor(store))
        await bootstrap.dispatch_lock.acquire()        # clear-runs holds it
        task = asyncio.ensure_future(
            bootstrap.launch(_hello_run(), image="devcake/dev-hello:latest"))
        await asyncio.sleep(0)                          # let launch try to run
        # blocked: no ACL user created, no executor start, nothing persisted
        assert not hasattr(messaging, "last_run_id")
        assert store.all() == []
        bootstrap.dispatch_lock.release()              # wipe done
        await task
        assert len(store.all()) == 1                    # now it proceeds
    asyncio.new_event_loop().run_until_complete(scenario())


def test_launch_stamps_store_gen_and_survives_prior_wipe():
    """After clear bumps wipe_generation, a new launch must stamp store_gen
    to the current generation and persist; pre-wipe saves must not resurrect."""
    store = InMemoryStore()
    bootstrap = RunBootstrap(store, FakeMessaging(), FakeExecutor(store))
    old = run_coro(bootstrap.launch(_hello_run("HELLO-OLD"), image="img"))
    assert old.store_gen == 0
    assert store.get(old.run_id) is not None

    store.clear()
    assert store.wipe_generation == 1
    assert store.all() == []
    # in-memory pre-wipe object must not resurrect after clear
    old.state = "finished"
    store.save(old)
    assert store.get(old.run_id) is None

    fresh = run_coro(bootstrap.launch(_hello_run("HELLO-NEW"), image="img"))
    assert fresh.store_gen == 1
    assert store.get(fresh.run_id) is not None
    assert store.get(fresh.run_id).store_gen == 1


def test_dispatch_hello_uses_bootstrap_spine():
    """Hello path (docs/04 §3.1): fields are hello-specific; spine is shared."""
    from devcake.domain.runs import HELLO_IMAGE, RunManager

    store = InMemoryStore()
    executor = FakeExecutor(store)
    manager = RunManager(store, FakeMessaging(), executor)

    run = run_coro(manager.dispatch_hello(sleep=1, payload_kb=2, timeout_seconds=30))

    assert run.mission_type == "HELLO"
    assert run.spec_env["HELLO_SLEEP"] == "1"
    assert run.spec_env["HELLO_PAYLOAD_KB"] == "2"
    assert run.timeout_seconds == 30
    assert run.auth_digest == auth_digest(FakeMessaging.PASSWORD)
    assert store.get(run.run_id) is not None
    params, dag_run_id = executor.starts[0]
    assert dag_run_id == run.run_id
    assert params["IMAGE"] == HELLO_IMAGE
    assert params["REDIS_PASSWORD"] == FakeMessaging.PASSWORD


def test_dispatch_mapper_uses_bootstrap_spine(tmp_path, monkeypatch):
    """Mapper path: MAPPER fields stay in MissionManager; spine is RunBootstrap."""
    from datetime import datetime, timezone

    from devcake.adapters.github import GitHubForge
    from devcake.config import AppConfig, DevType
    from devcake.domain.model import Mission
    from devcake.domain.runs import RunManager
    from devcake.harness import HARNESSES
    from fakes import make_mission_manager
    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    store = InMemoryStore()
    executor = FakeExecutor(store)
    runs = RunManager(store, FakeMessaging(), executor)
    mgr = make_mission_manager(
        runs=runs, messaging=FakeMessaging(),
        forge=GitHubForge("https://github.com/o/r", "tok"),
        config=AppConfig(),
    )

    dt = DevType(name="senior-dev", harness_template="grok-build", model="grok-4.5")
    m = Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                status="backlog", labels={"DEVCAKE"},
                updated_at=datetime.now(timezone.utc))
    run = run_coro(mgr.dispatch_mapper(dt, [m]))

    assert run.auth_digest == auth_digest(FakeMessaging.PASSWORD)
    assert store.get(run.run_id) is not None
    params, _ = executor.starts[0]
    assert params["IMAGE"] == HARNESSES["grok-build"].image
    assert params["REDIS_PASSWORD"] == FakeMessaging.PASSWORD
    assert run.spec_env["DEVCAKE_MISSION_TYPE"] == "MAPPER"


def test_oauth_start_uses_bootstrap_spine(tmp_path, monkeypatch):
    """OAuth path: session snapshot stays in OAuthManager; spine is RunBootstrap."""
    from devcake.config import DevType
    from devcake.domain.oauth import OAuthManager
    from devcake.domain.runs import RunManager
    from devcake.harness import HARNESSES

    monkeypatch.setenv("DEVCAKE_DATA_DIR", str(tmp_path))
    store = InMemoryStore()
    executor = FakeExecutor(store)
    runs = RunManager(store, FakeMessaging(), executor)
    mgr = OAuthManager(runs, FakeMessaging(), {
        "main-dev": DevType(name="main-dev", harness_template="grok-build"),
    })
    result = run_coro(mgr._start_inner("main-dev"))
    run_id = result["run_id"]

    assert store.get(run_id) is not None
    assert store.get(run_id).auth_digest == auth_digest(FakeMessaging.PASSWORD)
    params, dag_run_id = executor.starts[0]
    assert dag_run_id == run_id
    assert params["IMAGE"] == HARNESSES["grok-build"].image


def test_artifacts_route_to_finalizer_for_mission_run():
    """RunFinalizer seam: mission runs finalize through the injected port."""
    from devcake.domain.runs import RunManager

    class FakeFinalizer:
        def __init__(self):
            self.finalized = []

        async def finalize(self, run, payload):
            self.finalized.append((run.run_id, payload.get("result")))

        async def finalize_mapper(self, run, payload):
            raise AssertionError("mapper path not expected")

        async def restore_after_failure(self, run):
            raise AssertionError("restore not expected")

        def runspec_secret_payload(self, run):
            return None

        async def activity_payload(self, pmo_id, kind="issue"):
            return {"activity_md": "", "attachments": []}

    store = InMemoryStore()
    manager = RunManager(store, FakeMessaging(), FakeExecutor(store))
    finalizer = FakeFinalizer()
    manager.set_finalizer(finalizer)

    run = Run(
        run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1", mission_type="EXECUTE",
        dev_type="main-dev", seq=1, mission_pmo_id="p1",
    )
    store.save(run)
    run_coro(manager.handle(run.run_id, "run.artifacts",
                            {"result": {"outcome": "executed"}}))

    assert finalizer.finalized == [(run.run_id, {"outcome": "executed"})]
    # finalizer owns terminal state; ingress only moves to finalizing first
    assert store.get(run.run_id).state == "finalizing"


def test_artifacts_without_finalizer_use_pmo_less_path():
    """Hello / PMO-less runs finalize locally when no finalizer is bound."""
    from devcake.domain.runs import RunManager

    store = InMemoryStore()
    manager = RunManager(store, FakeMessaging(), FakeExecutor(store))
    run = Run(
        run_id="HELLO-1-HELLO-AAAAAA", mission_key="HELLO", mission_type="HELLO",
        dev_type="hello-stub", seq=1,
    )
    store.save(run)
    run_coro(manager.handle(run.run_id, "run.artifacts",
                            {"result": {"outcome": "hello"}}))

    saved = store.get(run.run_id)
    assert saved.state == "finished"
    assert saved.result == {"outcome": "hello"}


def test_kill_restores_via_finalizer():
    from devcake.domain.runs import RunManager

    class FakeFinalizer:
        def __init__(self):
            self.restored = []

        async def finalize(self, run, payload): pass
        async def finalize_mapper(self, run, payload): pass
        async def restore_after_failure(self, run):
            self.restored.append(run.run_id)
        def runspec_secret_payload(self, run): return None
        async def activity_payload(self, pmo_id, kind="issue"):
            return {}

    class Exec:
        async def stop(self, rid): return True
        async def node_errors(self, rid): return []
        async def start(self, params, dag_run_id): pass

    class Msg(FakeMessaging):
        async def delete_run_user(self, rid): pass
        async def delete_reply_stream(self, rid): pass

    store = InMemoryStore()
    manager = RunManager(store, Msg(), Exec())
    finalizer = FakeFinalizer()
    manager.set_finalizer(finalizer)
    run = Run(
        run_id="T-1-1-ONBOARD-AAAAAA", mission_key="T-1", mission_type="ONBOARD",
        dev_type="senior-dev", seq=1, mission_pmo_id="p1",
    )
    store.save(run)
    run_coro(manager.kill(run, "timed_out", "exceeded"))

    assert finalizer.restored == [run.run_id]
    assert store.get(run.run_id).state == "timed_out"
