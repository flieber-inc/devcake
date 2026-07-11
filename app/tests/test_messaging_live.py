"""INV-4 (Devs talk only through the scoped Redis channel) + docs/09 delivery
semantics — runs against the live compose redis (in-container suite)."""
import asyncio
import json
import os
import uuid

import pytest
import redis.asyncio as aioredis
from redis.exceptions import AuthenticationError, NoPermissionError

from devcake.messaging import INGRESS, Messaging

REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")
PASSWORD = os.environ.get("REDIS_PASSWORD", "")


@pytest.fixture()
def msg():
    return Messaging(REDIS_URL, PASSWORD)


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def test_acl_isolation_inv4(msg):
    async def main():
        rid_a, rid_b = f"T-{uuid.uuid4().hex[:6]}", f"T-{uuid.uuid4().hex[:6]}"
        pw_a = await msg.create_run_user(rid_a)
        pw_b = await msg.create_run_user(rid_b)
        await msg.reply(rid_a, "runspec.result", {"secret": "A"})
        # B can read its own reply stream but NOT A's
        rb = aioredis.from_url(REDIS_URL, username=f"dev-{rid_b}", password=pw_b,
                               decode_responses=True)
        with pytest.raises(NoPermissionError):
            await rb.xrange(f"devcake:reply:{rid_a}")
        # B cannot use a wrong password at all
        with pytest.raises(AuthenticationError):
            bad = aioredis.from_url(REDIS_URL, username=f"dev-{rid_b}", password="wrong",
                                    decode_responses=True)
            await bad.ping()
        await rb.aclose()
        await msg.delete_run_user(rid_a)
        await msg.delete_run_user(rid_b)
        await msg.delete_reply_stream(rid_a)
        await msg.delete_reply_stream(rid_b)
    run(main())


def test_forged_auth_dropped(msg):
    async def main():
        seen = []

        async def handler(run_id, kind, payload):
            seen.append((run_id, kind))

        def verify(run_id, auth):
            return auth == "correct-password"

        await msg.setup()
        env = {"v": 1, "run_id": "T-FORGE", "auth": "WRONG", "kind": "run.started",
               "payload": {}}
        entry = await msg.redis.xadd(INGRESS, {"m": json.dumps(env)})
        # consume just this entry through the internal handler path
        fields = {"m": json.dumps(env)}
        await msg._handle_entry(entry, fields, handler, verify)
        assert seen == []                     # forged message never reached the handler
    run(main())


def test_chunk_reassembly(msg):
    async def main():
        got = []

        async def handler(run_id, kind, payload):
            got.append(payload)

        blob = json.dumps({"result": {"outcome": "hello"}, "data": "x" * 900_000})
        parts = [blob[i:i + 400_000] for i in range(0, len(blob), 400_000)]
        for i, part in enumerate(parts, start=1):
            env = {"v": 1, "run_id": "T-CHUNK", "auth": "pw", "kind": "run.artifacts",
                   "payload": {"chunk": i, "of": len(parts), "data": part}}
            await msg._handle_entry(f"0-{i}", {"m": json.dumps(env)}, handler,
                                    lambda r, a: True)
        assert len(got) == 1 and got[0]["result"]["outcome"] == "hello"
        assert len(got[0]["data"]) == 900_000
    run(main())


def test_poison_after_five_deliveries(msg, monkeypatch):
    # hermetic stream: the live app consumes the real ingress concurrently
    import devcake.messaging as mm
    test_stream = f"devcake:test:ingress:{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(mm, "INGRESS", test_stream)

    async def main():
        env = {"v": 1, "run_id": "T-POISON", "auth": "x", "kind": "boom", "payload": {}}
        entry = await msg.redis.xadd(test_stream, {"m": json.dumps(env)})
        await msg.redis.xgroup_create(test_stream, "app", id="0")
        await msg.redis.xreadgroup("app", "app-1", {test_stream: ">"}, count=10)
        for _ in range(5):
            await msg.redis.xclaim(test_stream, "app", "app-1", 0, [entry])
        before = await msg.redis.xlen("devcake:dead")
        await msg._maybe_poison(entry, {"m": json.dumps(env)})
        after = await msg.redis.xlen("devcake:dead")
        assert after == before + 1
        pending = await msg.redis.xpending_range(test_stream, "app", entry, entry, count=1)
        assert not pending                    # acked away from the group
        await msg.redis.delete(test_stream)
    run(main())
