"""ports/executor.py: adapters must never leak httpx upward.

Parallel to forge F7 (test_forge_error_contract): Dagu REST is HTTP, so
network/status failures surface as ExecutorError. DuplicateRun stays the
409 already_exists signal (now defined on the port, not the adapter).
"""
from __future__ import annotations

import asyncio

import httpx
import pytest

from devcake.adapters.dagu.executor import DaguExecutor
from devcake.ports.executor import DuplicateRun, ExecutorError


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


def test_start_network_failure_is_executor_error_not_httpx():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ExecutorError) as exc:
        run_coro(_executor(handler).start({"IMAGE": "x"}, "run-1"))
    assert not isinstance(exc.value, httpx.HTTPError)
    assert exc.value.status is None
    assert "network" in str(exc.value).lower()


def test_start_http_5xx_is_executor_error_with_status():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="dagu unavailable")

    with pytest.raises(ExecutorError) as exc:
        run_coro(_executor(handler).start({"IMAGE": "x"}, "run-2"))
    assert exc.value.status == 503
    assert "503" in str(exc.value)


def test_start_409_remains_duplicate_run():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "already_exists"})

    with pytest.raises(DuplicateRun):
        run_coro(_executor(handler).start({}, "dup"))


def test_status_network_failure_is_executor_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with pytest.raises(ExecutorError) as exc:
        run_coro(_executor(handler).status("run-3"))
    assert exc.value.status is None


def test_stop_http_error_returns_false_without_leaking_httpx():
    """stop() is bool-success: HTTP errors become False, never httpx."""
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    assert run_coro(_executor(handler).stop("run-4")) is False


def test_duplicate_run_is_exported_from_ports():
    assert issubclass(DuplicateRun, ExecutorError)
