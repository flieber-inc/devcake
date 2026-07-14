"""ExecutorPort — how the app triggers and controls Dev runs (docs/13 §4).

One adapter today: adapters.dagu.DaguExecutor. A second (in-memory) adapter
lives in tests — two adapters justify the seam.
"""

from __future__ import annotations

from typing import Any, Protocol


class ExecutorPort(Protocol):
    async def start(self, params: dict[str, str], dag_run_id: str) -> None: ...
    async def stop(self, dag_run_id: str) -> bool: ...
    async def status(self, dag_run_id: str) -> dict[str, Any] | None: ...
    async def node_errors(self, dag_run_id: str) -> list[dict[str, str]]: ...
