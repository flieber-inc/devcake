"""OidcTokenPort over auth.x.ai (host-side refresh; never logs token values)."""

from __future__ import annotations

import logging

import httpx

from ...ports.oidc_token import (OidcRefreshRevoked, OidcTokenError,
                                 TokenRefreshResult)

log = logging.getLogger("devcake.xai.token")

DEFAULT_TOKEN_URL = "https://auth.x.ai/oauth2/token"


class XaiOidcTokenAdapter:
    def __init__(self, transport: httpx.BaseTransport | None = None):
        self._transport = transport

    def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str | None = None,
        token_url: str = DEFAULT_TOKEN_URL,
        timeout: float = 15.0,
    ) -> TokenRefreshResult:
        data: dict[str, str] = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        if client_id:
            data["client_id"] = client_id
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                resp = client.post(token_url, data=data)
        except httpx.TimeoutException as e:
            raise OidcTokenError("xAI token endpoint timed out") from e
        except httpx.HTTPError as e:
            raise OidcTokenError(f"xAI token endpoint unreachable: {e}") from e

        if resp.status_code in (400, 401, 403):
            # Do not include response body — may echo grant fragments.
            log.warning("xAI OAuth refresh rejected status=%s", resp.status_code)
            raise OidcRefreshRevoked(
                f"xAI OAuth refresh rejected ({resp.status_code})")
        if resp.status_code != 200:
            log.warning("xAI OAuth refresh failed status=%s", resp.status_code)
            raise OidcTokenError(
                f"xAI OAuth refresh failed ({resp.status_code})")
        try:
            body = resp.json()
        except ValueError as e:
            raise OidcTokenError("xAI OAuth refresh returned non-JSON") from e
        access = body.get("access_token")
        if not access or not isinstance(access, str):
            raise OidcTokenError("xAI OAuth refresh missing access_token")
        refresh = body.get("refresh_token")
        expires_in = body.get("expires_in")
        try:
            exp = float(expires_in) if expires_in is not None else None
        except (TypeError, ValueError):
            exp = None
        return TokenRefreshResult(
            access_token=access,
            refresh_token=refresh if isinstance(refresh, str) else None,
            expires_in=exp,
        )
