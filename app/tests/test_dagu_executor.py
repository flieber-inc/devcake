"""Hermetic HTTP contract for DaguExecutor (URL + method + body).

Unit suite otherwise only uses FakeExecutor — a production start() no-op
would pass. MockTransport makes the real adapter path fail closed.
"""
from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from devcake.adapters.dagu.executor import DAG_NAME, DaguExecutor
from devcake.ports.executor import DuplicateRun


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _executor(handler) -> DaguExecutor:
    return DaguExecutor(
        "http://dagu.test:8080",
        transport=httpx.MockTransport(handler),
        auth=("", ""),
    )


def test_start_posts_dag_start_with_params_and_run_id():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.method == "POST"
        assert str(request.url) == (
            f"http://dagu.test:8080/api/v1/dags/{DAG_NAME}/start")
        body = json.loads(request.content.decode())
        assert body["dagRunId"] == "run-abc"
        assert json.loads(body["params"]) == {"IMAGE": "devcake/dev-hello:latest"}
        return httpx.Response(200, json={})

    run_coro(_executor(handler).start(
        {"IMAGE": "devcake/dev-hello:latest"}, "run-abc"))
    assert len(seen) == 1


def test_start_409_raises_duplicate_run():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "already_exists"})

    with pytest.raises(DuplicateRun):
        run_coro(_executor(handler).start({}, "dup-id"))


def test_status_get_path_and_404_is_none():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == (
            f"http://dagu.test:8080/api/v1/dag-runs/{DAG_NAME}/missing")
        return httpx.Response(404, json={})

    assert run_coro(_executor(handler).status("missing")) is None


def test_status_returns_json():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "running", "dagRunId": "r1"})

    body = run_coro(_executor(handler).status("r1"))
    assert body == {"status": "running", "dagRunId": "r1"}


def test_stop_posts_dag_run_stop():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            f"http://dagu.test:8080/api/v1/dag-runs/{DAG_NAME}/run-x/stop")
        return httpx.Response(200, json={})

    assert run_coro(_executor(handler).stop("run-x")) is True


def test_stop_returns_false_on_non_success_without_raising():
    """Operator/clear kill paths treat a failed stop as best-effort bool —
    they must not raise on 4xx/5xx (teardown continues to ACL revoke)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "not stoppable"})

    assert run_coro(_executor(handler).stop("busy")) is False


def test_stop_all_posts_dags_stop_all():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == (
            f"http://dagu.test:8080/api/v1/dags/{DAG_NAME}/stop-all")
        return httpx.Response(200, json={"errors": ["e1"]})

    assert run_coro(_executor(handler).stop_all()) == ["e1"]


def test_stop_all_404_is_empty_errors():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={})

    assert run_coro(_executor(handler).stop_all()) == []


def test_delete_accepts_204_and_404():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        rid = request.url.path.rsplit("/", 1)[-1]
        if rid == "gone":
            return httpx.Response(404)
        if rid == "ok":
            return httpx.Response(204)
        if rid == "running":
            return httpx.Response(400, text="still running")
        return httpx.Response(500, text="boom")

    ex = _executor(handler)
    assert run_coro(ex.delete("ok")) is True
    assert run_coro(ex.delete("gone")) is True
    assert run_coro(ex.delete("running")) is False
    with pytest.raises(httpx.HTTPStatusError):
        run_coro(ex.delete("boom"))


def test_list_all_run_ids_paginates():
    pages = {
        None: ({"dagRuns": [{"dagRunId": "a"}, {"dagRunId": "b"}],
                "nextCursor": "c1"}),
        "c1": ({"dagRuns": [{"dagRunId": "c"}], "nextCursor": None}),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        cursor = request.url.params.get("cursor")
        body = pages[cursor]
        return httpx.Response(200, json=body)

    assert run_coro(_executor(handler).list_all_run_ids()) == ["a", "b", "c"]


def test_start_must_issue_http_request():
    """Mutation bar: a no-op start() leaves seen empty → fail."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    run_coro(_executor(handler).start({"k": "v"}, "must-hit-http"))
    assert len(seen) == 1
    assert seen[0].method == "POST"


def test_start_sends_basic_auth():
    """Load-bearing Dagu API auth must appear on the wire."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={})

    ex = DaguExecutor(
        "http://dagu.test:8080",
        transport=httpx.MockTransport(handler),
        auth=("dagu-user", "dagu-secret"),
    )
    run_coro(ex.start({"x": "1"}, "auth-run"))
    assert len(seen) == 1
    # httpx encodes basic auth on the request
    auth = seen[0].headers.get("Authorization", "")
    assert auth.startswith("Basic ")
    import base64
    decoded = base64.b64decode(auth.split(" ", 1)[1]).decode()
    assert decoded == "dagu-user:dagu-secret"
