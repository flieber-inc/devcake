"""`GET /api/v1/activity` — what the app is doing right now (docs/11 §0).

A cheap, uncached read: the in-flight registry's snapshot plus the poll
runtime's transient skips. No probes, no tracker calls — the status bar
polls it every few seconds at no cost to anything metered.
"""
from __future__ import annotations

from ..activity import IN_FLIGHT, InFlight


def build_activity_payload(*, in_flight: InFlight = IN_FLIGHT,
                           poll_rt=None) -> dict:
    snap = in_flight.snapshot()
    snap["poll_skips"] = dict(getattr(poll_rt, "poll_skips", {}) or {})
    snap["last_poll_at"] = (
        poll_rt.last_poll_at.isoformat()
        if poll_rt is not None and getattr(poll_rt, "last_poll_at", None)
        else None)
    return snap
