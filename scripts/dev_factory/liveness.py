"""App-health chokepoint for the host baker.

The baker's own loop calls classify_app / tick_decision — the same
shape as require_staffed, not a second supervisor thread. Down → wait
with backoff for a minutes-scale WALL-CLOCK budget, then exit. Slow (the
container is running but /health/live cannot answer within the probe's
budget — a CPU-starved host) → heartbeat only, at the short retry, for
as long as it takes: a slow app is alive. Sentinel → heartbeat only.
Ready → reconcile.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping

SENTINEL = "DEVCAKE_APP_DIGEST_UNSET"
# Minutes-scale budget so a short app restart / deploy window does not
# kill the baker. Under systemd Restart=on-failure the final exit is
# recoverable; the point is not to exit during every bounce.
UNHEALTHY_BUDGET_S = 300
UNHEALTHY_BACKOFF_START_S = 5
UNHEALTHY_BACKOFF_CAP_S = 30
# A slow app is re-probed at the tick interval, not the down backoff: the
# app paints the baker dead once its heartbeat is 30 s old
# (bake_status.HEARTBEAT_STALE_SECONDS), and a slow tick already spends
# the probe's own budget before it can stamp.
SLOW_RETRY_S = 5.0


def classify_app(*, healthy: bool | str, digest: str | None,
                 checkout: str | None = None) -> str:
    """`healthy` is the probe's verdict: True/"ok", False/"down", or
    "slow" (container running, route unanswered) — a distinct kind."""
    if healthy == "slow":
        return "slow"
    if not healthy or healthy == "down":
        return "down"
    if not digest or digest == SENTINEL:
        return "sentinel"
    if checkout is not None and checkout != digest:
        return "mismatch"
    return "ready"


def tick_decision(kind: str) -> str:
    if kind == "down":
        return "exit"
    if kind in ("sentinel", "mismatch", "slow"):
        return "heartbeat"
    return "reconcile"


def container_state(text: str) -> str | None:
    """The app container's state from `docker compose ps -a [--format json]`
    output: NDJSON (newer compose), a JSON array (older), or the plain
    table. None when the container is not listed at all."""
    text = (text or "").strip()
    if not text:
        return None
    rows: list = []
    if text.startswith("["):
        try:
            rows = json.loads(text)
        except ValueError:
            rows = []
    elif text.startswith("{"):
        for line in text.splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    for row in rows:
        if isinstance(row, dict) and row.get("State"):
            return str(row["State"]).lower()
    for line in text.splitlines():
        low = line.lower()
        if low.startswith("name") or not line.strip():
            continue
        if " up " in f" {low} ":
            return "running"
        if "exited" in low or "exit " in low:
            return "exited"
        if "restarting" in low:
            return "restarting"
        if "created" in low:
            return "created"
        if "paused" in low:
            return "paused"
    return None


@dataclass
class AppClock:
    """The baker loop's memory between ticks: when the container was first
    seen not running (wall clock), how many down probes in a row, and when
    the app was first seen slow (for the one transition line)."""
    down_since: float | None = None
    streak: int = 0
    slow_since: float | None = None


def app_gate(liveness: str, clock: AppClock, *, now: float,
             budget_s: float = UNHEALTHY_BUDGET_S) -> tuple[str, float]:
    """The pure decision behind the loop: (action, delay_s) for one probe
    verdict at wall-clock `now`. `ok` → ("reconcile", 0) and the clock
    resets; `slow` → ("heartbeat", SLOW_RETRY_S), the down clock untouched;
    `down` → ("wait", backoff clipped to the budget left) until the budget
    is spent since the container was first seen not running, then
    ("exit", 0). Probe time counts: the clock is wall time, not sleep."""
    if liveness == "ok":
        clock.down_since = None
        clock.streak = 0
        clock.slow_since = None
        return "reconcile", 0.0
    if liveness == "slow":
        if clock.slow_since is None:
            clock.slow_since = now
        return "heartbeat", SLOW_RETRY_S
    clock.slow_since = None
    if clock.down_since is None:
        clock.down_since = now
    clock.streak += 1
    remaining = budget_s - (now - clock.down_since)
    if remaining <= 0:
        return "exit", 0.0
    return "wait", float(min(unhealthy_backoff_s(clock.streak), remaining))


def unhealthy_backoff_s(streak: int) -> float:
    """Backoff grows from START toward CAP. *streak* is 1-based fail count."""
    if streak < 1:
        streak = 1
    delay = UNHEALTHY_BACKOFF_START_S * (2 ** (streak - 1))
    return float(min(delay, UNHEALTHY_BACKOFF_CAP_S))


def unhealthy_verdict(*, elapsed_s: float,
                      budget_s: float = UNHEALTHY_BUDGET_S) -> bool:
    """True when the app-down wait budget is exhausted (baker should exit)."""
    return elapsed_s >= budget_s


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
