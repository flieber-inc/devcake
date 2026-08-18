"""ports/messaging.py: adapters must never leak redis-py types upward.

Protocol surface methods wrap redis.exceptions / OSError as MessagingError
so domain (RunBootstrap, RunManager) never types against redis.
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError

from devcake.adapters.redis.messaging import Messaging
from devcake.ports.messaging import MessagingError


def run_coro(c):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(c)
    finally:
        loop.close()


def _messaging_with_redis(redis) -> Messaging:
    m = Messaging("redis://unused", "")
    m.redis = redis
    return m


def test_create_run_user_connection_failure_is_messaging_error():
    redis = MagicMock()
    redis.execute_command = AsyncMock(
        side_effect=RedisConnectionError("Connection refused"))
    m = _messaging_with_redis(redis)

    with pytest.raises(MessagingError) as exc:
        run_coro(m.create_run_user("run-x"))
    assert not isinstance(exc.value, RedisConnectionError)
    assert "network" in str(exc.value).lower() or "connection" in str(exc.value).lower()


def test_create_run_user_acl_response_error_is_messaging_error():
    redis = MagicMock()
    redis.execute_command = AsyncMock(
        side_effect=ResponseError("ERR unknown command ACL"))
    m = _messaging_with_redis(redis)

    with pytest.raises(MessagingError) as exc:
        run_coro(m.create_run_user("run-y"))
    assert not isinstance(exc.value, ResponseError)


def test_reply_connection_failure_is_messaging_error():
    pipe = MagicMock()
    pipe.xadd = MagicMock()
    pipe.expire = MagicMock()
    pipe.execute = AsyncMock(side_effect=RedisConnectionError("gone"))

    redis = MagicMock()
    redis.pipeline = MagicMock(return_value=pipe)
    m = _messaging_with_redis(redis)

    with pytest.raises(MessagingError):
        run_coro(m.reply("run-z", "runspec.result", {"a": 1}))


def test_delete_run_user_connection_failure_is_messaging_error():
    redis = MagicMock()
    redis.execute_command = AsyncMock(
        side_effect=RedisConnectionError("gone"))
    m = _messaging_with_redis(redis)
    with pytest.raises(MessagingError):
        run_coro(m.delete_run_user("run-teardown"))


def test_unresolved_run_ids_connection_failure_is_messaging_error():
    redis = MagicMock()
    redis.xrange = AsyncMock(side_effect=RedisConnectionError("gone"))
    m = _messaging_with_redis(redis)

    with pytest.raises(MessagingError):
        run_coro(m.unresolved_run_ids())
