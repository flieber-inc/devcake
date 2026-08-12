"""The grace-cycle skip END-TO-END (2026-08-12 audit test-gap): after DevCake
writes to a mission's PMO feed, the NEXT poll cycle must not re-dispatch that
mission on the feedback loop it just created (docs/04 §2). The wiring is
feed._audit → mgr._grace_next → poll.rotate_grace → schedule skips _grace.
The only prior touch of _grace_next poked a SimpleNamespace attribute; here
the real chain runs, so deleting the schedule skip (or the rotate) turns the
suite red."""

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


def test_grace_is_not_triggered_without_a_write(tmp_path, monkeypatch):
    """The skip must be precise: a mission we did NOT write to dispatches on
    the very first cycle."""
    mgr, fake, m, dispatched = _backlog_mgr(tmp_path, monkeypatch)
    mgr.rotate_grace()                        # empty rotate, no prior audit
    run_coro(schedule.schedule(mgr, [m], gate={}))
    assert dispatched == [m.pmo_id]
