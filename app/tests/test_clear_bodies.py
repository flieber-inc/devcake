"""clear_* subsystem bodies.

`clear_redis` runs against live Redis (ACL SETUSER/DELUSER, XTRIM).
`clear_dagu` and `clear_openobserve` run the function body against
httpx.MockTransport — they pin call order (stop-all before DELETE; skip
metadata streams), not Dagu settle / the SIGTERM-vs-ACL-DELUSER race.
That composed hazard is still an order test in test_clear.py with fakes.
"""

import asyncio

import httpx

from devcake.api import clear as clear_mod


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── clear_dagu (real body, MockTransport) ────────────────────────────────────


def test_clear_dagu_stops_then_deletes_every_run(monkeypatch):
    from devcake.adapters.dagu import DaguExecutor
    from devcake.adapters.dagu.executor import DAG_NAME

    state = {"runs": ["r-1", "r-2", "r-3"], "stopped": False, "deleted": []}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST" and path.endswith(f"/dags/{DAG_NAME}/stop-all"):
            state["stopped"] = True
            return httpx.Response(200, json={"errors": []})
        if req.method == "GET" and path.endswith(f"/dag-runs/{DAG_NAME}"):
            return httpx.Response(200, json={
                "dagRuns": [{"dagRunId": r} for r in state["runs"]],
                "nextCursor": None})
        if req.method == "DELETE" and "/dag-runs/" in path:
            rid = path.rsplit("/", 1)[-1]
            state["deleted"].append(rid)
            return httpx.Response(204)
        return httpx.Response(404)

    ex = DaguExecutor(base_url="http://dagu:8080",
                      transport=httpx.MockTransport(handler))
    out = run(clear_mod.clear_dagu(ex))   # real ~1.5s settle sleep (harmless)
    assert state["stopped"] is True, "must stop in-flight Devs BEFORE deleting"
    assert sorted(state["deleted"]) == ["r-1", "r-2", "r-3"]
    assert out == {"stop_errors": [], "listed": 3, "deleted": 3, "failed": []}


def test_clear_dagu_second_pass_retries_still_settling_runs(monkeypatch):
    from devcake.adapters.dagu import DaguExecutor
    from devcake.adapters.dagu.executor import DAG_NAME

    attempts = {"r-slow": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if req.method == "POST":
            return httpx.Response(200, json={"errors": []})
        if req.method == "GET" and path.endswith(f"/dag-runs/{DAG_NAME}"):
            return httpx.Response(200, json={
                "dagRuns": [{"dagRunId": "r-slow"}], "nextCursor": None})
        if req.method == "DELETE":
            attempts["r-slow"] += 1
            # 400 (still running) on the first pass, 204 on the retry
            return httpx.Response(204 if attempts["r-slow"] > 1 else 400,
                                  text="still running")
        return httpx.Response(404)

    ex = DaguExecutor(base_url="http://dagu:8080",
                      transport=httpx.MockTransport(handler))
    out = run(clear_mod.clear_dagu(ex))
    assert attempts["r-slow"] == 2 and out["deleted"] == 1 and out["failed"] == []


# ── clear_openobserve (real body, MockTransport) ─────────────────────────────


def test_clear_openobserve_deletes_data_streams_spares_metadata(monkeypatch):
    monkeypatch.setattr(clear_mod, "OO_URL", "http://oo:5080")
    monkeypatch.setattr(clear_mod, "_oo_auth_header", lambda: "Basic x")
    deleted = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "GET" and req.url.path.endswith("/streams"):
            return httpx.Response(200, json={"list": [
                {"name": "traces", "stream_type": "traces"},
                {"name": "app_logs", "stream_type": "logs"},
                {"name": "_index", "stream_type": "metadata"},
            ]})
        if req.method == "DELETE":
            deleted.append(req.url.path.rsplit("/", 1)[-1] + ":"
                           + req.url.params.get("type", ""))
            return httpx.Response(200)
        return httpx.Response(404)

    real_client = httpx.AsyncClient

    def _client(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real_client(*a, **k)

    monkeypatch.setattr(clear_mod.httpx, "AsyncClient", _client)
    out = run(clear_mod.clear_openobserve())
    assert sorted(out["deleted"]) == ["app_logs:logs", "traces:traces"]
    assert "_index:metadata" not in deleted, "metadata streams must be spared"
    assert out["errors"] == []


# ── clear_redis (real body, LIVE compose redis) ──────────────────────────────


def test_clear_redis_revokes_dev_acl_users_and_drains(monkeypatch):
    """The incident logic itself: bulk `dev-*` revoke (with ACL SAVE via
    Messaging.revoke_leftover_run_users) + reply-stream drain + ingress trim,
    run against the live redis. (test_clear.py pins the ORDER that keeps this
    from racing the SIGTERM grace; this pins the BODY. Hermetic ACL SAVE
    ordering is test_messaging_acl_durability.py.)"""
    import os
    import uuid

    from devcake.adapters.redis import INGRESS, Messaging

    url = os.environ.get("REDIS_URL", "redis://redis:6379/0")
    pw = os.environ.get("REDIS_PASSWORD", "")
    msg = Messaging(url, pw)
    tag = uuid.uuid4().hex[:6]
    reply_key = f"devcake:reply:test-{tag}"
    user = f"dev-test-{tag}"

    async def main():
        r = msg.redis
        await r.xadd(reply_key, {"m": "secret-bearing reply"})
        await r.xadd(INGRESS, {"m": "backlog"})
        await r.execute_command("ACL", "SETUSER", user, "on", ">pw", "+ping")
        msg._chunks[("x", "y", "z")] = {"parts": {}}

        out = await clear_mod.clear_redis(msg)

        assert await r.exists(reply_key) == 0, "reply stream (secrets) drained"
        assert await r.xlen(INGRESS) == 0, "ingress trimmed"
        users = await r.execute_command("ACL", "USERS")
        names = [u.decode() if isinstance(u, (bytes, bytearray)) else str(u)
                 for u in users]
        assert user not in names, "leftover per-run ACL user revoked"
        assert msg._chunks == {}, "in-process chunk reassembly cleared"
        assert out["acl_users_deleted"] >= 1 and out["ingress_trimmed"] is True

    try:
        run(main())
    finally:
        # never leave the test user behind if an assert tripped mid-body
        run(_best_effort_deluser(msg, user))


async def _best_effort_deluser(msg, user):
    try:
        await msg.redis.execute_command("ACL", "DELUSER", user)
    except Exception:  # noqa: BLE001 — teardown only
        pass
