"""docs/03 §8 — a step's bookkeeping threads under the step. The transcript
comment (🧾) stays top level and anchors the token report (🧮) and the
discovery harvest (🔎) as replies on vendors that declare `feed_threads`.
Nothing about the feed's CONTENT changes: same bodies, markers and
sentinel, same order; a flat vendor gets byte-identical top-level posts;
the Dev's ACTIVITY.md mirror renders DevCake's own replies exactly as it
renders top-level posts. The anchor is saved on the Run with the
transcript checkpoint so a redelivered finalize threads the same way."""
from datetime import datetime, timezone

from devcake.domain.model import Activity, ActivityEntry
from devcake.domain.orchestrator import steps
from devcake.domain.orchestrator.feed import (COMMENT_SENTINEL, _feed,
                                              post_attachment_comment)
from devcake.domain.orchestrator.markers import REPLY_MARKER
from devcake.domain.run import Run

from test_discovery_harvest import ENTRY, _payload
from test_steward import MapPMO, m as steward_mission
from test_steward import make_mgr as steward_mgr
from test_transitions import make_mgr, mission, run_coro

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def _exec_run(store=None, **fields):
    r = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
            mission_pmo_id="p1", mission_type="EXECUTE",
            dev_type="senior-dev", seq=1,
            stage_label_at_dispatch="DEVCAKE-EXECUTE", state="finalizing",
            **fields)
    if store is not None:
        store.save(r)
    return r


def _mgr(tmp_path, *, threads):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.feed_threads = threads
    return m, mgr, fake, store


def _parents(fake):
    """{comment body: parent id} for every issue comment the fake took."""
    return {body: parent for body, (_cid, parent)
            in zip(fake.comments, fake.threads)}


def _find(fake, needle):
    hits = [c for c in fake.comments if needle in c]
    assert len(hits) == 1, (needle, len(hits))
    return hits[0]


# ── the chokepoint ──────────────────────────────────────────────────────────

def test_chokepoint_passes_reply_to_only_when_the_vendor_threads(tmp_path):
    _, mgr, fake, _ = _mgr(tmp_path, threads=False)
    cid = run_coro(_feed(mgr, "p1", "issue", "hello", reply_to="c-anchor"))
    assert cid == "c1"                       # the vendor's id comes back
    assert fake.threads == [("c1", None)]    # flat vendor: never nested
    assert fake.comments[-1].endswith(COMMENT_SENTINEL)

    fake.feed_threads = True
    cid = run_coro(_feed(mgr, "p1", "issue", "hello", reply_to="c-anchor"))
    assert cid == "c2"
    assert fake.threads[-1] == ("c2", "c-anchor")
    # the body is the same bytes whether nested or not
    assert fake.comments[-1] == fake.comments[-2]


def test_chokepoint_without_reply_to_posts_top_level_on_a_threading_vendor(tmp_path):
    _, mgr, fake, _ = _mgr(tmp_path, threads=True)
    run_coro(_feed(mgr, "p1", "issue", "hello"))
    assert fake.threads == [("c1", None)]


def test_chokepoint_pages_every_part_under_the_same_anchor(tmp_path):
    _, mgr, fake, _ = _mgr(tmp_path, threads=True)
    fake.comment_max_chars = 400
    first = run_coro(_feed(mgr, "p1", "issue", "x" * 900,
                           reply_to="c-anchor", externalize=False))
    assert len(fake.comments) >= 2
    assert first == fake.threads[0][0]               # part 1's id
    assert {parent for _, parent in fake.threads} == {"c-anchor"}


def test_project_feed_returns_no_id(tmp_path):
    _, mgr, fake, _ = _mgr(tmp_path, threads=True)
    assert run_coro(_feed(mgr, "p1", "project", "hello",
                          reply_to="c-anchor")) is None
    assert fake.comments == []


def test_attachment_pipe_returns_the_comment_id_and_threads(tmp_path):
    _, mgr, fake, _ = _mgr(tmp_path, threads=True)
    cid = run_coro(post_attachment_comment(
        mgr, "p1", "issue", filename="X.md", content="dump",
        comment_of=lambda url: (f"see [X.md]({url})", True),
        reply_to="c-anchor"))
    assert cid == "c1" and fake.threads == [("c1", "c-anchor")]
    assert fake.uploads[0][0] == "X.md"


# ── finalize: one thread per step ───────────────────────────────────────────

def test_finalize_threads_token_report_and_harvest_under_the_transcript(tmp_path):
    _, mgr, fake, store = _mgr(tmp_path, threads=True)
    run = _exec_run(store)
    # a last message makes finalize post the answer comment too
    run_coro(mgr.finalize(run, dict(_payload([ENTRY]),
                                    last_message_md="the answer")))
    parents = _parents(fake)
    transcript = _find(fake, "🧾 DevCake transcript")
    anchor = dict(zip(fake.comments, (cid for cid, _ in fake.threads)))[transcript]
    assert parents[transcript] is None                       # top level
    assert parents[_find(fake, "🧮 DevCake token report")] == anchor
    assert parents[_find(fake, "devcake:discovery:v1")] == anchor
    assert parents[_find(fake, REPLY_MARKER)] is None        # the answer stays visible
    # persisted with the transcript checkpoint
    assert store.get(run.run_id).feed_anchor == anchor
    # order of the feed is unchanged: transcript, answer, token report, harvest
    order = [next(k for k in ("🧾", REPLY_MARKER, "🧮", "devcake:discovery:v1")
                  if k in c) for c in fake.comments
             if any(k in c for k in ("🧾", REPLY_MARKER, "🧮", "devcake:discovery:v1"))]
    assert order == ["🧾", REPLY_MARKER, "🧮", "devcake:discovery:v1"]


def test_flat_vendor_gets_byte_identical_bodies_top_level(tmp_path):
    _, mgr_t, fake_t, store_t = _mgr(tmp_path / "t", threads=True)
    _, mgr_f, fake_f, store_f = _mgr(tmp_path / "f", threads=False)
    run_coro(mgr_t.finalize(_exec_run(store_t), _payload([ENTRY])))
    run_coro(mgr_f.finalize(_exec_run(store_f), _payload([ENTRY])))
    assert fake_f.comments == fake_t.comments          # same feed material
    assert {parent for _, parent in fake_f.threads} == {None}
    assert store_f.get("T-1-1-EXECUTE-AAAAAA").feed_anchor == "c1"  # kept, unused


def test_redelivered_finalize_threads_under_the_saved_anchor(tmp_path):
    # the process died after the transcript checkpoint: the redelivery must
    # not re-post the transcript, and must still nest the report under it
    _, mgr, fake, store = _mgr(tmp_path, threads=True)
    run = _exec_run(store, finalized_steps=[steps.TRANSCRIPT],
                    feed_anchor="c-earlier")
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    assert not any("🧾 DevCake transcript" in c for c in fake.comments)
    parents = _parents(fake)
    assert parents[_find(fake, "🧮 DevCake token report")] == "c-earlier"
    assert parents[_find(fake, "devcake:discovery:v1")] == "c-earlier"


def test_vendor_returning_no_id_leaves_the_step_flat(tmp_path):
    _, mgr, fake, store = _mgr(tmp_path, threads=True)

    async def no_id(ref, markdown, *, reply_to=None):
        fake.comments.append(markdown)
        fake.threads = getattr(fake, "threads", [])
        fake.threads.append((None, reply_to))
        return None
    fake.post_feed = no_id
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    assert store.get(run.run_id).feed_anchor == ""
    assert {parent for _, parent in fake.threads} == {None}


# ── the mirror the Dev reads never shows DevCake's own nesting ──────────────

def _entries(nest):
    devcake = COMMENT_SENTINEL
    top = ActivityEntry(ts=NOW, author="bot", kind="comment", entry_id="c1",
                        body=f"🧾 DevCake transcript `1_EXECUTE.md`\n\n{devcake}")
    report = ActivityEntry(ts=NOW.replace(minute=1), author="bot", kind="comment",
                           entry_id="c2", parent_id="c1" if nest else None,
                           body=f"🧮 DevCake token report — step 1\n\n{devcake}")
    human = ActivityEntry(ts=NOW.replace(minute=2), author="felipe",
                          kind="comment", entry_id="c3",
                          parent_id="c1" if nest else None, body="thanks")
    return [top, report, human]


def _mirror(tmp_path, entries):
    mission = steward_mission("i1", "T-1")
    pmo = MapPMO([], activity=Activity(mission=mission, entries=entries))
    mgr = steward_mgr(tmp_path, pmo)
    return run_coro(mgr.activity_payload("i1"))["activity_md"]


def test_mirror_hides_devcake_reply_nesting_but_keeps_human_replies(tmp_path):
    md = _mirror(tmp_path, _entries(nest=True))
    assert md.count("↳ reply to") == 1               # only the human's
    assert "↳ reply to bot @" in md
    assert "🧮 DevCake token report" in md          # body still mirrored


def test_mirror_is_identical_for_a_nested_and_a_flat_devcake_report(tmp_path):
    nested = _entries(nest=True)
    nested[2].parent_id = None                       # isolate DevCake's reply
    flat = _entries(nest=False)
    assert _mirror(tmp_path / "a", nested) == _mirror(tmp_path / "b", flat)
