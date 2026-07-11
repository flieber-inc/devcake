"""Run records: one JSON file per run under /data/state/runs (docs/10).

Advisory telemetry only (INV-1) — wiping /data/state never corrupts mission
state. Atomic writes via tmp + fsync + rename.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

RUNS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "state" / "runs"

RunState = Literal[
    "dispatched", "running", "finalizing", "finished", "failed", "timed_out", "orphaned"
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(BaseModel):
    schema_version: int = 1
    run_id: str
    mission_key: str
    mission_type: str
    dev_type: str
    seq: int
    attempt_of_step: int = 1
    state: RunState = "dispatched"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    timeout_seconds: int = 120 * 60
    traceparent: Optional[str] = None
    # Per-run scoped Redis ACL password (docs/09 §1a). Local, 0600-dir, and
    # revoked at finalization — acceptable at-rest exposure for v0.
    redis_password: Optional[str] = None
    spec_env: dict[str, str] = Field(default_factory=dict)
    spec_files: list[dict[str, Any]] = Field(default_factory=list)
    finalized_steps: list[str] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    artifact_bytes: Optional[int] = None
    error: Optional[str] = None


class RunStore:
    def __init__(self, root: Path = RUNS_DIR):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def save(self, run: Run) -> None:
        path = self.root / f"{run.run_id}.json"
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(run.model_dump_json(indent=2))
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, run_id: str) -> Optional[Run]:
        path = self.root / f"{run_id}.json"
        if not path.exists():
            return None
        return Run.model_validate_json(path.read_text())

    def all(self) -> list[Run]:
        runs = []
        for p in sorted(self.root.glob("*.json")):
            try:
                runs.append(Run.model_validate_json(p.read_text()))
            except Exception:
                continue  # unreadable file: skip, never crash the loop
        return runs

    def active(self) -> list[Run]:
        return [r for r in self.all() if r.state in ("dispatched", "running", "finalizing")]
