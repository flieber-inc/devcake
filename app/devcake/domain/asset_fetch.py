"""PMO attachment download URL policy (docs/14 §11).

Ticket content can point `download_asset` at arbitrary URLs. The app must not
credential-fetch or SSRF-follow off an allowlist of known vendor asset hosts.
This module is pure policy — adapters supply their allowed hosts and perform
the HTTP GET.
"""

from __future__ import annotations

from urllib.parse import urljoin, urlsplit


class AssetUrlError(ValueError):
    """URL refused by the download allowlist / scheme policy."""


def assert_downloadable_asset_url(
    url: str,
    *,
    allowed_hosts: set[str],
    allow_http: bool = False,
) -> str:
    """Validate *url* for an authenticated asset download.

    Returns the normalized URL string on success.
    Raises AssetUrlError when the URL is empty, uses a forbidden scheme,
    carries userinfo, or its host is outside *allowed_hosts*.

    *allow_http*: internal Gitea origins on the docker network are http;
    public vendors (Linear) must be https.
    """
    if not url or not isinstance(url, str) or not url.strip():
        raise AssetUrlError("empty asset URL")
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "").lower()
    if allow_http:
        if scheme not in ("http", "https"):
            raise AssetUrlError(f"unsupported asset URL scheme {scheme!r}")
    elif scheme != "https":
        raise AssetUrlError(f"asset URL must be https, got {scheme!r}")
    if parts.username is not None or parts.password is not None:
        raise AssetUrlError("asset URL must not include userinfo")
    host = (parts.hostname or "").lower()
    if not host:
        raise AssetUrlError("asset URL missing host")
    allowed = {h.lower() for h in allowed_hosts}
    if host not in allowed:
        raise AssetUrlError(f"asset host {host!r} not in allowlist")
    return url.strip()


def resolve_redirect_location(current_url: str, location: str) -> str:
    """Resolve a redirect *Location* against *current_url* (RFC 3986 join).

    Always joins rather than special-casing ``http`` prefixes so uppercase
    schemes and relative paths behave consistently.
    """
    if not location or not str(location).strip():
        raise AssetUrlError("empty redirect Location")
    return urljoin(current_url, str(location).strip())
