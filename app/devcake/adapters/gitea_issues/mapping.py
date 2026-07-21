"""Pure normalization helpers for the Gitea Issues PMO adapter.

Forge-issue profile (docs/05): open → backlog always; closed → done unless
the cancel footer is present → canceled. Priority is always medium (Gitea
issues have no Linear-style priority field).
"""

from __future__ import annotations

from typing import Literal

# Durable cancel signal in the issue body (plan lock-in: do not expand
# ALL_LABELS). cancel_mission appends this; normalize_status reads it.
CANCEL_FOOTER = "`devcake:canceled:v1`"

NormalizedStatus = Literal["backlog", "in_progress", "done", "canceled"]
Priority = Literal["urgent", "high", "medium", "low"]


def parse_team_ref(team_ref: str) -> tuple[str, str]:
    """team_key for gitea_issues is owner/repo (exactly two path segments)."""
    raw = (team_ref or "").strip().strip("/")
    segs = [s for s in raw.split("/") if s]
    if len(segs) != 2:
        raise ValueError(
            f"gitea_issues team_key must be owner/repo, got {team_ref!r}")
    return segs[0], segs[1]


def mission_key(owner: str, repo: str, index: int) -> str:
    return f"{owner}/{repo}#{index}"


def normalize_status(state: str, body: str | None) -> NormalizedStatus:
    """Map Gitea open/closed (+ cancel footer) onto DevCake normalized status."""
    if (state or "").lower() != "closed":
        return "backlog"
    if CANCEL_FOOTER in (body or ""):
        return "canceled"
    return "done"


def normalize_priority(_raw: object = None) -> Priority:
    return "medium"
