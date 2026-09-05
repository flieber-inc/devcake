"""The in-flight registry behind the admin status bar (docs/11 §0).

Public seams under test:
- devcake.activity.InFlight (phase / start / finish / snapshot)
- devcake.api.activity.build_activity_payload
- PollRuntime.note_skip + the per-instance skip bookkeeping
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from devcake.activity import OVERDUE_FACTOR, RECENT, InFlight


class Clock:
    def __init__(self, t=1_700_000_000.0):
        self.t = t

    def __call__(self):
        return self.t

    def tick(self, s):
        self.t += s


def _registry():
    clock = Clock()
    return InFlight(clock=clock, mono=clock), clock


def test_a_phase_appears_between_start_and_finish_only():
    reg, clock = _registry()
    assert reg.snapshot()["items"] == []
    assert reg.snapshot()["idle_since"] == "2023-11-14T22:13:20+00:00"
    with reg.phase("poll.cycle", "cycle 7", expect_s=75) as p:
        clock.tick(12)
        snap = reg.snapshot()
        assert snap["idle_since"] is None
        assert snap["items"] == [{
            "kind": "poll.cycle", "subject": "cycle 7",
            "started_at": "2023-11-14T22:13:20+00:00", "elapsed_s": 12.0,
            "expect_s": 75, "overdue": False, "detail": {}}]
        p.set(done=3, total=9)
        assert reg.snapshot()["items"][0]["detail"] == {"done": 3, "total": 9}
    snap = reg.snapshot()
    assert snap["items"] == []
    assert snap["idle_since"] == "2023-11-14T22:13:32+00:00"
    assert snap["recent"][0] == {"kind": "poll.cycle", "subject": "cycle 7",
                                 "ended_at": "2023-11-14T22:13:32+00:00",
                                 "elapsed_s": 12.0, "error": None}


def test_an_exception_finishes_the_phase_and_names_it():
    reg, clock = _registry()
    with pytest.raises(RuntimeError):
        with reg.phase("mission.dispatch", "T-1 EXECUTE"):
            clock.tick(1)
            raise RuntimeError("boom")
    assert reg.snapshot()["items"] == []
    assert reg.snapshot()["recent"][0]["error"] == "RuntimeError"


def test_overdue_is_elapsed_past_the_expected_bound_times_the_factor():
    reg, clock = _registry()
    with reg.phase("poll.instance", "board", expect_s=10):
        clock.tick(10 * OVERDUE_FACTOR)
        assert reg.snapshot()["items"][0]["overdue"] is False
        clock.tick(0.5)
        assert reg.snapshot()["items"][0]["overdue"] is True
    with reg.phase("forge.sweep", "3 repositories"):       # no bound
        clock.tick(10_000)
        assert reg.snapshot()["items"][0]["overdue"] is False


def test_concurrent_phases_are_ordered_by_start_and_recent_is_bounded():
    reg, clock = _registry()
    a = reg.start("mirror.sync", "3 mirrors")
    clock.tick(1)
    b = reg.start("mission.dispatch", "T-2 PLAN")
    assert [i["kind"] for i in reg.snapshot()["items"]] == ["mirror.sync",
                                                             "mission.dispatch"]
    reg.finish(b)
    reg.finish(a)
    reg.finish(a)                                  # idempotent
    for i in range(RECENT + 5):
        reg.finish(reg.start("poll.cycle", f"cycle {i}"))
    recent = reg.snapshot()["recent"]
    assert len(recent) == RECENT
    assert recent[0]["subject"] == f"cycle {RECENT + 4}"      # newest first


def test_snapshot_is_json_safe_even_with_odd_detail_values():
    reg, _ = _registry()
    with reg.phase("pmo.budget.wait", "tracker/u1", reason=object(),
                   wait_s=12.5, instance=None):
        text = json.dumps(reg.snapshot())
    assert "wait_s" in text and "12.5" in text


def test_activity_payload_merges_the_poll_runtimes_skips():
    from devcake.api.activity import build_activity_payload
    reg, _ = _registry()
    rt = SimpleNamespace(poll_skips={"board": {"at": "t", "reason": "budget",
                                               "retry_after_s": 40.0}},
                         last_poll_at=None)
    out = build_activity_payload(in_flight=reg, poll_rt=rt)
    assert out["poll_skips"] == rt.poll_skips
    assert out["last_poll_at"] is None and out["items"] == []
    assert build_activity_payload(in_flight=reg)["poll_skips"] == {}


def test_poll_runtime_records_and_clears_transient_skips():
    from devcake.api.poll import PollRuntime
    rt = PollRuntime.__new__(PollRuntime)
    rt.poll_skips = {}
    rt.note_skip("board", "request budget: reserved for critical calls", 40.4)
    row = rt.poll_skips["board"]
    assert row["reason"].startswith("request budget") and row["retry_after_s"] == 40.4
    assert row["at"].endswith("+00:00")
    rt.note_skip("board", "PMO 502", None)
    assert rt.poll_skips["board"]["retry_after_s"] is None
    rt.poll_skips.pop("board", None)                   # what a green segment does
    assert rt.poll_skips == {}
