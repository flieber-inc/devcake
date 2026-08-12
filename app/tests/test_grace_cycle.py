"""The grace-cycle skip at the schedule seam (docs/04 §2).

This is a poll-wiring unit, not a PMO-staleness e2e: FakePMO, faked
dispatch, and an explicit rotate. The load-bearing rotate *site* is
`poll.py` (`mgr.rotate_grace()` after schedule); `test_poll_rotates_grace`
pins that call so deleting it turns red. The skip predicate itself is
`schedule.py`'s `_grace` check, pinned below."""

import asyncio

from devcake.domain.orchestrator import schedule
from devcake.domain.run import Run

from test_transitions import make_mgr, mission


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _backlog_mgr(tmp_path, monkeypatch):
    """A schedulable backlog mission on a resolved repo, real _audit wired to
    an isolated audit log (make_mgr's noop_audit would bypass _grace_next)."""
    import devcake.domain.orchestrator as orchestrator_mod

    m = mission("backlog", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    # ONBOARD (backlog derives to it) must staff the ONE dev type make_mgr
    # provides, or the assignment gate — not grace — is what skips
    from devcake.config import Assignment
    mgr.config.assignments = {mt: Assignment(dev_type="senior-dev")
                              for mt in ("ONBOARD", "PLAN", "EXECUTE", "REVIEW")}
    # restore the REAL _audit (make_mgr no-ops it) so it feeds _grace_next,
    # and point its append at a tmp file
    monkeypatch.setattr(orchestrator_mod.markers, "AUDIT_PATH",
                        tmp_path / "events.jsonl")
    del mgr._audit          # drop the noop override → the class method returns
    dispatched = []

    async def fake_dispatch(mission_, mtype, dev_type):
        dispatched.append(mission_.pmo_id)
        return None

    mgr.dispatch = fake_dispatch
    # a resolved repo so the per-mission repo gate doesn't pre-empt the test
    m.repo = "main"
    return mgr, fake, m, dispatched


def test_own_write_defers_dispatch_one_cycle(tmp_path, monkeypatch):
    mgr, fake, m, dispatched = _backlog_mgr(tmp_path, monkeypatch)

    # cycle 1: a normal audit (as any feed write does) marks the mission for
    # the grace skip, THEN the cycle ends and grace rotates
    mgr._audit(m.pmo_id, "label_swap", "DEVCAKE→DEVCAKE-PLAN")
    assert m.pmo_id in mgr._grace_next
    mgr.rotate_grace()                       # poll.py does this per segment
    assert m.pmo_id in mgr._grace

    # cycle 2: schedule must SKIP the mission we just wrote to — deleting the
    # `if m.pmo_id in mgr._grace: continue` line makes this dispatch (red)
    run_coro(schedule.schedule(mgr, [m], gate={}))
    assert dispatched == [], "a mission written to last cycle must not dispatch"

    # cycle 3: grace has rotated empty, so it dispatches normally now
    mgr.rotate_grace()
    run_coro(schedule.schedule(mgr, [m], gate={}))
    assert dispatched == [m.pmo_id], "the skip is ONE cycle, not permanent"


def test_poll_rotates_grace_after_schedule():
    """The rotate must live in the poll segment, not only in this file."""
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "devcake" / "api"
           / "poll.py").read_text()
    assert "rotate_grace" in src
    sched = src.find("schedule")
    rot = src.find("rotate_grace")
    assert sched != -1 and rot != -1 and rot > sched, (
        "poll.py must rotate grace AFTER schedule so a write this cycle "
        "is skipped next cycle")


def test_grace_is_not_triggered_without_a_write(tmp_path, monkeypatch):
    """The skip must be precise: a mission we did NOT write to dispatches on
    the very first cycle."""
    mgr, fake, m, dispatched = _backlog_mgr(tmp_path, monkeypatch)
    mgr.rotate_grace()                        # empty rotate, no prior audit
    run_coro(schedule.schedule(mgr, [m], gate={}))
    assert dispatched == [m.pmo_id]
