"""Mission completion on merge — THE chokepoint (ADR-0034).

"Mission completes on merge" used to be implemented at four sites
(review._done, review._done_merged, the merge sweep's merged branch, the
deferred-retry tail) and the AUD-010 conflict-trust rule at two — and
AUD-010's own history records the copies splitting doctrine once (the sweep
routed Gitea conflicts to EXECUTE while finalize handed them off).
Everything that differs per path lives in the _CAUSES table below; nobody
composes completion copy, labels, or ordering anywhere else. Feed strings
are today's exact bytes — copy changes are a decision, never a refactor
side effect (deliver's feed-probe idempotency greps the deliverable name
out of these threads, and operators grep them too).

Checkpoint semantics are preserved, NOT unified: the REVIEW-finalize path
checkpoints the core as the existing byte-stable key ``review:done`` so
in-flight finalizing runs at upgrade time resume through it; the sweep
paths execute directly — their idempotency is the label swap removing the
mission from the sweep's selection predicate, plus the zip's own durable
feed-probe guard. Unifying the two regimes would change PMO read costs and
failure economics for no defect closed (ADR-0034 out-of-scope list).

Leaf module by design: imports only model/run/freshness/feed/markers/ports
so both review and sweeps import downward (sweeps already imports review —
a chokepoint living in either would cycle).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum

from opentelemetry import trace

from ...ports.forge import mission_branch
from ..model import (LABEL_EXECUTE, LABEL_MERGE, LABEL_REVIEW, Mission,
                     MissionRef)
from ..run import Run
from . import freshness
from .feed import _unquoted
from .markers import CONFLICT_MARKER, MAX_CONFLICT_RESOLVES

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


class MergedCause(StrEnum):
    REVIEW_AUTO_MERGE = "review_auto_merge"        # finalize: our merge landed, or found merged on the re-probe
    SWEEP_EXTERNAL_MERGE = "sweep_external_merge"  # merge sweep: PR turned up merged (human / CI)
    DEFERRED_RETRY_MERGE = "deferred_retry_merge"  # windowed retry: our merge landed


@dataclass(frozen=True)
class _Cause:
    remove_label: str
    feed: str                 # .format(pr_url=…) — byte-exact legacy copy
    audit_action: str
    disclose_before: bool     # ADR-0031 D1 disclose-only pre-step


_CAUSES: dict[MergedCause, _Cause] = {
    MergedCause.REVIEW_AUTO_MERGE: _Cause(
        remove_label=LABEL_REVIEW,
        feed="✅ REVIEW approved; PR merged ({pr_url}). Mission done.",
        audit_action="review_approve_merged",
        disclose_before=False),
    MergedCause.SWEEP_EXTERNAL_MERGE: _Cause(
        remove_label=LABEL_MERGE,
        feed="✅ PR {pr_url} merged — mission done (merge sweep).",
        audit_action="merge_sweep_done",
        disclose_before=False),
    MergedCause.DEFERRED_RETRY_MERGE: _Cause(
        remove_label=LABEL_MERGE,
        feed="✅ Merged after deferred retry ({pr_url}). Mission done.",
        # ADR-0031 D1 (deferred-merge row): DISCLOSE-ONLY — the finalize-time
        # gate passed minutes-to-hours ago; material landing since still
        # closes (the merge was operator-sanctioned) but never silently
        audit_action="merge_retry_succeeded",
        disclose_before=True),
}


async def complete_merged(mgr, cause: MergedCause, *, ref: MissionRef,
                          mission_key: str, pr, pr_url: str,
                          run: Run | None = None,
                          mission: Mission | None = None) -> None:
    """Complete a mission whose PR is merged. Fixed order for every cause:
    disclose pre-step (table-driven) → core (swap → status → feed → audit;
    checkpointed as ``review:done`` when a Run is in flight, direct on sweep
    paths) → deliverable zip (best-effort — packaging must NEVER un-Done the
    mission, deliver.py doctrine). ``pr`` is forwarded verbatim to the zip
    path (a PR on the run path, a PR/PRState on the sweep paths)."""
    assert run is not None or mission is not None, (
        "complete_merged needs a Run (finalize path) or a Mission (sweep path)")
    spec = _CAUSES[cause]
    with tracer.start_as_current_span("mission.complete") as span:
        span.set_attribute("devcake.mission.key", mission_key)
        span.set_attribute("devcake.cause", str(cause))
        if spec.disclose_before and mission is not None:
            await freshness.disclose_unread_at_close(mgr, mission)

        async def _core():
            await mgr.pmo.swap_labels(ref, remove={spec.remove_label},
                                      add=set())
            await mgr.pmo.set_status(ref, "done")
            await mgr._feed(ref.pmo_id, ref.kind,
                            spec.feed.format(pr_url=pr_url))
            mgr._audit(ref.pmo_id, spec.audit_action, pr_url)

        if run is not None:
            await mgr._checkpoint(run, "review:done", _core)
        else:
            await _core()
        try:
            if run is not None:
                await mgr.deliver_internal_zip(run, pr)
            else:
                await mgr.deliver_internal_zip_for_mission(mission, pr)
        except Exception:  # noqa: BLE001 — packaging never un-Dones: zip failure degrades loudly in the log, the mission stays done
            log.exception("deliverable zip failed for %s — mission stays done",
                          mission_key)


# ── AUD-010 conflict doctrine (trust + routing + budget), one copy ───────────

def trusted_conflict(forge, verdict) -> bool:
    """A ``False`` mergeable verdict is a real conflict ONLY on a genuine
    tri-state forge (GitHub/GitLab); Gitea's boolean ``False`` can be "not
    computed yet" (AUD-010 / M11), so a failed merge there hands off to a
    human rather than routing EXECUTE rework."""
    caps = getattr(forge, "capabilities", None)
    return verdict is False and (caps is None or caps.mergeable_tristate)


async def conflict_attempts(mgr, pmo_id: str) -> int:
    """Prior auto-resolve attempts, derived from feed markers — the PMO is
    the source of truth (docs/03), so the count survives restarts and a
    human deleting directive comments deliberately resets it."""
    act = await mgr.pmo.get_activity(MissionRef(pmo_id, "issue"))
    hits = [int(mt.group(1)) for e in act.entries
            for mt in CONFLICT_MARKER.finditer(_unquoted(e.body))]
    return max(hits) if hits else 0


async def route_conflict_to_execute(mgr, pmo_id: str, key: str, pr_url: str,
                                    from_label: str, inst) -> bool:
    """docs/03 §4.1 — on an auto-resolvable merge failure (conflict or
    stale branch), route the mission back to EXECUTE with a resolve
    directive, max MAX_CONFLICT_RESOLVES attempts per mission. Returns
    True when routed; any failure or decline returns False so the caller's
    human fallback (DEVCAKE-MERGE) is never blocked. ``inst`` is the
    mission's RepoInstance (per-repo doctrine, ADR-0020)."""
    if not inst.auto_resolve_merge_conflicts:
        return False
    try:
        n = await conflict_attempts(mgr, pmo_id)
        if n >= MAX_CONFLICT_RESOLVES:
            mgr._audit(pmo_id, "conflict_resolve_exhausted", pr_url)
            return False
        # directive FIRST, then swap (mirrors the reject path): if the
        # post fails the mission stays put and the marker count never
        # undercounts. The EXECUTE playbook tells the Dev a 🧩 resolve
        # directive overrides its normal implement-the-mission job (🧩 is
        # reserved for this directive — 🔀 already means "PR opened").
        await mgr._feed(
            pmo_id, "issue",
            f"🧩 Auto-merge hit a merge conflict on {pr_url} (auto-resolve "
            f"attempt {n + 1}/{MAX_CONFLICT_RESOLVES}) — back to EXECUTE. "
            f"Next Dev: sync `{mission_branch(mgr.instance_name, key)}` with the default branch, "
            f"resolve the conflicts, and push; the PR then returns to "
            f"REVIEW. `devcake:conflict-resolve:{n + 1}`")
        await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                   remove={from_label}, add={LABEL_EXECUTE})
        mgr._audit(pmo_id, "conflict_resolve_dispatched",
                    f"attempt {n + 1} ({pr_url})")
        return True
    except Exception:
        log.exception("conflict auto-resolve routing failed for %s", key)
        return False
