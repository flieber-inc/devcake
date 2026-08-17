"""OidcTokenPort — refresh-token grants for control-plane OAuth (host-side).

Used at runspec credential inject so Grok CLI session files stay fresh across
ephemeral Devs without any Dev→host credential write-back (docs/14).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class TokenRefreshResult:
    access_token: str
    refresh_token: str | None = None
    expires_in: float | None = None


class OidcTokenError(Exception):
    """Refresh failed (network, 5xx, malformed body). Safe to retry later."""


class OidcRefreshRevoked(OidcTokenError):
    """Refresh grant rejected (4xx). Operator must re-run the OAuth wizard."""


class OidcTokenPort(Protocol):
    def refresh(
        self,
        *,
        refresh_token: str,
        client_id: str | None = None,
        token_url: str = "https://auth.x.ai/oauth2/token",
        timeout: float = 15.0,
    ) -> TokenRefreshResult: ...
