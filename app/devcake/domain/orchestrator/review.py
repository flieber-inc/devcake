"""REVIEW finalization, merge-before-Done, conflict auto-resolve (docs/03 §4.1)."""

from __future__ import annotations

import logging

from ...security import redact
from ..model import LABEL_EXECUTE, LABEL_MERGE, LABEL_REVIEW, MissionRef
from ..run import Run
from ...ports.forge import run_branch
from . import completion, dispatch, steps
from .freshness import review_freshness_gate
from .markers import (HANDOFF_APPEND_MAX, HANDOFF_MARKER,
                      MERGE_HANDOFF_MARKER, MERGE_RETRY_MARKER, defang)

log = logging.getLogger("devcake.missions")


async def _flag_out_of_pipeline_merge(mgr, run: Run) -> None:
    """Detection tripwire (docs/14, ADR-0007 addendum): the Dev's forge token
    can merge unless branch protection forbids it. If the mission's PR turns
    up merged while the mission is still mid-pipeline, say so loudly —
    detection only; a human decides (they may have merged early themselves)."""
    forge = mgr.forges.get(run.repo_ref)
    if forge is None:
        # repo vanished: the detection cannot run — say so instead of going
        # silently blind (an out-of-pipeline merge could slip past unseen)
        mgr.anomalies[run.mission_pmo_id] = (
            f"{run.mission_key}: out-of-pipeline-merge detection suspended — "
            f"repo '{run.repo_ref}' is no longer configured")
        log.warning("out-of-pipeline check skipped for %s: repo %r vanished",
                    run.run_id, run.repo_ref)
        return
    try:
        pr = await forge.get_pr_by_branch(run_branch(run))
        if not pr:
            return
        state = await forge.pr_state(pr.number)
        if not state.merged:
            return
        await mgr._feed(
            run.mission_pmo_id, run.pmo_kind,
            f"⚠️ **Out-of-pipeline merge detected:** {state.url} is already "
            f"merged, but this mission is still mid-pipeline "
            f"({run.mission_type}). If you merged it yourself on purpose, "
            f"mark the mission Done (or add `DEVCAKE-SKIP`); otherwise check "
            f"who merged it — DevCake did not.")
        mgr._audit(run.mission_pmo_id, "out_of_pipeline_merge", state.url)
        mgr.anomalies[run.mission_pmo_id] = (
            f"{run.mission_key}: PR merged outside the pipeline ({state.url})")
        log.warning("out-of-pipeline merge on %s (%s)", run.mission_key,
                    state.url)
    except Exception:  # noqa: BLE001 — anomaly probe is best-effort; failure logged (debug), the review flow is unaffected
        log.debug("out-of-pipeline merge check failed for %s",
                  run.mission_key, exc_info=True)


async def _append_handoff(mgr, run: Run, result: dict) -> None:
    """ADR-0032 — the mission's closing note, appended to its DESCRIPTION as
    a marked section on approve. Three hardenings, none optional: redact()
    (append_description is a raw pass-through in both adapters — the only
    model-output sink without its own choke point, and the text is
    re-injected into every downstream prompt); backtick-defang (a handoff
    quoting a decomposition / devcake-repo / handoff marker must never
    shadow a real one — description markers anchor last-match); cap +
    best-effort (lineage-note doctrine: a vendor description-cap failure
    must never block the close or burn attempts). Re-approves append again;
    consumers take the LAST marker (markers.handoff_of)."""
    text = str(result.get("handoff_md") or "").strip()
    if not text:
        return
    pmo_id = run.mission_pmo_id

    async def _note():
        body = defang(redact(text))[:HANDOFF_APPEND_MAX]
        note = f"\n\n---\n{HANDOFF_MARKER}\n{body}\n"
        try:
            await mgr.pmo.append_description(
                MissionRef(pmo_id, "issue"), note)
        except Exception as e:  # noqa: BLE001 — best-effort BY DESIGN (lineage-note precedent): the close proceeds, the failure is audited
            mgr._audit(pmo_id, "handoff_append_failed", str(e)[:200])
    await mgr._checkpoint(run, steps.REVIEW_HANDOFF, _note)


async def finalize_review(mgr, run: Run, result: dict) -> None:
    pmo_id = run.mission_pmo_id
    verdict = result.get("verdict")
    report = result.get("report_md") or result.get("summary") or ""
    forge = mgr.forges.get(run.repo_ref)
    # forges + instances are co-populated (rebuild / register_internal); still
    # treat a missing instance as vanished so a desync never AttributeErrors
    # mid-finalize (resolution-failure contract, domain/forge_runtime.py)
    inst = mgr.forges.instance(run.repo_ref) if forge is not None else None
    if forge is None or inst is None:
        run.verdict = redact(
            f"failed: repo '{run.repo_ref}' is no longer configured — "
            f"REVIEW outcome not applied")
        log.error("finalize_review %s: repo %r vanished — no transition",
                  run.run_id, run.repo_ref)
        return
    pr = await forge.get_pr_by_branch(run_branch(run))
    pr_url = (pr.url if pr else None) or result.get("pr_url") or "?"
    footer = forge.approval_footer(pr_url)

    if verdict == "approve":
        # ADR-0031 — the Freshness Gate, BEFORE any approval artifact: a
        # tripped run must post nothing a re-review would duplicate. Withheld
        # transition ⇒ the mission keeps DEVCAKE-REVIEW and the next poll
        # re-dispatches (attempt 1 — a finished run is a reset anchor).
        if await review_freshness_gate(mgr, run) == "tripped":
            return
        # ADR-0032 — one append site covers every eventual-done path (auto-
        # merge now, sweep/human merge later): the handoff is a property of
        # the APPROVE, not of the merge mechanics
        await _append_handoff(mgr, run, result)
        formal = False
        if pr:
            async def _pr_comment():
                await forge.post_pr_comment(
                    pr.number,
                    "## DevCake REVIEW: APPROVED-BY-DEVCAKE ✅\n\n"
                    + report + footer)
            await mgr._checkpoint(run, steps.REVIEW_PR_COMMENT, _pr_comment)

            async def _formal():
                try:
                    return await forge.approve(pr.number)
                except Exception:
                    log.exception("formal approval failed — falling back to marker")
                    return False
            if steps.REVIEW_FORMAL_APPROVE not in run.finalized_steps:
                formal = await _formal()
                run.finalized_steps.append(steps.REVIEW_FORMAL_APPROVE)
                if formal:
                    run.finalized_steps.append(steps.REVIEW_FORMAL_APPROVE_OK)
                mgr.runs.store.save(run)
            else:
                # redelivery: only claim formal approval if it succeeded
                formal = steps.REVIEW_FORMAL_APPROVE_OK in run.finalized_steps

        from ...config import auto_merge_permitted
        if auto_merge_permitted(mgr.config, inst, run.repo_ref,
                                mgr.dev_types) and pr:
            if steps.REVIEW_DONE not in run.finalized_steps \
                    and steps.REVIEW_MERGE_FAILED not in run.finalized_steps \
                    and steps.REVIEW_MERGE_DEFERRED not in run.finalized_steps \
                    and steps.REVIEW_CONFLICT_ROUTED not in run.finalized_steps:
                try:
                    async def _merge():
                        await forge.merge(pr.number)
                    await mgr._checkpoint(run, steps.REVIEW_MERGE, _merge)
                except Exception as e:  # noqa: BLE001 — every MERGE failure, whatever its type, must enter the re-probe → conflict-route → merge-failed recovery ladder; escaping would strand the mission mid-REVIEW
                    merge_err = e
                    handled_below = True
                else:
                    # merge landed: complete via the chokepoint. Its failures
                    # PROPAGATE (F4) — the run stays finalizing and converges
                    # via redelivery (the four-way guard above doesn't block;
                    # steps.REVIEW_MERGE no-ops; steps.REVIEW_DONE retries) or, past
                    # the stalled-finalize deadline, the next REVIEW cycle's
                    # re-probe of an already-merged PR.
                    await completion.complete_merged(
                        mgr, completion.MergedCause.REVIEW_AUTO_MERGE,
                        ref=MissionRef(pmo_id, "issue"),
                        mission_key=run.mission_key,
                        pr=pr, pr_url=pr_url, run=run)
                    handled_below = False
                if handled_below:
                    # Already-merged treated as success by forge.merge; if we
                    # still fail, re-probe before posting merge-failed
                    # (ISSUES #6). The try covers ONLY the probe (2026-08-12
                    # audit F4): the old scope also swallowed completion's
                    # PMO transients, misattributed them to the probe, and
                    # posted "auto-merge failed" on an already-merged PR.
                    merged = False
                    try:
                        merged = (await forge.pr_state(pr.number)).merged
                    except Exception:
                        log.exception("pr_state probe after merge fail for %s",
                                      pr_url)
                    if merged:
                        # absorb the found-merged stamp so a redelivery
                        # skips the merge checkpoint, then complete via the
                        # chokepoint — its failures PROPAGATE (run stays
                        # finalizing; redelivery/stalled-finalize converge)
                        if steps.REVIEW_MERGE not in run.finalized_steps:
                            run.finalized_steps.append(steps.REVIEW_MERGE)
                            mgr.runs.store.save(run)
                        await completion.complete_merged(
                            mgr, completion.MergedCause.REVIEW_AUTO_MERGE,
                            ref=MissionRef(pmo_id, "issue"),
                            mission_key=run.mission_key,
                            pr=pr, pr_url=pr_url, run=run)
                        return
                    mstate = None
                    try:
                        mstate = await forge.mergeable(pr.number)
                    except Exception:
                        log.exception("mergeable check failed for %s", pr_url)
                    if completion.trusted_conflict(forge, mstate):
                        if steps.REVIEW_CONFLICT_ROUTED in run.finalized_steps:
                            return
                        try:
                            routed = await completion.route_conflict_to_execute(
                                mgr, pmo_id, run.mission_key, pr_url,
                                LABEL_REVIEW, inst)
                        except Exception:
                            log.exception("conflict route failed for %s",
                                          run.mission_key)
                            routed = False
                        if routed:
                            run.finalized_steps.append(steps.REVIEW_CONFLICT_ROUTED)
                            mgr.runs.store.save(run)
                            return
                    if steps.REVIEW_MERGE_FAILED not in run.finalized_steps \
                            and steps.REVIEW_MERGE_DEFERRED not in run.finalized_steps:
                        async def _fail_path():
                            await mgr.pmo.swap_labels(
                                MissionRef(pmo_id, "issue"),
                                remove={LABEL_REVIEW}, add={LABEL_MERGE})
                            if mstate is not False and \
                                    inst.merge_retry_window_minutes > 0:
                                await mgr._feed(
                                    pmo_id, "issue",
                                    f"⏳ REVIEW approved but the merge is not "
                                    f"possible yet ({merge_err}) — DevCake keeps "
                                    f"retrying for up to "
                                    f"{inst.merge_retry_window_minutes} "
                                    f"minutes (mergeability computing / CI "
                                    f"pipeline running). You can merge {pr_url} "
                                    f"manually at any time. {MERGE_RETRY_MARKER}")
                                mgr._audit(pmo_id, "merge_deferred",
                                            str(merge_err)[:120])
                                mgr._merge_window_closed.discard(pmo_id)
                                run.finalized_steps.append(
                                    steps.REVIEW_MERGE_DEFERRED)
                            else:
                                await mgr._feed(
                                    pmo_id, "issue",
                                    f"⚠️ REVIEW approved but auto-merge failed "
                                    f"({merge_err}); awaiting human merge of "
                                    f"{pr_url} (`DEVCAKE-MERGE`). "
                                    f"{MERGE_HANDOFF_MARKER}")
                                mgr._audit(pmo_id,
                                            "review_approve_merge_failed",
                                            str(merge_err)[:120])
                                run.finalized_steps.append(
                                    steps.REVIEW_MERGE_FAILED)
                            mgr.runs.store.save(run)
                        await _fail_path()
        elif auto_merge_permitted(mgr.config, inst, run.repo_ref,
                                  mgr.dev_types) \
                and inst.merge_retry_window_minutes > 0 \
                and steps.REVIEW_MERGE_DEFERRED not in run.finalized_steps \
                and steps.REVIEW_AWAITING_MERGE not in run.finalized_steps:
            # AUD-006: auto_merge is ON but the PR wasn't visible at finalize
            # (forge list lag / branch-naming miss). Pure human-await copy
            # would strand app-driven merge FOREVER — the sweep silent-returns
            # on a missing PR and opens no window without a marker. Open a
            # deferred window instead: the sweep drives the merge the moment
            # the PR surfaces, and the window bounds the wait before handing
            # back. Never pure human-await when auto_merge is ON.
            async def _defer_missing_pr():
                await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                          remove={LABEL_REVIEW}, add={LABEL_MERGE})
                await mgr._feed(
                    pmo_id, "issue",
                    f"⏳ REVIEW approved but the PR isn't visible yet — DevCake "
                    f"will auto-merge it once it appears, retrying for up to "
                    f"{inst.merge_retry_window_minutes} minutes before handing "
                    f"back to you. You can merge {pr_url} manually at any time. "
                    f"{MERGE_RETRY_MARKER}")
                mgr._audit(pmo_id, "review_approve_defer_missing_pr", pr_url)
                mgr._merge_window_closed.discard(pmo_id)
                run.finalized_steps.append(steps.REVIEW_MERGE_DEFERRED)
                mgr.runs.store.save(run)
            await _defer_missing_pr()
        elif steps.REVIEW_AWAITING_MERGE not in run.finalized_steps:
            async def _await_merge():
                await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove={LABEL_REVIEW}, add={LABEL_MERGE})
                await mgr._feed(
                    pmo_id, "issue",
                    f"✅ REVIEW approved "
                    f"({'formal approval filed' if formal else 'APPROVED-BY-DEVCAKE marker'}). "
                    f"Awaiting human merge of {pr_url} — the merge sweep completes "
                    f"this mission once it merges." + footer)
                mgr._audit(pmo_id, "review_approve_awaiting_merge", pr_url)
            await mgr._checkpoint(run, steps.REVIEW_AWAITING_MERGE, _await_merge)
    else:  # reject
        rejections = 1 + sum(
            1 for r in mgr.runs.store.all()
            if r.mission_pmo_id == pmo_id and mgr._run_is_ours(r) and r.mission_type == "REVIEW"
            and r.state == "finished"
            and (r.result or {}).get("verdict") == "reject")

        async def _reject_feed():
            feed_body = (f"🔁 REVIEW rejected (round {rejections}) — back to "
                         f"EXECUTE.\n\n" + report)
            if report:
                try:
                    name = f"{run.seq}_REVIEW_REPORT.md"
                    url = await mgr.pmo.upload_attachment(
                        pmo_id, name, redact(report).encode())
                    feed_body = (
                        f"🔁 REVIEW rejected (round {rejections}) — back "
                        f"to EXECUTE. Full report attached: "
                        f"[{name}]({url})")
                except Exception:
                    log.exception("review report upload failed — posting inline")
            await mgr._feed(pmo_id, "issue", feed_body)

        async def _reject_pr_comment():
            if pr:
                await forge.post_pr_comment(
                    pr.number,
                    "## DevCake REVIEW: changes requested 🔁\n\n"
                    + report + footer)

        async def _reject_labels():
            await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                   remove={LABEL_REVIEW}, add={LABEL_EXECUTE})
            mgr._audit(pmo_id, "label_swap",
                        f"{LABEL_REVIEW}→{LABEL_EXECUTE}")

        async def _reject_loop_warn():
            every = mgr.config.review_loop_warning_every
            if every < 1 or rejections % every != 0:
                return
            # effective per run (ADR-0021): native harness cost when reported,
            # else the finalize-stamped estimate — grok spend no longer reads
            # as $0.00 here. The estimated share is named, not blended away.
            # ONE rollup (ADR-0034 PR-3): this used to inline a near-copy of
            # dispatch.mission_cost's arithmetic.
            cost, est = dispatch.mission_cost(mgr, pmo_id,
                                              split_estimated=True)
            share = f" (of which ${est:.2f} estimated)" if est else ""
            warn = (f"⚠️ **Loop warning:** this mission has been through "
                    f"{rejections} REVIEW rejections. Cumulative recorded "
                    f"cost so far: ${cost:.2f}{share} (runs with no cost "
                    f"data not included). Add `DEVCAKE-SKIP` to stop "
                    f"DevCake, or intervene on the PR directly.")
            await mgr._feed(pmo_id, "issue", warn)
            if pr:
                await forge.post_pr_comment(pr.number, warn)
            mgr._audit(pmo_id, "loop_warning", f"{rejections} rejections")

        await mgr._checkpoint(run, steps.REVIEW_REJECT_FEED, _reject_feed)
        await mgr._checkpoint(run, steps.REVIEW_REJECT_PR_COMMENT,
                               _reject_pr_comment)
        await mgr._checkpoint(run, steps.REVIEW_REJECT_LABELS, _reject_labels)
        await mgr._checkpoint(run, steps.REVIEW_REJECT_LOOP_WARN,
                               _reject_loop_warn)
        if steps.REVIEW_REJECT not in run.finalized_steps:
            run.finalized_steps.append(steps.REVIEW_REJECT)
            mgr.runs.store.save(run)

