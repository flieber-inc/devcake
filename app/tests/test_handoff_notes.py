"""ADR-0032 — mission handoff notes: REVIEW-authored closing note appended
to the mission description on approve (redacted, marker-defanged, capped,
best-effort), collected per done blocker BEFORE the mount drops, rendered in
the blocker prompt note and MISSION.md, with the ONBOARD staleness check."""
import asyncio
from datetime import datetime, timezone

from fakes import make_mission_manager

from devcake.domain.model import Mission
from devcake.domain.orchestrator import activity_payload as ap
from devcake.domain.orchestrator import dispatch, review
from devcake.domain.orchestrator.markers import (HANDOFF_APPEND_MAX,
                                                 HANDOFF_EXCERPT_MAX,
                                                 HANDOFF_MARKER, handoff_of)
from devcake.domain.run import Run

from test_transitions import FakeForge, make_mgr, mission  # noqa: F401

NOW = datetime.now(timezone.utc)


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


# ── defang: the ONE marker-neutralizing transformation ──────────────────────

def test_defang_neutralizes_both_marker_families():
    from devcake.domain.orchestrator.markers import defang
    text = ("quoting `devcake:handoff:v1` and `devcake-repo:web` and "
            "`devcake:discovery:v1 step=1 n=1` verbatim")
    out = defang(text)
    assert "`devcake:" not in out and "`devcake-repo:" not in out
    assert "devcake:handoff:v1" in out          # text kept, teeth pulled
    assert "devcake-repo:web" in out
    assert "devcake:discovery:v1 step=1 n=1" in out


# ── handoff_of: the LAST marker wins ─────────────────────────────────────────

def test_handoff_of_absent_and_single():
    assert handoff_of(None) == ""
    assert handoff_of("plain description") == ""
    d = f"desc\n\n---\n{HANDOFF_MARKER}\nrenamed Store to Vault\n"
    assert handoff_of(d) == "renamed Store to Vault"


def test_handoff_of_last_marker_wins_and_stops_at_rule():
    d = (f"desc\n\n---\n{HANDOFF_MARKER}\nold note\n"
         f"\n---\n{HANDOFF_MARKER}\nnew note\n\n---\n_lineage footer_")
    assert handoff_of(d) == "new note"


# ── the approve-finalize append ──────────────────────────────────────────────

def _approve(mgr, run, handoff="moved config to profiles; see PR"):
    result = {"verdict": "approve", "report_md": "ok"}
    if handoff is not None:
        result["handoff_md"] = handoff
    return run_coro(review.finalize_review(mgr, run, result))


def _review_run():
    return Run(run_id="T-1-1-REVIEW-AAAAAA", mission_key="T-1",
               mission_pmo_id="p1", mission_type="REVIEW",
               dev_type="senior-dev", seq=1,
               stage_label_at_dispatch="DEVCAKE-REVIEW")


def _approve_mgr(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    mgr.forges.instance("main").auto_merge = True
    return m, mgr, fake


def test_approve_appends_marked_handoff(tmp_path):
    m, mgr, fake = _approve_mgr(tmp_path)
    _approve(mgr, _review_run())
    assert HANDOFF_MARKER in (m.description or "")
    assert handoff_of(m.description) == "moved config to profiles; see PR"
    assert "done" in fake.statuses                    # close proceeded


def test_append_defangs_backtick_markers(tmp_path):
    m, mgr, fake = _approve_mgr(tmp_path)
    _approve(mgr, _review_run(), handoff=(
        "quoting `devcake:decomposition:v1 parent=x` and `devcake-repo:web` "
        "and even `devcake:handoff:v1` here"))
    body = handoff_of(m.description)
    assert "`devcake:" not in body and "`devcake-repo:" not in body
    assert "devcake:decomposition:v1" in body         # text kept, teeth pulled
    # the ONE live marker is the app-written line, not the quoted one
    assert (m.description or "").count(HANDOFF_MARKER) == 1


def test_append_caps_length(tmp_path):
    m, mgr, fake = _approve_mgr(tmp_path)
    _approve(mgr, _review_run(), handoff="x" * (HANDOFF_APPEND_MAX + 500))
    assert len(handoff_of(m.description)) <= HANDOFF_APPEND_MAX


def test_reject_and_empty_handoff_append_nothing(tmp_path):
    m, mgr, fake = _approve_mgr(tmp_path)
    run_coro(review.finalize_review(mgr, _review_run(),
                                    {"verdict": "reject", "report_md": "no",
                                     "handoff_md": "should not land"}))
    assert HANDOFF_MARKER not in (m.description or "")
    m2, mgr2, fake2 = _approve_mgr(tmp_path / "b")
    _approve(mgr2, _review_run(), handoff=None)
    assert HANDOFF_MARKER not in (m2.description or "")
    assert "done" in fake2.statuses


def test_append_failure_is_best_effort(tmp_path):
    # lineage-note doctrine: a vendor description-cap refusal must never
    # block the close
    m, mgr, fake = _approve_mgr(tmp_path)
    fake.fail_append = True
    _approve(mgr, _review_run())
    assert "done" in fake.statuses
    assert HANDOFF_MARKER not in (m.description or "")


def test_redelivery_appends_once(tmp_path):
    m, mgr, fake = _approve_mgr(tmp_path)
    run = _review_run()
    _approve(mgr, run)
    _approve(mgr, run)                                # redelivered finalize
    assert (m.description or "").count(HANDOFF_MARKER) == 1


def test_tripped_gate_appends_no_handoff(tmp_path):
    # the handoff is a property of the CLOSE — a withheld transition
    # (ADR-0031 trip) must not write one
    from devcake.domain.model import ActivityEntry
    m, mgr, fake = _approve_mgr(tmp_path)
    fake.activity_entries = [ActivityEntry(
        ts=NOW, author="felipe", kind="comment", body="new steering",
        entry_id="e2")]
    run = _review_run()
    run.feed_watermark = {"entry_id": "e1", "ts": NOW.isoformat()}
    _approve(mgr, run)
    assert HANDOFF_MARKER not in (m.description or "")
    assert fake.statuses == []


# ── blocker notes survive the mount drops ────────────────────────────────────

def _blocker_mission(pmo_id, key, status="done", handoff=None, blocked_by=()):
    desc = "original brief"
    if handoff:
        desc += f"\n\n---\n{HANDOFF_MARKER}\n{handoff}\n"
    return Mission(pmo_id=pmo_id, pmo_kind="issue", instance="linear",
                   key=key, title=f"title {key}", status=status,
                   labels={"DEVCAKE"}, updated_at=NOW, description=desc,
                   blocked_by=list(blocked_by))


def _blocker_run(mid, key, repo_ref):
    return Run(run_id=f"LINEAR-{key}-1-EXECUTE-AAAAAA", mission_key=key,
               mission_type="EXECUTE", dev_type="d", seq=1,
               repo_ref=repo_ref, mission_pmo_id=mid, pmo_ref="linear")


def _locator_mgr(tmp_path, missions):
    from test_blocker_work import BlockerPMO, _ROInternal
    return make_mission_manager(tmp_path, pmo=BlockerPMO(missions),
                                internal_forge=_ROInternal())


def test_notes_survive_same_repo_drop(tmp_path):
    # blocker shares the primary repo: mount silently dropped, note KEPT
    a = _blocker_mission("a", "T-A", handoff="the schema moved to v2")
    b = _blocker_mission("b", "T-B", status="backlog", blocked_by=["a"])
    mgr = _locator_mgr(tmp_path, {"a": a, "b": b})
    entries, skips, notes = run_coro(dispatch.resolve_blocker_work(
        mgr, b, "shared", [_blocker_run("a", "T-A", "shared")]))
    assert entries == []
    assert notes == [{"mission_key": "T-A", "title": "title T-A",
                      "handoff": "the schema moved to v2"}]


def test_notes_survive_the_cap_and_excerpts_are_bounded(tmp_path):
    blockers = {}
    ids = []
    for i in range(3):
        mid = f"b{i}"
        blockers[mid] = _blocker_mission(mid, f"T-{i}", handoff="h" * 2000)
        ids.append(mid)
    dep = _blocker_mission("dep", "T-D", status="backlog", blocked_by=ids)
    blockers["dep"] = dep
    mgr = _locator_mgr(tmp_path, blockers)
    runs = [_blocker_run(f"b{i}", f"T-{i}", f"repo-{i}") for i in range(3)]
    entries, skips, notes = run_coro(dispatch.resolve_blocker_work(
        mgr, dep, "primary", runs, max_extras=1))
    assert len(entries) == 1 and len(notes) == 3      # cap eats mounts only
    assert all(len(n["handoff"]) <= HANDOFF_EXCERPT_MAX for n in notes)


def test_not_done_blockers_get_no_note(tmp_path):
    a = _blocker_mission("a", "T-A", status="backlog", handoff="early note")
    b = _blocker_mission("b", "T-B", status="backlog", blocked_by=["a"])
    mgr = _locator_mgr(tmp_path, {"a": a, "b": b})
    entries, skips, notes = run_coro(dispatch.resolve_blocker_work(
        mgr, b, "primary", []))
    assert notes == [] and any("not done" in s for s in skips)


# ── rendering: prompt note + MISSION.md ──────────────────────────────────────

def test_blocker_note_renders_handoffs_and_staleness_rule(tmp_path):
    mgr = make_mission_manager(tmp_path)
    note = dispatch._blocker_repos_note(
        mgr,
        [{"repo_ref": "linear-t-a", "mission_key": "T-A"}],
        [],
        [{"mission_key": "T-A", "title": "title T-A", "handoff": "moved X"},
         {"mission_key": "T-B", "title": "title T-B", "handoff": "dropped Y"}])
    assert "Handoff: moved X" in note
    assert "`T-B` — title T-B (no work-repo mount)" in note
    assert "Handoff: dropped Y" in note
    assert "the handoff is newer" in note             # staleness rule


def test_blocker_note_empty_without_content(tmp_path):
    mgr = make_mission_manager(tmp_path)
    assert dispatch._blocker_repos_note(mgr, [], [], []) == ""
    # notes without handoff text add nothing on their own
    assert dispatch._blocker_repos_note(
        mgr, [], [], [{"mission_key": "T-A", "title": "t",
                       "handoff": ""}]) == ""


def test_mission_md_gains_handoff_block(tmp_path):
    from test_transitions import FakePMO
    m = mission()
    mgr = make_mission_manager(tmp_path, pmo=FakePMO(m))
    payload = run_coro(ap.activity_payload(
        mgr, "p1", "issue",
        blocker_notes=[{"mission_key": "T-A", "title": "title T-A",
                        "handoff": "moved X"}]))
    assert "## Blocked by (completed — handoffs)" in payload["mission_md"]
    assert "Handoff: moved X" in payload["mission_md"]
    # the Redis-fallback rebuild passes no notes → no section
    payload = run_coro(ap.activity_payload(mgr, "p1", "issue"))
    assert "Blocked by (completed" not in payload["mission_md"]


# ── playbook contracts ───────────────────────────────────────────────────────

def test_playbooks_carry_the_contract():
    from devcake.prompts import ONBOARD_PLAYBOOK, REVIEW_PLAYBOOK
    from devcake.prompts.customer_success import CS_PLAYBOOKS
    assert "Staleness check" in ONBOARD_PLAYBOOK
    assert "handoff_md" in REVIEW_PLAYBOOK
    assert "REQUIRED on approve" in REVIEW_PLAYBOOK
    assert "handoff_md" in CS_PLAYBOOKS["REVIEW"]