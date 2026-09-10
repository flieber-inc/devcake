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


def test_refresh_reads_the_feed_once_and_edits_in_place(tmp_path):
    """ADR-0042 §5 addendum: the Discoveries section is a view over the
    feed, so a refresh reads it ONCE (full) — and hands that read back so
    the boundary's record push reuses it instead of reading again."""
    m, mgr, fake, store = _mgr(tmp_path)
    fake.activity_entries = [ActivityEntry(ts=NOW, author="cake", kind="comment",
                                           body="old `devcake:status:v1`\n\n`devcake:v1`",
                                           entry_id="s1")]
    run = _run(store, 1, "ONBOARD", outcome="plan_needed", status_entry_id="s1")
    before = getattr(fake, "get_activity_calls", 0)
    act = run_coro(status_comment.refresh(mgr, "p1", reason="finalize", run=run, mission=m))
    (_pid, eid, body), = fake.edits
    assert eid == "s1" and "📋 triaged" in body and body.endswith("`devcake:v1`")
    assert getattr(fake, "get_activity_calls", 0) == before + 1
    assert act is not None and [e.entry_id for e in act.entries] == ["s1"]
    assert fake.activity_entries[0].ts == NOW                # the entry never moves
    # a caller's own read is reused: no second read
    run_coro(status_comment.refresh(mgr, "p1", reason="again", run=run, mission=m, act=act))
    assert getattr(fake, "get_activity_calls", 0) == before + 1


# ── the Discoveries section: one place, a view, no scan counts it ──────────

LEADS = ("📨 **Leads from T-S, step 2.** 1 lead — leads, not truths: verify against "
         "the source before relying on them. The findings are in full in the record below.\n\n"
         "> the config default changed to 5\n> Evidence: src/x.py:0\n\n"
         "<details>\n<summary>Details — record</summary>\n\n▸ **Record**\n\n"
         "`devcake:discovery-in:v1 src=T-S step=2`\n\n"
         "🔎 [T-S · step 2 · 2026-09-10] — leads, not truths: verify against the source "
         "before relying. Source record: `DISCOVERY_2.md` on T-S.\n\n"
         "`devcake:finding:v1 sha=e3ebbc098dc6`\n\n**1.**\n\n"
         "> Finding: the config default changed to 5\n>\n> Evidence: src/x.py:0; repro: pytest -k f0"
         "\n>\n> Scope: scope 0\n\n*— steward: target touches the same config*\n\n"
         "</details>\n\n`devcake:v1`")
CARD = ("🔀 Step 1 · EXECUTE · executed · 3 min · $0.10\n\n**Result:** pr\n**Next:** DevCake — REVIEW.\n\n"
        "<details>\n<summary>Details — token report · discoveries · run</summary>\n\n"
        "▸ **Discoveries**\n\n`devcake:discovery:v1 step=1 n=1`\n\n"
        "🔎 1 discovery from step 1 (EXECUTE) — leads for related missions, routed separately.\n\n"
        "`devcake:finding:v1 sha=bd26fc37be01`\n\nFull record attached: [DISCOVERY_1.md](https://files.example/DISCOVERY_1.md)\n\n"
        "**1.**\n\n> the regression suite already covers public_api\n> Evidence: tests/suites/regression.txt line 78\n\n"
        "▸ **Run**\n\n`T-1-1-EXECUTE-AAAAAA`\n\n</details>\n\n`devcake:v1`")


def _feed_with_discoveries():
    return [ActivityEntry(ts=NOW, author="cake", kind="comment", body=CARD, entry_id="c1"),
            ActivityEntry(ts=NOW + timedelta(minutes=5), author="cake", kind="comment",
                          body=LEADS, entry_id="c2"),
            ActivityEntry(ts=NOW + timedelta(minutes=9), author="cake", kind="comment",
                          body="old `devcake:status:v1`\n\n`devcake:v1`", entry_id="s1")]


def test_status_comment_gathers_discoveries_and_leads(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    _run(store, 1, "EXECUTE", outcome="executed")
    body = status_comment.render(mgr, m, status_comment.runs_of(mgr, "p1"), pr_url=None,
                                 collapsible="details", entries=_feed_with_discoveries())
    assert "▸ **Discoveries**" in body
    assert "**Reported by this mission**" in body
    assert "step 1 · 1 finding · [record file](https://files.example/DISCOVERY_1.md)" in body
    assert "> the regression suite already covers public_api" in body
    assert "**Leads received**" in body
    assert "from T-S, step 2 · 2026-09-09" in body
    assert "> Finding: the config default changed to 5" in body     # in full
    assert "> Scope: scope 0" in body
    assert "*— steward: target touches the same config*" in body
    # a VIEW: no scan counts it, no step-file token, no provenance line
    plain = unquoted(body)
    from devcake.domain.orchestrator.markers import (discovery_in_keys, discovery_posts,
                                                     finding_fingerprints)
    assert discovery_posts(plain) == [] and discovery_in_keys(plain) == set()
    assert finding_fingerprints(plain) == set()
    assert STEP_MARKER.search(plain) is None and "`DISCOVERY_" not in body
    assert "🔎 [" not in body
    sealed = body + "\n\n`devcake:v1`"
    assert sealed.count(STATUS_MARKER) == 1 and unquoted(sealed).count("`devcake:") == 2
    assert not freshness._is_material(sealed)
    assert is_devcake_comment(sealed) and feed.is_status_comment(sealed)
    entry = ActivityEntry(ts=NOW, author="cake", kind="comment", body=sealed, entry_id="s9")
    assert dispatch._derive_seq(Activity(mission=m, entries=[entry])) == 1
    # and the projection still drops it whole — nothing of it reaches a Dev
    assert [p.body for p in feed.unfold_entries([entry])] == []


def test_status_comment_without_discoveries_has_no_section(tmp_path):
    m, mgr, fake, store = _mgr(tmp_path)
    body = status_comment.render(mgr, m, [], pr_url=None, collapsible="details",
                                 entries=[ActivityEntry(ts=NOW, author="felipe", kind="comment",
                                                        body="hi", entry_id="h")])
    assert "Discoveries" not in body and "Record" in body


def test_digest_budget_drops_oldest_deliveries_and_says_so(tmp_path, monkeypatch):
    from devcake.domain.orchestrator import discovery
    monkeypatch.setattr(discovery, "DIGEST_BUDGET", 400)
    entries = []
    for i in range(4):
        entries.append(ActivityEntry(ts=NOW + timedelta(minutes=i), author="cake", kind="comment",
                                     body=LEADS.replace("T-S step=2", f"T-{i} step=2")
                                               .replace("from T-S,", f"from T-{i},")
                                               .replace("[T-S · step 2", f"[T-{i} · step 2"),
                                     entry_id=f"c{i}"))
    lines = discovery.digest_lines(entries)
    text = "\n\n".join(lines)
    assert "older deliveries omitted here" in text or "older delivery omitted here" in text
    assert "from T-3, step 2" in text                      # the newest survives
    assert "from T-0, step 2" not in text


def test_steward_delivery_refreshes_the_recipient_status(tmp_path):
    from test_steward import _apply, _route, _route_setup
    pmo, mgr, run = _route_setup(tmp_path)
    from devcake.domain.orchestrator import status_comment as sc
    calls = []
    async def fake_refresh(mgr_, pmo_id, **kw):
        calls.append((pmo_id, kw.get("reason")))
    import devcake.domain.orchestrator.steward as steward_mod
    orig = sc.refresh
    sc.refresh = fake_refresh
    try:
        assert _apply(mgr, run, [_route(finding=1)]) == (1, 0)
    finally:
        sc.refresh = orig
    assert ("tgt", "leads_delivered") in calls


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
