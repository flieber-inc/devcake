"""Run finalization core: checkpoint, transcript, token report, restore (docs/04 §4)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...security import redact, redact_value
from ..model import MissionRef
from ..run import Run, utcnow

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


async def _checkpoint(self, run: Run, key: str, fn) -> None:
    """Idempotent finalize sub-step (ISSUES #4–6): skip if already done;
    append+save only after the side effect succeeds.

    ``fn`` must be a zero-arg async callable (not a pre-created coroutine),
    so redelivery does not construct unawaited coroutines.
    """
    if key in run.finalized_steps:
        return
    await fn()
    run.finalized_steps.append(key)
    self.runs.store.save(run)


async def finalize(self, run: Run, payload: dict) -> None:
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
            await self._post_transcript(run, transcript)
            run.finalized_steps.append("transcript")
            self.runs.store.save(run)

        run.token_report = redact_value(token_report)  # persisted cost source
        # 2 — token report (INV-5: always)
        if "token_report" not in run.finalized_steps:
            await self._feed(pmo_id, run.pmo_kind,
                             self._token_report_md(run, token_report))
            run.finalized_steps.append("token_report")
            self.runs.store.save(run)

        # failure artifact (docs/07 §4 nonzero exits): evidence posted above,
        # NO transition — and the dispatch-time status write is REVERTED so the
        # mission re-derives exactly as before the attempt (INV-3; without this,
        # a failed first ONBOARD strands the mission at in_progress/no-label = row 9)
        if not outcome:
            exit_code = payload.get("exit_code")
            await self.messaging.delete_run_user(run.run_id)
            await self.messaging.delete_reply_stream(run.run_id)
            run.result = None
            run.state = "failed"
            run.error = self.dev_failure_error(run, payload)
            run.ended_at = utcnow()
            self.runs.store.save(run)
            span.set_attribute("devcake.verdict", f"failed: {run.error}")
            span.set_status(Status(StatusCode.ERROR, run.error))
            await self.restore_after_failure(run)
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
            try:
                await self._transition(run, result, plan_md)
            except ValueError as e:
                await self.messaging.delete_run_user(run.run_id)
                await self.messaging.delete_reply_stream(run.run_id)
                run.result = redact_value(result)
                run.state = "failed"
                run.error = redact(f"DEV_BAD_OUTPUT: {e}")
                run.ended_at = utcnow()
                self.runs.store.save(run)
                span.set_attribute("devcake.verdict", f"failed: {run.error}")
                span.set_status(Status(StatusCode.ERROR, run.error))
                await self.restore_after_failure(run)
                log.warning("run %s failed with DEV_BAD_OUTPUT: %s",
                            run.run_id, e)
                return
            run.finalized_steps.append("transition")
            self.runs.store.save(run)

        await self.messaging.delete_run_user(run.run_id)
        await self.messaging.delete_reply_stream(run.run_id)
        run.result = redact_value(result)
        run.state, run.ended_at = "finished", utcnow()
        self.runs.store.save(run)
        # app-level judgment onto the trace: Dagu can report the step green
        # while _transition refused to act — make that visible in OO
        span.set_attribute("devcake.verdict", run.verdict or "success")
        if run.verdict and not run.verdict.startswith("handed off"):
            span.set_status(Status(StatusCode.ERROR, run.verdict))
            span.add_event("devcake.verdict", {"detail": run.verdict})
        log.info("finalized %s (%s)", run.run_id, outcome)


def dev_failure_error(self, run: Run, payload: dict) -> str:
    # public: part of the RunFinalizer port (reconcile enriches exit-13 orphans)
    exit_code = payload.get("exit_code")
    if exit_code == 12:
        self._trip_breaker(run.dev_type, f"auth failure in {run.run_id}")
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
            self.forges.latch(
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


async def restore_after_failure(self, run: Run) -> None:
    """Revert the dispatch-time backlog→in_progress write after a failed attempt,
    iff the mission is still exactly as we left it (live re-read; human edits win)."""
    if run.stage_label_at_dispatch is not None or not run.mission_pmo_id:
        return  # only ONBOARD dispatches from backlog change the status
    try:
        live = await self.pmo.get(MissionRef(run.mission_pmo_id, run.pmo_kind))
        if live.status == "in_progress" and self._stage_of(live) is None:
            await self.pmo.set_status(
                MissionRef(run.mission_pmo_id, run.pmo_kind), "backlog")
            self._audit(run.mission_pmo_id, "set_status",
                        "backlog (restored after failed attempt)")
    except Exception:
        log.exception("status restore failed for %s", run.run_id)


async def _post_transcript(self, run: Run, transcript: str) -> None:
    transcript = redact(transcript)
    name = f"{run.seq}_{run.mission_type}.md"
    body = f"🧾 DevCake transcript `{name}` (run `{run.run_id}`)\n\n---\n\n{transcript}"
    if run.pmo_kind == "project":
        await self._feed(run.mission_pmo_id, "project", body)
        return
    try:  # docs/05 §4: transcripts always live as attachments, never inline
        url = await self.pmo.upload_attachment(run.mission_pmo_id, name,
                                               transcript.encode())
        # the backticked `{name}` must stay in the comment — STEP_MARKER
        # counts it for seq derivation (docs/02 §8)
        body = (f"🧾 DevCake transcript `{name}` (run `{run.run_id}`) — "
                f"attached: [{name}]({url})")
    except Exception:  # INV-5: the transcript is always posted, even inline
        log.exception("transcript upload failed — posting inline")
    await self._feed(run.mission_pmo_id, "issue", body)
    self._audit(run.mission_pmo_id, "transcript", name)


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

