"""App side of the Redis Streams protocol (docs/09).

- ingress consumer group on devcake:ingress (at-least-once, XACK after handling)
- per-run reply streams devcake:reply:{run_id} (runspec/activity request-reply)
- per-run ACL users (docs/09 §1a): scoped credentials, revoked at finalization
- envelope auth verification (forgery guard) and chunked-payload reassembly
"""

import asyncio
import contextlib
import json
import hashlib
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis
from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode
from redis.exceptions import (ConnectionError as RedisConnectionError,
                              ResponseError,
                              TimeoutError as RedisTimeoutError)

from ...ports.messaging import MessagingError
from ...security import MASK, register_runtime_secret, unregister_runtime_secret

log = logging.getLogger("devcake.messaging")
tracer = trace.get_tracer("devcake")

# Wire failures that must never escape Protocol surface methods as redis types
_REDIS_WIRE = (RedisConnectionError, RedisTimeoutError, ResponseError, OSError)

INGRESS = "devcake:ingress"
# concurrent run.artifacts finalizes (audit F11) — bounded so a burst of
# completions cannot stampede the PMO/forge; heartbeats are not budgeted
FINALIZE_CONCURRENCY = 4
FINALIZE_KINDS = frozenset({"run.artifacts"})
GROUP = "app"
CONSUMER = "app-1"
REPLY_TTL_SECONDS = 900          # secret material must not linger (docs/09 §5)
MAX_CHUNKS = 128
MAX_ASSEMBLED_BYTES = 50 * 1024 * 1024
MAX_ACTIVE_CHUNK_GROUPS = 16
MAX_BUFFERED_CHUNK_BYTES = 100 * 1024 * 1024
# ADR-0018 — a single XADD is materialized in memory before any cap can look at
# it, so bound it on its own. The sender splits at CHUNK_SIZE (400 KB); this is
# a generous ceiling that only rejects a malformed or hostile payload.
MAX_CHUNK_BYTES = 2 * 1024 * 1024
RECLAIM_INTERVAL_SECONDS = 60
# buffered chunks stay pending by design (restart recovery), so deliveries
# accumulate on slow uploads — only a group with no NEW chunk for this long
# may be poisoned
CHUNK_GROUP_STALL_SECONDS = 5 * RECLAIM_INTERVAL_SECONDS
DEAD_STREAM = "devcake:dead"
DEAD_STREAM_MAXLEN = 1000

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]  # (run_id, kind, payload)


def reply_stream(run_id: str) -> str:
    return f"devcake:reply:{run_id}"


def chunk_complete_key(run_id: str, chunk_id: str) -> str:
    return f"devcake:chunk-complete:{run_id}:{chunk_id}"


DEFAULT_REDIS_URL = "redis://redis:6379/0"


def redis_connect_env() -> tuple[str, str]:
    """Resolve REDIS_URL / REDIS_PASSWORD at call time (not import time).

    Shared by the composition root and /health redis probe so the two sites
    cannot drift on defaults (ADR-0034 pinned mirror).
    """
    return (
        os.environ.get("REDIS_URL", DEFAULT_REDIS_URL),
        os.environ.get("REDIS_PASSWORD", ""),
    )


class Messaging:
    def __init__(self, url: str, password: str):
        self.redis = aioredis.from_url(url, password=password or None, decode_responses=True)
        self._chunks: dict[tuple[str, str, str], dict[str, Any]] = {}
        # finalize offload (2026-08-12 audit F11): the ingress consumer was
        # ONE serialized loop, so two slow finalizes against a lagging PMO
        # starved heartbeat consumption past HEARTBEAT_GRACE and the
        # watchdog killed HEALTHY runs. run.artifacts handling now rides
        # per-run task chains (per-run ordering preserved) under a global
        # concurrency bound; heartbeats/runspec traffic stays inline and
        # always drains. Ack-after-handle is unchanged — a crash before a
        # worker acks leaves the entry for redelivery (at-least-once).
        self._finalize_tasks: dict[str, asyncio.Task] = {}   # run_id → chain tail
        self._inflight_entries: set[str] = set()             # reclaim guard
        self._finalize_sem = asyncio.Semaphore(FINALIZE_CONCURRENCY)

    async def setup(self) -> None:
        try:
            await self.redis.xgroup_create(INGRESS, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise MessagingError(
                    f"setup: consumer group create failed: {e}") from e
        except (RedisConnectionError, RedisTimeoutError, OSError) as e:
            raise MessagingError(f"setup: network failure: {e}") from e

    # ── per-run ACL users (docs/09 §1a) ──────────────────────────────────────

    async def create_run_user(self, run_id: str) -> str:
        password = secrets.token_urlsafe(24)
        # Redis 7 key selectors (ISSUES #14): write-only on shared ingress so a
        # concurrent Dev cannot XREAD other runs' plaintext `auth` envelopes;
        # read/write only on this run's reply stream.
        try:
            await self.redis.execute_command(
                "ACL", "SETUSER", f"dev-{run_id}", "on", f">{password}",
                f"%W~{INGRESS}", f"%RW~{reply_stream(run_id)}",
                "+xadd", "+xread", "+xlen", "+ping", "+client|setinfo",
            )
            await self._acl_save()
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"create_run_user({run_id}): network/ACL failure: {e}") from e
        # the only place the plaintext exists app-side — register it here so
        # redact() masks it in everything PMO-bound (docs/14 §7)
        register_runtime_secret(run_id, password)
        return password

    async def delete_run_user(self, run_id: str) -> None:
        try:
            await self.redis.execute_command("ACL", "DELUSER", f"dev-{run_id}")
            await self._acl_save()
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"delete_run_user({run_id}): network/ACL failure: {e}") from e
        unregister_runtime_secret(run_id)

    def clear_chunk_assemblies(self) -> None:
        """Drop in-process chunk-reassembly state (clear-runs / wipe path)."""
        self._chunks.clear()

    async def revoke_leftover_run_users(self) -> int:
        """Clear-runs bulk revoke of every leftover ``dev-*`` ACL user.

        Per-run ``delete_run_user`` already ``ACL SAVE``s; the wipe path used
        to ``DELUSER`` in a loop without SAVE, so a redis-only restart
        resurrected wiped users from the compose ``aclfile`` (boot preserves
        non-default lines). One SAVE after the bulk delete closes that gap.
        Also drops process-local redaction registry entries for those run ids.
        """
        users = await self.redis.execute_command("ACL", "USERS")
        deleted = 0
        for u in users or []:
            name = u.decode() if isinstance(u, (bytes, bytearray)) else str(u)
            if not name.startswith("dev-"):
                continue
            await self.redis.execute_command("ACL", "DELUSER", name)
            unregister_runtime_secret(name[len("dev-"):])
            deleted += 1
        if deleted:
            await self._acl_save()
        return deleted

    async def _acl_save(self) -> None:
        """Persist ACL users to the configured aclfile (2026-08 evaluation):
        SETUSER lives in redis MEMORY only — without this, a redis-only
        restart (OOM, image bump, operator `compose restart redis`) stranded
        every in-flight run: containers survived but instantly lost auth,
        hung to the heartbeat grace, and burned an attempt on finished work.

        Best-effort by design: a deployment without `aclfile` configured
        (pre-hardening compose, bare dev redis) answers an error — the run
        must still dispatch there, it just keeps the old restart exposure."""
        try:
            await self.redis.execute_command("ACL", "SAVE")
        except ResponseError as e:
            log.warning("ACL SAVE unavailable (no aclfile configured?) — "
                        "per-run users will not survive a redis restart: %s", e)

    # ── replies (app → one Dev) ──────────────────────────────────────────────

    async def reply(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        stream = reply_stream(run_id)
        envelope = {
            "v": 1, "run_id": run_id, "kind": kind,
            "ts": datetime.now(timezone.utc).isoformat(), "payload": payload,
        }
        try:
            pipe = self.redis.pipeline(transaction=False)
            pipe.xadd(stream, {"m": json.dumps(envelope)})
            pipe.expire(stream, REPLY_TTL_SECONDS)   # one round-trip: the TTL is
            # the docs/09 §5 secret-lingering guard — never separated from the add
            await pipe.execute()
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"reply({run_id}, {kind}): network failure: {e}") from e

    async def delete_runspec_result(self, run_id: str) -> None:
        """XDEL runspec.result entries as soon as the Dev acknowledges (docs/09 §1a)."""
        stream = reply_stream(run_id)
        try:
            entries = await self.redis.xrange(stream)
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"delete_runspec_result({run_id}): network failure: {e}") from e
        for entry_id, fields in entries:
            try:
                if json.loads(fields.get("m", "{}")).get("kind") == "runspec.result":
                    await self.redis.xdel(stream, entry_id)
            except Exception:  # noqa: BLE001 — best-effort secret-entry sweep; skip is logged
                log.warning("delete_runspec_result: skipping unparseable entry %s on %s",
                            entry_id, stream)
                continue

    async def delete_reply_stream(self, run_id: str) -> None:
        try:
            await self.redis.delete(reply_stream(run_id))
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"delete_reply_stream({run_id}): network failure: {e}") from e

    # ── ingress consumption (Devs → app) ─────────────────────────────────────

    async def reclaim_pending(self, handler: Handler,
                              verify_auth: Callable[[str, Optional[str]], bool]) -> None:
        """Startup reconciliation step 4 (docs/04 §6): re-handle pending entries."""
        await self.setup()
        start = "0-0"
        while True:
            resp = await self.redis.execute_command(
                "XAUTOCLAIM", INGRESS, GROUP, CONSUMER, 60000, start, "COUNT", 20)
            start, messages = resp[0], resp[1]
            for entry_id, fields in messages:
                fields = {k: v for k, v in (fields or {}).items()} if isinstance(fields, dict)                     else dict(zip(fields[::2], fields[1::2]))
                if entry_id in self._inflight_entries:
                    # a worker is finalizing this entry RIGHT NOW — reclaim
                    # must not hand it out again in-process (a >idle-window
                    # finalize would otherwise run twice concurrently)
                    continue
                try:
                    handled = await self._handle_entry(
                        entry_id, fields, handler, verify_auth)
                    if not handled:
                        await self._maybe_poison(entry_id, fields)
                except Exception:
                    log.exception("reclaim: failed handling %s", entry_id)
                    await self._maybe_poison(entry_id, fields)
            if not messages or start == "0-0":
                break

    async def consume_forever(
        self, handler: Handler, verify_auth: Callable[[str, Optional[str]], bool]
    ) -> None:
        await self.setup()
        last_reclaim = time.monotonic()
        while True:
            # Connection-level failures must not kill the loop (2026-08
            # redis-restart drill: a `compose restart redis` raised
            # ConnectionError out of the blocking XREADGROUP, the task died
            # silently, and every later run.artifacts sat unconsumed — runs
            # parked in `running` forever while the aclfile durability work
            # kept their CREDENTIALS alive). The client reconnects on the
            # next call; setup() re-ensures the group (BUSYGROUP-tolerant)
            # in case the restart lost an un-AOF'd group create.
            try:
                entries = await self.redis.xreadgroup(
                    GROUP, CONSUMER, {INGRESS: ">"}, count=10, block=5000
                )
            # redis-py's ConnectionError does NOT subclass the builtin —
            # a bare `except ConnectionError` silently misses it (measured)
            except (RedisConnectionError, RedisTimeoutError, OSError) as e:
                log.warning("ingress: redis unavailable (%s) — retrying in 2s", e)
                await asyncio.sleep(2)
                try:
                    await self.setup()
                except Exception:  # noqa: BLE001 — still down; next loop retries
                    pass
                continue
            for _stream, messages in entries or []:
                for entry_id, fields in messages:
                    try:
                        handled = await self._handle_entry(
                            entry_id, fields, handler, verify_auth)
                        if not handled:
                            # a mid-group chunk entry stays unacked — the
                            # poison check here is what eventually dead-
                            # letters a PERMANENTLY incomplete group (its
                            # redeliveries are no-progress "successes")
                            await self._maybe_poison(entry_id, fields)
                    except Exception:
                        log.exception("ingress: failed handling %s", entry_id)
                        await self._maybe_poison(entry_id, fields)

            if time.monotonic() - last_reclaim >= RECLAIM_INTERVAL_SECONDS:
                try:
                    await self.reclaim_pending(handler, verify_auth)
                except Exception:
                    log.exception("periodic pending-entry reclaim failed")
                last_reclaim = time.monotonic()

    async def unresolved_run_ids(self) -> set[str]:
        """run_ids with entries still on the ingress stream. Handled entries are
        XACK+XDELed and poisoned groups are dead-lettered off the stream, so
        anything here is unhandled, in-flight, or awaiting redelivery. The
        watchdog uses this to distinguish a finalizing run that redelivery will
        still resume from one whose artifacts entry is gone (nothing can ever
        resume it)."""
        out: set[str] = set()
        start = "-"
        try:
            while True:
                entries = await self.redis.xrange(
                    INGRESS, min=start, max="+", count=200)
                for _entry_id, fields in entries:
                    try:
                        envelope = json.loads((fields or {}).get("m", "{}"))
                        run_id = str(envelope.get("run_id", ""))
                    except Exception:  # noqa: BLE001 — scan must finish; skip is logged
                        log.warning(
                            "ingress run-id scan: skipping unparseable entry %s",
                            _entry_id)
                        continue
                    if run_id:
                        out.add(run_id)
                if len(entries) < 200:
                    return out
                start = "(" + entries[-1][0]
        except _REDIS_WIRE as e:
            raise MessagingError(
                f"unresolved_run_ids: network failure: {e}") from e

    async def _ack_delete(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        # ATOMIC (audit F12): XACK then XDEL in a MULTI/EXEC. A crash between
        # the two used to leave an entry ACKED but PRESENT — which
        # unresolved_run_ids (an xrange over present entries) then counted as
        # "resumable", contradicting its own docstring invariant that handled
        # entries leave the stream.
        pipe = self.redis.pipeline(transaction=True)
        pipe.xack(INGRESS, GROUP, *entry_ids)
        pipe.xdel(INGRESS, *entry_ids)
        await pipe.execute()

    @staticmethod
    def _chunk_identity(envelope: dict[str, Any]) -> Optional[tuple[str, str, str]]:
        payload = envelope.get("payload") or {}
        if not isinstance(payload, dict):
            return None
        if "chunk" not in payload:
            return None
        return (
            str(envelope.get("run_id", "")),
            str(envelope.get("kind", "")),
            str(payload.get("chunk_id", "")),
        )

    @staticmethod
    def _scrub_envelope_auth(value: Any, auth: str) -> Any:
        """Prevent the relay credential being echoed through Dev-controlled output."""
        if len(auth) < 16:
            return value
        if isinstance(value, str):
            return value.replace(auth, MASK)
        if isinstance(value, list):
            return [Messaging._scrub_envelope_auth(item, auth) for item in value]
        if isinstance(value, dict):
            return {k: Messaging._scrub_envelope_auth(v, auth)
                    for k, v in value.items()}
        return value

    async def _maybe_poison(self, entry_id, fields) -> None:
        """After five deliveries, dead-letter and remove the whole chunk group."""
        try:
            pending = await self.redis.xpending_range(INGRESS, GROUP, entry_id,
                                                      entry_id, count=1)
            if not pending or pending[0].get("times_delivered", 0) < 5:
                return
        except Exception:
            log.exception("poison threshold check failed for %s", entry_id)
            return
        # best-effort parse: a malformed body must still be dead-letterable,
        # or the entry is reclaimed and redelivered forever
        try:
            envelope = json.loads(fields.get("m", "{}"))
            if not isinstance(envelope, dict):
                envelope = {}
        except Exception:  # noqa: BLE001 — a malformed body must still be dead-letterable; empty envelope, poison path proceeds
            envelope = {}
        key = self._chunk_identity(envelope)
        assembly = self._chunks.get(key) if key else None
        if assembly is not None and (time.monotonic() - assembly["last_progress"]
                                     < CHUNK_GROUP_STALL_SECONDS):
            log.info("ingress: chunk group %s still progressing; deferring poison", key)
            return
        entry_ids = list((assembly or {}).get("entry_ids", [])) or [entry_id]
        try:
            # reliability event — traced so dead-lettering is queryable in OO
            with tracer.start_as_current_span("ingress.poison") as span:
                span.set_attribute("devcake.run.id", str(envelope.get("run_id", "")))
                span.set_attribute("devcake.kind", str(envelope.get("kind", "")))
                span.set_attribute("devcake.poison.entries", len(entry_ids))
                span.set_status(Status(StatusCode.ERROR,
                                       "poison message dead-lettered"))
                payload = envelope.get("payload")
                await self.redis.xadd(
                    DEAD_STREAM,
                    {
                        "m": json.dumps({
                            "v": envelope.get("v"),
                            "run_id": envelope.get("run_id", ""),
                            "kind": envelope.get("kind", ""),
                            "ts": envelope.get("ts", ""),
                            "payload_keys": sorted(payload.keys())
                            if isinstance(payload, dict) else [],
                        }),
                        "src": json.dumps(entry_ids),
                        "chunk_group": json.dumps(key) if key else "",
                    },
                    maxlen=DEAD_STREAM_MAXLEN,
                    approximate=True,
                )
                await self._ack_delete(entry_ids)
                if key:
                    self._chunks.pop(key, None)
                log.error("ingress: POISON group %s moved to %s", entry_ids, DEAD_STREAM)
        except Exception:
            log.exception("poison handling failed for %s", entry_id)

    def _admit_chunk(self, assembly, chunk: int, data: str, total: int,
                     digest: str) -> None:
        """Admission control for one chunk (ADR-0018) — validate and size-check
        BEFORE storing, then store. Raises to reject, leaving no residue.

        Previously the caps were evaluated AFTER `parts[chunk] = data` and
        before the completion test, which made the global budget a one-way
        ratchet: past the limit no group could ever complete — including the
        chunk that would have finished one and freed its memory — while
        rejected chunks stayed resident and, on a first delivery, had already
        refreshed `last_progress`, deferring the stall reaper that was supposed
        to be the recovery path.
        """
        # The group's OWN metadata decides completion and integrity — never the
        # current envelope's. Without this, a redelivery declaring a different
        # `of` either KeyErrors out of the join or assembles across generations
        # and verifies the result against the wrong digest.
        if assembly["total"] != total or assembly["sha256"] != digest:
            raise ValueError("inconsistent chunk group metadata")
        old = assembly["parts"].get(chunk)
        if old is not None:
            if old != data:
                raise ValueError("conflicting duplicate chunk")
            return          # redelivery of a buffered chunk: not progress
        chunk_bytes = len(data.encode("utf-8"))
        if chunk_bytes > MAX_CHUNK_BYTES:
            raise ValueError("chunk exceeds per-chunk size limit")
        group_bytes = sum(len(p.encode("utf-8"))
                          for p in assembly["parts"].values()) + chunk_bytes
        if group_bytes > MAX_ASSEMBLED_BYTES:
            raise ValueError("chunk group exceeds assembled-size limit")
        # A COMPLETING chunk is always admitted: handling it drains the whole
        # group, so it can only reduce pressure. Admitting it is what breaks
        # the ratchet. Anything else must fit inside the global budget.
        if len(assembly["parts"]) + 1 < total:
            buffered = sum(len(part.encode("utf-8"))
                           for group in self._chunks.values()
                           for part in group["parts"].values())
            if buffered + chunk_bytes > MAX_BUFFERED_CHUNK_BYTES:
                raise ValueError("global chunk buffer limit exceeded")
        assembly["parts"][chunk] = data
        assembly["last_progress"] = time.monotonic()   # accepted ⇒ real progress

    def _spawn_finalize(self, run_id, kind, payload, entry_ids, fields,
                        handler, chunk_key=None, chunk_id=None) -> None:
        """Chain a run.artifacts handling task after the run's previous one
        (per-run ordering) under the global finalize bound. The WORKER owns
        ack/complete-key/poison — exactly what the inline path did."""
        prev = self._finalize_tasks.get(run_id)
        self._inflight_entries.update(entry_ids)

        async def _work():
            if prev is not None and not prev.done():
                with contextlib.suppress(Exception):
                    await asyncio.shield(prev)
            async with self._finalize_sem:
                try:
                    await handler(run_id, kind, payload)
                    if chunk_id:
                        await self.redis.set(
                            chunk_complete_key(run_id, chunk_id), "1",
                            ex=REPLY_TTL_SECONDS)
                    await self._ack_delete(list(entry_ids))
                    if chunk_key:
                        self._chunks.pop(chunk_key, None)
                except Exception:
                    log.exception("ingress: finalize failed for %s", run_id)
                    with contextlib.suppress(Exception):
                        await self._maybe_poison(entry_ids[-1], fields)
                finally:
                    self._inflight_entries.difference_update(entry_ids)
                    if self._finalize_tasks.get(run_id) is task:
                        self._finalize_tasks.pop(run_id, None)

        task = asyncio.create_task(_work())
        self._finalize_tasks[run_id] = task

    async def drain_finalizes(self) -> None:
        """Await every in-flight finalize chain (tests + orderly paths)."""
        while self._finalize_tasks:
            tasks = list(self._finalize_tasks.values())
            await asyncio.gather(*tasks, return_exceptions=True)
            if all(t.done() for t in tasks) and not any(
                    t2 for t2 in self._finalize_tasks.values()
                    if t2 not in tasks):
                break

    async def _handle_entry(self, entry_id, fields, handler, verify_auth) -> bool:
        envelope = json.loads(fields.get("m", "{}"))
        run_id = envelope.get("run_id", "")
        kind = envelope.get("kind", "")
        payload = envelope.get("payload", {}) or {}
        if not isinstance(payload, dict):
            raise ValueError("envelope payload must be an object")

        auth = str(envelope.get("auth") or "")
        if not verify_auth(run_id, auth):
            # security event — traced so forgery attempts are queryable in OO
            with tracer.start_as_current_span("ingress.forged_drop") as span:
                span.set_attribute("devcake.run.id", run_id)
                span.set_attribute("devcake.kind", kind)
                span.set_status(Status(StatusCode.ERROR,
                                       "forged/unauthenticated message dropped"))
                log.warning("ingress: FORGED/unauthenticated message dropped "
                            "(run_id=%s kind=%s)", run_id, kind)
                await self._ack_delete([entry_id])
            return True
        if "chunk" in payload:  # reassemble chunked payloads (docs/09 §3)
            if kind != "run.artifacts":
                raise ValueError("only run.artifacts may be chunked")
            chunk = int(payload["chunk"])
            total = int(payload["of"])
            chunk_id = str(payload.get("chunk_id", ""))
            digest = str(payload.get("sha256", ""))
            data = payload.get("data")
            if not chunk_id or len(chunk_id) > 128:
                raise ValueError("invalid chunk_id")
            if total < 1 or total > MAX_CHUNKS or chunk < 1 or chunk > total:
                raise ValueError("invalid chunk index/count")
            if not isinstance(data, str):
                raise ValueError("chunk data must be text")
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError("invalid chunk sha256")
            key = (run_id, kind, chunk_id)
            if await self.redis.exists(chunk_complete_key(run_id, chunk_id)):
                await self._ack_delete([entry_id])
                return True
            if key not in self._chunks and len(self._chunks) >= MAX_ACTIVE_CHUNK_GROUPS:
                raise ValueError("too many active chunk groups")
            created = key not in self._chunks
            assembly = self._chunks.setdefault(key, {
                "total": total, "sha256": digest, "parts": {}, "entry_ids": [],
                "last_progress": time.monotonic(),
            })
            try:
                self._admit_chunk(assembly, chunk, data, total, digest)
            except Exception:
                # a group this delivery created and never populated must not
                # linger holding one of the MAX_ACTIVE_CHUNK_GROUPS slots
                if created and not assembly["parts"]:
                    self._chunks.pop(key, None)
                raise
            if entry_id not in assembly["entry_ids"]:
                assembly["entry_ids"].append(entry_id)
            if len(assembly["parts"]) < total:
                return False   # unacked mid-group entry: poison check applies
            joined = "".join(assembly["parts"][i] for i in range(1, total + 1))
            if hashlib.sha256(joined.encode("utf-8")).hexdigest() != digest:
                raise ValueError("assembled chunk sha256 mismatch")
            payload = json.loads(joined)
            if not isinstance(payload, dict):
                raise ValueError("assembled payload must be an object")
            payload = self._scrub_envelope_auth(payload, auth)
            # chunked payloads are run.artifacts by construction (checked
            # above) — the finalize rides the per-run chain (audit F11)
            self._spawn_finalize(run_id, kind, payload,
                                 list(assembly["entry_ids"]), fields, handler,
                                 chunk_key=key, chunk_id=chunk_id)
            return True

        payload = self._scrub_envelope_auth(payload, auth)
        if kind in FINALIZE_KINDS:
            self._spawn_finalize(run_id, kind, payload, [entry_id], fields,
                                 handler)
            return True
        await handler(run_id, kind, payload)
        await self._ack_delete([entry_id])
        return True
