"""Redis Streams bus — Dev → app (docs/09).

Composition root (entrypoint) sets RUN_ID/credentials via environment before
import. No domain logic here.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import redis

RUN_ID = os.environ["DEVCAKE_RUN_ID"]
REDIS_URL = os.environ["REDIS_URL"]
INGRESS = "devcake:ingress"
REPLY = f"devcake:reply:{RUN_ID}"
CHUNK_LIMIT, CHUNK_SIZE = 512 * 1024, 400 * 1024

r = redis.from_url(REDIS_URL, username=os.environ["REDIS_USER"],
                   password=os.environ["REDIS_PASSWORD"], decode_responses=True)

MAX_ARTIFACT_BYTES = 50 * 1024 * 1024 - 256 * 1024
SHRINKABLE_FIELDS = ("transcript_md", "plan_md", "last_message_md")
TRUNCATE_FLOOR = 10_000


# attempts × backoff. The default (~1.75s) covers a momentary blip; the
# BOOT and FINALIZE sends widen it (2026-08-12 audit OPS-M5): an operator
# `compose restart redis` at container start used to kill the first
# heartbeat, and a blip at run end turned a cleanly classified failure into
# an unclassified crash-with-traceback. ~15s of retry rides both windows out.
SEND_ATTEMPTS = 4
SEND_ATTEMPTS_RESILIENT = 8


def send(kind: str, payload: dict, *, attempts: int = SEND_ATTEMPTS) -> None:
    envelope = {"v": 1, "run_id": RUN_ID, "auth": os.environ["REDIS_PASSWORD"],
                "kind": kind, "ts": datetime.now(timezone.utc).isoformat(),
                "payload": payload}
    for attempt in range(attempts):
        try:
            r.xadd(INGRESS, {"m": json.dumps(envelope)})
            return
        except redis.RedisError:
            if attempt == attempts - 1:
                raise
            time.sleep(min(4.0, 0.25 * (2 ** attempt)))


def _fit_payload(payload: dict) -> dict:
    """Shrink oversized artifact text fields so result/exit_code always ship."""
    if len(json.dumps(payload).encode("utf-8")) <= MAX_ARTIFACT_BYTES:
        return payload
    payload = dict(payload)
    while len(json.dumps(payload).encode("utf-8")) > MAX_ARTIFACT_BYTES:
        shrinkable = [f for f in SHRINKABLE_FIELDS
                      if isinstance(payload.get(f), str)
                      and len(payload[f]) > TRUNCATE_FLOOR]
        if not shrinkable:
            raise ValueError(
                "artifact payload exceeds Redis ingress limits even after truncation")
        field = max(shrinkable, key=lambda f: len(payload[f]))
        text = payload[field]
        keep = len(text) // 2
        payload[field] = (text[:keep] + f"\n\n[devcake] {field} truncated: kept "
                          f"{keep} of {len(text)} characters to fit ingress limits\n")
    return payload


ARTIFACTS_SENT = False


def send_artifacts(payload: dict) -> None:
    # FINALIZE send (OPS-M5): the artifacts are the run's whole verdict — a
    # transient Redis blip here must not lose them to an unclassified crash,
    # so ride the wider retry budget.
    global ARTIFACTS_SENT
    payload = _fit_payload(payload)
    blob = json.dumps(payload)
    if len(blob) <= CHUNK_LIMIT:
        send("run.artifacts", payload, attempts=SEND_ATTEMPTS_RESILIENT)
        ARTIFACTS_SENT = True
        return
    parts = [blob[i:i + CHUNK_SIZE] for i in range(0, len(blob), CHUNK_SIZE)]
    if len(parts) > 128 or len(blob.encode("utf-8")) > 50 * 1024 * 1024:
        raise ValueError("artifact payload exceeds Redis ingress limits")
    chunk_id = uuid.uuid4().hex
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    for i, part in enumerate(parts, start=1):
        send("run.artifacts", {"chunk": i, "of": len(parts),
                               "chunk_id": chunk_id, "sha256": digest,
                               "data": part}, attempts=SEND_ATTEMPTS_RESILIENT)
    ARTIFACTS_SENT = True


def request_reply(kind: str, want: str, timeout: int = 90,
                  payload: dict | None = None) -> dict:
    # ADR-0025: runspec.get carries {"phase": ...} so the app can serve the
    # provision step a reduced, secret-free spec; no payload = full spec.
    send(kind, payload or {})
    last_id, deadline = "0", time.time() + timeout
    while time.time() < deadline:
        for _s, msgs in r.xread({REPLY: last_id}, block=5000, count=10) or []:
            for entry_id, fields in msgs:
                last_id = entry_id
                env = json.loads(fields["m"])
                if env.get("kind") == want:
                    return env["payload"]
                if env.get("kind") == "runspec.error":
                    print(env.get("payload", {}).get("error", "run spec unavailable"),
                          file=sys.stderr)
                    sys.exit(20)
    print(f"{kind} timed out", file=sys.stderr)
    sys.exit(20)


def heartbeat_loop(stop: threading.Event) -> None:
    # The FIRST beat was outside the try (OPS-M5): a boot-window Redis blip
    # raised out of it, the thread died PERMANENTLY, and a perfectly healthy
    # multi-hour run was later killed by heartbeat grace. Guard it like the
    # loop body, with the wider boot-window budget.
    try:
        send("run.heartbeat", {"phase": "starting"},
             attempts=SEND_ATTEMPTS_RESILIENT)
    except Exception:
        pass
    while not stop.wait(30):
        try:
            send("run.heartbeat", {"phase": "working"})
        except Exception:
            pass
