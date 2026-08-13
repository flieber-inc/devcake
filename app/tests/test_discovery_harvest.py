"""ADR-0033 harvest half — discoveries memorialized at finalize: the
structured `discoveries` result key rendered to DISCOVERY_<seq>.md (always a
step deliverable, founder ruling 2026-08-13), a marked source-feed comment,
the DEVCAKE-DISCOVERY sweep-gate label, and label-gated pending detection.
Best-effort throughout: harvest must never wedge a close."""
import asyncio

from devcake.domain.orchestrator.markers import (DISCOVERY_MARKER_RE,
                                                 discovery_marker,
                                                 discovery_posts,
                                                 discovery_receipts)
from devcake.domain.orchestrator.feed import unquoted


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


# ── the marker pair: render ↔ parse round-trip ───────────────────────────────

def test_discovery_marker_round_trips():
    line = discovery_marker(3, 2)
    assert discovery_posts(line) == [(3, 2)]
    m = DISCOVERY_MARKER_RE.search(line)
    assert m and (int(m.group(1)), int(m.group(2))) == (3, 2)


def test_discovery_posts_collects_every_marker():
    text = (f"step one:\n{discovery_marker(1, 3)}\nlater:\n"
            f"{discovery_marker(4, 1)}\n")
    assert discovery_posts(text) == [(1, 3), (4, 1)]
    assert discovery_posts("no markers here") == []


def test_quoted_discovery_marker_is_excluded_by_unquoted():
    # IRON RULE composition: markers.py takes pre-unquoted text; a `>`-quoted
    # marker never reaches the parser
    body = f"as posted earlier:\n> {discovery_marker(2, 1)}\nplain text"
    assert discovery_posts(unquoted(body)) == []
    body2 = f"{discovery_marker(2, 1)}\n> {discovery_marker(9, 9)}"
    assert discovery_posts(unquoted(body2)) == [(2, 1)]


def test_discovery_receipts_are_a_set_of_step_target_pairs():
    text = ("routed: `devcake:discovery-routed:v1 step=2 to=T-7`\n"
            "routed: `devcake:discovery-routed:v1 step=2 to=T-9`\n"
            "routed again (idempotent re-run): "
            "`devcake:discovery-routed:v1 step=2 to=T-7`")
    assert discovery_receipts(text) == {(2, "T-7"), (2, "T-9")}
    assert discovery_receipts("nothing") == set()


def test_marker_line_is_far_below_inline_max():
    from devcake.domain.orchestrator.markers import FEED_INLINE_MAX
    assert len(discovery_marker(999, 99)) < 64 < FEED_INLINE_MAX


def test_discovery_in_keys_are_a_distinct_pair_set():
    from devcake.domain.orchestrator.markers import discovery_in_keys
    text = ("`devcake:discovery-in:v1 src=T-7 step=2`\n"
            "`devcake:discovery-in:v1 src=T-7 step=2`\n"   # idempotent re-run
            "`devcake:discovery-in:v1 src=T-9 step=1`")
    assert discovery_in_keys(text) == {("T-7", 2), ("T-9", 1)}
    assert discovery_in_keys("no markers") == set()


def test_elevated_markers_carry_exactly_the_delivery_class():
    # ADR-0031's seam gains its first member (ADR-0033); the SOURCE-side
    # marker must never join — a mission's own discovery post would trip
    # its own freshness gate
    from devcake.domain.orchestrator.markers import (DISCOVERY_IN_MARKER_RE,
                                                     DISCOVERY_MARKER_RE,
                                                     ELEVATED_MARKERS)
    assert ELEVATED_MARKERS == [DISCOVERY_IN_MARKER_RE]
    assert DISCOVERY_MARKER_RE not in ELEVATED_MARKERS


# ── the harvest seam (finalize hook, full path) ──────────────────────────────

from datetime import datetime, timezone  # noqa: E402

from devcake.domain.model import ActivityEntry, LABEL_DISCOVERY  # noqa: E402
from devcake.domain.orchestrator import discovery  # noqa: E402
from devcake.domain.run import Run  # noqa: E402

from test_transitions import make_mgr, mission  # noqa: F401, E402

SENTINEL = "`devcake:v1`"

ENTRY = {"finding": "the config default for retries changed to 5",
         "evidence": "src/config.py:42; repro: pytest -k retries",
         "scope": "any mission touching config loading"}


def _entry(entry_id, body, author="felipe"):
    return ActivityEntry(ts=datetime.now(timezone.utc), author=author,
                         kind="comment", body=body, entry_id=entry_id)


def _exec_run(store=None):
    r = Run(run_id="T-1-1-EXECUTE-AAAAAA", mission_key="T-1",
            mission_pmo_id="p1", mission_type="EXECUTE",
            dev_type="senior-dev", seq=1,
            stage_label_at_dispatch="DEVCAKE-EXECUTE", state="finalizing")
    if store is not None:
        store.save(r)
    return r


def _payload(discoveries=None, outcome="executed", **result_extra):
    result = {"outcome": outcome, "summary": "s", **result_extra}
    if discoveries is not None:
        result["discoveries"] = discoveries
    return {"result": result, "transcript_md": "T",
            "token_report": {"extraction_method": "unavailable", "model": "m"}}


def _harvest_mgr(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    return m, mgr, fake, store


def test_execute_finalize_posts_marked_discovery_comment(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload([ENTRY, dict(ENTRY)])))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert len(disc) == 1
    # marker line FIRST and unquoted — the scan surface
    assert unquoted(disc[0]).splitlines()[0] == discovery_marker(1, 2)
    # the close proceeded: transition applied, run finished
    assert {"DEVCAKE-REVIEW"} in [add for _, add in fake.swaps]
    assert run.state == "finished"


def test_discovery_md_attached_as_step_deliverable(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run_coro(mgr.finalize(_exec_run(store), _payload([ENTRY])))
    uploads = [u for u in fake.uploads if u[0] == "DISCOVERY_1.md"]
    assert len(uploads) == 1
    body = uploads[0][1].decode()
    assert ENTRY["finding"] in body and ENTRY["evidence"] in body \
        and ENTRY["scope"] in body
    assert any("DISCOVERY_1.md" in c and "https://fake/DISCOVERY_1.md" in c
               for c in fake.comments)


def test_cap_applies_and_zero_is_unlimited(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    five = [dict(ENTRY, finding=f"finding {i}") for i in range(5)]
    run_coro(mgr.finalize(_exec_run(store), _payload(five)))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert discovery_posts(unquoted(disc[0])) == [(1, 3)]   # default cap 3
    uploaded = next(u[1].decode() for u in fake.uploads if u[0] == "DISCOVERY_1.md")
    assert "finding 3" not in uploaded and "finding 4" not in uploaded
    assert "finding 0" in uploaded and "finding 2" in uploaded
    m2, mgr2, fake2, store2 = _harvest_mgr(tmp_path / "b")
    mgr2.config.budgets.discoveries_per_run = 0             # 0 = unlimited
    run_coro(mgr2.finalize(_exec_run(store2), _payload(five)))
    disc2 = [c for c in fake2.comments if "devcake:discovery:v1" in c]
    assert discovery_posts(unquoted(disc2[0])) == [(1, 5)]


def test_invalid_entries_dropped_silently(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    bad = ["not a dict", {"finding": "x", "scope": "y"},          # no evidence
           {"finding": "", "evidence": "e", "scope": "s"},        # empty field
           {"finding": 7, "evidence": "e", "scope": "s"}]         # non-string
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload(bad)))
    assert not any("devcake:discovery" in c for c in fake.comments)
    assert not any(u[0].startswith("DISCOVERY") for u in fake.uploads)
    assert LABEL_DISCOVERY not in m.labels
    assert "discovery:post" not in run.finalized_steps
    assert run.state == "finished"                          # close unharmed


def test_mixed_list_keeps_only_valid_entries(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run_coro(mgr.finalize(_exec_run(store), _payload(
        [ENTRY, {"finding": "x", "scope": "y"}])))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert discovery_posts(unquoted(disc[0])) == [(1, 1)]
    uploaded = next(u[1].decode() for u in fake.uploads if u[0] == "DISCOVERY_1.md")
    assert ENTRY["finding"] in uploaded
    assert uploaded.count("## ") == 1


def test_missing_key_changes_nothing(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload(None)))
    assert not any("discovery" in c.lower() for c in fake.comments)
    assert LABEL_DISCOVERY not in m.labels
    assert mgr._discoveries_pending == set()
    # non-list discoveries degrades identically
    m2, mgr2, fake2, store2 = _harvest_mgr(tmp_path / "b")
    run_coro(mgr2.finalize(_exec_run(store2),
                           _payload(outcome="executed",
                                    discoveries={"not": "a list"})))
    assert not any("devcake:discovery" in c for c in fake2.comments)


def test_entry_text_is_defanged_and_quarantined(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    sneaky = dict(ENTRY, finding=("quoting `devcake:handoff:v1` and "
                                  "`devcake:discovery:v1 step=9 n=9` here"))
    run_coro(mgr.finalize(_exec_run(store), _payload([sneaky])))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c][0]
    # the ONE live marker is the app's own line — the quoted text lost its
    # backticks (defang) AND sits on `>`-quoted lines (quarantine)
    assert discovery_posts(unquoted(disc)) == [(1, 1)]
    assert "devcake:handoff:v1" in disc            # text kept, teeth pulled
    assert "`devcake:handoff:v1`" not in disc
    uploaded = next(u[1].decode() for u in fake.uploads if u[0] == "DISCOVERY_1.md")
    assert "`devcake:handoff:v1`" not in uploaded
    assert "devcake:handoff:v1" in uploaded


def test_label_added_and_queue_seeded(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run_coro(mgr.finalize(_exec_run(store), _payload([ENTRY])))
    assert (set(), {LABEL_DISCOVERY}) in fake.swaps
    assert LABEL_DISCOVERY in m.labels
    assert mgr._discoveries_pending == {"p1"}


def test_upload_failure_falls_back_inline_and_close_proceeds(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)

    async def _boom(pmo_id, filename, data):
        raise RuntimeError("attachment API down")
    fake.upload_attachment = _boom
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert len(disc) == 1 and "full record inline" in disc[0]
    assert ENTRY["evidence"] in disc[0]            # full fidelity inline
    assert run.state == "finished"
    assert LABEL_DISCOVERY in m.labels


def test_post_failure_is_audited_and_never_wedges(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    orig = fake.post_feed
    audits = []
    mgr._audit = lambda *a, **k: audits.append(a)

    async def _refuse(ref, markdown):
        if "devcake:discovery:v1" in markdown:
            raise RuntimeError("PMO down")
        await orig(ref, markdown)
    fake.post_feed = _refuse
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    assert run.state == "finished"                 # the close proceeded
    assert "discovery:post" not in run.finalized_steps
    assert LABEL_DISCOVERY not in m.labels
    assert (set(), {LABEL_DISCOVERY}) not in fake.swaps
    assert any(len(a) > 1 and a[1] == "discovery_post_failed" for a in audits)
    assert not any(len(a) > 1 and a[1] == "discovery_post" for a in audits)
    # redelivery retries the post
    fake.post_feed = orig
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    assert any("devcake:discovery:v1" in c for c in fake.comments)
    assert "discovery:post" in run.finalized_steps
    assert LABEL_DISCOVERY in m.labels


def test_redelivery_posts_once(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    run_coro(mgr.finalize(run, _payload([ENTRY])))
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert len(disc) == 1
    assert len([u for u in fake.uploads if u[0] == "DISCOVERY_1.md"]) == 1


def test_failed_runs_never_harvest(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    run = _exec_run(store)
    payload = {"result": {"discoveries": [ENTRY]}, "transcript_md": "T",
               "exit_code": 11,
               "token_report": {"extraction_method": "unavailable"}}
    run_coro(mgr.finalize(run, payload))
    assert run.state == "failed"
    assert not any("devcake:discovery" in c for c in fake.comments)
    assert "discovery:post" not in run.finalized_steps


def test_plan_project_and_steward_shapes_never_harvest(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    plan = Run(run_id="T-1-1-PLAN-AAAAAA", mission_key="T-1",
               mission_pmo_id="p1", mission_type="PLAN",
               dev_type="senior-dev", seq=1,
               stage_label_at_dispatch="DEVCAKE-PLAN")
    assert run_coro(discovery.harvest(
        mgr, plan, {"discoveries": [ENTRY]})) == 0
    proj = _exec_run()
    proj.pmo_kind = "project"
    assert run_coro(discovery.harvest(
        mgr, proj, {"discoveries": [ENTRY]})) == 0
    steward = Run(run_id="X-TEAM-1-STEWARD-AAAAAA", mission_key="TEAM",
                  mission_pmo_id="", mission_type="STEWARD",
                  dev_type="steward", seq=1, stage_label_at_dispatch=None)
    assert run_coro(discovery.harvest(
        mgr, steward, {"discoveries": [ENTRY]})) == 0
    assert fake.comments == [] and fake.uploads == []


def test_onboard_and_review_harvest_too(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    for i, mtype in enumerate(("ONBOARD", "REVIEW"), 1):
        r = Run(run_id=f"T-1-{i}-{mtype}-AAAAAA", mission_key="T-1",
                mission_pmo_id="p1", mission_type=mtype,
                dev_type="senior-dev", seq=i, stage_label_at_dispatch=None)
        store.save(r)
        assert run_coro(discovery.harvest(
            mgr, r, {"discoveries": [ENTRY]})) == 1
    disc = [c for c in fake.comments if "devcake:discovery:v1" in c]
    assert len(disc) == 2


def test_pending_from_board_is_label_gated(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    out = run_coro(discovery.pending_from_board(mgr, [m]))
    assert out == {}
    assert getattr(fake, "get_activity_calls", 0) == 0     # ZERO feed reads
    m.labels = m.labels | {LABEL_DISCOVERY}
    fake.activity_entries = [
        _entry("e1", discovery_marker(1, 2) + "\n\n2 findings\n\n" + SENTINEL,
               author="devcake"),
        _entry("e2", "quoting:\n> " + discovery_marker(9, 9)),  # never counts
    ]
    out = run_coro(discovery.pending_from_board(mgr, [m]))
    assert out == {"p1": [(1, 2)]}
    assert fake.get_activity_calls == 1
    assert getattr(fake, "get_activity_full_calls", 0) == 1


def test_truncated_source_is_unreadable_not_empty(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    m.labels = m.labels | {LABEL_DISCOVERY}
    fake.activity_entries = [
        _entry("e1", discovery_marker(1, 2) + "\n\n" + SENTINEL,
               author="devcake"),
    ]
    fake.activity_truncated = True
    state = run_coro(discovery.scan_source(mgr, m))
    assert state.truncated
    assert run_coro(discovery.pending_from_board(mgr, [m])) == {}
    run_coro(discovery.discovery_sweep(mgr, m))
    assert ({LABEL_DISCOVERY}, set()) not in fake.swaps
    assert not any("to=-" in c for c in fake.comments)


def test_pending_excludes_receipted_steps(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    m.labels = m.labels | {LABEL_DISCOVERY}
    fake.activity_entries = [
        _entry("e1", discovery_marker(1, 2) + "\n\n" + SENTINEL,
               author="devcake"),
        _entry("e2", "routed: `devcake:discovery-routed:v1 step=1 to=T-9`"
               + "\n\n" + SENTINEL, author="devcake"),
    ]
    out = run_coro(discovery.pending_from_board(mgr, [m]))
    assert out == {}                                # posted − receipted = ∅


def test_dev_bad_output_at_transition_still_memorializes(tmp_path):
    # harvest sits BEFORE the transition (Decision 11: memorialization is
    # unconditional) — a structurally invalid decomposition still fails the
    # run, but its receipts stay on the board. Deliberate pin.
    m = mission("in_progress", {"DEVCAKE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = Run(run_id="T-1-1-ONBOARD-AAAAAA", mission_key="T-1",
              mission_pmo_id="p1", mission_type="ONBOARD",
              dev_type="senior-dev", seq=1, stage_label_at_dispatch=None,
              state="finalizing")
    store.save(run)
    run_coro(mgr.finalize(run, _payload(
        [ENTRY], outcome="decomposed")))            # no decomposition list
    assert run.state == "failed"
    assert "DEV_BAD_OUTPUT" in (run.error or "")
    assert any("devcake:discovery:v1" in c for c in fake.comments)


# ── MISSION.md discovery block (ADR-0033 D6, PR-2) ──────────────────────────

def test_mission_md_renders_the_advisory_discovery_block(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    fake.activity_entries = [
        _entry("e1", "`devcake:discovery-in:v1 src=T-7 step=2`\n\nlead text"
               "\n\n`devcake:v1`", author="devcake"),
        _entry("e2", "quoting:\n> `devcake:discovery-in:v1 src=T-9 step=1`"),
    ]
    md = run_coro(mgr.activity_payload("p1", "issue"))["mission_md"]
    assert "leads, not truths" in md               # the founder's register
    assert "[T-7 · step 2" in md
    assert "DISCOVERY_2.md" in md
    assert "T-9" not in md                         # quoted never counts


def test_mission_md_without_deliveries_has_no_block(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    fake.activity_entries = [_entry("e1", "plain human comment")]
    md = run_coro(mgr.activity_payload("p1", "issue"))["mission_md"]
    assert "leads, not truths" not in md


def test_mission_md_dedupes_delivery_pairs(tmp_path):
    m, mgr, fake, store = _harvest_mgr(tmp_path)
    fake.activity_entries = [
        _entry("e1", "`devcake:discovery-in:v1 src=T-7 step=2`\n\n`devcake:v1`",
               author="devcake"),
        _entry("e2", "`devcake:discovery-in:v1 src=T-7 step=2`\n\n`devcake:v1`",
               author="devcake"),
    ]
    md = run_coro(mgr.activity_payload("p1", "issue"))["mission_md"]
    assert md.count("[T-7 · step 2") == 1
