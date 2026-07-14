"""StatePort — run-record persistence (docs/10).

One adapter today: adapters.files.RunStore. In-memory fakes in tests are the
second adapter that justifies the seam.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.run import Run


class StatePort(Protocol):
    def save(self, run: Run) -> None: ...
    def get(self, run_id: str) -> Run | None: ...
    def all(self) -> list[Run]: ...
    def active(self) -> list[Run]: ...
