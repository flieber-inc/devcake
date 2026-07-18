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
