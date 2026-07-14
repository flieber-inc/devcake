"""Per-mission repo resolution (M10, docs/16 F3): 0-or-1 configured repo per
mission, resolved as marker > instance default > zero-repo gate — and STICKY
once a run exists.

Stickiness is load-bearing (v0.1 plan finding H3): attempt 1 mints the branch
and PR on the resolved repo; if a marker edit or default change re-routed a
mission mid-flight, rework would open a duplicate PR on the new repo and
orphan the old one — the PR-reuse invariant (M4) would silently break. So for
a mission with run history the latest run's repo_ref wins unconditionally,
and a conflicting edit GATES the mission with an explicit human-action reason
instead of re-routing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .orchestrator.markers import REPO_MARKER

if TYPE_CHECKING:
    from ..config import PMOInstance
    from .model import Mission
    from .run import Run

# gate reasons (also matched by tests; keep stable)
REASON_ZERO_REPO = ("no repository resolved — gated until the internal "
                    "fallback forge (M11)")


def marker_repo(description: str) -> str | None:
    """The `devcake-repo:<name>` override from a mission description
    (first match wins; the name is lowercased)."""
    m = REPO_MARKER.search(description or "")
    return m.group(1).lower() if m else None


def resolve_repo(mission: "Mission", instance: "PMOInstance",
                 repo_names: set[str],
                 run_history: "list[Run]") -> tuple[str | None, str | None]:
    """→ (repo_name, None) when resolved; (None, reason) when gated.

    `run_history`: this mission's prior runs (any state), newest first —
    only their repo_ref is read. Mapper/hello records never carry a
    mission's repo and must not be passed in.
    """
    marker = marker_repo(mission.description)
    fresh = marker if marker is not None else instance.default_repo

    sticky = next((r.repo_ref for r in run_history if r.repo_ref), None)
    if sticky is not None:
        if sticky not in repo_names:
            return None, (f"repo '{sticky}' (used by this mission's previous "
                          f"runs) is no longer configured — restore it or "
                          f"have a human close out the mission")
        if fresh is not None and fresh != sticky:
            return None, (f"repo routing changed mid-mission ('{sticky}' → "
                          f"'{fresh}') — resolution is sticky once a run "
                          f"exists; remove the change or have a human close "
                          f"out the mission on '{sticky}'")
        return sticky, None

    if marker is not None:
        if marker not in repo_names:
            return None, (f"unknown repo '{marker}' — fix the "
                          f"`devcake-repo:` marker (configured: "
                          f"{sorted(repo_names) or '(none)'})")
        return marker, None
    if instance.default_repo is not None:
        # config cross-validates default_repo against repos; belt-and-braces
        if instance.default_repo not in repo_names:
            return None, (f"instance default repo '{instance.default_repo}' "
                          f"is not configured")
        return instance.default_repo, None
    return None, REASON_ZERO_REPO
