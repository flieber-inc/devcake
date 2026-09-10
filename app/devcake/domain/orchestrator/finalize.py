"""Run finalization core: checkpoint, transcript, token report, restore (docs/04 §4)."""

from __future__ import annotations

import logging
import re

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...security import redact, redact_value
from .. import backend_health, costing, failure_taxonomy
from ..model import MissionRef
from ..run import Run, is_pre_wipe, utcnow
from . import activity_payload as activity_payload_mod
from . import discovery, status_comment, steps, transitions
from .feed import (SECTION_ANSWER, SECTION_DISCOVERIES, SECTION_RUN,
                   SECTION_TOKEN_REPORT, SECTION_TRANSITION, FoldSection,
                   StepCardParts, blockquote, collapsible_of,
                   post_attachment_comment, post_attachments_comment,
                   render_step_card, stage_of)
from .markers import FEED_INLINE_MAX, REPLY_MARKER, answer_token
from ...activity import IN_FLIGHT

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")

# Entrypoint contract (images/common/dev_entrypoint.py): strict memory clone
# failures prefix the detail with this shape before the clone stderr.
_NOTEBOOK_CLONE_FAILED = re.compile(
    r"^memory notebook (\S+) clone failed(?:\b|:)")


def notebook_card_from_forge_auth_detail(
        detail: str | None,
        memory_mounts: list[dict] | None = None) -> str | None:
    """Card name to latch for notebook-clone DEV_FORGE_AUTH, else None.

    When ``memory_mounts`` is non-empty, the parsed card must appear there
    (dispatch snapshot) — otherwise fall through to the work-repo latch.
    """
    m = _NOTEBOOK_CLONE_FAILED.match(detail or "")
    if not m:
        return None
    card = m.group(1)
    mounts = memory_mounts or []
    if mounts:
        names = {str(x.get("card") or "") for x in mounts}
        if card not in names:
            return None
    return card


def _pre_wipe(mgr, run: Run) -> bool:
    """True when run is not stamped for the store's current wipe generation.

    After any clear-runs in this process, only an exact store_gen match is
    current — prior-process stamps (store_gen > wipe_generation) used to
    slip past a strict-less-than check and resurrect wiped records.
    Shared with RunManager via ``domain.run.is_pre_wipe``.
    """
    return is_pre_wipe(mgr.runs.store, run)


async def _checkpoint(mgr, run: Run, key: str, fn) -> None:
    """Idempotent finalize sub-step: skip if already done; append+save only
    after the side effect succeeds.

    ``fn`` must be a zero-arg async callable (not a pre-created coroutine),
    so redelivery does not construct unawaited coroutines.
    """
    if key in run.finalized_steps:
        return
    if _pre_wipe(mgr, run):
        return
    await fn()
    if _pre_wipe(mgr, run):
        return
    run.finalized_steps.append(key)
    mgr.runs.store.save(run)


FINALIZE_EXPECT_S = 180     # incl. one governed wait for tracker quota


async def finalize(mgr, run: Run, payload: dict) -> None:
    """Close one run (docs/04 §4). The in-flight phase wraps the whole verb —
    result handling, harvest, transition, replies — so the status bar shows
    "finalizing R" including any tracker-quota wait inside."""
    with IN_FLIGHT.phase("run.finalize", run.run_id, expect_s=FINALIZE_EXPECT_S,
                         instance=getattr(mgr, "instance_name", "")):
        return await _finalize(mgr, run, payload)


async def _finalize(mgr, run: Run, payload: dict) -> None:
    # Clear-runs wipe generation (docs/10): a run stamped before the last
    # wipe must not post to the PMO or resurrect local records. Saves are
    # already no-ops at RunStore; re-check after every await so a wipe that
    # lands mid-finalize stops further feed/transition side effects.
    if _pre_wipe(mgr, run):
        log.info("skip mission finalize for pre-wipe run %s", run.run_id)
        return

    result = payload.get("result") or {}
    outcome = result.get("outcome", "")
    transcript = payload.get("transcript_md", "")
    # ADR-0021: stamp the app-side rate-card estimate (cost_usd_estimated +
    # rate_card_id) before OTel/feed/persist all read the same dict. The
    # harness never estimates; native cost_usd_native is never touched.
    token_report = costing.stamp_estimate(
        payload.get("token_report") or {}, mgr.config.cost_inputs)
    plan_md = payload.get("plan_md")
    pmo_id = run.mission_pmo_id
    # ADR-0022 — stamped before the span so success AND failure branches
    # persist it; container-authored, so parsed defensively
    try:
        run.continuations_used = int(payload.get("continuations_used") or 0)
    except (TypeError, ValueError):
        run.continuations_used = 0
    # Routing-race fix (ADR-0033): the harvest (2b) posts the discovery
    # marker BEFORE the close used to write `result` onto the record, and
    # the sweep / steward read the RECORD — a poll-cycle sweep landing in
    # that window read "no result" as "cleared" and closed the batch for
    # good with a to=- receipt. Persist the result first; the failure
    # branches below still overwrite it (None on a failed run).
    if outcome:
        run.result = redact_value(result)
        mgr.runs.store.save(run)

    ctx = None
    if run.traceparent:
        from opentelemetry.propagate import extract
        ctx = extract({"traceparent": run.traceparent})
    with tracer.start_as_current_span("run.finalize", context=ctx,
                                      kind=SpanKind.CONSUMER) as span:
        span.set_attribute("devcake.run.id", run.run_id)
        span.set_attribute("devcake.outcome", outcome)
        for k in ("input_tokens", "output_tokens", "total_tokens",
                  "cache_read_tokens", "cache_write_tokens",
                  "reasoning_tokens"):
            if token_report.get(k) is not None:
                span.set_attribute(f"devcake.tokens.{k.removesuffix('_tokens')}",
                                   token_report[k])
        # devcake.cost.usd keeps its NAME (docs/12 contract) over the v1 key:
        # it still means "billed as reported by the harness"
        if token_report.get("cost_usd_native") is not None:
            span.set_attribute("devcake.cost.usd",
                               token_report["cost_usd_native"])
        # ADR-0021: the estimate rides its OWN attribute — devcake.cost.usd
        # keeps meaning "billed as reported by the harness", never a guess
        if token_report.get("cost_usd_estimated") is not None:
            span.set_attribute("devcake.cost.usd_estimated",
                               token_report["cost_usd_estimated"])
            span.set_attribute("devcake.cost.rate_card",
                               str(token_report.get("rate_card_id")))
        if run.continuations_used:                       # ADR-0022
            span.set_attribute("devcake.continuations", run.continuations_used)

        run.token_report = redact_value(token_report)  # persisted cost source
        # ADR-0033 harvest, the pure half: the entries the card memorializes
        # (Decision 11: unconditional, even for an outcome the transition
        # parks or rejects). Bookkeeping commits after the card is on the
        # feed. Never for a failed run (no outcome).
        harvest_part = (discovery.harvest_part(mgr, run, result)
                        if outcome else None)
        legacy_tail = (steps.TRANSCRIPT in run.finalized_steps
                       and steps.STEP_CARD not in run.finalized_steps)

        # 1 — the step card (ADR-0042 §2): transcript + answer + token report
        # + harvest + the transition's PR line in ONE comment, idempotent via
        # finalized_steps. The legacy keys are stamped with it so every
        # reader of them still sees the step as posted.
        if not legacy_tail and steps.STEP_CARD not in run.finalized_steps:
            if _pre_wipe(mgr, run):
                log.info("abort finalize (step card) pre-wipe %s", run.run_id)
                return
            anchor = await _post_step_card(
                mgr, run, transcript, payload.get("last_message_md"),
                token_report, harvest_part, result, outcome,
                exit_code=payload.get("exit_code"))
            # the card's entry id: late bookkeeping (routing receipts) is
            # appended to its fold; saved with the checkpoint
            run.feed_anchor = anchor or ""
            run.finalized_steps += [steps.STEP_CARD, steps.TRANSCRIPT,
                                    steps.REPLY, steps.TOKEN_REPORT]
            mgr.runs.store.save(run)
        elif legacy_tail:
            # a run whose transcript was posted by the pre-card build:
            # finish it in the old shape (answer comment, token report
            # threaded under the transcript; the harvest below likewise)
            await _legacy_tail(mgr, run, payload, token_report, outcome)

        if _pre_wipe(mgr, run):
            log.info("abort finalize mid-flight pre-wipe %s", run.run_id)
            return

        # failure artifact (docs/07 §4 nonzero exits): evidence posted above,
        # NO transition — and the dispatch-time status write is REVERTED so the
        # mission re-derives exactly as before the attempt (INV-3; without this,
        # a failed first ONBOARD strands the mission at in_progress/no-label = row 9)
        if not outcome:
            if _pre_wipe(mgr, run):
                return
            exit_code = payload.get("exit_code")
            await mgr.messaging.delete_run_user(run.run_id)
            await mgr.messaging.delete_reply_stream(run.run_id)
            run.result = None
            run.state = "failed"
            run.error = dev_failure_error(mgr, run, payload)
            run.ended_at = utcnow()
            mgr.runs.store.save(run)
            span.set_attribute("devcake.verdict", f"failed: {run.error}")
            span.set_status(Status(StatusCode.ERROR, run.error))
            if not _pre_wipe(mgr, run):
                await restore_after_failure(mgr, run)
            log.warning("run %s failed (exit %s, attempt %d)",
                        run.run_id, exit_code, run.attempt_of_step)
            await status_comment.refresh(mgr, pmo_id, reason="failed", run=run)
            await activity_payload_mod.record_activity(
                mgr, pmo_id, run.pmo_kind, run.mission_key, "failed")
            return

        # 2b — ADR-0033 harvest bookkeeping (label, pending set, routing
        # trigger, claims), BEFORE the transition so even an outcome the
        # transition parks or rejects keeps its receipts. The marker is
        # already on the feed inside the card; a legacy-tail run posts the
        # pre-card harvest comment here instead. Best-effort inside —
        # never wedges the close.
        if legacy_tail:
            harvested = await discovery.harvest(mgr, run, result)
        elif harvest_part is not None:
            harvested = await discovery.harvest_commit(mgr, run, harvest_part)
        else:
            harvested = 0
        if harvested:
            span.set_attribute("devcake.discoveries.harvested", harvested)

        # 3 — compare-and-transition. A ValueError from a transition means
        # the Dev's payload was structurally invalid (e.g. malformed
        # decomposition / bad blocked_by) — that is DEV_BAD_OUTPUT, a
        # counted attempt (docs/15 §2), NOT an exception to propagate: the
        # run must fail cleanly so the mission reschedules next cycle
        # instead of stranding in `finalizing` until the watchdog timeout.
        if steps.TRANSITION not in run.finalized_steps:
            if _pre_wipe(mgr, run):
                return
            try:
                await transitions.transition(mgr, run, result, plan_md)
            except ValueError as e:
                await mgr.messaging.delete_run_user(run.run_id)
                await mgr.messaging.delete_reply_stream(run.run_id)
                run.result = redact_value(result)
                run.state = "failed"
                run.error = redact(f"{failure_taxonomy.DEV_BAD_OUTPUT}: {e}")
                # ADR-0018: this path bypasses dev_failure_error, so it stamps
                # its own class. It matters more here than elsewhere — `e` can
                # embed Dev-authored text verbatim (decomposition.py raises with
                # the Dev's blocked_by list), which is exactly the injection the
                # structured field exists to defeat.
                run.error_class = failure_taxonomy.DEV_BAD_OUTPUT
                run.ended_at = utcnow()
                mgr.runs.store.save(run)
                span.set_attribute("devcake.verdict", f"failed: {run.error}")
                span.set_status(Status(StatusCode.ERROR, run.error))
                if not _pre_wipe(mgr, run):
                    await restore_after_failure(mgr, run)
                log.warning("run %s failed with DEV_BAD_OUTPUT: %s",
                            run.run_id, e)
                await status_comment.refresh(mgr, pmo_id, reason="bad_output",
                                             run=run)
                await activity_payload_mod.record_activity(
                    mgr, pmo_id, run.pmo_kind, run.mission_key, "bad_output")
                return
            if _pre_wipe(mgr, run):
                return
            run.finalized_steps.append(steps.TRANSITION)
            mgr.runs.store.save(run)

        await mgr.messaging.delete_run_user(run.run_id)
        await mgr.messaging.delete_reply_stream(run.run_id)
        run.result = redact_value(result)
        run.state, run.ended_at = "finished", utcnow()
        mgr.runs.store.save(run)
        # app-level judgment onto the trace: Dagu can report the step green
        # while _transition refused to act — make that visible in OO
        span.set_attribute("devcake.verdict", run.verdict or "success")
        if run.verdict and not run.verdict.startswith("handed off"):
            span.set_status(Status(StatusCode.ERROR, run.verdict))
            span.add_event("devcake.verdict", {"detail": run.verdict})
        log.info("finalized %s (%s)", run.run_id, outcome)
        # ADR-0042 §5 — the LAST statement of the close: the status comment
        # says what the record now says (best-effort, never a gate)
        await status_comment.refresh(mgr, pmo_id, reason="finalize", run=run)
        # ADR-0043 §1 — then the record: the activity repository holds what
        # the feed now holds (best-effort, never a gate)
        await activity_payload_mod.record_activity(
            mgr, pmo_id, run.pmo_kind, run.mission_key, "finalize")


def dev_failure_error(mgr, run: Run, payload: dict) -> str:
    """Classify a Dev failure artifact into `run.error`, and stamp the
    STRUCTURED `run.error_class` / `run.attempt_counted` (ADR-0018).

    ADR-0027: which row an exit code resolves to — including the exit-13
    structured/bare split — comes from `failure_taxonomy.classify`; only the
    genuinely behavioral arms (breaker trips, the correlated-excusal
    accounting) live here, keyed on the row. Adding a code/class is a table
    row plus, at most, a handler.

    Every row stamps a class. Stamping only the new ones would leave 12/13/14
    at `error_class == ""` post-upgrade, dropping them into the legacy
    `error`-prefix branch of `attempt_number` — where `DEV_FORGE` matches
    nothing and keeps counting, making `UNCOUNTED_CLASSES` dead code.
    """
    # public: part of the RunFinalizer port (reconcile enriches pre-harness orphans)
    exit_code = payload.get("exit_code")
    detail = redact(str(payload.get("error_detail") or ""))
    detail = " ".join(detail.split())[:500]
    structured = str(payload.get("error_class") or "")
    row = failure_taxonomy.classify(exit_code, structured)
    if row is None:
        run.error_class = run.error_class or failure_taxonomy.DEV_CRASH
        return f"dev failure artifact (exit {exit_code})"
    run.error_class = row.error_class
    return _ROW_HANDLERS.get(row.error_class, _detail_only)(
        mgr, run, row, detail, structured, exit_code)


def _detail_only(mgr, run, row, detail, structured, exit_code):
    """Rows with no behavior beyond the stamp (14 MCP setup; 16 turn budget —
    deterministic by nature: retrying the same cap cannot help, so the table
    marks it always-counted and never brake evidence)."""
    return f"{row.error_class}: " + (detail or row.default_detail)


def _auth(mgr, run, row, detail, structured, exit_code):
    mgr._trip_breaker(run.dev_type, f"auth failure in {run.run_id}")
    return (f"{row.error_class} (does not count toward attempts; "
            "breaker tripped)")


def _forge_auth(mgr, run, row, detail, structured, exit_code):
    # The row is structured_only: ONLY the container's structured
    # classification reaches this handler — and with it the breaker latch AND
    # the unconditional exemption of UNCOUNTED_CLASSES. The pairing is the
    # whole safety argument (dispatch.py: "both latch a breaker … cannot
    # livelock"), so the class may never be stamped on evidence that latches
    # nothing: a bare "403"/"401" can be a push rate limit or an incidental
    # URL fragment, and a pre-taxonomy image sends no class at all — either
    # way the run would be uncounted, breaker-less and re-dispatched FOREVER.
    # Auth *wording* alone therefore falls through to the bounded DEV_FORGE
    # row (classify's bare-sibling rule; the detail still names it), which
    # terminates.
    #
    # Latch the failing card only (M10 / ADR-0035): primary clone or push
    # auth → run.repo_ref; strict memory-notebook clone auth → that notebook
    # card. Never latch the work repo for a notebook-prefix failure — healthy
    # work-repo probes would clear it while DEV_FORGE_AUTH stays uncounted.
    #
    # CAKE-118: key the latch by the credential field that path used.
    # Notebook/memory clones are read-preferred (token_ro or token); work
    # clone/push is the write path (token). Clear requires a probe of the
    # same field — a healthy write token must not clear a dead token_ro.
    notebook = notebook_card_from_forge_auth_detail(detail, run.memory_mounts)
    target = notebook or run.repo_ref
    if notebook is not None:
        inst = mgr.forges.instance(notebook)
        field = ("token_ro" if (inst is not None and inst.token_ro)
                 else "token")
    else:
        field = "token"
    mgr.forges.latch(
        target, f"repository credential rejected in {run.run_id}",
        credential_field=field)
    return f"{row.error_class}: " + (detail or row.default_detail)


def _forge(mgr, run, row, detail, structured, exit_code):
    # "forge-bounded": uncounted while the step has excusals — a forge outage
    # should not burn missions — but bounded, because plain exit 13 latches no
    # breaker and would otherwise re-dispatch forever on a permanent
    # misconfiguration. An orphaned or skew-dropped GENUINE credential failure
    # lands here too: a terminating path is the safe-by-construction
    # degradation.
    run.attempt_counted = not backend_health.excusals_left(
        mgr.runs.store.all(), run, error_class=row.error_class)
    return f"{row.error_class}: " + (detail or row.default_detail)


def _correlated_excusal(mgr, run, row, detail, structured, exit_code):
    """The ADR-0018 §4a accounting, shared by exits 15 and 11 — the rows
    differ only in table fields, which encode two deliberate asymmetries:

    * `excusal_requires_structured_class` (15 True, 11 False): for 15,
      correlation — and therefore excusing an attempt — requires the
      STRUCTURED class from the container. A reconcile-synthesized orphan
      payload carries the numeric code only: it earns the label and the
      evidence, and contributes to future correlation, but is never itself
      excused. That is the skew-safe direction. Exit 11 has no in-band
      structured class — the exit code IS the classification (app-side), so
      an orphan carries the same evidence value as a live finalize (ADR-0026).
    * `brake_evidence` (15 "always", 11 "opt-in"): the 11 arm runs only under
      `brake_on_bad_output`, widening the brake to a correlated fleet-wide
      bad-output cascade exactly like exit 15. Excusals bound the loop per
      step either way.
    """
    enabled = (row.brake_evidence == "always"
               or (row.brake_evidence == "opt-in"
                   and mgr.config.brake_on_bad_output))
    skew_ok = (not row.excusal_requires_structured_class
               or structured == row.error_class)
    if enabled and skew_ok:
        runs = mgr.runs.store.all()
        correlated = backend_health.backend_correlated(
            runs, run.dev_type,
            classes=backend_health.fault_classes(
                mgr.config.brake_on_bad_output))
        # Count when the failure is NOT correlated, OR when this step has
        # spent its excusals. The operator must be `or`: with `and`,
        # exhausting the budget would produce MORE excusing (inverting the
        # escape hatch) and a solitary failure on an exhausted step would
        # stop counting.
        run.attempt_counted = (
            correlated is None
            or not backend_health.excusals_left(
                runs, run, error_class=row.error_class))
        if correlated and not run.attempt_counted:
            return (f"{row.error_class} (correlated fleet failure; does not "
                    "count toward attempts): "
                    + (detail or row.default_detail))
    return f"{row.error_class}: " + (detail or row.default_detail)


def _crash(mgr, run, row, detail, structured, exit_code):
    return f"{row.error_class}: " + (
        detail or f"harness or entrypoint failure (exit {exit_code})")


_ROW_HANDLERS = {
    failure_taxonomy.DEV_AUTH: _auth,
    failure_taxonomy.DEV_FORGE_AUTH: _forge_auth,
    failure_taxonomy.DEV_FORGE: _forge,
    failure_taxonomy.DEV_HARNESS_FAULT: _correlated_excusal,
    failure_taxonomy.DEV_BAD_OUTPUT: _correlated_excusal,
    failure_taxonomy.DEV_CRASH: _crash,
}


async def restore_after_failure(mgr, run: Run) -> None:
    """Revert the dispatch-time backlog→in_progress write after a failed attempt,
    iff the mission is still exactly as we left it (live re-read; human edits win)."""
    if run.stage_label_at_dispatch is not None or not run.mission_pmo_id:
        return  # only ONBOARD dispatches from backlog change the status
    try:
        live = await mgr.pmo.get(MissionRef(run.mission_pmo_id, run.pmo_kind))
        if live.status == "in_progress" and stage_of(live) is None:
            await mgr.pmo.set_status(
                MissionRef(run.mission_pmo_id, run.pmo_kind), "backlog")
            mgr._audit(run.mission_pmo_id, "set_status",
                        "backlog (restored after failed attempt)")
    except Exception:
        log.exception("status restore failed for %s", run.run_id)


def _card_copy(mgr, run: Run, result: dict, outcome: str, *,
               harvest_part, exit_code) -> tuple[str, str, str, str]:
    """(glyph, outcome word, Result line, Next line) for the card head —
    DevCake-authored prose about the step, from the record it already
    holds; the docs/03 glyph vocabulary. Model-derived text (a PR url) is
    a link, never a marker: defanged and bounded."""
    from .markers import defang
    mtype = run.mission_type
    pr_url = defang(str(result.get("pr_url") or run.pr_url or "")).strip()[:500]
    if not outcome:
        code = f" (exit {exit_code})" if exit_code is not None else ""
        return ("⚠️", "failed", f"No result — the run failed{code}.",
                "DevCake retries under the attempts policy; a give-up posts "
                "a notice.")
    if outcome == "human_needed":
        return ("✋", "handed off", "Blocked on a person.",
                "You — see the notice below.")
    if mtype == "ONBOARD" and outcome == "plan_needed":
        if result.get("plan_md") or result.get("plan"):
            nxt = ("You — approve the plan (see below)."
                   if getattr(mgr.instance, "plan_approval", False)
                   else "DevCake — EXECUTE.")
            return ("📋", "triaged",
                    "Opportunistic plan attached (posted below).", nxt)
        return ("📋", "triaged", "Needs a plan.", "DevCake — PLAN.")
    if mtype == "ONBOARD" and outcome == "decomposed":
        n = len(result.get("decomposition") or [])
        return ("🧩", "decomposed", f"Split into {n} missions.",
                "DevCake creates them; this mission is canceled in their favour.")
    if mtype == "PLAN" and outcome == "planned":
        nxt = ("You — approve the plan (see below)."
               if getattr(mgr.instance, "plan_approval", False)
               else "DevCake — EXECUTE.")
        return ("📋", "planned", "Plan posted below.", nxt)
    if mtype == "EXECUTE" and outcome == "executed":
        where = f"Pull request {pr_url}." if pr_url else "Pull request opened."
        return ("🔀", "executed", where, "DevCake — REVIEW.")
    if mtype == "REVIEW" and outcome == "reviewed":
        verdict = str(result.get("verdict") or "").lower()
        if verdict == "approve":
            nxt = "DevCake merges."
            try:
                from ...config import auto_merge_permitted
                inst = mgr.forges.instance(run.repo_ref)
                if not (inst is not None and auto_merge_permitted(
                        mgr.config, inst, run.repo_ref, mgr.dev_types)):
                    nxt = (f"You — merge {pr_url} (the command is in the "
                           "notice below)." if pr_url else
                           "You — merge the pull request (see the notice below).")
                elif int(getattr(inst, "merge_settle_minutes", 0) or 0) > 0:
                    nxt = (f"DevCake waits {inst.merge_settle_minutes} min for "
                           "sibling discoveries, then merges.")
            except Exception:  # noqa: BLE001 — the head is prose; a config lookup must never fail a close
                pass
            return ("✅", "approved", "Verdict: approve.", nxt)
        if verdict == "reject":
            return ("🔁", "rejected", "Verdict: reject — report posted below.",
                    "DevCake — EXECUTE rework.")
        return ("⚠️", outcome, f"Verdict: {verdict or '(none)'}.",
                "You — see the notice below.")
    return ("⚠️", outcome, "Not a legal outcome for this step.",
            "You — see the notice below.")


def _answer_token_applies(run: Run, last_message: str | None,
                          outcome: str) -> bool:
    """The pre-card answer-comment rule (`_post_reply`), carried by the
    card's Answer section: issues only, a non-empty last message, never on
    REVIEW/`reviewed` (an approval note must not displace the EXECUTE
    answer as the newest one)."""
    if run.pmo_kind != "issue" or not (last_message or "").strip():
        return False
    return not (run.mission_type == "REVIEW" and outcome == "reviewed")


async def _post_step_card(mgr, run: Run, transcript: str,
                          last_message: str | None, token_report: dict,
                          harvest_part, result: dict, outcome: str, *,
                          exit_code=None) -> str | None:
    """ADR-0042 §2: ONE comment per step. Transcript attached (the file
    token on the head's Transcript line is the seq-derivation surface),
    the answer quoted and cut at a boundary, Result/Next, and the fold
    with the record: answer token, token report (docs/03 §8, text
    unchanged), the harvest (marker first), the transition's PR line, the
    run id. externalize=False always — counted markers ride the comment.
    Returns the card's entry id (the step's fold anchor), None for
    projects / vendors that return none."""
    transcript = redact(transcript)
    name = f"{run.seq}_{run.mission_type}.md"
    if run.pmo_kind == "project":
        await mgr._feed(run.mission_pmo_id, "project",
                         f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)"
                         f"\n\n---\n\n{transcript}")
        return None    # project updates have no comment feed (suppressed)
    lm = redact(last_message) if last_message else ""
    glyph, word, result_line, next_line = _card_copy(
        mgr, run, result, outcome, harvest_part=harvest_part,
        exit_code=exit_code)
    duration = None
    if token_report.get("duration_ms") is not None:
        duration = float(token_report["duration_ms"]) / 1000
    elif run.started_at is not None:
        duration = (utcnow() - run.started_at).total_seconds()
    cost = token_report.get("cost_usd_native")
    if cost is None:
        cost = token_report.get("cost_usd_estimated")
    files = [(name, transcript)]
    if harvest_part is not None:
        files.append((harvest_part.name, harvest_part.md))

    def _comment(urls):
        sections = []
        if _answer_token_applies(run, lm, outcome):
            sections.append(FoldSection(SECTION_ANSWER, answer_token(run.seq)))
        sections.append(FoldSection(SECTION_TOKEN_REPORT, _token_report_md(
            run, token_report, mgr.config.cost_inputs)))
        if harvest_part is not None:
            sections.append(FoldSection(SECTION_DISCOVERIES, harvest_part.section_body(
                run, urls.get(harvest_part.name))))
        if run.mission_type == "EXECUTE" and outcome == "executed":
            _f = mgr.forges.get(run.repo_ref)
            noun = _f.descriptor.pr_noun if _f else "pull request"
            sections.append(FoldSection(SECTION_TRANSITION, (
                f"🔀 DevCake opened/updated the {noun}: "
                f"{result.get('pr_url', '(no url reported)')} — awaiting REVIEW.")))
        sections.append(FoldSection(SECTION_RUN, f"`{run.run_id}`"))
        parts = StepCardParts(
            seq=run.seq, mission_type=run.mission_type, glyph=glyph,
            outcome_word=word, result_line=result_line, next_line=next_line,
            transcript_name=name, transcript_url=urls.get(name),
            sections=sections, answer_md=lm or None,
            transcript_md=transcript, duration_s=duration,
            cost_usd=float(cost) if cost is not None else None)
        return render_step_card(parts, collapsible=collapsible_of(mgr)), False

    try:
        anchor = await post_attachments_comment(
            mgr, run.mission_pmo_id, "issue", files=files, comment_of=_comment)
    except Exception as e:  # noqa: BLE001 — audited, then re-raised: a failed card is a failed close, like a failed transcript before it
        if harvest_part is not None:
            mgr._audit(run.mission_pmo_id, "discovery_post_failed", str(e)[:200])
        mgr._audit(run.mission_pmo_id, "step_card_failed", str(e)[:200])
        raise
    mgr._audit(run.mission_pmo_id, "transcript", name)
    return anchor


async def _legacy_tail(mgr, run: Run, payload: dict, token_report: dict,
                       outcome: str) -> None:
    """A run whose transcript was posted by the pre-card build finishes in
    the pre-card shape: the answer comment, then the token report threaded
    under the saved anchor (the harvest follows in `_finalize`). Deleted
    one release after the card ships."""
    await _checkpoint(mgr, run, steps.REPLY, lambda: _post_reply(
        mgr, run, payload.get("last_message_md"), outcome))
    if _pre_wipe(mgr, run):
        return
    if steps.TOKEN_REPORT not in run.finalized_steps:
        await mgr._feed(run.mission_pmo_id, run.pmo_kind,
                         _token_report_md(run, token_report,
                                          mgr.config.cost_inputs),
                         reply_to=run.feed_anchor or None)
        if _pre_wipe(mgr, run):
            return
        run.finalized_steps.append(steps.TOKEN_REPORT)
        mgr.runs.store.save(run)


async def _post_transcript(mgr, run: Run, transcript: str,
                           last_message: str | None = None) -> str | None:
    """ADR-0014 D1: attachment = full dump; comment = step line + the
    `>`-blockquoted last message. last_message missing/empty ⇒ the pointer-only
    comment (old-image payloads; never derived from the transcript).
    Returns the transcript comment's entry id (the step's thread anchor),
    None for projects / vendors that return none."""
    transcript = redact(transcript)
    name = f"{run.seq}_{run.mission_type}.md"
    if run.pmo_kind == "project":
        await mgr._feed(run.mission_pmo_id, "project",
                         f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)"
                         f"\n\n---\n\n{transcript}")
        return None    # project updates have no threads
    def _comment(url):
        if url is None:
            # INV-5: the transcript is always posted, even inline —
            # QUARANTINED (ADR-0014 D2): the dump is model text; only the
            # step-marker header line stays unquoted for seq derivation.
            # No attachment ⇒ the last message already rides inside the
            # quoted dump; externalization stays on as the size second-chance
            return (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)\n\n"
                    + blockquote(f"---\n\n{transcript}")), True
        # the backticked `{name}` must stay in the comment — STEP_MARKER
        # counts it for seq derivation (docs/02 §8)
        body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`) — "
                f"attached: [{name}]({url})")
        if not last_message:
            return body, True
        # redact BEFORE truncate+quote: truncation must never split a secret
        # across the boundary, and quoting must never break a multi-line
        # value's exact-match redaction (review 1.3-1.5 finding 1)
        lm = redact(last_message)
        if len(lm) > FEED_INLINE_MAX:
            lm = (lm[:FEED_INLINE_MAX]
                  + "\n\n… (truncated — full text in the attachment)")
        # quoting quarantines the model text from every feed scan; the
        # opt-out is safe because the full text already rides the attachment
        return body + "\n\n" + blockquote(lm), False

    # docs/05 §4: transcripts always live as attachments, never inline —
    # via the ONE attachment+comment pipe (ADR-0033 chokepoint ruling)
    anchor = await post_attachment_comment(mgr, run.mission_pmo_id, "issue",
                                           filename=name, content=transcript,
                                           comment_of=_comment)
    mgr._audit(run.mission_pmo_id, "transcript", name)
    return anchor


async def _post_reply(mgr, run: Run, last_message: str | None,
                      outcome: str = "") -> None:
    """The answer, marked so downstream feed consumers can find it.

    No last message (old-image payload) or an empty one ⇒ no comment: no
    content, no post — an empty marked answer is worse than none. Issues only —
    the contract is defined on the issue comment feed; project feeds are a
    different surface.

    REVIEW + ``reviewed`` is also a no-op: consumers take the newest REPLY as
    the mission's answer, and a short approve/reject "LGTM" would displace the
    EXECUTE answer. ``human_needed`` on REVIEW still posts — that text *is*
    the ask. Intermediate ONBOARD/PLAN/EXECUTE replies are intentional
    progressive posts consumers may use or ignore.
    """
    if run.pmo_kind != "issue" or not (last_message or "").strip():
        return
    if run.mission_type == "REVIEW" and outcome == "reviewed":
        return
    # redact BEFORE truncate, same rule as the transcript comment: a clipped
    # half-secret no longer matches its own pattern.
    body = redact(last_message)
    if len(body) > FEED_INLINE_MAX:
        # This comment has no attachment of its own; the full last message
        # lives in the step transcript on the issue. Never claim an
        # attachment exists.
        body = (body[:FEED_INLINE_MAX]
                + "\n\n… (truncated — full text in the step transcript "
                  "on this issue)")
    await mgr._feed(
        run.mission_pmo_id, "issue",
        f"{REPLY_MARKER}\n\n" + blockquote(body),
        externalize=False,
    )


def _token_report_md(run: Run, tr: dict, cost_inputs=None) -> str:
    """docs/03 §8 (normative format). The `run:` footer is the idempotency
    anchor — additions go ABOVE it and only appear when their datum exists,
    so pre-ADR-0021 reports render byte-identically."""
    def fmt(v):  # noqa: ANN001
        return "—" if v is None else v
    cost = tr.get("cost_usd_native")
    est = tr.get("cost_usd_estimated")
    # reasoning is informational (a subset of output, never priced) — a
    # first-class v1 scalar (ADR-0029; pre-v1 it hid in a `notes` regex)
    reasoning = (f" · reasoning: {tr['reasoning_tokens']}"
                 if tr.get("reasoning_tokens") is not None else "")
    # estimated line: fills the native gap by default; with override_native
    # on, it appears ALONGSIDE the native line (both shown — honest)
    show_est = est is not None and (
        cost is None
        or (cost_inputs is not None and cost_inputs.override_native))
    return (
        f"🧮 DevCake token report — step {run.seq} ({run.mission_type}, {run.dev_type})\n"
        f"model: {fmt(tr.get('model'))} · input: {fmt(tr.get('input_tokens'))} · "
        f"output: {fmt(tr.get('output_tokens'))}\n"
        f"cache read/write: {fmt(tr.get('cache_read_tokens'))}/"
        f"{fmt(tr.get('cache_write_tokens'))}"
        + (f" · total: {tr['total_tokens']}" if tr.get('total_tokens') is not None else "")
        + reasoning
        + (f"\ncost: ${cost:.4f}" if cost is not None else "")
        + (f"\ncost (estimated, {tr.get('rate_card_id')}): ${est:.4f}"
           if show_est else "")
        # ADR-0022: only when the loop fired — zero-continuation reports (and
        # every pre-ADR-0022 one) render byte-identically
        + (f"\ncontinuations: {run.continuations_used}"
           if run.continuations_used else "")
        + f"\nextraction: {fmt(tr.get('source'))}\nrun: {run.run_id}")

