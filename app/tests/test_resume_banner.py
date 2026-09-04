"""A resume is a human's answer — the Dev sees it (docs/03 §4a).

Public seams under test:
- orchestrator.resume.resumed_after_handoff (run history → hand-off info)
- orchestrator.resume.seen_until (the comment boundary = the watermark)
- orchestrator.resume.banner_lines (the ACTIVITY.md banner)
- activity_payload.activity_payload (banner + MISSION.md pointer; never on
  an ancestor mirror)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from devcake.domain.model import Activity, ActivityEntry
from devcake.domain.orchestrator import activity_payload, resume
from devcake.domain.run import Run

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)
HANDOFF = "handed off: needs human on EXECUTE"


def _run(rid, *, mission_type="EXECUTE", state="finished", outcome=None,
         summary="", verdict="", created=T0, ended=None, pmo_id="h",
         watermark=None):
    r = Run(run_id=rid, mission_key="T-H", mission_pmo_id=pmo_id,
            mission_type=mission_type, dev_type="senior-dev", seq=1,
            state=state, created_at=created, ended_at=ended, verdict=verdict,
            feed_watermark=dict(watermark or {}))
    if outcome is not None:
        r.result = {"outcome": outcome, "summary": summary}
    return r


def _handoff(rid="L-T-H-1-EXECUTE-AAAAAA", *, summary="approve the outline?",
             created=T0, ended=None, watermark=None, **kw):
    return _run(rid, outcome="human_needed", summary=summary, verdict=HANDOFF,
                created=created, ended=ended or created + timedelta(hours=1),
                watermark=watermark, **kw)


def _mgr(*runs):
    return SimpleNamespace(runs=SimpleNamespace(store=SimpleNamespace(
        all=lambda: list(runs))), _run_is_ours=lambda r: True)


def _entry(body, ts, *, author="alice", kind="comment"):
    return ActivityEntry(ts=ts, author=author, kind=kind, body=body,
                         entry_id=body[:24])


def test_no_runs_or_no_handoff_is_not_a_resume():
    assert resume.resumed_after_handoff(_mgr(), "h") is None
    executed = _run("L-T-H-1-EXECUTE-AAAAAA", outcome="executed",
                    summary="done", ended=T0 + timedelta(hours=1))
    assert resume.resumed_after_handoff(_mgr(executed), "h") is None


def test_a_genuine_handoff_is_reported_with_its_ask_and_boundaries():
    wm = {"entry_id": "e9", "ts": (T0 - timedelta(minutes=3)).isoformat()}
    info = resume.resumed_after_handoff(
        _mgr(_handoff(mission_type="REVIEW", watermark=wm)), "h")
    assert info == {"mission_type": "REVIEW",
                    "at": T0 - timedelta(minutes=3),       # what it had read
                    "ended": T0 + timedelta(hours=1),
                    "ask": "approve the outline?"}
    # no watermark (empty feed at dispatch / legacy) → the dispatch time
    assert resume.resumed_after_handoff(_mgr(_handoff()), "h")["at"] == T0
    bad = _handoff(watermark={"ts": "not a date"})
    assert resume.seen_until(bad) == T0


def test_human_needed_without_a_hold_never_banners():
    """An external stage change halts a run with outcome human_needed and a
    `skipped:` verdict; an illegal outcome parks it with `rejected:`. No
    person removed a hold, so no banner (the playbook tells the Dev to
    proceed on it)."""
    halted = _run("L-T-H-1-EXECUTE-AAAAAA", outcome="human_needed",
                  summary="x", ended=T0,
                  verdict="skipped: mission state changed externally")
    assert resume.resumed_after_handoff(_mgr(halted), "h") is None
    illegal = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary="x", ended=T0,
                   verdict="rejected: PLAN may not return human_needed")
    assert resume.resumed_after_handoff(_mgr(illegal), "h") is None
    no_verdict = _run("L-T-H-1-EXECUTE-AAAAAA", outcome="human_needed",
                      summary="x", ended=T0)
    assert resume.resumed_after_handoff(_mgr(no_verdict), "h") is None


def test_only_the_most_recent_finished_run_counts():
    handoff = _handoff()
    later_ok = _run("L-T-H-2-EXECUTE-BBBBBB", outcome="executed",
                    summary="done", created=T0 + timedelta(hours=2),
                    ended=T0 + timedelta(hours=3))
    assert resume.resumed_after_handoff(_mgr(handoff, later_ok), "h") is None
    # a hand-off after an executed run IS a resume
    again = _handoff("L-T-H-3-EXECUTE-CCCCCC", summary="need creds",
                     created=T0 + timedelta(hours=4))
    assert resume.resumed_after_handoff(
        _mgr(later_ok, again), "h")["ask"] == "need creds"


def test_a_failed_retry_after_the_release_keeps_the_banner():
    """DEV_BAD_OUTPUT keeps the result on a `failed` run; only `finished`
    runs decide, so the retry still learns it was released."""
    handoff = _handoff()
    failed = _run("L-T-H-2-EXECUTE-BBBBBB", state="failed",
                  outcome="executed", summary="garbled",
                  created=T0 + timedelta(hours=2),
                  ended=T0 + timedelta(hours=2, minutes=5))
    info = resume.resumed_after_handoff(_mgr(handoff, failed), "h")
    assert info is not None and info["ask"] == "approve the outline?"


def test_in_flight_steward_and_foreign_runs_are_ignored():
    handoff = _handoff()
    running = _run("L-T-H-2-EXECUTE-BBBBBB", state="running",
                   created=T0 + timedelta(hours=2))      # the mid-run refresh
    steward = _run("SYS-STEWARD-9-ZZZZZZ", mission_type="STEWARD",
                   outcome="stewarded", summary="s",
                   created=T0 + timedelta(hours=3),
                   ended=T0 + timedelta(hours=3))
    other = _run("L-T-X-1-EXECUTE-DDDDDD", outcome="executed", summary="x",
                 created=T0 + timedelta(hours=4),
                 ended=T0 + timedelta(hours=4), pmo_id="x")
    info = resume.resumed_after_handoff(
        _mgr(handoff, running, steward, other), "h")
    assert info is not None and info["ask"] == "approve the outline?"
    mine = _mgr(handoff)
    mine._run_is_ours = lambda r: False
    assert resume.resumed_after_handoff(mine, "h") is None


def test_ask_is_bounded():
    ask = resume.resumed_after_handoff(
        _mgr(_handoff(summary="x" * 900)), "h")["ask"]
    assert len(ask) == resume.ASK_MAX + 1 and ask.endswith("…")


def _info(at=T0, ended=None, ask="approve the outline?", mission_type="EXECUTE"):
    return {"mission_type": mission_type, "at": at,
            "ended": ended or at + timedelta(hours=1), "ask": ask}


def test_banner_counts_human_comments_the_run_never_saw():
    before = _entry("earlier chat", T0 - timedelta(hours=1))
    during = _entry("No — do X instead.", T0 + timedelta(minutes=10))
    after = _entry("Plan approved, please move forward.",
                   T0 + timedelta(hours=2))
    status = _entry("moved to In Progress", T0 + timedelta(hours=3),
                    kind="status_change")
    lines = resume.banner_lines(_info(), [before, during, after, status])
    assert lines[0].startswith(
        "▶ RESUMED BY A HUMAN — your previous EXECUTE run")
    assert "2026-01-10 13:00 UTC" in lines[0]            # when it ended
    assert lines[1] == "> approve the outline?"
    assert any("Human comments that run never saw: 2" in ln for ln in lines)
    assert not any("release itself is the answer" in ln for ln in lines)
    assert lines[-1] == ""                              # separates the mirror


def test_banner_prints_the_end_time_in_utc():
    local = datetime(2026, 1, 10, 10, 0,
                     tzinfo=timezone(timedelta(hours=-3)))
    lines = resume.banner_lines(_info(ended=local), [])
    assert "2026-01-10 13:00 UTC" in lines[0]


def test_banner_without_a_comment_says_the_release_is_the_answer():
    before = _entry("earlier chat", T0 - timedelta(hours=1))
    lines = resume.banner_lines(_info(ask=""), [before])
    assert lines[1] == "> (no summary was recorded)"
    assert any("release itself is the answer" in ln for ln in lines)
    assert any("Re-read MISSION.md" in ln for ln in lines)
    assert any("Hand off again only if the obstacle demonstrably persists"
               in ln for ln in lines)
    assert not any("Human comments that run never saw" in ln for ln in lines)


def _board(tmp_path, entries):
    from test_steward import MapPMO, m, make_mgr
    mission = m("h", "T-H", status="in_progress")
    act = Activity(mission=mission, entries=entries, truncated=False)
    return make_mgr(tmp_path, MapPMO([mission], activity=act))


def test_activity_payload_opens_with_the_banner_after_a_handoff(tmp_path):
    from test_steward import run_coro
    mgr = _board(tmp_path, [
        _entry("brief discussion", T0 - timedelta(days=1)),
        _entry("Plan approved, please move forward.",
               T0 + timedelta(minutes=5)),
    ])
    handoff = _handoff()
    handoff.pmo_ref = mgr.instance_name
    mgr.runs.store.save(handoff)
    payload = run_coro(mgr.activity_payload("h"))
    text = payload["activity_md"]
    assert text.startswith("▶ RESUMED BY A HUMAN — your previous EXECUTE run")
    assert "Human comments that run never saw: 1" in text
    assert "Plan approved, please move forward." in text
    assert payload["mission_md"].startswith(resume.MISSION_NOTE)
    assert "# T-H: T-H" in payload["mission_md"]
    # an ancestor mirror (the nested rebuild) never claims a release
    nested = run_coro(activity_payload.activity_payload(
        mgr, "h", include_upstream=False))
    assert "RESUMED BY A HUMAN" not in nested["activity_md"]
    assert "RESUMED BY A HUMAN" not in nested["mission_md"]
    # a later executed run → no banner, no pointer
    mgr.runs.store.save(_run("L-T-H-2-EXECUTE-BBBBBB", outcome="executed",
                             summary="done", created=T0 + timedelta(hours=2),
                             ended=T0 + timedelta(hours=3)))
    payload = run_coro(mgr.activity_payload("h"))
    assert "RESUMED BY A HUMAN" not in payload["activity_md"]
    assert "RESUMED BY A HUMAN" not in payload["mission_md"]
