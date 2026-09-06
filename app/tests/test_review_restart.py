"""REVIEW recovery through artifact ingress and startup reconciliation.

Remote PMO/forge state and pending messages survive; RunStore, RunManager,
MissionManager, and FinalizerRouter are reconstructed from disk on restart.
Faults occur at port operations, never at private checkpoints.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from devcake.adapters.files.run_store import RunStore
from devcake.config import RepoInstance
from devcake.domain.orchestrator import FinalizerRouter
from devcake.domain.reconcile import reconcile_runs
from devcake.domain.run import Run, auth_digest
from devcake.domain.runs import RunManager
from devcake.ports.forge import ForgeError, PullRequest
from devcake.ports.pmo import PMOTransient
from fakes import FakeExecutor, FakeForgeRuntime, NullMessaging, make_mission_manager
from test_transitions import FakeForge, FakePMO, mission

RUN_ID = "T-1-1-REVIEW-ABCDEF"
AUTH = "obvious-fake-run-password"
PAYLOAD = {
    "result": {"outcome": "reviewed", "verdict": "approve",
               "report_md": "The change satisfies the ticket."},
    "transcript_md": "A synthetic REVIEW transcript.",
    "token_report": {"input_tokens": 10, "output_tokens": 5},
}


class ProcessStopped(BaseException):
    """A process exit bypasses production's recoverable Exception handlers."""


class ReviewForge(FakeForge):
    def __init__(self, *, fault_at: str = "approve",
                 fault: BaseException | None = None) -> None:
        super().__init__()
        self.approved = False
        self.merged = False
        self.fault_at = fault_at
        self.fault = fault if fault is not None else ProcessStopped(fault_at)

    def after_write(self, operation: str) -> None:
        if self.fault_at == operation:
            self.fault_at = ""
            raise self.fault

    async def approve(self, pr_number: int) -> bool:
        self.approved = True
        self.after_write("approve")
        return True

    async def merge(self, pr_number: int) -> None:
        self.merged = True
        self.after_write("merge")

    async def pr_state(self, pr_number: int) -> PullRequest:
        return PullRequest(number=8, url="https://forge/pr/8",
                           state="closed" if self.merged else "open",
                           merged=self.merged)

    async def get_pr_by_branch(self, branch: str) -> PullRequest:
        return await self.pr_state(8)


class PendingArtifacts(NullMessaging):
    def __init__(self, run_id: str = RUN_ID, payload: dict | None = None) -> None:
        self.pending = {run_id}
        self.users = {run_id}
        self.reply_streams = {run_id}
        self.payload = PAYLOAD if payload is None else payload

    async def unresolved_run_ids(self) -> set[str]:
        return set(self.pending)

    async def reclaim_pending(self, handler, verify_auth) -> None:
        for run_id in tuple(self.pending):
            assert verify_auth(run_id, AUTH)
            await handler(run_id, "run.artifacts", self.payload)
            self.pending.remove(run_id)  # ack only after successful handling

    async def delete_run_user(self, run_id: str) -> None:
        self.users.discard(run_id)

    async def delete_reply_stream(self, run_id: str) -> None:
        self.reply_streams.discard(run_id)

    async def delete_runspec_result(self, run_id: str) -> None:
        pass


class CompletionPMO(FakePMO):
    def __init__(self, *, accept_done: bool = False) -> None:
        super().__init__(mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"}))
        self.accept_done = accept_done
        self.fail_done = True

    async def set_status(self, ref, status: str) -> None:
        if status == "done" and self.fail_done:
            self.fail_done = False
            if self.accept_done:
                await super().set_status(ref, status)
                raise ProcessStopped("Done accepted; process stopped before response")
            raise PMOTransient("board unavailable before Done")
        await super().set_status(ref, status)


def app(root: Path, pmo: FakePMO, forge: ReviewForge,
        messaging: PendingArtifacts, *, auto_merge: bool = True) -> RunManager:
    store = RunStore(root / "runs")
    runs = RunManager(store, messaging, FakeExecutor())
    repo = RepoInstance(name="main", url="https://forge/org/repo",
                        auto_merge=auto_merge)
    mgr = make_mission_manager(
        pmo=pmo, forge_runtime=FakeForgeRuntime(forge, repo), runs=runs,
        messaging=messaging, noop_audit=True)
    runs.set_finalizer(FinalizerRouter({mgr.instance_name: mgr}, store, messaging))
    return runs


def seed(runs: RunManager) -> None:
    runs.store.save(Run(
        run_id=RUN_ID, mission_key="T-1", mission_pmo_id="p1",
        mission_type="REVIEW", dev_type="judgment", seq=1,
        pmo_ref="linear", repo_ref="main", state="running",
        stage_label_at_dispatch="DEVCAKE-REVIEW", auth_digest=auth_digest(AUTH)))


def run_scenario(coro) -> None:
    # Do not install/clear the process-global loop: older suites still use it.
    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(coro)
    finally:
        loop.close()


@pytest.mark.parametrize("auto_merge", [True, False])
def test_cancellation_with_review_label_survives_approval_restart(tmp_path, auto_merge):
    async def scenario():
        pmo = FakePMO(mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"}))
        forge, messaging = ReviewForge(), PendingArtifacts()
        runs = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert forge.approved and not forge.merged
        assert runs.store.get(RUN_ID).state == "finalizing"

        # Trackers can cancel a ticket without removing its workflow labels.
        pmo.mission.status = "canceled"
        restarted = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        await reconcile_runs(restarted)

        assert pmo.mission.status == "canceled"
        assert pmo.mission.labels == {"DEVCAKE", "DEVCAKE-REVIEW"}
        assert not forge.merged
        assert restarted.store.get(RUN_ID).state == "finished"
        assert "changed externally" in restarted.store.get(RUN_ID).verdict
        assert messaging.pending == messaging.users == messaging.reply_streams == set()

    run_scenario(scenario())


@pytest.mark.parametrize("fault_at,auto_merge", [
    ("approve", True), ("merge", True), ("approve", False),
])
def test_accepted_forge_write_recovers_after_restart(tmp_path, fault_at, auto_merge):
    async def scenario():
        pmo = FakePMO(mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"}))
        forge, messaging = ReviewForge(fault_at=fault_at), PendingArtifacts()
        runs = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert forge.approved
        assert forge.merged is (fault_at == "merge")
        assert runs.store.get(RUN_ID).state == "finalizing"
        assert pmo.mission.status == "in_progress"

        restarted = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        await reconcile_runs(restarted)

        assert restarted.store.get(RUN_ID).state == "finished"
        assert forge.merged is auto_merge
        assert pmo.mission.status == ("done" if auto_merge else "in_progress")
        assert pmo.mission.labels == (
            {"DEVCAKE"} if auto_merge else {"DEVCAKE", "DEVCAKE-MERGE"})
        assert messaging.pending == messaging.users == messaging.reply_streams == set()
        assert any("token" in text.lower() for text in pmo.comments)
        assert any(data == b"A synthetic REVIEW transcript." for _, data in pmo.uploads)
        if fault_at == "merge":
            # The process died before any local merge receipt: detection must
            # remain active when the app cannot establish who merged remotely.
            assert any("Out-of-pipeline merge detected" in text for text in pmo.comments)

        # Completed artifact replay is a no-op even after another restart.
        comments, pr_comments = list(pmo.comments), list(forge.pr_comments)
        again = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        await reconcile_runs(again)
        await again.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert pmo.comments == comments
        assert forge.pr_comments == pr_comments
        assert forge.merged is auto_merge

    run_scenario(scenario())


@pytest.mark.parametrize("fault_at,auto_merge", [
    ("approve", True), ("merge", True), ("approve", False),
])
def test_accepted_forge_write_with_lost_response_converges(tmp_path, fault_at, auto_merge):
    async def scenario():
        pmo = FakePMO(mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"}))
        forge = ReviewForge(fault_at=fault_at, fault=ForgeError("response lost"))
        messaging = PendingArtifacts()
        runs = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        seed(runs)
        await messaging.reclaim_pending(runs.handle, runs.verify_auth)
        restarted = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        await reconcile_runs(restarted)

        assert forge.approved
        assert forge.merged is auto_merge
        assert pmo.mission.status == ("done" if auto_merge else "in_progress")
        assert pmo.mission.labels == (
            {"DEVCAKE"} if auto_merge else {"DEVCAKE", "DEVCAKE-MERGE"})
        assert restarted.store.get(RUN_ID).state == "finished"
        assert messaging.pending == messaging.users == messaging.reply_streams == set()
        assert not any("auto-merge failed" in text for text in pmo.comments)

    run_scenario(scenario())


@pytest.mark.parametrize("accept_done", [False, True])
def test_completion_recovers_without_misreporting_a_recorded_merge(tmp_path, accept_done):
    async def scenario():
        pmo = CompletionPMO(accept_done=accept_done)
        forge, messaging = ReviewForge(fault_at=""), PendingArtifacts()
        runs = app(tmp_path, pmo, forge, messaging)
        seed(runs)
        with pytest.raises(ProcessStopped if accept_done else PMOTransient):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert forge.merged
        assert runs.store.get(RUN_ID).state == "finalizing"
        assert pmo.mission.status == ("done" if accept_done else "in_progress")

        restarted = app(tmp_path, pmo, forge, messaging)
        await reconcile_runs(restarted)

        assert pmo.mission.status == "done"
        assert pmo.mission.labels == {"DEVCAKE"}
        assert restarted.store.get(RUN_ID).state == "finished"
        assert messaging.pending == messaging.users == messaging.reply_streams == set()
        assert not any("auto-merge failed" in text for text in pmo.comments)
        assert not any("Out-of-pipeline merge detected" in text for text in pmo.comments)

    run_scenario(scenario())


@pytest.mark.parametrize("labels", [
    {"DEVCAKE", "DEVCAKE-EXECUTE"},
    {"DEVCAKE", "DEVCAKE-REVIEW", "DEVCAKE-SKIP"},
])
@pytest.mark.parametrize("auto_merge", [True, False])
def test_human_label_change_wins_over_replayed_approval(tmp_path, labels, auto_merge):
    async def scenario():
        pmo = FakePMO(mission(labels={"DEVCAKE", "DEVCAKE-REVIEW"}))
        forge, messaging = ReviewForge(), PendingArtifacts()
        runs = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)

        pmo.mission.labels = set(labels)
        restarted = app(tmp_path, pmo, forge, messaging, auto_merge=auto_merge)
        await reconcile_runs(restarted)

        assert pmo.mission.labels == labels
        assert pmo.mission.status == "in_progress"
        assert forge.approved and not forge.merged
        assert restarted.store.get(RUN_ID).state == "finished"
        assert "changed externally" in restarted.store.get(RUN_ID).verdict
        assert messaging.pending == messaging.users == messaging.reply_streams == set()

    run_scenario(scenario())
