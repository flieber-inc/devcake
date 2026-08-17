"""Span-shaped baker outbox records. No tracer — the app replays them."""

from __future__ import annotations

import secrets
from typing import Any, Mapping


def new_ids() -> tuple[str, str]:
    """(trace_id 32 hex, span_id 16 hex) — OTEL wire shape."""
    return secrets.token_hex(16), secrets.token_hex(8)


def span_record(
    *,
    name: str,
    trace_id: str,
    span_id: str,
    parent: str = "",
    start_ns: int,
    end_ns: int,
    status: str = "ok",
    cause: str = "",
    **extra: Any,
) -> dict[str, Any]:
    rec = {
        "event": "span",
        "name": name,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent": parent or "",
        "start_ns": int(start_ns),
        "end_ns": int(end_ns),
        "status": status,
        "cause": cause or "",
    }
    rec.update(extra)
    return rec


def probe_spans_from_receipt(
    receipt: Mapping[str, Any],
    *,
    trace_id: str,
    parent: str,
    start_ns: int,
    end_ns: int,
) -> list[dict[str, Any]]:
    """One child span per classify row that did not pass."""
    out: list[dict[str, Any]] = []
    for row in receipt.get("rows") or []:
        if not isinstance(row, Mapping):
            continue
        if row.get("status") == "pass":
            continue
        if row.get("kind") == "flag_accept":
            continue
        name = str(row.get("name") or "row")
        # plan_mode is flag_accept; skip non-classify without observed/cause
        if name == "plan_mode" and "observed" not in row and not row.get("cause"):
            continue
        _, sid = new_ids()
        status = "error" if row.get("status") in ("fail", "error") else "ok"
        out.append(span_record(
            name=f"baker.probe.{name}",
            trace_id=trace_id,
            span_id=sid,
            parent=parent,
            start_ns=start_ns,
            end_ns=end_ns,
            status=status,
            cause=str(row.get("cause") or ""),
            template=str(receipt.get("template") or ""),
            cli_version=str(receipt.get("cli_version") or ""),
        ))
    return out
