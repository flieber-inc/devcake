"""Feed-scan memo — decides WHEN a mission's feed is re-read, never WHAT it
says (ADR-0033 addendum: pending is always posted − receipts, recomputed
from the board; truncation is never memoized).

The discovery and merge sweeps derive a small state from a mission's whole
comment feed. Re-reading every labeled feed every poll cycle multiplies
vendor requests with board size (ADR-0040). A scan is reused while the
signals agree: the mission's `updated_at` is unchanged, DevCake itself has
not written to that feed since (our own posts bump a generation), and — on
a vendor whose `updated_at` does not move for comments
(`PMOCapabilities.updated_at_tracks_comments` False) — the scan is younger
than MAX_AGE, the safety rescan that catches a human's comment there. A
vendor that declares the capability needs no rescan: a comment moves the
mission, and the changed `updated_at` already misses the memo.
Process-local by construction: a restart or a config reload rescans.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from ..run import aware, utcnow

MAX_AGE = timedelta(minutes=5)


@dataclass
class _Entry:
    value: Any
    updated_at: Any
    scanned_at: datetime
    gen: int
    # the newest entry timestamp the scan folded (vendor clock) — what a
    # feed-changes witness compares its rows against
    feed_until: datetime | None = None


def feed_until(entries) -> datetime | None:
    """The newest entry timestamp a scan folded — the ONE place both feed
    scans compute it, so the witness compares like with like."""
    return max((aware(e.ts) for e in entries), default=None)


class FeedScanMemo:
    def __init__(self, clock: Callable[[], datetime] = utcnow,
                 max_age: timedelta | None = MAX_AGE) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._gen: dict[str, int] = {}
        self._clock = clock
        # None = no safety rescan: the vendor's `updated_at` covers comments
        self.max_age = max_age
        # feed-changes witness (docs/04 §1): the newest vendor change time
        # seen so far — the next read asks for changes after it; None until
        # the first cycle anchors it on the board's newest `updated_at`
        self.watermark: datetime | None = None
        # a permanent witness failure (a schema mismatch, never a transient)
        # latched until the next settings Save or restart, so it cannot
        # traceback every cycle
        self.delta_error: str | None = None
        # per mission, the newest feed change the witness has reported — the
        # floor of the next scan's `feed_until` (see `witnessed`)
        self._witnessed: dict[str, datetime] = {}

    @classmethod
    def for_pmo(cls, pmo: Any) -> "FeedScanMemo":
        """The memo shaped by the adapter's self-description: a vendor whose
        `updated_at` moves on every comment needs no safety rescan. A fake or
        a broken self-description keeps the conservative rescan."""
        try:
            tracks = bool(pmo.capabilities().updated_at_tracks_comments)
        except Exception:  # noqa: BLE001 — a missing/broken capability row keeps the rescan, never fails the manager
            tracks = False
        return cls(max_age=None if tracks else MAX_AGE)

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
        if self.max_age is not None \
                and self._clock() - e.scanned_at > self.max_age:
            return None
        return e.value

    def put(self, kind: str, mission: Any, value: Any, gen: int, *,
            feed_until: datetime | None = None,
            floor: datetime | None = None) -> None:
        """Store a scan taken while the generation was `gen` (captured
        BEFORE the await); a write that landed mid-scan moved the
        generation and the stale result is discarded. `feed_until` is the
        newest entry the scan folded (the module helper computes it);
        `floor` is `witnessed()` captured before the same await, and lifts
        `feed_until` to the newest change the witness reported."""
        if gen != self.generation(mission.pmo_id):
            return
        if floor is not None and (feed_until is None or floor > feed_until):
            feed_until = floor
        self._entries[(kind, mission.pmo_id)] = _Entry(
            value, getattr(mission, "updated_at", None), self._clock(), gen,
            feed_until)

    # ── the feed-changes witness ───────────────────────────────────────────

    def witnessed(self, pmo_id: str) -> datetime | None:
        """The newest feed change the witness has reported for a mission.
        A scan captures it BEFORE its await (like the generation) and passes
        it to `put` as the floor of its `feed_until`: an edit or a removal
        carries a change time no entry's creation time ever reaches, so
        without the floor the same scan would be popped on every cycle."""
        return self._witnessed.get(pmo_id)

    def anchor(self, ts: datetime | None, *, follow: bool = False) -> None:
        """The watermark starts at the board's newest change time — the
        vendor's clock, never this process's. With `follow` it also moves
        forward to it: while nothing is memoized nothing can be missed, and
        the first witness after an idle stretch must not cover the gap."""
        if ts is None:
            return
        ts = aware(ts)
        if self.watermark is None or (follow and ts > self.watermark):
            self.watermark = ts

    def advance(self, delta: Any) -> None:
        """Move the watermark to the newest change the read reported —
        also when the read was truncated: the entries it could not list
        are dropped by `reconcile`, and the next read must not re-fetch the
        same newest pages forever."""
        if getattr(delta, "newest", None) is not None:
            t = aware(delta.newest)
            self.watermark = t if self.watermark is None else max(self.watermark, t)

    def reconcile(self, missions, delta: Any) -> int:
        """Apply a feed-changes witness (docs/04 §1). An entry whose feed
        changed after its scan (a row newer than `feed_until`: a new
        comment, an edit, a removal) is dropped — the next scan pays the
        read, once: the change time is remembered as that mission's floor.
        An entry the witness leaves untouched is re-stamped to the
        mission's current `updated_at`, so a mission that changed for a
        label, status, or relation edit costs no feed read. The scan's age
        is NOT refreshed: a vendor that keeps the safety rescan keeps it,
        because the witness cannot list what the vendor never reports (a
        removed comment on some vendors). A truncated witness proves
        nothing: every entry is dropped. Returns how many entries whose
        mission had changed were kept — the reads the witness saved."""
        if getattr(delta, "truncated", False):
            self._entries.clear()
            return 0
        touched: dict[str, datetime] = {}
        for c in delta.changes:
            t = aware(c.changed_at)
            if c.pmo_id not in touched or t > touched[c.pmo_id]:
                touched[c.pmo_id] = t
        for pmo_id, t in touched.items():
            prev = self._witnessed.get(pmo_id)
            if prev is None or t > prev:
                self._witnessed[pmo_id] = t
        by_id = {m.pmo_id: m for m in missions}
        kept = 0
        for key, e in list(self._entries.items()):
            pmo_id = key[1]
            if e.gen != self.generation(pmo_id):
                continue                       # our own write: the next scan reads
            t = touched.get(pmo_id)
            if t is not None and (e.feed_until is None or t > e.feed_until):
                self._entries.pop(key, None)   # the feed changed after the scan
                continue
            m = by_id.get(pmo_id)
            if m is None:
                continue
            current = getattr(m, "updated_at", None)
            if e.updated_at != current:
                kept += 1
            e.updated_at = current
        return kept

    def retain(self, pmo_ids) -> None:
        """Evict memoized scans of missions outside the cycle's set (terminal
        or vanished missions never accumulate). Generations are kept: a
        generation that dropped back to zero could let a scan captured
        before a write land after it."""
        keep = set(pmo_ids)
        for key in [k for k in self._entries if k[1] not in keep]:
            self._entries.pop(key, None)
        for pmo_id in [p for p in self._witnessed if p not in keep]:
            self._witnessed.pop(pmo_id, None)

    def clear(self) -> None:
        self._entries.clear()
        self._gen.clear()
        self._witnessed.clear()
        self.watermark = None
        self.delta_error = None

    def __len__(self) -> int:
        return len(self._entries)
