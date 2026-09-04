"""Feed-scan memo — decides WHEN a mission's feed is re-read, never WHAT it
says (ADR-0033 addendum: pending is always posted − receipts, recomputed
from the board; truncation is never memoized).

The discovery and merge sweeps derive a small state from a mission's whole
comment feed. Re-reading every labeled feed every poll cycle multiplies
vendor requests with board size (ADR-0040). A scan is reused while three
signals agree: the mission's `updated_at` is unchanged, DevCake itself has
not written to that feed since (our own posts bump a generation), and the
scan is younger than MAX_AGE — the safety rescan that catches a human's
comment on a vendor whose `updated_at` does not move for comments.
Process-local by construction: a restart or a config reload rescans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..run import utcnow

MAX_AGE = timedelta(minutes=5)


@dataclass
class _Entry:
    value: Any
    updated_at: Any
    scanned_at: datetime
    gen: int


class FeedScanMemo:
    def __init__(self, clock: Callable[[], datetime] = utcnow,
                 max_age: timedelta = MAX_AGE) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._gen: dict[str, int] = {}
        self._clock = clock
        self.max_age = max_age

    def generation(self, pmo_id: str) -> int:
        return self._gen.get(pmo_id, 0)

    def forget(self, pmo_id: str) -> None:
        """DevCake wrote to this feed: every memoized scan of it is stale,
        and a scan in flight (started before the write) must not land."""
        self._gen[pmo_id] = self.generation(pmo_id) + 1
        for key in [k for k in self._entries if k[1] == pmo_id]:
            self._entries.pop(key, None)

    def get(self, kind: str, mission: Any) -> Any | None:
        """The memoized value, or None when a scan is due: never scanned,
        the feed was written by us since, the mission changed, or the scan
        is older than max_age."""
        e = self._entries.get((kind, mission.pmo_id))
        if e is None:
            return None
        if e.gen != self.generation(mission.pmo_id):
            return None
        if e.updated_at != getattr(mission, "updated_at", None):
            return None
        if self._clock() - e.scanned_at > self.max_age:
            return None
        return e.value

    def put(self, kind: str, mission: Any, value: Any, gen: int) -> None:
        """Store a scan taken while the generation was `gen` (captured
        BEFORE the await); a write that landed mid-scan moved the
        generation and the stale result is discarded."""
        if gen != self.generation(mission.pmo_id):
            return
        self._entries[(kind, mission.pmo_id)] = _Entry(
            value, getattr(mission, "updated_at", None), self._clock(), gen)

    def clear(self) -> None:
        self._entries.clear()
        self._gen.clear()

    def __len__(self) -> int:
        return len(self._entries)
