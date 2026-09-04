"""A resume is a human's answer — the Dev sees it (docs/03 §4a).

Public seams under test:
- orchestrator.resume.resumed_after_handoff (run history → hand-off info)
- orchestrator.resume.banner_lines (the ACTIVITY.md banner)
- MissionManager.activity_payload (banner precedes the mirror)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from devcake.domain.model import Activity, ActivityEntry
from devcake.domain.orchestrator import resume
from devcake.domain.run import Run

T0 = datetime(2026, 1, 10, 12, 0, tzinfo=timezone.utc)


def _run(rid, *, mission_type="EXECUTE", state="finished", outcome=None,
         summary="", created=T0, ended=None, pmo_id="h"):
    r = Run(run_id=rid, mission_key="T-H", mission_pmo_id=pmo_id,
            mission_type=mission_type, dev_type="senior-dev", seq=1,
            state=state, created_at=created, ended_at=ended)
    if outcome is not None:
        r.result = {"outcome": outcome, "summary": summary}
    return r


def _mgr(*runs):
    return SimpleNamespace(runs=SimpleNamespace(store=SimpleNamespace(
        all=lambda: list(runs))), _run_is_ours=lambda r: True)


def _entry(body, ts, *, author="alice"):
    return ActivityEntry(ts=ts, author=author, kind="comment", body=body,
                         entry_id=body[:24])


def test_no_runs_or_no_handoff_is_not_a_resume():
    assert resume.resumed_after_handoff(_mgr(), "h") is None
    executed = _run("L-T-H-1-EXECUTE-AAAAAA", outcome="executed",
                    summary="done", ended=T0 + timedelta(hours=1))
    assert resume.resumed_after_handoff(_mgr(executed), "h") is None


def test_latest_finished_handoff_is_reported_with_its_ask():
    ask = "Need your approval for the outline before I write files."
    handoff = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary=ask,
                   ended=T0 + timedelta(minutes=30))
    info = resume.resumed_after_handoff(_mgr(handoff), "h")
    assert info == {"mission_type": "PLAN", "at": T0 + timedelta(minutes=30),
                    "ask": ask}


def test_only_the_most_recent_finished_run_counts():
    handoff = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary="approve?",
                   ended=T0 + timedelta(minutes=30))
    later_ok = _run("L-T-H-2-PLAN-BBBBBB", mission_type="PLAN",
                    outcome="executed", summary="planned",
                    created=T0 + timedelta(hours=2),
                    ended=T0 + timedelta(hours=3))
    assert resume.resumed_after_handoff(_mgr(handoff, later_ok), "h") is None
    # the other way round: a hand-off after an executed run IS a resume
    assert resume.resumed_after_handoff(
        _mgr(later_ok, _run("L-T-H-3-EXECUTE-CCCCCC", outcome="human_needed",
                            summary="need creds",
                            created=T0 + timedelta(hours=4),
                            ended=T0 + timedelta(hours=5))), "h"
    )["ask"] == "need creds"


def test_in_flight_steward_and_foreign_runs_are_ignored():
    handoff = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary="approve?",
                   ended=T0 + timedelta(minutes=30))
    # the mid-run refresh: the resumed run itself is still running
    running = _run("L-T-H-2-PLAN-BBBBBB", mission_type="PLAN",
                   state="running", created=T0 + timedelta(hours=2))
    # a newer steward run and another mission's run never count
    steward = _run("SYS-STEWARD-9-ZZZZZZ", mission_type="STEWARD",
                   outcome="stewarded", summary="s",
                   created=T0 + timedelta(hours=3),
                   ended=T0 + timedelta(hours=3))
    other = _run("L-T-X-1-EXECUTE-DDDDDD", outcome="executed", summary="x",
                 created=T0 + timedelta(hours=4),
                 ended=T0 + timedelta(hours=4), pmo_id="x")
    info = resume.resumed_after_handoff(
        _mgr(handoff, running, steward, other), "h")
    assert info is not None and info["ask"] == "approve?"
    mine = _mgr(handoff)
    mine._run_is_ours = lambda r: False
    assert resume.resumed_after_handoff(mine, "h") is None


def test_ask_is_bounded():
    handoff = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary="x" * 900, ended=T0)
    ask = resume.resumed_after_handoff(_mgr(handoff), "h")["ask"]
    assert len(ask) == resume.ASK_MAX + 1 and ask.endswith("…")


def test_banner_counts_human_comments_after_the_handoff():
    info = {"mission_type": "PLAN", "at": T0, "ask": "approve the outline?"}
    before = _entry("earlier chat", T0 - timedelta(hours=1))
    after = _entry("Plan approved, please move forward.",
                   T0 + timedelta(minutes=5))
    lines = resume.banner_lines(info, [before, after])
    assert lines[0].startswith("▶ RESUMED BY A HUMAN — your previous PLAN run")
    assert "2026-01-10 12:00 UTC" in lines[0]
    assert lines[1] == "> approve the outline?"
    assert any("Human comments since the hand-off: 1" in ln for ln in lines)
    assert not any("release itself is the answer" in ln for ln in lines)
    assert lines[-1] == ""                              # separates the mirror


def test_banner_without_a_comment_says_the_release_is_the_answer():
    info = {"mission_type": "EXECUTE", "at": T0, "ask": ""}
    before = _entry("earlier chat", T0 - timedelta(hours=1))
    lines = resume.banner_lines(info, [before])
    assert lines[1] == "> (no summary was recorded)"
    assert any("release itself is the answer" in ln for ln in lines)
    assert any("Hand off again only if the obstacle demonstrably persists"
               in ln for ln in lines)
    assert not any("Human comments since" in ln for ln in lines)


def test_activity_payload_opens_with_the_banner_after_a_handoff(tmp_path):
    from test_steward import MapPMO, m, make_mgr, run_coro
    mission = m("h", "T-H", status="in_progress")
    act = Activity(mission=mission, entries=[
        _entry("brief discussion", T0 - timedelta(days=1)),
        _entry("Plan approved, please move forward.",
               T0 + timedelta(minutes=5)),
    ], truncated=False)
    mgr = make_mgr(tmp_path, MapPMO([mission], activity=act))
    handoff = _run("L-T-H-1-PLAN-AAAAAA", mission_type="PLAN",
                   outcome="human_needed", summary="approve the outline?",
                   ended=T0)
    handoff.pmo_ref = mgr.instance_name
    mgr.runs.store.save(handoff)
    text = _activity_md(run_coro(mgr.activity_payload("h")))
    assert text.startswith("▶ RESUMED BY A HUMAN — your previous PLAN run")
    assert "Human comments since the hand-off: 1" in text
    assert "Plan approved, please move forward." in text
    # no hand-off → no banner
    mgr.runs.store.save(_run("L-T-H-2-PLAN-BBBBBB", mission_type="PLAN",
                             outcome="executed", summary="planned",
                             created=T0 + timedelta(hours=1),
                             ended=T0 + timedelta(hours=2)))
    text = _activity_md(run_coro(mgr.activity_payload("h")))
    assert "RESUMED BY A HUMAN" not in text


def _activity_md(payload: dict) -> str:
    return payload["activity_md"]
