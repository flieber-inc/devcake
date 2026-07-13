"""PMOPort — the ONE authoritative contract every PMO adapter implements
(docs/05 §1). Reads and writes are keyed by MissionRef: the adapter dispatches
on ref.kind internally, so vendor dualities (Linear's issue/project split)
never leak into the domain. Normalized DTOs live in domain.model."""

from typing import Optional, Protocol

from pydantic import BaseModel

from ..domain.model import Activity, Mission, MissionRef, NormalizedStatus


class PMOTransient(Exception):
    """Retryable PMO failure (429/5xx/network) — docs/15. Adapters raise this
    for anything worth retrying next poll cycle; everything else is permanent."""


class PMOHealth(BaseModel):
    """Neutral connection-probe result (docs/05): replaces the old private
    reach-ins into vendor JSON from /health and the admin test endpoint."""
    ok: bool
    workspace: str = ""                 # resolved team/workspace reference
    managed_labels_present: int = 0     # of DevCake's managed set, found remotely
    managed_labels_expected: int = 0
    detail: str = ""


class PMOCapabilities(BaseModel):
    projects_supported: bool
    project_labels_supported: bool
    attachment_max_bytes: int
    native_label_swap_atomic: bool
    relations_supported: bool = False


class PMOPort(Protocol):
    """Every operation DevCake needs from a PMO system. Contract notes:

    - `get_activity` on a ref without a comment feed (Linear projects) returns
      the mission with `entries=[]` — never raises.
    - `post_feed` targets the kind-appropriate channel (issue comment /
      project update). Feed POLICY (redaction, sentinel, suppression) is the
      orchestrator's job; transport is the adapter's.
    - `create_mission` returns `(key, pmo_id)` — the id wires relation edges.
    - `create_relation` is duplicate-tolerant (decomposition resume, ADR-0007).
    - `ensure_labels` creates the managed label set in every namespace the
      vendor requires (Linear: team issue labels + workspace project labels).
    """

    # ── reads ────────────────────────────────────────────────────────────────
    async def list_missions(self, team_ref: str) -> list[Mission]: ...
    async def list_all(self, team_ref: str) -> list[Mission]: ...
    async def get(self, ref: MissionRef) -> Mission: ...
    async def get_activity(self, ref: MissionRef) -> Activity: ...
    async def children_of(self, ref: MissionRef) -> list[Mission]: ...

    # ── writes ───────────────────────────────────────────────────────────────
    async def post_feed(self, ref: MissionRef, markdown: str) -> None: ...
    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None: ...
    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None: ...
    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: Optional[str] = None) -> tuple[str, str]: ...
    async def create_relation(self, blocker_id: str, blocked_id: str) -> None: ...
    async def ensure_labels(self, team_ref: str, names: set[str]) -> None: ...

    # ── assets ───────────────────────────────────────────────────────────────
    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str: ...
    async def download_asset(self, url: str) -> bytes: ...

    # ── meta ─────────────────────────────────────────────────────────────────
    async def health_probe(self, team_ref: str) -> PMOHealth: ...
    def capabilities(self) -> PMOCapabilities: ...
