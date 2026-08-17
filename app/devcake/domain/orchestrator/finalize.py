"""Run finalization core: checkpoint, transcript, token report, restore (docs/04 §4)."""

from __future__ import annotations

import logging

from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from ...security import redact, redact_value
from .. import backend_health, costing, failure_taxonomy
from ..model import MissionRef
from ..run import Run, utcnow
from . import discovery, steps, transitions
from .feed import blockquote, post_attachment_comment, stage_of
from .markers import FEED_INLINE_MAX, REPLY_MARKER

log = logging.getLogger("devcake.missions")
tracer = trace.get_tracer("devcake")


def _pre_wipe(mgr, run: Run) -> bool:
    """True when run is not stamped for the store's current wipe generation.

    After any clear-runs in this process, only an exact store_gen match is
    current — prior-process stamps (store_gen > wipe_generation) used to
    slip past a strict-less-than check and resurrect wiped records.
    """
    store = mgr.runs.store
    check = getattr(store, "is_current_generation", None)
    if callable(check):
        return not check(run)
    wipe_gen = int(getattr(store, "wipe_generation", 0) or 0)
    gen = int(getattr(run, "store_gen", 0) or 0)
    if wipe_gen <= 0:
        return False
    return gen != wipe_gen


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

        # 1 — transcript (idempotent via finalized_steps)
        if steps.TRANSCRIPT not in run.finalized_steps:
            if _pre_wipe(mgr, run):
                log.info("abort finalize (transcript) pre-wipe %s", run.run_id)
                return
            await _post_transcript(mgr, run, transcript,
                                        payload.get("last_message_md"))
            run.finalized_steps.append(steps.TRANSCRIPT)
            mgr.runs.store.save(run)

        # 1b — the answer as its own marked comment (ADR-0014 D2 quarantine
        # still applies). Marker first, so downstream feed consumers can find
        # the answer without parsing our transcript header or opening the zip.
        # REVIEW/`reviewed` is suppressed (see _post_reply) so a trailing
        # "LGTM" cannot displace the EXECUTE answer as the newest REPLY.
        await _checkpoint(mgr, run, steps.REPLY, lambda: _post_reply(
            mgr, run, payload.get("last_message_md"), outcome,
        ))

        if _pre_wipe(mgr, run):
            log.info("abort finalize mid-flight pre-wipe %s", run.run_id)
            return

        run.token_report = redact_value(token_report)  # persisted cost source
        # 2 — token report (INV-5: always)
        if steps.TOKEN_REPORT not in run.finalized_steps:
            await mgr._feed(pmo_id, run.pmo_kind,
                             _token_report_md(run, token_report,
                                              mgr.config.cost_inputs))
            if _pre_wipe(mgr, run):
                log.info("abort finalize after token_report pre-wipe %s",
                         run.run_id)
                return
            run.finalized_steps.append(steps.TOKEN_REPORT)
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

        # 2b — ADR-0033 harvest: unconditional memorialization of the run's
        # discoveries (Decision 11), BEFORE the transition so even an outcome
        # the transition parks or rejects keeps its receipts. Best-effort
        # inside — never wedges the close.
        harvested = await discovery.harvest(mgr, run, result)
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
    # latch only THIS run's repo (M10): a bad credential on repo A
    # must never stop repo B's missions
    mgr.forges.latch(
        run.repo_ref, f"repository credential rejected in {run.run_id}")
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
    await post_attachment_comment(mgr, run.mission_pmo_id, "issue",
                                  filename=name, content=transcript,
                                  comment_of=_comment)
    mgr._audit(run.mission_pmo_id, "transcript", name)


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

