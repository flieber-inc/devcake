"""Per-mission repo resolution (M10, docs/16 F3): 0-or-1 configured repo per
mission, resolved as marker > instance default > zero-repo gate — and STICKY
once a run exists.

Stickiness is load-bearing (v0.1 plan finding H3): attempt 1 mints the branch
and PR on the resolved repo; if a marker edit re-routed a mission mid-flight,
rework would open a duplicate PR on the new repo and orphan the old one — the
PR-reuse invariant (M4) would silently break. So for a mission with run
history the latest run's repo_ref wins; a conflicting MARKER edit gates with
an explicit human-action reason, while a changed instance DEFAULT does not
gate — sticky wins silently (founder decision 2026-07-14, audit A25: a
config default edit must not park every in-flight mission of the instance).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .orchestrator.markers import RAW_REPO_MARKER, REPO_MARKER

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
    if marker is None:
        raw = RAW_REPO_MARKER.search(mission.description or "")
        if raw:
            # devcake-repo:-shaped but unparseable = a typo'd routing intent
            # — silently landing on the default (and then latching sticky
            # there) is the exact hazard the marker exists to avoid (A26)
            return None, (f"unparseable `devcake-repo:` marker "
                          f"{raw.group(1)[:40]!r} — repo names are lowercase "
                          f"alnum, ≤12 chars; fix the marker")

    sticky = next((r.repo_ref for r in run_history if r.repo_ref), None)
    if sticky is not None:
        if sticky not in repo_names:
            return None, (f"repo '{sticky}' (used by this mission's previous "
                          f"runs) is no longer configured — restore it or "
                          f"have a human close out the mission")
        if marker is not None and marker != sticky:
            return None, (f"repo marker changed mid-mission ('{sticky}' → "
                          f"'{marker}') — resolution is sticky once a run "
                          f"exists; remove the marker or have a human close "
                          f"out the mission on '{sticky}'")
        # a changed instance DEFAULT never gates: sticky wins silently
        # (founder decision 2026-07-14 — see module docstring)
        return sticky, None

    allowed = list(instance.repos or [])
    if marker is not None:
        if marker not in repo_names:
            return None, (f"unknown repo '{marker}' — fix the "
                          f"`devcake-repo:` marker (configured: "
                          f"{sorted(repo_names) or '(none)'})")
        if allowed and marker not in allowed:
            # the instance's repo SET is its allowed set (item 2): a marker
            # naming a configured-but-unlisted repo gates rather than
            # silently crossing the instance boundary
            return None, (f"repo '{marker}' is not in this PMO instance's "
                          f"repo set {allowed} — add it to the instance's "
                          f"repositories or fix the marker")
        return marker, None
    if allowed:
        # unmarked missions route to the FIRST entry (the default); config
        # cross-validates set members against repos — belt-and-braces here
        if allowed[0] not in repo_names:
            return None, (f"instance default repo '{allowed[0]}' "
                          f"is not configured")
        return allowed[0], None
    return None, REASON_ZERO_REPO
