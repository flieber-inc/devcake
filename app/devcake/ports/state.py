"""StatePort — run-record persistence (docs/10).

One adapter today: adapters.files.RunStore. In-memory fakes in tests are the
second adapter that justifies the seam.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.run import Run


class StatePort(Protocol):
    # Wipe-generation fence (docs/10 store_gen): domain call sites depend on
    # these members — declaring them keeps fakes from silently disabling the
    # anti-resurrection fence via missing attrs.
    wipe_generation: int

    def save(self, run: Run) -> None: ...
    def get(self, run_id: str) -> Run | None: ...
    def delete(self, run_id: str) -> None: ...
    def all(self) -> list[Run]: ...
    def active(self) -> list[Run]: ...
    def is_current_generation(self, run: Run) -> bool: ...
