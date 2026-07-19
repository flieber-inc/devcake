"""Run finalization core: checkpoint, transcript, token report, restore (docs/04 §4)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...security import redact, redact_value
from ..model import MissionRef
from ..run import Run, utcnow
from . import transitions
from .feed import _blockquote, _stage_of
from .markers import FEED_INLINE_MAX

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _pre_wipe(mgr, run: Run) -> bool:
    """True when run.store_gen predates the store's last clear-runs wipe."""
    wipe_gen = int(getattr(mgr.runs.store, "wipe_generation", 0) or 0)
    return int(getattr(run, "store_gen", 0) or 0) < wipe_gen


async def _checkpoint(mgr, run: Run, key: str, fn) -> None:
    """Idempotent finalize sub-step (ISSUES #4–6): skip if already done;
    append+save only after the side effect succeeds.

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


async def finalize(mgr, run: Run, payload: dict) -> None:
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
    token_report = payload.get("token_report") or {}
    plan_md = payload.get("plan_md")
    pmo_id = run.mission_pmo_id

    ctx = None
    if run.traceparent:
        from opentelemetry.propagate import extract
        ctx = extract({"traceparent": run.traceparent})
    with tracer.start_as_current_span("run.finalize", context=ctx,
                                      kind=SpanKind.CONSUMER) as span:
        span.set_attribute("devcake.run.id", run.run_id)
        span.set_attribute("devcake.outcome", outcome)
        for k in ("input_tokens", "output_tokens", "total_tokens"):
            if token_report.get(k) is not None:
                span.set_attribute(f"devcake.tokens.{k.removesuffix('_tokens')}",
                                   token_report[k])
        if token_report.get("cost_usd") is not None:
            span.set_attribute("devcake.cost.usd", token_report["cost_usd"])

        # 1 — transcript (idempotent via finalized_steps)
        if "transcript" not in run.finalized_steps:
            if _pre_wipe(mgr, run):
                log.info("abort finalize (transcript) pre-wipe %s", run.run_id)
                return
            await _post_transcript(mgr, run, transcript,
                                        payload.get("last_message_md"))
            run.finalized_steps.append("transcript")
            mgr.runs.store.save(run)

        if _pre_wipe(mgr, run):
            log.info("abort finalize mid-flight pre-wipe %s", run.run_id)
            return

        run.token_report = redact_value(token_report)  # persisted cost source
        # 2 — token report (INV-5: always)
        if "token_report" not in run.finalized_steps:
            await mgr._feed(pmo_id, run.pmo_kind,
                             _token_report_md(run, token_report))
            if _pre_wipe(mgr, run):
                log.info("abort finalize after token_report pre-wipe %s",
                         run.run_id)
                return
            run.finalized_steps.append("token_report")
            mgr.runs.store.save(run)

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
            return

        # 3 — compare-and-transition. A ValueError from a transition means
        # the Dev's payload was structurally invalid (e.g. malformed
        # decomposition / bad blocked_by) — that is DEV_BAD_OUTPUT, a
        # counted attempt (docs/15 §2), NOT an exception to propagate: the
        # run must fail cleanly so the mission reschedules next cycle
        # instead of stranding in `finalizing` until the watchdog timeout.
        if "transition" not in run.finalized_steps:
            if _pre_wipe(mgr, run):
                return
            try:
                await transitions.transition(mgr, run, result, plan_md)
            except ValueError as e:
                await mgr.messaging.delete_run_user(run.run_id)
                await mgr.messaging.delete_reply_stream(run.run_id)
                run.result = redact_value(result)
                run.state = "failed"
                run.error = redact(f"DEV_BAD_OUTPUT: {e}")
                run.ended_at = utcnow()
                mgr.runs.store.save(run)
                span.set_attribute("devcake.verdict", f"failed: {run.error}")
                span.set_status(Status(StatusCode.ERROR, run.error))
                if not _pre_wipe(mgr, run):
                    await restore_after_failure(mgr, run)
                log.warning("run %s failed with DEV_BAD_OUTPUT: %s",
                            run.run_id, e)
                return
            if _pre_wipe(mgr, run):
                return
            run.finalized_steps.append("transition")
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


def dev_failure_error(mgr, run: Run, payload: dict) -> str:
    # public: part of the RunFinalizer port (reconcile enriches exit-13 orphans)
    exit_code = payload.get("exit_code")
    if exit_code == 12:
        mgr._trip_breaker(run.dev_type, f"auth failure in {run.run_id}")
        return "DEV_AUTH (does not count toward attempts; breaker tripped)"
    detail = redact(str(payload.get("error_detail") or ""))
    detail = " ".join(detail.split())[:500]
    error_class = str(payload.get("error_class") or "")
    auth_markers = ("403", "401", "authentication failed", "repository not found",
                    "write access to repository not granted")
    if exit_code == 13 and (error_class == "DEV_FORGE_AUTH"
                            or any(marker in detail.lower()
                                   for marker in auth_markers)):
        # only the structured Dev classification may latch the global
        # breaker — a bare "403"/"401" substring can be a rate limit or an
        # incidental URL fragment; the poll-loop re-probe clears mistakes
        if error_class == "DEV_FORGE_AUTH":
            # latch only THIS run's repo (M10): a bad credential on repo A
            # must never stop repo B's missions
            mgr.forges.latch(
                run.repo_ref, f"repository credential rejected in {run.run_id}")
        return "DEV_FORGE_AUTH: " + (detail or "repository credential rejected")
    if exit_code == 13:
        return "DEV_FORGE: " + (detail or "clone/push setup failed")
    if exit_code == 14:
        # operator-configured MCP setup failed in-container (docs/15 §1,
        # counted attempt): the detail names the command + stderr tail so
        # the fix is obvious from the Runs page
        return "DEV_MCP_SETUP: " + (detail or "MCP setup command failed")
    return f"dev failure artifact (exit {exit_code})"


async def restore_after_failure(mgr, run: Run) -> None:
    """Revert the dispatch-time backlog→in_progress write after a failed attempt,
    iff the mission is still exactly as we left it (live re-read; human edits win)."""
    if run.stage_label_at_dispatch is not None or not run.mission_pmo_id:
        return  # only ONBOARD dispatches from backlog change the status
    try:
        live = await mgr.pmo.get(MissionRef(run.mission_pmo_id, run.pmo_kind))
        if live.status == "in_progress" and _stage_of(live) is None:
            await mgr.pmo.set_status(
                MissionRef(run.mission_pmo_id, run.pmo_kind), "backlog")
            mgr._audit(run.mission_pmo_id, "set_status",
                        "backlog (restored after failed attempt)")
    except Exception:
        log.exception("status restore failed for %s", run.run_id)


async def _post_transcript(mgr, run: Run, transcript: str,
                           last_message: str | None = None) -> None:
    """ADR-0014 D1: attachment = full dump; comment = step line + the
    `>`-blockquoted last message. last_message missing/empty ⇒ the pointer-only
    comment (old-image payloads; never derived from the transcript)."""
    transcript = redact(transcript)
    name = f"{run.seq}_{run.mission_type}.md"
    if run.pmo_kind == "project":
        await mgr._feed(run.mission_pmo_id, "project",
                         f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)"
                         f"\n\n---\n\n{transcript}")
        return
    attached = False
    try:  # docs/05 §4: transcripts always live as attachments, never inline
        url = await mgr.pmo.upload_attachment(run.mission_pmo_id, name,
                                               transcript.encode())
        # the backticked `{name}` must stay in the comment — STEP_MARKER
        # counts it for seq derivation (docs/02 §8)
        body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`) — "
                f"attached: [{name}]({url})")
        attached = True
    except Exception:  # INV-5: the transcript is always posted, even inline —
        # QUARANTINED (ADR-0014 D2): the dump is model text; only the
        # step-marker header line stays unquoted for seq derivation
        log.exception("transcript upload failed — posting inline (quoted)")
        body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)\n\n"
                + _blockquote(f"---\n\n{transcript}"))
    if last_message and attached:
        # redact BEFORE truncate+quote: truncation must never split a secret
        # across the boundary, and quoting must never break a multi-line
        # value's exact-match redaction (review 1.3-1.5 finding 1)
        last_message = redact(last_message)
        if len(last_message) > FEED_INLINE_MAX:
            last_message = (last_message[:FEED_INLINE_MAX]
                            + "\n\n… (truncated — full text in the attachment)")
        # quoting quarantines the model text from every feed scan; the
        # opt-out is safe because the full text already rides the attachment
        body += "\n\n" + _blockquote(last_message)
        await mgr._feed(run.mission_pmo_id, "issue", body, externalize=False)
    else:
        # no attachment ⇒ the last message already rides inside the (quoted)
        # inline dump; externalization stays on as the size second-chance
        await mgr._feed(run.mission_pmo_id, "issue", body)
    mgr._audit(run.mission_pmo_id, "transcript", name)


def _token_report_md(run: Run, tr: dict) -> str:
    def fmt(v):  # noqa: ANN001
        return "—" if v is None else v
    cost = tr.get("cost_usd")
    return (
        f"🧮 DevCake token report — step {run.seq} ({run.mission_type}, {run.dev_type})\n"
        f"model: {fmt(tr.get('model'))} · input: {fmt(tr.get('input_tokens'))} · "
        f"output: {fmt(tr.get('output_tokens'))}\n"
        f"cache read/write: {fmt(tr.get('cache_read_tokens'))}/"
        f"{fmt(tr.get('cache_write_tokens'))}"
        + (f" · total: {tr['total_tokens']}" if tr.get('total_tokens') is not None else "")
        + (f"\ncost: ${cost:.4f}" if cost is not None else "")
        + f"\nextraction: {fmt(tr.get('extraction_method'))}\nrun: {run.run_id}")

