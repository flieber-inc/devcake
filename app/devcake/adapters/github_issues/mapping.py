"""Pure normalization helpers for the GitHub Issues PMO adapter.

Forge-issue profile (docs/05 §9.2): open → backlog; closed → done unless
the cancel footer is present → canceled. Priority is always medium.
"""

from __future__ import annotations

from typing import Literal

from ..forge_issue import CANCEL_FOOTER

NormalizedStatus = Literal["backlog", "in_progress", "done", "canceled"]
Priority = Literal["urgent", "high", "medium", "low"]


def parse_team_ref(team_ref: str) -> tuple[str, str]:
    raw = (team_ref or "").strip().strip("/")
    segs = [s for s in raw.split("/") if s]
    if len(segs) != 2:
        raise ValueError(
            f"github_issues team_key must be owner/repo, got {team_ref!r}")
    return segs[0], segs[1]


def mission_key(owner: str, repo: str, number: int) -> str:
    return f"{owner}/{repo}#{number}"


def normalize_status(state: str, body: str | None) -> NormalizedStatus:
    if (state or "").lower() != "closed":
        return "backlog"
    if CANCEL_FOOTER in (body or ""):
        return "canceled"
    return "done"


def normalize_priority(_raw: object = None) -> Priority:
    return "medium"
