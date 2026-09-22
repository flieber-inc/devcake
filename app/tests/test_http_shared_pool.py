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


# ── pool meter (ADR-0044 visibility) ─────────────────────────────────────────
# One /proc/net/tcp line: "sl local rem st ..." — rem port 01BB = 443.
_TCP_HDR = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def _tcp_line(idx, state):
    return (f"   {idx}: 0100007F:9C40 4EFB41AC:01BB {state} 00000000:00000000 "
            "00:00000000 00000000  1000        0 12345 1 0000000000000000 "
            "100 0 0 10 0\n")


def test_pool_report_counts_connections_and_sockets(tmp_path):
    """Sockets to :443 by state come from the process's /proc/net/tcp;
    `leaked_estimate` is what the kernel holds beyond what the pool knows."""
    from devcake import deadline
    from devcake.adapters.http import pool_report
    (tmp_path / "tcp").write_text(
        _TCP_HDR + "".join(_tcp_line(i, "01") for i in range(3))
        + "".join(_tcp_line(i, "08") for i in range(3, 5)))
    (tmp_path / "tcp6").write_text(_TCP_HDR)
    shared_client(20)                                # one client, no connections yet
    rep = pool_report(proc_net=tmp_path)
    assert rep["clients"] == 1
    assert rep["connections"] == 0
    assert rep["max_connections"] == 64
    assert rep["sockets"] == {"established": 3, "close_wait": 2}
    assert rep["leaked_estimate"] == 5
    assert rep["background"] == deadline.pending() == 0


def test_pool_report_without_proc_is_honest(tmp_path):
    from devcake.adapters.http import pool_report
    rep = pool_report(proc_net=tmp_path / "missing")
    assert rep["sockets"] is None
    assert rep["leaked_estimate"] is None
    assert rep["max_connections"] == 64


def test_pool_report_grades_the_leak_at_one_place(tmp_path):
    """The warning/critical grade is computed HERE and read by the status
    verb and the admin alert — never re-derived from raw counts."""
    from devcake.adapters.http import pool_report
    (tmp_path / "tcp6").write_text(_TCP_HDR)

    def grade(established, close_wait):
        (tmp_path / "tcp").write_text(
            _TCP_HDR + "".join(_tcp_line(i, "01") for i in range(established))
            + "".join(_tcp_line(100 + i, "08") for i in range(close_wait)))
        return pool_report(proc_net=tmp_path)["level"]

    assert grade(2, 0) == "ok"
    assert grade(0, 8) == "warning"          # dead sockets piling up
    assert grade(16, 0) == "warning"         # leaked beyond the pool
    assert grade(40, 10) == "critical"       # near the 64 cap
    assert pool_report(proc_net=tmp_path / "missing")["level"] == "unknown"
