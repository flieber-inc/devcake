"""ADR-0031 phase 1 — the Freshness Gate on REVIEW's approve finalize.

A positive-verdict REVIEW is the pipeline's last feed read; material entries
landing after the run's dispatch watermark must withhold the done-transition
(counted re-review directive, conflict-resolve pattern) or, past the budget,
close with an explicit disclosure. The check is fail-open; the directive
post, once material is found, is not."""
import asyncio
from datetime import datetime, timedelta, timezone

from devcake.domain.model import ActivityEntry, MissionType
from devcake.domain.orchestrator import freshness, review
from devcake.domain.run import Run

from test_transitions import FakeForge, FakePMO, make_mgr, mission  # noqa: F401


def run_coro(c):
    return asyncio.new_event_loop().run_until_complete(c)


def _entry(entry_id, body, author="felipe", ts=None):
    return ActivityEntry(
        ts=ts or datetime.now(timezone.utc), author=author, kind="comment",
        body=body, entry_id=entry_id)


SENTINEL = "`devcake:v1`"


def _review_run(watermark_id="e1", steps=()):
    r = Run(run_id="T-1-1-REVIEW-AAAAAA", mission_key="T-1",
            mission_pmo_id="p1", mission_type="REVIEW", dev_type="senior-dev",
            seq=1, stage_label_at_dispatch="DEVCAKE-REVIEW")
    if watermark_id:
        r.feed_watermark = {"entry_id": watermark_id,
                            "ts": r.created_at.isoformat()}
    r.finalized_steps = list(steps)
    return r


def _gate_mgr(tmp_path, entries, auto_merge=True):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    mgr.forges.instance("main").auto_merge = auto_merge
    fake.activity_entries = list(entries)
    return m, mgr, fake


def _approve(mgr, run):
    return run_coro(review.finalize_review(
        mgr, run, {"verdict": "approve", "report_md": "ok"}))


# ── the gate decision ────────────────────────────────────────────────────────

def test_no_new_material_passes_and_closes(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [_entry("e1", "brief" + SENTINEL,
                                               author="devcake")])
    run = _review_run(watermark_id="e1")
    _approve(mgr, run)
    assert "done" in fake.statuses and "DEVCAKE-REVIEW" not in m.labels
    assert "review:freshness_ok" in run.finalized_steps
    assert not any("freshness-rereview" in c for c in fake.comments)


def test_sentinel_only_new_entries_are_immaterial(tmp_path):
    # everything DevCake posts after the watermark is bookkeeping BY
    # CONSTRUCTION — the livelock guarantee (ADR-0031 D3)
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "🧾 step marker `1_REVIEW.md`\n\n" + SENTINEL,
               author="devcake")])
    _approve(mgr, _review_run(watermark_id="e1"))
    assert "done" in fake.statuses


def test_human_comment_after_watermark_trips(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "wait — the config default changed, please re-check")])
    run = _review_run(watermark_id="e1")
    _approve(mgr, run)
    # withheld: no approval artifact, no transition, directive posted
    assert fake.statuses == [] and "DEVCAKE-REVIEW" in m.labels
    assert fake.swaps == []
    directives = [c for c in fake.comments
                  if "`devcake:freshness-rereview:1`" in c]
    assert len(directives) == 1
    assert "No reply needed" in directives[0]        # ack-spiral guard
    assert "review:freshness_tripped" in run.finalized_steps
    assert (run.verdict or "").startswith("handed off")  # OTel wording trap


def test_trip_posts_no_approval_artifacts(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "new steering")])
    forge = mgr.forges.get("main")
    _approve(mgr, _review_run(watermark_id="e1"))
    assert forge.pr_comments == [] and forge.merges == []


def test_exhausted_budget_closes_with_disclosure(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "🔄 directive `devcake:freshness-rereview:2`\n\n"
               + SENTINEL, author="devcake"),
        _entry("e2", "another human comment")])
    run = _review_run(watermark_id="e1")
    _approve(mgr, run)
    assert "done" in fake.statuses                    # proceeds, never holds
    warn = [c for c in fake.comments if "unevaluated activity" in c]
    assert len(warn) == 1 and "Cumulative recorded mission cost" in warn[0]
    assert not any("freshness-rereview:3" in c for c in fake.comments)
    assert "review:freshness_exhausted" in run.finalized_steps


def test_quoted_freshness_marker_does_not_count(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "as devcake said:\n> 🔄 `devcake:freshness-rereview:2`"),
        _entry("e2", "new human comment")])
    _approve(mgr, _review_run(watermark_id="e1"))
    assert any("`devcake:freshness-rereview:1`" in c for c in fake.comments)


def test_truncated_fetch_trips_never_passes(tmp_path):
    # gitea_issues pages ASCENDING — its hard stop drops the NEWEST entries,
    # exactly the ones the gate exists to catch: truncated ⇒ material-unknown
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake")])
    fake.activity_truncated = True
    _approve(mgr, _review_run(watermark_id="e1"))
    assert fake.statuses == []
    assert any("feed truncated" in c and "freshness-rereview:1" in c
               for c in fake.comments)


def test_deleted_watermark_treats_all_as_unread(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e9", "an old human comment")])
    _approve(mgr, _review_run(watermark_id="gone"))
    assert fake.statuses == []
    assert any("freshness-rereview:1" in c for c in fake.comments)


def test_empty_feed_without_watermark_passes(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [])
    _approve(mgr, _review_run(watermark_id=None))
    assert "done" in fake.statuses


def test_ts_fallback_without_watermark(tmp_path):
    # legacy record / internal forge absent: entries newer than the run's
    # creation are unread; older ones are not
    now = datetime.now(timezone.utc)
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry(None, "old comment", ts=now - timedelta(minutes=5)),
        _entry(None, "posted mid-run", ts=now + timedelta(minutes=5))])
    _approve(mgr, _review_run(watermark_id=None))
    assert fake.statuses == []
    assert any("freshness-rereview:1" in c for c in fake.comments)


# ── fail-open / fail-closed boundaries ───────────────────────────────────────

def test_check_failure_fails_open(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [])
    fake.activity_exc = RuntimeError("PMO 502")
    _approve(mgr, _review_run(watermark_id="e1"))
    assert "done" in fake.statuses                    # proceeded, no wedge
    assert not any("freshness-rereview" in c for c in fake.comments)


def test_directive_post_failure_still_withholds(tmp_path):
    # fail-closed on found material: a failed post must neither close the
    # mission nor wedge finalize — no marker posted, count cannot inflate,
    # the next plain REVIEW's mirror carries the material anyway
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "new steering")])
    orig = fake.post_feed

    async def _refuse(ref, markdown):
        if "freshness-rereview" in markdown:
            raise RuntimeError("PMO down")
        await orig(ref, markdown)
    fake.post_feed = _refuse
    run = _review_run(watermark_id="e1")
    _approve(mgr, run)                                # must not raise
    assert fake.statuses == [] and "DEVCAKE-REVIEW" in m.labels
    assert "review:freshness_tripped" in run.finalized_steps


# ── redelivery protocol ──────────────────────────────────────────────────────

def test_redelivered_tripped_finalize_returns_early(tmp_path):
    # the check may NOT re-run (it can fail open past a posted directive):
    # a would-trip feed state must produce zero new posts and no transition
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "new steering")])
    _approve(mgr, _review_run(watermark_id="e1",
                              steps=["review:freshness_tripped",
                                     "review:freshness_directive"]))
    assert fake.comments == [] and fake.statuses == [] and fake.swaps == []


def test_redelivered_ok_finalize_skips_the_check(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "would trip if re-checked")])
    _approve(mgr, _review_run(watermark_id="e1",
                              steps=["review:freshness_ok"]))
    assert "done" in fake.statuses
    assert not any("freshness-rereview" in c for c in fake.comments)


def test_pre_upgrade_redelivery_past_merge_skips_gate(tmp_path):
    # a run that already merged (pre-upgrade dispatch, post-upgrade
    # redelivery) must not have the gate withhold `review:done` on a merged
    # PR — an incoherent state
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "would trip")])
    _approve(mgr, _review_run(watermark_id="e1", steps=["review:merge"]))
    assert "done" in fake.statuses
    assert not any("freshness-rereview" in c for c in fake.comments)


# ── watermark capture (dispatch plumbing) ────────────────────────────────────

def _dispatch_with_entries(tmp_path, entries, forge_fake):
    from test_activity_repos import _dispatch_setup
    mgr, fake, m, launched = _dispatch_setup(tmp_path, forge_fake)
    fake.activity_entries = list(entries)
    run = run_coro(mgr.dispatch(m, MissionType.EXECUTE,
                                mgr.dev_types["senior-dev"]))
    return run


def test_dispatch_snapshots_watermark_from_the_mirror(tmp_path):
    from fakes import FakeInternalForge
    ts = datetime.now(timezone.utc)
    run = _dispatch_with_entries(tmp_path, [
        _entry("e1", "first", ts=ts - timedelta(minutes=1)),
        _entry("e2", "second", ts=ts)], FakeInternalForge())
    assert run.feed_watermark == {"entry_id": "e2", "ts": ts.isoformat()}


def test_failed_push_captures_no_watermark(tmp_path):
    # a failed push serves the PREVIOUS snapshot — a watermark from this
    # fetch would overclaim what the Dev actually read
    from fakes import FakeInternalForge
    run = _dispatch_with_entries(
        tmp_path, [_entry("e1", "x")],
        FakeInternalForge(push_exc=RuntimeError("gitea down")))
    assert run.feed_watermark == {}


def test_no_internal_forge_captures_no_watermark(tmp_path):
    run = _dispatch_with_entries(tmp_path, [_entry("e1", "x")], None)
    assert run.feed_watermark == {}


def test_empty_feed_captures_no_watermark(tmp_path):
    from fakes import FakeInternalForge
    run = _dispatch_with_entries(tmp_path, [], FakeInternalForge())
    assert run.feed_watermark == {}


# ── deferred-merge sweep: disclose-only ──────────────────────────────────────

def test_sweep_discloses_unread_at_close(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [
        _entry("e1", "brief" + SENTINEL, author="devcake"),
        _entry("e2", "posted during the retry window")])
    run = _review_run(watermark_id="e1")
    run.state = "finished"
    mgr.runs.store.save(run)
    run_coro(freshness.disclose_unread_at_close(mgr, m))
    assert any("did not see" in c for c in fake.comments)
    assert fake.statuses == []                        # disclosure ONLY


def test_sweep_disclosure_without_review_run_is_silent(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [_entry("e2", "human comment")])
    run_coro(freshness.disclose_unread_at_close(mgr, m))
    assert fake.comments == []


def test_sweep_disclosure_is_best_effort(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [])
    run = _review_run(watermark_id="e1")
    run.state = "finished"
    mgr.runs.store.save(run)
    fake.activity_exc = RuntimeError("PMO 502")
    run_coro(freshness.disclose_unread_at_close(mgr, m))  # must not raise
    assert fake.comments == []


# ── payload plumbing ─────────────────────────────────────────────────────────

def test_activity_payload_carries_watermark_and_survives_empty_feed(tmp_path):
    m, mgr, fake = _gate_mgr(tmp_path, [])
    payload = run_coro(mgr.activity_payload("p1", "issue"))
    assert payload["feed_watermark"] == {}            # empty feed: no crash
    ts = datetime.now(timezone.utc)
    fake.activity_entries = [_entry("e7", "x", ts=ts)]
    payload = run_coro(mgr.activity_payload("p1", "issue"))
    assert payload["feed_watermark"] == {"entry_id": "e7",
                                         "ts": ts.isoformat()}
