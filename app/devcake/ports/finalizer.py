"""RunFinalizer — mission-side finalization and restore (docs/04 §4).

Breaks the RunManager ↔ MissionManager concrete cycle: ingress and kill depend
only on this interface. MissionManager is the production adapter; tests inject
fakes (two adapters → real seam).
"""

from __future__ import annotations

from typing import Protocol

from ..domain.run import Run


class RunFinalizer(Protocol):
    async def finalize(self, run: Run, payload: dict) -> None: ...
    async def finalize_mapper(self, run: Run, payload: dict) -> None: ...
    async def restore_after_failure(self, run: Run) -> None: ...
    def runspec_secret_payload(self, run: Run) -> dict | None: ...
    async def activity_payload(self, pmo_id: str, kind: str = "issue") -> dict: ...
