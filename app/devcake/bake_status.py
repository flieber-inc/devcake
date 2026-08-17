"""Host baker status on /data. App reads; the host baker writes.

Missing or unreadable → idle. Liveness is the heartbeat the baker
stamps on every tick (and while waiting on docker bake). A baking
row with no recent heartbeat is a crash, not a live compile.

Baker jsonl is shipped to OpenObserve by the poll cycle via the
existing push_oo_log chokepoint — same shape as run_failures.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_NAME = "harness_bake_status.json"
BAKER_LOG_NAME = "harness_baker.jsonl"
OUTBOX_DIR = "harness_outbox"
PRUNE_REQUEST_NAME = "harness_prune_request.json"
CURSOR_NAME = "state/baker_log.offset"
HEARTBEAT_STALE_SECONDS = 30

IDLE: dict[str, Any] = {"state": "idle", "jobs": [], "detail": ""}


def _data_root(root: Path | None = None) -> Path:
    return Path(root) if root is not None else Path(
        os.environ.get("DEVCAKE_DATA_DIR", "/data"))


def read_bake_status(root: Path | None = None) -> dict[str, Any]:
    path = _data_root(root) / STATUS_NAME
    if not path.is_file():
        return dict(IDLE)
    try:
        body = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return dict(IDLE)
    if not isinstance(body, dict):
        return dict(IDLE)
    body.setdefault("state", "idle")
    body.setdefault("jobs", [])
    body.setdefault("detail", "")
    return body


def _parse_ts(raw: object) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    text = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def baker_liveness(status: dict[str, Any] | None, *,
                   now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    if not status:
        return {"alive": False, "reason": "host baker has not checked in"}
    ts = _parse_ts(status.get("heartbeat_at"))
    if ts is None:
        return {"alive": False, "reason": "host baker has not checked in"}
    age = (now - ts).total_seconds()
    if age > HEARTBEAT_STALE_SECONDS:
        return {
            "alive": False,
            "reason": f"host baker last checked in {int(age)}s ago",
        }
    return {"alive": True, "reason": ""}


def annotate_liveness(status: dict[str, Any] | None, *,
                      now: datetime | None = None) -> dict[str, Any]:
    body = dict(status or IDLE)
    live = baker_liveness(body, now=now)
    body["baker_alive"] = live["alive"]
    body["baker_detail"] = live["reason"]
    return body


def baker_transition(prev_alive: bool | None, alive: bool) -> str | None:
    if prev_alive is True and not alive:
        return "dead"
    if prev_alive is False and alive:
        return "alive"
    if prev_alive is None and not alive:
        return "dead"
    return None


def drain_baker_log(root: Path | None = None) -> list[dict[str, Any]]:
    """Return new baker records. Complete outbox files are claimed and
    deleted (baker→app mailbox). The legacy growing jsonl uses a cursor
    until emit_event writes only outbox files."""
    base = _data_root(root)
    records = _drain_outbox(base)
    log_path = base / BAKER_LOG_NAME
    cursor_path = base / CURSOR_NAME
    if not log_path.is_file():
        return records
    try:
        offset = int(cursor_path.read_text().strip() or "0")
    except (OSError, ValueError):
        offset = 0
    try:
        size = log_path.stat().st_size
    except OSError:
        return records
    if offset > size:
        offset = 0
    legacy: list[dict[str, Any]] = []
    try:
        with log_path.open() as fh:
            fh.seek(offset)
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    legacy.append(rec)
            new_offset = fh.tell()
    except OSError:
        return records
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(str(new_offset) + "\n")
    return records + legacy


def _drain_outbox(base: Path) -> list[dict[str, Any]]:
    box = base / OUTBOX_DIR
    if not box.is_dir():
        return []
    records: list[dict[str, Any]] = []
    for path in sorted(box.glob("*.jsonl")):
        if path.name.endswith(".taking"):
            continue
        taking = path.with_name(path.name + ".taking")
        try:
            path.replace(taking)
        except OSError:
            if taking.is_file():
                pass
            else:
                continue
        try:
            for line in taking.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
        except OSError:
            continue
        try:
            taking.unlink()
        except OSError:
            pass
    return records


def replay_baker_spans(records: list[dict[str, Any]], tracer) -> None:
    """Turn span-shaped outbox records into real OTEL spans (same trace_id).

    The host baker has no tracer. Poll is the chokepoint. Quiet / non-span
    records are ignored. Baker span_ids are replay keys; the exported tree
    parents children under the live parent span.
    """
    from opentelemetry import context as otel_context
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        SpanKind,
        Status,
        StatusCode,
        TraceFlags,
        set_span_in_context,
    )

    spans = [r for r in records
             if isinstance(r, dict) and r.get("event") == "span"
             and r.get("trace_id") and r.get("span_id") and r.get("name")]
    spans.sort(key=lambda r: int(r.get("start_ns") or 0))
    live: dict[str, object] = {}
    for rec in spans:
        try:
            tid = int(str(rec["trace_id"]), 16)
        except ValueError:
            continue
        parent_hex = str(rec.get("parent") or "")
        token = None
        if parent_hex and parent_hex in live:
            token = otel_context.attach(live[parent_hex])
        else:
            dummy = SpanContext(
                trace_id=tid, span_id=1, is_remote=True,
                trace_flags=TraceFlags(0x01))
            token = otel_context.attach(
                set_span_in_context(NonRecordingSpan(dummy)))
        try:
            start = rec.get("start_ns")
            end = rec.get("end_ns")
            kwargs: dict[str, Any] = {"kind": SpanKind.INTERNAL}
            if isinstance(start, int):
                kwargs["start_time"] = start
            span = tracer.start_span(str(rec["name"]), **kwargs)
            live[str(rec["span_id"])] = set_span_in_context(span)
            if rec.get("cause"):
                span.set_attribute(
                    "devcake.baker.cause", str(rec["cause"]))
            if rec.get("template"):
                span.set_attribute("devcake.harness", str(rec["template"]))
            if rec.get("cli_version"):
                span.set_attribute(
                    "devcake.cli_version", str(rec["cli_version"]))
            if rec.get("status") == "error":
                span.set_status(Status(StatusCode.ERROR))
            if isinstance(end, int):
                span.end(end_time=end)
            else:
                span.end()
        finally:
            if token is not None:
                otel_context.detach(token)


def write_prune_request(*, root: Path | None = None,
                        now: datetime | None = None) -> Path:
    """Ask the host baker to prune. No image names — the baker computes them."""
    now = now or datetime.now(timezone.utc)
    base = _data_root(root)
    base.mkdir(parents=True, exist_ok=True)
    dest = base / PRUNE_REQUEST_NAME
    body = json.dumps({"requested_at": now.isoformat()}) + "\n"
    fd, tmp = tempfile.mkstemp(dir=base, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(body)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, dest)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return dest


def request_prune(*, root: Path | None = None,
                  dev_types: dict | None = None) -> dict[str, Any]:
    """Prune inbox. When Dev Types are passed, also drop a fresh keep-set
    order so the baker has desired pins on the same tick."""
    if dev_types is not None:
        from .keep_set import publish_keep_set
        publish_keep_set(dev_types, root=root)
    write_prune_request(root=root)
    return {"ok": True, "requested": True}
