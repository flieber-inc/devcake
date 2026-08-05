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
