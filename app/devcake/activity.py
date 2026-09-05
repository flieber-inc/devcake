"""In-flight registry — what the app is doing right now (docs/11 §0a).

The admin's status bar answers one question the health payload cannot:
"is it frozen, or waiting?" Health reports state after the fact (last
poll time, active runs); this registry reports the phases in flight — the
poll cycle, a board's segment, a mirror sync with its progress, a forge
sweep, a dispatch, a finalize, a steward launch, a budget wait, a config
save waiting out the cycle — each with its start time and, when the phase
has a natural bound, whether it is overdue. Honest by construction: a
phase appears only between a real start and a real finish, registered at
the same chokepoints as the tracing spans (alongside the span where one
exists), never inferred from timestamps. Process-local, no persistence,
no I/O.
"""
from __future__ import annotations

import asyncio
import math
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

RECENT = 12                 # finished phases kept for "last: … N s ago"
OVERDUE_FACTOR = 2.0        # elapsed > expect_s × factor ⇒ overdue
# Containers have no bound of their own — a poll cycle lasts as long as the
# dispatches, syncs and waits inside it. One is overdue only when its own
# bound has passed AND no later-started phase is still within its bound:
# while a child is working, the parent is working.
CONTAINERS = frozenset({"poll.cycle", "poll.instance"})
# A phase parked on the poll lock is not working inside the cycle — it is
# waiting for it — so it never shields the cycle (detail `state` value).
WAITING_FOR_CYCLE = "waiting for the poll cycle"


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _jsonable(v: Any) -> Any:
    if isinstance(v, float) and not math.isfinite(v):
        return str(v)                     # NaN/inf would 500 the endpoint
    if v is None or isinstance(v, (bool, int, float, str)):
        return v
    return str(v)


class Phase:
    """One phase in flight. `set(**detail)` updates progress fields (a
    mirror sync's done/total, a wait's seconds) — detail is data, never a
    second bookkeeping."""
    __slots__ = ("id", "kind", "subject", "started_mono", "started_at",
                 "expect_s", "detail", "error")

    def __init__(self, id: int, kind: str, subject: str, started_mono: float,
                 started_at: float, expect_s: float | None, detail: dict):
        self.id, self.kind, self.subject = id, kind, subject
        self.started_mono, self.started_at = started_mono, started_at
        self.expect_s, self.detail, self.error = expect_s, detail, None

    def set(self, **detail: Any) -> None:
        self.detail.update({k: _jsonable(v) for k, v in detail.items()})


class InFlight:
    """ONE per process (module-level `IN_FLIGHT`); clocks injectable for
    tests. asyncio-single-threaded like the rest of the app — the dict
    mutations are synchronous."""

    def __init__(self, *, clock=time.time, mono=time.monotonic):
        self._clock, self._mono = clock, mono
        self._items: dict[int, Phase] = {}
        self._recent: deque = deque(maxlen=RECENT)
        self._seq = 0
        self._born_at = clock()
        self._last_finished_at: float | None = None

    # ── registration ──
    def start(self, kind: str, subject: str = "", *,
              expect_s: float | None = None, **detail: Any) -> Phase:
        self._seq += 1
        p = Phase(self._seq, kind, subject, self._mono(), self._clock(),
                  expect_s, {k: _jsonable(v) for k, v in detail.items()})
        self._items[p.id] = p
        return p

    def finish(self, p: Phase, *, error: str | None = None) -> None:
        if self._items.pop(p.id, None) is None:
            return
        now, now_mono = self._clock(), self._mono()
        self._last_finished_at = now
        self._recent.appendleft({
            "kind": p.kind, "subject": p.subject,
            "ended_at": _iso(now),
            "elapsed_s": round(max(now_mono - p.started_mono, 0.0), 1),
            "error": error,
        })

    @contextmanager
    def phase(self, kind: str, subject: str = "", *,
              expect_s: float | None = None, **detail: Any):
        p = self.start(kind, subject, expect_s=expect_s, **detail)
        try:
            yield p
        except BaseException as e:
            # a budget-killed sweep arrives as a cancellation, not a timeout
            self.finish(p, error=("cancelled or timed out"
                                  if isinstance(e, asyncio.CancelledError)
                                  else type(e).__name__))
            raise
        else:
            self.finish(p)

    # ── the surface ──
    def snapshot(self) -> dict:
        now, now_mono = self._clock(), self._mono()
        ordered = sorted(self._items.values(), key=lambda x: x.started_mono)
        elapsed = {p.id: max(now_mono - p.started_mono, 0.0) for p in ordered}

        def own_overdue(p: Phase) -> bool:
            return bool(p.expect_s and elapsed[p.id] > p.expect_s * OVERDUE_FACTOR)

        def shielded(p: Phase) -> bool:
            return p.kind in CONTAINERS and any(
                q.started_mono > p.started_mono and q.kind not in CONTAINERS
                and q.expect_s and not own_overdue(q)
                and q.detail.get("state") != WAITING_FOR_CYCLE
                for q in ordered)

        items = []
        for p in ordered:
            items.append({
                "kind": p.kind, "subject": p.subject,
                "started_at": _iso(p.started_at),
                "elapsed_s": round(elapsed[p.id], 1),
                "expect_s": p.expect_s,
                "overdue": own_overdue(p) and not shielded(p),
                "detail": dict(p.detail),
            })
        idle_since = None
        if not items:
            idle_since = _iso(self._last_finished_at
                              if self._last_finished_at is not None
                              else self._born_at)
        return {"now": _iso(now), "items": items, "idle_since": idle_since,
                "recent": list(self._recent)}


IN_FLIGHT = InFlight()
