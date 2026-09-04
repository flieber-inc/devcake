"""Ingress poison accounting under transient PMO failures (ADR-0040, docs/15
§5): a finalize that fails because the tracker rate-limited or blinked is
weather — the entry stays pending for reclaim and never counts toward the
dead-letter threshold, unless it has been failing that way for a day."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

import devcake.adapters.redis.messaging as mm
from devcake.adapters.redis.messaging import Messaging
from devcake.ports.pmo import PMOTransient


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _messaging() -> tuple[Messaging, MagicMock]:
    redis = MagicMock()
    redis.xadd = AsyncMock()
    redis.xpending_range = AsyncMock(return_value=[])
    pipe = MagicMock()
    pipe.execute = AsyncMock()
    redis.pipeline = MagicMock(return_value=pipe)
    m = Messaging("redis://unused", "")
    m.redis = redis
    return m, redis


def _entry_id(age_s: float) -> str:
    return f"{int((time.time() - age_s) * 1000)}-0"


def test_entry_age_is_read_from_the_stream_id():
    assert mm._entry_age_seconds(_entry_id(90)) == pytest.approx(90, abs=1)
    assert mm._entry_age_seconds("garbage") is None
    assert mm._entry_age_seconds(None) is None


def test_transient_failure_leaves_a_young_entry_pending():
    m, redis = _messaging()
    run_coro(m._maybe_poison(_entry_id(3600), {"m": "{}"}, transient=True))
    redis.xpending_range.assert_not_called()      # delivery count irrelevant
    redis.xadd.assert_not_called()                # nothing dead-lettered


def test_transient_failure_dead_letters_past_the_age_ceiling():
    m, redis = _messaging()
    entry = _entry_id(mm.TRANSIENT_MAX_AGE_SECONDS + 60)
    run_coro(m._maybe_poison(entry, {"m": '{"run_id": "R", "kind": "k"}'},
                             transient=True))
    redis.xadd.assert_awaited_once()
    stream, fields = redis.xadd.await_args.args[:2]
    assert stream == mm.DEAD_STREAM
    assert "transient" in fields["reason"] and "3h" in fields["reason"]
    redis.pipeline.return_value.execute.assert_awaited_once()   # acked away


def test_permanent_failure_still_dead_letters_at_the_threshold():
    m, redis = _messaging()
    redis.xpending_range.return_value = [{"times_delivered": mm.POISON_DELIVERIES - 1}]
    run_coro(m._maybe_poison(_entry_id(10), {"m": "{}"}))
    redis.xadd.assert_not_called()
    redis.xpending_range.return_value = [{"times_delivered": mm.POISON_DELIVERIES}]
    run_coro(m._maybe_poison(_entry_id(10), {"m": "{}"}))
    redis.xadd.assert_awaited_once()
    assert redis.xadd.await_args.args[1]["reason"] == "poison message dead-lettered"


@pytest.mark.parametrize("exc, transient", [
    (PMOTransient("rate limited by tracker.example/user:u1", retry_after=3), True),
    (RuntimeError("bad payload"), False),
])
def test_spawned_finalize_reports_the_failure_kind_to_poison(exc, transient):
    m, _redis = _messaging()
    seen: list[dict] = []

    async def spy(entry_id, fields, **kw):
        seen.append({"entry_id": entry_id, **kw})
    m._maybe_poison = spy          # type: ignore[method-assign]

    async def handler(run_id, kind, payload):
        raise exc

    async def main():
        m._spawn_finalize("R-1", "run.artifacts", {}, ["1-0"], {"m": "{}"}, handler)
        await m._finalize_tasks["R-1"]
    run_coro(main())
    assert seen == [{"entry_id": "1-0", "transient": transient}]
    assert "1-0" not in m._inflight_entries
