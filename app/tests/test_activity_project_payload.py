"""Project-fidelity fix: activity_payload for PROJECT-kind missions — full
activity fetch (kind flows through), documents materialized under docs/…,
provenance markers on the update mirror, honest empty-feed line."""

from __future__ import annotations

import base64
from datetime import datetime, timezone

from devcake.domain.model import Activity, ActivityEntry, AttachmentRef, Mission, MissionDocument
from devcake.domain.orchestrator.markers import COMMENT_SENTINEL
from test_steward import MapPMO, make_mgr, run_coro

NOW = datetime.now(timezone.utc)


def proj(pmo_id="p1", key="PRJ-x"):
    return Mission(pmo_id=pmo_id, pmo_kind="project", key=key, title="Proj",
                   status="backlog", labels={"DEVCAKE"}, updated_at=NOW,
                   description="the brief")


class KindPMO(MapPMO):
    """MapPMO that records the ref kind get_activity was called with."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.activity_refs = []

    async def get_activity(self, ref, full=False):
        self.activity_refs.append((ref.kind, full))
        return self.activity


def test_project_payload_mirrors_feed_with_provenance(tmp_path):
    entries = [
        ActivityEntry(ts=NOW, author="DevCake (project update)", kind="comment",
                      body="baton text\n\n" + COMMENT_SENTINEL, entry_id="u1"),
        ActivityEntry(ts=NOW, author="alice (project update)", kind="comment",
                      body="human update", entry_id="u2"),
        ActivityEntry(ts=NOW, author="bob", kind="comment", body="a comment",
                      entry_id="c1", parent_id="u2"),
    ]
    docs = [MissionDocument(title="Spec", content="# spec body",
                            url="https://linear/d1")]
    pmo = KindPMO([], activity=Activity(mission=proj(), entries=entries,
                                        documents=docs))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p1", "project"))

    # the project branch now rides the FULL fetch with the project kind
    assert pmo.activity_refs == [("project", True)]
    # document materialized under docs/…, bytes round-trip
    by_name = {a["filename"]: base64.b64decode(a["content_b64"])
               for a in payload["attachments"]}
    assert by_name["docs/Spec.md"] == b"# spec body"
    assert "## Project documents" in payload["mission_md"]
    assert "[document: docs/Spec.md]" in payload["mission_md"]
    # provenance: sentinel update is DevCake's, the rest are human (the
    # header explainer also carries the literal glyphs — count entry headers)
    md = payload["activity_md"]
    assert md.count("— 🤖 DevCake (comment)") == 1
    assert md.count("— 🧑 HUMAN (comment)") == 2
    assert "↳ reply to alice (project update)" in md
    # the pre-fix stub is gone
    assert "projects carry no comment feed" not in md


def test_project_payload_doc_dir_reserves_the_name(tmp_path):
    """Documents land first, so a later flat feed attachment literally named
    `docs` suffixes to docs-2 instead of a file-vs-dir tree conflict; twin
    doc titles get the -2 suffix inside docs/."""
    entries = [ActivityEntry(
        ts=NOW, author="bob", kind="comment", body="see file",
        entry_id="c1",
        attachments=[AttachmentRef(url="https://uploads.linear.app/x",
                                   name="docs")])]
    docs = [MissionDocument(title="Spec", content="one"),
            MissionDocument(title="Spec", content="two")]
    pmo = KindPMO([], activity=Activity(mission=proj(), entries=entries,
                                        documents=docs))

    async def download_asset(url):
        return b"bytes"
    pmo.download_asset = download_asset
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p1", "project"))
    names = [a["filename"] for a in payload["attachments"]]
    assert names == ["docs/Spec.md", "docs/Spec-2.md", "docs-2"]


def test_project_payload_oversize_document_skipped_honestly(tmp_path):
    docs = [MissionDocument(title="Huge", content="x" * 100,
                            url="https://linear/d9")]
    pmo = KindPMO([], activity=Activity(mission=proj(), entries=[],
                                        documents=docs))
    mgr = make_mgr(tmp_path, pmo)
    mgr._attachment_cap = lambda: 10
    payload = run_coro(mgr.activity_payload("p1", "project"))
    assert payload["attachments"] == []
    assert "[document too large to mirror: Huge](https://linear/d9)" \
        in payload["mission_md"]


def test_project_payload_empty_feed_is_self_explanatory(tmp_path):
    pmo = KindPMO([], activity=Activity(mission=proj(), entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("p1", "project"))
    assert "(no project updates yet" in payload["activity_md"]


def test_issue_payload_keeps_its_shape(tmp_path):
    """Issue-path parity: default kind stays `issue`, and the project-only
    empty-feed line never leaks into an issue mirror."""
    from test_steward import m as mission_m
    pmo = KindPMO([], activity=Activity(mission=mission_m("i1", "T-1"),
                                        entries=[]))
    mgr = make_mgr(tmp_path, pmo)
    payload = run_coro(mgr.activity_payload("i1"))
    assert pmo.activity_refs == [("issue", True)]
    assert "(no project updates yet" not in payload["activity_md"]
    assert "## Project documents" not in payload["mission_md"]
