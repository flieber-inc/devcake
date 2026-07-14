"""Poll-cycle merge and tracking sweeps (docs/04 §1, docs/03 §4.1)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ...ports.forge import mission_branch
from ..model import (LABEL_MERGE, LABEL_NEEDS_HUMAN, LABEL_TRACKING, Mission,
                     STAGE_LABELS)
from ..run import utcnow
from .markers import MERGE_HANDOFF_MARKER, MERGE_RETRY_MARKER

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def sweeps(self, missions: list[Mission]) -> None:
    # prune the merge-hand-off advisories to missions still awaiting a
    # merge: covers merged/canceled AND the human label-swap intervention
    # (which also reopens the window state for a possible next episode)
    merge_ids = {m.pmo_id for m in missions
                 if m.pmo_kind == "issue" and LABEL_MERGE in m.labels
                 and m.status == "in_progress"}
    self.merge_handoffs = {k: v for k, v in self.merge_handoffs.items()
                           if k in merge_ids}
    self._merge_window_closed &= merge_ids
    # needs-human advisories: rebuilt wholesale from the label each cycle
    # (restart-safe; clears the moment the human removes the label)
    self.needs_human = {
        m.pmo_id: (f"{m.key}: needs human"
                   + (f" on {next(iter(m.labels & STAGE_LABELS))}"
                      if m.labels & STAGE_LABELS else "")
                   + (f" — {m.url}" if m.url else ""))
        for m in missions if LABEL_NEEDS_HUMAN in m.labels
    }
    # sequential by design; a per-mission await may include an adapter's
    # short transient-retry sleeps (≤ ~6 s, docs/06 §5) — expected, not a
    # hang, and non-blocking for the event loop
    for m in missions:
        try:
            # spans only when a sweep actually acts (inside the helpers)
            if m.pmo_kind == "issue" and LABEL_MERGE in m.labels \
                    and m.status == "in_progress":
                await self._merge_sweep(m)
            if m.pmo_kind == "project" and LABEL_TRACKING in m.labels \
                    and m.status not in ("done", "canceled"):
                await self._tracking_sweep(m)
        except Exception:
            log.exception("sweep failed for %s", m.key)


async def _merge_sweep(self, m: Mission) -> None:
    pr = await self.forge.get_pr_by_branch(mission_branch(m.instance, m.key))
    if not pr:
        return
    state = await self.forge.pr_state(pr.number)
    if state.merged or state.state == "closed":
        with tracer.start_as_current_span("sweep.merge") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.outcome",
                               "merged" if state.merged else "closed")
            await self.pmo.swap_labels(m.ref, remove={LABEL_MERGE}, add=set())
            if state.merged:
                await self.pmo.set_status(m.ref, "done")
                await self._feed(
                    m.pmo_id, "issue",
                    f"✅ PR {state.url} merged — mission done (merge sweep).")
                self._audit(m.pmo_id, "merge_sweep_done", state.url)
            else:
                await self.pmo.set_status(m.ref, "canceled")
                await self._feed(
                    m.pmo_id, "issue",
                    f"🚫 PR {state.url} was closed without merging — mission "
                    f"canceled (merge sweep).")
                self._audit(m.pmo_id, "merge_sweep_canceled", state.url)
    else:
        # advisory banner (docs/11): an open PR on DEVCAKE-MERGE awaits a
        # human — unless the deferred-retry window is actively running
        # (_deferred_merge_retry pops the entry while it drives the window)
        self.merge_handoffs[m.pmo_id] = (
            f"{m.key}: awaiting human merge — {state.url}")
        if self.config.auto_merge:
            await self._deferred_merge_retry(m, pr, state.url)


async def _deferred_merge_retry(self, m: Mission, pr,
                                pr_url: str) -> None:
    """docs/03 §4.1 deferred-merge window: while `devcake:merge-retry` is
    the latest merge-state marker in the feed, keep watching the PR each
    sweep cycle — merge when it becomes ready, route to EXECUTE if a
    conflict emerges, and hand off to a human once
    merge_retry_window_minutes elapse. Elapsed time is measured from the
    marker entry's PMO timestamp (no local clocks), so the window is
    live-tunable and restart-safe. The label stays DEVCAKE-MERGE
    throughout: a manual human merge mid-window is caught by the
    external-merge branch above on the next cycle."""
    if m.pmo_id in self._merge_window_closed:
        return  # window known closed — skip the per-cycle feed read
    act = await self.pmo.get_activity(m.ref)
    retry_ts = handoff_ts = None
    for e in act.entries:
        body = self._unquoted(e.body)
        ts = self._aware(e.ts)  # a naive PMO timestamp must not TypeError
        if MERGE_RETRY_MARKER in body:
            retry_ts = max(retry_ts, ts) if retry_ts else ts
        if MERGE_HANDOFF_MARKER in body:
            handoff_ts = max(handoff_ts, ts) if handoff_ts else ts
    if not retry_ts or (handoff_ts and handoff_ts >= retry_ts):
        self._merge_window_closed.add(m.pmo_id)
        return  # no active retry window (auto_merge-OFF parks land here)
    window = self.config.merge_retry_window_minutes
    if (utcnow() - retry_ts).total_seconds() / 60 > window:
        with tracer.start_as_current_span("sweep.merge_retry") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.outcome", "window_exhausted")
            span.set_status(Status(StatusCode.ERROR,
                                   f"unmergeable after {window} min"))
            await self._feed(
                m.pmo_id, "issue",
                f"⚠️ Still unmergeable after {window} min — awaiting human "
                f"merge of {pr_url} (`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
            self._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
            self._merge_window_closed.add(m.pmo_id)
        return
    # window ACTIVE: DevCake is still driving the merge — no human action
    # needed, so the sweep's banner entry comes back off
    self.merge_handoffs.pop(m.pmo_id, None)
    verdict = await self.forge.mergeable(pr.number)
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
            await self.forge.merge(pr.number)
        except Exception:
            if verdict is False:
                span.set_attribute("devcake.outcome", "conflict")
                if not await self._maybe_route_conflict_to_execute(
                        m.pmo_id, m.key, pr_url, LABEL_MERGE):
                    span.set_attribute("devcake.outcome", "conflict_handoff")
                    await self._feed(
                        m.pmo_id, "issue",
                        f"⚠️ Merge conflict on {pr_url} and auto-resolve is "
                        f"unavailable (toggle off or attempts exhausted) — "
                        f"awaiting human merge (`DEVCAKE-MERGE`). "
                        f"{MERGE_HANDOFF_MARKER}")
                    self._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
                    self._merge_window_closed.add(m.pmo_id)
                    self.merge_handoffs[m.pmo_id] = (
                        f"{m.key}: awaiting human merge — {pr_url}")
            else:
                # state may have moved under us; next cycle re-reads
                span.set_attribute("devcake.outcome", "merge_failed_transient")
                log.debug("deferred merge retry failed for %s", m.key,
                          exc_info=True)
            return
        span.set_attribute("devcake.outcome", "merged")
        await self.pmo.swap_labels(m.ref, remove={LABEL_MERGE}, add=set())
        await self.pmo.set_status(m.ref, "done")
        await self._feed(
            m.pmo_id, "issue",
            f"✅ Merged after deferred retry ({pr_url}). Mission done.")
        self._audit(m.pmo_id, "merge_retry_succeeded", pr_url)


async def _tracking_sweep(self, m: Mission) -> None:
    children = await self.pmo.children_of(m.ref)
    if children and all(c.status in ("done", "canceled") for c in children):
        with tracer.start_as_current_span("sweep.tracking") as span:
            span.set_attribute("devcake.mission.key", m.key)
            span.set_attribute("devcake.children", len(children))
            await self.pmo.set_status(m.ref, "done")
            await self.pmo.swap_labels(m.ref, remove={LABEL_TRACKING}, add=set())
            self._audit(m.pmo_id, "tracking_sweep_completed",
                        f"{len(children)} children")
        log.info("project %s auto-completed (%d children done)", m.key, len(children))

