"""Dispatch and finalization (M1 scope: the hello-world Dev — docs/16 M1).

Real mission dispatch replaces the debug path at M3; the mechanics here
(run spec over runspec.get, ACL lifecycle, trace continuity, finalization
checklist) are the permanent ones from docs/04 §3.1 and docs/09.
"""

import base64
import logging
import os

from opentelemetry import trace
from opentelemetry.trace import SpanKind
from opentelemetry.propagate import extract, inject

from .dagu import DaguExecutor
from .ids import make_run_id
from .messaging import Messaging
from .state import Run, RunStore, utcnow
from .telemetry import OO_ORG, OO_URL

log = logging.getLogger("devcake.runs")
tracer = trace.get_tracer("devcake")

HELLO_IMAGE = os.environ.get("DEVCAKE_HELLO_IMAGE", "devcake/dev-hello:latest")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("DEVCAKE_DEV_TIMEOUT_MINUTES", "120")) * 60


def _oo_basic_auth() -> str:
    email = os.environ.get("OO_ROOT_EMAIL", "")
    password = os.environ.get("OO_ROOT_PASSWORD", "")
    return base64.b64encode(f"{email}:{password}".encode()).decode()


class RunManager:
    def __init__(self, store: RunStore, messaging: Messaging, executor: DaguExecutor):
        self.store = store
        self.messaging = messaging
        self.executor = executor
        self.mission_mgr = None  # wired by main (MissionManager); None for hello-only
        self.oauth_mgr = None    # wired by main (OAuthManager)

    # ── dispatch (docs/04 §3.1, hello variant) ───────────────────────────────

    async def dispatch_hello(
        self, sleep: int = 3, payload_kb: int = 1, timeout_seconds: int | None = None
    ) -> Run:
        seq = sum(1 for r in self.store.all() if r.mission_key == "HELLO") + 1
        run_id = make_run_id("HELLO", seq, "HELLO")

        with tracer.start_as_current_span(
            "mission.dispatch", kind=SpanKind.PRODUCER
        ) as span:
            span.set_attribute("devcake.run.id", run_id)
            span.set_attribute("devcake.mission.key", "HELLO")
            span.set_attribute("devcake.dev_type", "hello-stub")
            carrier: dict[str, str] = {}
            inject(carrier)  # W3C traceparent → Dev container (docs/12 §2)
            traceparent = carrier.get("traceparent", "")

            redis_password = await self.messaging.create_run_user(run_id)

            run = Run(
                run_id=run_id,
                mission_key="HELLO",
                mission_type="HELLO",
                dev_type="hello-stub",
                seq=seq,
                timeout_seconds=timeout_seconds or DEFAULT_TIMEOUT_SECONDS,
                traceparent=traceparent,
                redis_password=redis_password,
                spec_env={
                    "DEVCAKE_MISSION_ID": "debug-hello",
                    "DEVCAKE_MISSION_KEY": "HELLO",
                    "DEVCAKE_MISSION_TYPE": "HELLO",
                    "DEVCAKE_DEV_TYPE": "hello-stub",
                    "DEVCAKE_SEQ": str(seq),
                    "OTEL_EXPORTER_OTLP_ENDPOINT": f"{OO_URL}/api/{OO_ORG}/v1/traces",
                    "OTEL_EXPORTER_OTLP_BASIC": _oo_basic_auth(),
                    "HELLO_SLEEP": str(sleep),
                    "HELLO_PAYLOAD_KB": str(payload_kb),
                    # M1 secrets probe: must NEVER appear in Dagu's UI/run API
                    "FAKE_SECRET": f"devcake-fake-secret-{run_id}",
                },
                spec_files=[{
                    "path_hint": "~/.hello/creds.json",
                    "content": '{"fake": true}',
                    "mode": "600",
                }],
            )
            self.store.save(run)  # durable intent BEFORE the trigger (docs/04 §3.1)

            await self.executor.start(
                params={
                    "RUN_ID": run_id,
                    "IMAGE": HELLO_IMAGE,
                    "TRACEPARENT": traceparent,
                    "REDIS_USER": f"dev-{run_id}",
                    "REDIS_PASSWORD": redis_password,
                },
                dag_run_id=run_id,
            )
            log.info("dispatched %s (image=%s)", run_id, HELLO_IMAGE)
            return run

    # ── ingress handling ─────────────────────────────────────────────────────

    def verify_auth(self, run_id: str, auth: str | None) -> bool:
        run = self.store.get(run_id)
        return bool(run and run.redis_password and auth == run.redis_password)

    async def handle(self, run_id: str, kind: str, payload: dict) -> None:
        run = self.store.get(run_id)
        if run is None:
            log.warning("message for unknown run %s (%s)", run_id, kind)
            return

        if kind == "run.started":
            run.state, run.started_at = "running", utcnow()
            self.store.save(run)
        elif kind == "run.heartbeat":
            run.last_heartbeat = utcnow()
            self.store.save(run)
        elif kind == "runspec.get":
            await self.messaging.reply(
                run_id, "runspec.result",
                {"env": run.spec_env, "credential_files": run.spec_files,
                 "prompt": run.spec_prompt},
            )
        elif kind == "runspec.ack":
            await self.messaging.delete_runspec_result(run_id)
        elif kind == "activity.get":
            if self.mission_mgr and run.mission_pmo_id:
                await self.messaging.reply(
                    run_id, "activity.result",
                    await self.mission_mgr.activity_payload(run.mission_pmo_id,
                                                            run.pmo_kind))
            else:
                await self.messaging.reply(run_id, "activity.result",
                                           {"activity_md": "", "attachments": []})
        elif kind == "run.log":
            log.info("[%s] %s", run_id, payload.get("message", "") or payload)
            if self.oauth_mgr:
                self.oauth_mgr.on_log(run_id, payload)
        elif kind == "oauth.result":
            if self.oauth_mgr:
                await self.oauth_mgr.on_result(run_id, payload)
        elif kind == "run.artifacts":
            if run.state == "finished":
                return  # redelivery: idempotent no-op
            run.state = "finalizing"
            self.store.save(run)
            if self.mission_mgr and run.mission_pmo_id:
                await self.mission_mgr.finalize(run, payload)
            else:
                await self._finalize(run, payload)
        else:
            log.warning("unknown message kind %s for %s", kind, run_id)

    # ── finalization (docs/04 §4, M1 subset) ─────────────────────────────────

    async def _finalize(self, run: Run, payload: dict) -> None:
        ctx = extract({"traceparent": run.traceparent}) if run.traceparent else None
        with tracer.start_as_current_span(
            "run.finalize", context=ctx, kind=SpanKind.CONSUMER
        ) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            run.result = payload.get("result")
            run.artifact_bytes = len(str(payload))
            span.set_attribute("devcake.outcome", str((run.result or {}).get("outcome")))
            # M2+: post transcript + token report to the PMO here (INV-5).
            for step in ("acl_user_deleted", "reply_stream_deleted"):
                if step not in run.finalized_steps:
                    if step == "acl_user_deleted":
                        await self.messaging.delete_run_user(run.run_id)
                    else:
                        await self.messaging.delete_reply_stream(run.run_id)
                    run.finalized_steps.append(step)
                    self.store.save(run)
            run.state, run.ended_at = "finished", utcnow()
            run.redis_password = None  # revoked → stop persisting it
            self.store.save(run)
            log.info("finalized %s → finished", run.run_id)

    # ── watchdog support ─────────────────────────────────────────────────────

    async def kill(self, run: Run, new_state: str, reason: str) -> None:
        await self.executor.stop(run.run_id)
        await self.messaging.delete_run_user(run.run_id)
        await self.messaging.delete_reply_stream(run.run_id)
        run.state = new_state  # type: ignore[assignment]
        run.ended_at = utcnow()
        run.error = reason
        run.redis_password = None
        self.store.save(run)
        if self.mission_mgr and run.mission_pmo_id:
            await self.mission_mgr.restore_after_failure(run)
        if self.oauth_mgr and run.run_id in self.oauth_mgr.sessions:
            self.oauth_mgr.sessions[run.run_id].update(
                state="failed", error=f"login container died ({reason})")
        log.warning("killed %s → %s (%s)", run.run_id, new_state, reason)
