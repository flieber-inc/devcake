"""INV-4 (Devs talk only through the scoped Redis channel) + docs/09 delivery
semantics — runs against the live compose redis (in-container suite)."""
import asyncio
import hashlib
import json
import os
import uuid

import pytest
import redis.asyncio as aioredis
from redis.exceptions import AuthenticationError, NoPermissionError

from devcake.adapters.redis.messaging import INGRESS, Messaging

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
        # app-owned key namespace (runspec secrets are built on request now,
        # but the ACL must still fence Devs out of app keys)
        await msg.redis.set(f"devcake:runspec-secret:{rid_a}", "app-owned", ex=60)
        await msg.reply(rid_a, "runspec.result", {"secret": "A"})
        # B can read its own reply stream but NOT A's
        rb = aioredis.from_url(REDIS_URL, username=f"dev-{rid_b}", password=pw_b,
                               decode_responses=True)
        with pytest.raises(NoPermissionError):
            await rb.xrange(f"devcake:reply:{rid_a}")
        with pytest.raises(NoPermissionError):
            await rb.get(f"devcake:runspec-secret:{rid_a}")
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


def test_valid_envelope_auth_is_scrubbed_from_payload(msg):
    async def main():
        seen = []
        credential = "relay-password-long-enough-to-redact"

        async def handler(run_id, kind, payload):
            seen.append(payload)

        env = {"v": 1, "run_id": "T-SCRUB", "auth": credential,
               "kind": "run.log", "payload": {"lines": [f"leak {credential}"]}}
        entry = await msg.redis.xadd(INGRESS, {"m": json.dumps(env)})
        await msg._handle_entry(entry, {"m": json.dumps(env)}, handler,
                                lambda _r, auth: auth == credential)
        assert credential not in str(seen)
        assert "REDACTED" in str(seen)
    run(main())


def test_chunk_reassembly(msg):
    async def main():
        got = []

        async def handler(run_id, kind, payload):
            got.append(payload)

        blob = json.dumps({"result": {"outcome": "hello"}, "data": "x" * 900_000})
        parts = [blob[i:i + 400_000] for i in range(0, len(blob), 400_000)]
        digest = hashlib.sha256(blob.encode()).hexdigest()
        chunk_id = uuid.uuid4().hex
        for i, part in enumerate(parts, start=1):
            env = {"v": 1, "run_id": "T-CHUNK", "auth": "pw", "kind": "run.artifacts",
                   "payload": {"chunk": i, "of": len(parts), "chunk_id": chunk_id,
                               "sha256": digest, "data": part}}
            await msg._handle_entry(f"0-{i}", {"m": json.dumps(env)}, handler,
                                    lambda r, a: True)
        assert len(got) == 1 and got[0]["result"]["outcome"] == "hello"
        assert len(got[0]["data"]) == 900_000
    run(main())


def test_poison_after_five_deliveries(msg, monkeypatch):
    # hermetic stream: the live app consumes the real ingress concurrently
    import devcake.adapters.redis.messaging as mm
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
        _dead_id, dead_fields = (await msg.redis.xrevrange("devcake:dead", count=1))[0]
        dead_body = json.loads(dead_fields["m"])
        assert "auth" not in dead_body and "payload" not in dead_body
        assert dead_body["payload_keys"] == []
        pending = await msg.redis.xpending_range(test_stream, "app", entry, entry, count=1)
        assert not pending                    # acked away from the group
        await msg.redis.delete(test_stream)
    run(main())


def test_poison_handles_malformed_body(msg, monkeypatch):
    """A non-JSON (or missing) 'm' field must still dead-letter — the old code
    raised inside the poison handler and redelivered the entry forever."""
    import devcake.adapters.redis.messaging as mm
    test_stream = f"devcake:test:ingress:{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(mm, "INGRESS", test_stream)

    async def main():
        for fields in ({"m": "not-json{{"}, {"x": "1"}):
            entry = await msg.redis.xadd(test_stream, fields)
            try:
                await msg.redis.xgroup_create(test_stream, "app", id="0")
            except Exception:
                pass
            await msg.redis.xreadgroup("app", "app-1", {test_stream: ">"}, count=10)
            for _ in range(5):
                await msg.redis.xclaim(test_stream, "app", "app-1", 0, [entry])
            before = await msg.redis.xlen("devcake:dead")
            await msg._maybe_poison(entry, fields)
            assert await msg.redis.xlen("devcake:dead") == before + 1
            _id, dead_fields = (await msg.redis.xrevrange("devcake:dead", count=1))[0]
            dead_body = json.loads(dead_fields["m"])
            assert "auth" not in dead_body and "payload" not in dead_body
            assert dead_body["payload_keys"] == [] and dead_body["run_id"] == ""
            pending = await msg.redis.xpending_range(test_stream, "app",
                                                     entry, entry, count=1)
            assert not pending                # acked — never redelivered again
        await msg.redis.delete(test_stream)
    run(main())


def _chunk_env(run_id, chunk, of, chunk_id, digest, data):
    return {"v": 1, "run_id": run_id, "auth": "pw", "kind": "run.artifacts",
            "payload": {"chunk": chunk, "of": of, "chunk_id": chunk_id,
                        "sha256": digest, "data": data}}


def test_active_chunk_group_survives_poison_threshold(msg, monkeypatch):
    """Buffered chunks stay pending by design (restart recovery), so a slow
    upload accumulates deliveries — a still-progressing group must never be
    dead-lettered, and must complete once the last chunk lands."""
    import devcake.adapters.redis.messaging as mm
    test_stream = f"devcake:test:ingress:{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(mm, "INGRESS", test_stream)

    async def main():
        got = []

        async def handler(run_id, kind, payload):
            got.append(payload)

        rid = f"T-{uuid.uuid4().hex[:6]}"
        blob = json.dumps({"result": {"outcome": "hello"}, "data": "x" * 1000})
        half = len(blob) // 2
        digest = hashlib.sha256(blob.encode()).hexdigest()
        chunk_id = uuid.uuid4().hex
        env1 = _chunk_env(rid, 1, 2, chunk_id, digest, blob[:half])
        entry1 = await msg.redis.xadd(test_stream, {"m": json.dumps(env1)})
        await msg.redis.xgroup_create(test_stream, "app", id="0")
        await msg.redis.xreadgroup("app", "app-1", {test_stream: ">"}, count=10)
        await msg._handle_entry(entry1, {"m": json.dumps(env1)}, handler,
                                lambda r, a: True)
        for _ in range(5):
            await msg.redis.xclaim(test_stream, "app", "app-1", 0, [entry1])
        before = await msg.redis.xlen("devcake:dead")
        await msg._maybe_poison(entry1, {"m": json.dumps(env1)})
        assert await msg.redis.xlen("devcake:dead") == before      # deferred
        assert (rid, "run.artifacts", chunk_id) in msg._chunks
        pending = await msg.redis.xpending_range(test_stream, "app",
                                                 entry1, entry1, count=1)
        assert pending                       # still pending: recovery intact

        env2 = _chunk_env(rid, 2, 2, chunk_id, digest, blob[half:])
        entry2 = await msg.redis.xadd(test_stream, {"m": json.dumps(env2)})
        await msg.redis.xreadgroup("app", "app-1", {test_stream: ">"}, count=10)
        await msg._handle_entry(entry2, {"m": json.dumps(env2)}, handler,
                                lambda r, a: True)
        assert len(got) == 1 and got[0]["result"]["outcome"] == "hello"
        assert (rid, "run.artifacts", chunk_id) not in msg._chunks
        await msg.redis.delete(test_stream)
    run(main())


def test_stalled_chunk_group_is_poisoned(msg, monkeypatch):
    """A group with no NEW chunk for CHUNK_GROUP_STALL_SECONDS is dead — the
    whole group dead-letters and every buffered entry is acked."""
    import time as _time
    import devcake.adapters.redis.messaging as mm
    test_stream = f"devcake:test:ingress:{uuid.uuid4().hex[:6]}"
    monkeypatch.setattr(mm, "INGRESS", test_stream)

    async def main():
        rid = f"T-{uuid.uuid4().hex[:6]}"
        blob = json.dumps({"data": "x" * 1000})
        digest = hashlib.sha256(blob.encode()).hexdigest()
        chunk_id = uuid.uuid4().hex
        env1 = _chunk_env(rid, 1, 2, chunk_id, digest, blob[:100])
        entry1 = await msg.redis.xadd(test_stream, {"m": json.dumps(env1)})
        await msg.redis.xgroup_create(test_stream, "app", id="0")
        await msg.redis.xreadgroup("app", "app-1", {test_stream: ">"}, count=10)
        await msg._handle_entry(entry1, {"m": json.dumps(env1)},
                                lambda *a: None, lambda r, a: True)
        key = (rid, "run.artifacts", chunk_id)
        msg._chunks[key]["last_progress"] = _time.monotonic() - 10_000
        for _ in range(5):
            await msg.redis.xclaim(test_stream, "app", "app-1", 0, [entry1])
        before = await msg.redis.xlen("devcake:dead")
        await msg._maybe_poison(entry1, {"m": json.dumps(env1)})
        assert await msg.redis.xlen("devcake:dead") == before + 1
        _id, dead_fields = (await msg.redis.xrevrange("devcake:dead", count=1))[0]
        assert json.loads(dead_fields["chunk_group"]) == list(key)
        assert key not in msg._chunks
        pending = await msg.redis.xpending_range(test_stream, "app",
                                                 entry1, entry1, count=1)
        assert not pending
        await msg.redis.delete(test_stream)
    run(main())


def test_run_user_password_registered_for_redaction(msg):
    """The relay credential must be masked in PMO-bound text while the run is
    live (regression: the redact hook went dead when redis_password left the
    Run record)."""
    from devcake.security import redact

    async def main():
        rid = f"T-{uuid.uuid4().hex[:6]}"
        pw = await msg.create_run_user(rid)
        assert pw not in redact(f"leak {pw} end")
        await msg.delete_run_user(rid)
        assert pw in redact(f"leak {pw} end")
    run(main())
