"""Decomposition recovery through artifact ingress and startup reconciliation.

The board and pending envelopes survive; application objects and the file
store are reconstructed. Failures happen at PMOPort writes, not checkpoints.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from devcake.adapters.files.run_store import RunStore
from devcake.config import AppConfig, PMOInstance
from devcake.domain.model import Mission, MissionRef
from devcake.domain.orchestrator import FinalizerRouter, MissionManager
from devcake.domain.orchestrator.schedule import gate_map
from devcake.domain.reconcile import reconcile_runs
from devcake.domain.run import Run, auth_digest
from devcake.domain.runs import RunManager
from devcake.ports.pmo import PMOTransient
from fakes import FakeExecutor, make_mission_manager
from test_review_restart import AUTH, PendingArtifacts, ProcessStopped, run_scenario
from test_transitions import FakePMO, mission

RUN_ID = "T-1-1-ONBOARD-ABCDEF"
PAYLOAD = {
    "result": {"outcome": "decomposed", "decomposition": [
        {"title": "Design", "description": "Define the interface."},
        {"title": "Implement", "description": "Build the interface.", "blocked_by": [1]},
    ]},
    "transcript_md": "A synthetic decomposition transcript.",
    "token_report": {"input_tokens": 10, "output_tokens": 5},
}


class Board(FakePMO):
    def __init__(self, *, stop_after_child: int = 1,
                 fail_edge: tuple[str, str] | None = None,
                 accept_edge: bool = False,
                 stop_at_cancel: str | None = None,
                 fault: BaseException | None = None) -> None:
        super().__init__(mission())
        self.mission.blocked_by = ["up"]
        self.mission.parent_ref = "project"
        upstream = mission("backlog")
        upstream.pmo_id, upstream.key = "up", "T-UP"
        downstream = mission("backlog")
        downstream.pmo_id, downstream.key = "down", "T-DOWN"
        downstream.blocked_by = ["p1"]
        self.all_missions.extend([upstream, downstream])
        self.stop_after_child = stop_after_child
        self.fail_edge = fail_edge
        self.accept_edge = accept_edge
        self.stop_at_cancel = stop_at_cancel
        self.fault = fault if fault is not None else ProcessStopped("child accepted")

    async def get(self, ref: MissionRef) -> Mission:
        self._check_ref(ref)
        return next(m for m in self.all_missions if m.ref == ref).model_copy(deep=True)

    async def list_all(self, team_ref: str) -> list[Mission]:
        return [m.model_copy(deep=True) for m in self.all_missions]

    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: str | None = None) -> tuple[str, str]:
        created = await super().create_mission(
            team_ref, title, description, priority, label_names, parent_ref)
        if len(self.created) == self.stop_after_child:
            self.stop_after_child = 0
            raise self.fault
        return created

    async def create_relation(self, blocker_id: str, blocked_id: str) -> None:
        fail = self.fail_edge == (blocker_id, blocked_id)
        if fail:
            self.fail_edge = None
            if not self.accept_edge:
                raise self.fault
        # Reflect real, duplicate-tolerant board edges in subsequent reads.
        await super().create_relation(blocker_id, blocked_id)
        blocked = next(m for m in self.all_missions if m.pmo_id == blocked_id)
        if blocker_id not in blocked.blocked_by:
            blocked.blocked_by.append(blocker_id)
        if fail:
            raise self.fault

    async def cancel_mission(self, ref: MissionRef) -> None:
        stop = self.stop_at_cancel
        self.stop_at_cancel = None
        if stop == "before":
            raise self.fault
        await super().cancel_mission(ref)
        if stop == "after":
            raise self.fault


def app(root: Path, board: Board, messaging: PendingArtifacts, *,
        plan_approval: bool = False, depth_limit: int = 2
        ) -> tuple[RunManager, MissionManager]:
    store = RunStore(root / "runs")
    runs = RunManager(store, messaging, FakeExecutor())
    instance = PMOInstance(name="linear", team_key="DEV", plan_approval=plan_approval)
    config = AppConfig(pmos=[instance], max_decomposition_depth=depth_limit)
    mgr = make_mission_manager(
        pmo=board, runs=runs, messaging=messaging, instance=instance,
        config=config, noop_audit=True)
    runs.set_finalizer(FinalizerRouter({instance.name: mgr}, store, messaging))
    return runs, mgr


def seed(runs: RunManager) -> None:
    runs.store.save(Run(
        run_id=RUN_ID, mission_key="T-1", mission_pmo_id="p1",
        mission_type="ONBOARD", dev_type="judgment", seq=1,
        pmo_ref="linear", repo_ref="main", state="running",
        stage_label_at_dispatch=None, auth_digest=auth_digest(AUTH)))


def children(board: Board) -> dict[str, Mission]:
    return {m.pmo_id: m for m in board.all_missions
            if m.pmo_id != "p1" and "DEVCAKE-CREATED" in m.labels}


def assert_completed(board: Board, runs: RunManager, messaging: PendingArtifacts,
                     *, plan_approval: bool = False) -> None:
    parts = children(board)
    assert set(parts) == {"id-1", "id-2"}
    assert parts["id-1"].title == "Design"
    assert parts["id-2"].title == "Implement"
    assert set(parts["id-1"].blocked_by) == {"up"}
    assert set(parts["id-2"].blocked_by) == {"up", "id-1"}
    downstream = next(m for m in board.all_missions if m.pmo_id == "down")
    assert set(downstream.blocked_by) == {"p1", "id-1", "id-2"}
    labels = {"DEVCAKE", "DEVCAKE-CREATED"}
    if plan_approval:
        labels.add("DEVCAKE-NEEDS-HUMAN")
    for child in parts.values():
        assert child.labels == labels
        assert child.parent_ref == "project"
    assert board.mission.status == "canceled"
    assert runs.store.get(RUN_ID).state == "finished"
    assert messaging.pending == messaging.users == messaging.reply_streams == set()


@pytest.mark.parametrize("stop_after_child", [1, 2])
@pytest.mark.parametrize("fault_type", [ProcessStopped, PMOTransient])
@pytest.mark.parametrize("plan_approval", [False, True])
def test_accepted_child_write_recovers_from_board_provenance(
        tmp_path, stop_after_child, fault_type, plan_approval):
    async def scenario():
        board = Board(stop_after_child=stop_after_child, fault=fault_type("response lost"))
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, mgr = app(tmp_path, board, messaging, plan_approval=plan_approval)
        seed(runs)
        with pytest.raises(fault_type):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert board.mission.status == "in_progress"
        assert runs.store.get(RUN_ID).state == "finalizing"
        gates = await gate_map(mgr, await board.list_all("DEV"))
        assert {"down", *children(board)} <= set(gates)

        restarted, _ = app(tmp_path, board, messaging, plan_approval=plan_approval)
        await reconcile_runs(restarted)
        assert_completed(board, restarted, messaging, plan_approval=plan_approval)
        assert board.mission.description.count("_Decomposed by DevCake into T-2, T-3_") == 1

        # A further restart and duplicate artifact leave the whole board intact.
        snapshot = await board.list_all("DEV")
        comments = list(board.comments)
        again, _ = app(tmp_path, board, messaging, plan_approval=plan_approval)
        await reconcile_runs(again)
        await again.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert await board.list_all("DEV") == snapshot
        assert board.comments == comments

    run_scenario(scenario())


@pytest.mark.parametrize("edge", [("id-1", "id-2"), ("up", "id-1"), ("id-1", "down")])
@pytest.mark.parametrize("accepted", [False, True])
def test_dependency_write_failure_keeps_parent_open_until_recovery(tmp_path, edge, accepted):
    async def scenario():
        board = Board(stop_after_child=0, fail_edge=edge, accept_edge=accepted,
                      fault=PMOTransient("relation response lost"))
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, mgr = app(tmp_path, board, messaging)
        seed(runs)
        with pytest.raises(PMOTransient):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert set(children(board)) == {"id-1", "id-2"}
        assert board.mission.status == "in_progress"
        assert runs.store.get(RUN_ID).state == "finalizing"
        blocked = await board.get(MissionRef(edge[1], "issue"))
        assert (edge[0] in blocked.blocked_by) is accepted
        gates = await gate_map(mgr, await board.list_all("DEV"))
        assert {"id-1", "id-2", "down"} <= set(gates)

        restarted, _ = app(tmp_path, board, messaging)
        await reconcile_runs(restarted)
        assert_completed(board, restarted, messaging)

    run_scenario(scenario())


def test_lowered_depth_limit_finishes_a_remotely_committed_split(tmp_path):
    async def scenario():
        board = Board()
        board.mission.labels.add("DEVCAKE-CREATED")
        board.mission.description = (
            "Existing first-generation child.\n"
            "`devcake:decomposition:v1 parent=ancestor manifest=" + "a" * 64
            + " part=1/1 depth=1`")
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, _ = app(tmp_path, board, messaging, depth_limit=2)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert set(children(board)) == {"id-1"}
        assert board.mission.status == "in_progress"

        restarted, _ = app(tmp_path, board, messaging, depth_limit=1)
        await reconcile_runs(restarted)

        assert_completed(board, restarted, messaging)
        assert "DEVCAKE-SKIP" not in board.mission.labels

    run_scenario(scenario())


@pytest.mark.parametrize("when", ["before", "after"])
def test_restart_at_parent_cancellation_preserves_complete_graph(tmp_path, when):
    async def scenario():
        board = Board(stop_after_child=0, stop_at_cancel=when)
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, _ = app(tmp_path, board, messaging)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        assert board.mission.status == ("in_progress" if when == "before" else "canceled")
        parts = children(board)
        assert set(parts["id-1"].blocked_by) == {"up"}
        assert set(parts["id-2"].blocked_by) == {"up", "id-1"}
        downstream = await board.get(MissionRef("down", "issue"))
        assert set(downstream.blocked_by) == {"p1", "id-1", "id-2"}

        restarted, _ = app(tmp_path, board, messaging)
        await reconcile_runs(restarted)
        assert_completed(board, restarted, messaging)
        assert board.mission.description.count("_Decomposed by DevCake into T-2, T-3_") == 1

    run_scenario(scenario())


@pytest.mark.parametrize("change", ["title", "manifest", "unmanaged"])
def test_remote_child_provenance_cannot_bypass_replay_guards(tmp_path, change):
    async def scenario():
        board = Board()
        board.mission.labels.add("DEVCAKE-CREATED")
        board.mission.description = (
            "`devcake:decomposition:v1 parent=ancestor manifest=" + "a" * 64
            + " part=1/1 depth=1`")
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, _ = app(tmp_path, board, messaging)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        child = children(board)["id-1"]
        if change == "title":
            child.title = "Human revised design"
        elif change == "manifest":
            child.description = (
                "`devcake:decomposition:v1 parent=p1 manifest=" + "b" * 64
                + " part=1/2 depth=2`")
        else:
            child.labels.remove("DEVCAKE-CREATED")
        snapshot = child.model_copy(deep=True)

        restarted, _ = app(tmp_path, board, messaging, depth_limit=1)
        await reconcile_runs(restarted)
        assert child == snapshot
        assert not any(m.pmo_id == "id-2" for m in board.all_missions)
        assert board.mission.status == "backlog"
        expected = "DEVCAKE-SKIP" if change == "unmanaged" else "DEVCAKE-NEEDS-HUMAN"
        assert expected in board.mission.labels
        assert restarted.store.get(RUN_ID).state == "finished"

    run_scenario(scenario())


@pytest.mark.parametrize("label", ["DEVCAKE-PLAN", "DEVCAKE-SKIP"])
def test_human_label_change_between_deliveries_stops_decomposition(tmp_path, label):
    async def scenario():
        board = Board()
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, _ = app(tmp_path, board, messaging)
        seed(runs)
        with pytest.raises(ProcessStopped):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)
        board.mission.labels.add(label)
        snapshot = await board.list_all("DEV")

        restarted, _ = app(tmp_path, board, messaging)
        await reconcile_runs(restarted)
        assert await board.list_all("DEV") == snapshot
        assert restarted.store.get(RUN_ID).state == "finished"
        assert "changed externally" in restarted.store.get(RUN_ID).verdict

    run_scenario(scenario())


def test_repeated_dependency_failure_survives_multiple_restarts(tmp_path):
    async def scenario():
        edge = ("id-1", "down")
        board = Board(stop_after_child=0, fail_edge=edge,
                      fault=PMOTransient("board relations unavailable"))
        messaging = PendingArtifacts(RUN_ID, PAYLOAD)
        runs, _ = app(tmp_path, board, messaging)
        seed(runs)
        with pytest.raises(PMOTransient):
            await runs.handle(RUN_ID, "run.artifacts", PAYLOAD)

        for _ in range(2):
            board.fail_edge = edge
            restarted, mgr = app(tmp_path, board, messaging)
            await reconcile_runs(restarted)
            assert restarted.store.get(RUN_ID).state == "finalizing"
            assert messaging.pending == {RUN_ID}
            assert board.mission.status == "in_progress"
            assert set(children(board)) == {"id-1", "id-2"}
            gates = await gate_map(mgr, await board.list_all("DEV"))
            assert {"id-1", "id-2", "down"} <= set(gates)

        recovered, _ = app(tmp_path, board, messaging)
        await reconcile_runs(recovered)
        assert_completed(board, recovered, messaging)

    run_scenario(scenario())
