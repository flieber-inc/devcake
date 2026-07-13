"""INV-3 (no-lock atomicity: compare-and-transition, failure symmetry) and
INV-5 (transcript + token report always posted) — exercised against a FakePMO.
Also the docs/03 §4.1 merge-failure branches (conflict rework, deferred retry)
against a FakeForge, and the docs/05 §4 attachment policy."""
import asyncio
from datetime import datetime, timezone

import pytest

from devcake.config import AppConfig, DevType
from devcake.forge import ForgeError
from devcake.missions import MissionManager
from devcake.pmo import Activity, ActivityEntry, Mission
from devcake.state import Run, RunStore


class FakePMO:
    def __init__(self, mission):
        self.mission = mission
        self.comments = []
        self.swaps = []
        self.statuses = []
        self.created = []       # (title, project_id)
        self.relations = []     # (blocker_id, blocked_id)
        self.uploads = []       # (name, bytes)
        self.activity_entries = []

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
        self.uploads.append((name, data))
        return f"https://fake/{name}"

    async def get_activity(self, pmo_id):
        self.get_activity_calls = getattr(self, "get_activity_calls", 0) + 1
        return Activity(mission=self.mission, entries=list(self.activity_entries))

    async def create_mission(self, team_ref, title, description, priority,
                             labels, project_id=None):
        self.created.append((title, project_id))
        return f"T-{len(self.created) + 1}", f"id-{len(self.created)}"

    async def create_relation(self, blocker_id, blocked_id):
        self.relations.append((blocker_id, blocked_id))

    async def create_project_update(self, project_id, body):
        self.project_updates = getattr(self, "project_updates", [])
        self.project_updates.append((project_id, body))

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


class FakeForge:
    """Port-shaped fake (docs/06). merge_exc raised on every merge() call;
    mergeable_result is the tri-state mergeable() answer."""

    def __init__(self, merge_exc=None, mergeable_result=None):
        self.merge_exc = merge_exc
        self.mergeable_result = mergeable_result
        self.merges = []
        self.pr_comments = []

    async def get_pr_by_branch(self, branch):
        return {"number": 8, "html_url": "https://forge/pr/8", "state": "open"}

    async def post_pr_comment(self, pr_number, markdown):
        self.pr_comments.append(markdown)

    async def approve(self, pr_number):
        return False

    async def merge(self, pr_number):
        self.merges.append(pr_number)
        if self.merge_exc:
            raise self.merge_exc

    async def mergeable(self, pr_number):
        return self.mergeable_result

    async def pr_state(self, pr_number):
        return {"state": "open", "merged": False,
                "url": "https://forge/pr/8", "number": pr_number}

    @staticmethod
    def approval_footer(pr_url):
        return "\n\n---\nfooter"


def make_mgr(tmp_path, m, forge=None):
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
    mgr.merge_handoffs, mgr._merge_window_closed = {}, set()
    mgr.needs_human = {}
    mgr._audit = lambda *a, **k: None
    if forge is not None:       # default stays attribute-ABSENT (trust boundary
        mgr.forge = forge       # test relies on a forge call raising)
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
    run_coro(mgr._transition(run, {"outcome": "human_needed",
                                   "summary": "grant the scope"}, None))
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels      # not parked with SKIP
    assert "DEVCAKE-SKIP" not in m.labels
    # the baton MUST be PMO-visible: comments are suppressed for projects, so
    # it goes out as a project update, sentinel-signed (docs/05 §6)
    pid, body = fake.project_updates[-1]
    assert "grant the scope" in body and body.endswith("`devcake:v1`")


def test_illegal_outcome_parks_never_acts(tmp_path):
    # the trust boundary (docs/03 §6): an EXECUTE dev forging "reviewed" must
    # never reach _finalize_review (no forge attribute here — a forge call
    # would raise AttributeError and fail this test)
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run(), {"outcome": "reviewed",
                                      "verdict": "approve"}, None))
    assert "DEVCAKE-SKIP" in m.labels
    assert "DEVCAKE-EXECUTE" in m.labels          # stage untouched, only parked
    assert any("not a legal outcome" in c for c in fake.comments)
    # and a PLAN run may only return "planned"
    m2 = mission("in_progress", {"DEVCAKE", "DEVCAKE-PLAN"})
    mgr2, fake2, _ = make_mgr(tmp_path, m2)
    run_coro(mgr2._transition(_run("PLAN", "DEVCAKE-PLAN"),
                              {"outcome": "decomposed", "decomposition": [{}]},
                              None))
    assert "DEVCAKE-SKIP" in m2.labels and fake2.created == []


def test_second_handoff_escalates_warning(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run(), {"outcome": "human_needed", "summary": "s1"},
                             None))
    assert not any("Hand-off #" in c for c in fake.comments)   # first: no warning
    prior = _run()
    prior.run_id = "T-1-1-EXECUTE-PRIOR1"
    prior.state = "finished"
    prior.result = {"outcome": "human_needed"}
    store.save(prior)
    m.labels.discard("DEVCAKE-NEEDS-HUMAN")       # human resolved + resumed
    run_coro(mgr._transition(_run(), {"outcome": "human_needed", "summary": "s2"},
                             None))
    assert any("Hand-off #2" in c and "DEVCAKE-SKIP" in c for c in fake.comments)
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels      # warned, never parked


def test_malformed_decomposition_fails_run_not_poison(tmp_path):
    # docs/15: DEV_BAD_OUTPUT is a counted attempt — the run fails cleanly and
    # the mission reschedules; the exception must NOT escape finalize()
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-BADDEC", mission_key="T-1", mission_pmo_id="p1",
              mission_type="ONBOARD", dev_type="senior-dev", seq=1,
              stage_label_at_dispatch=None, state="finalizing")
    store.save(run)
    payload = {"result": {"outcome": "decomposed", "summary": "s",
                          "decomposition": [{"title": "a", "blocked_by": [1]}]},
               "transcript_md": "T", "token_report": {}}
    run_coro(mgr.finalize(run, payload))
    assert run.state == "failed" and "DEV_BAD_OUTPUT" in (run.error or "")
    assert fake.statuses == ["backlog"]           # dispatch-time write reverted
    assert fake.created == []                     # no children materialized


def test_quoted_sentinel_still_classifies_human():
    quoted = ("please also add tests\n\n"
              "> ✋ **DevCake needs a human.** blah\n> `devcake:v1`")
    genuine = "> user asked:\n> do X\n\nDone.\n\n`devcake:v1`"
    assert not MissionManager._is_devcake_comment(quoted)
    assert MissionManager._is_devcake_comment(genuine)


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
    # docs/05 §4: the transcript is an attachment; the referencing comment
    # keeps the backticked step marker (seq derivation) and the link
    assert ("1_ONBOARD.md", b"T") in fake.uploads
    assert any("`1_ONBOARD.md`" in c and "https://fake/1_ONBOARD.md" in c
               for c in fake.comments)
    assert ({"DEVCAKE-PLAN"} in [add for _, add in fake.swaps]) # transition applied


# ── docs/05 §4 attachment policy ─────────────────────────────────────────────

def test_feed_externalizes_over_2048(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._feed("p1", "issue", "x" * 3000))
    assert len(fake.uploads) == 1
    posted = fake.comments[-1]
    assert "full text attached:" in posted and "https://fake/" in posted
    assert len(posted) < 1024
    assert posted.endswith("`devcake:v1`")
    assert posted.count("`devcake:v1`") == 1
    # at/below the threshold: inline, no upload
    run_coro(mgr._feed("p1", "issue", "y" * 2048))
    assert len(fake.uploads) == 1
    assert "y" * 2048 in fake.comments[-1]


def test_reject_report_attached_feed_short_pr_full(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    report = "finding: " + "R" * 3000
    run_coro(mgr._finalize_review(_run("REVIEW", "DEVCAKE-REVIEW"),
                                  {"verdict": "reject", "report_md": report}))
    assert any(n == "1_REVIEW_REPORT.md" for n, _ in fake.uploads)
    feed = next(c for c in fake.comments if "REVIEW rejected" in c)
    assert "round 1" in feed and "1_REVIEW_REPORT.md" in feed
    assert "R" * 3000 not in feed                     # short comment, not the dump
    assert "R" * 3000 in forge.pr_comments[-1]        # PR keeps the full report
    assert "DEVCAKE-EXECUTE" in m.labels and "DEVCAKE-REVIEW" not in m.labels


def test_reject_report_upload_redacts_run_password(tmp_path):
    # the attachment bypasses _feed's active-run redaction — the upload must
    # scrub the finishing run's relay password itself
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    run.redis_password = "s3cret-relay-pw"
    report = "leaked: s3cret-relay-pw\n" + "R" * 3000
    run_coro(mgr._finalize_review(run, {"verdict": "reject",
                                        "report_md": report}))
    name, data = next(u for u in fake.uploads if u[0] == "1_REVIEW_REPORT.md")
    assert b"s3cret-relay-pw" not in data


# ── docs/03 §4.1 merge-failure branches ──────────────────────────────────────

def _approve_review(mgr):
    return mgr._finalize_review(_run("REVIEW", "DEVCAKE-REVIEW"),
                                {"verdict": "approve", "report_md": "ok"})


def merge_fail_mgr(tmp_path, mergeable_result):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge(merge_exc=ForgeError("PUT /pulls/8/merge → 405: conflicts",
                                           status=405),
                      mergeable_result=mergeable_result)
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.config.auto_merge = True
    return m, mgr, fake, forge


def test_merge_conflict_routes_back_to_execute(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=False)
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-EXECUTE" in m.labels
    assert "DEVCAKE-MERGE" not in m.labels and "DEVCAKE-REVIEW" not in m.labels
    assert any("`devcake:conflict-resolve:1`" in c for c in fake.comments)


def test_merge_conflict_cap_falls_back_to_merge(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=False)
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body="🔀 rework `devcake:conflict-resolve:2`\n\n`devcake:v1`")]
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-MERGE" in m.labels and "DEVCAKE-EXECUTE" not in m.labels
    assert not any("`devcake:conflict-resolve:3`" in c for c in fake.comments)
    assert any("auto-merge failed" in c and "`devcake:merge-handoff`" in c
               for c in fake.comments)


def test_quoted_conflict_marker_does_not_count(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=False)
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="felipe", kind="comment",
        body="as devcake said:\n> 🔀 rework `devcake:conflict-resolve:2`")]
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-EXECUTE" in m.labels               # counted 0, not 2
    assert any("`devcake:conflict-resolve:1`" in c for c in fake.comments)


def test_merge_conflict_toggle_off_keeps_current_behavior(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=False)
    mgr.config.auto_resolve_merge_conflicts = False
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-MERGE" in m.labels and "DEVCAKE-EXECUTE" not in m.labels
    assert not any("devcake:conflict-resolve" in c for c in fake.comments)
    assert any("auto-merge failed" in c for c in fake.comments)


def test_non_conflict_merge_failure_defers(tmp_path):
    # mergeable unknown (CI running / still computing) → DEVCAKE-MERGE with an
    # active retry window, not the terminal hand-off
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=None)
    run_coro(_approve_review(mgr))
    assert "DEVCAKE-MERGE" in m.labels
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert not any("`devcake:merge-handoff`" in c for c in fake.comments)


def test_zero_window_skips_deferred_retry(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=None)
    mgr.config.merge_retry_window_minutes = 0
    run_coro(_approve_review(mgr))
    assert any("`devcake:merge-handoff`" in c for c in fake.comments)
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)


# ── docs/03 §4.1 deferred-merge sweep window ─────────────────────────────────

def sweep_mgr(tmp_path, mergeable_result, merge_exc=None):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    forge = FakeForge(merge_exc=merge_exc, mergeable_result=mergeable_result)
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.config.auto_merge = True
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body="⏳ deferred `devcake:merge-retry`\n\n`devcake:v1`")]
    return m, mgr, fake, forge


def test_sweep_merges_when_ready(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == [8]
    assert "DEVCAKE-MERGE" not in m.labels and m.status == "done"
    assert any("Merged after deferred retry" in c for c in fake.comments)


def test_sweep_waits_while_computing(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == [] and fake.comments == []
    assert "DEVCAKE-MERGE" in m.labels                # untouched, next cycle re-reads


def test_sweep_routes_conflict_to_execute(tmp_path):
    m, mgr, fake, forge = sweep_mgr(
        tmp_path, mergeable_result=False,
        merge_exc=ForgeError("405: conflicts", status=405))
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == [8]                # merge tried before the rework
    assert "DEVCAKE-EXECUTE" in m.labels and "DEVCAKE-MERGE" not in m.labels
    assert any("`devcake:conflict-resolve:1`" in c for c in fake.comments)


def test_sweep_merges_behind_branch_without_rework(tmp_path):
    # "behind" reads as False but is only blocking under strict up-to-date
    # rules — when the plain merge succeeds, no EXECUTE rework is dispatched
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=False)
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == [8] and m.status == "done"
    assert "DEVCAKE-EXECUTE" not in m.labels
    assert not any("devcake:conflict-resolve" in c for c in fake.comments)


def test_sweep_window_expiry_hands_off_once(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    mgr.config.merge_retry_window_minutes = 0          # expires immediately
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == []                          # no merge attempt past expiry
    handoffs = [c for c in fake.comments if "`devcake:merge-handoff`" in c]
    assert len(handoffs) == 1 and "DEVCAKE-MERGE" in m.labels
    # the hand-off marker closes the window: later sweeps skip this mission
    fake.activity_entries.append(ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body=handoffs[0]))
    run_coro(mgr._merge_sweep(m))
    assert len([c for c in fake.comments
                if "`devcake:merge-handoff`" in c]) == 1


def test_sweep_ignores_missions_without_retry_marker(tmp_path):
    # auto_merge-OFF parks carry no marker — the sweep must not touch them
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    run_coro(mgr._merge_sweep(m))
    assert forge.merges == [] and fake.comments == []
    assert "DEVCAKE-MERGE" in m.labels


# ── docs/11 awaiting-human-merge advisory + sweep skip-set ───────────────────

def test_manual_park_banners_without_feed_read(tmp_path):
    # auto_merge OFF: the banner entry derives from labels alone — zero
    # get_activity calls for the operator's normal merge queue
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    mgr.config.auto_merge = False
    run_coro(mgr._merge_sweep(m))
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]
    assert getattr(fake, "get_activity_calls", 0) == 0


def test_active_retry_window_suppresses_banner(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    run_coro(mgr._merge_sweep(m))
    assert m.pmo_id not in mgr.merge_handoffs        # DevCake is still driving
    assert m.pmo_id not in mgr._merge_window_closed


def test_expired_window_banners_and_skips_future_feed_reads(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    mgr.config.merge_retry_window_minutes = 0        # expires immediately
    run_coro(mgr._merge_sweep(m))
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]
    assert m.pmo_id in mgr._merge_window_closed
    calls = fake.get_activity_calls
    run_coro(mgr._merge_sweep(m))                    # second cycle: skip-set hit
    assert fake.get_activity_calls == calls          # no new feed read
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]


def test_closed_window_park_banners_without_repeat_reads(tmp_path):
    # no retry marker at all (e.g. restart onto an old park): one feed read
    # discovers the closed window, then the skip-set takes over
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    run_coro(mgr._merge_sweep(m))
    run_coro(mgr._merge_sweep(m))
    assert fake.get_activity_calls == 1
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]


def test_sweeps_prune_drops_departed_missions(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})   # left MERGE
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    mgr.merge_handoffs = {"p1": "T-1: awaiting human merge — x"}
    mgr._merge_window_closed = {"p1"}
    run_coro(mgr.sweeps([m]))
    assert mgr.merge_handoffs == {} and mgr._merge_window_closed == set()


def test_fresh_retry_marker_reopens_window(tmp_path):
    m, mgr, fake, forge = merge_fail_mgr(tmp_path, mergeable_result=None)
    mgr._merge_window_closed = {"p1"}                # stale from a prior episode
    run_coro(_approve_review(mgr))
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert "p1" not in mgr._merge_window_closed      # new episode reopened


# ── app-level verdicts on the run record (admin diagnostics) ─────────────────

def test_illegal_outcome_sets_verdict(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _run()
    run_coro(mgr._transition(run, {"outcome": "reviewed", "verdict": "approve"},
                             None))
    assert run.verdict and run.verdict.startswith("rejected:")
    assert "illegal for EXECUTE" in run.verdict


def test_external_transition_sets_verdict(tmp_path):
    m = mission(labels={"DEVCAKE", "DEVCAKE-PLAN"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-XXXXXX", mission_key="T-1",
              mission_pmo_id="p1", mission_type="ONBOARD",
              dev_type="senior-dev", seq=1, stage_label_at_dispatch=None)
    run_coro(mgr._transition(run, {"outcome": "plan_needed"}, None))
    assert run.verdict and run.verdict.startswith("skipped:")


def test_human_needed_sets_verdict_and_advisory(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _run()
    run_coro(mgr._transition(run, {"outcome": "human_needed", "summary": "s"},
                             None))
    assert run.verdict == "handed off: needs human on EXECUTE"
    assert "p1" in mgr.needs_human
    assert mgr.needs_human["p1"].startswith("T-1: needs human")
