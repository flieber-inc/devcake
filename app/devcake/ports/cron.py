"""CronStore — scheduled-task fire outcome ledger (ADR-0035).

Domain CronService derives degradation and the elapsed-interval window
from this port. Production wires adapters.files.CronStore; tests use
in-memory fakes. Advisory only (INV-1): wiping re-arms automatic fires.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol


class CronStore(Protocol):
    def record(self, job_id: str, outcome: str, *,
               fired_at: str | None = None) -> None:
        """Append one outcome (created/skipped/failed); keep last 3.
        When fired_at is set, stamp last_fire_at for the interval window."""
        ...

    def outcomes(self, job_id: str) -> list[str]:
        """Last up to 3 automatic (and re-arming Run-now) outcomes."""
        ...

    def last_fire_at(self, job_id: str) -> datetime | None:
        """Persisted window stamp, or None if missing/unparseable."""
        ...
