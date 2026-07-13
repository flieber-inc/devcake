"""App side of the Redis Streams protocol (docs/09).

- ingress consumer group on devcake:ingress (at-least-once, XACK after handling)
- per-run reply streams devcake:reply:{run_id} (runspec/activity request-reply)
- per-run ACL users (docs/09 §1a): scoped credentials, revoked at finalization
- envelope auth verification (forgery guard) and chunked-payload reassembly
"""

import json
import hashlib
import logging
import secrets
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from ...security import register_runtime_secret, unregister_runtime_secret

log = logging.getLogger("devcake.messaging")

INGRESS = "devcake:ingress"
GROUP = "app"
CONSUMER = "app-1"
REPLY_TTL_SECONDS = 900          # secret material must not linger (docs/09 §5)
CHUNK_LIMIT = 512 * 1024         # payloads above this arrive chunked
MAX_CHUNKS = 128
MAX_ASSEMBLED_BYTES = 50 * 1024 * 1024
MAX_ACTIVE_CHUNK_GROUPS = 16
MAX_BUFFERED_CHUNK_BYTES = 100 * 1024 * 1024
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


def runspec_secret_key(run_id: str) -> str:
    return f"devcake:runspec-secret:{run_id}"


def chunk_complete_key(run_id: str, chunk_id: str) -> str:
    return f"devcake:chunk-complete:{run_id}:{chunk_id}"


class Messaging:
    def __init__(self, url: str, password: str):
        self.redis = aioredis.from_url(url, password=password or None, decode_responses=True)
        self._chunks: dict[tuple[str, str, str], dict[str, Any]] = {}

    async def setup(self) -> None:
        try:
            await self.redis.xgroup_create(INGRESS, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    async def scrub_legacy_dead_letters(self) -> int:
        """Remove pre-v2 dead letters that copied raw auth/payload envelopes."""
        removed = 0
        for entry_id, fields in await self.redis.xrange(DEAD_STREAM):
            try:
                body = json.loads(fields.get("m", ""))
                safe = isinstance(body, dict) and "payload_keys" in body \
                    and "auth" not in body and "payload" not in body
            except Exception:
                safe = False
            if not safe:
                await self.redis.xdel(DEAD_STREAM, entry_id)
                removed += 1
        if removed:
            log.warning("removed %d legacy credential-bearing dead letters", removed)
        return removed

    async def delete_acknowledged_ingress(self) -> int:
        """Remove pre-v2 acknowledged history while preserving pending/unread work."""
        groups = await self.redis.xinfo_groups(INGRESS)
        group = next((item for item in groups if item.get("name") == GROUP), None)
        if not group:
            return 0
        last_delivered = group.get("last-delivered-id", "0-0")
        summary = await self.redis.xpending(INGRESS, GROUP)
        pending_count = int(summary.get("pending", 0))
        if pending_count > 10_000:
            log.error("refusing ingress history cleanup with %d pending entries",
                      pending_count)
            return 0
        pending_rows = await self.redis.xpending_range(
            INGRESS, GROUP, "-", "+", count=max(1, pending_count))
        pending_ids = {row["message_id"] for row in pending_rows}

        removed = 0
        minimum = "-"
        while True:
            batch = await self.redis.xrange(
                INGRESS, min=minimum, max=last_delivered, count=250)
            if not batch:
                break
            deletable = [entry_id for entry_id, _fields in batch
                         if entry_id not in pending_ids]
            if deletable:
                removed += await self.redis.xdel(INGRESS, *deletable)
            if len(batch) < 250:
                break
            minimum = f"({batch[-1][0]}"
        if removed:
            log.info("removed %d acknowledged legacy ingress entries", removed)
        return removed

    # ── per-run ACL users (docs/09 §1a) ──────────────────────────────────────

    async def create_run_user(self, run_id: str) -> str:
        password = secrets.token_urlsafe(24)
        await self.redis.execute_command(
            "ACL", "SETUSER", f"dev-{run_id}", "on", f">{password}",
            f"~{INGRESS}", f"~{reply_stream(run_id)}",
            "+xadd", "+xread", "+xlen", "+ping", "+client|setinfo",
        )
        # the only place the plaintext exists app-side — register it here so
        # redact() masks it in everything PMO-bound (docs/14 §5)
        register_runtime_secret(run_id, password)
        return password

    async def delete_run_user(self, run_id: str) -> None:
        await self.redis.execute_command("ACL", "DELUSER", f"dev-{run_id}")
        unregister_runtime_secret(run_id)

    async def delete_runspec_secret(self, run_id: str) -> None:
        # runspec secrets are built on request since v2.1 (docs/09 §5) — this
        # only cleans keys left behind by runs dispatched before the upgrade
        await self.redis.delete(runspec_secret_key(run_id))

    # ── replies (app → one Dev) ──────────────────────────────────────────────

    async def reply(self, run_id: str, kind: str, payload: dict[str, Any]) -> None:
        stream = reply_stream(run_id)
        envelope = {
            "v": 1, "run_id": run_id, "kind": kind,
            "ts": datetime.now(timezone.utc).isoformat(), "payload": payload,
        }
        await self.redis.xadd(stream, {"m": json.dumps(envelope)})
        await self.redis.expire(stream, REPLY_TTL_SECONDS)

    async def delete_runspec_result(self, run_id: str) -> None:
        """XDEL runspec.result entries as soon as the Dev acknowledges (docs/09 §1a)."""
        stream = reply_stream(run_id)
        for entry_id, fields in await self.redis.xrange(stream):
            try:
                if json.loads(fields.get("m", "{}")).get("kind") == "runspec.result":
                    await self.redis.xdel(stream, entry_id)
            except Exception:
                continue

    async def delete_reply_stream(self, run_id: str) -> None:
        await self.redis.delete(reply_stream(run_id), runspec_secret_key(run_id))

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
                try:
                    await self._handle_entry(entry_id, fields, handler, verify_auth)
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
            entries = await self.redis.xreadgroup(
                GROUP, CONSUMER, {INGRESS: ">"}, count=10, block=5000
            )
            for _stream, messages in entries or []:
                for entry_id, fields in messages:
                    try:
                        await self._handle_entry(entry_id, fields, handler, verify_auth)
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

    async def _ack_delete(self, entry_ids: list[str]) -> None:
        if not entry_ids:
            return
        pipe = self.redis.pipeline(transaction=False)
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
            return value.replace(auth, "«REDACTED»")
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
        except Exception:
            envelope = {}
        key = self._chunk_identity(envelope)
        assembly = self._chunks.get(key) if key else None
        if assembly is not None and (time.monotonic() - assembly["last_progress"]
                                     < CHUNK_GROUP_STALL_SECONDS):
            log.info("ingress: chunk group %s still progressing; deferring poison", key)
            return
        entry_ids = list((assembly or {}).get("entry_ids", [])) or [entry_id]
        try:
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

    async def _handle_entry(self, entry_id, fields, handler, verify_auth) -> None:
        envelope = json.loads(fields.get("m", "{}"))
        run_id = envelope.get("run_id", "")
        kind = envelope.get("kind", "")
        payload = envelope.get("payload", {}) or {}
        if not isinstance(payload, dict):
            raise ValueError("envelope payload must be an object")

        auth = str(envelope.get("auth") or "")
        if not verify_auth(run_id, auth):
            log.warning("ingress: FORGED/unauthenticated message dropped (run_id=%s kind=%s)",
                        run_id, kind)
            await self._ack_delete([entry_id])
            return
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
                return
            if key not in self._chunks and len(self._chunks) >= MAX_ACTIVE_CHUNK_GROUPS:
                raise ValueError("too many active chunk groups")
            assembly = self._chunks.setdefault(key, {
                "total": total, "sha256": digest, "parts": {}, "entry_ids": [],
                "last_progress": time.monotonic(),
            })
            if assembly["total"] != total or assembly["sha256"] != digest:
                raise ValueError("inconsistent chunk group metadata")
            old = assembly["parts"].get(chunk)
            if old is not None and old != data:
                raise ValueError("conflicting duplicate chunk")
            if old is None:
                # redeliveries of already-buffered chunks are not progress —
                # a dead sender's group must still stall out and poison
                assembly["last_progress"] = time.monotonic()
            assembly["parts"][chunk] = data
            if entry_id not in assembly["entry_ids"]:
                assembly["entry_ids"].append(entry_id)
            byte_count = sum(len(part.encode("utf-8"))
                             for part in assembly["parts"].values())
            if byte_count > MAX_ASSEMBLED_BYTES:
                raise ValueError("chunk group exceeds assembled-size limit")
            buffered = sum(
                len(part.encode("utf-8"))
                for group in self._chunks.values()
                for part in group["parts"].values()
            )
            if buffered > MAX_BUFFERED_CHUNK_BYTES:
                raise ValueError("global chunk buffer limit exceeded")
            if len(assembly["parts"]) < total:
                return
            joined = "".join(assembly["parts"][i] for i in range(1, total + 1))
            if hashlib.sha256(joined.encode("utf-8")).hexdigest() != digest:
                raise ValueError("assembled chunk sha256 mismatch")
            payload = json.loads(joined)
            if not isinstance(payload, dict):
                raise ValueError("assembled payload must be an object")
            payload = self._scrub_envelope_auth(payload, auth)
            await handler(run_id, kind, payload)
            await self.redis.set(
                chunk_complete_key(run_id, chunk_id), "1", ex=REPLY_TTL_SECONDS)
            await self._ack_delete(list(assembly["entry_ids"]))
            self._chunks.pop(key, None)
            return

        await handler(run_id, kind, self._scrub_envelope_auth(payload, auth))
        await self._ack_delete([entry_id])
