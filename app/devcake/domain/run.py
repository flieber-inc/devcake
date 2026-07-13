"""The Run record (docs/10): pure domain model, persisted by the files
adapter (adapters/files/run_store.py).

Advisory telemetry only (INV-1) — wiping /data/state never corrupts mission
state.
"""

from datetime import datetime, timezone
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

RunState = Literal[
    "dispatched", "running", "finalizing", "finished", "failed", "timed_out", "orphaned"
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Run(BaseModel):
    schema_version: int = 1
    run_id: str
    mission_key: str
    mission_pmo_id: str = ""
    pmo_kind: str = "issue"
    # which configured instance served this run (AppConfig.pmos/repos entry id;
    # additive with defaults, so pre-v2 run JSONs parse unchanged)
    pmo_ref: str = "main"
    repo_ref: str = "main"
    mission_type: str
    dev_type: str
    seq: int
    attempt_of_step: int = 1
    stage_label_at_dispatch: Optional[str] = None
    spec_prompt: str = ""
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
    token_report: Optional[dict[str, Any]] = None
    artifact_bytes: Optional[int] = None
    error: Optional[str] = None
    # App-level judgment when it diverges from the executor's: a run can end
    # state="finished" (Dagu succeeded, artifacts were legal) yet carry a
    # verdict like "rejected: …" because _transition refused to act on the
    # outcome. None means an ordinary success.
    verdict: Optional[str] = None

