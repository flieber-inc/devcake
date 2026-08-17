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


def test_download_asset_refuses_oversized_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=b"y" * 80,
            headers={"Content-Length": "80"})

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler))
    a.capabilities = lambda: type(  # type: ignore[method-assign]
        "C", (), {"attachment_max_bytes": 40})()
    with pytest.raises(RuntimeError, match="refused"):
        run(a.download_asset("https://uploads.linear.app/team/big.bin"))


def test_download_asset_maps_http_status_to_domain_errors():
    """Port Liskov: httpx status/network failures must not escape the adapter
    — only PMOTransient (retryable) or permanent RuntimeError."""
    from devcake.ports.pmo import PMOTransient

    def handler_5xx(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="upstream down")

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler_5xx))
    with pytest.raises(PMOTransient, match="download"):
        run(a.download_asset("https://uploads.linear.app/team/x.bin"))

    def handler_4xx(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(handler_4xx))
    with pytest.raises(RuntimeError, match="download") as ei:
        run(a.download_asset("https://uploads.linear.app/team/x.bin"))
    assert not isinstance(ei.value, httpx.HTTPError)


async def _file_upload_gql(query, variables=None):
    """Canned fileUpload mutation so tests can drive only the PUT status map."""
    _ = query, variables
    return {
        "fileUpload": {
            "success": True,
            "uploadFile": {
                "uploadUrl": "https://uploads.linear.app/put",
                "assetUrl": "https://uploads.linear.app/asset/x",
                "headers": [{"key": "Upload-Key", "value": "v"}],
            },
        },
    }


def test_upload_attachment_maps_http_status_to_domain_errors():
    """Port Liskov: permanent PUT 4xx must not become PMOTransient via
    raise_for_status() + blanket HTTPError (HTTPStatusError ⊆ HTTPError).
    Retryable 429/5xx → PMOTransient; other ≥400 → permanent RuntimeError."""
    from devcake.ports.pmo import PMOTransient

    def put_status(code: int, text: str = ""):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.method == "PUT"
            return httpx.Response(code, text=text or f"status {code}")
        return handler

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(put_status(403, "forbidden")))
    a._gql = _file_upload_gql  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="upload") as ei:
        run(a.upload_attachment("issue-id", "plan.md", b"# plan"))
    assert not isinstance(ei.value, PMOTransient)
    assert not isinstance(ei.value, httpx.HTTPError)

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(put_status(503, "upstream down")))
    a._gql = _file_upload_gql  # type: ignore[method-assign]
    with pytest.raises(PMOTransient, match="upload"):
        run(a.upload_attachment("issue-id", "plan.md", b"# plan"))

    a = LinearAdapter(
        api_key="lin_api_test_key_xxxxxxxxxxxx",
        transport=httpx.MockTransport(put_status(429, "slow down")))
    a._gql = _file_upload_gql  # type: ignore[method-assign]
    with pytest.raises(PMOTransient, match="upload"):
        run(a.upload_attachment("issue-id", "plan.md", b"# plan"))
