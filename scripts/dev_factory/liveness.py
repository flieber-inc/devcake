"""App-health chokepoint for the host baker.

The baker's own 5s loop calls classify_app / tick_decision — the same
shape as require_staffed, not a second supervisor thread. Down → exit.
Sentinel → heartbeat only. Ready → reconcile.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

SENTINEL = "DEVCAKE_APP_DIGEST_UNSET"
UNHEALTHY_NEED = 3


def classify_app(*, healthy: bool, digest: str | None,
                 checkout: str | None = None) -> str:
    if not healthy:
        return "down"
    if not digest or digest == SENTINEL:
        return "sentinel"
    if checkout is not None and checkout != digest:
        return "mismatch"
    return "ready"


def tick_decision(kind: str) -> str:
    if kind == "down":
        return "exit"
    if kind in ("sentinel", "mismatch"):
        return "heartbeat"
    return "reconcile"


def unhealthy_verdict(streak: int) -> bool:
    return streak >= UNHEALTHY_NEED


def stamp_heartbeat(payload: Mapping, *, now: datetime | None = None,
                    pid: int | None = None) -> dict:
    import os
    body = dict(payload)
    body["heartbeat_at"] = (now or datetime.now(timezone.utc)).isoformat()
    body["pid"] = int(pid if pid is not None else os.getpid())
    return body


def append_baker_event(path: Path | str, record: Mapping) -> dict:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(record)
    rec.setdefault("ts", datetime.now(timezone.utc).isoformat())
    with dest.open("a") as fh:
        fh.write(json.dumps(rec, separators=(",", ":")) + "\n")
    return rec
