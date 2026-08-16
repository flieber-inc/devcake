"""Pure normalization helpers for the GitLab Issues PMO adapter.

Forge-issue profile (docs/05 §9.2): opened → backlog; closed → done unless
the cancel footer is present → canceled. Priority is always medium.
"""

from __future__ import annotations

from typing import Literal
from urllib.parse import quote

from ..forge_issue import CANCEL_FOOTER

NormalizedStatus = Literal["backlog", "in_progress", "done", "canceled"]
Priority = Literal["urgent", "high", "medium", "low"]


def parse_team_ref(team_ref: str) -> str:
    """team_key is the GitLab path_with_namespace (owner/repo or group/sub/repo)."""
    raw = (team_ref or "").strip().strip("/")
    segs = [s for s in raw.split("/") if s]
    if len(segs) < 2:
        raise ValueError(
            f"gitlab_issues team_key must be namespace/project, got {team_ref!r}")
    return "/".join(segs)


def project_path_encoded(path_with_namespace: str) -> str:
    return quote(path_with_namespace, safe="")


def mission_key(path_with_namespace: str, iid: int) -> str:
    return f"{path_with_namespace}#{iid}"


def normalize_status(state: str, body: str | None) -> NormalizedStatus:
    st = (state or "").lower()
    if st not in ("closed",):
        return "backlog"
    if CANCEL_FOOTER in (body or ""):
        return "canceled"
    return "done"


def normalize_priority(_raw: object = None) -> Priority:
    return "medium"
