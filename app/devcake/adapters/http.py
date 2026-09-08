"""One shared HTTP connection pool for the whole process (2026-09 field
incident), behind the per-adapter `PooledClient` seam of the 2026-08
evaluation (F16).

F16 replaced a fresh `httpx.AsyncClient` per request with one lazily-created
client per adapter instance — keep-alive instead of a TCP+TLS handshake per
call. With one forge adapter per repository card that became one POOL per
card: a host with 325 cards held 984 idle keep-alive sockets, sat at the
container's 1,024-descriptor soft cap, and every boot fan-out (the forge
probe of every card, the mirror warm-up) failed for two minutes with "too
many open files" — git spawns refused, secret files read as corrupt,
mirrors deleted and re-cloned, the health probe's accept() resetting behind
the proxy. httpx reaps an expired keep-alive only on that client's next
request, so 325 pools never drained.

Now every adapter without an injected transport shares ONE client per
timeout value, with bounded limits (`SHARED_LIMITS`). Authentication rides
per request as headers, so nothing about a connection is per-card; the
pool is keyed by origin inside httpx. An adapter built with a `transport`
(tests, ad-hoc probes) keeps a private client. `PooledClient.aclose()`
closes a private client only; the shared clients outlive adapters and are
closed once at app shutdown (`aclose_shared`). Special-purpose calls (large
asset downloads with redirects, long-timeout uploads) stay per-call.
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


# Bounds for the process-wide pool: enough parallelism for a poll cycle's
# fan-out, a keep-alive set that fits any descriptor cap by a wide margin,
# and an expiry short enough that an idle set drains between cycles.
SHARED_LIMITS = httpx.Limits(max_connections=64, max_keepalive_connections=16,
                             keepalive_expiry=15.0)
_SHARED: dict[float, httpx.AsyncClient] = {}       # keyed by timeout


def shared_client(timeout: float) -> httpx.AsyncClient:
    """The process-wide client for this timeout (created lazily; a closed
    one is replaced — the lifespan closes them at shutdown, tests too)."""
    client = _SHARED.get(timeout)
    # getattr: a test double standing in for httpx.AsyncClient may not
    # carry is_closed; treat it as open, exactly as the per-adapter client did
    if client is None or getattr(client, "is_closed", False):
        client = httpx.AsyncClient(timeout=timeout, limits=SHARED_LIMITS)
        _SHARED[timeout] = client
    return client


async def aclose_shared() -> None:
    """Close every shared client (app shutdown)."""
    clients = list(_SHARED.values())
    _SHARED.clear()
    for client in clients:
        if not getattr(client, "is_closed", False):
            close = getattr(client, "aclose", None)
            if close is not None:
                await close()


class PooledClient:
    """The adapter-side seam: `get()` hands the shared client back unless
    this adapter was built with its own transport."""

    def __init__(self, *, timeout: float = 20,
                 transport: "httpx.AsyncBaseTransport | None" = None):
        self._timeout = timeout
        self._transport = transport
        self._client: httpx.AsyncClient | None = None

    @property
    def shared(self) -> bool:
        return self._transport is None

    def get(self) -> httpx.AsyncClient:
        if self.shared:
            return shared_client(self._timeout)
        if self._client is None or getattr(self._client, "is_closed", False):
            self._client = httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport)
        return self._client

    async def aclose(self) -> None:
        # a private client is ours to close; the shared pool outlives us
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
