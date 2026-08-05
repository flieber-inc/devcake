"""ADR-0025 — per-run workspace lifecycle.

WorkspaceStore filesystem behavior (create/sentinel, cleanup, sweep fences,
wipe), the RunManager hooks (A: post-finalize, B: kill teardown), the
hardened run.started transition, the phase-scoped runspec (R1), and the
launch ordering (Hook C: save → create → start).
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from devcake.domain.run import Run
from devcake.domain.runs import RunManager, provision_runspec_reply
from devcake.domain.workspaces import (
    SENTINEL_REL,
    SWEEP_AGE_SECONDS,
    NullWorkspaceStore,
    WorkspaceStore,
    WorkspaceUnavailable,
)

# Shared module loop — NEVER asyncio.run(): it closes+unsets the loop and
# poisons the legacy get_event_loop suites in the same process (ADR-0024
# gotcha, recorded in test_repo_mirror.py).
_LOOP = asyncio.new_event_loop()


def run_coro(c):
    return _LOOP.run_until_complete(c)


# ── minimal port fakes (test_run_bootstrap pattern) ──────────────────────────

class InMemoryStore:
    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self.wipe_generation: int = 0

    def save(self, run: Run) -> None:
        if int(getattr(run, "store_gen", 0) or 0) < self.wipe_generation:
            return
        self._runs[run.run_id] = run.model_copy(deep=True)

    def get(self, run_id: str) -> Run | None:
        r = self._runs.get(run_id)
        return r.model_copy(deep=True) if r else None

    def delete(self, run_id: str) -> None:
        self._runs.pop(run_id, None)

    def all(self) -> list[Run]:
        return [r.model_copy(deep=True) for r in self._runs.values()]

    def active(self) -> list[Run]:
        return [r for r in self.all()
                if r.state in ("dispatched", "running", "finalizing")]


class FakeMessaging:
    def __init__(self):
        self.replies: list[tuple[str, str, dict]] = []

    async def create_run_user(self, run_id):
        return "pw"

    async def delete_run_user(self, run_id):
        pass

    async def delete_reply_stream(self, run_id):
        pass

    async def delete_runspec_result(self, run_id):
        pass

    async def reply(self, run_id, kind, payload):
        self.replies.append((run_id, kind, payload))


class FakeExecutor:
    def __init__(self):
        self.starts = []

    async def start(self, params, dag_run_id):
        self.starts.append(dag_run_id)

    async def stop(self, dag_run_id):
        return True

    async def status(self, dag_run_id):
        return None

    async def node_errors(self, dag_run_id):
        return []


class RecordingWS:
    """WorkspaceStore stand-in that records every hook call."""

    def __init__(self):
        self.created: list[str] = []
        self.cleaned: list[str] = []
        self.volume_error = None

    def create(self, run_id):
        self.created.append(run_id)
        return Path("/workspaces") / run_id

    def cleanup(self, run_id):
        self.cleaned.append(run_id)
        return True

    def sweep(self, store):
        return 0

    def wipe_all(self):
        return 0

    def verify_writable(self):
        return None

    def leaked_count(self, store):
        return 0

    def disk_stats(self):
        return None


def _run(run_id="T-1-1-EXECUTE-AAAAAA", state="dispatched", **kw) -> Run:
    return Run(run_id=run_id, mission_key="T-1", mission_type="EXECUTE",
               dev_type="dev", seq=1, timeout_seconds=600, state=state,
               spec_env={"DEVCAKE_MISSION_TYPE": "EXECUTE"}, **kw)


def _mgr(store, ws=None) -> tuple[RunManager, FakeMessaging]:
    messaging = FakeMessaging()
    mgr = RunManager(store, messaging, FakeExecutor(), workspaces=ws)
    return mgr, messaging


# ── WorkspaceStore: create ───────────────────────────────────────────────────

def test_create_writes_0700_dir_and_run_id_sentinel(tmp_path):
    ws = WorkspaceStore(tmp_path)
    p = ws.create("R-1-1-EXECUTE-AAAAAA")
    assert p == tmp_path / "R-1-1-EXECUTE-AAAAAA"
    assert (p.stat().st_mode & 0o777) == 0o700
    assert ((p / ".devcake").stat().st_mode & 0o777) == 0o700
    assert (p / SENTINEL_REL).read_text() == "R-1-1-EXECUTE-AAAAAA"


def test_create_rejects_invalid_run_ids(tmp_path):
    ws = WorkspaceStore(tmp_path)
    for bad in ("", "short", "../escape", "a/b", "x" * 65, "spaced name"):
        # AUD-011: "short" (5 chars) is rejected — the fence is {6,64},
        # matching the DAG precondition and docs/02, not the old {1,64}
        with pytest.raises(ValueError):
            ws.create(bad)
    assert list(tmp_path.iterdir()) == []


def test_create_is_loud_on_collision(tmp_path):
    """exist_ok=False on purpose (R10): a pre-existing dir means a collision
    or a daemon-created husk — adopted silently it would defeat the
    sentinel."""
    ws = WorkspaceStore(tmp_path)
    ws.create("R-1-1-EXECUTE-AAAAAA")
    with pytest.raises(OSError):
        ws.create("R-1-1-EXECUTE-AAAAAA")


# ── WorkspaceStore: cleanup ──────────────────────────────────────────────────

def test_cleanup_removes_tree_including_readonly_content(tmp_path):
    ws = WorkspaceStore(tmp_path)
    p = ws.create("R-1-1-EXECUTE-AAAAAA")
    packs = p / "repo" / "r" / ".git" / "objects" / "pack"
    packs.mkdir(parents=True)
    f = packs / "pack-abc.pack"
    f.write_text("x")
    f.chmod(0o444)                       # git pack files are read-only
    assert ws.cleanup("R-1-1-EXECUTE-AAAAAA") is True
    assert not p.exists()


def test_cleanup_chmod_retries_through_unreadable_subdir(tmp_path):
    """A Dev-created mode-0000 subdir must not leak forever — same-uid
    chmod-and-retry clears it (R10)."""
    ws = WorkspaceStore(tmp_path)
    p = ws.create("R-1-1-EXECUTE-AAAAAA")
    locked = p / "locked"
    locked.mkdir()
    (locked / "inner.txt").write_text("x")
    locked.chmod(0o000)
    try:
        assert ws.cleanup("R-1-1-EXECUTE-AAAAAA") is True
        assert not p.exists()
    finally:
        if locked.exists():              # never leave tmp_path unremovable
            locked.chmod(0o700)


def test_cleanup_unlinks_symlink_without_following(tmp_path):
    ws = WorkspaceStore(tmp_path)
    target = tmp_path / "precious"
    target.mkdir()
    (target / "keep.txt").write_text("keep")
    link = tmp_path / "R-1-1-EXECUTE-AAAAAA"
    link.symlink_to(target)
    assert ws.cleanup("R-1-1-EXECUTE-AAAAAA") is True
    assert not link.exists()
    assert (target / "keep.txt").exists()


def test_cleanup_never_raises_on_garbage_ids(tmp_path):
    ws = WorkspaceStore(tmp_path)
    assert ws.cleanup("../..") is False
    assert ws.cleanup("") is False
    assert ws.cleanup("R-1-1-EXECUTE-GONE42") is False   # absent dir


# ── WorkspaceStore: sweep + wipe fences ──────────────────────────────────────

def _backdate(path: Path) -> None:
    old = path.stat().st_mtime - SWEEP_AGE_SECONDS - 60
    os.utime(path, (old, old))


def test_sweep_respects_charset_age_and_record_state(tmp_path):
    ws = WorkspaceStore(tmp_path)
    store = InMemoryStore()

    keeper = _run("R-1-1-EXECUTE-ACTIVE", state="running")
    store.save(keeper)
    ws.create("R-1-1-EXECUTE-ACTIVE")
    _backdate(tmp_path / "R-1-1-EXECUTE-ACTIVE")   # old but record active

    done = _run("R-1-1-EXECUTE-DONE00", state="finished")
    store.save(done)
    ws.create("R-1-1-EXECUTE-DONE00")
    _backdate(tmp_path / "R-1-1-EXECUTE-DONE00")   # old + terminal → goes

    ws.create("R-1-1-EXECUTE-NOREC0")              # recordless
    _backdate(tmp_path / "R-1-1-EXECUTE-NOREC0")   # old + no record → goes

    ws.create("R-1-1-EXECUTE-FRESH0")              # recordless but YOUNG

    operator = tmp_path / "not a run id!"          # charset fence
    operator.mkdir()
    _backdate(operator)

    stray = tmp_path / "R-1-1-EXECUTE-AFILE0"      # old non-dir, run-id shaped
    stray.write_text("x")
    _backdate(stray)

    removed = ws.sweep(store)
    assert removed == 3
    assert (tmp_path / "R-1-1-EXECUTE-ACTIVE").exists()
    assert (tmp_path / "R-1-1-EXECUTE-FRESH0").exists()
    assert operator.exists()
    assert not (tmp_path / "R-1-1-EXECUTE-DONE00").exists()
    assert not (tmp_path / "R-1-1-EXECUTE-NOREC0").exists()
    assert not stray.exists()


def test_leaked_count_matches_sweep_candidates(tmp_path):
    ws = WorkspaceStore(tmp_path)
    store = InMemoryStore()
    ws.create("R-1-1-EXECUTE-NOREC0")
    _backdate(tmp_path / "R-1-1-EXECUTE-NOREC0")
    ws.create("R-1-1-EXECUTE-FRESH0")
    assert ws.leaked_count(store) == 1


def test_wipe_all_ignores_age_and_records_but_keeps_charset_fence(tmp_path):
    ws = WorkspaceStore(tmp_path)
    store = InMemoryStore()
    store.save(_run("R-1-1-EXECUTE-ACTIVE", state="running"))
    ws.create("R-1-1-EXECUTE-ACTIVE")              # young AND active — goes:
    operator = tmp_path / "not a run id!"          # records are being wiped
    operator.mkdir()
    assert ws.wipe_all() == 1
    assert not (tmp_path / "R-1-1-EXECUTE-ACTIVE").exists()
    assert operator.exists()


# ── WorkspaceStore: probes ───────────────────────────────────────────────────

def test_verify_writable_ok_and_failure_names_the_fix(tmp_path):
    ws = WorkspaceStore(tmp_path / "base")
    ws.verify_writable()
    assert ws.volume_error is None

    ro_parent = tmp_path / "ro"
    ro_parent.mkdir()
    ro_parent.chmod(0o500)
    try:
        bad = WorkspaceStore(ro_parent / "base")
        bad.verify_writable()
        assert bad.volume_error is not None
        assert "chown -R 1000:1000" in bad.volume_error
    finally:
        ro_parent.chmod(0o700)


def test_disk_stats_shape_and_null_store_noops(tmp_path):
    ws = WorkspaceStore(tmp_path)
    stats = ws.disk_stats()
    assert set(stats) == {"total_bytes", "free_bytes"}

    null = NullWorkspaceStore()
    assert null.create("x") == Path("/workspaces/x")
    assert null.cleanup("x") is False
    assert null.sweep(None) == 0
    assert null.wipe_all() == 0
    assert null.leaked_count(None) == 0
    assert null.disk_stats() is None
    assert null.volume_error is None


# ── run.started hardening (dispatched-only) ──────────────────────────────────

def test_started_accepts_only_the_dispatched_transition():
    """ADR-0025: a replayed run.started must not overwrite started_at (the
    Runs-page runtime metric) and must not revert `finalizing` to `running`
    — the pre-existing latent bug this hardening pins shut."""
    store = InMemoryStore()
    mgr, _ = _mgr(store)

    live = _run("R-1-1-EXECUTE-LIVE00")
    store.save(live)
    run_coro(mgr.handle(live.run_id, "run.started", {}))
    first_started = store.get(live.run_id).started_at
    assert store.get(live.run_id).state == "running"
    assert first_started is not None

    run_coro(mgr.handle(live.run_id, "run.started", {}))   # replay
    assert store.get(live.run_id).started_at == first_started

    fin = _run("R-1-1-EXECUTE-FINAL0", state="finalizing")
    store.save(fin)
    run_coro(mgr.handle(fin.run_id, "run.started", {}))
    assert store.get(fin.run_id).state == "finalizing"     # NOT reverted


# ── phase-scoped runspec (R1) ────────────────────────────────────────────────

def test_provision_runspec_reply_is_secret_free_for_mirrored_work_repos():
    run = _run()
    run.spec_env = {"DEVCAKE_MISSION_TYPE": "EXECUTE",
                    "DEVCAKE_MIRROR_PATH": "/mirrors/r.git"}
    secret = {"env": {"DEVCAKE_FORGE_TOKEN": "tok",
                      "ANTHROPIC_API_KEY": "sk-secret"},
              "credential_files": [{"path_hint": "~/.x", "content": "S"}],
              "extra_repos": [{"name": "beta", "mirror_path": "/mirrors/b.git"}],
              "mcp_setup_commands": ["cmd $DD_API_KEY"],
              "activity_repo": {"url": "u", "token": "at"}}
    reply = provision_runspec_reply(run, secret)
    assert reply["env"] == run.spec_env                   # no token, no keys
    assert reply["credential_files"] == []
    assert reply["skills"] == [] and reply["skills_dir"] == ""
    assert reply["mcp_setup_commands"] == []
    assert reply["prompt"] == ""
    assert reply["extra_repos"] == secret["extra_repos"]  # provision clones them
    assert reply["activity_repo"] == secret["activity_repo"]


def test_provision_runspec_reply_serves_forge_token_only_for_direct_clones():
    run = _run()
    run.spec_env = {"DEVCAKE_MISSION_TYPE": "EXECUTE", "DEVCAKE_MIRROR_PATH": ""}
    secret = {"env": {"DEVCAKE_FORGE_TOKEN": "tok", "XAI_API_KEY": "x"}}
    reply = provision_runspec_reply(run, secret)
    assert reply["env"]["DEVCAKE_FORGE_TOKEN"] == "tok"   # direct clone needs it
    assert "XAI_API_KEY" not in reply["env"]


def test_runspec_get_phase_provision_serves_reduced_spec():
    """Wire-level pin via the HELLO secret (FAKE_SECRET + a credential
    file): provision gets neither; harness/no-phase gets both."""
    store = InMemoryStore()
    mgr, messaging = _mgr(store)
    run = Run(run_id="H-1-1-HELLO-AAAAAA", mission_key="HELLO",
              mission_type="HELLO", dev_type="hello-stub", seq=1,
              timeout_seconds=180, spec_env={"HELLO_SLEEP": "1"})
    store.save(run)

    run_coro(mgr.handle(run.run_id, "runspec.get", {"phase": "provision"}))
    _, kind, reduced = messaging.replies[-1]
    assert kind == "runspec.result"
    assert reduced["credential_files"] == []
    assert "FAKE_SECRET" not in reduced["env"]

    run_coro(mgr.handle(run.run_id, "runspec.get", {"phase": "harness"}))
    _, _, full = messaging.replies[-1]
    assert full["credential_files"]
    assert "FAKE_SECRET" in full["env"]

    run_coro(mgr.handle(run.run_id, "runspec.get", {}))   # defensive no-phase
    _, _, legacy = messaging.replies[-1]
    assert legacy["credential_files"]                      # → full spec
    assert "FAKE_SECRET" in legacy["env"]


# ── Hooks A + B ──────────────────────────────────────────────────────────────

def test_hook_a_cleans_workspace_after_terminal_finalize():
    store = InMemoryStore()
    ws = RecordingWS()
    mgr, _ = _mgr(store, ws)
    run = _run("R-1-1-EXECUTE-ART000", state="running")
    store.save(run)
    run_coro(mgr.handle(run.run_id, "run.artifacts",
                        {"result": {"outcome": "hello"}}))
    assert store.get(run.run_id).state == "finished"
    assert ws.cleaned == [run.run_id]


def test_hook_a_skips_cleanup_when_finalize_stalls_in_finalizing():
    """A finalize crash leaves `finalizing` — the workspace must survive for
    artifact redelivery / the stalled-finalize killer to reach later."""
    store = InMemoryStore()
    ws = RecordingWS()
    mgr, _ = _mgr(store, ws)

    class StallingFinalizer:
        async def finalize(self, run, payload):
            return None                      # never reaches a terminal state

    mgr.finalizer = StallingFinalizer()
    run = _run("R-1-1-EXECUTE-STALL0", state="running",
               mission_pmo_id="pmo-1")
    store.save(run)
    run_coro(mgr.handle(run.run_id, "run.artifacts",
                        {"result": {"outcome": "executed"}}))
    assert store.get(run.run_id).state == "finalizing"
    assert ws.cleaned == []


def test_hook_b_cleans_workspace_on_every_kill():
    store = InMemoryStore()
    ws = RecordingWS()
    mgr, _ = _mgr(store, ws)
    run = _run("R-1-1-EXECUTE-KILL00", state="running")
    store.save(run)
    run_coro(mgr.kill(run, "timed_out", "exceeded 600s"))
    assert store.get(run.run_id).state == "timed_out"
    assert ws.cleaned == [run.run_id]


# ── Hook C: launch ordering ──────────────────────────────────────────────────

def test_launch_orders_save_then_create_then_start():
    """Record-before-dir keeps the sweep predicate sound; dir-before-start
    kills the dockerd root-autocreate edge (ADR-0025 Hook C)."""
    store = InMemoryStore()
    sequence: list[str] = []

    class SeqStore(InMemoryStore):
        def save(self, run):
            sequence.append("save")
            super().save(run)

    class SeqWS(RecordingWS):
        def __init__(self, store):
            super().__init__()
            self._store = store

        def create(self, run_id):
            assert self._store.get(run_id) is not None, \
                "create before durable save"
            sequence.append("create")
            return super().create(run_id)

    class SeqExecutor(FakeExecutor):
        async def start(self, params, dag_run_id):
            sequence.append("start")
            await super().start(params, dag_run_id)

    store = SeqStore()
    ws = SeqWS(store)
    mgr = RunManager(store, FakeMessaging(), SeqExecutor(), workspaces=ws)
    run = _run("R-1-1-EXECUTE-ORDER0")
    run_coro(mgr.bootstrap.launch(run, image="devcake/dev-x:latest"))
    assert sequence == ["save", "create", "start"]


def test_launch_create_failure_unwinds_and_gates_without_burning_attempt():
    """AUD-001: a create failure AFTER the durable save must unwind the ACL
    user + the record and raise WorkspaceUnavailable — no phantom `dispatched`
    run for the watchdog to kill 90 s later (attempt burn), no container."""
    store = InMemoryStore()
    deleted = []

    class TrackingMsg(FakeMessaging):
        async def delete_run_user(self, run_id):
            deleted.append(run_id)

    class FailingWS(RecordingWS):
        def create(self, run_id):
            raise OSError("disk full")

    executor = FakeExecutor()
    mgr = RunManager(store, TrackingMsg(), executor, workspaces=FailingWS())
    run = _run("R-1-1-EXECUTE-NOSPC0")
    with pytest.raises(WorkspaceUnavailable):
        run_coro(mgr.bootstrap.launch(run, image="devcake/dev-x:latest"))
    assert executor.starts == []          # no container was ever asked for
    assert store.get(run.run_id) is None  # record unwound — no attempt burned
    assert deleted == [run.run_id]        # ACL user unwound


def test_launch_gates_on_volume_error_before_any_side_effect():
    """AUD-001/002: a persistent unusable base (volume_error latched at boot)
    fails fast — no ACL user, no record, no container — so dispatch gates
    cleanly and the SPA's 'dispatch is frozen' alert is backed by real
    gating rather than being aspirational."""
    store = InMemoryStore()
    created = []

    class TrackingMsg(FakeMessaging):
        async def create_run_user(self, run_id):
            created.append(run_id)
            return "pw"

    ws = RecordingWS()
    ws.volume_error = "EACCES: base not writable by uid 1000"
    executor = FakeExecutor()
    mgr = RunManager(store, TrackingMsg(), executor, workspaces=ws)
    run = _run("R-1-1-EXECUTE-FROZEN")
    with pytest.raises(WorkspaceUnavailable):
        run_coro(mgr.bootstrap.launch(run, image="devcake/dev-x:latest"))
    assert created == []                   # not even an ACL user
    assert store.get(run.run_id) is None
    assert executor.starts == []
    assert ws.created == []                 # create() never reached
