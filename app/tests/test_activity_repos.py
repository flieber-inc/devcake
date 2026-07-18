"""ADR-0014 D4: per-mission activity repos — the app-written, Dev-read-only
record of what each step's Dev actually received. Name rule, dispatch
pre-push hook (never gates), runspec carriage."""

import asyncio

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
    # the sweeper discriminator: operator card names (^[a-z][a-z0-9]{0,11}$)
    # can never start with the hyphen-bearing prefix — even one literally
    # named "activity"
    assert not "activity".startswith(ACTIVITY_PREFIX)
    assert long.startswith(ACTIVITY_PREFIX)


# ── dispatch pre-push hook (slice 3.8) ───────────────────────────────────────

from devcake.domain.model import MissionType
from fakes import FakeInternalForge


def _dispatch_setup(tmp_path, forge_fake):
    from test_transitions import make_mgr, mission
    from test_prompt_templates import _ForgeWithDescriptor
    from devcake.config import PMOInstance

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
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
