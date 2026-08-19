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

import asyncio
import logging
import re
from contextlib import asynccontextmanager
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


_LAST_PAGE_RE = re.compile(r'[?&]page=(\d+)[^>]*>\s*;\s*rel="last"', re.I)


def last_page_from_headers(headers, *, page_size: int) -> int | None:
    """GitHub/Gitea Link rel=last, or X-Total-Count / page_size."""
    if headers is None:
        return None
    # httpx headers are case-insensitive; plain dicts in tests are not.
    link = ""
    total = None
    if hasattr(headers, "get"):
        link = headers.get("link") or headers.get("Link") or ""
        total = headers.get("x-total-count") or headers.get("X-Total-Count")
    hit = _LAST_PAGE_RE.search(link)
    if hit:
        return int(hit.group(1))
    if total and page_size > 0:
        try:
            n = int(total)
        except (TypeError, ValueError):
            return None
        if n <= 0:
            return 1
        return (n + page_size - 1) // page_size
    return None


def _rest_ceiling(what: str, max_pages: int, page_size: int,
                  on_ceiling: str, ceiling_error: str | None) -> None:
    if on_ceiling == "raise":
        raise RuntimeError(
            ceiling_error or f"{what}: more than {max_pages * page_size} "
                             f"entries — refusing a truncated read")
    if on_ceiling == "warn":
        log.warning("%s hit page ceiling (%s pages)", what, max_pages)


async def paginate_rest_newest(
        fetch_page: Callable[[int], Awaitable[tuple[list, int | None]]],
        *, page_size: int, max_pages: int, what: str,
        on_ceiling: str = "warn",
        ceiling_error: str | None = None) -> tuple[list, bool]:
    """Oldest-first REST: keep the newest `max_pages` when the ceiling hits.

    fetch_page(n) → (items, last_page or None). Returns items chronological.
    last_page comes from last_page_from_headers. If the vendor never
    discloses a last page and page 1 is full, we cannot find the tail —
    walk forward (legacy) and still flag truncated at the ceiling.
    """
    first, last = await fetch_page(1)
    if not first:
        return [], False
    if last is None and len(first) < page_size:
        return list(first), False
    if last is None:
        items = list(first)
        for page in range(2, max_pages + 1):
            batch, hinted = await fetch_page(page)
            if hinted is not None:
                last = hinted
                break
            if not batch:
                return items, False
            items.extend(batch)
            if len(batch) < page_size:
                return items, False
        else:
            _rest_ceiling(what, max_pages, page_size, on_ceiling, ceiling_error)
            return items, True
    assert last is not None
    if last <= max_pages:
        items = []
        for page in range(1, last + 1):
            batch, _ = await fetch_page(page)
            items.extend(batch)
        return items, False
    start = last - max_pages + 1
    items = []
    for page in range(start, last + 1):
        batch, _ = await fetch_page(page)
        items.extend(batch)
    _rest_ceiling(what, max_pages, page_size, on_ceiling, ceiling_error)
    return items, True


# --- per-mission label-write serialization ---------------------------------

_LABEL_LOCKS: dict[str, "asyncio.Lock"] = {}
# Entrants that have entered label_write_lock (before acquire) and not yet
# left — holders and waiters. Eviction must not use Lock.locked() alone:
# locked() is False after release even while waiters remain (CAKE-74).
_LABEL_LOCK_OCCUPANCY: dict[str, int] = {}
_LABEL_LOCKS_MAX = 512


def _label_lock(pmo_id: str) -> "asyncio.Lock":
    lock = _LABEL_LOCKS.get(pmo_id)
    if lock is None:
        if len(_LABEL_LOCKS) >= _LABEL_LOCKS_MAX:
            for key in list(_LABEL_LOCKS):
                if _LABEL_LOCK_OCCUPANCY.get(key, 0) == 0:
                    del _LABEL_LOCKS[key]
                    _LABEL_LOCK_OCCUPANCY.pop(key, None)
        lock = _LABEL_LOCKS.setdefault(pmo_id, asyncio.Lock())
    return lock


@asynccontextmanager
async def label_write_lock(pmo_id: str):
    """Serialize label mutations per mission within this process.

    Every adapter's ``swap_labels`` is a read-modify-write over the issue's
    full label set (or, on Linear issues, a read that steers per-label
    mutations). Two orchestrator tasks legitimately mutate the same mission's
    labels in the same instant — the ingress consumer's finalize advancing
    the stage label while a poll-loop sweep retires a gate label — and a
    full-set write computed from a pre-write read then deletes the other
    writer's label. Observed live 2026-08-17 (CAKE-48): the discovery
    sweep's retire erased the freshly added stage label, stranding the
    mission as "in_progress without stage label — not DevCake's".

    Keyed by pmo_id across adapter instances; a numeric-id collision between
    two boards only over-serializes, never under-serializes. The registry
    drops locks with no in-flight entrants once it grows past
    _LABEL_LOCKS_MAX (not merely unheld — a released lock may still have
    waiters; CAKE-74).
    """
    lock = _label_lock(pmo_id)
    _LABEL_LOCK_OCCUPANCY[pmo_id] = _LABEL_LOCK_OCCUPANCY.get(pmo_id, 0) + 1
    try:
        async with lock:
            yield
    finally:
        remaining = _LABEL_LOCK_OCCUPANCY.get(pmo_id, 1) - 1
        if remaining <= 0:
            _LABEL_LOCK_OCCUPANCY.pop(pmo_id, None)
        else:
            _LABEL_LOCK_OCCUPANCY[pmo_id] = remaining
