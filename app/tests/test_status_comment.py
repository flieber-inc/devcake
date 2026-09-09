"""ADR-0042 §5 — the status comment: one per mission, created by whichever
dispatch finds none, edited in place at the events of the record, a view
that never gates and never counts."""
from datetime import datetime, timedelta, timezone

from devcake.domain.model import Activity, ActivityEntry
from devcake.domain.orchestrator import dispatch, feed, freshness, status_comment
from devcake.domain.orchestrator.feed import is_devcake_comment, unquoted
from devcake.domain.orchestrator.markers import (PART_LINE, STATUS_MARKER,
                                                 STEP_MARKER)
from devcake.domain.run import Run
from devcake.ports.pmo import PMOBudgetExceeded, PMOTransient

from test_discovery_harvest import _exec_run, _payload
from test_transitions import make_mgr, mission, run_coro

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _run(store, seq, mtype, *, state="finished", outcome=None, attempt=1,
         verdict=None, minutes=5, cost=None, status_entry_id=""):
    r = Run(run_id=f"T-1-{seq}-{mtype}-AAAAAA{attempt}", mission_key="T-1",
            mission_pmo_id="p1", mission_type=mtype, dev_type="senior-dev",
            seq=seq, attempt_of_step=attempt, state=state,
            status_entry_id=status_entry_id)
    r.created_at = NOW + timedelta(minutes=seq * 10 + attempt)
    r.started_at = r.created_at
    if state == "finished":
        r.ended_at = r.started_at + timedelta(minutes=minutes)
        r.result = {"outcome": outcome, "summary": "s"}
        if verdict:
            r.result["verdict"] = verdict
    if cost is not None:
        r.token_report = {"cost_usd_native": cost}
    store.save(r)
    return r


def _mgr(tmp_path, labels=frozenset({"DEVCAKE"}), status="in_progress"):
    m = mission(status, set(labels))
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.record_feed = True
    return m, mgr, fake, store


def _render(mgr, m, pr_url=None):
    return status_comment.render(mgr, m, status_comment.runs_of(mgr, m.pmo_id),
                                 pr_url=pr_url, collapsible="details")


# ── invariants ──────────────────────────────────────────────────────────────

def test_body_is_a_view_that_no_scan_counts(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    _run(store, 1, "ONBOARD", outcome="plan_needed", cost=0.5)
    _run(store, 2, "EXECUTE", state="running")
    body = _render(mgr, m) + "\n\n`devcake:v1`"
    assert is_devcake_comment(body) and feed.is_status_comment(body)
    assert STEP_MARKER.search(unquoted(body)) is None       # "1 · ONBOARD", never a file token
    assert not any(PART_LINE.match(l) for l in body.splitlines())
    assert not freshness._is_material(body)
    entry = ActivityEntry(ts=NOW, author="cake", kind="comment", body=body, entry_id="s1")
    assert dispatch._derive_seq(Activity(mission=m, entries=[entry])) == 1
    assert unquoted(body).count("`devcake:") == 2            # status marker + sentinel only
    assert "**Now:** ⏳ Running step 2 · EXECUTE (attempt 1) since" in body
    assert "| 1 · ONBOARD | 📋 triaged | 5 min | $0.50 |" in body
    assert "| 2 · EXECUTE | ⏳ running | " in body
    assert "**Cost so far:** $0.50 across 2 steps" in body


def test_ladder_is_the_runs_never_the_label_history(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path, {"DEVCAKE", "DEVCAKE-REVIEW"})
    _run(store, 1, "REVIEW", outcome="reviewed", verdict="approve", cost=3.05)
    body = _render(mgr, m, pr_url="https://forge.example/pr/8")
    assert body.count("| 1 · REVIEW |") == 1 and "ONBOARD" not in body
    assert "| 1 · REVIEW | ✅ approved | 5 min | $3.05 |" in body
    assert "**PR:** https://forge.example/pr/8" in body
    assert "**Now:** ⏳ Queued for REVIEW." in body           # nothing in flight


def test_latest_attempt_per_step_and_now_lines(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path, {"DEVCAKE", "DEVCAKE-EXECUTE", "DEVCAKE-FAILED"})
    _run(store, 1, "EXECUTE", state="failed", attempt=1)
    _run(store, 1, "EXECUTE", state="failed", attempt=3)
    body = _render(mgr, m)
    assert body.count("| 1 · EXECUTE |") == 1 and "(attempt 3)" in body
    assert "**Now:** ⚠️ Gave up — needs a person" in body
    m.labels = {"DEVCAKE", "DEVCAKE-MERGE"}
    assert "**Now:** ⏳ Awaiting merge of https://x." in _render(mgr, m, pr_url="https://x")
    m.labels = {"DEVCAKE", "DEVCAKE-NEEDS-HUMAN"}
    assert "✋ Parked — needs a person" in _render(mgr, m)
    m.labels = {"DEVCAKE", "DEVCAKE-SKIP"}
    assert "⏸ Stopped by DEVCAKE-SKIP" in _render(mgr, m)
    m.status = "done"
    assert "**Now:** ✅ Done." in _render(mgr, m)


def test_cost_line_is_the_one_cost_rollup(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    _run(store, 1, "ONBOARD", outcome="plan_needed", cost=1.25)
    _run(store, 2, "EXECUTE", outcome="executed", cost=2.0)
    total = dispatch.mission_cost(mgr, "p1")
    assert f"**Cost so far:** ${total:.2f}" in _render(mgr, m)


# ── ensure / refresh ────────────────────────────────────────────────────────

def test_ensure_creates_once_and_adopts_a_found_entry(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    run = _run(store, 1, "ONBOARD", state="running")
    cid = run_coro(status_comment.ensure(mgr, m, run, found=""))
    assert cid and len(fake.comments) == 1
    body = fake.comments[0]
    assert feed.is_status_comment(body) and STATUS_MARKER in unquoted(body)
    assert mgr._status_entries["p1"] == cid
    # a later dispatch finds it by marker: adopted, nothing posted
    assert run_coro(status_comment.ensure(mgr, m, run, found="s-existing")) == "s-existing"
    assert len(fake.comments) == 1
    # a project mission never gets one
    m.pmo_kind = "project"
    assert run_coro(status_comment.ensure(mgr, m, run, found="")) == ""


def test_refresh_edits_in_place_without_a_feed_read(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    fake.activity_entries = [ActivityEntry(ts=NOW, author="cake", kind="comment",
                                           body="old `devcake:status:v1`\n\n`devcake:v1`",
                                           entry_id="s1")]
    run = _run(store, 1, "ONBOARD", outcome="plan_needed", status_entry_id="s1")
    before = getattr(fake, "get_activity_full_calls", 0)
    run_coro(status_comment.refresh(mgr, "p1", reason="finalize", run=run, mission=m))
    (_pid, eid, body), = fake.edits
    assert eid == "s1" and "📋 triaged" in body and body.endswith("`devcake:v1`")
    assert getattr(fake, "get_activity_full_calls", 0) == before
    assert fake.activity_entries[0].ts == NOW                # the entry never moves


def test_refresh_without_an_id_audits_once_and_never_edits(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    audits = []
    mgr._audit = lambda pid, action, detail="": audits.append(action)
    run_coro(status_comment.refresh(mgr, "p1", reason="finalize", mission=m))
    run_coro(status_comment.refresh(mgr, "p1", reason="finalize", mission=m))
    assert audits == ["status_comment_unknown"]
    assert getattr(fake, "edits", []) == []


def test_refresh_failures_are_swallowed_and_audited(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    run = _run(store, 1, "ONBOARD", outcome="plan_needed", status_entry_id="gone")
    fake.strict_edits = True
    audits = []
    mgr._audit = lambda pid, action, detail="": audits.append(action)
    run_coro(status_comment.refresh(mgr, "p1", reason="finalize", run=run, mission=m))
    assert audits == ["status_comment_failed"]
    assert run.status_entry_id == "" and "p1" not in mgr._status_entries

    async def starved(ref, entry_id, markdown):
        raise PMOBudgetExceeded("reserve reached", retry_after=30)
    fake.edit_feed = starved
    run.status_entry_id = "s1"
    run_coro(status_comment.refresh(mgr, "p1", reason="finalize", run=run, mission=m))
    assert audits[-1] == "status_comment_failed"
    assert run.status_entry_id == "s1"                        # a transient keeps the id
    assert isinstance(PMOBudgetExceeded("x"), PMOTransient)


def test_finalize_refreshes_the_status_comment_last(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.activity_entries = [ActivityEntry(ts=NOW, author="cake", kind="comment",
                                           body="old `devcake:status:v1`\n\n`devcake:v1`",
                                           entry_id="s1")]
    run = _exec_run(store)
    run.status_entry_id = "s1"
    store.save(run)
    run_coro(mgr.finalize(run, _payload(pr_url="https://forge.example/pr/8")))
    assert run.state == "finished"
    edits = [e for e in getattr(fake, "edits", []) if e[1] == "s1"]
    assert len(edits) == 1
    body = edits[0][2]
    assert "| 1 · EXECUTE | 🔀 PR opened |" in body
    assert "**PR:** https://forge.example/pr/8" in body
    assert "**Now:** ⏳ Queued for REVIEW." in body           # labels moved on
