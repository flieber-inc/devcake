"""REVIEW finalization, merge-before-Done, conflict auto-resolve (docs/03 §4.1)."""

from __future__ import annotations

import logging

from ...security import redact
from .. import costing
from ..model import LABEL_EXECUTE, LABEL_MERGE, LABEL_REVIEW, MissionRef
from ..run import Run
from ...ports.forge import mission_branch, run_branch
from .feed import _unquoted
from .markers import (CONFLICT_MARKER, MAX_CONFLICT_RESOLVES, MERGE_HANDOFF_MARKER,
                      MERGE_RETRY_MARKER)

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


async def _conflict_attempts(mgr, pmo_id: str) -> int:
    """Prior auto-resolve attempts, derived from feed markers — the PMO is
    the source of truth (docs/03), so the count survives restarts and a
    human deleting directive comments deliberately resets it."""
    act = await mgr.pmo.get_activity(MissionRef(pmo_id, "issue"))
    hits = [int(mt.group(1)) for e in act.entries
            for mt in CONFLICT_MARKER.finditer(_unquoted(e.body))]
    return max(hits) if hits else 0


async def _maybe_route_conflict_to_execute(mgr, pmo_id: str, key: str,
                                           pr_url: str,
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
        n = await _conflict_attempts(mgr, pmo_id)
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
        formal = False
        if pr:
            async def _pr_comment():
                await forge.post_pr_comment(
                    pr.number,
                    "## DevCake REVIEW: APPROVED-BY-DEVCAKE ✅\n\n"
                    + report + footer)
            await mgr._checkpoint(run, "review:pr_comment", _pr_comment)

            async def _formal():
                try:
                    return await forge.approve(pr.number)
                except Exception:
                    log.exception("formal approval failed — falling back to marker")
                    return False
            if "review:formal_approve" not in run.finalized_steps:
                formal = await _formal()
                run.finalized_steps.append("review:formal_approve")
                if formal:
                    run.finalized_steps.append("review:formal_approve_ok")
                mgr.runs.store.save(run)
            else:
                # redelivery: only claim formal approval if it succeeded
                formal = "review:formal_approve_ok" in run.finalized_steps

        if inst.auto_merge and pr:
            if "review:done" not in run.finalized_steps \
                    and "review:merge_failed" not in run.finalized_steps \
                    and "review:merge_deferred" not in run.finalized_steps \
                    and "review:conflict_routed" not in run.finalized_steps:
                try:
                    async def _merge():
                        await forge.merge(pr.number)
                    await mgr._checkpoint(run, "review:merge", _merge)
                    async def _done():
                        await mgr.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                             remove={LABEL_REVIEW}, add=set())
                        await mgr.pmo.set_status(MissionRef(pmo_id, "issue"), "done")
                        await mgr._feed(
                            pmo_id, "issue",
                            f"✅ REVIEW approved; PR merged ({pr_url}). "
                            f"Mission done.")
                        mgr._audit(pmo_id, "review_approve_merged", pr_url)
                    await mgr._checkpoint(run, "review:done", _done)
                    await mgr.deliver_internal_zip(run, pr)
                except Exception as e:  # noqa: BLE001 — every merge failure, whatever its type, must enter the re-probe → conflict-route → merge-failed recovery ladder; escaping would strand the mission mid-REVIEW
                    # Capture for nested async defs (static F821 + safe if
                    # await order changes later); not a known production 500.
                    merge_err = e
                    # Already-merged treated as success by forge.merge; if we
                    # still fail, re-probe before posting merge-failed (ISSUES #6).
                    try:
                        state = await forge.pr_state(pr.number)
                        if state.merged:
                            async def _done_merged():
                                if "review:merge" not in run.finalized_steps:
                                    run.finalized_steps.append("review:merge")
                                    mgr.runs.store.save(run)
                                await mgr.pmo.swap_labels(
                                    MissionRef(pmo_id, "issue"),
                                    remove={LABEL_REVIEW}, add=set())
                                await mgr.pmo.set_status(
                                    MissionRef(pmo_id, "issue"), "done")
                                await mgr._feed(
                                    pmo_id, "issue",
                                    f"✅ REVIEW approved; PR merged ({pr_url}). "
                                    f"Mission done.")
                                mgr._audit(pmo_id, "review_approve_merged", pr_url)
                            await mgr._checkpoint(run, "review:done", _done_merged)
                            await mgr.deliver_internal_zip(run, pr)
                            return
                    except Exception:
                        log.exception("pr_state probe after merge fail for %s",
                                      pr_url)
                    mstate = None
                    try:
                        mstate = await forge.mergeable(pr.number)
                    except Exception:
                        log.exception("mergeable check failed for %s", pr_url)
                    # capability branch (M11): a `False` verdict is trustworthy
                    # as a real conflict only when the forge exposes a genuine
                    # mergeable tri-state (GitHub/GitLab). On a boolean-only
                    # forge (Gitea) it can be a transient not-computed-yet, so
                    # don't route to EXECUTE rework on the verdict alone — the
                    # merge already failed above; hand off to a human instead
                    caps = getattr(forge, "capabilities", None)
                    trust_conflict = mstate is False and (
                        caps is None or caps.mergeable_tristate)
                    if trust_conflict:
                        if "review:conflict_routed" in run.finalized_steps:
                            return
                        try:
                            routed = await _maybe_route_conflict_to_execute(
                                mgr, pmo_id, run.mission_key, pr_url,
                                LABEL_REVIEW, inst)
                        except Exception:
                            log.exception("conflict route failed for %s",
                                          run.mission_key)
                            routed = False
                        if routed:
                            run.finalized_steps.append("review:conflict_routed")
                            mgr.runs.store.save(run)
                            return
                    if "review:merge_failed" not in run.finalized_steps \
                            and "review:merge_deferred" not in run.finalized_steps:
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
                                    "review:merge_deferred")
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
                                    "review:merge_failed")
                            mgr.runs.store.save(run)
                        await _fail_path()
        elif "review:awaiting_merge" not in run.finalized_steps:
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
            await mgr._checkpoint(run, "review:awaiting_merge", _await_merge)
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
            ci = mgr.config.cost_inputs
            cost = est = 0.0
            for r in mgr.runs.store.all():
                if r.mission_pmo_id != pmo_id or not mgr._run_is_ours(r):
                    continue
                tr = r.token_report or {}
                native = tr.get("cost_usd")
                estimated = tr.get("cost_usd_estimated")
                eff = costing.effective_cost(native, estimated, ci)
                if eff is None:
                    continue
                cost += eff
                if estimated is not None and (ci.override_native
                                              or native is None):
                    est += eff
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

        await mgr._checkpoint(run, "review:reject:feed", _reject_feed)
        await mgr._checkpoint(run, "review:reject:pr_comment",
                               _reject_pr_comment)
        await mgr._checkpoint(run, "review:reject:labels", _reject_labels)
        await mgr._checkpoint(run, "review:reject:loop_warn",
                               _reject_loop_warn)
        if "review:reject" not in run.finalized_steps:
            run.finalized_steps.append("review:reject")
            mgr.runs.store.save(run)

