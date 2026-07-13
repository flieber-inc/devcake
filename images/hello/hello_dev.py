"""Hello-world stub Dev (docs/16 M1) — exercises the full Dev container contract
(docs/07): scoped Redis auth, runspec.get/ack, run.started, heartbeats,
dev.run/harness.exec spans linked via TRACEPARENT, and (chunked) run.artifacts.

Permanent CI fixture: the deterministic stand-in for real harnesses (docs/16 M7).
"""

import json
import hashlib
import os
import pathlib
import sys
import time
import uuid
from datetime import datetime, timezone

import redis

RUN_ID = os.environ["DEVCAKE_RUN_ID"]
REDIS_URL = os.environ["REDIS_URL"]
REDIS_USER = os.environ["REDIS_USER"]
REDIS_PASSWORD = os.environ["REDIS_PASSWORD"]
TRACEPARENT = os.environ.get("TRACEPARENT", "")
INGRESS = "devcake:ingress"
REPLY = f"devcake:reply:{RUN_ID}"
CHUNK_LIMIT = 512 * 1024
CHUNK_SIZE = 400 * 1024


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


r = redis.from_url(REDIS_URL, username=REDIS_USER, password=REDIS_PASSWORD,
                   decode_responses=True)


def send(kind: str, payload: dict) -> None:
    envelope = {"v": 1, "run_id": RUN_ID, "auth": REDIS_PASSWORD, "kind": kind,
                "ts": now(), "payload": payload}
    for attempt in range(4):
        try:
            r.xadd(INGRESS, {"m": json.dumps(envelope)})
            return
        except redis.RedisError:
            if attempt == 3:
                raise
            time.sleep(0.25 * (2 ** attempt))


MAX_ARTIFACT_BYTES = 50 * 1024 * 1024 - 256 * 1024  # headroom under ingress caps
SHRINKABLE_FIELDS = ("transcript_md", "plan_md")     # never result/exit_code/token_report
TRUNCATE_FLOOR = 10_000


def _fit_payload(payload: dict) -> dict:
    """Shrink an oversized artifact instead of dying at the end of the run:
    halve the largest shrinkable text field (with an explicit notice) until
    the blob fits, so result/exit_code/token_report always ship."""
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


def send_artifacts(payload: dict) -> None:
    payload = _fit_payload(payload)
    blob = json.dumps(payload)
    if len(blob) <= CHUNK_LIMIT:
        send("run.artifacts", payload)
        return
    parts = [blob[i:i + CHUNK_SIZE] for i in range(0, len(blob), CHUNK_SIZE)]
    if len(parts) > 128 or len(blob.encode("utf-8")) > 50 * 1024 * 1024:
        raise ValueError("artifact payload exceeds Redis ingress limits")
    chunk_id = uuid.uuid4().hex
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    for i, part in enumerate(parts, start=1):
        send("run.artifacts", {"chunk": i, "of": len(parts),
                               "chunk_id": chunk_id, "sha256": digest,
                               "data": part})


def fetch_runspec() -> dict:
    send("runspec.get", {})
    last_id = "0"
    deadline = time.time() + 60
    while time.time() < deadline:
        entries = r.xread({REPLY: last_id}, block=5000, count=10)
        for _stream, messages in entries or []:
            for entry_id, fields in messages:
                last_id = entry_id
                env = json.loads(fields["m"])
                if env.get("kind") == "runspec.result":
                    send("runspec.ack", {})
                    return env["payload"]
                if env.get("kind") == "runspec.error":
                    print(env.get("payload", {}).get("error", "run spec unavailable"),
                          file=sys.stderr)
                    sys.exit(20)
    print("runspec.get timed out", file=sys.stderr)
    sys.exit(20)


def main() -> None:
    spec = fetch_runspec()
    for key, value in spec.get("env", {}).items():
        os.environ[key] = value
    for f in spec.get("credential_files", []):
        path = pathlib.Path(os.path.expanduser(f["path_hint"]))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f["content"])
        path.chmod(0o600)

    send("run.started", {"container_hostname": os.uname().nodename})

    # Telemetry after the runspec (endpoint + auth arrive in stage 2 — docs/07 §3).
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagate import extract
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "devcake-dev"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(
        endpoint=os.environ["OTEL_EXPORTER_OTLP_ENDPOINT"],
        headers={"Authorization": f"Basic {os.environ['OTEL_EXPORTER_OTLP_BASIC']}"},
    )))
    trace.set_tracer_provider(provider)
    tracer = trace.get_tracer("devcake-dev")
    ctx = extract({"traceparent": TRACEPARENT}) if TRACEPARENT else None

    sleep_s = int(os.environ.get("HELLO_SLEEP", "3"))
    payload_kb = int(os.environ.get("HELLO_PAYLOAD_KB", "1"))
    heartbeat_every = int(os.environ.get("HELLO_HEARTBEAT", "10"))

    with tracer.start_as_current_span("dev.run", context=ctx) as span:
        span.set_attribute("devcake.run.id", RUN_ID)
        span.set_attribute("devcake.dev_type", os.environ.get("DEVCAKE_DEV_TYPE", "hello-stub"))
        span.set_attribute("devcake.harness", "stub")
        with tracer.start_as_current_span("harness.exec"):
            waited = 0.0
            while waited < sleep_s:
                time.sleep(min(1.0, sleep_s - waited))
                waited += 1.0
                if int(waited) % heartbeat_every == 0:
                    send("run.heartbeat", {"phase": "working"})
        span.set_attribute("devcake.outcome", "hello")

    transcript = "# hello transcript\n" + ("devcake " * 128 + "\n") * payload_kb
    send_artifacts({
        "result": {"schema_version": 1, "outcome": "hello",
                   "summary": f"hello from stub dev {RUN_ID}"},
        "transcript_md": transcript,
        "token_report": {"model": "stub", "extraction_method": "unavailable",
                         "total_tokens": None},
    })
    provider.force_flush()
    print(f"hello dev {RUN_ID} done")


if __name__ == "__main__":
    main()
