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
from devcake.domain.orchestrator import decomposition, review, transitions
from devcake.domain.model import Activity, ActivityEntry, Mission, MissionType
from devcake.adapters.files.run_store import RunStore
from devcake.domain.run import Run
from devcake.domain import backend_health
from devcake.domain.orchestrator import dispatch, feed, sweeps


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

    def capabilities(self):
        from fakes import fake_pmo_capabilities
        return fake_pmo_capabilities()   # global_ids=True — peer path enabled

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
    run_coro(transitions.transition(mgr, run, {"outcome": "plan_needed"}, None))
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
    run_coro(transitions.transition(mgr, _run(), {"outcome": "human_needed",
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
    run_coro(transitions.transition(mgr, _run("ONBOARD", None),
                             {"outcome": "human_needed", "summary": "s"}, None))
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels
    assert fake.statuses == ["backlog"]           # avoids row-9 stranding


def test_human_needed_allowed_for_projects(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    m.pmo_kind = "project"
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(transitions.transition(mgr, run, {"outcome": "human_needed",
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
    mgr.forges.instance("main").auto_merge = False
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    run.finalized_steps = ["review:awaiting_merge"]            # checkpointed
    run_coro(transitions.transition(mgr, run, {"outcome": "reviewed", "verdict": "approve",
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
    run_coro(transitions.transition(mgr, _run(), {"outcome": "reviewed",
                                      "verdict": "approve"}, None))
    assert "DEVCAKE-SKIP" in m.labels
    assert "DEVCAKE-EXECUTE" in m.labels          # stage untouched, only parked
    assert any("not a legal outcome" in c for c in fake.comments)
    # and a PLAN run may only return "planned"
    m2 = mission("in_progress", {"DEVCAKE", "DEVCAKE-PLAN"})
    mgr2, fake2, _ = make_mgr(tmp_path, m2)
    run_coro(transitions.transition(mgr2, _run("PLAN", "DEVCAKE-PLAN"),
                              {"outcome": "decomposed", "decomposition": [{}]},
                              None))
    assert "DEVCAKE-SKIP" in m2.labels and fake2.created == []


def test_second_handoff_escalates_warning(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run_coro(transitions.transition(mgr, _run(), {"outcome": "human_needed", "summary": "s1"},
                             None))
    assert not any("Hand-off #" in c for c in fake.comments)   # first: no warning
    prior = _run()
    prior.run_id = "T-1-1-EXECUTE-PRIOR1"
    prior.state = "finished"
    prior.result = {"outcome": "human_needed"}
    store.save(prior)
    m.labels.discard("DEVCAKE-NEEDS-HUMAN")       # human resolved + resumed
    run_coro(transitions.transition(mgr, _run(), {"outcome": "human_needed", "summary": "s2"},
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
    assert dispatch.attempt_number(mgr, "p1", "ONBOARD") == 3

    auth = _run("ONBOARD", None)
    auth.run_id = "T-1-3-ONBOARD-AUTH"
    auth.state = "failed"
    auth.error = "DEV_FORGE_AUTH: denied"
    store.save(auth)
    assert dispatch.attempt_number(mgr, "p1", "ONBOARD") == 3

    success = _run("ONBOARD", None)
    success.run_id = "T-1-4-ONBOARD-OK"
    success.state = "finished"
    store.save(success)
    assert dispatch.attempt_number(mgr, "p1", "ONBOARD") == 1


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
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE") == 3

    failed_review = _run("REVIEW", None)
    failed_review.run_id = "T-1-3-REVIEW-FAIL"
    failed_review.state = "failed"
    failed_review.error = "DEV_BAD_OUTPUT"
    failed_review.created_at = t0 + timedelta(seconds=3)
    store.save(failed_review)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE") == 3   # failure ≠ resolution

    stray = _run("MAPPER", None)
    stray.run_id = "T-0-1-MAPPER-OK"
    stray.mission_pmo_id = ""
    stray.state = "finished"
    stray.created_at = t0 + timedelta(seconds=4)
    store.save(stray)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE") == 3   # other missions don't reset

    ok_review = _run("REVIEW", None)
    ok_review.run_id = "T-1-4-REVIEW-OK"
    ok_review.state = "finished"
    ok_review.created_at = t0 + timedelta(seconds=5)
    store.save(ok_review)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE") == 1


def _failed_execute_runs(store, t0, n=2):
    from datetime import timedelta
    for i in range(1, n + 1):
        r = _run("EXECUTE", None)
        r.run_id = f"T-1-{i}-EXECUTE-FAIL"
        r.state = "failed"
        r.error = "DEV_BAD_OUTPUT"
        r.created_at = t0 + timedelta(seconds=i)
        store.save(r)


def test_attempts_ignore_plain_comments_under_default_policy(tmp_path, monkeypatch):
    """ADR-0026 regression (critical evaluation 2026-08-04): under the strict
    default (`label-ops`) an ordinary comment — human or integration bot —
    does NOT reset the attempt count. The pre-0026 rule let any chatty
    integration (Linear↔GitHub sync, CI notifier) keep the counter at 1
    forever, defeating max_attempts and unbounding token spend."""
    from datetime import timedelta
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "no-audit.jsonl")
    assert mgr.config.attempt_reset == "label-ops"   # the shipped default
    t0 = datetime.now(timezone.utc)
    _failed_execute_runs(store, t0)

    bot = ActivityEntry(ts=t0 + timedelta(seconds=20), author="sync-bot",
                        kind="comment", body="Synced from GitHub · #4711")
    human = ActivityEntry(ts=t0 + timedelta(seconds=30), author="felix",
                          kind="comment", body="resolved this by hand, carry on")
    activity = Activity(mission=m, entries=[bot, human])
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", activity) == 3

    # the deliberate gesture DOES reset — and the sentinel guard still holds:
    # a DevCake-authored post mentioning the token is not an intervention
    devcake_echo = ActivityEntry(
        ts=t0 + timedelta(seconds=40), author="devcake",
        kind="comment", body="mention of DEVCAKE-RETRY\n\n`devcake:v1`")
    activity = Activity(mission=m, entries=[bot, human, devcake_echo])
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", activity) == 3

    retry = ActivityEntry(ts=t0 + timedelta(seconds=50), author="felix",
                          kind="comment", body="fixed the fixture — DEVCAKE-RETRY")
    activity = Activity(mission=m, entries=[bot, human, devcake_echo, retry])
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", activity) == 1


def test_attempts_reset_on_any_comment_when_opted_in(tmp_path, monkeypatch):
    """`attempt_reset: any-comment` restores the pre-0026 rule: a human
    comment is an intervention and grants fresh attempts. DevCake's own
    sentinel-signed comments never reset."""
    from datetime import timedelta
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("in_progress", {"DEVCAKE"})
    mgr, _fake, store = make_mgr(tmp_path, m)
    mgr.config.attempt_reset = "any-comment"
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "no-audit.jsonl")
    t0 = datetime.now(timezone.utc)
    _failed_execute_runs(store, t0)

    devcake_note = ActivityEntry(ts=t0 + timedelta(seconds=10), author="devcake",
                                 kind="comment", body="posted\n\n`devcake:v1`")
    old_human = ActivityEntry(ts=t0 - timedelta(minutes=5), author="felix",
                              kind="comment", body="please pick this up")
    activity = Activity(mission=m, entries=[devcake_note, old_human])
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", activity) == 3

    human = ActivityEntry(ts=t0 + timedelta(seconds=20), author="felix",
                          kind="comment", body="resolved this by hand, carry on")
    activity = Activity(mission=m, entries=[devcake_note, old_human, human])
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", activity) == 1


def test_unlimited_never_gives_up_and_warns_at_cadence(tmp_path, monkeypatch):
    """`attempt_reset: unlimited` (ADR-0026): the app never applies
    DEVCAKE-FAILED — the gate proceeds past max_attempts, posting a loud
    cumulative-cost warning every review_loop_warning_every failures, deduped
    across poll cycles."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    mgr.config.attempt_reset = "unlimited"
    mgr.config.max_attempts = 1
    mgr.config.review_loop_warning_every = 3
    dispatch._UNLIMITED_WARNED.clear()

    # attempt 4 = 3 failures → warning cadence hit; gate still proceeds
    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 4)) is True
    assert fake.swaps == []                       # DEVCAKE-FAILED never applied
    assert any("Unlimited-attempts mode" in c and "$" in c
               for c in fake.comments)
    warned = len(fake.comments)

    # same attempt recurring (a later gate deferred the dispatch) → no spam
    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 4)) is True
    assert len(fake.comments) == warned

    # off-cadence failure count → silent, still proceeds
    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 5)) is True
    assert len(fake.comments) == warned

    # the default policy still gives up: gate refuses and applies the label
    mgr.config.attempt_reset = "label-ops"
    assert run_coro(dispatch._attempt_gate(
        mgr, m, MissionType.EXECUTE, 4)) is False
    assert any("DEVCAKE-FAILED" in str(add) for _rm, add in fake.swaps)


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
    assert run.error_class == "DEV_FORGE"      # and not the exempt class
    assert "forge" not in mgr.breakers and not mgr.forges.breakers


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
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", None) == 2   # counted


def test_executed_trivially_is_illegal_and_parks(tmp_path):
    """Founder decision 2026-07-18 (rode ADR-0014's PR): ONBOARD never
    implements — trivial work rides the opportunistic-plan path to EXECUTE
    (the only stage holding a write token). `executed_trivially` is gone
    outright (preproduction, no deprecation window); a stray one parks."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(transitions.transition(mgr, _run("ONBOARD", None),
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
    run_coro(transitions.transition(mgr, _run("ONBOARD", None),
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
    assert not feed._is_devcake_comment(quoted)
    assert feed._is_devcake_comment(genuine)


def test_decomposition_wires_blocked_by_edges(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    drafts = [{"title": "docs", "priority": "high"},
              {"title": "code", "priority": "high", "blocked_by": [1]},
              {"title": "polish", "blocked_by": [1, 2]}]
    run_coro(transitions.transition(mgr, _run("ONBOARD", None),
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
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None), {"outcome": "decomposed", "decomposition": drafts}))
    children = fake.all_missions[1:]
    assert len(children) == 2
    assert all("`devcake-repo:beta`" in c.description for c in children)
    # replay with the marker present stays idempotent
    m.status = "in_progress"
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None), {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.created) == 2


def test_decomposition_children_clean_without_marker(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(decomposition.finalize_decomposition(mgr, 
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
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "a"},
                                                    {"title": "b"}]}))
    assert [pr for _, pr in fake.created] == ["proj-9", "proj-9"]

    solo = mission("in_progress", {"DEVCAKE"})
    mgr2, fake2, _ = make_mgr(tmp_path / "solo", solo)
    run_coro(decomposition.finalize_decomposition(mgr2, 
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "c"}]}))
    assert [pr for _, pr in fake2.created] == [None]

    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr3, fake3, _ = make_mgr(tmp_path / "proj", proj)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(decomposition.finalize_decomposition(mgr3, 
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
    run_coro(sweeps.tracking_sweep(mgr, proj))
    assert proj.status == "in_progress"           # grandchild holds it open
    grandchild.status = "done"
    run_coro(sweeps.tracking_sweep(mgr, proj))
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
    run_coro(decomposition.finalize_decomposition(mgr, 
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
    run_coro(decomposition.finalize_decomposition(mgr, 
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
        run_coro(decomposition.finalize_decomposition(mgr, 
            run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status != "canceled"                 # original still gates D
    assert ("status", "canceled") not in fake.ops

    fake.fail_relations = set()                   # transient error heals
    run_coro(decomposition.finalize_decomposition(mgr, 
        run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status == "canceled"
    assert ("id-1", "d1") in fake.relations and ("id-2", "d1") in fake.relations

    m2 = mission("in_progress", {"DEVCAKE"})      # sibling edges stay strict
    mgr2, fake2, _ = make_mgr(tmp_path / "sib", m2)
    fake2.fail_relations = {("id-1", "id-2")}
    with pytest.raises(RuntimeError):
        run_coro(decomposition.finalize_decomposition(mgr2, 
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
        run_coro(decomposition.finalize_decomposition(mgr, 
            run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status != "canceled"

    fake.all_missions.remove(doomed)              # human deleted the issue
    run_coro(decomposition.finalize_decomposition(mgr, 
        run, {"outcome": "decomposed", "decomposition": drafts}))
    assert m.status == "canceled"
    assert not any(blocked == "d1" for _, blocked in fake.relations)


def test_lineage_note_failure_never_blocks_cancel(tmp_path):
    """The note is hygiene, not safety: a failing append_description audits
    and finalization still cancels the original."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.fail_append = True
    run_coro(decomposition.finalize_decomposition(mgr, 
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
    run_coro(decomposition.finalize_decomposition(mgr, 
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
        run_coro(decomposition.finalize_decomposition(mgr, 
            run, {"outcome": "decomposed",
                  "decomposition": [{"title": "a"},
                                    {"title": "b", "blocked_by": [1]}]}))
    mgr.config.max_decomposition_depth = 1         # operator lowers the limit
    fake.fail_relations = set()
    run_coro(decomposition.finalize_decomposition(mgr, 
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
    run_coro(decomposition.finalize_decomposition(mgr, 
        first, {"outcome": "decomposed", "decomposition": drafts}))
    count = len(fake.relations)
    run_coro(decomposition.finalize_decomposition(mgr, 
        first, {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.relations) == count           # checkpoints all skipped

    m.status = "in_progress"
    run_coro(decomposition.finalize_decomposition(mgr, 
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
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None), {"outcome": "decomposed",
                                "decomposition": drafts}))
    assert "_Decomposed by DevCake into T-2, T-3_" in m.description
    note_at = fake.ops.index(("append_description",
                              "\n\n---\n_Decomposed by DevCake into T-2, T-3_"))
    assert note_at < fake.ops.index(("status", "canceled"))

    m.status = "in_progress"                      # fresh-run replay
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None), {"outcome": "decomposed",
                                "decomposition": drafts}))
    assert m.description.count("_Decomposed by DevCake into") == 1


def test_project_decomposition_appends_no_lineage_note(tmp_path):
    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(decomposition.finalize_decomposition(mgr, 
        run, {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert not any(op[0] == "append_description" for op in fake.ops)


def test_project_decomposition_inherits_nothing(tmp_path):
    proj = mission("in_progress", {"DEVCAKE"})
    proj.pmo_kind = "project"
    mgr, fake, _store = make_mgr(tmp_path, proj)
    fake.all_missions.append(dep_mission("d1", "T-90", blocked_by=["p1"]))
    run = _run("ONBOARD", None)
    run.pmo_kind = "project"
    run_coro(decomposition.finalize_decomposition(mgr, 
        run, {"outcome": "decomposed", "decomposition": [{"title": "a"}]}))
    assert not any(blocked == "d1" for _, blocked in fake.relations)


def test_decomposition_replay_reuses_marker_parts(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    drafts = [{"title": "docs"}, {"title": "code", "blocked_by": [1]}]
    first = _run("ONBOARD", None)
    run_coro(decomposition.finalize_decomposition(mgr, 
        first, {"outcome": "decomposed", "decomposition": drafts}))
    assert len(fake.created) == 2

    m.status = "in_progress"
    second = _run("ONBOARD", None)
    run_coro(decomposition.finalize_decomposition(mgr, 
        second, {"outcome": "decomposed", "decomposition": drafts}))

    assert len(fake.created) == 2
    assert fake.relations[-1] == ("id-1", "id-2")
    assert "DEVCAKE-NEEDS-HUMAN" not in m.labels


def test_decomposition_manifest_conflict_hands_off_without_writes(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    run_coro(decomposition.finalize_decomposition(mgr, 
        _run("ONBOARD", None),
        {"outcome": "decomposed", "decomposition": [{"title": "original"}]}))
    assert len(fake.created) == 1

    m.status = "in_progress"
    retry = _run("ONBOARD", None)
    run_coro(decomposition.finalize_decomposition(mgr, 
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
            run_coro(decomposition.finalize_decomposition(mgr, 
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


def test_finalize_stamps_rate_card_estimate(tmp_path):
    """ADR-0021: a grok-shaped report (full split, mapped model, no native
    cost) persists cost_usd_estimated + rate_card_id; an unmapped model
    persists neither; native cost_usd is never invented or touched."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    grok_report = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
                   "cache_write_tokens": None, "output_tokens": 500_000,
                   "total_tokens": 3_500_000, "cost_usd": None,
                   "model": "grok-4.5-build", "extraction_method": "end_event"}
    run_coro(mgr.finalize(run, _finalize_payload(token_report=grok_report)))
    saved = store.get(run.run_id).token_report
    assert saved["cost_usd_estimated"] == 5.60      # $2/$0.30/$6 per 1M
    assert saved["rate_card_id"] == "builtin-v1"
    assert saved["cost_usd"] is None


def test_finalize_leaves_unmapped_model_unstamped(tmp_path):
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    claude_report = {"input_tokens": 10_000, "cache_read_tokens": 5_000,
                     "cache_write_tokens": 2_000, "output_tokens": 1_000,
                     "total_tokens": 18_000, "cost_usd": 0.1234,
                     "model": "claude-opus-5",
                     "extraction_method": "session_json"}
    run_coro(mgr.finalize(run, _finalize_payload(token_report=claude_report)))
    saved = store.get(run.run_id).token_report
    assert "cost_usd_estimated" not in saved
    assert "rate_card_id" not in saved
    assert saved["cost_usd"] == 0.1234              # native untouched


def _grok_shaped_report(**over):
    base = {"input_tokens": 1_000_000, "cache_read_tokens": 2_000_000,
            "cache_write_tokens": None, "output_tokens": 500_000,
            "total_tokens": 3_500_000, "cost_usd": None,
            "model": "grok-4.5-build", "extraction_method": "end_event",
            "notes": "reasoning_tokens=20616"}
    base.update(over)
    return base


def test_feed_shows_estimated_cost_and_reasoning(tmp_path):
    """docs/03 §8 + ADR-0021: native cost absent + estimate stamped → the
    labeled estimated line appears (never the bare native line), and the
    reasoning counter surfaces from notes without being priced."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(
        token_report=_grok_shaped_report())))
    report = next(c for c in fake.comments if "token report" in c)
    assert "cost (estimated, builtin-v1): $5.6000" in report
    assert "\ncost: $" not in report
    assert "reasoning: 20616" in report


def test_feed_native_only_report_stays_byte_stable(tmp_path):
    """A pre-ADR-0021-shaped report (native cost, unmapped model, no notes
    counter) renders exactly the historical layout — no estimated line, no
    reasoning segment."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(token_report={
        "input_tokens": 10_000, "cache_read_tokens": 5_000,
        "cache_write_tokens": 2_000, "output_tokens": 1_000,
        "total_tokens": 18_000, "cost_usd": 0.1234,
        "model": "claude-opus-5", "extraction_method": "session_json"})))
    report = next(c for c in fake.comments if "token report" in c)
    assert "\ncost: $0.1234" in report
    assert "estimated" not in report
    assert "reasoning" not in report


def test_feed_override_native_shows_both_cost_lines(tmp_path):
    """override_native on + a mapped model WITH native cost → both lines
    appear (the honest form of 'operator rates override the display')."""
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    mgr.config.cost_inputs.override_native = True
    run = _saved_run(store)
    run_coro(mgr.finalize(run, _finalize_payload(
        token_report=_grok_shaped_report(cost_usd=4.4321))))
    report = next(c for c in fake.comments if "token report" in c)
    assert "\ncost: $4.4321" in report
    assert "cost (estimated, builtin-v1): $5.6000" in report


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
    assert feed._is_devcake_comment(comment)   # provenance holds


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
    assert dispatch._derive_seq(
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
    assert dispatch._derive_seq(
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
    run_coro(review.finalize_review(mgr, _run("REVIEW", "DEVCAKE-REVIEW"),
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
        run_coro(review.finalize_review(mgr, run, {"verdict": "reject",
                                            "report_md": report}))
    finally:
        unregister_runtime_secret(run.run_id)
    name, data = next(u for u in fake.uploads if u[0] == "1_REVIEW_REPORT.md")
    assert b"s3cret-relay-pw" not in data


# ── docs/03 §4.1 merge-failure branches ──────────────────────────────────────

def _approve_review(mgr):
    return review.finalize_review(mgr, _run("REVIEW", "DEVCAKE-REVIEW"),
                                {"verdict": "approve", "report_md": "ok"})


def merge_fail_mgr(tmp_path, mergeable_result):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge(merge_exc=ForgeError("PUT /pulls/8/merge → 405: conflicts",
                                           status=405),
                      mergeable_result=mergeable_result)
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.forges.instance("main").auto_merge = True
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
    mgr.forges.instance("main").auto_resolve_merge_conflicts = False
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
    mgr.forges.instance("main").merge_retry_window_minutes = 0
    run_coro(_approve_review(mgr))
    assert any("`devcake:merge-handoff`" in c for c in fake.comments)
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)


# ── docs/03 §4.1 deferred-merge sweep window ─────────────────────────────────

def sweep_mgr(tmp_path, mergeable_result, merge_exc=None):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    forge = FakeForge(merge_exc=merge_exc, mergeable_result=mergeable_result)
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.forges.instance("main").auto_merge = True
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body="⏳ deferred `devcake:merge-retry`\n\n`devcake:v1`")]
    return m, mgr, fake, forge


def test_sweep_merges_when_ready(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [8]
    assert "DEVCAKE-MERGE" not in m.labels and m.status == "done"
    assert any("Merged after deferred retry" in c for c in fake.comments)


def test_sweep_waits_while_computing(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [] and fake.comments == []
    assert "DEVCAKE-MERGE" in m.labels                # untouched, next cycle re-reads


def test_sweep_routes_conflict_to_execute(tmp_path):
    m, mgr, fake, forge = sweep_mgr(
        tmp_path, mergeable_result=False,
        merge_exc=ForgeError("405: conflicts", status=405))
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [8]                # merge tried before the rework
    assert "DEVCAKE-EXECUTE" in m.labels and "DEVCAKE-MERGE" not in m.labels
    assert any("`devcake:conflict-resolve:1`" in c for c in fake.comments)


def test_sweep_merges_behind_branch_without_rework(tmp_path):
    # "behind" reads as False but is only blocking under strict up-to-date
    # rules — when the plain merge succeeds, no EXECUTE rework is dispatched
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=False)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [8] and m.status == "done"
    assert "DEVCAKE-EXECUTE" not in m.labels
    assert not any("devcake:conflict-resolve" in c for c in fake.comments)


def test_sweep_window_expiry_hands_off_once(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    mgr.forges.instance("main").merge_retry_window_minutes = 0  # expires immediately
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == []                          # no merge attempt past expiry
    handoffs = [c for c in fake.comments if "`devcake:merge-handoff`" in c]
    assert len(handoffs) == 1 and "DEVCAKE-MERGE" in m.labels
    # the hand-off marker closes the window: later sweeps skip this mission
    fake.activity_entries.append(ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body=handoffs[0]))
    run_coro(sweeps.merge_sweep(mgr, m))
    assert len([c for c in fake.comments
                if "`devcake:merge-handoff`" in c]) == 1


def test_sweep_boolean_forge_conflict_hands_off_not_execute(tmp_path):
    """AUD-010: on a boolean-only forge (Gitea, `mergeable_tristate=False`) a
    failed merge with verdict False must NOT route to EXECUTE — a False can be
    'not computed yet' there, so it hands off, IDENTICAL to finalize. Without
    the capability check the sweep routed Gitea conflicts to rework while
    finalize handed them off — the doctrine split the audit found."""
    from types import SimpleNamespace
    m, mgr, fake, forge = sweep_mgr(
        tmp_path, mergeable_result=False,
        merge_exc=ForgeError("409: conflict", status=409))
    forge.capabilities = SimpleNamespace(mergeable_tristate=False)  # Gitea-like
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [8]                          # merge WAS tried first
    assert "DEVCAKE-EXECUTE" not in m.labels            # never routed to rework
    assert not any("conflict-resolve" in c for c in fake.comments)
    assert "DEVCAKE-MERGE" in m.labels                  # stays parked; retries


def test_rearm_retained_when_parked_missions_pr_is_missing(tmp_path):
    """AUD-005: a repo's OFF→ON re-arm must survive a cycle where the parked
    mission's PR can't be found (forge lag) — otherwise the flag is cleared
    unconditionally and the window is lost until another toggle."""
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)

    async def _no_pr(_branch):
        return None
    forge.get_pr_by_branch = _no_pr          # PR not visible this cycle
    mgr.rearm_merge_repos = {"main"}
    run_coro(mgr.sweeps([m]))
    assert mgr.rearm_merge_repos == {"main"}  # retained — not lost
    assert m.pmo_id in mgr.blocked_reasons    # AUD-006: missing PR is VISIBLE
    # once the PR appears, the rearm fires and the flag clears (one-shot)
    async def _pr(_branch):
        return PullRequest(number=8, url="https://forge/pr/8", state="open")
    forge.get_pr_by_branch = _pr
    fake.activity_entries = []
    run_coro(mgr.sweeps([m]))
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert mgr.rearm_merge_repos == set()


def _no_pr_forge(tmp_path, auto_merge, window=30):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FakeForge()

    async def _none(_branch):
        return None
    forge.get_pr_by_branch = _none           # forge list lag at finalize
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    mgr.forges.instance("main").auto_merge = auto_merge
    mgr.forges.instance("main").merge_retry_window_minutes = window
    return m, mgr, fake


def test_finalize_missing_pr_auto_merge_opens_deferred_window(tmp_path):
    """AUD-006: auto_merge ON but no PR at REVIEW finalize (forge lag) must
    open a deferred window (retry marker) — NOT pure human-await, which would
    strand app-driven merge forever (the sweep silent-returns on a missing
    PR and opens no window without a marker)."""
    m, mgr, fake = _no_pr_forge(tmp_path, auto_merge=True)
    run_coro(review.finalize_review(
        mgr, _run("REVIEW", "DEVCAKE-REVIEW"),
        {"verdict": "approve", "report_md": "ok", "pr_url": "https://forge/pr/8"}))
    assert "DEVCAKE-MERGE" in m.labels
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert not any("Awaiting human merge" in c for c in fake.comments)


def test_finalize_missing_pr_manual_repo_keeps_human_await(tmp_path):
    """Contrast: auto_merge OFF + no PR → the honest human-await copy, no
    retry marker (the app was never going to merge it)."""
    m, mgr, fake = _no_pr_forge(tmp_path, auto_merge=False)
    run_coro(review.finalize_review(
        mgr, _run("REVIEW", "DEVCAKE-REVIEW"),
        {"verdict": "approve", "report_md": "ok", "pr_url": "https://forge/pr/8"}))
    assert "DEVCAKE-MERGE" in m.labels
    assert any("Awaiting human merge" in c for c in fake.comments)
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)


def test_sweep_ignores_missions_without_retry_marker(tmp_path):
    # auto_merge-OFF parks carry no marker — the sweep must not touch them
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    run_coro(sweeps.merge_sweep(mgr, m))
    assert forge.merges == [] and fake.comments == []
    assert "DEVCAKE-MERGE" in m.labels


# ── docs/11 awaiting-human-merge advisory + sweep skip-set ───────────────────

def test_manual_park_banners_without_feed_read(tmp_path):
    # auto_merge OFF: the banner entry derives from labels alone — zero
    # get_activity calls for the operator's normal merge queue
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    mgr.forges.instance("main").auto_merge = False
    run_coro(sweeps.merge_sweep(mgr, m))
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]
    assert getattr(fake, "get_activity_calls", 0) == 0


def test_active_retry_window_suppresses_banner(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=None)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert m.pmo_id not in mgr.merge_handoffs        # DevCake is still driving
    assert m.pmo_id not in mgr._merge_window_closed


def test_expired_window_banners_and_skips_future_feed_reads(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    mgr.forges.instance("main").merge_retry_window_minutes = 0  # expires immediately
    run_coro(sweeps.merge_sweep(mgr, m))
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]
    assert m.pmo_id in mgr._merge_window_closed
    calls = fake.get_activity_calls
    run_coro(sweeps.merge_sweep(mgr, m))                    # second cycle: skip-set hit
    assert fake.get_activity_calls == calls          # no new feed read
    assert "awaiting human merge" in mgr.merge_handoffs[m.pmo_id]


def test_closed_window_park_banners_without_repeat_reads(tmp_path):
    # no retry marker at all (e.g. restart onto an old park): one feed read
    # discovers the closed window, then the skip-set takes over
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    run_coro(sweeps.merge_sweep(mgr, m))
    run_coro(sweeps.merge_sweep(mgr, m))
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
    run_coro(transitions.transition(mgr, run, {"outcome": "reviewed", "verdict": "approve"},
                             None))
    assert run.verdict and run.verdict.startswith("rejected:")
    assert "illegal for EXECUTE" in run.verdict


def test_external_transition_sets_verdict(tmp_path):
    m = mission(labels={"DEVCAKE", "DEVCAKE-PLAN"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-XXXXXX", mission_key="T-1",
              mission_pmo_id="p1", mission_type="ONBOARD",
              dev_type="senior-dev", seq=1, stage_label_at_dispatch=None)
    run_coro(transitions.transition(mgr, run, {"outcome": "plan_needed"}, None))
    assert run.verdict and run.verdict.startswith("skipped:")


def test_human_needed_sets_verdict_and_advisory(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _run()
    run_coro(transitions.transition(mgr, run, {"outcome": "human_needed", "summary": "s"},
                             None))
    assert run.verdict == "handed off: needs human on EXECUTE"
    assert "p1" in mgr.needs_human
    assert mgr.needs_human["p1"].startswith("T-1: needs human")


# ── auto-merge OFF→ON re-arm (founder request 2026-07-15, per-repo ADR-0020) ─
# A mission parked at DEVCAKE-MERGE while auto_merge was OFF carries no retry
# marker, so flipping auto_merge ON used to leave it awaiting a human forever
# (the sweep closed the window on first read and the skip-set cached it).
# The config PUT adds the flipped repo name to a one-shot re-arm set: the next
# sweep posts a fresh retry-window entry (visible, marker-timestamped) for
# parked missions on that repo; the cycle after that reads the marker and
# drives the merge inside the normal merge_retry_window_minutes bound.

def test_rearm_reopens_parked_mission_when_auto_merge_flips_on(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []            # the auto_merge-OFF park: no marker
    mgr.rearm_merge_repos = {"main"}     # what put_config sets on OFF→ON
    run_coro(mgr.sweeps([m]))             # cycle 1: posts the window entry
    rearm_comments = [c for c in fake.comments if "`devcake:merge-retry`" in c]
    assert len(rearm_comments) == 1
    assert m.pmo_id not in mgr._merge_window_closed
    assert mgr.rearm_merge_repos == set()             # one-shot
    assert forge.merges == []                         # merge happens NEXT cycle
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body=rearm_comments[0])]
    run_coro(mgr.sweeps([m]))             # cycle 2: fresh marker → merge
    assert forge.merges == [8]
    assert m.status == "done"


def test_apply_auto_merge_rearm_populates_set_off_to_on(tmp_path):
    """AUD-024: the shared re-arm (config PUT AND bundle/profile apply both
    call apply_auto_merge_rearm — settings_bundle.py, config_service.py)
    unions OFF→ON repos into every manager's rearm set."""
    from devcake.config import RepoInstance, apply_auto_merge_rearm
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    prev = [RepoInstance(name="main", url="https://github.com/o/r",
                         auto_merge=False)]
    new = [RepoInstance(name="main", url="https://github.com/o/r",
                        auto_merge=True)]
    flipped = apply_auto_merge_rearm(prev, new, {"linear": mgr})
    assert flipped == {"main"}
    assert "main" in mgr.rearm_merge_repos


def test_rearm_reaches_missions_already_in_skip_set(tmp_path):
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    mgr._merge_window_closed = {"p1"}     # cached closed from prior cycles
    mgr.rearm_merge_repos = {"main"}
    run_coro(mgr.sweeps([m]))
    assert any("`devcake:merge-retry`" in c for c in fake.comments)
    assert "p1" not in mgr._merge_window_closed


def test_rearm_noop_when_window_zero(tmp_path):
    # window 0 = "hand off immediately" — flipping auto_merge ON must not
    # open a window the operator has configured away
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    mgr.forges.instance("main").merge_retry_window_minutes = 0
    mgr.rearm_merge_repos = {"main"}
    run_coro(mgr.sweeps([m]))
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)
    assert forge.merges == []


# ── ADR-0020: per-repo merge doctrine ────────────────────────────────────────

def test_per_repo_auto_merge_only_merges_that_repos_missions(tmp_path):
    """Two repos: A auto_merge ON merges on REVIEW approve; B OFF parks."""
    from devcake.config import RepoInstance
    from fakes import make_mission_manager

    forge_a, forge_b = FakeForge(), FakeForge()
    inst_a = RepoInstance(name="alpha", url="https://github.com/o/a",
                          auto_merge=True)
    inst_b = RepoInstance(name="beta", url="https://github.com/o/b",
                          auto_merge=False)

    class MultiRuntime:
        def __init__(self):
            self._map = {"alpha": (forge_a, inst_a), "beta": (forge_b, inst_b)}
            self.health, self.breakers, self.internal = {}, {}, set()

        def get(self, name):
            return self._map[name][0] if name in self._map else None

        def instance(self, name):
            return self._map[name][1] if name in self._map else None

        @property
        def forges(self):
            return {k: v[0] for k, v in self._map.items()}

        @property
        def instances(self):
            return {k: v[1] for k, v in self._map.items()}

    rt = MultiRuntime()
    ma = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    ma.repo = "alpha"
    mb = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mb.pmo_id, mb.key, mb.repo = "p2", "T-2", "beta"
    cfg = AppConfig()
    mgr_a = make_mission_manager(
        tmp_path, pmo=FakePMO(ma), forge_runtime=rt, config=cfg,
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(), noop_audit=True)
    mgr_b = make_mission_manager(
        tmp_path, pmo=FakePMO(mb), forge_runtime=rt, config=cfg,
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(), noop_audit=True)
    run_a = _run("REVIEW", "DEVCAKE-REVIEW")
    run_a.repo_ref = "alpha"
    run_b = Run(run_id="T-2-1-REVIEW-BBBBBB", mission_key="T-2",
                mission_pmo_id="p2", mission_type="REVIEW",
                dev_type="senior-dev", seq=1,
                stage_label_at_dispatch="DEVCAKE-REVIEW", repo_ref="beta")
    run_coro(review.finalize_review(mgr_a, run_a,
                                    {"verdict": "approve", "report_md": "ok"}))
    run_coro(review.finalize_review(mgr_b, run_b,
                                    {"verdict": "approve", "report_md": "ok"}))
    assert forge_a.merges == [8]
    assert ma.status == "done" and "DEVCAKE-MERGE" not in ma.labels
    assert forge_b.merges == []
    assert "DEVCAKE-MERGE" in mb.labels and mb.status == "in_progress"


def test_conflict_auto_resolve_honors_mission_repo_flag(tmp_path):
    """Mission on alpha (resolve OFF) parks even when beta would resolve ON."""
    from devcake.config import RepoInstance
    from fakes import make_mission_manager

    forge_a = FakeForge(
        merge_exc=ForgeError("405: conflicts", status=405),
        mergeable_result=False)
    inst_a = RepoInstance(name="alpha", url="https://github.com/o/a",
                          auto_merge=True,
                          auto_resolve_merge_conflicts=False)
    inst_b = RepoInstance(name="beta", url="https://github.com/o/b",
                          auto_merge=True,
                          auto_resolve_merge_conflicts=True)

    class MultiRuntime:
        def __init__(self):
            self._map = {"alpha": (forge_a, inst_a), "beta": (None, inst_b)}
            self.health, self.breakers, self.internal = {}, {}, set()

        def get(self, name):
            pair = self._map.get(name)
            return pair[0] if pair else None

        def instance(self, name):
            pair = self._map.get(name)
            return pair[1] if pair else None

        @property
        def forges(self):
            return {k: v[0] for k, v in self._map.items() if v[0] is not None}

        @property
        def instances(self):
            return {k: v[1] for k, v in self._map.items()}

    ma = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    ma.repo = "alpha"
    mgr = make_mission_manager(
        tmp_path, pmo=FakePMO(ma), forge_runtime=MultiRuntime(),
        config=AppConfig(),
        dev_types={"senior-dev": DevType(name="senior-dev",
                                         harness_template="claude-code")},
        messaging=NullMessaging(), noop_audit=True)
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    run.repo_ref = "alpha"
    run_coro(review.finalize_review(mgr, run,
                                    {"verdict": "approve", "report_md": "ok"}))
    assert "DEVCAKE-MERGE" in ma.labels and "DEVCAKE-EXECUTE" not in ma.labels
    assert not any("devcake:conflict-resolve" in c for c in mgr.pmo.comments)
    # beta's ON flag must not have been consulted — alpha parked


def test_rearm_only_targets_flipped_repo(tmp_path):
    """rearm_merge_repos={alpha} must not reopen a mission parked on beta."""
    m, mgr, fake, forge = sweep_mgr(tmp_path, mergeable_result=True)
    fake.activity_entries = []
    m.repo = "main"
    mgr.rearm_merge_repos = {"other"}   # flipped a different repo
    run_coro(mgr.sweeps([m]))
    assert not any("`devcake:merge-retry`" in c for c in fake.comments)
    assert forge.merges == []
    # now re-arm the mission's own repo
    mgr.rearm_merge_repos = {"main"}
    run_coro(mgr.sweeps([m]))
    assert any("`devcake:merge-retry`" in c for c in fake.comments)


def test_config_put_rearm_is_per_repo(tmp_path, monkeypatch):
    """apply_config_patch only re-arms repos that flipped OFF→ON."""
    from devcake.api import config_service
    from devcake.config import RepoInstance

    cfg = AppConfig(repos=[
        RepoInstance(name="alpha", url="https://github.com/o/a",
                     auto_merge=False),
        RepoInstance(name="beta", url="https://github.com/o/b",
                     auto_merge=False),
    ])
    mgr = make_mgr(tmp_path, mission())[0]
    managers = {"linear": mgr}
    monkeypatch.setattr(config_service, "save_config", lambda c: None)
    monkeypatch.setattr(
        "devcake.api.config_service.secrets_store.delete_connection_instance",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "devcake.api.config_service.validate_config_semantics",
        lambda *a, **k: None)
    monkeypatch.setattr(
        "devcake.api.config_service.dry_run_adapters", lambda *a, **k: None)

    def _reload():
        pass

    body = {
        "repos": [
            {"name": "alpha", "forge": "github",
             "url": "https://github.com/o/a", "auto_merge": True,
             "auto_resolve_merge_conflicts": True,
             "merge_retry_window_minutes": 30, "default_branch": "main",
             "api_base": None},
            {"name": "beta", "forge": "github",
             "url": "https://github.com/o/b", "auto_merge": False,
             "auto_resolve_merge_conflicts": True,
             "merge_retry_window_minutes": 30, "default_branch": "main",
             "api_base": None},
        ],
    }
    run_coro(config_service.apply_config_patch(
        body, config=cfg, dev_types={}, managers=managers, reload=_reload))
    assert mgr.rearm_merge_repos == {"alpha"}


def test_executed_feed_uses_descriptor_pr_noun(tmp_path):
    """Audit A29 heir: the executed-path feed uses the forge's own noun (the
    old trivial-twin pin died with the removed outcome — re-pin here)."""
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    forge = FakeForge()
    forge.descriptor = type("D", (), {"pr_noun": "merge request"})()
    mgr, fake, _store = make_mgr(tmp_path, m, forge=forge)
    run_coro(transitions.transition(mgr, _run("EXECUTE", "DEVCAKE-EXECUTE"),
                             {"outcome": "executed",
                              "pr_url": "https://forge/mr/1", "summary": "s"},
                             None))
    assert any("merge request" in c for c in fake.comments)
    assert not any("pull request" in c for c in fake.comments)


# ── ADR-0018: structured error classes and correlated-failure accounting ─────

def _save_failed(store, run_id, *, error_class, mission="p1", mtype="EXECUTE",
                 dev_type="senior-dev", counted=True, state="failed"):
    r = Run(run_id=run_id, mission_key="T-1", mission_pmo_id=mission,
            mission_type=mtype, dev_type=dev_type, seq=1, state=state,
            error_class=error_class, attempt_counted=counted,
            error=f"{error_class}: boom")
    store.save(r)
    return r


@pytest.mark.parametrize("payload,expected_class", [
    ({"exit_code": 10}, "DEV_CRASH"),
    ({"exit_code": 20}, "DEV_CRASH"),
    ({"exit_code": 11}, "DEV_BAD_OUTPUT"),
    ({"exit_code": 13}, "DEV_FORGE"),
    ({"exit_code": 14}, "DEV_MCP_SETUP"),
    ({"exit_code": 16}, "DEV_TURN_BUDGET"),
])
def test_every_exit_code_stamps_a_structured_class(tmp_path, payload, expected_class):
    """Stamping only the NEW codes would leave the others at error_class == ""
    in the legacy branch, where DEV_FORGE matches nothing and keeps counting —
    silently not delivering the uncounted-DEV_FORGE decision."""
    mgr, _fake, _store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, payload)
    assert run.error_class == expected_class


def test_exit_15_stamps_harness_fault_and_trips_no_breaker(tmp_path):
    mgr, _fake, _store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    error = mgr.dev_failure_error(run, {
        "exit_code": 15, "error_class": "DEV_HARNESS_FAULT",
        "error_detail": "claude returned no assistant output at all"})
    assert error.startswith("DEV_HARNESS_FAULT")
    assert run.error_class == "DEV_HARNESS_FAULT"
    # NOT a circuit breaker: it throttles and self-heals, so it must never
    # latch the dev-type or per-repo maps
    assert not mgr.breakers and not mgr.forges.breakers


def test_turn_budget_always_counts_even_while_degraded(tmp_path):
    """Turn exhaustion is deterministic — retrying the same cap cannot help, so
    it is never excused and never contributes correlation evidence."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    _save_failed(store, "T-1-1-EXECUTE-OLD1", error_class="DEV_HARNESS_FAULT",
                 mission="p2")
    _save_failed(store, "T-1-1-EXECUTE-OLD2", error_class="DEV_HARNESS_FAULT",
                 mission="p3")
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 16, "error_class": "DEV_TURN_BUDGET"})
    assert run.error_class == "DEV_TURN_BUDGET"
    assert run.attempt_counted is True


def test_solitary_backend_fault_counts(tmp_path):
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    _save_failed(store, "T-1-1-EXECUTE-OLD1", error_class="DEV_HARNESS_FAULT")
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 15,
                                "error_class": "DEV_HARNESS_FAULT"})
    assert run.attempt_counted is True


def test_solitary_backend_fault_counts_with_excusals_spent(tmp_path):
    """The missing truth-table row: solitary (backend_correlated None) AND the
    step's excusals spent. Both terms of the accounting `or` are true here, so
    the failure counts — the exhausted budget must never flip a solitary
    failure back to excused (that inversion is what an `and` would produce)."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-EXECUTE-EX{i}", error_class="DEV_HARNESS_FAULT",
                     counted=False)
    run = _run("EXECUTE", None)
    assert backend_health.backend_correlated(store.all(), "senior-dev") is None
    assert backend_health.excusals_left(store.all(), run) is False
    mgr.dev_failure_error(run, {"exit_code": 15,
                                "error_class": "DEV_HARNESS_FAULT"})
    assert run.attempt_counted is True


def test_correlated_backend_fault_does_not_count(tmp_path):
    """Evidence across ≥2 distinct missions means the backend, not the mission."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_HARNESS_FAULT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_HARNESS_FAULT",
                 mission="p3")
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 15,
                                "error_class": "DEV_HARNESS_FAULT"})
    assert run.attempt_counted is False


def test_correlated_fault_counts_again_once_excusals_are_spent(tmp_path):
    """THE escape hatch. The evidence IS the faults, so without a per-step cap
    an armed detector excuses every later fault forever — a permanently bad
    model id would retry with no give-up at all."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_HARNESS_FAULT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_HARNESS_FAULT",
                 mission="p3")
    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-EXECUTE-EX{i}", error_class="DEV_HARNESS_FAULT",
                     counted=False)
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 15,
                                "error_class": "DEV_HARNESS_FAULT"})
    assert run.attempt_counted is True, "exhausted excusals must start counting"


def test_orphan_payload_without_a_structured_class_is_never_excused(tmp_path):
    """reconcile can recover the numeric code from Dagu's node error but never
    error_class, so an orphan earns the label and contributes evidence — it is
    just never itself excused. The skew-safe direction."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_HARNESS_FAULT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_HARNESS_FAULT",
                 mission="p3")
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 15})       # numeric only
    assert run.error_class == "DEV_HARNESS_FAULT"
    assert run.attempt_counted is True


def test_bad_output_counts_even_when_correlated_by_default(tmp_path):
    """ADR-0026 default: brake_on_bad_output off keeps the ADR-0018 design —
    a fleet-wide exit-11 cascade still burns attempts (2026-07-24 behavior)."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    assert mgr.config.brake_on_bad_output is False    # the shipped default
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_BAD_OUTPUT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_BAD_OUTPUT",
                 mission="p3")
    run = _run("EXECUTE", None)
    error = mgr.dev_failure_error(run, {"exit_code": 11})
    assert error.startswith("DEV_BAD_OUTPUT")
    assert run.error_class == "DEV_BAD_OUTPUT"
    assert run.attempt_counted is True


def test_bad_output_correlated_is_excused_when_opted_in(tmp_path):
    """brake_on_bad_output on: exit-11 evidence across ≥2 missions excuses the
    attempt exactly like exit 15 — no container-class precondition, because
    exit 11 has no in-band structured class (the code IS the classification)."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    mgr.config.brake_on_bad_output = True
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_BAD_OUTPUT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_BAD_OUTPUT",
                 mission="p3")
    run = _run("EXECUTE", None)
    error = mgr.dev_failure_error(run, {"exit_code": 11})
    assert run.attempt_counted is False
    assert "does not count toward attempts" in error


def test_bad_output_solitary_counts_when_opted_in(tmp_path):
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    mgr.config.brake_on_bad_output = True
    _save_failed(store, "T-1-1-EXECUTE-OLD", error_class="DEV_BAD_OUTPUT")
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 11})
    assert run.attempt_counted is True


def test_bad_output_excusals_run_out_when_opted_in(tmp_path):
    """The same escape hatch as exit 15, on the DEV_BAD_OUTPUT ledger."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    mgr.config.brake_on_bad_output = True
    _save_failed(store, "T-2-1-EXECUTE-A", error_class="DEV_BAD_OUTPUT",
                 mission="p2")
    _save_failed(store, "T-3-1-EXECUTE-B", error_class="DEV_BAD_OUTPUT",
                 mission="p3")
    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-EXECUTE-EX{i}",
                     error_class="DEV_BAD_OUTPUT", counted=False)
    run = _run("EXECUTE", None)
    mgr.dev_failure_error(run, {"exit_code": 11})
    assert run.attempt_counted is True, "exhausted excusals must start counting"


def test_dev_forge_no_longer_counts_toward_attempts(tmp_path):
    """The old marker tuple carried a dead "dev failure artifact (exit 13)"
    entry that never matched, so plain forge failures burned attempts in
    contradiction of docs/15 §2."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    run.state = "failed"
    run.error = mgr.dev_failure_error(run, {"exit_code": 13,
                                            "error_detail": "could not resolve host"})
    store.save(run)
    assert run.error_class == "DEV_FORGE"
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", None) == 1


def test_dev_forge_is_never_unconditionally_uncounted():
    """Deviation guard. Adding DEV_FORGE to UNCOUNTED_CLASSES makes the excusal
    cap in dev_failure_error DEAD CODE — `counts_toward_attempts` short-circuits
    on the class before it ever reads `attempt_counted`. Plain exit 13 latches no
    breaker, so the mission would be re-dispatched every poll interval forever on
    a bad branch name, a DNS failure or a 500: the unbounded livelock that
    `excusals_left` exists to bound. Uncounted-but-bounded is the shipped rule."""
    assert "DEV_FORGE" not in dispatch.UNCOUNTED_CLASSES, (
        "DEV_FORGE in UNCOUNTED_CLASSES makes dev_failure_error's excusal cap "
        "dead code (counts_toward_attempts never reads attempt_counted for an "
        "uncounted CLASS) and restores an unbounded livelock: plain exit 13 "
        "latches no breaker, so the step re-dispatches every poll interval "
        "forever with no give-up")


def test_marker_only_auth_wording_is_bounded_dev_forge(tmp_path):
    """App-side mirror of the container's incidental-403 conservatism. Auth
    WORDING without the structured class (a push rate limit, or any pre-taxonomy
    image that sends no error_class at all) used to be stamped DEV_FORGE_AUTH:
    unconditionally uncounted via UNCOUNTED_CLASSES, with the breaker latch
    reserved for the structured arm — i.e. uncounted, breaker-less, retried
    FOREVER. It now takes the excusal-bounded DEV_FORGE path."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    error = mgr.dev_failure_error(run, {
        "exit_code": 13,
        "error_detail": "remote: HTTP 403 rate limit exceeded — retry the push"})
    assert run.error_class == "DEV_FORGE"
    assert error.startswith("DEV_FORGE:")          # legacy prefix match holds
    assert "403" in error                          # the evidence is still visible
    assert not mgr.forges.breakers and not mgr.breakers
    assert run.attempt_counted is False            # excused while bounded…

    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-EXECUTE-FG{i}", error_class="DEV_FORGE",
                     counted=False)
    again = _run("EXECUTE", None)
    mgr.dev_failure_error(again, {"exit_code": 13,
                                  "error_detail": "fatal: Authentication failed"})
    assert again.error_class == "DEV_FORGE"
    assert again.attempt_counted is True           # …and the bound is real
    assert not mgr.forges.breakers


def test_excusal_budgets_do_not_bleed_between_classes(tmp_path):
    """One spent budget must not silently spend the other: they are separate
    per-(mission, mission_type, class) allowances, so a forge outage cannot eat
    the backend-fault escape hatch (or the reverse)."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-EXECUTE-HF{i}", error_class="DEV_HARNESS_FAULT",
                     counted=False)
    run = _run("EXECUTE", None)
    runs = store.all()
    assert backend_health.excusals_left(runs, run) is False           # spent
    assert backend_health.excusals_left(
        runs, run, error_class="DEV_FORGE") is True                   # untouched
    mgr.dev_failure_error(run, {"exit_code": 13,
                                "error_detail": "could not resolve host"})
    assert run.attempt_counted is False    # exit 13 still has its own allowance

    # …and the mirror, on a step whose DEV_FORGE budget is the spent one
    for i in range(backend_health.MAX_EXCUSALS_PER_STEP):
        _save_failed(store, f"T-1-1-ONBOARD-FG{i}", error_class="DEV_FORGE",
                     mtype="ONBOARD", counted=False)
    onboard = _run("ONBOARD", None)
    runs = store.all()
    assert backend_health.excusals_left(
        runs, onboard, error_class="DEV_FORGE") is False
    assert backend_health.excusals_left(runs, onboard) is True


def test_attempt_matching_is_exact_not_substring(tmp_path):
    """Injection regression: decomposition.py raises with the Dev's blocked_by
    list VERBATIM and finalize wraps it into run.error, so a Dev emitting
    blocked_by: ["DEV_AUTH"] must not make its own failures stop counting."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    run.state = "failed"
    run.error_class = "DEV_BAD_OUTPUT"
    run.error = ("DEV_BAD_OUTPUT: decomposition part 1: blocked_by must be "
                 "1-based indexes of EARLIER parts, got ['DEV_AUTH']")
    store.save(run)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", None) == 2   # counted


def test_legacy_records_without_a_class_still_honour_uncounted_prefixes(tmp_path):
    """Pre-upgrade records have error_class == "" and fall back to a PREFIX
    match — injected text can only ever land in the tail."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))
    run = _run("EXECUTE", None)
    run.state = "failed"
    run.error_class = ""
    run.error = "DEV_AUTH (does not count toward attempts; breaker tripped)"
    store.save(run)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", None) == 1

    other = _run("EXECUTE", None)
    other.run_id = "T-1-2-EXECUTE-BBBBBB"
    other.state = "failed"
    other.error_class = ""
    other.error = "DEV_BAD_OUTPUT: the Dev mentioned DEV_AUTH in its output"
    store.save(other)
    assert dispatch.attempt_number(mgr, "p1", "EXECUTE", None) == 2   # tail ignored


# ── ADR-0018 §8: the kill chokepoint classifies, callers never enumerate ─────

def _killable(store, run_id):
    run = _run("EXECUTE", None)
    run.run_id = run_id
    run.state = "running"
    store.save(run)
    return run


@pytest.mark.parametrize("new_state,expected", [
    ("timed_out", "DEV_TIMEOUT"),
    ("orphaned", "DEV_ORPHANED"),
    ("failed", "DEV_KILLED"),
    ("evicted", "DEV_KILLED"),      # a state nobody remembers to add to the map
])
def test_kill_stamps_the_class_for_its_target_state(tmp_path, new_state, expected):
    """Two drafts of the plan tried to ENUMERATE the seven kill sites and both
    were wrong, so `_kill_inner` classifies from a state-keyed map with a
    DEV_KILLED catch-all: no kill path can leave a run unclassified and fall
    back to `attempt_number`'s legacy error-prefix matching."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))

    async def _no_ship(*a, **k):        # keep the OO shipping side effect out
        pass
    mgr.runs._ship_failure = _no_ship   # type: ignore[method-assign]
    run = _killable(store, f"T-1-1-EXECUTE-K{new_state[:5].upper()}")
    run_coro(mgr.runs.kill(run, new_state, "watchdog: no heartbeat"))
    # asserted on the record kill mutated: the catch-all case deliberately uses
    # a state outside RunState, which the store's re-read would reject
    assert run.state == new_state
    assert run.error_class == expected


def test_explicit_error_class_wins_over_the_state_default(tmp_path):
    """The operator stops (clear-runs, stop-run, stop-all) pass their own class:
    a deliberate stop must not read as DEV_KILLED in the taxonomy."""
    mgr, _fake, store = make_mgr(tmp_path, mission("in_progress", {"DEVCAKE"}))

    async def _no_ship(*a, **k):
        pass
    mgr.runs._ship_failure = _no_ship   # type: ignore[method-assign]
    run = _killable(store, "T-1-1-EXECUTE-OPSTOP")
    run_coro(mgr.runs.kill(run, "failed", "stopped by operator (stop all)",
                           error_class="DEV_OPERATOR_STOP"))
    assert store.get(run.run_id).error_class == "DEV_OPERATOR_STOP"

