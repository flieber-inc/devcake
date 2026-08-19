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
import base64
import logging
import re
import time

log = logging.getLogger("devcake.missions")
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Protocol

from fastapi import HTTPException

from ..domain import failure_taxonomy
from ..domain.model import LABEL_MERGE, MissionRef
from ..domain.orchestrator import freshness
from ..domain.run import TERMINAL_STATES
from ..ports.pmo import PMOTransient
from ..security import redact


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
    async def kill(self, run: Any, new_state: str, reason: str, *,
                   error_class: str | None = None) -> None: ...


# ── helpers ─────────────────────────────────────────────────────────────────

def _find_row(missions_cache: list[dict], pmo_id: str,
              instance: str | None = None) -> dict:
    """Resolve one mission row. `pmo_id` alone matched the FIRST cache row
    (mission_actions.py:90, 2026-08-12 audit) — but gitea_issues pmo_ids are
    per-repo issue NUMBERS, so #3 collides across instances and the action
    could land on the wrong board. When the caller qualifies with `instance`
    (the SPA does whenever ≥2 PMOs are configured), match both; a bare id
    that now resolves to >1 instance is a 409, not a silent wrong-board hit."""
    if instance:
        for row in missions_cache:
            if row.get("pmo_id") == pmo_id and row.get("instance") == instance:
                return row
        raise HTTPException(
            status_code=404,
            detail=f"mission {pmo_id} not found on instance {instance!r}")
    matches = [row for row in missions_cache if row.get("pmo_id") == pmo_id]
    if not matches:
        raise HTTPException(status_code=404, detail="mission not found")
    if len({row.get("instance") for row in matches}) > 1:
        raise HTTPException(
            status_code=409,
            detail=f"mission id {pmo_id} exists on multiple instances "
                   f"({sorted({r.get('instance') for r in matches})}) — "
                   f"retry with an explicit instance")
    return matches[0]


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
    except Exception:  # noqa: BLE001 — audit is advisory; failure is logged, never surfaced
        log.debug("audit write failed for %s/%s", pmo_id, action, exc_info=True)


# ── 1) label actions: retry / park / unpark / resume ────────────────────────

_FORCE_REASONS = {
    "pass": "nothing_unread",
    "tripped": "reopened",
    "exhausted": "budget_exhausted",
    "no_review_anchor": "no_review_anchor",
}


async def force_freshness(
    pmo_id: str,
    *,
    missions_cache: list[dict],
    managers: dict[str, Any],
    instance: str | None = None,
) -> dict:
    """Operator re-check: if material landed after the last REVIEW watermark
    while the mission sits on DEVCAKE-MERGE, post a freshness re-review
    directive and re-open REVIEW. See freshness.recheck_and_maybe_rereview."""
    row = _find_row(missions_cache, pmo_id, instance)
    mgr = _resolve_mgr(managers, row["instance"])
    labels = set(row.get("labels") or [])
    if LABEL_MERGE not in labels:
        raise HTTPException(
            status_code=409,
            detail="cannot force_freshness: missing labels "
                   f"['{LABEL_MERGE}']")
    ref = MissionRef(pmo_id, row["kind"])
    try:
        mission = await mgr.pmo.get(ref)
    except PMOTransient as e:
        raise HTTPException(
            status_code=502,
            detail=f"pmo transient error while loading mission: {e}") from e
    except Exception as e:  # noqa: BLE001 — surface adapter refusal as 502, not 500
        raise HTTPException(
            status_code=502,
            detail=f"pmo rejected mission get: {e}") from e

    try:
        outcome = await freshness.recheck_and_maybe_rereview(
            mgr, mission, reason="operator_force")
    except PMOTransient as e:
        raise HTTPException(
            status_code=502,
            detail=f"pmo transient error during freshness recheck: {e}") from e
    except Exception as e:  # noqa: BLE001 — recheck failures must not 500 the UI; 502 keeps the PMO/forge family together
        log.exception("force_freshness recheck failed for %s", pmo_id)
        raise HTTPException(
            status_code=502,
            detail=f"freshness recheck failed: {e}") from e

    # recheck mutates mission.labels on trip (and FakePMO.swap_labels does too);
    # otherwise keep the cache projection the UI already had.
    if getattr(mission, "labels", None) is not None:
        projected = sorted(mission.labels)
    else:
        projected = sorted(labels)

    reason = _FORCE_REASONS.get(outcome, outcome)
    _try_audit(mgr, pmo_id, "ui_force_freshness", reason)
    return {
        "labels": projected,
        "tripped": outcome == "tripped",
        "reason": reason,
    }


async def label_action(
    pmo_id: str,
    action: str,
    *,
    missions_cache: list[dict],
    managers: dict[str, Any],
    instance: str | None = None,
) -> dict:
    """Apply the label swap for one UI action. Returns the projected label list."""
    if action == "force_freshness":
        return await force_freshness(
            pmo_id, missions_cache=missions_cache, managers=managers,
            instance=instance)

    spec = ACTION_SPECS.get(action)
    if spec is None:
        known = sorted(set(ACTION_SPECS) | {"force_freshness"})
        raise HTTPException(
            status_code=422,
            detail=f"unknown action: {action!r}; "
                   f"expected one of {known}")

    row = _find_row(missions_cache, pmo_id, instance)
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
    instance: str | None = None,
) -> dict:
    """Post a human-authored feed comment (no sentinel) on behalf of the operator."""
    if not (body and body.strip()):
        raise HTTPException(status_code=422, detail="body must not be blank")

    row = _find_row(missions_cache, pmo_id, instance)
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


# ── 2b) operator mission creation (ADR-0030 Decision 3) ─────────────────────

PRIORITIES: frozenset[str] = frozenset({"urgent", "high", "medium", "low"})
MAX_CREATE_ATTACHMENTS = 10
_SAFE_ATTACHMENT_NAME = re.compile(r"^[\w][\w .()\[\]-]{0,119}$")


async def create_mission(
    *,
    instance: str,
    title: str,
    description: str = "",
    priority: str = "medium",
    adopt: bool = True,
    attachments: list[dict] | None = None,
    managers: dict[str, Any],
    adoption_mode: str,
) -> dict:
    """Transcribe an operator-originated mission onto the PMO board
    (ADR-0030: DevCake holds the pen; the operator originates the intent).

    Write-through only — no local record; the PMO stays the source of truth
    (INV-1). Resurrects the PR-#14-deleted seam with three deliberate
    changes: `instance` is REQUIRED (multi-PMO implicit routing is a
    footgun), title/description are REDACTED before the port call (the old
    dialog's paste-safety copy claimed this and the old backend never did
    it), and v1 carries attachments (uploaded after create, never aborting
    the loop — partial failure is disclosed, not rolled back: the mission
    EXISTS, a retried POST would duplicate it, and the port deliberately
    has no delete)."""
    if not (title and title.strip()):
        raise HTTPException(status_code=422, detail="title must not be blank")
    if priority not in PRIORITIES:
        # the Linear adapter maps priority via dict lookup — an unknown value
        # would KeyError into a 500 there; refuse it at the boundary instead
        raise HTTPException(
            status_code=422,
            detail=f"unknown priority {priority!r}; "
                   f"expected one of {sorted(PRIORITIES)}")
    if instance not in managers:
        raise HTTPException(
            status_code=404,
            detail=f"no configured PMO instance named {instance!r}")
    mgr = managers[instance]
    team_key = getattr(mgr.instance, "team_key", "")
    if not team_key:
        raise HTTPException(
            status_code=409,
            detail=f"instance {instance!r} has no team_key configured")

    files: list[tuple[str, bytes]] = []
    raw_atts = attachments or []
    if raw_atts and not getattr(
            mgr.pmo.capabilities(), "attachments_supported", True):
        raise HTTPException(
            status_code=422,
            detail="this PMO does not support issue file attachments")
    if len(raw_atts) > MAX_CREATE_ATTACHMENTS:
        raise HTTPException(
            status_code=422,
            detail=f"at most {MAX_CREATE_ATTACHMENTS} attachments per mission")
    cap = mgr.pmo.capabilities().attachment_max_bytes
    for att in raw_atts:
        name = (att.get("name") or "").strip()
        if not _SAFE_ATTACHMENT_NAME.match(name) or name in (".", ".."):
            raise HTTPException(
                status_code=422,
                detail=f"attachment name {name!r} must be a plain filename")
        try:
            data = base64.b64decode(att.get("content_b64") or "",
                                    validate=True)
        except Exception:  # noqa: BLE001 — boundary validation: any decode failure is the client's 422, never a 500
            raise HTTPException(
                status_code=422,
                detail=f"attachment {name!r}: content_b64 is not valid base64")
        if len(data) > cap:
            raise HTTPException(
                status_code=422,
                detail=f"attachment {name!r} exceeds this PMO's "
                       f"{cap // (1024 * 1024)}MB attachment limit")
        files.append((name, data))

    # redaction BEFORE the port call — a pasted secret must never land on
    # the external board (the same guarantee post_steering gives)
    clean_title = redact(title.strip())
    clean_description = redact(description or "")
    # adopt rides the create as the DEVCAKE label — only meaningful under
    # opt_in (opt_out adopts everything; sending the label would be noise).
    # NEVER the DEVCAKE-CREATED family-gate label: that marks decomposition
    # children and would withhold the mission behind a marker-parent scan.
    label_names = ({"DEVCAKE"} if adoption_mode == "opt_in" and adopt
                   else set())
    try:
        key, pmo_id = await mgr.pmo.create_mission(
            team_key, clean_title, clean_description, priority, label_names)
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

    url = ""
    try:
        url = (await mgr.pmo.get(MissionRef(pmo_id, "issue"))).url
    except Exception:  # noqa: BLE001 — the deep link is a courtesy; its failure must not fail a mission that EXISTS
        log.warning("created mission %s: url lookup failed", key)

    uploaded: list[tuple[str, str]] = []
    attachment_failures: list[dict] = []
    for name, data in files:
        try:
            asset_url = await mgr.pmo.upload_attachment(pmo_id, name, data)
            uploaded.append((name, asset_url))
        except Exception as e:  # noqa: BLE001 — partial-failure honesty: one bad upload must not abort the rest; each failure is disclosed by name
            attachment_failures.append(
                {"name": name, "error": redact(str(e))[:160]})

    feed_posted = False
    if uploaded:
        # Linear attachments are INVISIBLE unless referenced from a feed
        # post (docs/05 §1); harmless and consistent on Gitea. Sentinel-free
        # on purpose: operator-authored = HUMAN classification is honest,
        # and a fresh mission has no attempt counter to protect.
        lines = ["Attached at creation:"] + [
            f"- [{name}]({asset_url})" for name, asset_url in uploaded]
        try:
            await mgr.pmo.post_feed(MissionRef(pmo_id, "issue"),
                                    "\n".join(lines))
            feed_posted = True
        except Exception as e:  # noqa: BLE001 — disclosure over failure: the files ARE uploaded; a failed index post is a warning, not an error
            log.warning("created mission %s: attachment index post failed: %s",
                        key, e)

    _try_audit(mgr, pmo_id, "ui_create",
               f"{clean_title[:100]} atts={len(uploaded)}/{len(files)}")
    return {"key": key, "pmo_id": pmo_id, "instance": instance, "url": url,
            "adopted": bool(label_names) or adoption_mode != "opt_in",
            "attachment_failures": attachment_failures,
            "feed_posted": feed_posted}


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

    await run_manager.kill(
        run, "failed", "stopped by operator from the admin UI",
        error_class=failure_taxonomy.DEV_OPERATOR_STOP)
    return {"ok": True, "run_id": run_id, "state": "failed"}


async def stop_all_runs(
    *,
    run_manager: _RunManager,
    run_store: _RunStore,
) -> dict:
    """Operator stop-everything (Clear-Runs hardening, 2026-07-18 root cause):
    kill every dispatched/running run. Finalizing runs are SKIPPED, not 409'd —
    their containers already exited and the watchdog's never-kill-finalizing
    rule applies (see stop_run above); they surface in the response so the
    operator knows what is still completing on its own.

    Each run's state is RE-FETCHED immediately before the kill (audit D5 #6):
    the ingress consumer runs concurrently, so a run in the initial snapshot
    may have flipped to `finalizing` while an earlier kill was awaiting — the
    stale snapshot object would still read `running` and we'd kill a run whose
    container already exited. Each kill is isolated (audit D5 #9): one failure
    (e.g. an OSError from a full/read-only /data during store.save) records an
    error and moves on rather than aborting the batch and losing accounting."""
    stopped: list[str] = []
    skipped: list[str] = []
    errors: list[dict[str, str]] = []
    for snap in run_store.active():
        run = run_store.get(snap.run_id)                # re-read current state
        if run is None:
            # the record was deleted under us (a concurrent clear-runs wipe);
            # killing would store.save it back, resurrecting a phantom failed
            # run after "start fresh" (re-audit #1). Nothing to stop here.
            continue
        if getattr(run, "state", None) not in ("dispatched", "running"):
            skipped.append(run.run_id)                  # finalizing/terminal
            continue
        try:
            await run_manager.kill(
                run, "failed", "stopped by operator (stop all)",
                error_class=failure_taxonomy.DEV_OPERATOR_STOP)
            stopped.append(run.run_id)
        except Exception as e:  # noqa: BLE001 — one kill failure must not abort the batch or lose accounting; recorded per-run
            log.exception("stop_all_runs: kill failed for %s", run.run_id)
            errors.append({"run_id": run.run_id, "error": str(e)[:200]})
    return {"ok": not errors, "stopped": stopped,
            "skipped_finalizing": skipped, "errors": errors}


# ── 5) force-poll endpoint ──────────────────────────────────────────────────
# The most on-philosophy write we ship: forcing a poll cycle is asking the PMO
# "what's true right now?" — INV-1 amplified, not sidestepped (docs/00 §4).
# Precedent: `POST /api/v1/steward/run` (docs/11 §1, docs/04 §1.6),
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
        # `started` is wall-clock for the response (operator-facing timestamp);
        # `t0` is monotonic for duration so an NTP step / container resume
        # mid-cycle can't produce a negative or garbage `duration_ms`.
        started = datetime.now(timezone.utc)
        t0 = time.monotonic()
        try:
            await run_cycle(cycle_num)
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"poll cycle raised: {type(e).__name__}: "
                       f"{str(e)[:150]}") from e
        elapsed_ms = int((time.monotonic() - t0) * 1000)
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
    "create_mission",
    "force_freshness",
    "force_poll_now",
    "label_action",
    "post_steering",
    "stop_all_runs",
    "stop_run",
]
