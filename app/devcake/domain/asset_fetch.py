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


def assert_fetch_netloc(url: str, origin: str) -> str:
    """Require *url* netloc to match *origin* netloc (host + port).

    Used after presentation-host rewrite so credentialed GETs only hit the
    configured api_base origin, not another port on the same hostname.
    """
    if not origin or not str(origin).strip():
        raise AssetUrlError("empty fetch origin")
    u = urlsplit(url.strip())
    o = urlsplit(origin.strip())
    if not u.netloc or not o.netloc:
        raise AssetUrlError("asset URL or origin missing netloc")
    if u.netloc.lower() != o.netloc.lower():
        raise AssetUrlError(
            f"asset netloc {u.netloc!r} != origin netloc {o.netloc!r}")
    return url.strip()


def assert_path_prefix(url: str, prefix: str) -> str:
    """Require URL path to start with *prefix* (e.g. Gitea ``/attachments/``)."""
    if not prefix:
        raise AssetUrlError("empty path prefix")
    path = urlsplit(url.strip()).path or ""
    if not path.startswith(prefix):
        raise AssetUrlError(
            f"asset path {path!r} must start with {prefix!r}")
    return url.strip()


def enforce_download_byte_cap(
    content: bytes,
    *,
    content_length: str | None,
    max_bytes: int,
) -> bytes:
    """Refuse oversized attachment bodies (capabilities().attachment_max_bytes).

    Checks Content-Length when present, then the actual body length.
    """
    if max_bytes <= 0:
        raise AssetUrlError("invalid attachment size cap")
    if content_length is not None and str(content_length).strip():
        try:
            declared = int(str(content_length).strip())
        except ValueError as e:
            raise AssetUrlError(
                f"invalid Content-Length {content_length!r}") from e
        if declared > max_bytes:
            raise AssetUrlError(
                f"asset Content-Length {declared} exceeds cap {max_bytes}")
    if len(content) > max_bytes:
        raise AssetUrlError(
            f"asset body {len(content)} bytes exceeds cap {max_bytes}")
    return content
