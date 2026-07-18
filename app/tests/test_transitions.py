"""INV-3 (no-lock atomicity: compare-and-transition, failure symmetry) and
INV-5 (transcript + token report always posted) — exercised against a FakePMO.
Also the docs/03 §4.1 merge-failure branches (conflict rework, deferred retry)
against a FakeForge, and the docs/05 §4 attachment policy."""
import asyncio
from datetime import datetime, timezone

import pytest

from devcake.config import AppConfig, DevType
from devcake.ports.forge import ForgeError, PullRequest
from devcake.domain.orchestrator import MissionManager
from devcake.domain.model import Activity, ActivityEntry, Mission
from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run


class FakePMO:
    """Implements the unified PMOPort surface (MissionRef-keyed; the adapter
    owns kind dispatch). STRICT on ref types: a caller passing a bare pmo_id
    string must fail here in tests, not in production (the DEV-62 dispatch
    regression). Project feed posts land in project_updates so tests can
    assert the baton path separately from issue comments."""

    @staticmethod
    def _check_ref(ref):
        from devcake.domain.model import MissionRef
        assert isinstance(ref, MissionRef), f"port called with {ref!r}, not a MissionRef"

    def __init__(self, mission):
        self.mission = mission
        self.comments = []
        self.swaps = []
        self.statuses = []
        self.created = []       # (title, parent_ref)
        self.relations = []     # (blocker_id, blocked_id)
        self.uploads = []       # (name, bytes)
        self.activity_entries = []
        self.all_missions = [mission]
        self.ops = []           # chronological (op, …) log for ordering asserts
        self.fail_relations = set()   # (blocker, blocked) pairs that raise

    async def get(self, ref):
        self._check_ref(ref)
        return self.mission

    async def post_feed(self, ref, markdown):
        self._check_ref(ref)
        if ref.kind == "project":
            self.project_updates = getattr(self, "project_updates", [])
            self.project_updates.append((ref.pmo_id, markdown))
        else:
            self.comments.append(markdown)

    async def swap_labels(self, ref, remove, add):
        self._check_ref(ref)
        self.swaps.append((set(remove), set(add)))
        self.mission.labels = (self.mission.labels - set(remove)) | set(add)

    async def set_status(self, ref, status):
        self._check_ref(ref)
        self.statuses.append(status)
        self.ops.append(("status", status))
        self.mission.status = status

    async def cancel_mission(self, ref):
        await self.set_status(ref, "canceled")

    async def upload_attachment(self, pmo_id, filename, data):
        self.uploads.append((filename, data))
        return f"https://fake/{filename}"

    async def get_activity(self, ref, full=False):
        self._check_ref(ref)
        self.get_activity_calls = getattr(self, "get_activity_calls", 0) + 1
        return Activity(mission=self.mission, entries=list(self.activity_entries))

    async def create_mission(self, team_ref, title, description, priority,
                             label_names, parent_ref=None):
        self.created.append((title, parent_ref))
        key, pmo_id = f"T-{len(self.created) + 1}", f"id-{len(self.created)}"
        self.all_missions.append(Mission(
            instance="linear", pmo_id=pmo_id, pmo_kind="issue", key=key, title=title,
            description=description, status="backlog", priority=priority,
            labels=set(label_names), updated_at=datetime.now(timezone.utc),
            parent_ref=parent_ref,
        ))
        return key, pmo_id

    async def list_all(self, team_ref):
        return list(self.all_missions)

    async def append_description(self, ref, text):
        self._check_ref(ref)
        if getattr(self, "fail_append", False):
            raise RuntimeError("description update refused")
        self.mission.description = (self.mission.description or "") + text
        self.ops.append(("append_description", text))

    async def create_relation(self, blocker_id, blocked_id):
        if (blocker_id, blocked_id) in self.fail_relations:
            raise RuntimeError("relation refused")
        self.relations.append((blocker_id, blocked_id))
        self.ops.append(("relation", blocker_id, blocked_id))

    async def children_of(self, ref):
        self._check_ref(ref)
        return list(getattr(self, "children", []))


class NullMessaging:
    async def create_run_user(self, rid): return "pw"
    async def delete_run_user(self, rid): pass
    async def delete_reply_stream(self, rid): pass


def mission(status="in_progress", labels=frozenset({"DEVCAKE"})):
    return Mission(instance="linear", pmo_id="p1", pmo_kind="issue", key="T-1", title="t",
                   status=status, labels=set(labels), repo="main",
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
        return PullRequest(number=8, url="https://forge/pr/8", state="open")

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
        return PullRequest(number=pr_number, url="https://forge/pr/8",
                           state="open", merged=False)

    @staticmethod
    def approval_footer(pr_url):
        return "\n\n---\nfooter"


def make_mgr(tmp_path, m, forge=None):
    from fakes import make_mission_manager
    cfg = AppConfig()
    fake = FakePMO(m)
    mgr = make_mission_manager(
        tmp_path, pmo=fake, forge=forge, config=cfg,
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(),
        noop_audit=True,  # transition tests assert labels/feed, not audit
    )
    return mgr, fake, mgr.runs.store


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


def test_awaiting_merge_redelivery_not_misread_as_external(tmp_path):
    """Audit A6: review:awaiting_merge swaps REVIEW→MERGE but was missing
    from _SWAP_MARKER_STAGE — a crash between the checkpoint and the coarse
    transition marker made the redelivery see MERGE as an EXTERNAL change,
    posting a false 'state was changed externally' comment and recording a
    wrong skipped verdict (the exact defect class the map exists to stop)."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})   # our own swap
    forge = FakeForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.config.auto_merge = False
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    run.finalized_steps = ["review:awaiting_merge"]            # checkpointed
    run_coro(mgr._transition(run, {"outcome": "reviewed", "verdict": "approve",
                                   "report_md": "ok"}, None))
    assert not any("changed externally" in c for c in fake.comments)
    assert not (run.verdict or "").startswith("skipped:")
    assert fake.swaps == []                    # idempotent — no re-swap either


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


def test_attempts_count_across_transcript_sequences_and_reset(tmp_path, monkeypatch):
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "no-audit.jsonl")
    for seq in (1, 2):
        r = _run("ONBOARD", None)
        r.run_id = f"T-1-{seq}-ONBOARD-FAIL"
        r.seq = seq
        r.state = "failed"
        r.error = "DEV_BAD_OUTPUT"
        store.save(r)
    assert mgr._attempt_number("p1", "ONBOARD") == 3

    auth = _run("ONBOARD", None)
    auth.run_id = "T-1-3-ONBOARD-AUTH"
    auth.state = "failed"
    auth.error = "DEV_FORGE_AUTH: denied"
    store.save(auth)
    assert mgr._attempt_number("p1", "ONBOARD") == 3

    success = _run("ONBOARD", None)
    success.run_id = "T-1-4-ONBOARD-OK"
    success.state = "finished"
    store.save(success)
    assert mgr._attempt_number("p1", "ONBOARD") == 1


def test_attempts_reset_when_other_step_finishes(tmp_path, monkeypatch):
    """A later step finishing implies the failing step was resolved (manually
    or otherwise) — the failure count must not leak into the new step."""
    from datetime import timedelta
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "no-audit.jsonl")
    t0 = datetime.now(timezone.utc)
    for i in (1, 2):
        r = _run("EXECUTE", None)
        r.run_id = f"T-1-{i}-EXECUTE-FAIL"
        r.seq = i + 1
        r.state = "failed"
        r.error = "DEV_BAD_OUTPUT"
        r.created_at = t0 + timedelta(seconds=i)
        store.save(r)
    assert mgr._attempt_number("p1", "EXECUTE") == 3

    failed_review = _run("REVIEW", None)
    failed_review.run_id = "T-1-3-REVIEW-FAIL"
    failed_review.state = "failed"
    failed_review.error = "DEV_BAD_OUTPUT"
    failed_review.created_at = t0 + timedelta(seconds=3)
    store.save(failed_review)
    assert mgr._attempt_number("p1", "EXECUTE") == 3   # failure ≠ resolution

    stray = _run("MAPPER", None)
    stray.run_id = "T-0-1-MAPPER-OK"
    stray.mission_pmo_id = ""
    stray.state = "finished"
    stray.created_at = t0 + timedelta(seconds=4)
    store.save(stray)
    assert mgr._attempt_number("p1", "EXECUTE") == 3   # other missions don't reset

    ok_review = _run("REVIEW", None)
    ok_review.run_id = "T-1-4-REVIEW-OK"
    ok_review.state = "finished"
    ok_review.created_at = t0 + timedelta(seconds=5)
    store.save(ok_review)
    assert mgr._attempt_number("p1", "EXECUTE") == 1


def test_attempts_reset_on_human_activity(tmp_path, monkeypatch):
    """A human comment on the mission is an intervention: the step gets fresh
    attempts. DevCake's own sentinel-signed comments never reset."""
    from datetime import timedelta
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "no-audit.jsonl")
    t0 = datetime.now(timezone.utc)
    for i in (1, 2):
        r = _run("EXECUTE", None)
        r.run_id = f"T-1-{i}-EXECUTE-FAIL"
        r.state = "failed"
        r.error = "DEV_BAD_OUTPUT"
        r.created_at = t0 + timedelta(seconds=i)
        store.save(r)

    devcake_note = ActivityEntry(ts=t0 + timedelta(seconds=10), author="devcake",
                                 kind="comment", body="posted\n\n`devcake:v1`")
    old_human = ActivityEntry(ts=t0 - timedelta(minutes=5), author="felix",
                              kind="comment", body="please pick this up")
    activity = Activity(mission=m, entries=[devcake_note, old_human])
    assert mgr._attempt_number("p1", "EXECUTE", activity) == 3

    human = ActivityEntry(ts=t0 + timedelta(seconds=20), author="felix",
                          kind="comment", body="resolved this by hand, carry on")
    activity = Activity(mission=m, entries=[devcake_note, old_human, human])
    assert mgr._attempt_number("p1", "EXECUTE", activity) == 1


def test_forge_auth_artifact_trips_repo_breaker(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, _store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    error = mgr.dev_failure_error(run, {
        "exit_code": 13,
        "error_class": "DEV_FORGE_AUTH",
        "error_detail": "remote returned 403: write access not granted",
    })
    assert error.startswith("DEV_FORGE_AUTH:")
    # M10: the latch is per-repo on the runtime, never the dev-type dict
    assert "main" in mgr.forges.breakers and not mgr.breakers


def test_stderr_403_without_error_class_does_not_trip_breaker(tmp_path):
    """A bare '403' in stderr (rate limit, URL fragment) must not halt all
    dispatch — only the Dev's structured DEV_FORGE_AUTH class may latch."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, _store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    error = mgr.dev_failure_error(run, {
        "exit_code": 13,
        "error_detail": "fatal: unable to access 'https://forge/team-403/repo/': timeout",
    })
    assert error.startswith("DEV_FORGE")
    assert "forge" not in mgr.breakers


def test_mcp_setup_artifact_maps_to_dev_mcp_setup(tmp_path):
    """Exit-14 artifacts (newly reachable: the runspec now delivers
    mcp_setup_commands and the entrypoint sends artifacts before exiting)
    map to a visible DEV_MCP_SETUP error naming the failed command; they
    trip NO breaker and DO count toward attempts (a transient install
    failure deserves counted retries, unlike auth)."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    run = _run("EXECUTE", None)
    error = mgr.dev_failure_error(run, {
        "exit_code": 14,
        "error_class": "DEV_MCP_SETUP",
        "error_detail": "claude mcp add logs -e K=$DD_API_KEY -- x: "
                        "exit 1: unknown flag",
    })
    assert error.startswith("DEV_MCP_SETUP:")
    assert "claude mcp add logs" in error
    assert not mgr.breakers and not mgr.forges.breakers
    run.state = "failed"
    run.error = error
    store.save(run)
    assert mgr._attempt_number("p1", "EXECUTE", None) == 2   # counted


def test_executed_trivially_is_illegal_and_parks(tmp_path):
    """Founder decision 2026-07-18 (rode ADR-0014's PR): ONBOARD never
    implements — trivial work rides the opportunistic-plan path to EXECUTE
    (the only stage holding a write token). `executed_trivially` is gone
    outright (preproduction, no deprecation window); a stray one parks."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run("ONBOARD", None),
                             {"outcome": "executed_trivially",
                              "pr_url": "https://forge/mr/1", "summary": "s"},
                             None))
    assert ({"DEVCAKE-SKIP"} in [add for _, add in fake.swaps])  # parked
    assert "DEVCAKE-REVIEW" not in m.labels
    assert any("not a legal outcome" in c for c in fake.comments)


def test_onboard_opportunistic_plan_skips_plan_stage(tmp_path):
    """The trivial path's replacement: ONBOARD attaches the plan it already
    formed and the mission jumps straight to EXECUTE."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(mgr._transition(_run("ONBOARD", None),
                             {"outcome": "plan_needed", "summary": "s"},
                             "## Plan\nappend the line to README.md"))
    assert any(n == "PLAN_1.md" for n, _ in fake.uploads)
    assert any("opportunistic plan" in c for c in fake.comments)
    assert ({"DEVCAKE-EXECUTE"} in [add for _, add in fake.swaps])


def test_apply_health_and_latch_noop_for_unregistered_repo():
    """Audit A29 (resurrection race): a probe or finalize latch completing
    AFTER a rebuild/delete removed the repo must not re-create health or
    breaker entries — refresh_all only walks registered repos, so a stale
    entry would sit in /health until the next config PUT."""
    from devcake.domain.forge_runtime import ForgeRuntime
    rt = ForgeRuntime()
    rt.apply_health("ghost", {"ok": False, "transient": False, "detail": "401"})
    rt.latch("ghost", "401")
    assert "ghost" not in rt.health and "ghost" not in rt.breakers


def test_apply_forge_health_breaker_policy():
    """The latch/clear policy moved to ForgeRuntime (M10): per repo name."""
    from devcake.domain.forge_runtime import ForgeRuntime
    rt = ForgeRuntime()
    rt.forges = {"main": object(), "a": object(), "b": object()}
    rt.apply_health("main", {"ok": False, "transient": False, "detail": "401 bad token"})
    assert rt.breakers["main"] == "401 bad token"            # definitive latches
    rt.apply_health("main", {"ok": False, "transient": True, "detail": "HTTP 500"})
    assert rt.breakers["main"] == "401 bad token"            # transient never clears
    rt.breakers.clear()
    rt.apply_health("main", {"ok": False, "transient": True, "detail": "HTTP 500"})
    assert "main" not in rt.breakers                         # transient never latches
    rt.breakers["main"] = "stale"
    rt.apply_health("main", {"ok": True, "detail": ""})
    assert "main" not in rt.breakers                         # success clears
    # isolation: repo A's latch never blocks repo B
    rt.apply_health("a", {"ok": False, "transient": False, "detail": "401"})
    assert "a" in rt.breakers and "b" not in rt.breakers


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


def test_decomposition_children_inherit_repo_marker(tmp_path):
    """Audit A24 (founder decision 2026-07-14): a marker-routed parent's
    children carry the same `devcake-repo:` marker — the mission family
    stays on one repo instead of silently splitting to the instance default
    (or per-child internal repos)."""
    m = mission("in_progress", {"DEVCAKE"})
    m.description = "Do the big thing\n\n`devcake-repo:beta`"
    mgr, fake, _store = make_mgr(tmp_path, m)
    drafts = [{"title": "docs"}, {"title": "code", "blocked_by": [1]}]
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None), {"outcome": "decomposed", "decomposition": drafts}))
    children = fake.all_missions[1:]
    assert len(children) == 2
    assert all("`devcake-repo:beta`" in c.description for c in children)
    # replay with the marker present stays idempotent
    m.status = "in_progress"
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None), {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.created) == 2


def test_decomposition_children_clean_without_marker(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "solo"}]}))
    assert "devcake-repo" not in fake.all_missions[1].description


def test_decomposition_children_inherit_containing_project(tmp_path):
    """ADR-0012: an issue's children stay in its containing project (the
    tracking sweep then waits for them); standalone issues stay standalone;
    a project original keeps parenting its children (pinned)."""
    m = mission("in_progress", {"DEVCAKE"})
    m.parent_ref = "proj-9"
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "a"},
                                                    {"title": "b"}]}))
    assert [pr for _, pr in fake.created] == ["proj-9", "proj-9"]

    solo = mission("in_progress", {"DEVCAKE"})
    mgr2, fake2, _ = make_mgr(tmp_path / "solo", solo)
    run_coro(mgr2._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "c"}]}))
    assert [pr for _, pr in fake2.created] == [None]

    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr3, fake3, _ = make_mgr(tmp_path / "proj", proj)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(mgr3._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": [{"title": "d"}]}))
    assert [pr for _, pr in fake3.created] == ["p1"]


def test_tracking_sweep_waits_for_open_grandchildren(tmp_path):
    """First _tracking_sweep coverage: a canceled (replaced-by-decomposition)
    child counts terminal, but any open issue in the project — including a
    grandchild — holds the project open."""
    proj = mission("in_progress", {"DEVCAKE-TRACKING"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    canceled_child = mission("canceled", {"DEVCAKE", "DEVCAKE-CREATED"})
    grandchild = mission("backlog", {"DEVCAKE", "DEVCAKE-CREATED"})
    fake.children = [canceled_child, grandchild]
    run_coro(mgr._tracking_sweep(proj))
    assert proj.status == "in_progress"           # grandchild holds it open
    grandchild.status = "done"
    run_coro(mgr._tracking_sweep(proj))
    assert proj.status == "done"
    assert "DEVCAKE-TRACKING" not in proj.labels


def dep_mission(pmo_id, key, status="backlog", blocked_by=()):
    m = Mission(instance="linear", pmo_id=pmo_id, pmo_kind="issue", key=key,
                title=key, status=status, labels={"DEVCAKE"}, repo="main",
                updated_at=datetime.now(timezone.utc),
                blocked_by=list(blocked_by))
    return m


def test_decomposition_inherits_dependent_edges_before_cancel(tmp_path):
    """ADR-0012, the ordering bug fix: every still-open dependent of the
    original is re-pointed at EVERY child, and all inherited edges exist
    before the original is canceled — the gate never sees a canceled
    original without replacement edges."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.all_missions.append(dep_mission("d1", "T-90", blocked_by=["p1"]))
    fake.all_missions.append(dep_mission("d2", "T-91", status="done",
                                         blocked_by=["p1"]))
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "a"},
                                                    {"title": "b"}]}))
    assert ("id-1", "d1") in fake.relations
    assert ("id-2", "d1") in fake.relations
    assert not any(blocked == "d2" for _, blocked in fake.relations)
    cancel_at = fake.ops.index(("status", "canceled"))
    for i, op in enumerate(fake.ops):
        if op[0] == "relation":
            assert i < cancel_at, "inherited edge wired after the cancel"


def test_decomposition_inherits_open_blockers_onto_children(tmp_path):
    """Inbound edges: the original's still-open blockers gate every child;
    terminal blockers are not resurrected; an off-snapshot blocker counts
    as open (additive fail-safe)."""
    m = mission("in_progress", {"DEVCAKE"})
    m.blocked_by = ["b-open", "b-done", "ghost"]
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.all_missions.append(dep_mission("b-open", "T-80"))
    fake.all_missions.append(dep_mission("b-done", "T-81", status="done"))
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "a"},
                                                    {"title": "b"}]}))
    for child in ("id-1", "id-2"):
        assert ("b-open", child) in fake.relations
        assert ("ghost", child) in fake.relations
        assert ("b-done", child) not in fake.relations


def test_inherited_edge_failure_keeps_original_open_and_gated(tmp_path):
    """Fail-closed (ADR-0012): an inherited-edge failure aborts finalization
    BEFORE the cancel — the original stays open and keeps gating its
    dependents; the retry (same run, checkpoints intact) completes once the
    edge succeeds. Canceling past a missing edge would silently release
    downstream work early."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.all_missions.append(dep_mission("d1", "T-90", blocked_by=["p1"]))
    fake.fail_relations = {("id-1", "d1")}
    run = _run("ONBOARD", None)
    drafts = [{"title": "a"}, {"title": "b"}]
    with pytest.raises(RuntimeError):
        run_coro(mgr._finalize_decomposition(
            run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status != "canceled"                 # original still gates D
    assert ("status", "canceled") not in fake.ops

    fake.fail_relations = set()                   # transient error heals
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status == "canceled"
    assert ("id-1", "d1") in fake.relations and ("id-2", "d1") in fake.relations

    m2 = mission("in_progress", {"DEVCAKE"})      # sibling edges stay strict
    mgr2, fake2, _ = make_mgr(tmp_path / "sib", m2)
    fake2.fail_relations = {("id-1", "id-2")}
    with pytest.raises(RuntimeError):
        run_coro(mgr2._finalize_decomposition(
            _run("ONBOARD", None),
            {"outcome": "decomposed",
             "decomposition": [{"title": "a"},
                               {"title": "b", "blocked_by": [1]}]}))


def test_inherited_edge_to_deleted_dependent_converges_on_retry(tmp_path):
    """A dependent deleted mid-finalize fails its edge strictly, but the
    retry recomputes dependents from a fresh snapshot — the vanished mission
    drops out and finalization completes without it."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    doomed = dep_mission("d1", "T-90", blocked_by=["p1"])
    fake.all_missions.append(doomed)
    fake.fail_relations = {("id-1", "d1"), ("id-2", "d1")}
    run = _run("ONBOARD", None)
    drafts = [{"title": "a"}, {"title": "b"}]
    with pytest.raises(RuntimeError):
        run_coro(mgr._finalize_decomposition(
            run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status != "canceled"

    fake.all_missions.remove(doomed)              # human deleted the issue
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status == "canceled"
    assert not any(blocked == "d1" for _, blocked in fake.relations)


def test_lineage_note_failure_never_blocks_cancel(tmp_path):
    """The note is hygiene, not safety: a failing append_description audits
    and finalization still cancels the original."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.fail_append = True
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert m.status == "canceled"
    assert "_Decomposed by DevCake into" not in (m.description or "")


def test_depth_park_restores_backlog_and_sets_verdict(tmp_path):
    """The depth park mirrors the conflict park: status back to backlog (no
    phantom in-progress) and a 'handed off:' verdict so the run never reads
    as a clean success."""
    from test_decomposition_depth import marker
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = marker(depth=2)
    mgr, fake, _store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert "DEVCAKE-SKIP" in m.labels
    assert m.status == "backlog"
    assert run.verdict == "handed off: decomposition depth limit"


def test_depth_gate_replay_stable_after_limit_change(tmp_path):
    """Once child checkpoints exist the decomposition decision was taken —
    lowering the limit mid-resume must finish the wiring, not strand live
    children behind a SKIP park."""
    from test_decomposition_depth import marker
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-CREATED"})
    m.description = "part\n\n" + marker()          # level 1, legal at limit 2
    mgr, fake, _store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    drafts = [{"title": "a"}, {"title": "b"}]
    fake.fail_relations = {("id-1", "id-2")}       # crash mid-wiring
    with pytest.raises(RuntimeError):
        run_coro(mgr._finalize_decomposition(
            run, {"outcome": "decomposed",
                  "decomposition": [{"title": "a"},
                                    {"title": "b", "blocked_by": [1]}]}))
    mgr.config.max_decomposition_depth = 1         # operator lowers the limit
    fake.fail_relations = set()
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed",
              "decomposition": [{"title": "a"},
                                {"title": "b", "blocked_by": [1]}]}))
    assert "DEVCAKE-SKIP" not in m.labels
    assert m.status == "canceled"                  # wiring completed


def test_inherited_edges_replay_semantics(tmp_path):
    """Same-run redelivery re-executes nothing; a fresh run only re-creates
    duplicate-tolerated edges (membership stable, children reused)."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.all_missions.append(dep_mission("d1", "T-90", blocked_by=["p1"]))
    drafts = [{"title": "a"}, {"title": "b"}]
    first = _run("ONBOARD", None)
    run_coro(mgr._finalize_decomposition(
        first, {"outcome": "decomposed", "decomposition": drafts}))
    count = len(fake.relations)
    run_coro(mgr._finalize_decomposition(
        first, {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.relations) == count           # checkpoints all skipped

    m.status = "in_progress"
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.created) == 2                 # children reused
    assert ("id-1", "d1") in fake.relations and ("id-2", "d1") in fake.relations


def test_canceled_parent_gets_lineage_note(tmp_path):
    """ADR-0012 hygiene: the canceled original carries a durable description
    note naming every child — appended before the cancel, exactly once even
    across a fresh-run replay."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    drafts = [{"title": "a"}, {"title": "b"}]
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None), {"outcome": "decomposed",
                                "decomposition": drafts}))
    assert "_Decomposed by DevCake into T-2, T-3_" in m.description
    note_at = fake.ops.index(("append_description",
                              "\n\n---\n_Decomposed by DevCake into T-2, T-3_"))
    assert note_at < fake.ops.index(("status", "canceled"))

    m.status = "in_progress"                      # fresh-run replay
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None), {"outcome": "decomposed",
                                "decomposition": drafts}))
    assert m.description.count("_Decomposed by DevCake into") == 1


def test_project_decomposition_appends_no_lineage_note(tmp_path):
    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert not any(op[0] == "append_description" for op in fake.ops)


def test_project_decomposition_inherits_nothing(tmp_path):
    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    fake.all_missions.append(dep_mission("d1", "T-90", blocked_by=["p1"]))
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(mgr._finalize_decomposition(
        run, {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert not any(blocked == "d1" for _, blocked in fake.relations)


def test_decomposition_replay_reuses_marker_parts(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    drafts = [{"title": "docs"}, {"title": "code", "blocked_by": [1]}]
    first = _run("ONBOARD", None)
    run_coro(mgr._finalize_decomposition(
        first, {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.created) == 2

    m.status = "in_progress"
    second = _run("ONBOARD", None)
    run_coro(mgr._finalize_decomposition(
        second, {"outcome": "decomposed", "decomposition": drafts}))

    assert len(fake.created) == 2
    assert fake.relations[-1] == ("id-1", "id-2")
    assert "DEVCAKE-NEEDS-HUMAN" not in m.labels


def test_decomposition_manifest_conflict_hands_off_without_writes(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(mgr._finalize_decomposition(
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "original"}]}))
    assert len(fake.created) == 1

    m.status = "in_progress"
    retry = _run("ONBOARD", None)
    run_coro(mgr._finalize_decomposition(
        retry,
        {"outcome": "decomposed", "decomposition": [{"title": "changed"}]}))

    assert len(fake.created) == 1
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels
    assert retry.verdict == "handed off: decomposition replay conflict"
    assert any("no children were created" in comment for comment in fake.comments)


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


def _finalize_payload(**over):
    base = {"result": {"outcome": "plan_needed", "summary": "s"},
            "transcript_md": "FULL DUMP",
            "token_report": {"extraction_method": "unavailable", "model": "m"}}
    base.update(over)
    return base


def _saved_run(store):
    run = Run(run_id="T-1-1-ONBOARD-ZZZZZZ", mission_key="T-1",
              mission_pmo_id="p1", mission_type="ONBOARD",
              dev_type="senior-dev", seq=1, stage_label_at_dispatch=None,
              state="finalizing")
    store.save(run)
    return run


def test_finalize_posts_last_message_blockquoted(tmp_path):
    # ADR-0014 D1 flip: attachment = full dump; comment = step line + the
    # last message, EVERY line `>`-prefixed (blank lines as bare `>`)
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(
        last_message_md="Done.\n\nDetails here.")))
    assert ("1_ONBOARD.md", b"FULL DUMP") in fake.uploads
    comment = next(c for c in fake.comments if "`1_ONBOARD.md`" in c)
    assert "https://fake/1_ONBOARD.md" in comment
    assert "> Done." in comment and "> Details here." in comment
    lines = comment.splitlines()
    blank_between = lines[lines.index("> Done.") + 1]
    assert blank_between == ">"                    # blank lines quoted too
    assert comment.count("`devcake:v1`") == 1
    assert "Details here." not in "\n".join(       # no unquoted model text
        l for l in lines if not l.lstrip().startswith(">"))
    assert MissionManager._is_devcake_comment(comment)   # provenance holds


def test_finalize_without_last_message_keeps_pointer_comment(tmp_path):
    # rolling-deploy pin: an old-image payload (no last_message_md) — and an
    # empty one — posts exactly today's pointer-only comment, never derived
    # from the dump
    for payload in (_finalize_payload(), _finalize_payload(last_message_md="")):
        m = mission("in_progress", {"DEVCAKE"})
        mgr, fake, store = make_mgr(tmp_path, m)
        run = _saved_run(store)
        run_coro(mgr.finalize(run, payload))
        comment = next(c for c in fake.comments if "`1_ONBOARD.md`" in c)
        assert "https://fake/1_ONBOARD.md" in comment
        assert not any(l.lstrip().startswith(">")
                       for l in comment.splitlines())
        assert "FULL DUMP" not in comment


def test_finalize_truncates_pathological_last_message(tmp_path):
    # a giant last message stays inline-bounded: truncated with a pointer,
    # no comment-*.md externalization (the full text is the attachment)
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(
        last_message_md="z" * 10_000)))
    assert [n for n, _ in fake.uploads] == ["1_ONBOARD.md"]   # only the dump
    comment = next(c for c in fake.comments if "`1_ONBOARD.md`" in c)
    # the notice itself is INSIDE the quoted block (quarantine holds)
    assert "> … (truncated — full text in the attachment)" in comment
    assert len(comment) < 4096


def test_finalize_last_message_markers_are_quarantined(tmp_path):
    # ADR-0014's core safety claim, at public seams: a last message that
    # mentions marker-shaped strings changes NOTHING in the state machine
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(
        last_message_md="see `9_EXECUTE.md` — ships as `T-1-deliverable.zip`")))
    comment = next(c for c in fake.comments if "`1_ONBOARD.md`" in c)
    entry = ActivityEntry(ts=datetime.now(timezone.utc), author="cake",
                          kind="comment", body=comment)
    assert MissionManager._derive_seq(
        Activity(mission=m, entries=[entry])) == 2      # only the real step
    from devcake.domain.orchestrator.feed import _unquoted
    assert "T-1-deliverable.zip" not in _unquoted(comment)


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


def test_finalize_upload_failure_posts_quoted_inline(tmp_path):
    # INV-5 fallback under ADR-0014: the inline transcript is model text and
    # must ride QUARANTINED — only the step-marker header stays unquoted
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)

    async def _boom(*a, **k):
        raise RuntimeError("upload down")
    fake.upload_attachment = _boom
    run_coro(mgr.finalize(run, _finalize_payload(
        transcript_md="dump mentions `9_EXECUTE.md`", last_message_md="Done.")))
    comment = next(c for c in fake.comments if "`1_ONBOARD.md`" in c)
    assert "> dump mentions" in comment                  # transcript quoted
    entry = ActivityEntry(ts=datetime.now(timezone.utc), author="cake",
                          kind="comment", body=comment)
    assert MissionManager._derive_seq(
        Activity(mission=m, entries=[entry])) == 2       # 9 never counts


def test_feed_externalized_preview_strips_quoted_lines(tmp_path):
    # the 300-char preview of an externalized comment flattens newlines —
    # quoted content must be dropped from it, or "> " prefixes land mid-line
    # and markers leak back into scan scope
    from devcake.domain.orchestrator.feed import _blockquote
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    body = "header line\n" + _blockquote("see `7_EXECUTE.md`\n" + "x" * 3000)
    run_coro(mgr._feed("p1", "issue", body))
    posted = fake.comments[-1]
    assert "full text attached:" in posted
    assert "7_EXECUTE.md" not in posted        # quoted text never previews


def test_feed_externalize_opt_out_posts_long_body_inline(tmp_path):
    # ADR-0014 D1: the finalize post opts out of the 2048 externalization;
    # redaction and the sentinel are untouched by the opt-out
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(mgr._feed("p1", "issue", "x" * 3000, externalize=False))
    assert not fake.uploads
    posted = fake.comments[-1]
    assert "x" * 3000 in posted
    assert posted.count("`devcake:v1`") == 1


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
    # scrub the finishing run's relay password, which redact() knows through
    # the runtime registry (populated at ACL creation, live until teardown)
    from devcake.security import register_runtime_secret, unregister_runtime_secret

    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    register_runtime_secret(run.run_id, "s3cret-relay-pw")
    try:
        report = "leaked: s3cret-relay-pw\n" + "R" * 3000
        run_coro(mgr._finalize_review(run, {"verdict": "reject",
                                            "report_md": report}))
    finally:
        unregister_runtime_secret(run.run_id)
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


# ── auto-merge OFF→ON re-arm (founder request 2026-07-15) ────────────────────
# A mission parked at DEVCAKE-MERGE while auto_merge was OFF carries no retry
# marker, so flipping auto_merge ON used to leave it awaiting a human forever
# (the sweep closed the window on first read and the skip-set cached it).
# The config PUT sets a one-shot re-arm flag on an OFF→ON flip: the next
# sweep posts a fresh retry-window entry (visible, marker-timestamped) for
# every parked mission; the cycle after that reads the marker and drives the
# merge inside the normal merge_retry_window_minutes bound.

def test_rearm_reopens_parked_mission_when_auto_merge_flips_on(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []            # the auto_merge-OFF park: no marker
    mgr.rearm_merge_windows = True        # what put_config sets on OFF→ON
    run_coro(mgr.sweeps([m]))             # cycle 1: posts the window entry
    rearm_comments = [c for c in fake.comments if "`devcake:merge-retry`" in c]
    assert len(rearm_comments) == 1
    assert m.pmo_id not in mgr._merge_window_closed
    assert mgr.rearm_merge_windows is False           # one-shot
    assert forge.merges == []                         # merge happens NEXT cycle
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body=rearm_comments[0])]
    run_coro(mgr.sweeps([m]))             # cycle 2: fresh marker → merge
    assert forge.merges == [8]
    assert m.status == "done"


def test_rearm_reaches_missions_already_in_skip_set(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    mgr._merge_window_closed = {"p1"}     # cached closed from prior cycles
    mgr.rearm_merge_windows = True
    run_coro(mgr.sweeps([m]))
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert "p1" not in mgr._merge_window_closed


def test_rearm_noop_when_window_zero(tmp_path):
    # window 0 = "hand off immediately" — flipping auto_merge ON must not
    # open a window the operator has configured away
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    mgr.config.merge_retry_window_minutes = 0
    mgr.rearm_merge_windows = True
    run_coro(mgr.sweeps([m]))
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)
    assert forge.merges == []
