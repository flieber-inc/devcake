"""ADR-0042 PR-2: `feed._edit` — the second feed chokepoint (edit DevCake's
own entry) — and the two Run columns the status comment rests on."""
import asyncio

import pytest

from devcake.domain.model import ActivityEntry
from devcake.domain.orchestrator import feed
from devcake.domain.orchestrator.feed import is_devcake_comment
from devcake.domain.orchestrator.markers import COMMENT_SENTINEL
from devcake.domain.run import Run
from devcake.ports.pmo import PMOTransient
from devcake.security import MASK, register_runtime_secret, unregister_runtime_secret

from test_discovery_harvest import _exec_run, _payload
from test_transitions import FakeForge, make_mgr, mission, run_coro
from datetime import datetime, timezone

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def _seeded(tmp_path, body="old body\n\n" + COMMENT_SENTINEL, **fake_attrs):
    m = mission()
    mgr, fake, store = make_mgr(tmp_path, m)
    fake.activity_entries = [ActivityEntry(ts=NOW, author="cake", kind="comment",
                                           body=body, entry_id="c1")]
    for k, v in fake_attrs.items():
        setattr(fake, k, v)
    return m, mgr, fake, store


def test_edit_redacts_and_seals_exactly_once(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path)
    secret = "edit-secret-token-xyz-123"
    register_runtime_secret("test:feed-edit", secret)
    try:
        run_coro(mgr._edit(m.pmo_id, "issue", "c1",
                           f"new body {secret}\n\n{COMMENT_SENTINEL}"))
    finally:
        unregister_runtime_secret("test:feed-edit")
    (_pid, eid, body), = fake.edits
    assert eid == "c1" and secret not in body and MASK in body
    assert body.count(COMMENT_SENTINEL) == 1 and body.endswith(COMMENT_SENTINEL)
    assert is_devcake_comment(body)
    # the recorded entry was rewritten in place, id and ts unchanged
    e = fake.activity_entries[0]
    assert e.entry_id == "c1" and e.ts == NOW and e.body == body


def test_edit_of_a_project_entry_is_suppressed_and_audited(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path)
    audits = []
    mgr._audit = lambda pid, action, detail="": audits.append((pid, action))
    run_coro(mgr._edit(m.pmo_id, "project", "pu1", "status"))
    assert getattr(fake, "edits", []) == []
    assert (m.pmo_id, "project_feed_suppressed") in audits


def test_edit_never_externalizes_a_long_body(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path)
    long = "x" * 5000 + "\n`devcake:discovery-routed:v1 step=1 to=-`"
    run_coro(mgr._edit(m.pmo_id, "issue", "c1", long))
    assert fake.uploads == []
    assert "`devcake:discovery-routed:v1 step=1 to=-`" in fake.edits[0][2]


def test_edit_over_the_vendor_cap_is_refused_before_the_wire(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path, comment_max_chars=100)
    with pytest.raises(ValueError):
        run_coro(mgr._edit(m.pmo_id, "issue", "c1", "y" * 200))
    assert getattr(fake, "edits", []) == []


def test_edit_invalidates_the_memo_on_success_and_on_failure(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path, strict_edits=True)
    mgr.feed_memo.put("discovery", m, "scan", mgr.feed_memo.generation(m.pmo_id))
    run_coro(mgr._edit(m.pmo_id, "issue", "c1", "fine"))
    assert mgr.feed_memo.get("discovery", m) is None
    mgr.feed_memo.put("discovery", m, "scan", mgr.feed_memo.generation(m.pmo_id))
    with pytest.raises(RuntimeError):            # a vanished entry: permanent
        run_coro(mgr._edit(m.pmo_id, "issue", "gone", "fine"))
    assert mgr.feed_memo.get("discovery", m) is None


def test_edit_propagates_a_transient(tmp_path):
    m, mgr, fake, _ = _seeded(tmp_path)

    async def boom(ref, entry_id, markdown):
        raise PMOTransient("rate limited", retry_after=5)
    fake.edit_feed = boom
    with pytest.raises(PMOTransient):
        run_coro(mgr._edit(m.pmo_id, "issue", "c1", "fine"))


# ── Run.pr_url ───────────────────────────────────────────────────────────────

def test_execute_finalize_records_the_reported_pr_url_once(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-EXECUTE"})
    mgr, fake, store = make_mgr(tmp_path, m)
    run = _exec_run(store)
    run_coro(mgr.finalize(run, _payload(pr_url="https://forge.example/pr/8")))
    assert store.get(run.run_id).pr_url == "https://forge.example/pr/8"
    # a redelivery with a different reported url never overwrites the record
    again = store.get(run.run_id)
    run_coro(mgr.finalize(again, _payload(pr_url="https://forge.example/pr/9")))
    assert store.get(run.run_id).pr_url == "https://forge.example/pr/8"


def test_review_finalize_prefers_the_forge_verified_pr_url(tmp_path):
    from devcake.domain.orchestrator import review
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, store = make_mgr(tmp_path, m, forge=FakeForge())
    run = Run(run_id="T-1-2-REVIEW-BBBBBB", mission_key="T-1",
              mission_pmo_id="p1", mission_type="REVIEW", dev_type="senior-dev",
              seq=2, stage_label_at_dispatch="DEVCAKE-REVIEW",
              state="finalizing", repo_ref="main",
              pr_url="https://forge.example/reported")
    store.save(run)
    run_coro(review.finalize_review(
        mgr, run, {"outcome": "reviewed", "verdict": "approve",
                   "report_md": "ok", "summary": "s"}))
    assert run.pr_url == "https://forge/pr/8"
