"""Run lifecycle shared by every dispatch path (docs/04 §3.1, docs/09):
run-spec service over runspec.get, envelope auth, ACL lifecycle, trace
continuity, watchdog kill, and the finalization checklist. Also hosts the
hello stub dispatch — the permanent debug/CI fixture (scripts/ci_suite.sh).

Dispatch spine (ACL → digest → durable save → executor.start) lives in
RunBootstrap; this module owns ingress, kill, and hello-specific fields.
"""

from __future__ import annotations

import hmac
import logging
import os

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.propagate import extract, inject

from ..ports.executor import ExecutorPort
from ..ports.finalizer import RunFinalizer
from ..ports.messaging import MessagingPort
from ..ports.state import StatePort
from ..telemetry import OTEL_COLLECTOR_URL
from .ids import make_run_id
from .run import Run, auth_digest, utcnow
from .run_bootstrap import RunBootstrap

log = logging.getLogger("devcake.runs")
tracer = trace.get_tracer("devcake")

HELLO_IMAGE = os.environ.get(
    "DEVCAKE_HELLO_IMAGE",
    f"devcake/dev-hello:{os.environ.get('DEVCAKE_TAG', 'latest')}")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("DEVCAKE_DEV_TIMEOUT_MINUTES", "120")) * 60


RUN_FAILURES_STREAM = "run_failures"


def failure_record(run: "Run", outcome: str, reason: str,
                   errors: list[dict[str, str]]) -> dict:
    """OO log record for a run the executor lost (docs/12 §6). `detail` carries
    the Dev container's dying words (Dagu step errors + stderr tails), redacted
    like everything else that leaves the app."""
    from ..security import redact
    detail = "\n\n".join(f"[{e['step']} {e['status']}] {e['error']}"
                         for e in errors) or "(no step error recorded in dagu)"
    trace_id = run.traceparent.split("-")[1] if run.traceparent else ""
    return {
        "level": "error",
        "run_id": run.run_id,
        "mission_key": run.mission_key,
        "mission_type": run.mission_type,
        "dev_type": run.dev_type,
        "seq": run.seq,
        "outcome": outcome,
        "reason": reason,
        "trace_id": trace_id,
        "detail": redact(detail),
    }


class RunManager:
    def __init__(
        self,
        store: StatePort,
        messaging: MessagingPort,
        executor: ExecutorPort,
        finalizer: RunFinalizer | None = None,
    ):
        self.store = store
        self.messaging = messaging
        self.executor = executor
        self.bootstrap = RunBootstrap(store, messaging, executor)
        self.finalizer = finalizer  # MissionManager (or fake); optional for hello-only
        self.oauth_mgr = None       # wired by main (OAuthManager)
        self.runlog = None          # wired by main (RunLogStore)

    def set_finalizer(self, finalizer: RunFinalizer) -> None:
        """Bind mission finalization after composition (breaks construct cycle)."""
        self.finalizer = finalizer

    # ── dispatch (docs/04 §3.1, hello variant) ───────────────────────────────

    async def dispatch_hello(
        self, sleep: int = 3, payload_kb: int = 1, timeout_seconds: int | None = None
    ) -> Run:
        seq = sum(1 for r in self.store.all() if r.mission_key == "HELLO") + 1
        run_id = make_run_id("sys", "HELLO", seq, "HELLO")

        with tracer.start_as_current_span(
            "mission.dispatch", kind=SpanKind.PRODUCER
        ) as span:
            span.set_attribute("devcake.run.id", run_id)
            span.set_attribute("devcake.mission.key", "HELLO")
            span.set_attribute("devcake.dev_type", "hello-stub")
            carrier: dict[str, str] = {}
            inject(carrier)  # W3C traceparent → Dev container (docs/12 §2)
            traceparent = carrier.get("traceparent", "")

            run = Run(
                run_id=run_id,
                mission_key="HELLO",
                mission_type="HELLO",
                dev_type="hello-stub",
                seq=seq,
                timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
                traceparent=traceparent,
                spec_env={
                    "DEVCAKE_MISSION_ID": "debug-hello",
                    "DEVCAKE_MISSION_KEY": "HELLO",
                    "DEVCAKE_MISSION_TYPE": "HELLO",
                    "DEVCAKE_DEV_TYPE": "hello-stub",
                    "DEVCAKE_SEQ": str(seq),
                    "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OTEL_COLLECTOR_URL}/v1/traces",
                    "HELLO_SLEEP": str(sleep),
                    "HELLO_PAYLOAD_KB": str(payload_kb),
                },
            )
            await self.bootstrap.launch(run, image=HELLO_IMAGE)
            log.info("dispatched %s (image=%s)", run_id, HELLO_IMAGE)
            return run

    # ── ingress handling ─────────────────────────────────────────────────────

    def _runspec_secret(self, run: Run) -> dict | None:
        """Secret half of a run spec, built from current config on request
        (docs/09 §5): nothing secret is at rest between dispatch and the Dev's
        runspec.get, so a slow container start or a Redis restart cannot
        expire it. The requester is already authenticated (verify_auth)."""
        if run.mission_type == "HELLO":
            return {"env": {"FAKE_SECRET": f"devcake-fake-secret-{run.run_id}"},
                    "credential_files": [{"path_hint": "~/.hello/creds.json",
                                          "content": '{"fake": true}',
                                          "mode": "600"}]}
        if run.mission_type == "OAUTH":
            return {"env": {}, "credential_files": []}
        if self.finalizer is None:
            return None
        return self.finalizer.runspec_secret_payload(run)

    def verify_auth(self, run_id: str, auth: str | None) -> bool:
        run = self.store.get(run_id)
        return bool(
            run and run.auth_digest and auth
            and hmac.compare_digest(run.auth_digest, auth_digest(auth))
        )

    async def handle(self, run_id: str, kind: str, payload: dict) -> None:
        run = self.store.get(run_id)
        if run is None:
            log.warning("message for unknown run %s (%s)", run_id, kind)
            return

        # heartbeats (2/min/run) and streamed-log batches (one every few
        # seconds while a harness talks) are deliberately span-free — pure
        # liveness/output noise that would drown the run's trace (docs/12 §2)
        chatty = kind == "run.heartbeat" or (
            kind == "run.log" and isinstance(payload.get("lines"), list))
        if chatty:
            await self._handle_inner(run, run_id, kind, payload)
            return

        # every other ingress message is handled on a span in the Dev's trace
        ctx = extract({"traceparent": run.traceparent}) if run.traceparent else None
        with tracer.start_as_current_span(
            "ingress.handle", context=ctx, kind=SpanKind.CONSUMER
        ) as span:
            span.set_attribute("devcake.run.id", run_id)
            span.set_attribute("devcake.kind", kind)
            await self._handle_inner(run, run_id, kind, payload)

    async def _handle_inner(self, run: Run, run_id: str, kind: str,
                            payload: dict) -> None:
        if kind == "run.heartbeat":
            run.last_heartbeat = utcnow()
            self.store.save(run)
        elif kind == "run.started":
            run.state, run.started_at = "running", utcnow()
            self.store.save(run)
        elif kind == "runspec.get":
            secret = (self._runspec_secret(run)
                      if run.state in ("dispatched", "running") else None)
            if secret is None:
                await self.messaging.reply(
                    run_id, "runspec.error",
                    {"error": "run is not active, its dev type was deleted, or its repo was removed from config"},
                )
                return
            await self.messaging.reply(
                run_id, "runspec.result",
                {"env": {**run.spec_env, **(secret.get("env") or {})},
                 "credential_files": secret.get("credential_files") or [],
                 "prompt": run.spec_prompt},
            )
        elif kind == "runspec.ack":
            await self.messaging.delete_runspec_result(run_id)
        elif kind == "activity.get":
            if self.finalizer and run.mission_pmo_id:
                await self.messaging.reply(
                    run_id, "activity.result",
                    await self.finalizer.activity_payload(run))
            else:
                await self.messaging.reply(run_id, "activity.result",
                                           {"activity_md": "", "attachments": []})
        elif kind == "run.log":
            # {"lines": [...]} = streamed harness output (docs/09 §2);
            # {"oauth_url"}/{"oauth_error"} = device-code login progress,
            # routed to the OAuthManager (docs/09 §3)
            if self.runlog is not None and isinstance(payload.get("lines"), list):
                from ..security import redact
                self.runlog.append(
                    run_id, [redact(str(l))[:4000] for l in payload["lines"]])
            else:
                log.info("[%s] %s", run_id, payload.get("message", "") or payload)
                if self.oauth_mgr:
                    self.oauth_mgr.on_log(run_id, payload)
        elif kind == "oauth.result":
            if self.oauth_mgr:
                await self.oauth_mgr.on_result(run_id, payload)
        elif kind == "run.artifacts":
            # Terminal states: redelivery is a pure no-op. Only finished used
            # to short-circuit; failed/timed_out/orphaned re-entering finalize
            # double-ran side effects after mid-finalize crashes (ISSUES #1).
            if run.state in ("finished", "failed", "timed_out", "orphaned"):
                return
            run.state = "finalizing"
            self.store.save(run)
            if self.finalizer and run.mission_type == "MAPPER":
                await self.finalizer.finalize_mapper(run, payload)
            elif self.finalizer and run.mission_pmo_id:
                await self.finalizer.finalize(run, payload)
            else:
                await self._finalize(run, payload)
            if self.runlog is not None:
                self.runlog.close(run.run_id)  # end any live log followers
        else:
            log.warning("unknown message kind %s for %s", kind, run_id)

    # ── finalization (docs/04 §4 — PMO-less runs: hello) ─────────────────────

    async def _finalize(self, run: Run, payload: dict) -> None:
        ctx = extract({"traceparent": run.traceparent}) if run.traceparent else None
        with tracer.start_as_current_span(
            "run.finalize", context=ctx, kind=SpanKind.CONSUMER
        ) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            from ..security import redact_value
            run.result = redact_value(payload.get("result"))
            run.artifact_bytes = len(str(payload))
            span.set_attribute("devcake.outcome", str((run.result or {}).get("outcome")))
            # Mission runs post transcripts/token reports in MissionManager
            # .finalize (INV-5); this path serves runs with no PMO host.
            for step in ("acl_user_deleted", "reply_stream_deleted"):
                if step not in run.finalized_steps:
                    if step == "acl_user_deleted":
                        await self.messaging.delete_run_user(run.run_id)
                    else:
                        await self.messaging.delete_reply_stream(run.run_id)
                    run.finalized_steps.append(step)
                    self.store.save(run)
            run.state, run.ended_at = "finished", utcnow()
            self.store.save(run)
            log.info("finalized %s → finished", run.run_id)

    # ── watchdog support ─────────────────────────────────────────────────────

    async def kill(self, run: Run, new_state: str, reason: str) -> None:
        from opentelemetry import trace as _t
        from opentelemetry.propagate import extract as _ex
        ctx = _ex({"traceparent": run.traceparent}) if run.traceparent else None
        with _t.get_tracer("devcake").start_as_current_span(
                "watchdog.kill", context=ctx) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            span.set_attribute("devcake.outcome", new_state)
            span.set_attribute("devcake.kill.reason", reason)
            span.set_attribute("devcake.verdict", f"{new_state}: {reason}")
            span.set_status(_t.Status(_t.StatusCode.ERROR, reason))
            await self._kill_inner(run, new_state, reason)

    async def _ship_failure(self, run: Run, new_state: str, reason: str) -> None:
        """The executor's failure detail must land where everything else does:
        Dagu keeps the Dev's stderr tail in its run record but nothing ships it
        to OpenObserve — fluent-bit only sees the dagu/redis containers' own
        stdout, and Dev containers are removed on exit (docs/12 §6)."""
        from ..telemetry import push_oo_log
        try:
            errors = await self.executor.node_errors(run.run_id)
        except Exception:
            log.warning("no dagu node errors for %s", run.run_id, exc_info=True)
            errors = []
        record = failure_record(run, new_state, reason, errors)
        await push_oo_log(RUN_FAILURES_STREAM, record)
        log.warning("run %s → %s (%s): %s", run.run_id, new_state, reason,
                    record["detail"][:500])

    async def _kill_inner(self, run: Run, new_state: str, reason: str) -> None:
        # Fail-safe teardown (ISSUES #3): stop/_ship_failure may raise on
        # transport errors; ACL delete + terminal state MUST still run so the
        # run leaves store.active() and the watchdog does not re-kill forever.
        try:
            try:
                await self.executor.stop(run.run_id)
            except Exception:
                log.exception("executor.stop failed for %s — continuing teardown",
                              run.run_id)
            try:
                await self._ship_failure(run, new_state, reason)
            except Exception:
                log.exception("ship_failure failed for %s — continuing teardown",
                              run.run_id)
        finally:
            try:
                await self.messaging.delete_run_user(run.run_id)
            except Exception:
                log.exception("delete_run_user failed for %s", run.run_id)
            try:
                await self.messaging.delete_reply_stream(run.run_id)
            except Exception:
                log.exception("delete_reply_stream failed for %s", run.run_id)
            run.state = new_state  # type: ignore[assignment]
            run.ended_at = utcnow()
            from ..security import redact
            run.error = redact(reason)
            self.store.save(run)
        if self.finalizer and run.mission_pmo_id:
            try:
                await self.finalizer.restore_after_failure(run)
            except Exception:
                log.exception("restore_after_failure failed for %s", run.run_id)
        if self.oauth_mgr and run.run_id in self.oauth_mgr.sessions:
            self.oauth_mgr.sessions[run.run_id].update(
                state="failed", error=f"login container died ({reason})")
        if self.runlog is not None:
            self.runlog.close(run.run_id)  # end any live log followers
        log.warning("killed %s → %s (%s)", run.run_id, new_state, reason)
