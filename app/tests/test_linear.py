"""docs/05 §3: get_activity is cursor-paginated like every other list read
(a single page was verified LOSSY at 108 comments on DEV-50, 2026-07-12).
Ordering is pinned with orderBy: createdAt (verified live: newest-first); the
10-page/1,000-comment safety ceiling logs a truncation WARNING, never silent."""
import asyncio

from devcake.adapters.linear.adapter import MAX_COMMENT_PAGES, LinearAdapter


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
    act = run_coro(_paged_adapter(108, queries).get_activity("i1"))  # DEV-50 shape
    assert len(act.entries) == 108
    assert len(queries) == 2                        # 100 + 8, exactly two reads
    assert "orderBy: createdAt" in queries[0][0]    # pinned ordering
    assert queries[1][1]["after"] == "100"          # cursor threaded through
    ts = [e.ts for e in act.entries]
    assert ts == sorted(ts)                         # chronological index
    assert act.entries[0].body == "c0"              # oldest survived the walk


def test_get_activity_single_page_needs_one_read():
    queries = []
    act = run_coro(_paged_adapter(99, queries).get_activity("i1"))
    assert len(act.entries) == 99 and len(queries) == 1


def test_get_activity_ceiling_warns_and_keeps_newest(caplog):
    total = 100 * MAX_COMMENT_PAGES + 50            # 50 past the ceiling
    queries = []
    with caplog.at_level("WARNING", logger="devcake.linear"):
        act = run_coro(_paged_adapter(total, queries).get_activity("i1"))
    assert len(act.entries) == 100 * MAX_COMMENT_PAGES
    assert len(queries) == MAX_COMMENT_PAGES        # stopped at the valve
    assert any("comment ceiling" in r.message for r in caplog.records)
    # newest-first walk: the dropped 50 are the OLDEST (c0..c49)
    assert min(e.body for e in act.entries if e.body.startswith("c")) != "c0"
    assert f"c{total - 1}" in {e.body for e in act.entries}
