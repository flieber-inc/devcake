"""docs/05 §3: get_activity is cursor-paginated like every other list read
(a single page was verified LOSSY at 108 comments on DEV-50, 2026-07-12).
Ordering is pinned with orderBy: createdAt (verified live: newest-first); the
10-page/1,000-comment safety ceiling logs a truncation WARNING, never silent."""
import asyncio

from devcake.adapters.linear.adapter import MAX_COMMENT_PAGES, LinearAdapter
from devcake.domain.model import MissionRef


def run_coro(c):
    return asyncio.get_event_loop().run_until_complete(c)


def _header():
    return {"id": "i1", "identifier": "T-1", "title": "t", "description": "",
            "url": "https://linear/t-1", "updatedAt": "2026-07-12T10:00:00.000Z",
            "priority": 2, "state": {"name": "In Progress", "type": "started"},
            "labels": {"nodes": []}, "project": None}


def _comment(i):
    return {"body": f"c{i}", "user": {"name": "u"},
            "createdAt": f"2026-07-{(i // 1440) + 1:02d}"
                         f"T{(i // 60) % 24:02d}:{i % 60:02d}:00.000Z"}


def _paged_adapter(total, queries):
    """Serve `total` comments newest-first in 100-comment cursor pages."""
    ad = LinearAdapter("key")
    order = sorted(range(total), reverse=True)      # newest (highest i) first

    async def _gql(query, variables=None):
        queries.append((query, dict(variables or {})))
        after = (variables or {}).get("after")
        start = int(after) if after else 0
        page = order[start:start + 100]
        issue = _header() if not after else {}
        issue["comments"] = {
            "pageInfo": {"hasNextPage": start + 100 < total,
                         "endCursor": str(start + 100)},
            "nodes": [_comment(i) for i in page]}
        return {"issue": issue}
    ad._gql = _gql
    return ad


def test_get_activity_walks_all_pages_chronologically():
    queries = []
    act = run_coro(_paged_adapter(108, queries).get_activity(MissionRef("i1", "issue")))  # DEV-50 shape
    assert len(act.entries) == 108
    assert len(queries) == 2                        # 100 + 8, exactly two reads
    assert "orderBy: createdAt" in queries[0][0]    # pinned ordering
    assert queries[1][1]["after"] == "100"          # cursor threaded through
    ts = [e.ts for e in act.entries]
    assert ts == sorted(ts)                         # chronological index
    assert act.entries[0].body == "c0"              # oldest survived the walk


def test_get_activity_single_page_needs_one_read():
    queries = []
    act = run_coro(_paged_adapter(99, queries).get_activity(MissionRef("i1", "issue")))
    assert len(act.entries) == 99 and len(queries) == 1


def _full_adapter(queries=None, issue_extra=None, comments=()):
    """One-page adapter with custom issue fields for full-mode tests."""
    ad = LinearAdapter("key")

    async def _gql(query, variables=None):
        if queries is not None:
            queries.append((query, dict(variables or {})))
        issue = {**_header(), **(issue_extra or {})}
        issue["comments"] = {"pageInfo": {"hasNextPage": False,
                                          "endCursor": "0"},
                             "nodes": list(comments)}
        return {"issue": issue}
    ad._gql = _gql
    return ad


def test_get_activity_shallow_query_unchanged():
    # ADR-0014 cost pin: the marker-scan call paths stay on the cheap query —
    # no reply ids, no attachments connection, unchanged pagination window
    queries = []
    run_coro(_paged_adapter(5, queries).get_activity(MissionRef("i1", "issue")))
    assert "parent" not in queries[0][0]
    assert "attachments" not in queries[0][0]


def test_get_activity_full_walks_entire_history():
    queries = []
    act = run_coro(_paged_adapter(1500, queries).get_activity(
        MissionRef("i1", "issue"), full=True))
    assert len(act.entries) == 1500 and len(queries) == 15   # past the shallow valve
    assert act.truncated is False


def test_get_activity_full_carries_reply_ids():
    comments = [
        {"id": "c1", "parent": None, "body": "root", "user": {"name": "u"},
         "createdAt": "2026-07-01T10:00:00.000Z"},
        {"id": "c2", "parent": {"id": "c1"}, "body": "reply",
         "user": {"name": "u"}, "createdAt": "2026-07-01T10:01:00.000Z"},
    ]
    queries = []
    act = run_coro(_full_adapter(queries, comments=comments).get_activity(
        MissionRef("i1", "issue"), full=True))
    assert "parent" in queries[0][0]
    by_id = {e.entry_id: e for e in act.entries}
    assert by_id["c2"].parent_id == "c1" and by_id["c1"].parent_id is None
    act2 = run_coro(_paged_adapter(3, []).get_activity(MissionRef("i1", "issue")))
    assert all(e.entry_id is None and e.parent_id is None for e in act2.entries)


def test_get_activity_full_surfaces_description_and_native_attachments():
    desc = "brief with [spec.pdf](https://uploads.linear.app/dd/spec) inline"
    extra = {"description": desc, "attachments": {"nodes": [
        {"url": "https://uploads.linear.app/dd/photo", "title": "photo.png"},
        {"url": "https://github.com/x/pull/1", "title": "PR #1"},
        {"url": "https://uploads.linear.app/dd/spec", "title": "dup of desc"},
    ]}}
    act = run_coro(_full_adapter(issue_extra=extra).get_activity(
        MissionRef("i1", "issue"), full=True))
    att = {a.url: a for a in act.mission_attachments}
    assert len(act.mission_attachments) == 3               # url-deduped
    assert att["https://uploads.linear.app/dd/spec"].name == "spec.pdf"
    assert att["https://uploads.linear.app/dd/spec"].kind == "file"
    assert att["https://uploads.linear.app/dd/photo"].kind == "file"
    assert att["https://github.com/x/pull/1"].kind == "link"
    act2 = run_coro(_paged_adapter(2, []).get_activity(MissionRef("i1", "issue")))
    assert act2.mission_attachments == []                  # shallow: never


def test_get_activity_full_native_attachments_overflow_warns(caplog):
    # the 51st native attachment must never vanish silently
    extra = {"attachments": {"pageInfo": {"hasNextPage": True}, "nodes": [
        {"url": f"https://uploads.linear.app/{i}", "title": f"f{i}"}
        for i in range(50)]}}
    with caplog.at_level("WARNING", logger="devcake.linear"):
        act = run_coro(_full_adapter(issue_extra=extra).get_activity(
            MissionRef("i1", "issue"), full=True))
    assert len(act.mission_attachments) == 50
    assert any("native attachment" in r.message for r in caplog.records)


def test_get_activity_full_exact_boundary_not_truncated():
    from devcake.adapters.linear.adapter import MAX_COMMENT_PAGES_FULL
    act = run_coro(_paged_adapter(100 * MAX_COMMENT_PAGES_FULL, [])
                   .get_activity(MissionRef("i1", "issue"), full=True))
    assert len(act.entries) == 100 * MAX_COMMENT_PAGES_FULL
    assert act.truncated is False


def test_get_activity_full_hard_stop_sets_truncated(caplog):
    from devcake.adapters.linear.adapter import MAX_COMMENT_PAGES_FULL
    total = 100 * MAX_COMMENT_PAGES_FULL + 50
    queries = []
    with caplog.at_level("ERROR", logger="devcake.linear"):
        act = run_coro(_paged_adapter(total, queries).get_activity(
            MissionRef("i1", "issue"), full=True))
    assert len(act.entries) == 100 * MAX_COMMENT_PAGES_FULL
    assert act.truncated is True                           # builder's banner cue
    assert any("hard stop" in r.message for r in caplog.records)


def test_get_activity_ceiling_warns_and_keeps_newest(caplog):
    total = 100 * MAX_COMMENT_PAGES + 50            # 50 past the ceiling
    queries = []
    with caplog.at_level("WARNING", logger="devcake.linear"):
        act = run_coro(_paged_adapter(total, queries).get_activity(MissionRef("i1", "issue")))
    assert len(act.entries) == 100 * MAX_COMMENT_PAGES
    assert len(queries) == MAX_COMMENT_PAGES        # stopped at the valve
    assert any("comment ceiling" in r.message for r in caplog.records)
    # newest-first walk: the dropped 50 are the OLDEST (c0..c49)
    assert min(e.body for e in act.entries if e.body.startswith("c")) != "c0"
    assert f"c{total - 1}" in {e.body for e in act.entries}


# ── project full-history mode (project-fidelity fix) ─────────────────────────

def _project_header():
    return {"id": "p1", "name": "Proj X", "description": "short",
            "content": "long body",
            "url": "https://linear/proj-x", "updatedAt": "2026-07-12T10:00:00.000Z",
            "priority": 2, "status": {"name": "Started", "type": "started"},
            "labels": {"nodes": []}}


def _conn(nodes, has_next=False, cursor="0"):
    return {"pageInfo": {"hasNextPage": has_next, "endCursor": cursor},
            "nodes": list(nodes)}


def _update(i, comments=None, user="alice"):
    return {"id": f"u{i}", "body": f"update {i}", "user": {"name": user},
            "createdAt": f"2026-07-01T10:{i % 60:02d}:00.000Z",
            "comments": comments if comments is not None else _conn([])}


def _project_adapter(queries=None, *, content=None, docs=None, updates=None,
                     ext_links=(), proj_attachments=(), walk_pages=None,
                     fail_enrichment=False):
    """Canned _gql dispatching on query shape: base project read, combined
    enrichment first page, per-connection walks. `walk_pages` maps
    'documents'/'projectUpdates'/'updateComments' → list of extra pages."""
    ad = LinearAdapter("key")
    walks = {k: list(v) for k, v in (walk_pages or {}).items()}

    async def _gql(query, variables=None):
        if queries is not None:
            queries.append((query, dict(variables or {})))
        header = _project_header()
        if content is not None:
            header["content"] = content
        if "projectUpdate(id:" in query:                     # comment overflow walk
            page = walks["updateComments"].pop(0)
            return {"projectUpdate": {"comments": page}}
        if "documents(first:" in query and "$after" in query:
            return {"project": {"documents": walks["documents"].pop(0)}}
        if "projectUpdates(first:" in query and "$after" in query:
            return {"project": {"projectUpdates": walks["projectUpdates"].pop(0)}}
        if "documents(first:" in query:                      # combined enrichment
            if fail_enrichment:
                raise RuntimeError("enrichment boom")
            return {"project": {
                "documents": docs if docs is not None else _conn([]),
                "projectUpdates": updates if updates is not None else _conn([]),
                "externalLinks": _conn(ext_links),
                "attachments": _conn(proj_attachments)}}
        return {"project": header}                           # base read
    ad._gql = _gql
    return ad


def test_project_shallow_stays_cheap():
    # no production caller rides this path; it must never pay enrichment cost
    queries = []
    act = run_coro(_project_adapter(queries).get_activity(MissionRef("p1", "project")))
    assert act.entries == [] and act.documents == []
    assert len(queries) == 1
    assert "documents" not in queries[0][0]
    assert "projectUpdates" not in queries[0][0]


def test_project_full_maps_documents_updates_links():
    doc = {"id": "d1", "title": "Spec",
           "content": "See [img.png](https://uploads.linear.app/x/img.png)",
           "url": "https://linear/doc1"}
    comments = _conn([{"id": "c1", "parent": None, "body": "reply body",
                       "user": {"name": "bob"},
                       "createdAt": "2026-07-01T10:05:00.000Z"}])
    act = run_coro(_project_adapter(
        content="hello [a.txt](https://uploads.linear.app/y/a.txt)",
        docs=_conn([doc]), updates=_conn([_update(0, comments)]),
        ext_links=[{"url": "https://example.com/design", "label": "Design doc"}],
        proj_attachments=[{"url": "https://uploads.linear.app/z/file.pdf",
                           "title": "file.pdf"}],
    ).get_activity(MissionRef("p1", "project"), full=True))
    # documents surfaced inline
    assert [(d.title, d.url) for d in act.documents] == [("Spec", "https://linear/doc1")]
    # update + its comment as entries, chronological, threaded under the update
    assert [e.entry_id for e in act.entries] == ["u0", "c1"]
    assert act.entries[0].author == "alice (project update)"
    assert act.entries[0].parent_id is None
    assert act.entries[1].author == "bob" and act.entries[1].parent_id == "u0"
    # attachments: content upload + doc-embedded upload + link + native file
    att = {a.url: a for a in act.mission_attachments}
    assert att["https://uploads.linear.app/y/a.txt"].kind == "file"
    assert att["https://uploads.linear.app/y/a.txt"].name == "a.txt"
    assert att["https://uploads.linear.app/x/img.png"].kind == "file"
    assert att["https://example.com/design"].kind == "link"
    assert att["https://example.com/design"].name == "Design doc"
    assert att["https://uploads.linear.app/z/file.pdf"].kind == "file"
    assert len(act.mission_attachments) == 4
    assert act.truncated is False


def test_project_full_threaded_update_comment_keeps_comment_parent():
    comments = _conn([
        {"id": "c1", "parent": None, "body": "root", "user": {"name": "u"},
         "createdAt": "2026-07-01T10:05:00.000Z"},
        {"id": "c2", "parent": {"id": "c1"}, "body": "nested",
         "user": {"name": "u"}, "createdAt": "2026-07-01T10:06:00.000Z"}])
    act = run_coro(_project_adapter(
        updates=_conn([_update(0, comments)]),
    ).get_activity(MissionRef("p1", "project"), full=True))
    by_id = {e.entry_id: e for e in act.entries}
    assert by_id["c1"].parent_id == "u0"     # top-level → under the update
    assert by_id["c2"].parent_id == "c1"     # threaded reply keeps its parent


def test_project_full_document_walk_and_ceiling(caplog):
    from devcake.adapters.linear.adapter import MAX_PROJECT_DOC_PAGES
    first = _conn([{"id": "d0", "title": "D0", "content": "", "url": ""}],
                  has_next=True)
    # every walk page still claims another — the ceiling must trip loudly
    more = [_conn([{"id": f"d{i}", "title": f"D{i}", "content": "", "url": ""}],
                  has_next=True) for i in range(1, MAX_PROJECT_DOC_PAGES)]
    with caplog.at_level("WARNING", logger="devcake.linear"):
        act = run_coro(_project_adapter(
            docs=first, walk_pages={"documents": more},
        ).get_activity(MissionRef("p1", "project"), full=True))
    assert len(act.documents) == MAX_PROJECT_DOC_PAGES
    assert any("document ceiling" in r.message for r in caplog.records)


def test_project_full_update_feed_hard_stop_sets_truncated(caplog):
    from devcake.adapters.linear.adapter import MAX_PROJECT_UPDATE_PAGES
    first = _conn([_update(0)], has_next=True)
    more = [_conn([_update(i)], has_next=True)
            for i in range(1, MAX_PROJECT_UPDATE_PAGES)]
    with caplog.at_level("ERROR", logger="devcake.linear"):
        act = run_coro(_project_adapter(
            updates=first, walk_pages={"projectUpdates": more},
        ).get_activity(MissionRef("p1", "project"), full=True))
    assert len(act.entries) == MAX_PROJECT_UPDATE_PAGES
    assert act.truncated is True             # builder's banner cue
    assert any("INCOMPLETE" in r.message for r in caplog.records)


def test_project_full_update_comment_overflow_walks():
    overflow_first = _conn([{"id": "c1", "parent": None, "body": "one",
                             "user": {"name": "u"},
                             "createdAt": "2026-07-01T10:05:00.000Z"}],
                           has_next=True, cursor="c1")
    walk_page = _conn([{"id": "c2", "parent": None, "body": "two",
                        "user": {"name": "u"},
                        "createdAt": "2026-07-01T10:06:00.000Z"}])
    queries = []
    act = run_coro(_project_adapter(
        queries, updates=_conn([_update(0, overflow_first)]),
        walk_pages={"updateComments": [walk_page]},
    ).get_activity(MissionRef("p1", "project"), full=True))
    assert {e.entry_id for e in act.entries} == {"u0", "c1", "c2"}
    assert any("projectUpdate(id:" in q for q, _ in queries)


def test_project_full_enrichment_failure_fails_open(caplog):
    # the mission must still dispatch on the brief alone — never raise into
    # the Dev's activity.get reply
    with caplog.at_level("WARNING", logger="devcake.linear"):
        act = run_coro(_project_adapter(fail_enrichment=True).get_activity(
            MissionRef("p1", "project"), full=True))
    assert act.mission.key == "PRJ-proj-x"
    assert act.entries == [] and act.documents == []
    assert any("enrichment failed" in r.message for r in caplog.records)


def test_project_full_base_read_stays_fail_closed():
    # an unreadable mission must NOT dispatch — only enrichment is best-effort
    ad = LinearAdapter("key")

    async def _gql(query, variables=None):
        raise RuntimeError("project gone")
    ad._gql = _gql
    import pytest
    with pytest.raises(RuntimeError):
        run_coro(ad.get_activity(MissionRef("p1", "project"), full=True))


def test_asset_regexes_tolerate_angle_bracket_urls():
    # Linear's DOCUMENT serializer emits [name](<url>) — verified live
    # 2026-08-04; a trailing > must never ride into the captured URL
    from devcake.adapters.linear.adapter import _ASSET_RE, _NAMED_ASSET_RE
    doc = "See [seed.md](<https://uploads.linear.app/x/seed.md>) here"
    assert _ASSET_RE.findall(doc) == ["https://uploads.linear.app/x/seed.md"]
    assert _NAMED_ASSET_RE.findall(doc) == [
        ("seed.md", "https://uploads.linear.app/x/seed.md")]
    plain = "See [r.md](https://uploads.linear.app/y/r.md) and bare "\
            "https://uploads.linear.app/z/s"
    assert _NAMED_ASSET_RE.findall(plain) == [
        ("r.md", "https://uploads.linear.app/y/r.md")]
    assert _ASSET_RE.findall(plain) == ["https://uploads.linear.app/y/r.md",
                                        "https://uploads.linear.app/z/s"]
