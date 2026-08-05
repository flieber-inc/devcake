"""The ingress consumer survives a redis restart (2026-08 live drill).

The deploy drill measured the exact failure: `docker compose restart redis`
raised redis-py's ConnectionError out of the blocking XREADGROUP, the
consumer task died silently, and every later run.artifacts sat unconsumed —
runs parked in `running` forever while the aclfile durability work kept
their CREDENTIALS alive. Two pins here: the loop retries through connection
loss, and the exception classes are redis-py's own (they do NOT subclass
the builtins — a bare `except ConnectionError` misses them, measured)."""

import asyncio

from redis.exceptions import ConnectionError as RedisConnectionError

from devcake.adapters.redis.messaging import Messaging

_LOOP = asyncio.new_event_loop()
asyncio.set_event_loop(_LOOP)


def run_coro(c):
    return _LOOP.run_until_complete(c)


class RestartingRedis:
    """xreadgroup dies with redis-py's ConnectionError once (the restart),
    then delivers one entry; xgroup_create tracks the re-ensure calls."""

    def __init__(self):
        self.reads = 0
        self.group_creates = 0

    async def xgroup_create(self, *a, **kw):
        self.group_creates += 1

    async def xreadgroup(self, *a, **kw):
        # the real client always yields at the socket — a fake that returns
        # without yielding turns consume_forever into a starvation loop
        await asyncio.sleep(0)
        self.reads += 1
        if self.reads == 1:
            raise RedisConnectionError("Connection closed by server.")
        return []


def test_consumer_survives_a_redis_restart(monkeypatch):
    m = Messaging.__new__(Messaging)
    m.redis = RestartingRedis()
    m._chunks = {}

    # patching module-attr sleep patches the SHARED asyncio module, so the
    # replacement must still yield to the loop or nothing else ever runs
    real_sleep = asyncio.sleep

    async def instant_sleep(_s):
        await real_sleep(0)

    monkeypatch.setattr(
        "devcake.adapters.redis.messaging.asyncio.sleep", instant_sleep)

    async def run_three_iterations():
        task = asyncio.get_event_loop().create_task(
            m.consume_forever(handler=None, verify_auth=None))
        # yield until the loop has survived the error and read again
        for _ in range(50):
            await asyncio.sleep(0)
            if m.redis.reads >= 3:
                break
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    run_coro(run_three_iterations())
    assert m.redis.reads >= 3, "the loop must retry through ConnectionError"
    # setup() ran at start AND re-ran after the connection loss (the restart
    # may have lost an un-AOF'd group create)
    assert m.redis.group_creates >= 2


def test_redis_exception_classes_do_not_subclass_builtins():
    """The trap that would silently revert this fix in a refactor."""
    assert not issubclass(RedisConnectionError, ConnectionError)
