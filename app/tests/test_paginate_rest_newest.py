"""paginate_rest_newest: oldest-first vendors keep the newest pages."""

from __future__ import annotations

import asyncio

from devcake.adapters._toolkit import (
    last_page_from_headers, paginate_rest_newest)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_last_page_from_github_link_header():
    link = (
        '<https://api.github.com/x?page=2>; rel="next", '
        '<https://api.github.com/x?page=12>; rel="last"'
    )
    assert last_page_from_headers({"Link": link}, page_size=50) == 12


def test_last_page_from_x_total_count():
    assert last_page_from_headers(
        {"X-Total-Count": "251"}, page_size=50) == 6


def test_newest_walk_keeps_the_tail_when_ceiling_hits():
    """11 pages, ceiling 2 → pages 10 and 11 (items 10..11), truncated."""
    async def fetch(page):
        return [page], 11

    items, truncated = run(paginate_rest_newest(
        fetch, page_size=1, max_pages=2, what="test", on_ceiling="flag"))
    assert truncated is True
    assert items == [10, 11]


def test_newest_walk_returns_everything_when_under_ceiling():
    async def fetch(page):
        return [page], 3

    items, truncated = run(paginate_rest_newest(
        fetch, page_size=1, max_pages=10, what="test", on_ceiling="flag"))
    assert truncated is False
    assert items == [1, 2, 3]
