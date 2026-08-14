"""Cron fire-outcome persistence: one JSON file under /data/state
(PLAN_MEMORY §6.2). Degradation must be store-derived and restart-safe
like the Steward's — but Steward derives from the run store because
steward runs ARE runs, while cron fires create PMO tickets, so they
need this tiny ledger of their own. Advisory telemetry only (INV-1):
wiping it re-arms automatic fires, nothing else."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import datetime
from pathlib import Path

CRON_STATE_PATH = (Path(os.environ.get("DEVCAKE_DATA_DIR", "/data"))
                   / "state" / "cron_outcomes.json")

OUTCOME_WINDOW = 3  # last N outcomes per job drive the degraded signal

log = logging.getLogger("devcake.cronstore")


class CronStore:
    """{job_id: {"outcomes": [last 3 of created/skipped/failed],
    "last_fire_at": iso}} — outcomes drive degradation, last_fire_at
    drives the elapsed-interval schedule (one attempt per window,
    restart-safe)."""

    def __init__(self, path: Path = CRON_STATE_PATH):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, dict] = self._load()

    def _load(self) -> dict[str, dict]:
        try:
            raw = json.loads(self.path.read_text())
        except FileNotFoundError:
            return {}
        except Exception:  # noqa: BLE001 — a corrupt ledger re-arms, never wedges
            log.exception("cron outcome ledger unreadable — starting empty")
            return {}
        if not isinstance(raw, dict):
            return {}
        return {k: dict(v) for k, v in raw.items() if isinstance(v, dict)}

    def _write(self) -> None:
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(self._state, indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def record(self, job_id: str, outcome: str, *,
               fired_at: str | None = None) -> None:
        row = self._state.setdefault(job_id, {})
        outs = list(row.get("outcomes") or [])
        outs.append(outcome)
        row["outcomes"] = outs[-OUTCOME_WINDOW:]
        if fired_at:
            row["last_fire_at"] = fired_at
        try:
            self._write()
        except Exception:  # noqa: BLE001 — telemetry write must not fail a fire
            log.exception("cron outcome ledger write failed")

    def outcomes(self, job_id: str) -> list[str]:
        return list(self._state.get(job_id, {}).get("outcomes") or [])

    def last_fire_at(self, job_id: str) -> datetime | None:
        raw = self._state.get(job_id, {}).get("last_fire_at")
        if not raw:
            return None
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
