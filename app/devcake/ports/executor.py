"""ExecutorPort — how the app triggers and controls Dev runs (docs/13 §4).

One adapter today: adapters.dagu.DaguExecutor. A second (in-memory) adapter
lives in tests — two adapters justify the seam.

Adapters must never leak httpx (or other wire-client) types upward —
surface `ExecutorError` / `DuplicateRun` instead.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExecutorError(Exception):
    """Raised by ExecutorPort adapters for HTTP-level failures (docs/13 §4).

    `status` carries the HTTP status code when one exists. Network failures
    use status=None. Adapters must never leak httpx exceptions upward.
    """

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class DuplicateRun(ExecutorError):
    """The executor already has a run for this dagRunId (Dagu 409)."""

    def __init__(self, dag_run_id: str):
        super().__init__(f"duplicate dagRunId {dag_run_id}", status=409)
        self.dag_run_id = dag_run_id


class ExecutorPort(Protocol):
    async def start(self, params: dict[str, str], dag_run_id: str) -> None: ...
    async def stop(self, dag_run_id: str) -> bool: ...
    async def status(self, dag_run_id: str) -> dict[str, Any] | None: ...
    async def node_errors(self, dag_run_id: str) -> list[dict[str, str]]: ...
