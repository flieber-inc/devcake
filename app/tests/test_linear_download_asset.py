"""Linear download_asset host allowlist + redirect policy (docs/14 §11)."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from devcake.adapters.linear.adapter import LinearAdapter


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_download_asset_refuses_evil_host():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(500)

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="refused"):
        run(a.download_asset("https://evil.example/steal"))
    assert seen == []  # never issued HTTP


def test_download_asset_refuses_evil_redirect():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if "good" in str(request.url):
            return httpx.Response(
                302, headers={"Location": "https://evil.example/exfil"})
        return httpx.Response(200, content=b"nope")

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="redirect refused"):
        run(a.download_asset("https://uploads.linear.app/team/good"))
    assert len(seen) == 1
    assert "uploads.linear.app" in seen[0]
    assert "evil.example" not in "".join(seen)


def test_download_asset_follows_same_host_redirect():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/start"):
            return httpx.Response(
                302, headers={"Location": "/team/final.bin"})
        return httpx.Response(200, content=b"linear-bytes")

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler))
    body = run(a.download_asset("https://uploads.linear.app/team/start"))
    assert body == b"linear-bytes"
