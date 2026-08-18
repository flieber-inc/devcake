"""Poll-cycle merge and tracking sweeps (docs/04 §1, docs/03 §4.1)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ...ports.forge import legacy_branch, mission_branch
from ..model import (LABEL_MERGE, LABEL_NEEDS_HUMAN, LABEL_TRACKING, Mission,
                     STAGE_LABELS)
from ..run import aware, utcnow
from . import completion, discovery, dispatch, feed, freshness
from .markers import (MERGE_HANDOFF_MARKER, MERGE_RETRY_MARKER,
                      MERGE_SETTLE_MARKER)

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def sweeps(mgr, missions: list[Mission]) -> None:
    # prune the merge-hand-off advisories to missions still awaiting a
    # merge: covers merged/canceled AND the human label-swap intervention
    # (which also reopens the window state for a possible next episode).
    # Status gate is non-terminal (not only in_progress): forge-issue PMOs
    # (GitHub/Gitea Issues) map open → backlog (docs/05 §9.2); requiring
    # in_progress stranded DEVCAKE-MERGE after review:merge_deferred forever.
    merge_ids = {m.pmo_id for m in missions
                 if m.pmo_kind == "issue" and LABEL_MERGE in m.labels
                 and m.status not in ("done", "canceled")}
    mgr.merge_handoffs = {k: v for k, v in mgr.merge_handoffs.items()
                           if k in merge_ids}
    mgr._merge_window_closed &= merge_ids
    # needs-human advisories: rebuilt wholesale from the label each cycle
    # (restart-safe; clears the moment the human removes the label)
    mgr.needs_human = {
        m.pmo_id: (f"{m.key}: needs human"
                   + (f" on {next(iter(m.labels & STAGE_LABELS))}"
                      if m.labels & STAGE_LABELS else "")
                   + (f" — {m.url}" if m.url else ""))
        for m in missions if LABEL_NEEDS_HUMAN in m.labels
    }
    # AUD-005: a repo's OFF→ON re-arm must survive a cycle in which its parked
    # mission's PR could not be reached (forge lag, branch miss, mid-loop
    # error) — otherwise the flag is cleared and the window is lost forever
    # until another toggle. merge_sweep marks a mission "satisfied" (pmo_id in
    # this set) only once it actually reached the deferred-retry driver for it;
    # a repo stays armed while any of its parked missions is still unsatisfied.
    mgr._rearm_satisfied = set()
    # sequential by design; a per-mission await may include an adapter's
    # short transient-retry sleeps (≤ ~6 s, docs/06 §5) — expected, not a
    # hang, and non-blocking for the event loop
    for m in missions:
        try:
            # spans only when a sweep actually acts (inside the helpers)
            if m.pmo_kind == "issue" and LABEL_MERGE in m.labels \
                    and m.status not in ("done", "canceled"):
                await merge_sweep(mgr, m)
            if m.pmo_kind == "project" and LABEL_TRACKING in m.labels \
                    and m.status not in ("done", "canceled"):
                await tracking_sweep(mgr, m)
            # ADR-0033: label-gated INSIDE the helper — sweeps.py never
            # reads the discovery label (guard allowlist stays tight)
            await discovery.discovery_sweep(mgr, m)
        except Exception:
            log.exception("sweep failed for %s", m.key)
    # Keep a repo armed iff a parked DEVCAKE-MERGE mission on it was NOT
    # satisfied this cycle (its window never got opened). Repos whose missions
    # were all driven — and repos with nothing parked to re-arm — drop out.
    unsatisfied = {
        m.repo for m in missions
        if m.pmo_kind == "issue" and LABEL_MERGE in m.labels
        and m.status not in ("done", "canceled")
        and m.repo in mgr.rearm_merge_repos
        and m.pmo_id not in mgr._rearm_satisfied}
    mgr.rearm_merge_repos = mgr.rearm_merge_repos & unsatisfied


async def merge_sweep(mgr, m: Mission) -> None:
    forge = mgr.forges.get(m.repo) if m.repo else None
    # forges + instances are co-populated; missing instance still VISIBLE-gates
    # so a desync never AttributeErrors mid-sweep (forge_runtime.py contract)
    inst = (mgr.forges.instance(m.repo)
            if forge is not None and m.repo else None)
    if forge is None or inst is None:
        # the mission's repo vanished (or resolution gates it): skip with a
        # VISIBLE reason — a parked DEVCAKE-MERGE mission must never wedge
        # silently (resolution-failure contract, domain/forge_runtime.py)
        mgr.blocked_reasons[m.pmo_id] = (
            m.repo_reason or f"repo '{m.repo}' no longer configured — "
            f"merge sweep skipped")
        return
    pr = await forge.get_pr_by_branch(mission_branch(m.instance, m.key))
    if not pr:
        # pre-v3 branches carry no instance prefix — re-probe the legacy
        # convention so parked missions from before the upgrade still complete
        pr = await forge.get_pr_by_branch(legacy_branch(m.key))
    if not pr:
        # AUD-006: a DEVCAKE-MERGE mission with no discoverable PR is not
        # normal (forge list lag / branch-naming miss). Surface it instead of
        # returning silently — otherwise a mission whose PR never appears sits
        # parked with no visible reason. Self-clears next cycle once the PR is
        # found (gate_map rebuilds blocked_reasons at the top of each poll).
        # When auto_merge is ON, finalize has already posted a retry marker, so
        # the merge auto-drives the moment the PR surfaces.
        mgr.blocked_reasons[m.pmo_id] = (
            f"{m.key}: no open PR found for branch "
            f"{mission_branch(m.instance, m.key)} — merge sweep deferred "
            f"(forge lag or branch mismatch)")
        return
    state = await forge.pr_state(pr.number)
    if state.merged or state.state == "closed":
        mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: mission completing
        with tracer.start_as_current_span("sweep.merge") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.outcome",
                               "merged" if state.merged else "closed")
            if state.merged:
                await completion.complete_merged(
                    mgr, completion.MergedCause.SWEEP_EXTERNAL_MERGE,
                    ref=m.ref, mission_key=m.key,
                    pr=state, pr_url=state.url, mission=m)
            else:
                # a CLOSED-unmerged PR is a cancellation, not a completion —
                # deliberately outside the chokepoint (ADR-0034 scope note).
                # cancel FIRST (same commit-point rule as complete_merged):
                # stripping MERGE before cancel lands hides the mission from
                # the next sweep if cancel raises.
                await mgr.pmo.cancel_mission(m.ref)
                await mgr.pmo.swap_labels(m.ref, remove={LABEL_MERGE},
                                          add=set())
                await mgr._feed(
                    m.pmo_id, "issue",
                    f"🚫 PR {state.url} was closed without merging — mission "
                    f"canceled (merge sweep).")
                mgr._audit(m.pmo_id, "merge_sweep_canceled", state.url)
    else:
        # advisory banner (docs/11): an open PR on DEVCAKE-MERGE awaits a
        # human — unless the deferred-retry window is actively running
        # (_deferred_merge_retry pops the entry while it drives the window)
        mgr.merge_handoffs[m.pmo_id] = (
            f"{m.key}: awaiting human merge — {state.url}")
        from ...config import auto_merge_permitted
        if auto_merge_permitted(mgr.config, inst, m.repo, mgr.dev_types):
            await _deferred_merge_retry(mgr, m, pr, state.url, inst)


async def _deferred_merge_retry(mgr, m: Mission, pr,
                                pr_url: str, inst) -> None:
    """docs/03 §4.1 deferred-merge window: while `devcake:merge-retry` is
    the latest merge-state marker in the feed, keep watching the PR each
    sweep cycle — merge when it becomes ready, route to EXECUTE if a
    conflict emerges, and hand off to a human once
    merge_retry_window_minutes elapse. Elapsed time is measured from the
    marker entry's PMO timestamp (no local clocks), so the window is
    live-tunable and restart-safe. The label stays DEVCAKE-MERGE
    throughout: a manual human merge mid-window is caught by the
    external-merge branch above on the next cycle. ``inst`` is the
    mission's RepoInstance (per-repo doctrine, ADR-0020).

    Post-approve settle (``devcake:merge-settle``): when that marker is
    latest, wait ``merge_settle_minutes`` for sibling discoveries, then
    recheck freshness (may re-open REVIEW) before the first merge attempt.
    """
    rearm = m.repo in mgr.rearm_merge_repos
    if m.pmo_id in mgr._merge_window_closed:
        if not rearm:
            return  # window known closed — skip the per-cycle feed read
        mgr._merge_window_closed.discard(m.pmo_id)   # re-read the feed once
    forge = mgr.forges.get(m.repo) if m.repo else None
    if forge is None:
        # resolution-failure contract (domain/forge_runtime.py): visible
        # reason, no crash — the parked mission waits for the repo to return
        mgr.blocked_reasons[m.pmo_id] = (
            m.repo_reason or f"repo '{m.repo}' no longer configured — "
            f"deferred merge retry skipped")
        return
    act = await mgr.pmo.get_activity(m.ref)
    retry_ts = handoff_ts = settle_ts = None
    for e in act.entries:
        body = feed.unquoted(e.body)
        ts = aware(e.ts)  # a naive PMO timestamp must not TypeError
        if MERGE_RETRY_MARKER in body:
            retry_ts = max(retry_ts, ts) if retry_ts else ts
        if MERGE_HANDOFF_MARKER in body:
            handoff_ts = max(handoff_ts, ts) if handoff_ts else ts
        if MERGE_SETTLE_MARKER in body:
            settle_ts = max(settle_ts, ts) if settle_ts else ts

    # Settle is active when its marker is the newest merge-state marker.
    settle_active = bool(
        settle_ts
        and (not retry_ts or settle_ts >= retry_ts)
        and (not handoff_ts or settle_ts >= handoff_ts))
    if settle_active:
        settle_min = int(getattr(inst, "merge_settle_minutes", 0) or 0)
        # App is intentionally holding — not a human-merge banner
        mgr.merge_handoffs.pop(m.pmo_id, None)
        mgr._rearm_satisfied.add(m.pmo_id)
        if settle_min > 0 and (utcnow() - settle_ts).total_seconds() / 60 < settle_min:
            return  # still coalescing sibling discoveries
        # Window elapsed (or settle_min lowered to 0 live): recheck feed
        outcome = await freshness.recheck_and_maybe_rereview(
            mgr, m, reason="merge_settle")
        if outcome == "tripped":
            return  # REVIEW re-opened; next poll dispatches
        # pass / exhausted / no_review_anchor → attempt merge below
        # Open a merge-retry window so forge-not-ready can keep driving
        # without re-entering settle on every cycle (retry marker is newer).
        window = inst.merge_retry_window_minutes
        if window > 0:
            await mgr._feed(
                m.pmo_id, "issue",
                f"⏳ Settle complete — DevCake is auto-merging {pr_url}, "
                f"retrying for up to {window} minutes if the forge is not "
                f"ready yet. {MERGE_RETRY_MARKER}")
            mgr._audit(m.pmo_id, "merge_settle_complete", pr_url)
            return  # next cycle drives via merge-retry
        # window 0: fall through into a one-shot merge attempt (no retry marker)

    window = inst.merge_retry_window_minutes
    if not settle_active and (
            not retry_ts or (handoff_ts and handoff_ts >= retry_ts)):
        if rearm and window > 0:
            # auto_merge flipped OFF→ON for this mission's repo with it
            # parked (founder request 2026-07-15, per-repo ADR-0020): open
            # a fresh window. The feed entry IS the window state (marker
            # timestamp = start), so this is restart-safe and visible; the
            # next cycle reads it and drives the merge.
            await mgr._feed(
                m.pmo_id, "issue",
                f"⏳ Auto-merge is now ON — DevCake resumes driving the merge "
                f"of {pr_url}, retrying for up to {window} minutes before "
                f"handing back to you. {MERGE_RETRY_MARKER}")
            mgr._audit(m.pmo_id, "merge_retry_rearmed", pr_url)
            mgr.merge_handoffs.pop(m.pmo_id, None)
            mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: window (re)opened
            return
        mgr._merge_window_closed.add(m.pmo_id)
        mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: nothing more to (re)arm
        return  # no active retry window (auto_merge-OFF parks land here)
    if not settle_active and (utcnow() - retry_ts).total_seconds() / 60 > window:
        with tracer.start_as_current_span("sweep.merge_retry") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.outcome", "window_exhausted")
            span.set_status(Status(StatusCode.ERROR,
                                   f"unmergeable after {window} min"))
            await mgr._feed(
                m.pmo_id, "issue",
                f"⚠️ Still unmergeable after {window} min — awaiting human "
                f"merge of {pr_url} (`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
            mgr._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
            mgr._merge_window_closed.add(m.pmo_id)
        mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: window ran its course
        return
    # window ACTIVE: DevCake is still driving the merge — no human action
    # needed, so the sweep's banner entry comes back off
    mgr.merge_handoffs.pop(m.pmo_id, None)
    mgr._rearm_satisfied.add(m.pmo_id)       # AUD-005: window already driving
    verdict = await forge.mergeable(pr.number)
    if verdict is None:
        return  # still computing / CI running — next cycle re-reads
    # a False verdict can be a non-blocking "behind" (strict up-to-date
    # rules are what make it fail) — one plain merge attempt is far
    # cheaper than an EXECUTE rework, so always try the merge first and
    # only route to rework when it actually fails on a real conflict
    with tracer.start_as_current_span("sweep.merge_retry") as span:
        span.set_attribute("devcake.mission.key", m.key)
        span.set_attribute("devcake.merge.verdict", str(verdict))
        try:
            await forge.merge(pr.number)
        except Exception:  # noqa: BLE001 — a failed merge IS the signal here: a real conflict routes/hands off, anything else is logged transient and next cycle retries
            # AUD-010: trust `verdict is False` as a real conflict only when
            # the forge exposes a genuine mergeable tri-state (GitHub/GitLab).
            # On a boolean-only forge (Gitea) a False can be "not computed
            # yet", so a failed merge hands off to a human rather than routing
            # rework — IDENTICAL doctrine to finalize (review.py capability
            # branch). Without this, the sweep routed Gitea conflicts to
            # EXECUTE while finalize handed them off — a doctrine split.
            if completion.trusted_conflict(forge, verdict):
                span.set_attribute("devcake.outcome", "conflict")
                if not await completion.route_conflict_to_execute(
                        mgr, m.pmo_id, m.key, pr_url, LABEL_MERGE, inst):
                    span.set_attribute("devcake.outcome", "conflict_handoff")
                    await mgr._feed(
                        m.pmo_id, "issue",
                        f"⚠️ Merge conflict on {pr_url} and auto-resolve is "
                        f"unavailable (toggle off or attempts exhausted) — "
                        f"awaiting human merge (`DEVCAKE-MERGE`). "
                        f"{MERGE_HANDOFF_MARKER}")
                    mgr._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
                    mgr._merge_window_closed.add(m.pmo_id)
                    mgr.merge_handoffs[m.pmo_id] = (
                        f"{m.key}: awaiting human merge — {pr_url}")
            else:
                # state may have moved under us; next cycle re-reads
                span.set_attribute("devcake.outcome", "merge_failed_transient")
                log.debug("deferred merge retry failed for %s", m.key,
                          exc_info=True)
            return
        span.set_attribute("devcake.outcome", "merged")
        # disclose-only freshness pre-step rides the cause table
        # (DEFERRED_RETRY_MERGE → disclose_before=True)
        await completion.complete_merged(
            mgr, completion.MergedCause.DEFERRED_RETRY_MERGE,
            ref=m.ref, mission_key=m.key, pr=pr, pr_url=pr_url, mission=m)


async def tracking_sweep(mgr, m: Mission) -> None:
    try:
        children = await mgr.pmo.children_of(m.ref)
    except Exception as e:
        # Port F1: projects_supported=False adapters raise on project-kind
        # refs (never return []). Transient children reads can also fail.
        # Never false-complete; surface like merge missing-forge/PR so the
        # wedge is admin-visible — outer sweeps() only log.exception is not
        # enough for /health blocked_reasons (CAKE-46).
        mgr.blocked_reasons[m.pmo_id] = (
            f"{m.key}: tracking children unreadable — {type(e).__name__}: "
            f"{str(e)[:120]}")
        raise
    if children and all(c.status in ("done", "canceled") for c in children):
        with tracer.start_as_current_span("sweep.tracking") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.children", len(children))
            # status FIRST (same commit-point as complete_merged): a failed
            # status leaves TRACKING so the next cycle still selects; a failed
            # swap after Done is leftover hygiene on a terminal project.
            await mgr.pmo.set_status(m.ref, "done")
            await mgr.pmo.swap_labels(m.ref, remove={LABEL_TRACKING}, add=set())
            mgr._audit(m.pmo_id, "tracking_sweep_completed",
                        f"{len(children)} children")
        log.info("project %s auto-completed (%d children done)", m.key, len(children))

