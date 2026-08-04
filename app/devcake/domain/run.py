"""The Run record (docs/10): pure domain model, persisted by the files
adapter (adapters/files/run_store.py).

Advisory telemetry only (INV-1) — wiping /data/state never corrupts mission
state.
"""

from datetime import datetime, timezone
import hashlib
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

RunState = Literal[
    "dispatched", "running", "finalizing", "finished", "failed", "timed_out", "orphaned"
]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def auth_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class Run(BaseModel):
    schema_version: int = 2
    run_id: str
    mission_key: str
    mission_pmo_id: str = ""
    pmo_kind: str = "issue"
    # which configured instance served this run (AppConfig.pmos/repos entry id)
    pmo_ref: str = "main"
    repo_ref: str = "main"
    mission_type: str
    dev_type: str
    seq: int
    attempt_of_step: int = 1
    # Done direct blockers' work repos (non-secret snapshot at dispatch):
    # [{repo_ref, mission_key}] — tokens are attached at runspec time.
    # Empty on legacy / MAPPER / no-blocker runs.
    blocker_work: list[dict[str, str]] = Field(default_factory=list)
    # The mirror gate's needed_for set, snapshotted at dispatch (2026-08
    # evaluation F12): which repos this run's extras serve via mirror_path is
    # decided ONCE, when the gate proved them fresh — exactly like the
    # primary's DEVCAKE_MIRROR_PATH in spec_env. Empty on legacy records
    # (pre-field) → runspec falls back to the live derivation.
    mirror_repos: list[str] = Field(default_factory=list)
    stage_label_at_dispatch: Optional[str] = None
    # the PR branch minted at dispatch (schema v3): stored so review/merge
    # lookups can never drift from what the Dev actually pushed; "" on
    # legacy/mapper/hello records (ports.forge.run_branch derives those)
    branch: str = ""
    spec_prompt: str = ""
    # skill-store files for the Dev (non-secret, fetched at dispatch so a
    # mid-run Gitea outage can't change what a runspec re-request serves):
    # [{name, files: [{path, content_b64}]}]
    spec_skills: list[dict[str, Any]] = Field(default_factory=list)
    # HOME-relative dir the entrypoint writes spec_skills under — snapshotted
    # at dispatch from the harness registry so dir, skill content, and launch
    # image come from the same read; "" on legacy records → entrypoint default
    spec_skills_dir: str = ""
    state: RunState = "dispatched"
    created_at: datetime = Field(default_factory=utcnow)
    started_at: Optional[datetime] = None
    ended_at: Optional[datetime] = None
    last_heartbeat: Optional[datetime] = None
    timeout_seconds: int = 120 * 60
    traceparent: Optional[str] = None
    # One-way verifier for the per-run Redis envelope credential. The raw ACL
    # password is passed directly to Dagu and is never persisted in run state.
    auth_digest: Optional[str] = None
    spec_env: dict[str, str] = Field(default_factory=dict)
    finalized_steps: list[str] = Field(default_factory=list)
    result: Optional[dict[str, Any]] = None
    token_report: Optional[dict[str, Any]] = None
    artifact_bytes: Optional[int] = None
    error: Optional[str] = None
    # ADR-0018 — the STRUCTURED half of `error`. Every terminal path stamps one
    # (the taxonomy of docs/15 §1); "" means a pre-upgrade record, which
    # attempt counting still honours via a prefix match on `error`. Matching on
    # this field instead of on `error` closes an injection: the tail of `error`
    # can carry Dev-authored text (decomposition.py raises with the Dev's
    # blocked_by list verbatim), so a Dev could otherwise emit
    # blocked_by:["DEV_AUTH"] and make its own failures stop counting.
    error_class: str = ""
    # ADR-0018 — frozen at failure time, never recomputed. A correlated
    # backend fault does not burn the mission's attempt; that verdict must not
    # flip later when the backend heals and the evidence window clears, or the
    # excused missions would give up all at once.
    attempt_counted: bool = True
    # App-level judgment when it diverges from the executor's: a run can end
    # state="finished" (Dagu succeeded, artifacts were legal) yet carry a
    # verdict like "rejected: …" because _transition refused to act on the
    # outcome. None means an ordinary success.
    verdict: Optional[str] = None
    # ADR-0022 — how many in-container continuations (nudge relaunches) the
    # entrypoint used before this run ended, success or failure. 0 = the loop
    # never fired, and every pre-ADR-0022 record reads as 0.
    continuations_used: int = 0
    # Process-local wipe generation stamped at launch (docs/10): RunStore.clear
    # bumps wipe_generation then unlinks files; save() drops any run whose
    # store_gen is older so in-flight finalize/heartbeat cannot resurrect a
    # record after "start fresh". Default 0 = born before any wipe in-process
    # (legacy records load as 0). Optional so older JSON still validates.
    store_gen: int = 0
