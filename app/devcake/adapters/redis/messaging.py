"""App side of the Redis Streams protocol (docs/09).

- ingress consumer group on devcake:ingress (at-least-once, XACK after handling)
- per-run reply streams devcake:reply:{run_id} (runspec/activity request-reply)
- per-run ACL users (docs/09 §1a): scoped credentials, revoked at finalization
- envelope auth verification (forgery guard) and chunked-payload reassembly
"""

import json
import logging
import secrets
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

log = logging.getLogger("devcake.messaging")

INGRESS = "devcake:ingress"
GROUP = "app"
CONSUMER = "app-1"
REPLY_TTL_SECONDS = 900          # secret material must not linger (docs/09 §5)
CHUNK_LIMIT = 512 * 1024         # payloads above this arrive chunked

Handler = Callable[[str, str, dict[str, Any]], Awaitable[None]]  # (run_id, kind, payload)


def reply_stream(run_id: str) -> str:
    return f"devcake:reply:{run_id}"


class Messaging:
    def __init__(self, url: str, password: str):
        self.redis = aioredis.from_url(url, password=password or None, decode_responses=True)
        self._chunks: dict[tuple[str, str], dict[int, str]] = {}
        self._chunk_totals: dict[tuple[str, str], int] = {}

    async def setup(self) -> None:
        try:
            await self.redis.xgroup_create(INGRESS, GROUP, id="0", mkstream=True)
        except ResponseError as e:
            if "BUSYGROUP" not in str(e):
                raise

    # ── per-run ACL users (docs/09 §1a) ──────────────────────────────────────

    async def create_run_user(self, run_id: str) -> str:
        password = secrets.token_urlsafe(24)
        await self.redis.execute_command(
            "ACL", "SETUSER", f"dev-{run_id}", "on", f">{password}",
            f"~{INGRESS}", f"~{reply_stream(run_id)}",
            "+xadd", "+xread", "+xlen", "+ping", "+client|setinfo",
        )
        return password

    async def delete_run_user(self, run_id: str) -> None:
        await self.redis.execute_command("ACL", "DELUSER", f"dev-{run_id}")

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
        await self.redis.delete(reply_stream(run_id))

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
                except Exception:
                    log.exception("reclaim: failed handling %s", entry_id)
                    await self._maybe_poison(entry_id, fields)
            if not messages or start == "0-0":
                break

    async def consume_forever(
        self, handler: Handler, verify_auth: Callable[[str, Optional[str]], bool]
    ) -> None:
        await self.setup()
        while True:
            entries = await self.redis.xreadgroup(
                GROUP, CONSUMER, {INGRESS: ">"}, count=10, block=5000
            )
            for _stream, messages in entries or []:
                for entry_id, fields in messages:
                    try:
                        await self._handle_entry(entry_id, fields, handler, verify_auth)
                    except Exception:
                        log.exception("ingress: failed handling %s", entry_id)
                        await self._maybe_poison(entry_id, fields)

    async def _maybe_poison(self, entry_id, fields) -> None:
        """docs/09 §4: an entry failing 5 deliveries moves to devcake:dead."""
        try:
            pending = await self.redis.xpending_range(INGRESS, GROUP, entry_id,
                                                      entry_id, count=1)
            if pending and pending[0].get("times_delivered", 0) >= 5:
                await self.redis.xadd("devcake:dead", {"m": fields.get("m", ""),
                                                       "src": entry_id})
                await self.redis.xack(INGRESS, GROUP, entry_id)
                log.error("ingress: POISON message %s moved to devcake:dead", entry_id)
        except Exception:
            log.exception("poison handling failed for %s", entry_id)

    async def _handle_entry(self, entry_id, fields, handler, verify_auth) -> None:
        envelope = json.loads(fields.get("m", "{}"))
        run_id = envelope.get("run_id", "")
        kind = envelope.get("kind", "")
        payload = envelope.get("payload", {}) or {}

        if not verify_auth(run_id, envelope.get("auth")):
            log.warning("ingress: FORGED/unauthenticated message dropped (run_id=%s kind=%s)",
                        run_id, kind)
            await self.redis.xack(INGRESS, GROUP, entry_id)
            return

        if "chunk" in payload:  # reassemble chunked payloads (docs/09 §3)
            key = (run_id, kind)
            self._chunks.setdefault(key, {})[int(payload["chunk"])] = payload["data"]
            self._chunk_totals[key] = int(payload["of"])
            await self.redis.xack(INGRESS, GROUP, entry_id)
            if len(self._chunks[key]) < self._chunk_totals[key]:
                return
            data = "".join(self._chunks[key][i] for i in range(1, self._chunk_totals[key] + 1))
            del self._chunks[key], self._chunk_totals[key]
            payload = json.loads(data)
            await handler(run_id, kind, payload)
            return

        await handler(run_id, kind, payload)
        await self.redis.xack(INGRESS, GROUP, entry_id)
