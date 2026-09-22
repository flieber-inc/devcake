"""Deadlines that never cancel a socket holder (ADR-0044).

An outbound HTTP coroutine is bounded only by httpx's own timeouts: those
raise ordinary exceptions and clean up their connection. A caller-side
deadline used to wrap such calls in `asyncio.timeout(...)`, and a
cancellation that lands during the TLS handshake leaks the TCP socket —
httpcore's `start_tls` cleans up on `Exception` only, and `CancelledError`
is not one. On a CPU-starved host the health builder cancelled dozens of
GitLab probes mid-handshake; the leaked sockets sat in CLOSE_WAIT and the
process-wide pool (64) refused every tracker and forge call for 40 minutes.

This module is the one sanctioned way to bound a wait: `bounded()` runs the
work as a task, waits on a *shielded* view of it for `timeout` seconds and,
on expiry, hands back `Bounded(done=False)` while the task finishes in the
background. Tasks are strongly referenced until done; a late exception is
retrieved and logged, never raised into nobody. `drain()` exists for
shutdown and tests only — it is the single place a pending task may be
cancelled, because the pool closes right after.

The structure guard `test_deadlines_never_cancel_a_socket_holder` keeps
`asyncio.timeout` / `asyncio.wait_for` out of the package except for the
allowlisted non-holders (subprocess, in-process queue, shutdown drain).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Coroutine

log = logging.getLogger("devcake.deadline")

# strong refs: the loop holds tasks weakly — a background task with no other
# reference could be garbage-collected mid-flight (same idiom as the shared
# pool's close tasks and security's alarm tasks)
_TASKS: set[asyncio.Task] = set()
# tasks a `bounded()` caller is waiting on right now: their failure reaches
# that caller, so the done callback must not report it a second time
_WAITED: set[asyncio.Task] = set()
# grace after the shutdown cancel: a task that swallows its cancellation
# must not hold the process; it is logged and left behind
DRAIN_GRACE_S = 2.0


@dataclass(frozen=True)
class Bounded:
    """Outcome of `bounded()`: `done` says whether the work finished inside
    the deadline; `value` is its result when it did; `task` is the still
    running (or finished) task either way, reusable for a later wait."""
    done: bool
    value: Any
    task: asyncio.Task


def _name_of(task: asyncio.Task) -> str:
    return task.get_name()


def _retire(task: asyncio.Task) -> None:
    _TASKS.discard(task)
    if task.cancelled():
        log.info("%s cancelled at shutdown", _name_of(task))
        return
    exc = task.exception()          # retrieved either way: never "never retrieved"
    if exc is not None and task not in _WAITED:
        log.warning("%s failed after its deadline: %s: %s",
                    _name_of(task), type(exc).__name__, exc)


def spawn(aw: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task:
    """Start `aw` as a strongly referenced background task. Its late outcome
    is retrieved by the done callback (logged on failure)."""
    task = asyncio.get_running_loop().create_task(aw, name=name)
    _TASKS.add(task)
    task.add_done_callback(_retire)
    return task


async def bounded(aw: Coroutine[Any, Any, Any] | asyncio.Task,
                  timeout: float, *, name: str = "") -> Bounded:
    """Wait at most `timeout` seconds for `aw` WITHOUT ever cancelling it.

    A coroutine is spawned (see `spawn`); a task is reused as-is, which is
    how a caller that missed the deadline waits on the same work next time
    (single-flight). Inside the deadline the task's result is returned or
    its exception re-raised, exactly as a plain `await` would; past it,
    `Bounded(done=False)` comes back and the task keeps running."""
    task = aw if isinstance(aw, asyncio.Task) else spawn(aw, name=name or "bounded")
    if task.done():
        return Bounded(True, task.result(), task)
    _WAITED.add(task)               # a failure inside the deadline is ours to raise
    try:
        value = await asyncio.wait_for(asyncio.shield(task), timeout)
    except TimeoutError:
        _WAITED.discard(task)       # from here on a failure is nobody's: logged
        return Bounded(False, None, task)
    return Bounded(True, value, task)


def reset() -> None:
    """Forget every task (tests only: a test's loop is gone by teardown)."""
    _TASKS.clear()
    _WAITED.clear()


def pending() -> int:
    """Background tasks still alive (exposed on /health.http_pool)."""
    return sum(1 for t in _TASKS if not t.done())


async def drain(timeout: float | None = None) -> int:
    """Wait for every pending task, at most `timeout` seconds, then cancel
    the leftovers. Returns how many were cancelled. Shutdown and tests only:
    the only sanctioned cancellation of a socket holder."""
    live = [t for t in _TASKS if not t.done()]
    if not live:
        return 0
    _done, left = await asyncio.wait(live, timeout=timeout)
    for t in left:
        t.cancel()
    if left:
        _done, stuck = await asyncio.wait(left, timeout=DRAIN_GRACE_S)
        for t in stuck:
            log.warning("%s still running after its cancel — left behind",
                        _name_of(t))
    return len(left)
