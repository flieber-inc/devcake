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

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException

from ..domain.model import MissionRef
from ..ports.pmo import PMOTransient
from ..security import redact


# Terminal states duplicated locally to avoid importing from `main.py` (which
# would cycle) — kept in lockstep with the `RunState` literal in domain/run.py.
TERMINAL_STATES: frozenset[str] = frozenset(
    {"finished", "failed", "timed_out", "orphaned"})


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


# ── 3) stop-run endpoint ────────────────────────────────────────────────────

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


# ── 5) force-poll endpoint ──────────────────────────────────────────────────
# The most on-philosophy write we ship: forcing a poll cycle is asking the PMO
# "what's true right now?" — INV-1 amplified, not sidestepped (docs/00 §4).
# Precedent: `POST /api/v1/relations-mapper/run` (docs/11 §1, docs/04 §1.6),
# the same "manual-dispatch mirrors a periodic cadence" shape.

async def force_poll_now(
    *,
    lock: asyncio.Lock,
    next_cycle_id: Callable[[], int],
    run_cycle: Callable[[int], Awaitable[None]],
) -> dict:
    """Run one poll cycle on demand. Returns cycle metadata.

    409 if a poll is already in flight — periodic or another manual trigger.
    The caller retries on the next tick; we deliberately do NOT queue behind
    the running cycle because the operator's intent is "do it now" and the
    next automatic cycle is at most `poll_interval_seconds` away anyway.

    The lock is released even if `run_cycle` raises — a leaked lock would
    permanently 409 every subsequent trigger. `run_poll_cycle` already swallows
    its own errors, so this is defence-in-depth: any escaped exception surfaces
    as 502 so the SPA can show honest failure copy.
    """
    if lock.locked():
        raise HTTPException(
            status_code=409,
            detail="a poll cycle is already in progress; try again shortly")
    async with lock:
        cycle_num = next_cycle_id()
        started = datetime.now(timezone.utc)
        try:
            await run_cycle(cycle_num)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"poll cycle raised: {type(e).__name__}: "
                       f"{str(e)[:150]}") from e
        elapsed_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        return {
            "ok": True,
            "cycle": cycle_num,
            "started_at": started.isoformat(),
            "duration_ms": elapsed_ms,
        }


# ── explicit re-exports (nice for `from ... import ...` in main.py) ─────────

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "TERMINAL_STATES",
    "force_poll_now",
    "label_action",
    "post_steering",
    "stop_run",
]
