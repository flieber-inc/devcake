"""PMO domain: normalized DTOs, the managed label set, and Mission Type
derivation (docs/02). Pure logic — no I/O, no vendor types (docs/01 §3)."""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional, Protocol

from pydantic import BaseModel, Field

# ── The nine managed labels (docs/02 §5 — defined once) ─────────────────────

LABEL_OPTIN = "DEVCAKE"
LABEL_PLAN = "DEVCAKE-PLAN"
LABEL_EXECUTE = "DEVCAKE-EXECUTE"
LABEL_REVIEW = "DEVCAKE-REVIEW"
LABEL_MERGE = "DEVCAKE-MERGE"
LABEL_CREATED = "DEVCAKE-CREATED"
LABEL_FAILED = "DEVCAKE-FAILED"
LABEL_SKIP = "DEVCAKE-SKIP"
LABEL_TRACKING = "DEVCAKE-TRACKING"

ALL_LABELS = {LABEL_OPTIN, LABEL_PLAN, LABEL_EXECUTE, LABEL_REVIEW, LABEL_MERGE,
              LABEL_CREATED, LABEL_FAILED, LABEL_SKIP, LABEL_TRACKING}
STAGE_LABELS = {LABEL_PLAN, LABEL_EXECUTE, LABEL_REVIEW}

NormalizedStatus = Literal["backlog", "in_progress", "done", "canceled"]
Priority = Literal["urgent", "high", "medium", "low"]

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3}


class MissionType(str, Enum):
    ONBOARD = "ONBOARD"
    PLAN = "PLAN"
    EXECUTE = "EXECUTE"
    REVIEW = "REVIEW"


class Mission(BaseModel):
    pmo_id: str
    pmo_kind: Literal["issue", "project"]
    key: str
    title: str
    description: str = ""
    status: NormalizedStatus
    priority: Priority = "medium"
    labels: set[str] = Field(default_factory=set)
    updated_at: datetime
    url: str = ""
    parent_ref: Optional[str] = None


class Derivation(BaseModel):
    mission_type: Optional[MissionType] = None
    schedulable: bool = False
    reason: str  # row of the derivation table that fired (for /api/v1/missions + logs)


def derive(mission: Mission, adoption_mode: str) -> Derivation:
    """The normative derivation table, docs/02 §2 (incl. the adoption gate)."""
    labels = mission.labels
    stage = labels & STAGE_LABELS

    if mission.status in ("done", "canceled"):                       # row 5
        return Derivation(reason="terminal — ignored")
    if adoption_mode == "opt_in" and LABEL_OPTIN not in labels:      # adoption gate
        return Derivation(reason="not adopted (opt-in mode, no DEVCAKE label)")
    if len(stage) >= 2:                                              # row 6
        return Derivation(reason="LABEL_CONFLICT — ≥2 stage labels; human must fix")
    if LABEL_SKIP in labels:                                         # row 7
        return Derivation(reason="DEVCAKE-SKIP — human opt-out")
    if LABEL_FAILED in labels:                                       # row 8
        return Derivation(reason="DEVCAKE-FAILED — needs human attention")
    if LABEL_MERGE in labels:                                        # row 10
        return Derivation(reason="awaiting merge (merge sweep handles)")
    if stage == {LABEL_PLAN}:                                        # row 2
        return Derivation(mission_type=MissionType.PLAN, schedulable=True, reason="stage label")
    if stage == {LABEL_EXECUTE}:                                     # row 3
        return Derivation(mission_type=MissionType.EXECUTE, schedulable=True, reason="stage label")
    if stage == {LABEL_REVIEW}:                                      # row 4
        return Derivation(mission_type=MissionType.REVIEW, schedulable=True, reason="stage label")
    if mission.status == "backlog":                                  # row 1
        return Derivation(mission_type=MissionType.ONBOARD, schedulable=True,
                          reason="backlog, no stage label")
    return Derivation(reason="in_progress without stage label — not DevCake's")  # row 9


class ActivityEntry(BaseModel):
    ts: datetime
    author: str
    kind: Literal["comment", "status_change", "attachment"]
    body: str = ""
    attachments: list[str] = Field(default_factory=list)  # asset URLs


class Activity(BaseModel):
    mission: Mission
    entries: list[ActivityEntry]


class PMOCapabilities(BaseModel):
    projects_supported: bool
    project_labels_supported: bool
    attachment_max_bytes: int
    native_label_swap_atomic: bool


class PMOPort(Protocol):
    """docs/05 §1 — M2 implements the read path + labels; writes land M3–M5."""

    async def list_missions(self, team_ref: str) -> list[Mission]: ...
    async def get_mission(self, pmo_id: str) -> Mission: ...
    async def get_activity(self, pmo_id: str) -> Activity: ...
    async def post_comment(self, pmo_id: str, markdown: str) -> None: ...
    async def set_status(self, pmo_id: str, status: NormalizedStatus) -> None: ...
    async def swap_labels(self, pmo_id: str, remove: set[str], add: set[str]) -> None: ...
    async def ensure_labels(self, team_ref: str, names: set[str]) -> None: ...
    def capabilities(self) -> PMOCapabilities: ...
