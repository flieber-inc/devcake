"""adapters/http: one shared, bounded HTTP pool for the process (2026-09
field incident: one pool per repository card held 984 idle sockets against
a 1,024-descriptor cap, and every boot fan-out failed with EMFILE)."""
import asyncio

import httpx

from devcake.adapters import http as http_mod
from devcake.adapters.http import PooledClient, SHARED_LIMITS, aclose_shared, shared_client


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def setup_function(_fn):
    run(aclose_shared())


def test_adapters_without_a_transport_share_one_client_per_timeout():
    a, b, c = PooledClient(timeout=20), PooledClient(timeout=20), PooledClient(timeout=30)
    assert a.shared and b.shared and c.shared
    assert a.get() is b.get()                       # one pool, many adapters
    assert a.get() is not c.get()                   # timeouts keep their own
    assert a.get() is shared_client(20)
    assert len(http_mod._SHARED) == 2


def test_shared_client_is_bounded():
    client = shared_client(20)
    pool = client._transport._pool                  # httpcore's pool
    assert pool._max_connections == SHARED_LIMITS.max_connections == 64
    assert pool._max_keepalive_connections == SHARED_LIMITS.max_keepalive_connections == 16
    assert pool._keepalive_expiry == SHARED_LIMITS.keepalive_expiry == 15.0


def test_injected_transport_keeps_a_private_client():
    seen = []

    def handler(req):
        seen.append(req.url.path)
        return httpx.Response(200, json={})
    private = PooledClient(timeout=20, transport=httpx.MockTransport(handler))
    assert not private.shared
    assert private.get() is not shared_client(20)
    run(private.get().get("http://x.invalid/ping"))
    assert seen == ["/ping"]
    run(private.aclose())
    assert private.get().is_closed is False         # a closed private client is replaced lazily


def test_aclose_on_a_sharing_adapter_never_closes_the_pool():
    a, b = PooledClient(timeout=20), PooledClient(timeout=20)
    client = a.get()
    run(a.aclose())
    assert not client.is_closed
    assert b.get() is client
    run(aclose_shared())
    assert client.is_closed
    assert shared_client(20) is not client          # replaced on next use


def test_close_adapters_on_rebuild_leaves_the_pool_alive():
    """ForgeRuntime.rebuild fire-and-forget-closes the outgoing adapters
    (F16): with a shared pool that must be a no-op for the pool itself."""
    class Adapter:
        def __init__(self):
            self._http = PooledClient(timeout=20)

        async def aclose(self):
            await self._http.aclose()
    adapters = [Adapter() for _ in range(5)]
    client = adapters[0]._http.get()

    async def scenario():
        http_mod.aclose_adapters(adapters)
        await asyncio.sleep(0)                      # let the close tasks run
        await asyncio.sleep(0)
    run(scenario())
    assert not client.is_closed
