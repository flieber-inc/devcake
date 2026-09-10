"""ADR-0014 D4: per-mission activity repos — the app-written, Dev-read-only
record of what each step's Dev actually received. Name rule, dispatch
pre-push hook (never gates), runspec carriage."""

import asyncio
import base64

from devcake.ports.internal_forge import (ACTIVITY_PREFIX, activity_repo_name,
                                          internal_repo_name)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def test_activity_repo_name_prefix_and_cap():
    assert activity_repo_name("linear", "DEV-17") == "activity-linear-dev-17"
    # prefix applied AFTER the 60-char sanitize/cap: ≤69 total, and no
    # re-truncation (re-truncating could collide two long mission keys)
    long = activity_repo_name("linear", "X" * 200)
    assert long == ACTIVITY_PREFIX + internal_repo_name("linear", "X" * 200)
    assert len(long) <= 69
    # the sweeper discriminator: operator card names (INSTANCE_NAME_BODY)
    # can never start with the hyphen-bearing prefix — even one literally
    # named "activity"
    assert not "activity".startswith(ACTIVITY_PREFIX)
    assert long.startswith(ACTIVITY_PREFIX)


def test_internal_repo_name_preserves_underscore_in_instance():
    """CAKE-151: INSTANCE_NAME_BODY allows '_'; the compound scrubber must
    keep it so {instance}-{key} stays unambiguous ('_' ≠ '-' separator)."""
    assert internal_repo_name("acme_eng", "DEV-1") == "acme_eng-dev-1"
    assert internal_repo_name("acme", "ENG-DEV-1") == "acme-eng-dev-1"
    assert internal_repo_name("acme_eng", "DEV-1") != internal_repo_name(
        "acme", "ENG-DEV-1")
    # activity wrapper + first-hyphen-after-prefix parse (same idiom as
    # adapters/gitea/provision.py::list_activity_repos) recovers the key
    name = activity_repo_name("acme_eng", "DEV-1")
    assert name == "activity-acme_eng-dev-1"
    stem = name[len(ACTIVITY_PREFIX):]
    assert stem.split("-", 1)[-1] == "dev-1"


# ── dispatch pre-push hook (slice 3.8) ───────────────────────────────────────

from devcake.domain.model import MissionType
from fakes import FakeInternalForge


def _dispatch_setup(tmp_path, forge_fake, m=None):
    from test_transitions import make_mgr, mission
    from test_prompt_templates import _ForgeWithDescriptor
    from devcake.config import PMOInstance

    m = m if m is not None else mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _store = make_mgr(tmp_path, m, forge=_ForgeWithDescriptor())
    mgr.internal_forge = forge_fake
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    launched = []

    async def launch(run, image):
        launched.append(run)
    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    return mgr, fake, m, launched


def test_dispatch_pushes_activity_snapshot_before_launch(tmp_path):
    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert forge.ensured == [("linear", "T-1")]
    repo, files, message = forge.pushes[0]
    assert repo == "activity-linear-t-1"
    assert message == "step 1 EXECUTE dispatch"
    paths = {f["path"] for f in files}
    assert "ACTIVITY.md" in paths and "MISSION.md" in paths


def test_activity_push_failure_never_gates_dispatch(tmp_path):
    forge = FakeInternalForge(push_exc=RuntimeError("gitea down"))
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    audits = []
    mgr._audit = lambda pmo_id, action, detail="": audits.append(action)
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched            # ADR-0014: NEVER gates
    assert "activity_repo_push_failed" in audits


def test_dispatch_without_internal_forge_skips_push(tmp_path):
    mgr, fake, m, launched = _dispatch_setup(tmp_path, None)
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched


def test_runspec_carries_activity_repo_ro(tmp_path):
    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    payload = mgr.runspec_secret_payload(run)
    assert payload["activity_repo"] == {
        "url": "http://gitea:3000/devcake-repos/activity-linear-t-1.git",
        "clone_user": "devcake-activity-ro", "token": "act-ro-tok"}
    forge.ro_token = ""                        # boot mint absent → no key
    assert "activity_repo" not in mgr.runspec_secret_payload(run)
    mgr.internal_forge = None                  # forge disabled → no key
    assert "activity_repo" not in mgr.runspec_secret_payload(run)


def test_runspec_no_activity_repo_for_steward(tmp_path):
    from devcake.domain.run import Run
    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge)
    steward = Run(run_id="LINEAR-DEV-1-STEWARD-AAAAAA", mission_key="DEV",
                 mission_type="STEWARD", dev_type="senior-dev", seq=1,
                 repo_ref="main", state="dispatched")
    payload = mgr.runspec_secret_payload(steward)
    assert payload is not None
    assert "activity_repo" not in payload


def test_dispatch_pushes_for_project_missions(tmp_path):
    # ADR-0014: EVERY mission gets a repo — projects included (their payload
    # is MISSION.md = the brief + the no-feed ACTIVITY.md stub)
    from datetime import datetime, timezone
    from devcake.domain.model import Mission
    proj = Mission(instance="linear", pmo_id="p9", pmo_kind="project",
                   key="P-1", title="proj", status="backlog",
                   labels={"DEVCAKE"}, description="the brief",
                   updated_at=datetime.now(timezone.utc), repo="main")
    forge = FakeInternalForge()
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge, m=proj)
    run = run_coro(mgr.dispatch(proj, MissionType.ONBOARD,
                                mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert forge.ensured == [("linear", "P-1")]
    _, files, message = forge.pushes[0]
    assert message == "step 1 ONBOARD dispatch"
    assert {f["path"] for f in files} == {"MISSION.md", "ACTIVITY.md"}


# ── ADR-0043 §1: the record follows every run boundary ───────────────────

def _finalize_setup(tmp_path, forge):
    from test_transitions import make_mgr, mission
    from devcake.config import PMOInstance
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.record_feed = True                 # posts become feed entries
    mgr.internal_forge = forge
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    return mgr, fake, store


def _run(store, **over):
    from devcake.domain.run import Run
    kw = dict(run_id="T-1-1-ONBOARD-ZZZZZZ", mission_key="T-1",
              mission_pmo_id="p1", mission_type="ONBOARD",
              dev_type="senior-dev", seq=1, stage_label_at_dispatch=None,
              state="finalizing")
    kw.update(over)
    run = Run(**kw)
    store.save(run)
    return run


def test_finalize_pushes_the_record_and_keeps_upstream(tmp_path):
    """After the step's card lands, the mission's repo is refreshed from the
    feed; the dispatch-time upstream/ subtree is left in place."""
    from test_transitions import _finalize_payload
    forge = FakeInternalForge()
    forge.seed_snapshot("activity-linear-t-1", {
        "MISSION.md": b"stale brief", "ACTIVITY.md": b"stale feed",
        "upstream/ROOT-1/MISSION.md": b"root brief",
        "old-attachment.md": b"gone from the feed"})
    mgr, fake, store = _finalize_setup(tmp_path, forge)
    run = _run(store)
    run_coro(mgr.finalize(run, _finalize_payload()))
    repo, files, message = forge.pushes[-1]
    assert repo == "activity-linear-t-1" and message == "record: finalize"
    snap = forge.snapshots["activity-linear-t-1"]
    assert "upstream/ROOT-1/MISSION.md" in snap          # kept, not pruned
    assert "old-attachment.md" not in snap               # stale = pruned
    activity = base64.b64decode(snap["ACTIVITY.md"]).decode()
    assert "Step 1" in activity or "ONBOARD" in activity  # the card is in
    assert not any(f["path"].startswith("upstream/") for f in files)


def test_failed_run_pushes_the_record_too(tmp_path):
    forge = FakeInternalForge()
    mgr, fake, store = _finalize_setup(tmp_path, forge)
    run = _run(store)
    run_coro(mgr.finalize(run, {"result": None, "exit_code": 15,
                                "transcript_md": ""}))
    assert forge.pushes and forge.pushes[-1][2] == "record: failed"


def test_record_push_failure_never_raises(tmp_path):
    from test_transitions import _finalize_payload
    forge = FakeInternalForge(push_exc=RuntimeError("gitea down"))
    mgr, fake, store = _finalize_setup(tmp_path, forge)
    run = _run(store)
    run_coro(mgr.finalize(run, _finalize_payload()))   # no raise
    assert store.get(run.run_id).state == "finished"


def test_record_skips_without_internal_forge(tmp_path):
    from test_transitions import _finalize_payload
    mgr, fake, store = _finalize_setup(tmp_path, None)
    run = _run(store)
    run_coro(mgr.finalize(run, _finalize_payload()))
    assert store.get(run.run_id).state == "finished"
