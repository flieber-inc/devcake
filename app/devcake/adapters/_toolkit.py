"""Shared content-level adapter helpers (ADR-0034; 2026-08-12 audit F13).

`adapters/http.py` stays the wire layer (pooled clients, the forge request
chokepoint); this module holds the content-level logic that used to exist
as near-identical copies inside individual adapters — and had already
drifted (the credentialed download loop differed on `allow_http` between
its Linear and gitea_issues copies; an SSRF-hardening fix would have been
applied to one and missed in the other).

Import direction (guarded in test_structure_guards): adapters may import
this module; this module imports stdlib, httpx, and domain.asset_fetch
primitives ONLY — never a vendor adapter package; domain/ and ports/ never
import it.
"""

from __future__ import annotations

import logging
from typing import Awaitable, Callable

import httpx

log = logging.getLogger("devcake.adapters")

_REDIRECTS = (301, 302, 303, 307, 308)


async def fetch_following_safe_redirects(
        client: httpx.AsyncClient, url: str, *, allowed_hosts: set[str],
        headers: dict, allow_http: bool,
        pin: Callable[[str], str] | None = None,
        max_hops: int = 5) -> httpx.Response:
    """THE credentialed redirect walk (docs/14 §11) — returns the final
    non-redirect response. Every hop re-validates against the allowlist so
    an open redirect can never carry the Authorization header off-host;
    ``pin`` (per-hop, e.g. gitea's netloc/path pin) re-applies after each
    validation. ``allow_http`` is a REQUIRED keyword with no default:
    the policy difference between the copies this replaces (Linear https-
    only; self-hosted Gitea legitimately plain-http) is now stated at each
    call site instead of drifting silently.

    Raises AssetUrlError (refusals) and lets httpx errors propagate — the
    ADAPTER wraps both into its own taxonomy/messages; status handling and
    the byte cap (enforce_download_byte_cap) also stay with the caller.
    RuntimeError on hop exhaustion."""
    from ..domain.asset_fetch import (assert_downloadable_asset_url,
                                      resolve_redirect_location)
    current = assert_downloadable_asset_url(
        url, allowed_hosts=allowed_hosts, allow_http=allow_http)
    if pin is not None:
        current = pin(current)
    for _ in range(max_hops):
        resp = await client.get(current, headers=headers)
        if resp.status_code in _REDIRECTS:
            loc = resp.headers.get("location") or ""
            nxt = resolve_redirect_location(current, loc)
            nxt = assert_downloadable_asset_url(
                nxt, allowed_hosts=allowed_hosts, allow_http=allow_http)
            current = pin(nxt) if pin is not None else nxt
            continue
        return resp
    raise RuntimeError("too many redirects")


async def paginate_rest(
        fetch_page: Callable[[int], Awaitable[list]], *, page_size: int,
        max_pages: int, what: str, on_ceiling: str = "warn",
        ceiling_error: str | None = None) -> tuple[list, bool]:
    """THE REST page walk: pages 1..max_pages, stops on an empty or short
    page. → (items, truncated). Ceiling behavior is declared, never
    improvised per copy (audit F8's destruction case was a one-page label
    read feeding a full-set rewrite):
      'warn'  — log.warning, return truncated=True (list reads)
      'flag'  — silent truncated=True (caller owns the disclosure — the
                freshness gate treats truncation as material-unknown)
      'raise' — RuntimeError(ceiling_error or a generic message): reads
                that feed full-set REWRITES must refuse, not truncate.
    GraphQL connection walks (Linear) deliberately do NOT come through
    here — pageInfo-shaped iteration is a different protocol, and forcing
    it into this helper would manufacture a false abstraction (ADR-0034
    out-of-scope note)."""
    items: list = []
    for page in range(1, max_pages + 1):
        batch = await fetch_page(page)
        if not batch:
            return items, False
        items.extend(batch)
        if len(batch) < page_size:
            return items, False
    if on_ceiling == "raise":
        raise RuntimeError(
            ceiling_error or f"{what}: more than {max_pages * page_size} "
                             f"entries — refusing a truncated read")
    if on_ceiling == "warn":
        log.warning("%s hit page ceiling (%s pages)", what, max_pages)
    return items, True
