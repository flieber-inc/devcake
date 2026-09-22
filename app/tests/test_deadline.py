"""`devcake.deadline` — the one sanctioned way to put a deadline on a
coroutine that may hold a network socket (ADR-0044). The rule under test:
an expired deadline NEVER cancels the awaitable; it returns `pending` and
the work finishes in the background, strongly referenced, its late outcome
retrieved and logged. Cancelling mid TLS handshake leaks the socket (httpcore
cleans up on `Exception` only), which is how the shared pool filled with
dead connections on a starved host."""

import asyncio
import logging

from devcake import deadline


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def test_expired_deadline_never_cancels_the_awaitable():
    seen = {}
    release = asyncio.Event()
    landed = {}

    async def slow():
        try:
            await release.wait()
        except asyncio.CancelledError:
            seen["cancelled"] = True
            raise
        landed["ok"] = True
        return "late"

    async def drive():
        res = await deadline.bounded(slow(), 0.01, name="slow")
        assert res.done is False
        assert res.value is None
        assert deadline.pending() == 1
        release.set()
        await deadline.drain()
        return res.task.result()

    assert run_coro(drive()) == "late"
    assert seen == {}
    assert landed == {"ok": True}
    assert deadline.pending() == 0


def test_result_inside_the_deadline_is_returned():
    async def quick():
        return 42

    async def drive():
        res = await deadline.bounded(quick(), 1.0)
        await deadline.drain()
        return res

    res = run_coro(drive())
    assert res.done is True
    assert res.value == 42


def test_exception_inside_the_deadline_propagates():
    async def boom():
        raise ValueError("nope")

    async def drive():
        try:
            await deadline.bounded(boom(), 1.0)
        except ValueError as e:
            await deadline.drain()
            return str(e)
        raise AssertionError("must raise")

    assert run_coro(drive()) == "nope"


def test_late_exception_is_logged_not_raised(caplog):
    release = asyncio.Event()

    async def late_boom():
        await release.wait()
        raise RuntimeError("after the deadline")

    async def drive():
        res = await deadline.bounded(late_boom(), 0.01, name="late_boom")
        assert res.done is False
        release.set()
        await deadline.drain()

    with caplog.at_level(logging.WARNING, logger="devcake.deadline"):
        run_coro(drive())
    assert any("late_boom" in r.getMessage() and "after its deadline" in r.getMessage()
               for r in caplog.records)
    assert deadline.pending() == 0


def test_a_task_is_reused_across_two_bounded_waits():
    """The single-flight shape (poll sweep): a caller that missed the
    deadline waits on the SAME task next time; the coroutine runs once."""
    runs = []
    release = asyncio.Event()

    async def once():
        runs.append(1)
        await release.wait()
        return "done"

    async def drive():
        task = deadline.spawn(once(), name="once")
        first = await deadline.bounded(task, 0.01)
        release.set()
        second = await deadline.bounded(task, 1.0)
        await deadline.drain()
        return first, second

    first, second = run_coro(drive())
    assert first.done is False and first.task is second.task
    assert second.done is True and second.value == "done"
    assert runs == [1]


def test_drain_cancels_only_past_its_budget():
    """Shutdown is the one place a pending socket holder may be cancelled —
    the pool closes right after. `drain(timeout)` waits that long, then
    cancels what is left and reports how many."""
    never = asyncio.Event()

    async def forever():
        await never.wait()

    async def drive():
        deadline.spawn(forever(), name="forever")
        cancelled = await deadline.drain(0.01)
        return cancelled

    assert run_coro(drive()) == 1
    assert deadline.pending() == 0
