"""Compare-and-transition router for non-REVIEW outcomes (docs/04 §4, docs/03)."""

from __future__ import annotations

import logging

from ...security import redact
from ..model import (LABEL_EXECUTE, LABEL_NEEDS_HUMAN, LABEL_PLAN, LABEL_REVIEW,
                     LABEL_SKIP, MissionRef)
from ..run import Run
from .markers import COMMENT_SENTINEL, LEGAL_OUTCOMES, _SWAP_MARKER_STAGE

log = logging.getLogger("devcake.missions")


async def _transition(self, run: Run, result: dict, plan_md: str | None) -> None:
    outcome = result.get("outcome", "")
    pmo_id = run.mission_pmo_id
    if outcome not in LEGAL_OUTCOMES.get(run.mission_type, frozenset()):
        # illegal (or unknown) outcome for this step — the trust boundary.
        # Park for a human; never act on it (docs/03 §6, docs/15).
        async def _illegal():
            await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                   remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"⛔ DevCake received outcome `{outcome or '(empty)'}` from a "
                f"**{run.mission_type}** run — not a legal outcome for that step. "
                f"No transition was applied; parked with `DEVCAKE-SKIP` for a "
                f"human to inspect.")
            self._audit(pmo_id, "illegal_outcome",
                        f"{outcome or '(empty)'} from {run.mission_type}")
        await self._checkpoint(run, "transition:illegal", _illegal)
        run.verdict = redact(
            f"rejected: outcome {outcome or '(empty)'} is illegal "
            f"for {run.mission_type} — parked with DEVCAKE-SKIP")
        log.warning("illegal outcome %r from %s run %s — parked",
                    outcome, run.mission_type, run.run_id)
        return
    live = await self.pmo.get(MissionRef(pmo_id, run.pmo_kind))               # live re-read
    if run.pmo_kind == "project" and outcome not in ("decomposed", "human_needed"):
        async def _proj_park():
            await self.pmo.swap_labels(MissionRef(pmo_id, "project"),
                                       remove=set(), add={LABEL_SKIP})
            self._audit(pmo_id, "project_bad_outcome_parked", outcome)
        await self._checkpoint(run, "transition:project_park", _proj_park)
        run.verdict = redact(
            f"rejected: project returned {outcome} (only decomposed "
            f"is legal) — parked with DEVCAKE-SKIP")
        log.warning("project %s returned %s (only decomposed is legal) — parked",
                    run.mission_key, outcome)
        return
    # A redelivery must not mistake its OWN checkpointed label swap for an
    # external change — but a change a human made BETWEEN deliveries (e.g.
    # canceling the mission while the app was down) must still halt the
    # resumed finalize. So: the live stage is acceptable iff it equals the
    # dispatch stage OR the exact stage a checkpointed swap of ours left
    # behind (_SWAP_MARKER_STAGE). Markers that never touch stage labels
    # (feeds, uploads, pr comments) deliberately contribute nothing, and
    # unmapped future markers degrade to the strict pre-swap check, never
    # to suppression.
    expected_stages = {run.stage_label_at_dispatch}
    expected_stages.update(
        stage for marker, stage in _SWAP_MARKER_STAGE.items()
        if marker in run.finalized_steps)
    if self._stage_of(live) not in expected_stages:
        async def _external():
            await self._feed(
                pmo_id, run.pmo_kind,
                f"ℹ️ DevCake completed a **{run.mission_type}** run (`{run.run_id}`), but "
                f"this mission's state was changed externally while it ran. The output is "
                f"posted above; **no status or label changes were applied**.")
            self._audit(pmo_id, "external_transition", run.run_id)
        await self._checkpoint(run, "transition:external", _external)
        run.verdict = ("skipped: mission state changed externally while the "
                       "run was in flight — no transition applied")
        log.warning("EXTERNAL_TRANSITION on %s — no labels applied", run.run_id)
        return

    if outcome in ("executed", "executed_trivially", "reviewed"):
        await self._flag_out_of_pipeline_merge(run)

    if outcome == "planned":
        plan_name = f"PLAN_{run.seq}.md"
        plan_url_box: list[str] = []

        async def _plan_upload():
            url = await self.pmo.upload_attachment(
                pmo_id, plan_name, redact(plan_md or "").encode())
            plan_url_box.clear()
            plan_url_box.append(url)

        async def _plan_feed():
            url = plan_url_box[0] if plan_url_box else f"(see attachment {plan_name})"
            await self._feed(
                pmo_id, run.pmo_kind,
                f"📋 DevCake plan for this mission: [{plan_name}]({url})")

        async def _plan_labels():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove={LABEL_PLAN}, add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_swap", f"{LABEL_PLAN}→{LABEL_EXECUTE}")

        await self._checkpoint(run, "transition:planned:upload", _plan_upload)
        await self._checkpoint(run, "transition:planned:feed", _plan_feed)
        await self._checkpoint(run, "transition:planned:labels", _plan_labels)
        if "transition:planned" not in run.finalized_steps:
            run.finalized_steps.append("transition:planned")
            self.runs.store.save(run)
    elif outcome == "executed":
        async def _executed_labels():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove={LABEL_EXECUTE}, add={LABEL_REVIEW})
            self._audit(pmo_id, "label_swap", f"{LABEL_EXECUTE}→{LABEL_REVIEW}")

        async def _executed_feed():
            _f = self.forges.get(run.repo_ref)
            noun = _f.descriptor.pr_noun if _f else "pull request"
            await self._feed(
                pmo_id, run.pmo_kind,
                f"🔀 DevCake opened/updated the {noun}: "
                f"{result.get('pr_url', '(no url reported)')} — awaiting REVIEW.")

        await self._checkpoint(run, "transition:executed:labels", _executed_labels)
        await self._checkpoint(run, "transition:executed:feed", _executed_feed)
        if "transition:executed" not in run.finalized_steps:
            run.finalized_steps.append("transition:executed")
            self.runs.store.save(run)
    elif outcome == "executed_trivially":
        async def _trivial_labels():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove=set(), add={LABEL_REVIEW})
            self._audit(pmo_id, "label_add", LABEL_REVIEW)

        async def _trivial_feed():
            _f = self.forges.get(run.repo_ref)
            noun = _f.descriptor.pr_noun if _f else "pull request"
            await self._feed(
                pmo_id, run.pmo_kind,
                f"🔀 Trivial path: {noun} opened ({result.get('pr_url', '?')}) "
                f"— the trivial path never skips REVIEW (docs/03 §1.1).")

        await self._checkpoint(run, "transition:executed_trivially:labels",
                               _trivial_labels)
        await self._checkpoint(run, "transition:executed_trivially:feed",
                               _trivial_feed)
        if "transition:executed_trivially" not in run.finalized_steps:
            run.finalized_steps.append("transition:executed_trivially")
            self.runs.store.save(run)
    elif outcome == "reviewed":
        await self._finalize_review(run, result)
    elif outcome == "decomposed":
        await self._finalize_decomposition(run, result)
    elif outcome == "plan_needed" and plan_md:
        plan_name = f"PLAN_{run.seq}.md"
        plan_url_box: list[str] = []

        async def _plan_attach_upload():
            url = await self.pmo.upload_attachment(
                pmo_id, plan_name, redact(plan_md).encode())
            plan_url_box.clear()
            plan_url_box.append(url)

        async def _plan_attach_feed():
            url = plan_url_box[0] if plan_url_box else f"(see {plan_name})"
            await self._feed(
                pmo_id, run.pmo_kind,
                f"📋 DevCake attached an opportunistic plan from triage: "
                f"[{plan_name}]({url}) — skipping the PLAN step.")

        async def _plan_attach_labels():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove=set(), add={LABEL_EXECUTE})
            self._audit(pmo_id, "label_add", LABEL_EXECUTE)

        await self._checkpoint(run, "transition:plan_needed_attach:upload",
                               _plan_attach_upload)
        await self._checkpoint(run, "transition:plan_needed_attach:feed",
                               _plan_attach_feed)
        await self._checkpoint(run, "transition:plan_needed_attach:labels",
                               _plan_attach_labels)
        if "transition:plan_needed_attach" not in run.finalized_steps:
            run.finalized_steps.append("transition:plan_needed_attach")
            self.runs.store.save(run)
    elif outcome == "plan_needed":
        async def _plan_needed():
            await self.pmo.swap_labels(MissionRef(pmo_id, "issue"),
                                       remove=set(), add={LABEL_PLAN})
            self._audit(pmo_id, "label_add", LABEL_PLAN)
        await self._checkpoint(run, "transition:plan_needed", _plan_needed)
    elif outcome == "human_needed":
        # deliberate hand-off (docs/03 §4a, ADR-0007): the run finished
        # cleanly, so it never counts toward max_attempts; the stage label
        # stays so work resumes at the same step once the human removes
        # DEVCAKE-NEEDS-HUMAN. Checkpointed so redelivery never re-posts
        # the baton (ISSUES #5).
        async def _human():
            await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                       remove=set(), add={LABEL_NEEDS_HUMAN})
            if run.stage_label_at_dispatch is None and live.status == "in_progress":
                await self.pmo.set_status(MissionRef(pmo_id, run.pmo_kind), "backlog")
                self._audit(pmo_id, "set_status", "backlog (human hand-off)")
            nth = 1 + sum(
                1 for r in self.runs.store.all()
                if r.mission_pmo_id == pmo_id and self._run_is_ours(r) and r.state == "finished"
                and (r.result or {}).get("outcome") == "human_needed"
                and r.stage_label_at_dispatch == run.stage_label_at_dispatch)
            warn = "" if nth < 2 else (
                f"⚠️ **Hand-off #{nth} on this step.** If DevCake keeps returning "
                f"here, the mission may need re-scoping — add `DEVCAKE-SKIP` to "
                f"stop DevCake on it.\n\n")
            baton = (f"{warn}✋ **DevCake needs a human.** "
                     f"{result.get('summary', '(no details reported)')}\n\n"
                     f"When resolved, remove the `DEVCAKE-NEEDS-HUMAN` label and "
                     f"DevCake resumes where it left off.")
            await self._feed(pmo_id, run.pmo_kind, baton)
            if run.pmo_kind == "project":
                try:
                    await self.pmo.post_feed(
                        MissionRef(pmo_id, "project"),
                        redact(baton) + "\n\n" + COMMENT_SENTINEL)
                except Exception:
                    log.warning("project-update hand-off failed for %s — "
                                "summary lives in the audit log only",
                                run.mission_key, exc_info=True)
            self._audit(pmo_id, "devcake_needs_human",
                        f"#{nth}: " + (result.get("summary") or "")[:110])
        await self._checkpoint(run, "transition:human_needed", _human)
        run.verdict = f"handed off: needs human on {run.mission_type}"
        self.needs_human[pmo_id] = (
            f"{run.mission_key}: needs human on {run.mission_type}"
            + (f" — {live.url}" if live.url else ""))
    else:
        async def _unknown():
            await self.pmo.swap_labels(MissionRef(pmo_id, run.pmo_kind),
                                       remove=set(), add={LABEL_SKIP})
            await self._feed(
                pmo_id, run.pmo_kind,
                f"ℹ️ DevCake received unknown outcome `{outcome}` — parked with "
                f"`DEVCAKE-SKIP` for a human to inspect.")
            self._audit(pmo_id, "label_add",
                        f"{LABEL_SKIP} (unknown outcome {outcome})")
        await self._checkpoint(run, "transition:unknown_park", _unknown)
        run.verdict = redact(
            f"rejected: unknown outcome {outcome} — parked with DEVCAKE-SKIP")

