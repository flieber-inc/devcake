"""Application service for admin-UI mission actions (docs/05 §1, INV-1).

Sits between the FastAPI driving-adapter (`main.py`) and the domain/ports.
Depends on abstractions only — `PMOPort` (via each `MissionManager.pmo`),
`RunManager` / `RunStore` (via duck-typed protocols), and the pure domain
`MissionRef`. Never imports the concrete Linear adapter and never imports
`main.py` (would cycle at import time given the singleton wiring).

Each function raises `HTTPException` so the routes in `main.py` stay one-line
forwards and every status-code decision is unit-testable with fakes.

Precondition discipline:
- 409 for label preconditions is decided against the CACHED labels BEFORE the
  swap. Only `RuntimeError` bubbling out of `swap_labels` (e.g. a managed
  label was deleted in Linear between polls) is 502. Do not conflate them.
- Steering comments are posted via `pmo.post_feed` directly with plain
  markdown — NEVER via the orchestrator's `_feed` helper, which appends the
  `devcake:v1` sentinel. Without that sentinel the next poll classifies the
  comment as HUMAN, renders it as `🧑 HUMAN context`, and resets the attempt
  counter (docs/03 §8a).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from fastapi import HTTPException

from ..domain.model import MissionRef
from ..ports.pmo import PMOTransient
from ..security import redact


# Terminal states duplicated locally to avoid importing from `main.py` (which
# would cycle) — kept in lockstep with the `RunState` literal in domain/run.py.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"finished", "failed", "timed_out", "orphaned"})

# The PMOPort priority vocabulary (adapters map it to vendor codes by dict
# lookup — validate here so a bad value 422s instead of 500ing in the adapter).
PRIORITIES: frozenset[str] = frozenset({"urgent", "high", "medium", "low"})


# ── SOLID/OCP: label actions as data, not a switch statement ────────────────

@dataclass(frozen=True)
class ActionSpec:
    """Precondition + label swap for one UI action.

    `require_present` and `require_absent` are validated against the CACHED
    row labels; violation is a 409 and the port is never called. `remove` and
    `add` are handed to `pmo.swap_labels`.
    """
    require_present: frozenset[str] = frozenset()
    require_absent:  frozenset[str] = frozenset()
    remove:          frozenset[str] = frozenset()
    add:             frozenset[str] = frozenset()


ACTION_SPECS: dict[str, ActionSpec] = {
    "retry":  ActionSpec(require_present=frozenset({"DEVCAKE-FAILED"}),
                         remove=frozenset({"DEVCAKE-FAILED"})),
    "park":   ActionSpec(require_absent=frozenset({"DEVCAKE-SKIP"}),
                         add=frozenset({"DEVCAKE-SKIP"})),
    "unpark": ActionSpec(require_present=frozenset({"DEVCAKE-SKIP"}),
                         remove=frozenset({"DEVCAKE-SKIP"})),
    "resume": ActionSpec(require_present=frozenset({"DEVCAKE-NEEDS-HUMAN"}),
                         remove=frozenset({"DEVCAKE-NEEDS-HUMAN"})),
}


# ── narrow duck-typed protocols (ISP) ───────────────────────────────────────

class _RunStore(Protocol):
    def get(self, run_id: str) -> Any: ...


class _RunManager(Protocol):
    async def kill(self, run: Any, new_state: str, reason: str) -> None: ...


# ── helpers ─────────────────────────────────────────────────────────────────

def _find_row(missions_cache: list[dict], pmo_id: str) -> dict:
    for row in missions_cache:
        if row.get("pmo_id") == pmo_id:
            return row
    raise HTTPException(status_code=404, detail="mission not found")


def _resolve_mgr(managers: dict[str, Any], instance_name: str) -> Any:
    mgr = managers.get(instance_name)
    if mgr is None:
        raise HTTPException(
            status_code=409,
            detail=f"instance {instance_name!r} is no longer configured")
    return mgr


def _try_audit(mgr: Any, pmo_id: str, action: str, detail: str = "") -> None:
    """Audit is advisory — never fail an action because of an audit-write hiccup."""
    fn = getattr(mgr, "_audit", None)
    if fn is None:
        return
    try:
        fn(pmo_id, action, detail)
    except Exception:  # pragma: no cover - defensive
        pass


# ── 1) label actions: retry / park / unpark / resume ────────────────────────

async def label_action(
    pmo_id: str,
    action: str,
    *,
    missions_cache: list[dict],
    managers: dict[str, Any],
) -> dict:
    """Apply the label swap for one UI action. Returns the projected label list."""
    spec = ACTION_SPECS.get(action)
    if spec is None:
        raise HTTPException(
            status_code=422,
            detail=f"unknown action: {action!r}; "
                   f"expected one of {sorted(ACTION_SPECS)}")

    row = _find_row(missions_cache, pmo_id)
    mgr = _resolve_mgr(managers, row["instance"])

    labels = set(row.get("labels") or [])
    missing = spec.require_present - labels
    if missing:
        raise HTTPException(
            status_code=409,
            detail=f"cannot {action}: missing labels {sorted(missing)}")
    present_but_forbidden = spec.require_absent & labels
    if present_but_forbidden:
        raise HTTPException(
            status_code=409,
            detail=f"cannot {action}: labels already present "
                   f"{sorted(present_but_forbidden)}")

    ref = MissionRef(pmo_id, row["kind"])
    try:
        await mgr.pmo.swap_labels(ref, set(spec.remove), set(spec.add))
    except PMOTransient as e:
        raise HTTPException(
            status_code=502,
            detail=f"pmo transient error while swapping labels: {e}") from e
    except RuntimeError as e:
        # A managed label was deleted in Linear between polls, or the adapter
        # otherwise refused the swap. Distinct from a precondition failure.
        raise HTTPException(
            status_code=502,
            detail=f"pmo rejected label swap: {e}") from e

    projected = sorted((labels - set(spec.remove)) | set(spec.add))
    _try_audit(mgr, pmo_id, f"ui_{action}", "")
    return {"labels": projected}


# ── 2) steering / comment endpoint ──────────────────────────────────────────

async def post_steering(
    pmo_id: str,
    body: str,
    *,
    missions_cache: list[dict],
    managers: dict[str, Any],
) -> dict:
    """Post a human-authored feed comment (no sentinel) on behalf of the operator."""
    if not (body and body.strip()):
        raise HTTPException(status_code=422, detail="body must not be blank")

    row = _find_row(missions_cache, pmo_id)
    mgr = _resolve_mgr(managers, row["instance"])

    ref = MissionRef(pmo_id, row["kind"])
    redacted = redact(body)
    try:
        # CRITICAL: bypass `_feed` on purpose — appending COMMENT_SENTINEL
        # would classify this as a DevCake-authored record and skip the
        # attempt-counter reset (docs/03 §8a).
        await mgr.pmo.post_feed(ref, redacted)
    except PMOTransient as e:
        raise HTTPException(
            status_code=502,
            detail=f"pmo transient error while posting feed: {e}") from e
    except RuntimeError as e:
        # The adapter raises RuntimeError for GraphQL-level errors that are
        # not transient (revoked token, deleted mission, etc.). 502 keeps
        # the "PMO refused us" family together — never leak as 500.
        raise HTTPException(
            status_code=502,
            detail=f"pmo rejected feed post: {e}") from e

    _try_audit(mgr, pmo_id, "ui_steer", redacted[:120])
    return {"ok": True}


# ── 3) create-mission endpoint ──────────────────────────────────────────────

async def create_mission(
    *,
    title: str,
    description: str = "",
    priority: str = "medium",
    instance_name: str | None,
    managers: dict[str, Any],
    team_keys: dict[str, str],
) -> dict:
    """Create a new backlog mission with only the `DEVCAKE` opt-in label.

    ONBOARD triage happens next cycle via the native flow. `team_key` is passed
    as `team_ref` (positional) into `PMOPort.create_mission`.
    """
    if not (title and title.strip()):
        raise HTTPException(status_code=422, detail="title must not be blank")
    if priority not in PRIORITIES:
        # the Linear adapter maps priority via dict lookup — an unknown value
        # would KeyError into a 500 there; refuse it at the boundary instead
        raise HTTPException(
            status_code=422,
            detail=f"unknown priority {priority!r}; "
                   f"expected one of {sorted(PRIORITIES)}")

    if instance_name is None:
        first = next(iter(managers), None)
        if first is None:
            raise HTTPException(
                status_code=409,
                detail="no PMO instance is configured; add one in Configuration")
        instance_name = first

    mgr = _resolve_mgr(managers, instance_name)
    team_key = team_keys.get(instance_name, "")
    if not team_key:
        raise HTTPException(
            status_code=409,
            detail=f"instance {instance_name!r} has no team_key configured")

    clean_title = title.strip()
    try:
        key, pmo_id = await mgr.pmo.create_mission(
            team_key, clean_title, description, priority, {"DEVCAKE"})
    except PMOTransient as e:
        raise HTTPException(
            status_code=502,
            detail=f"pmo transient error while creating mission: {e}") from e
    except RuntimeError as e:
        # Non-transient adapter errors (revoked token, unknown team) surface
        # as 502 — never let a bare RuntimeError leak as 500 to the SPA.
        raise HTTPException(
            status_code=502,
            detail=f"pmo rejected mission creation: {e}") from e

    _try_audit(mgr, pmo_id, "ui_create", clean_title[:120])
    return {"key": key, "pmo_id": pmo_id, "instance": instance_name}


# ── 4) stop-run endpoint ────────────────────────────────────────────────────

async def stop_run(
    run_id: str,
    *,
    run_manager: _RunManager,
    run_store: _RunStore,
) -> dict:
    """Kill an in-flight run. Counts as a failed attempt (documented in the UI copy)."""
    run = run_store.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="run not found")
    if getattr(run, "state", None) in TERMINAL_STATES:
        raise HTTPException(
            status_code=409,
            detail=f"run is already terminal (state={run.state!r})")
    if getattr(run, "state", None) == "finalizing":
        # The Dev container has already exited — there is nothing to stop,
        # only app-side finalize bookkeeping to corrupt. Killing here races
        # the in-flight finalize coroutine (conflicting PMO writes, burned
        # attempt for completed Dev work); the watchdog deliberately never
        # kills finalizing either, and its stall deadline already handles the
        # pathological dead-letter case (domain/watchdog.py).
        raise HTTPException(
            status_code=409,
            detail="run is finalizing — the Dev has already exited and "
                   "DevCake is finishing bookkeeping; it completes or fails "
                   "on its own")

    await run_manager.kill(run, "failed", "stopped by operator from the admin UI")
    return {"ok": True, "run_id": run_id, "state": "failed"}


# ── explicit re-exports (nice for `from ... import ...` in main.py) ─────────

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "TERMINAL_STATES",
    "create_mission",
    "label_action",
    "post_steering",
    "stop_run",
]
