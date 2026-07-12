"""INV-3 (no-lock atomicity: compare-and-transition, failure symmetry) and
INV-5 (transcript + token report always posted) — exercised against a FakePMO."""
import asyncio
from datetime import datetime, timezone

import pytest

from devcake.config import AppConfig, DevType
from devcake.missions import MissionManager
from devcake.pmo import Mission
from devcake.state import Run, RunStore


class FakePMO:
    def __init__(self, mission):
        self.mission = mission
        self.comments = []
        self.swaps = []
        self.statuses = []
        self.created = []       # (title, project_id)
        self.relations = []     # (blocker_id, blocked_id)

    async def get_mission(self, pmo_id):
        return self.mission

    async def get_project(self, pmo_id):
        return self.mission

    async def post_comment(self, pmo_id, md):
        self.comments.append(md)

    async def swap_labels(self, pmo_id, remove, add):
        self.swaps.append((set(remove), set(add)))
        self.mission.labels = (self.mission.labels - set(remove)) | set(add)

    async def swap_labels_project(self, pmo_id, remove, add):
        await self.swap_labels(pmo_id, remove, add)

    async def set_status(self, pmo_id, status):
        self.statuses.append(status)
        self.mission.status = status

    async def set_project_status(self, pmo_id, status):
        await self.set_status(pmo_id, status)

    async def upload_attachment(self, pmo_id, name, data):
        return f"https://fake/{name}"

    async def create_mission(self, team_ref, title, description, priority,
                             labels, project_id=None):
        self.created.append((title, project_id))
        return f"T-{len(self.created) + 1}", f"id-{len(self.created)}"

    async def create_relation(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))

    async def children_of_project(self, project_id):
        return []


class NullMessaging:
    async def create_run_user(self, rid): return "pw"
    async def delete_run_user(self, rid): pass
    async def delete_reply_stream(self, rid): pass


def mission(status="in_progress", labels=frozenset({"DEVCAKE"})):
    return Mission(pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                   status=status, labels=set(labels),
                   updated_at=datetime.now(timezone.utc))


def make_mgr(tmp_path, m):
    cfg = AppConfig()
    fake = FakePMO(m)
    store = RunStore(tmp_path / "runs")

    class Runs:
        pass
    runs = Runs()
    runs.store = store
    runs.mission_mgr = None
    mgr = MissionManager.__new__(MissionManager)
    mgr.config = cfg
    mgr.dev_types = {"senior-dev": DevType(name="senior-dev",
                                           harness_template="claude-code")}
    mgr.pmo = fake
    mgr.runs = runs
    mgr.messaging = NullMessaging()
    mgr._grace, mgr._grace_next, mgr.breakers = set(), set(), {}
    mgr._audit = lambda *a, **k: None
    return mgr, fake, store


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def test_external_transition_aborts_inv3(tmp_path):
    m = mission(labels={"DEVCAKE", "DEVCAKE-PLAN"})   # human added PLAN mid-run
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-XXXXXX", mission_key="T-1", mission_pmo_id="p1",
              mission_type="ONBOARD", dev_type="senior-dev", seq=1,
              stage_label_at_dispatch=None)   # dispatched when NO stage label
    run_coro(mgr._transition(run, {"outcome": "plan_needed"}, None))
    assert fake.swaps == []                    # nothing applied — human won
    assert any("changed externally" in c for c in fake.comments)


def test_failure_restores_status_inv3(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-YYYYYY", mission_key="T-1", mission_pmo_id="p1",
              mission_type="ONBOARD", dev_type="senior-dev", seq=1,
              stage_label_at_dispatch=None)
    run_coro(mgr.restore_after_failure(run))
    assert fake.statuses == ["backlog"]        # dispatch-time write reverted


def _run(mission_type="EXECUTE", stage="DEVCAKE-EXECUTE"):
    return Run(run_id=f"T-1-1-{mission_type}-AAAAAA", mission_key="T-1",
               mission_pmo_id="p1", mission_type=mission_type,
               dev_type="senior-dev", seq=1, stage_label_at_dispatch=stage)


def test_human_needed_keeps_stage_and_hands_off(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run(), {"outcome": "human_needed",
                                      "summary": "grant repo:write to the token"},
                             None))
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels
    assert "DEVCAKE-EXECUTE" in m.labels          # resumes at the same stage
    assert fake.statuses == []                    # status untouched mid-pipeline
    assert any("grant repo:write" in c and "DEVCAKE-NEEDS-HUMAN" in c
               for c in fake.comments)


def test_human_needed_from_onboard_restores_backlog(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})       # ONBOARD flipped it in_progress
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run("ONBOARD", None),
                             {"outcome": "human_needed", "summary": "s"}, None))
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels
    assert fake.statuses == ["backlog"]           # avoids row-9 stranding


def test_human_needed_allowed_for_projects(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    m.pmo_kind = "project"
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(mgr._transition(run, {"outcome": "human_needed", "summary": "s"}, None))
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels      # not parked with SKIP
    assert "DEVCAKE-SKIP" not in m.labels


def test_decomposition_wires_blocked_by_edges(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    drafts = [{"title": "docs", "priority": "high"},
              {"title": "code", "priority": "high", "blocked_by": [1]},
              {"title": "polish", "blocked_by": [1, 2]}]
    run_coro(mgr._transition(_run("ONBOARD", None),
                             {"outcome": "decomposed", "decomposition": drafts},
                             None))
    assert [t for t, _ in fake.created] == ["docs", "code", "polish"]
    assert fake.relations == [("id-1", "id-2"), ("id-1", "id-3"), ("id-2", "id-3")]
    assert fake.statuses == ["canceled"]


def test_decomposition_rejects_forward_or_self_blocked_by(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    for bad in ([1], [2]):                        # self-reference / forward reference
        with pytest.raises(ValueError):
            run_coro(mgr._finalize_decomposition(
                _run("ONBOARD", None),
                {"outcome": "decomposed",
                 "decomposition": [{"title": "a", "blocked_by": bad},
                                   {"title": "b"}]}))
    assert fake.created == []                     # nothing created before validation


def test_feed_appends_sentinel_exactly_once(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._feed("p1", "issue", "hello there\n"))
    assert fake.comments[-1].endswith("\n\n`devcake:v1`")
    assert fake.comments[-1].count("`devcake:v1`") == 1


def test_finalize_always_posts_report_inv5(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-ZZZZZZ", mission_key="T-1", mission_pmo_id="p1",
              mission_type="ONBOARD", dev_type="senior-dev", seq=1,
              stage_label_at_dispatch=None, state="finalizing")
    store.save(run)
    payload = {"result": {"outcome": "plan_needed", "summary": "s"},
               "transcript_md": "T",
               "token_report": {"extraction_method": "unavailable", "model": "m"}}
    run_coro(mgr.finalize(run, payload))
    assert any("token report" in c for c in fake.comments)      # INV-5
    assert any("1_ONBOARD.md" in c for c in fake.comments)      # transcript
    assert ({"DEVCAKE-PLAN"} in [add for _, add in fake.swaps]) # transition applied
