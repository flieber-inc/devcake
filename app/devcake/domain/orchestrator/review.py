"""REVIEW finalization, merge-before-Done, conflict auto-resolve (docs/03 §4.1)."""

from __future__ import annotations

import logging

from ...security import redact
from ..model import LABEL_EXECUTE, LABEL_MERGE, LABEL_REVIEW, MissionRef
from ..run import Run
from ...ports.forge import mission_branch, run_branch
from .markers import (CONFLICT_MARKER, MAX_CONFLICT_RESOLVES, MERGE_HANDOFF_MARKER,
                      MERGE_RETRY_MARKER)

log = logging.getLogger("devcake.missions")


async def _flag_out_of_pipeline_merge(self, run: Run) -> None:
    """Detection tripwire (docs/14, ADR-0007 addendum): the Dev's forge token
    can merge unless branch protection forbids it. If the mission's PR turns
    up merged while the mission is still mid-pipeline, say so loudly —
    detection only; a human decides (they may have merged early themselves)."""
    forge = self.forges.get(run.repo_ref)
    if forge is None:
        # repo vanished: the detection cannot run — say so instead of going
        # silently blind (an out-of-pipeline merge could slip past unseen)
        self.anomalies[run.mission_pmo_id] = (
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
        await self._feed(
            run.mission_pmo_id, run.pmo_kind,
            f"⚠️ **Out-of-pipeline merge detected:** {state.url} is already "
            f"merged, but this mission is still mid-pipeline "
            f"({run.mission_type}). If you merged it yourself on purpose, "
            f"mark the mission Done (or add `DEVCAKE-SKIP`); otherwise check "
            f"who merged it — DevCake did not.")
        self._audit(run.mission_pmo_id, "out_of_pipeline_merge", state.url)
        self.anomalies[run.mission_pmo_id] = (
            f"{run.mission_key}: PR merged outside the pipeline ({state.url})")
        log.warning("out-of-pipeline merge on %s (%s)", run.mission_key,
                    state.url)
    except Exception:
        log.debug("out-of-pipeline merge check failed for %s",
                  run.mission_key, exc_info=True)


async def _conflict_attempts(self, pmo_id: str) -> int:
    """Prior auto-resolve attempts, derived from feed markers — the PMO is
    the source of truth (docs/03), so the count survives restarts and a
    human deleting directive comments deliberately resets it."""
    act = await self.pmo.get_activity(MissionRef(pmo_id, "issue"))
    hits = [int(mt.group(1)) for e in act.entries
            for mt in CONFLICT_MARKER.finditer(self._unquoted(e.body))]
    return max(hits) if hits else 0


async def _maybe_route_conflict_to_execute(self, pmo_id: str, key: str,
                                           pr_url: str,
                                           from_label: str) -> bool:
    """docs/03 §4.1 — on an auto-resolvable merge failure (conflict or
    stale branch), route the mission back to EXECUTE with a resolve
    directive, max MAX_CONFLICT_RESOLVES attempts per mission. Returns
    True when routed; any failure or decline returns False so the caller's
    human fallback (DEVCAKE-MERGE) is never blocked."""
    if not self.config.auto_resolve_merge_conflicts:
        return False
    try:
        n = await self._conflict_attempts(pmo_id)
        if n >= MAX_CONFLICT_RESOLVES:
            self._audit(pmo_id, "conflict_resolve_exhausted", pr_url)
            return False
        # directive FIRST, then swap (mirrors the reject path): if the
        # post fails the mission stays put and the marker count never
        # undercounts. The EXECUTE playbook tells the Dev a 🧩 resolve
        # directive overrides its normal implement-the-mission job (🧩 is
        # reserved for this directive — 🔀 already means "PR opened").
        await self._feed(
            pmo_id, "issue",
            f"🧩 Auto-merge hit a merge conflict on {pr_url} (auto-resolve "
            f"attempt {n + 1}/{MAX_CONFLICT_RESOLVES}) — back to EXECUTE. "
            f"Next Dev: sync `{mission_branch(self.instance_name, key)}` with the default branch, "
            f"resolve the conflicts, and push; the PR then returns to "
            f"REVIEW. `devcake:conflict-resolve:{n + 1}`")
        await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                   remove={from_label}, add={LABEL_EXECUTE})
        self._audit(pmo_id, "conflict_resolve_dispatched",
                    f"attempt {n + 1} ({pr_url})")
        return True
    except Exception:
        log.exception("conflict auto-resolve routing failed for %s", key)
        return False


async def _finalize_review(self, run: Run, result: dict) -> None:
    pmo_id = run.mission_pmo_id
    verdict = result.get("verdict")
    report = result.get("report_md") or result.get("summary") or ""
    forge = self.forges.get(run.repo_ref)
    if forge is None:
        # the run's repo vanished from config mid-flight: fail CLEANLY —
        # transcripts/report are already posted; no transition is applied
        # (the resolution-failure contract, domain/forge_runtime.py)
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
            await self._checkpoint(run, "review:pr_comment", _pr_comment)

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
                self.runs.store.save(run)
            else:
                # redelivery: only claim formal approval if it succeeded
                formal = "review:formal_approve_ok" in run.finalized_steps

        if self.config.auto_merge and pr:
            if "review:done" not in run.finalized_steps \
                    and "review:merge_failed" not in run.finalized_steps \
                    and "review:merge_deferred" not in run.finalized_steps \
                    and "review:conflict_routed" not in run.finalized_steps:
                try:
                    async def _merge():
                        await forge.merge(pr.number)
                    await self._checkpoint(run, "review:merge", _merge)
                    async def _done():
                        await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                             remove={LABEL_REVIEW}, add=set())
                        await self.pmo.set_status(MissionRef(pmo_id, "issue"), "done")
                        await self._feed(
                            pmo_id, "issue",
                            f"✅ REVIEW approved; PR merged ({pr_url}). "
                            f"Mission done.")
                        self._audit(pmo_id, "review_approve_merged", pr_url)
                    await self._checkpoint(run, "review:done", _done)
                except Exception as e:
                    # Already-merged treated as success by forge.merge; if we
                    # still fail, re-probe before posting merge-failed (ISSUES #6).
                    try:
                        state = await forge.pr_state(pr.number)
                        if state.merged:
                            async def _done_merged():
                                if "review:merge" not in run.finalized_steps:
                                    run.finalized_steps.append("review:merge")
                                    self.runs.store.save(run)
                                await self.pmo.swap_labels(
                                    MissionRef(pmo_id, "issue"),
                                    remove={LABEL_REVIEW}, add=set())
                                await self.pmo.set_status(
                                    MissionRef(pmo_id, "issue"), "done")
                                await self._feed(
                                    pmo_id, "issue",
                                    f"✅ REVIEW approved; PR merged ({pr_url}). "
                                    f"Mission done.")
                                self._audit(pmo_id, "review_approve_merged", pr_url)
                            await self._checkpoint(run, "review:done", _done_merged)
                            return
                    except Exception:
                        log.exception("pr_state probe after merge fail for %s",
                                      pr_url)
                    mstate = None
                    try:
                        mstate = await forge.mergeable(pr.number)
                    except Exception:
                        log.exception("mergeable check failed for %s", pr_url)
                    if mstate is False:
                        if "review:conflict_routed" in run.finalized_steps:
                            return
                        try:
                            routed = await self._maybe_route_conflict_to_execute(
                                pmo_id, run.mission_key, pr_url, LABEL_REVIEW)
                        except Exception:
                            log.exception("conflict route failed for %s",
                                          run.mission_key)
                            routed = False
                        if routed:
                            run.finalized_steps.append("review:conflict_routed")
                            self.runs.store.save(run)
                            return
                    if "review:merge_failed" not in run.finalized_steps \
                            and "review:merge_deferred" not in run.finalized_steps:
                        async def _fail_path():
                            await self.pmo.swap_labels(
                                MissionRef(pmo_id, "issue"),
                                remove={LABEL_REVIEW}, add={LABEL_MERGE})
                            if mstate is not False and \
                                    self.config.merge_retry_window_minutes > 0:
                                await self._feed(
                                    pmo_id, "issue",
                                    f"⏳ REVIEW approved but the merge is not "
                                    f"possible yet ({e}) — DevCake keeps "
                                    f"retrying for up to "
                                    f"{self.config.merge_retry_window_minutes} "
                                    f"minutes (mergeability computing / CI "
                                    f"pipeline running). You can merge {pr_url} "
                                    f"manually at any time. {MERGE_RETRY_MARKER}")
                                self._audit(pmo_id, "merge_deferred",
                                            str(e)[:120])
                                self._merge_window_closed.discard(pmo_id)
                                run.finalized_steps.append(
                                    "review:merge_deferred")
                            else:
                                await self._feed(
                                    pmo_id, "issue",
                                    f"⚠️ REVIEW approved but auto-merge failed "
                                    f"({e}); awaiting human merge of {pr_url} "
                                    f"(`DEVCAKE-MERGE`). {MERGE_HANDOFF_MARKER}")
                                self._audit(pmo_id,
                                            "review_approve_merge_failed",
                                            str(e)[:120])
                                run.finalized_steps.append(
                                    "review:merge_failed")
                            self.runs.store.save(run)
                        await _fail_path()
        elif "review:awaiting_merge" not in run.finalized_steps:
            async def _await_merge():
                await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                           remove={LABEL_REVIEW}, add={LABEL_MERGE})
                await self._feed(
                    pmo_id, "issue",
                    f"✅ REVIEW approved "
                    f"({'formal approval filed' if formal else 'APPROVED-BY-DEVCAKE marker'}). "
                    f"Awaiting human merge of {pr_url} — the merge sweep completes "
                    f"this mission once it merges." + footer)
                self._audit(pmo_id, "review_approve_awaiting_merge", pr_url)
            await self._checkpoint(run, "review:awaiting_merge", _await_merge)
    else:  # reject
        rejections = 1 + sum(
            1 for r in self.runs.store.all()
            if r.mission_pmo_id == pmo_id and self._run_is_ours(r) and r.mission_type == "REVIEW"
            and r.state == "finished"
            and (r.result or {}).get("verdict") == "reject")

        async def _reject_feed():
            feed_body = (f"🔁 REVIEW rejected (round {rejections}) — back to "
                         f"EXECUTE.\n\n" + report)
            if report:
                try:
                    name = f"{run.seq}_REVIEW_REPORT.md"
                    url = await self.pmo.upload_attachment(
                        pmo_id, name, redact(report).encode())
                    feed_body = (
                        f"🔁 REVIEW rejected (round {rejections}) — back "
                        f"to EXECUTE. Full report attached: "
                        f"[{name}]({url})")
                except Exception:
                    log.exception("review report upload failed — posting inline")
            await self._feed(pmo_id, "issue", feed_body)

        async def _reject_pr_comment():
            if pr:
                await forge.post_pr_comment(
                    pr.number,
                    "## DevCake REVIEW: changes requested 🔁\n\n"
                    + report + footer)

        async def _reject_labels():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                   remove={LABEL_REVIEW}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_swap",
                        f"{LABEL_REVIEW}→{LABEL_EXECUTE}")

        async def _reject_loop_warn():
            every = self.config.review_loop_warning_every
            if every < 1 or rejections % every != 0:
                return
            cost = sum((r.token_report or {}).get("cost_usd") or 0
                       for r in self.runs.store.all()
                       if r.mission_pmo_id == pmo_id and self._run_is_ours(r))
            warn = (f"⚠️ **Loop warning:** this mission has been through "
                    f"{rejections} REVIEW rejections. Cumulative recorded "
                    f"cost so far: ${cost:.2f} (runs without cost data not "
                    f"included). Add `DEVCAKE-SKIP` to stop DevCake, or "
                    f"intervene on the PR directly.")
            await self._feed(pmo_id, "issue", warn)
            if pr:
                await forge.post_pr_comment(pr.number, warn)
            self._audit(pmo_id, "loop_warning", f"{rejections} rejections")

        await self._checkpoint(run, "review:reject:feed", _reject_feed)
        await self._checkpoint(run, "review:reject:pr_comment",
                               _reject_pr_comment)
        await self._checkpoint(run, "review:reject:labels", _reject_labels)
        await self._checkpoint(run, "review:reject:loop_warn",
                               _reject_loop_warn)
        if "review:reject" not in run.finalized_steps:
            run.finalized_steps.append("review:reject")
            self.runs.store.save(run)

