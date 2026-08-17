"""XaiOidcTokenAdapter — MockTransport contract tests."""
from __future__ import annotations

import json

import httpx
import pytest

from devcake.adapters.xai.token import XaiOidcTokenAdapter
from devcake.ports.oidc_token import OidcRefreshRevoked, OidcTokenError


def _handler(status: int, body: dict | str):
    def h(request: httpx.Request) -> httpx.Response:
        if isinstance(body, dict):
            return httpx.Response(status, json=body)
        return httpx.Response(status, text=body)
    return h


def test_refresh_200_maps_fields():
    transport = httpx.MockTransport(_handler(200, {
        "access_token": "at-1",
        "refresh_token": "rt-1",
        "expires_in": 3600,
    }))
    adapter = XaiOidcTokenAdapter(transport=transport)
    result = adapter.refresh(refresh_token="rt-old", client_id="cid")
    assert result.access_token == "at-1"
    assert result.refresh_token == "rt-1"
    assert result.expires_in == 3600.0


def test_refresh_400_is_revoked():
    transport = httpx.MockTransport(_handler(400, {"error": "invalid_grant"}))
    adapter = XaiOidcTokenAdapter(transport=transport)
    with pytest.raises(OidcRefreshRevoked):
        adapter.refresh(refresh_token="rt-old")


def test_refresh_500_is_token_error():
    transport = httpx.MockTransport(_handler(500, "nope"))
    adapter = XaiOidcTokenAdapter(transport=transport)
    with pytest.raises(OidcTokenError, match="failed"):
        adapter.refresh(refresh_token="rt-old")


def test_refresh_missing_access_token():
    transport = httpx.MockTransport(_handler(200, {"token_type": "bearer"}))
    adapter = XaiOidcTokenAdapter(transport=transport)
    with pytest.raises(OidcTokenError, match="access_token"):
        adapter.refresh(refresh_token="rt-old")


def test_refresh_does_not_log_tokens(caplog):
    transport = httpx.MockTransport(_handler(400, {
        "error": "invalid_grant",
        "error_description": "secret-should-not-appear",
    }))
    adapter = XaiOidcTokenAdapter(transport=transport)
    with caplog.at_level("WARNING"):
        with pytest.raises(OidcRefreshRevoked):
            adapter.refresh(refresh_token="SUPER-SECRET-RT")
    joined = " ".join(r.message for r in caplog.records)
    assert "SUPER-SECRET-RT" not in joined
    assert "secret-should-not-appear" not in joined
