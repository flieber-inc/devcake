"""Shared lazy HTTP client per adapter instance (2026-08 evaluation F16).

Every adapter previously built a fresh `httpx.AsyncClient` per request — a
full TCP+TLS handshake each time, the root cause of the 319-repo sweep
incident that was mitigated with a semaphore + budget rather than fixed.
`PooledClient` keeps ONE lazily-created client (connection keep-alive) for
the adapter's hot `_req` path; special-purpose calls (large asset downloads
with redirects, long-timeout uploads) deliberately stay per-call.

Lifecycle: `ForgeRuntime.rebuild` fire-and-forget-closes the outgoing
adapter set's clients (`aclose_adapters`); an adapter that is simply GC'd
(tests, PMO manager rebuilds) closes nothing — httpx finalizers cover it.
"""

from __future__ import annotations

import asyncio
import json
import logging

import httpx

log = logging.getLogger("devcake.adapters.http")

# strong refs: the loop holds tasks weakly — a fire-and-forget close could
# be GC'd before running (same idiom as security's alarm tasks)
_CLOSE_TASKS: set = set()


class PooledClient:
    def __init__(self, *, timeout: float = 20,
                 transport: "httpx.AsyncBaseTransport | None" = None):
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    def get(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()


async def forge_request(client: httpx.AsyncClient, method: str, url: str, *,
                        path_label: str, headers: dict | None = None,
                        raw: bool = False, **kwargs):
    """THE forge wire call (ADR-0034; 2026-08-12 audit F7). ports/forge.py
    declares "adapters must never leak httpx exceptions upward" — yet all
    three forge `_req`s let ConnectError/ReadTimeout escape raw while both
    PMO adapters wrapped correctly (the audit's first-tenant/second-tenant
    pattern). Network failures become ForgeError(status=None), which
    health_probe's definitive-status check already reads as transient.
    Success is HTTP 2xx only — 3xx raise ForgeError; non-JSON 2xx bodies
    map through ForgeError rather than leaking json.JSONDecodeError."""
    from ..ports.forge import ForgeError
    try:
        resp = await client.request(method, url, headers=headers, **kwargs)
    except httpx.HTTPError as e:
        raise ForgeError(f"{method} {path_label} → network: {e}",
                         status=None) from e
    if resp.status_code < 200 or resp.status_code >= 300:
        raise ForgeError(f"{method} {path_label} → {resp.status_code}: "
                         f"{resp.text[:200]}", status=resp.status_code)
    if raw:                           # file_content wants bytes, not JSON
        return resp.content
    if not resp.text:
        return None
    try:
        return resp.json()
    except json.JSONDecodeError as e:
        raise ForgeError(
            f"{method} {path_label} → {resp.status_code}: non-JSON body: "
            f"{resp.text[:200]}", status=resp.status_code) from e


def aclose_adapters(adapters) -> None:
    """Best-effort async close of an outgoing adapter set from a SYNC caller
    (rebuild). No running loop (boot, sync tests) ⇒ skip — finalizers cover
    it; never raises."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    for adapter in adapters:
        close = getattr(adapter, "aclose", None)
        if close is None:
            continue
        try:
            task = loop.create_task(close())
            _CLOSE_TASKS.add(task)
            task.add_done_callback(_CLOSE_TASKS.discard)
        except Exception:  # noqa: BLE001 — closing old clients must never break a config apply
            log.debug("adapter close scheduling failed", exc_info=True)
