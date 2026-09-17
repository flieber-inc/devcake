"""Delivery destination (ADR-0017 addendum): the marker grammar, the
description-append chokepoint, and the dispatch snapshot."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from devcake.domain.model import MissionRef, MissionType
from devcake.domain.orchestrator import feed
from devcake.domain.orchestrator.markers import (
    DELIVERY_MARKER_RE, DELIVERY_REPOSITORY, DELIVERY_TICKET, HANDOFF_MARKER,
    delivery_marker, delivery_of, delivery_recorded)


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


# ── grammar ──────────────────────────────────────────────────────────────────

def test_delivery_marker_bytes_are_pinned():
    # canary: the wire bytes of the record — a change here is a record
    # migration, never a refactor
    assert delivery_marker("ticket") == "`devcake:delivery:v1 to=ticket`"
    assert delivery_marker("repository") == "`devcake:delivery:v1 to=repository`"
    assert DELIVERY_MARKER_RE.fullmatch(delivery_marker("ticket"))
    with pytest.raises(ValueError):
        delivery_marker("archive")


def test_delivery_of_defaults_to_repository_and_last_marker_wins():
    assert delivery_of(None) == DELIVERY_REPOSITORY
    assert delivery_of("plain brief") == DELIVERY_REPOSITORY
    assert not delivery_recorded("plain brief")
    desc = ("brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: a report\n"
            "\n---\n`devcake:delivery:v1 to=repository`\n")
    assert delivery_of(desc) == DELIVERY_REPOSITORY      # the person reverted
    assert delivery_recorded(desc)
    assert delivery_of("x `devcake:delivery:v1 to=ticket` y") == DELIVERY_TICKET


def test_delivery_of_ignores_defanged_prose():
    # a Dev quoting the marker in a body loses the backtick on every append
    # path (feed.marked_note) — the scan matches the backticked form only
    assert delivery_of("devcake:delivery:v1 to=ticket (quoted)") == DELIVERY_REPOSITORY


# ── the description-append chokepoint ────────────────────────────────────────

def test_marked_note_redacts_before_capping_and_defangs():
    secret = "ghp_" + "A" * 36
    body = "see `devcake:delivery:v1 to=ticket` and token " + secret + " tail"
    note = feed.marked_note(HANDOFF_MARKER, body, cap=60)
    assert note.startswith("\n\n---\n" + HANDOFF_MARKER + "\n")
    assert secret not in note and "ghp_A" not in note      # redacted, not split
    assert "`devcake:delivery:v1" not in note              # defanged
    assert "devcake:delivery:v1 to=ticket" in note          # words kept
    assert note.endswith("\n")


def test_marked_note_bytes_match_the_legacy_handoff_shape():
    # the handoff note's shape is a record (markers.handoff_of parses it);
    # routing it through the chokepoint changed no byte
    assert feed.marked_note(HANDOFF_MARKER, "what changed", cap=4000) == (
        "\n\n---\n" + HANDOFF_MARKER + "\nwhat changed\n")


class _PMO:
    def __init__(self, fail=False):
        self.fail = fail
        self.appended = []

    async def append_description(self, ref, text):
        if self.fail:
            raise RuntimeError("description at the vendor cap")
        self.appended.append((ref.pmo_id, text))


class _Mgr:
    def __init__(self, pmo):
        self.pmo = pmo
        self.audits = []

    def _audit(self, pmo_id, action, detail=""):
        self.audits.append((pmo_id, action, detail))


def test_append_note_returns_true_and_appends():
    mgr = _Mgr(_PMO())
    ok = run_coro(feed.append_note(mgr, MissionRef("p1", "issue"), "\n\n---\nnote",
                                   audit_action="x_failed"))
    assert ok is True and mgr.pmo.appended == [("p1", "\n\n---\nnote")]
    assert mgr.audits == []


def test_append_note_failure_audits_and_returns_false_never_raises():
    mgr = _Mgr(_PMO(fail=True))
    ok = run_coro(feed.append_note(mgr, MissionRef("p1", "issue"), "n",
                                   audit_action="delivery_note_failed"))
    assert ok is False
    assert mgr.audits == [("p1", "delivery_note_failed",
                           "description at the vendor cap")]


# ── the dispatch snapshot ────────────────────────────────────────────────────

def test_dispatch_snapshots_the_recorded_destination(tmp_path):
    from test_prompt_templates import _ForgeWithDescriptor
    from test_transitions import make_mgr, mission

    m = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    m.description = "brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: a report\n"
    from devcake.config import PMOInstance
    mgr, fake, _store = make_mgr(tmp_path, m, forge=_ForgeWithDescriptor())
    mgr.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    launched = []

    async def launch(run, image):
        launched.append(run)
    mgr.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE, mgr.dev_types["senior-dev"]))
    assert run is not None and launched
    assert run.delivery_to == DELIVERY_TICKET

    m2 = mission(labels={"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr2, _fake2, _s2 = make_mgr(tmp_path / "b", m2, forge=_ForgeWithDescriptor())
    mgr2.instance = PMOInstance(name="linear", team_key="DEV", repos=["main"])
    mgr2.runs.bootstrap = type("B", (), {"launch": staticmethod(launch)})()
    run2 = run_coro(mgr2.dispatch(m2, MissionType.EXECUTE, mgr2.dev_types["senior-dev"]))
    assert run2.delivery_to == DELIVERY_REPOSITORY


def test_legacy_run_record_reads_as_repository():
    from devcake.domain.run import Run
    r = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1", mission_pmo_id="p1",
            mission_type="EXECUTE", dev_type="d", seq=1)
    assert r.delivery_to == ""       # "" ⇒ repository for every consumer
    _ = datetime.now(timezone.utc)   # keep the import honest for future rows


# ── ONBOARD declares; the app writes once, then only proposes ────────────────

def _tx():
    from test_transitions import _finalize_payload, _run, make_mgr, mission
    from devcake.domain.orchestrator import transitions, steps
    return _finalize_payload, _run, make_mgr, mission, transitions, steps


def _delivery_notices(fake):
    return [c for c in fake.comments if "Delivery" in c or "delivery" in c]


def test_onboard_bare_plan_needed_writes_the_marker_and_says_so(tmp_path):
    _fp, _run, make_mgr, mission, transitions, steps = _tx()
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _s = make_mgr(tmp_path, m)
    run = _run("ONBOARD", None)
    run_coro(transitions.transition(
        mgr, run, {"outcome": "plan_needed", "summary": "s",
                   "delivery_to": "ticket",
                   "delivery_reason": "the deliverable is a usage report"}, None))
    assert "`devcake:delivery:v1 to=ticket`\nReason: the deliverable is a usage report" in m.description
    assert "DEVCAKE-PLAN" in m.labels and "DEVCAKE-NEEDS-HUMAN" not in m.labels
    notes = _delivery_notices(fake)
    assert len(notes) == 1 and "to this ticket" in notes[0]
    assert "edit the `devcake:delivery:v1` line" in notes[0]
    assert steps.TRANSITION_DELIVERY_NOTE in run.finalized_steps
    assert steps.TRANSITION_DELIVERY_FEED in run.finalized_steps
    # the record write precedes the outcome's own label (a dispatch must
    # never see the stage label without the destination)
    ops = [op[0] for op in fake.ops]
    assert "append_description" in ops


def test_onboard_opportunistic_plan_states_the_destination_and_follows_plan_approval(tmp_path, monkeypatch):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    for gated in (True, False):
        m = mission("in_progress", {"DEVCAKE"})
        mgr, fake, _s = make_mgr(tmp_path / str(gated), m)
        monkeypatch.setattr(mgr.instance, "plan_approval", gated)
        run_coro(transitions.transition(
            mgr, _run("ONBOARD", None),
            {"outcome": "plan_needed", "summary": "s", "delivery_to": "ticket",
             "delivery_reason": "report only"}, "# plan"))
        plan_comment = next(c for c in fake.comments if "opportunistic plan" in c)
        assert "**Delivery:** to this ticket" in plan_comment
        assert "Reason: report only." in plan_comment
        assert "DEVCAKE-EXECUTE" in m.labels
        assert ("DEVCAKE-NEEDS-HUMAN" in m.labels) is gated   # the park follows plan_approval
        assert "`devcake:delivery:v1 to=ticket`" in m.description


def test_onboard_default_and_repository_are_silent(tmp_path):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    for result in ({"outcome": "plan_needed", "summary": "s"},
                   {"outcome": "plan_needed", "summary": "s",
                    "delivery_to": "repository"}):
        m = mission("in_progress", {"DEVCAKE"})
        mgr, fake, _s = make_mgr(tmp_path / str(len(result)), m)
        before = m.description
        run_coro(transitions.transition(mgr, _run("ONBOARD", None), result, "# plan"))
        assert m.description == before
        assert not any("Delivery" in c for c in fake.comments)


@pytest.mark.parametrize("result", [
    {"outcome": "plan_needed", "summary": "s", "delivery_to": "ticket"},
    {"outcome": "plan_needed", "summary": "s", "delivery_to": "archive",
     "delivery_reason": "x"},
    {"outcome": "plan_needed", "summary": "s", "delivery_to": 3},
])
def test_onboard_malformed_declaration_is_bad_output(tmp_path, result):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _s = make_mgr(tmp_path, m)
    with pytest.raises(ValueError):
        run_coro(transitions.transition(mgr, _run("ONBOARD", None), result, None))
    assert "DEVCAKE-PLAN" not in m.labels


def test_onboard_ticket_on_a_board_without_attachments_is_forced_to_repository(tmp_path):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _s = make_mgr(tmp_path, m)
    fake.attachments_supported = False
    run_coro(transitions.transition(
        mgr, _run("ONBOARD", None),
        {"outcome": "plan_needed", "summary": "s", "delivery_to": "ticket",
         "delivery_reason": "report"}, "# plan"))
    assert "devcake:delivery" not in m.description
    plan_comment = next(c for c in fake.comments if "opportunistic plan" in c)
    assert "cannot receive files" in plan_comment
    assert "DEVCAKE-EXECUTE" in m.labels


def test_onboard_append_failure_is_honest_and_never_strands(tmp_path):
    _fp, _run, make_mgr, mission, transitions, steps = _tx()
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _s = make_mgr(tmp_path, m)
    fake.fail_append = True
    run = _run("ONBOARD", None)
    run_coro(transitions.transition(
        mgr, run, {"outcome": "plan_needed", "summary": "s",
                   "delivery_to": "ticket", "delivery_reason": "report"}, None))
    assert "DEVCAKE-PLAN" in m.labels                    # the transition completed
    assert "devcake:delivery" not in (m.description or "")
    note = _delivery_notices(fake)[0]
    assert "could not write the destination" in note
    assert "`devcake:delivery:v1 to=ticket`" in note      # the line to paste
    assert steps.TRANSITION_PLAN_NEEDED in run.finalized_steps


def test_onboard_on_a_recorded_mission_only_proposes(tmp_path):
    _fp, _run, make_mgr, mission, transitions, steps = _tx()
    m = mission("in_progress", {"DEVCAKE"})
    m.description = "brief\n\n---\n`devcake:delivery:v1 to=repository`\n"
    mgr, fake, _s = make_mgr(tmp_path, m)
    before = m.description
    run = _run("ONBOARD", None)
    run_coro(transitions.transition(
        mgr, run, {"outcome": "plan_needed", "summary": "s",
                   "delivery_to": "ticket", "delivery_reason": "a person asked"}, None))
    assert m.description == before                      # never written by a Dev
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels             # even with plan_approval off
    assert "DEVCAKE-PLAN" in m.labels
    notice = next(c for c in fake.comments if "proposes" in c)
    assert "`devcake:delivery:v1 to=ticket`" in notice
    assert "remove DEVCAKE-NEEDS-HUMAN" in notice
    assert steps.TRANSITION_DELIVERY_PROPOSAL in run.finalized_steps
    assert "proposed delivery change" in mgr.needs_human["p1"]


def test_execute_differing_declaration_proposes_and_still_advances(tmp_path):
    _fp, _run, make_mgr, mission, transitions, steps = _tx()
    from test_transitions import FakeForge
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=FakeForge())
    run = _run()                                        # delivery_to "" ⇒ repository
    run_coro(transitions.transition(
        mgr, run, {"outcome": "executed", "summary": "s",
                   "pr_url": "https://forge/pr/9", "delivery_to": "ticket",
                   "delivery_reason": "the human comment asked for the report here"},
        None))
    assert "DEVCAKE-REVIEW" in m.labels                  # the work stands
    assert "DEVCAKE-NEEDS-HUMAN" in m.labels             # the change asks
    assert "devcake:delivery" not in (m.description or "")
    assert any("EXECUTE proposes" in c for c in fake.comments)


def test_execute_matching_declaration_is_a_noop(tmp_path):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    from test_transitions import FakeForge
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=FakeForge())
    run_coro(transitions.transition(
        mgr, _run(), {"outcome": "executed", "summary": "s",
                      "pr_url": "https://forge/pr/9", "delivery_to": "repository"},
        None))
    assert "DEVCAKE-NEEDS-HUMAN" not in m.labels
    assert not any("proposes" in c for c in fake.comments)


def test_review_never_proposes(tmp_path):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    from test_transitions import FakeForge
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=FakeForge())
    before = m.description
    run_coro(transitions.transition(
        mgr, _run("REVIEW", "DEVCAKE-REVIEW"),
        {"outcome": "reviewed", "verdict": "approve", "report_md": "ok",
         "pr_url": "https://forge/pr/8", "delivery_to": "ticket",
         "delivery_reason": "x"}, None))
    assert m.description == before
    assert "DEVCAKE-NEEDS-HUMAN" not in m.labels
    assert not any("proposes" in c for c in fake.comments)


def test_planned_gate_restates_the_recorded_destination(tmp_path, monkeypatch):
    _fp, _run, make_mgr, mission, transitions, _steps = _tx()
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-PLAN"})
    m.description = "brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: report\n"
    mgr, fake, _s = make_mgr(tmp_path, m)
    monkeypatch.setattr(mgr.instance, "plan_approval", True)
    before = m.description
    run_coro(transitions.transition(
        mgr, _run("PLAN", "DEVCAKE-PLAN"), {"outcome": "planned"}, "# plan"))
    plan_comment = next(c for c in fake.comments if "DevCake plan for this mission" in c)
    assert "**Delivery:** to this ticket" in plan_comment
    assert "This board requires a human to approve plans" in plan_comment
    assert m.description == before                      # PLAN writes nothing


# ── decomposition children carry the destination from birth ─────────────────

def _decompose(tmp_path, drafts, *, attachments=True):
    _fp, _run, make_mgr, mission, _t, _s = _tx()
    from devcake.domain.orchestrator import decomposition
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, _store = make_mgr(tmp_path, m)
    fake.attachments_supported = attachments
    descs = []
    orig = fake.create_mission

    async def rec(team, title, description, *a, **k):
        descs.append(description)
        return await orig(team, title, description, *a, **k)
    fake.create_mission = rec
    run_coro(decomposition.finalize_decomposition(
        mgr, _run("ONBOARD", None),
        {"outcome": "decomposed", "summary": "s", "decomposition": drafts}))
    return descs


def test_decomposition_child_carries_the_destination_marker(tmp_path):
    descs = _decompose(tmp_path, [
        {"title": "measure", "description": "Measure usage.",
         "delivery_to": "ticket", "delivery_reason": "a report for the ticket"},
        {"title": "remove", "description": "Remove the unused fields."},
    ])
    assert len(descs) == 2
    assert descs[0].endswith(
        "`devcake:delivery:v1 to=ticket`\nReason: a report for the ticket")
    assert "devcake:decomposition:v1" in descs[0]     # the lineage marker stays
    assert "devcake:delivery" not in descs[1]


def test_decomposition_child_reason_is_neutralized(tmp_path):
    descs = _decompose(tmp_path, [
        {"title": "a", "description": "d", "delivery_to": "ticket",
         "delivery_reason": "see `devcake:delivery:v1 to=repository` too"},
    ])
    body, _, tail = descs[0].rpartition("`devcake:delivery:v1 to=ticket`")
    assert "`devcake:delivery:v1 to=repository`" not in body + tail   # defanged
    assert delivery_of(descs[0]) == DELIVERY_TICKET


def test_decomposition_child_without_reason_is_bad_output(tmp_path):
    with pytest.raises(ValueError):
        _decompose(tmp_path, [{"title": "a", "description": "d",
                               "delivery_to": "ticket"}])


def test_decomposition_child_on_board_without_attachments_stays_repository(tmp_path):
    descs = _decompose(tmp_path, [
        {"title": "a", "description": "d", "delivery_to": "ticket",
         "delivery_reason": "r"}], attachments=False)
    assert "devcake:delivery" not in descs[0]
