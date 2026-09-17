"""Ticket delivery (ADR-0017 addendum): the pull request's files attached to
the ticket at REVIEW approve, Done through the completion chokepoint, the
pull request closed unmerged; the merge sweep honouring a late `to=ticket`
on a parked pull request."""
from __future__ import annotations

import asyncio
import zipfile
import io
from datetime import datetime, timezone

import pytest

from devcake.domain.model import ActivityEntry
from devcake.domain.orchestrator import completion, deliver, review, sweeps
from devcake.domain.orchestrator.markers import DELIVERABLE_TOKEN, DELIVERY_TICKET
from devcake.ports.forge import ForgeError, PRFile, PRFilesResult, PullRequest
from test_transitions import FakeForge, make_mgr, mission, _run


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


class FilesForge(FakeForge):
    """A forge whose pull request changed a small set of files."""

    def __init__(self, files=None, truncated=False, **kw):
        super().__init__(**kw)
        self.files = files if files is not None else {
            "reports/usage-report.md": b"# Usage\n\nall unknown\n",
            "reports/data.csv": b"field,hits\nsales,0\n",
        }
        self.truncated = truncated
        self.fetched = []

    async def pr_files(self, n):
        return PRFilesResult(
            files=[PRFile(path=p, status="added") for p in self.files],
            truncated=self.truncated)

    async def file_content(self, path, ref):
        self.fetched.append((path, ref))
        return self.files[path]


def _ticket_run():
    run = _run("REVIEW", "DEVCAKE-REVIEW")
    run.delivery_to = DELIVERY_TICKET
    return run


APPROVE = {"verdict": "approve", "report_md": "ok — files only",
           "pr_url": "https://forge/pr/8", "handoff_md": "delivered a report"}


# ── names ────────────────────────────────────────────────────────────────────

def test_ticket_attachment_names_use_basenames_and_guard_reserved_shapes():
    names = deliver.ticket_attachment_names(
        ["docs/report.md", "out/2_EXECUTE.md", "a/PLAN_3.md", "b/report.md",
         "x/archive.zip", "MISSION.md"], "T-1")
    assert names["docs/report.md"] == "report.md"
    assert names["out/2_EXECUTE.md"] == "T-1-2_EXECUTE.md"    # a step transcript's shape
    assert names["a/PLAN_3.md"] == "T-1-PLAN_3.md"
    assert names["b/report.md"] == "T-1-report.md"            # basename collision
    assert names["x/archive.zip"] == "T-1-archive.zip"        # the folder extracts zips
    assert names["MISSION.md"] == "T-1-MISSION.md"


# ── deliver_change_set ───────────────────────────────────────────────────────

def test_small_change_set_is_attached_file_by_file(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()
    mgr, fake, _s = make_mgr(tmp_path, m, forge=forge)
    pr = PullRequest(number=8, url="https://forge/pr/8", state="open")
    names = run_coro(deliver.deliver_change_set(
        mgr, repo_ref="main", mission_key="T-1", pmo_id="p1", pmo_kind="issue",
        pr=pr, ref="devcake/LINEAR-T-1"))
    assert names == ["usage-report.md", "data.csv"]
    assert [n for n, _ in fake.uploads] == ["usage-report.md", "data.csv"]
    assert all(ref == "devcake/LINEAR-T-1" for _, ref in forge.fetched)
    [note] = fake.comments
    assert DELIVERABLE_TOKEN in note and "reports/usage-report.md" in note
    assert "closed without merging; no repository changed" in note
    assert "https://fake/usage-report.md" in note


def test_text_files_are_redacted_and_binaries_pass_through(tmp_path):
    secret = "ghp_" + "B" * 36
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge(files={"notes.md": f"token {secret}\n".encode(),
                              "logo.png": b"\x89PNG\r\n\x1a\n\xff\xfe"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=forge)
    run_coro(deliver.deliver_change_set(
        mgr, repo_ref="main", mission_key="T-1", pmo_id="p1", pmo_kind="issue",
        pr=PullRequest(number=8, url="https://forge/pr/8", state="open"),
        ref="devcake/LINEAR-T-1"))
    data = dict(fake.uploads)
    assert secret.encode() not in data["notes.md"]
    assert data["logo.png"] == b"\x89PNG\r\n\x1a\n\xff\xfe"


def test_large_change_set_falls_back_to_the_archive(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge(files={f"f{i}.md": b"x" for i in range(deliver.TICKET_MAX_FILES + 1)})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=forge)
    names = run_coro(deliver.deliver_change_set(
        mgr, repo_ref="main", mission_key="T-1", pmo_id="p1", pmo_kind="issue",
        pr=PullRequest(number=8, url="https://forge/pr/8", state="open"),
        ref="devcake/LINEAR-T-1"))
    assert names == ["T-1-deliverable.zip"]
    [(name, blob)] = fake.uploads
    assert name == "T-1-deliverable.zip"
    assert len(zipfile.ZipFile(io.BytesIO(blob)).namelist()) == deliver.TICKET_MAX_FILES + 1
    assert "T-1-deliverable.zip" in fake.comments[0]


def test_delivery_failure_raises_instead_of_degrading(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=FilesForge())

    async def _boom(pmo_id, filename, data):
        raise RuntimeError("upload refused")
    fake.upload_attachment = _boom
    with pytest.raises(RuntimeError):
        run_coro(deliver.deliver_change_set(
            mgr, repo_ref="main", mission_key="T-1", pmo_id="p1", pmo_kind="issue",
            pr=PullRequest(number=8, url="https://forge/pr/8", state="open"),
            ref="devcake/LINEAR-T-1"))
    assert fake.comments == []                    # no note claims a delivery


def test_empty_change_set_is_a_failure(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    mgr, fake, _s = make_mgr(tmp_path, m, forge=FilesForge(files={}))
    with pytest.raises(RuntimeError):
        run_coro(deliver.deliver_change_set(
            mgr, repo_ref="main", mission_key="T-1", pmo_id="p1", pmo_kind="issue",
            pr=PullRequest(number=8, url="https://forge/pr/8", state="open"),
            ref="devcake/LINEAR-T-1"))


# ── REVIEW approve in ticket mode ────────────────────────────────────────────

def test_approve_in_ticket_mode_delivers_completes_and_closes(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _ticket_run()
    store.save(run)
    run_coro(review.finalize_review(mgr, run, APPROVE))
    assert [n for n, _ in fake.uploads] == ["usage-report.md", "data.csv"]
    assert m.status == "done"
    assert "DEVCAKE-REVIEW" not in m.labels and "DEVCAKE-MERGE" not in m.labels
    assert forge.closed == [8] and forge.merges == []
    assert "`devcake:handoff:v1`" in m.description       # the closing handoff still lands
    assert forge.pr_comments and "footer" not in forge.pr_comments[0]   # no merge invitation
    done = [c for c in fake.comments if "Mission done." in c]
    assert len(done) == 1 and "no repository changed" in done[0]
    assert "closed without merging" in done[0]
    assert "review:ticket_delivery" in run.finalized_steps
    assert "review:done" in run.finalized_steps
    assert "review:pr_closed" in run.finalized_steps
    # redelivery: nothing twice
    before = (len(fake.uploads), len(fake.comments), len(forge.closed))
    run_coro(review.finalize_review(mgr, run, APPROVE))
    assert (len(fake.uploads), len(fake.comments), len(forge.closed)) == before


def test_approve_in_ticket_mode_never_completes_when_delivery_fails(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)

    async def _boom(pmo_id, filename, data):
        raise RuntimeError("upload refused")
    fake.upload_attachment = _boom
    run = _ticket_run()
    store.save(run)
    with pytest.raises(RuntimeError):
        run_coro(review.finalize_review(mgr, run, APPROVE))
    assert m.status != "done" and "DEVCAKE-REVIEW" in m.labels
    assert forge.closed == []
    assert "review:ticket_delivery" not in run.finalized_steps   # redelivery retries it


def test_approve_in_ticket_mode_close_failure_is_told_not_fatal(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()
    forge.close_exc = ForgeError("forge down", status=503)
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _ticket_run()
    store.save(run)
    run_coro(review.finalize_review(mgr, run, APPROVE))
    assert m.status == "done"
    assert any("could not be closed" in c and "do not merge it" in c
               for c in fake.comments)
    assert "review:pr_closed" in run.finalized_steps          # never retried into a fatal


def test_approve_in_ticket_mode_with_no_pr_takes_the_missing_pr_path(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()

    async def _none(_branch):
        return None
    forge.get_pr_by_branch = _none
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _ticket_run()
    store.save(run)
    run_coro(review.finalize_review(mgr, run, APPROVE))
    assert m.status != "done" and "DEVCAKE-MERGE" in m.labels
    assert fake.uploads == []


def test_repository_mode_approve_is_unchanged(tmp_path):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-REVIEW"})
    forge = FilesForge()
    mgr, fake, store = make_mgr(tmp_path, m, forge=forge)
    run = _run("REVIEW", "DEVCAKE-REVIEW")           # delivery_to "" ⇒ repository
    store.save(run)
    run_coro(review.finalize_review(mgr, run, APPROVE))
    assert fake.uploads == [] and forge.closed == []
    assert "DEVCAKE-MERGE" in m.labels                # manual repo: awaiting merge


# ── the merge sweep honours a late `to=ticket` ──────────────────────────────

def _parked(tmp_path, *, marker=True, forge=None):
    m = mission("in_progress", {"DEVCAKE", "DEVCAKE-MERGE"})
    if marker:
        m.description = "brief\n\n---\n`devcake:delivery:v1 to=ticket`\nReason: report\n"
    forge = forge or FilesForge()
    mgr, fake, _s = make_mgr(tmp_path, m, forge=forge)
    mgr.forges.instance("main").auto_merge = False
    fake.activity_entries = [ActivityEntry(
        ts=datetime.now(timezone.utc), author="devcake", kind="comment",
        body="✅ approved `devcake:v1`")]
    return m, mgr, fake, forge


def test_sweep_delivers_a_parked_pr_when_the_record_says_ticket(tmp_path):
    m, mgr, fake, forge = _parked(tmp_path)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert [n for n, _ in fake.uploads] == ["usage-report.md", "data.csv"]
    assert m.status == "done" and "DEVCAKE-MERGE" not in m.labels
    assert forge.closed == [8] and forge.merges == []
    assert any("on your instruction" in c and "Mission done." in c for c in fake.comments)


def test_sweep_leaves_a_parked_pr_alone_without_the_marker(tmp_path):
    m, mgr, fake, forge = _parked(tmp_path, marker=False)
    run_coro(sweeps.merge_sweep(mgr, m))
    assert fake.uploads == [] and forge.closed == [] and m.status != "done"
    assert "p1" in mgr.merge_handoffs


def test_sweep_ticket_delivery_failure_hands_back_once(tmp_path):
    m, mgr, fake, forge = _parked(tmp_path)

    async def _boom(pmo_id, filename, data):
        raise RuntimeError("upload refused")
    fake.upload_attachment = _boom
    run_coro(sweeps.merge_sweep(mgr, m))
    assert m.status != "done" and "DEVCAKE-MERGE" in m.labels
    handoffs = [c for c in fake.comments if "`devcake:merge-handoff`" in c]
    assert len(handoffs) == 1 and "could not attach them" in handoffs[0]
    assert forge.closed == []
    run_coro(sweeps.merge_sweep(mgr, m))                 # latched: silence
    assert len([c for c in fake.comments if "`devcake:merge-handoff`" in c]) == 1


# ── the completion chokepoint's new rows ─────────────────────────────────────

@pytest.mark.parametrize("cause,label", [
    (completion.CompletionCause.REVIEW_DELIVERED_TO_TICKET, "DEVCAKE-REVIEW"),
    (completion.CompletionCause.SWEEP_DELIVERED_TO_TICKET, "DEVCAKE-MERGE"),
])
def test_ticket_causes_skip_invalidation_and_the_archive(tmp_path, monkeypatch, cause, label):
    m = mission("in_progress", {"DEVCAKE", label})
    mgr, fake, store = make_mgr(tmp_path, m)
    invalidated, zipped, disclosed = [], [], []
    monkeypatch.setattr(mgr.repo_cache, "invalidate", lambda repo: invalidated.append(repo))

    async def fake_zip(*a, **kw):
        zipped.append(a)
    monkeypatch.setattr(mgr, "deliver_internal_zip", fake_zip)
    monkeypatch.setattr(mgr, "deliver_internal_zip_for_mission", fake_zip)

    async def fake_disclose(mgr_, mission_):
        disclosed.append(mission_.pmo_id)
    monkeypatch.setattr(completion.freshness, "disclose_unread_at_close", fake_disclose)
    pr = PullRequest(number=8, url="https://forge/pr/8", state="open")
    run = _ticket_run() if label == "DEVCAKE-REVIEW" else None
    if run:
        store.save(run)
    run_coro(completion.complete_mission(
        mgr, cause, ref=m.ref, mission_key="T-1", pr=pr, pr_url=pr.url,
        run=run, mission=None if run else m))
    assert m.status == "done" and label not in m.labels
    assert invalidated == [] and zipped == []            # no repository changed
    [body] = fake.comments
    assert "no repository changed" in body and "merged" not in body.split("without merging")[0].lower().replace("unmerged", "")
    assert disclosed == (["p1"] if cause is completion.CompletionCause.SWEEP_DELIVERED_TO_TICKET else [])
