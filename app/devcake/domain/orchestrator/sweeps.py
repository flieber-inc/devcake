"""Poll-cycle merge and tracking sweeps (docs/04 §1, docs/03 §4.1)."""

from __future__ import annotations

import logging
from datetime import timedelta

from opentelemetry import trace
from opentelemetry.trace import Status, StatusCode

from ...ports.forge import ForgeError, legacy_branch, mission_branch
from ...ports.pmo import PMOTransient
from ..model import (LABEL_MERGE, LABEL_NEEDS_HUMAN, LABEL_TRACKING, Mission,
                     STAGE_LABELS)
from ..run import aware, utcnow
from . import (activity_payload, board, completion, discovery, dispatch,
               feed, feed_memo,
               freshness, status_comment)
from .markers import (MERGE_HANDOFF_MARKER, MERGE_RETRY_MARKER,
                      MERGE_SETTLE_MARKER)

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")

# feed-changes witness (docs/04 §1): the read asks for changes after the
# watermark minus this overlap (replica lag), and walks at most this many
# pages — past the cap the witness proves nothing and the memo is dropped
FEED_DELTA_OVERLAP = timedelta(seconds=60)
FEED_DELTA_MAX_PAGES = 10
# admitted attempts past the merge window that may fail before the hand-off
# (docs/03 §4.1): two, so a single transient forge error is not terminal
CLOSING_ATTEMPTS = 2


def _feed_delta_supported(mgr) -> bool:
    try:
        return bool(mgr.pmo.capabilities().feed_delta)
    except Exception:  # noqa: BLE001 — no self-description means no witness, never a failed sweep
        return False


async def _reconcile_feed_memo(mgr, missions: list[Mission]) -> None:
    """One team-wide feed-changes read stands in for the per-mission
    re-reads (docs/04 §1, ADR-0033 addendum): the memoized scans of
    missions whose `updated_at` moved for a label, status, or relation
    edit are kept when the witness shows their feed untouched, and dropped
    when it changed. A refused read (the request budget) changes nothing —
    the per-mission arms fall back exactly as before; a permanent failure
    latches the witness off until the next settings Save or restart."""
    memo = getattr(mgr, "feed_memo", None)
    if memo is None or not missions or not _feed_delta_supported(mgr) \
            or memo.delta_error:
        return
    newest = max((aware(m.updated_at) for m in missions
                  if getattr(m, "updated_at", None)), default=None)
    if len(memo) == 0:
        # nothing memoized: nothing to witness, and nothing that could be
        # missed — the watermark follows the board so the first witness
        # after an idle stretch does not have to cover the whole gap
        memo.anchor(newest, follow=True)
        return
    memo.anchor(newest)
    if memo.watermark is None:
        return
    board.bump(mgr, "feed_delta_reads")
    try:
        delta = await mgr.pmo.feed_changes_since(
            mgr.instance.team_key, memo.watermark - FEED_DELTA_OVERLAP,
            limit_pages=FEED_DELTA_MAX_PAGES)
    except PMOTransient as e:
        log.warning("feed changes deferred for %s: %s", mgr.instance_name, e)
        return
    except Exception as e:  # noqa: BLE001 — a schema mismatch must not traceback every cycle; latched, visible in the log once
        log.exception("feed changes read failed for %s — witness off until "
                      "the next settings Save or restart", mgr.instance_name)
        memo.delta_error = f"{type(e).__name__}: {e}"[:200]
        return
    memo.advance(delta)
    board.bump(mgr, "feed_scan_memo_kept", memo.reconcile(missions, delta))


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
    mgr._merge_pr_numbers = {k: v for k, v in mgr._merge_pr_numbers.items()
                             if k in merge_ids}
    mgr._merge_closing_attempts = {
        k: v for k, v in mgr._merge_closing_attempts.items() if k in merge_ids}
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
    memo = getattr(mgr, "feed_memo", None)
    if memo is not None:
        memo.retain(m.pmo_id for m in missions)
    await _reconcile_feed_memo(mgr, missions)
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
        except PMOTransient as e:
            # rate limit / budget reserve / brief outage: retried next cycle
            # by construction — one line, no traceback per mission (ADR-0040)
            log.warning("sweep deferred for %s: %s", m.key, e)
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


async def _lookup_pr(forge, m: Mission):
    pr = await forge.get_pr_by_branch(mission_branch(m.instance, m.key))
    if not pr:
        # pre-v3 branches carry no instance prefix — re-probe the legacy
        # convention so parked missions from before the upgrade still complete
        pr = await forge.get_pr_by_branch(legacy_branch(m.key))
    return pr


async def _parked_pr(mgr, forge, m: Mission):
    """(pr, state) for a parked mission, or (None, None) when no PR can be
    found for its branch. The branch→PR lookup is memoized per mission
    (docs/04 §1): the number is a stable fact once the PR exists, so a
    cycle pays the state read alone. A memoized number the forge no longer
    knows (404) is looked up again; a TERMINAL answer on a memoized number
    (merged, or closed without merging) is confirmed by the live lookup
    before the sweep writes on it — a newer PR on the same branch wins,
    exactly as it did when every cycle looked the branch up."""
    memo = mgr._merge_pr_numbers
    number = memo.get(m.pmo_id)
    if number is not None:
        try:
            state = await forge.pr_state(number)
        except ForgeError as e:
            if e.status != 404:
                raise
            memo.pop(m.pmo_id, None)
        else:
            if not (state.merged or state.state == "closed"):
                return state, state
            fresh = await _lookup_pr(forge, m)
            if fresh is not None and fresh.number == number:
                return state, state
            memo.pop(m.pmo_id, None)
            if fresh is None:
                return None, None
            memo[m.pmo_id] = fresh.number
            return fresh, await forge.pr_state(fresh.number)
    pr = await _lookup_pr(forge, m)
    if not pr:
        return None, None
    memo[m.pmo_id] = pr.number
    return pr, await forge.pr_state(pr.number)


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
    pr, state = await _parked_pr(mgr, forge, m)
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
                # a write-back (ADR-0040 §3): critical class, so a starved
                # key never leaves a closed PR's mission parked
                with completion.write_back_class():
                    await mgr.pmo.cancel_mission(m.ref)
                    await mgr.pmo.swap_labels(m.ref, remove={LABEL_MERGE},
                                              add=set())
                    await mgr._feed(
                        m.pmo_id, "issue",
                        feed.notice(
                            mgr, feed.FOR_THE_RECORD,
                            f"{state.url} was closed without merging, so the "
                            f"mission is canceled.",
                            f"🚫 PR {state.url} was closed without merging — "
                            f"mission canceled (merge sweep)."))
                mgr._audit(m.pmo_id, "merge_sweep_canceled", state.url)
                with completion.write_back_class():
                    act = await status_comment.refresh(
                        mgr, m.pmo_id, reason="merge_sweep_canceled",
                        pr_url=state.url)
                    await activity_payload.record_activity(
                        mgr, m.pmo_id, m.pmo_kind or "issue", m.key,
                        "merge_sweep_canceled", act=act)
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
    merge_retry_window_minutes elapse AND one further admitted attempt has
    not merged (a cycle whose reads the request budget refused never
    spends the window). Elapsed time is measured from the
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
    # the merge-state stamps are memoized per mission (ADR-0033 addendum):
    # they change only through our own feed writes (which invalidate the
    # memo) or a human's comment (a changed `updated_at`, or the memo's
    # safety rescan on a vendor whose `updated_at` does not track comments)
    memo = getattr(mgr, "feed_memo", None)
    stamps = memo.get("merge", m) if memo is not None else None
    if stamps is not None:
        board.bump(mgr, "feed_scan_memo_hits")
        retry_ts, handoff_ts, settle_ts = stamps
    else:
        gen = memo.generation(m.pmo_id) if memo is not None else 0
        floor = memo.witnessed(m.pmo_id) if memo is not None else None
        board.bump(mgr, "feed_scan_reads")
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
        if memo is not None and not getattr(act, "truncated", False):
            memo.put("merge", m, (retry_ts, handoff_ts, settle_ts), gen,
                     feed_until=feed_memo.feed_until(act.entries), floor=floor)

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
                feed.notice(
                    mgr, feed.INFO,
                    f"The settle window passed — DevCake is merging {pr_url}, "
                    f"retrying for up to {window} minutes if the forge is not "
                    f"ready yet.",
                    f"⏳ Settle complete — DevCake is auto-merging {pr_url}, "
                    f"retrying for up to {window} minutes if the forge is not "
                    f"ready yet. {MERGE_RETRY_MARKER}"))
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
                feed.notice(
                    mgr, feed.INFO,
                    f"Auto-merge is on again — DevCake resumes driving the "
                    f"merge of {pr_url}, retrying for up to {window} minutes "
                    f"before handing back to you.",
                    f"⏳ Auto-merge is now ON — DevCake resumes driving the merge "
                    f"of {pr_url}, retrying for up to {window} minutes before "
                    f"handing back to you. {MERGE_RETRY_MARKER}"))
            mgr._audit(m.pmo_id, "merge_retry_rearmed", pr_url)
            mgr.merge_handoffs.pop(m.pmo_id, None)
            mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: window (re)opened
            return
        mgr._merge_window_closed.add(m.pmo_id)
        mgr._rearm_satisfied.add(m.pmo_id)   # AUD-005: nothing more to (re)arm
        return  # no active retry window (auto_merge-OFF parks land here)
    # the window has elapsed once the marker is older than the bound; the
    # sweep still makes ONE admitted attempt below — a cycle whose feed read
    # was refused or skipped must not turn a mergeable PR into a hand-off —
    # and hands off only if that attempt does not merge
    elapsed = (not settle_active
               and (utcnow() - retry_ts).total_seconds() / 60 > window)
    # window ACTIVE (or its closing attempt): DevCake is still driving the
    # merge — no human action needed, so the sweep's banner entry comes off
    mgr.merge_handoffs.pop(m.pmo_id, None)
    mgr._rearm_satisfied.add(m.pmo_id)       # AUD-005: window already driving
    verdict = await forge.mergeable(pr.number)
    if verdict is None:
        if elapsed:
            # still computing past the window: a closing attempt that did
            # not merge (the second in a row hands off)
            with tracer.start_as_current_span("sweep.merge_retry") as span:
                span.set_attribute("devcake.mission.key", m.key)
                await _closing_attempt_failed(mgr, m, pr_url, window, span)
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
                    with completion.write_back_class():
                        await mgr._feed(
                            m.pmo_id, "issue",
                            feed.notice(
                                mgr, feed.NEEDS_YOU,
                                f"{pr_url} has a merge conflict and "
                                f"auto-resolve is unavailable (toggle off or "
                                f"attempts exhausted) — the merge is yours.",
                                f"⚠️ Merge conflict on {pr_url} and auto-resolve "
                                f"is unavailable (toggle off or attempts "
                                f"exhausted) — awaiting human merge "
                                f"(`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}",
                                todo="Resolve and merge it; the merge sweep "
                                     "completes the mission once it lands."))
                    mgr._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
                    mgr._merge_window_closed.add(m.pmo_id)
                    mgr.merge_handoffs[m.pmo_id] = (
                        f"{m.key}: awaiting human merge — {pr_url}")
                    with completion.write_back_class():
                        act = await status_comment.refresh(
                            mgr, m.pmo_id, reason="conflict_handoff",
                            mission=m, pr_url=pr_url)
                        await activity_payload.record_activity(
                            mgr, m.pmo_id, m.pmo_kind or "issue", m.key,
                            "conflict_handoff", act=act)
            elif elapsed:
                # a closing attempt that did not merge and was no conflict:
                # the second in a row hands off
                await _closing_attempt_failed(mgr, m, pr_url, window, span)
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


def _mark_window_exhausted(span, window: int) -> None:
    span.set_attribute("devcake.outcome", "window_exhausted")
    span.set_status(Status(StatusCode.ERROR,
                           f"unmergeable after {window} min"))


async def _hand_off_exhausted(mgr, m: Mission, pr_url: str,
                              window: int) -> None:
    """The window's closing act, posted once: the marker outlived
    merge_retry_window_minutes and the attempt just made did not merge. A
    write-back (ADR-0040 §3), so it rides the critical class — the reserve
    exists so a hand-off is never refused on a starved key. The banner is
    set first: it is true whether or not the post lands this cycle."""
    mgr.merge_handoffs[m.pmo_id] = f"{m.key}: awaiting human merge — {pr_url}"
    with completion.write_back_class():
        await mgr._feed(
            m.pmo_id, "issue",
            feed.notice(
                mgr, feed.NEEDS_YOU,
                f"{pr_url} is still unmergeable after {window} min — the "
                f"merge is yours.",
                f"⚠️ Still unmergeable after {window} min — awaiting human "
                f"merge of {pr_url} (`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}",
                todo="Merge it once the forge allows; the merge sweep "
                     "completes the mission once it lands."))
    mgr._audit(m.pmo_id, "merge_retry_exhausted", pr_url)
    mgr._merge_window_closed.add(m.pmo_id)
    with completion.write_back_class():
        act = await status_comment.refresh(mgr, m.pmo_id, reason="merge_handoff",
                                           mission=m, pr_url=pr_url)
        await activity_payload.record_activity(
            mgr, m.pmo_id, m.pmo_kind or "issue", m.key, "merge_handoff",
            act=act)


async def _closing_attempt_failed(mgr, m: Mission, pr_url: str, window: int,
                                  span) -> None:
    """An admitted attempt past the window did not merge. The second in a
    row hands off — one transient forge error must not end a mission's
    automation, and a forge that says "still computing" twice past the
    window has had its chance. The count is process-local and pruned when
    the mission leaves MERGE; a restart grants at most one extra attempt."""
    n = mgr._merge_closing_attempts.get(m.pmo_id, 0) + 1
    mgr._merge_closing_attempts[m.pmo_id] = n
    if n < CLOSING_ATTEMPTS:
        span.set_attribute("devcake.outcome", "closing_attempt_failed")
        return
    _mark_window_exhausted(span, window)
    await _hand_off_exhausted(mgr, m, pr_url, window)


async def tracking_sweep(mgr, m: Mission) -> None:
    # Snapshot gate (ADR-0003 amendment): the cycle's board already holds
    # this project's team-local children; while any of them is open the
    # project cannot complete, so no live read is due. Only when every
    # known child is terminal — or none is known (vendors without
    # `parent_ref`, cross-team projects) — is the live, paginated read
    # paid, and completion is still decided on THAT read.
    snap = getattr(mgr, "snapshot", None)
    known = snap.children_of(m.pmo_id) if snap is not None else []
    if known and any(c.status not in ("done", "canceled") for c in known):
        return
    board.bump(mgr, "tracking_children_live")
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
            # a write-back (ADR-0040 §3): critical class, never refused at
            # the reserve while the poll's own reads are
            with completion.write_back_class():
                await mgr.pmo.set_status(m.ref, "done")
                await mgr.pmo.swap_labels(m.ref, remove={LABEL_TRACKING},
                                          add=set())
            mgr._audit(m.pmo_id, "tracking_sweep_completed",
                        f"{len(children)} children")
        log.info("project %s auto-completed (%d children done)", m.key, len(children))

