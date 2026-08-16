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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATUS_NAME = "harness_bake_status.json"
BAKER_LOG_NAME = "harness_baker.jsonl"
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
    """Return new jsonl records since the last drain. Cursor is a byte offset."""
    base = _data_root(root)
    log_path = base / BAKER_LOG_NAME
    cursor_path = base / CURSOR_NAME
    if not log_path.is_file():
        return []
    try:
        offset = int(cursor_path.read_text().strip() or "0")
    except (OSError, ValueError):
        offset = 0
    try:
        size = log_path.stat().st_size
    except OSError:
        return []
    if offset > size:
        offset = 0
    records: list[dict[str, Any]] = []
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
                    records.append(rec)
            new_offset = fh.tell()
    except OSError:
        return []
    cursor_path.parent.mkdir(parents=True, exist_ok=True)
    cursor_path.write_text(str(new_offset) + "\n")
    return records
