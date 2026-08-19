"""Run lifecycle shared by every dispatch path (docs/04 §3.1, docs/09):
run-spec service over runspec.get, envelope auth, ACL lifecycle, trace
continuity, watchdog kill, and the finalization checklist. Also hosts the
hello stub dispatch — the permanent debug/CI fixture (scripts/ci_suite.sh).

Dispatch spine (ACL → digest → durable save → executor.start) lives in
RunBootstrap; this module owns ingress, kill, and hello-specific fields.
"""

from __future__ import annotations

import asyncio
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
from . import failure_taxonomy
from .ids import make_run_id
from .run import Run, TERMINAL_STATES, auth_digest, is_pre_wipe, utcnow
from .run_bootstrap import RunBootstrap
from .workspaces import NullWorkspaceStore

log = logging.getLogger("devcake.runs")
tracer = trace.get_tracer("devcake")

HELLO_IMAGE = os.environ.get(
    "DEVCAKE_HELLO_IMAGE",
    f"devcake/dev-hello:{os.environ.get('DEVCAKE_TAG', 'latest')}")
DEFAULT_TIMEOUT_SECONDS = int(os.environ.get("DEVCAKE_DEV_TIMEOUT_MINUTES", "120")) * 60


RUN_FAILURES_STREAM = "run_failures"

# ADR-0018 — error class per kill target state, applied at the `_kill_inner`
# chokepoint. `DEV_KILLED` is the catch-all for any state not named here (and
# for a future one nobody remembers to add), so no kill path can leave a run
# unclassified and silently fall back to the legacy `error`-prefix matching in
# `attempt_number`. Operator-initiated stops pass DEV_OPERATOR_STOP explicitly.
# ADR-0027: the mapping is the taxonomy table's `kill_state` column.
KILL_CLASSES = failure_taxonomy.KILL_CLASSES


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


def provision_runspec_reply(run: "Run", secret: dict) -> dict:
    """ADR-0025 R1 — the REDUCED spec served to a `runspec.get` that names
    phase "provision". The provision container runs trusted code but sits
    in the same image on the same network, so it gets only what cloning
    needs: the run's non-secret spec_env, the extras entries (mirrored ones
    are already tokenless), the activity-repo credential, and the forge
    token ONLY when the work repo direct-clones (mirror_path empty —
    internal repos). Harness/model keys, Dev-Type secret env and credential
    FILE content never ride this reply."""
    env = dict(run.spec_env)
    if not env.get("DEVCAKE_MIRROR_PATH", ""):
        tok = (secret.get("env") or {}).get("DEVCAKE_FORGE_TOKEN")
        if tok:
            env["DEVCAKE_FORGE_TOKEN"] = tok
    return {"env": env,
            "credential_files": [],
            "extra_repos": secret.get("extra_repos") or [],
            "memory_repos": secret.get("memory_repos") or [],
            "skills": [],
            "skills_dir": "",
            "mcp_setup_commands": [],
            "activity_repo": secret.get("activity_repo") or None,
            "prompt": ""}


class RunManager:
    def __init__(
        self,
        store: StatePort,
        messaging: MessagingPort,
        executor: ExecutorPort,
        finalizer: RunFinalizer | None = None,
        workspaces=None,
        config=None,
    ):
        self.store = store
        self.messaging = messaging
        self.executor = executor
        # ADR-0025: per-run workspace lifecycle (Null default keeps every
        # existing construction — tests included — a no-op, like NullRepoCache)
        self.workspaces = workspaces or NullWorkspaceStore()
        self.bootstrap = RunBootstrap(store, messaging, executor,
                                      workspaces=self.workspaces,
                                      config=config)
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
                pmo_ref="sys",   # not the legacy-marker default "main" (A29)
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

    def _pre_wipe(self, run: Run) -> bool:
        """True when this run is not stamped for the store's current wipe
        generation (docs/10 store_gen). In-flight finalize/heartbeat must not
        resurrect records or drive further PMO side effects after "start
        fresh". Delegates to ``is_pre_wipe`` so finalize shares one fence."""
        return is_pre_wipe(self.store, run)

    async def handle(self, run_id: str, kind: str, payload: dict) -> None:
        run = self.store.get(run_id)
        if run is None:
            log.warning("message for unknown run %s (%s)", run_id, kind)
            return
        # A concurrent clear can wipe the file while we still hold an older
        # in-memory object only if handle was entered before clear — get()
        # already returned None above for post-wipe messages. Pre-wipe check
        # still matters for callers that pass a cached Run into kill/finalize.
        if self._pre_wipe(run):
            log.info("drop ingress %s for pre-wipe run %s", kind, run_id)
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
            # ADR-0025: accept ONLY the dispatched→running transition. Any
            # replay (ingress redelivery, the bus's XADD retry, a Dagu-UI
            # re-run) previously overwrote started_at — corrupting the Runs
            # page's runtime metric — and even reverted a `finalizing` run
            # to `running`; and under the two-step DAG only the provision
            # step sends run.started, so anything else is a replay by
            # construction. (Supersedes the narrower terminal-only guard,
            # audit D5 #10.)
            if run.state != "dispatched":
                return
            run.state, run.started_at = "running", utcnow()
            ver = (payload or {}).get("harness_version")
            if isinstance(ver, str) and ver.strip():
                run.harness_version = ver.strip()[:120]
            self.store.save(run)
        elif kind == "runspec.get":
            secret = None
            refresh_err: str | None = None
            if run.state in ("dispatched", "running"):
                try:
                    # Host-side Grok OAuth refresh (flock + sync httpx) must
                    # not monopolize the event loop during runspec.get.
                    secret = await asyncio.to_thread(self._runspec_secret, run)
                except Exception as e:  # noqa: BLE001 — narrowed to CredentialRefreshError below; other types re-raise
                    # Fail closed at inject: host-side Grok OAuth refresh.
                    from .grok_oauth import CredentialRefreshError
                    if isinstance(e, CredentialRefreshError):
                        refresh_err = str(e)
                        log.warning("runspec refused for %s: %s", run_id,
                                    refresh_err)
                    else:
                        raise
            if secret is None:
                await self.messaging.reply(
                    run_id, "runspec.error",
                    {"error": refresh_err or (
                        "run is not active, its dev type was deleted, "
                        "or its repo was removed from config")},
                )
                return
            # ADR-0025 R1: the provision step asks with {"phase":
            # "provision"} and gets a reduced, secret-free spec; the harness
            # step asks with "harness" and gets the full one. Anything else
            # (defensively) gets the full spec. Stateless per request — both
            # steps of one run ask independently.
            if (payload or {}).get("phase") == "provision":
                await self.messaging.reply(
                    run_id, "runspec.result",
                    provision_runspec_reply(run, secret))
                return
            await self.messaging.reply(
                run_id, "runspec.result",
                {"env": {**run.spec_env, **(secret.get("env") or {})},
                 "credential_files": secret.get("credential_files") or [],
                 # multi-repo ONBOARD triage (item 2): read-only sibling
                 # clones, each with its own read token
                 "extra_repos": secret.get("extra_repos") or [],
                 "memory_repos": secret.get("memory_repos") or [],
                 # skill store: non-secret, snapshotted on the Run at
                 # dispatch — the entrypoint writes them under
                 # $HOME/<skills_dir> (harness registry; "" → its default)
                 "skills": run.spec_skills or [],
                 "skills_dir": run.spec_skills_dir or "",
                 # per-Dev-Type MCP registration, run by the entrypoint
                 # before harness launch (docs/07 §5 step 5, exit 14)
                 "mcp_setup_commands": secret.get("mcp_setup_commands") or [],
                 # ADR-0014 D4: clone-first activity materialization; None →
                 # the entrypoint uses the Redis activity.get fallback
                 "activity_repo": secret.get("activity_repo") or None,
                 "prompt": run.spec_prompt},
            )
        elif kind == "runspec.ack":
            await self.messaging.delete_runspec_result(run_id)
        elif kind == "activity.get":
            if self.finalizer and run.mission_pmo_id:
                reply = await self.finalizer.activity_payload(run)
                # ADR-0031: this fallback rebuilds the payload at container
                # start, LATER than the dispatch-time snapshot — the Dev is
                # about to read a fresher mirror than the Run's receipt
                # claims. Refresh the watermark so the Freshness Gate judges
                # against what was actually served (stale receipt = spurious
                # re-review, the wasteful direction).
                wm = reply.get("feed_watermark")
                if wm:
                    # Re-read: this handle() object was parsed before the
                    # await. An offloaded finalize can have finished the
                    # run in that window; saving `run` would last-writer-
                    # wins over finalized_steps / state (F11 leftover).
                    fresh = self.store.get(run_id)
                    if fresh is None or fresh.state not in (
                            "dispatched", "running"):
                        await self.messaging.reply(
                            run_id, "activity.result", reply)
                        return
                    fresh.feed_watermark = wm
                    self.store.save(fresh)
                await self.messaging.reply(run_id, "activity.result", reply)
            else:
                await self.messaging.reply(
                    run_id, "activity.result",
                    {"mission_md": "", "activity_md": "", "attachments": []})
        elif kind == "run.log":
            # {"lines": [...]} = streamed harness output (docs/09 §2);
            # {"oauth_url"}/{"oauth_error"} = device-code login progress,
            # routed to the OAuthManager (docs/09 §3)
            if self.runlog is not None and isinstance(payload.get("lines"), list):
                from ..security import redact
                self.runlog.append(
                    run_id, [redact(str(l))[:4000] for l in payload["lines"]])
            else:
                # OAuth device-code ceremony: never log url/code values
                # (short-lived auth material; status API still holds them).
                if payload.get("oauth_url") is not None or payload.get("oauth_error") is not None:
                    log.info(
                        "[%s] oauth progress url=%s has_code=%s error=%s",
                        run_id,
                        "present" if payload.get("oauth_url") else "absent",
                        bool(payload.get("code")),
                        "present" if payload.get("oauth_error") else "absent",
                    )
                else:
                    msg = payload.get("message", "")
                    log.info("[%s] %s", run_id, msg if msg else "(non-lines run.log)")
                if self.oauth_mgr:
                    self.oauth_mgr.on_log(run_id, payload)
        elif kind == "oauth.result":
            if self.oauth_mgr:
                await self.oauth_mgr.on_result(run_id, payload)
        elif kind == "run.artifacts":
            # Terminal + prior finalize work ⇒ true redelivery no-op.
            # Terminal with empty finalized_steps ⇒ first delivery after a
            # premature orphan/kill (boot reconcile or kill-race) — reopen
            # finalize so INV-5 / entrypoint _on_term are not dropped (CAKE-73).
            if run.state in TERMINAL_STATES:
                if run.finalized_steps:
                    return
                log.info(
                    "first artifacts delivery on premature terminal %s (%s) — "
                    "reopening finalize",
                    run_id, run.state,
                )
            if self._pre_wipe(run):
                log.info("drop finalize for pre-wipe run %s", run_id)
                return
            run.state = "finalizing"
            self.store.save(run)
            if self._pre_wipe(run):
                # clear raced between the check and save — save was a no-op;
                # do not drive PMO transitions on a wiped run.
                log.info("abort finalize after wipe race for %s", run_id)
                return
            if self.finalizer and run.mission_type == "STEWARD":
                await self.finalizer.finalize_steward(run, payload)
            elif self.finalizer and run.mission_pmo_id:
                await self.finalizer.finalize(run, payload)
            else:
                await self._finalize(run, payload)
            if self.runlog is not None:
                self.runlog.close(run.run_id)  # end any live log followers
            # ADR-0025 Hook A: one cleanup covers every finalize exit
            # (mission success/failure, steward, hello) — but ONLY once the
            # run is actually terminal. A finalize crash leaves `finalizing`
            # and the workspace intact for artifact redelivery / the
            # stalled-finalize killer to reach later. The container already
            # exited (artifacts are the dying words) — no mount race.
            fresh = self.store.get(run_id)
            if fresh is None or fresh.state in TERMINAL_STATES:
                self.workspaces.cleanup(run_id)
        else:
            log.warning("unknown message kind %s for %s", kind, run_id)

    # ── finalization (docs/04 §4 — PMO-less runs: hello) ─────────────────────

    async def _finalize(self, run: Run, payload: dict) -> None:
        if self._pre_wipe(run):
            log.info("skip hello finalize for pre-wipe run %s", run.run_id)
            return
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
            # lazy: importing orchestrator.steps at module top would cycle
            # (runs -> orchestrator/__init__ -> manager -> runs)
            from .orchestrator import steps
            if steps.ACL_USER_DELETED not in run.finalized_steps:
                await self.messaging.delete_run_user(run.run_id)
                run.finalized_steps.append(steps.ACL_USER_DELETED)
                self.store.save(run)
            if steps.REPLY_STREAM_DELETED not in run.finalized_steps:
                await self.messaging.delete_reply_stream(run.run_id)
                run.finalized_steps.append(steps.REPLY_STREAM_DELETED)
                self.store.save(run)
            run.state, run.ended_at = "finished", utcnow()
            self.store.save(run)
            log.info("finalized %s → finished", run.run_id)

    # ── watchdog support ─────────────────────────────────────────────────────

    async def kill(self, run: Run, new_state: str, reason: str, *,
                   error_class: str | None = None) -> None:
        from opentelemetry.trace import Status, StatusCode
        ctx = extract({"traceparent": run.traceparent}) if run.traceparent else None
        with tracer.start_as_current_span(
                "watchdog.kill", context=ctx) as span:
            span.set_attribute("devcake.run.id", run.run_id)
            span.set_attribute("devcake.outcome", new_state)
            span.set_attribute("devcake.kill.reason", reason)
            span.set_attribute("devcake.verdict", f"{new_state}: {reason}")
            span.set_status(Status(StatusCode.ERROR, reason))
            await self._kill_inner(run, new_state, reason, error_class=error_class)

    async def _ship_failure(self, run: Run, new_state: str, reason: str) -> None:
        """The executor's failure detail must land where everything else does:
        Dagu keeps the Dev's stderr tail in its run record but nothing ships it
        to OpenObserve — fluent-bit only sees the dagu/redis containers' own
        stdout, and Dev containers are removed on exit (docs/12 §6)."""
        from ..telemetry import push_oo_log
        try:
            errors = await self.executor.node_errors(run.run_id)
        except Exception:  # noqa: BLE001 — detail probe is best-effort (logged); the failure record must still ship to OpenObserve
            log.warning("no dagu node errors for %s", run.run_id, exc_info=True)
            errors = []
        record = failure_record(run, new_state, reason, errors)
        await push_oo_log(RUN_FAILURES_STREAM, record)
        log.warning("run %s → %s (%s): %s", run.run_id, new_state, reason,
                    record["detail"][:500])

    async def _kill_inner(self, run: Run, new_state: str, reason: str, *,
                          error_class: str | None = None) -> None:
        # The state the killer DECIDED on, captured before any await in this
        # method: the teardown below yields repeatedly, and finalize can claim
        # or finish the run in those windows (2026-08 evaluation TOCTOU).
        prior_state = run.state
        # Fail-safe teardown: stop/_ship_failure may raise on transport errors;
        # ACL delete + terminal state MUST still run so the run leaves
        # store.active() and the watchdog does not re-kill forever.
        try:
            try:
                await self.executor.stop(run.run_id)
            except Exception:  # noqa: BLE001 — best-effort teardown: stop may raise on transport errors; ACL + terminal state must still run
                log.exception("executor.stop failed for %s — continuing teardown",
                              run.run_id)
            try:
                await self._ship_failure(run, new_state, reason)
            except Exception:  # noqa: BLE001 — best-effort teardown: failure record may fail to ship; ACL + terminal state must still run
                log.exception("ship_failure failed for %s — continuing teardown",
                              run.run_id)
        finally:
            try:
                await self.messaging.delete_run_user(run.run_id)
            except Exception:  # noqa: BLE001 — best-effort teardown: ACL delete is mandatory-attempt; continue past transport errors
                log.exception("delete_run_user failed for %s", run.run_id)
            try:
                await self.messaging.delete_reply_stream(run.run_id)
            except Exception:  # noqa: BLE001 — best-effort teardown: reply-stream delete continues past transport errors
                log.exception("delete_reply_stream failed for %s", run.run_id)
            # Two guards on one FRESH read, no await before save (atomic under
            # asyncio's cooperative scheduling):
            # 1. Do not RESURRECT a record a concurrent clear-runs wipe just
            #    deleted (re-audit #31 #1/#2): a record clear already unlinked
            #    reads None → skip; it is already out of store.active() so
            #    nothing re-kills it.
            # 2. Do not OVERWRITE a run whose state moved under the kill
            #    (2026-08 evaluation): the teardown awaits above are yield
            #    points where finalize can claim (running → finalizing) or
            #    finish the run — stamping timed_out/failed then would revert
            #    a landed PMO transition or race the in-flight finalize.
            #    Comparing against `prior_state` (what the killer decided on)
            #    keeps the stalled-finalize path legal: it re-reads and
            #    passes a run still in `finalizing`, so the states match.
            #    NOTE: the record object stays UNMUTATED on the skip path —
            #    `run` may be the store's shared parse-cache instance
            #    (store.all()), and a dead killer must not scribble on an
            #    object other coroutines are reading.
            current = self.store.get(run.run_id)
            state_moved = current is not None and current.state != prior_state
            if state_moved:
                # The record object stays UNMUTATED on this path — `run` may
                # be the store's shared parse-cache instance (store.all()),
                # and a dead killer must not scribble on an object other
                # coroutines are reading.
                log.info("kill of %s aborted at save: state moved %s → %s "
                         "under the kill — the mover wins", run.run_id,
                         prior_state, current.state)
            else:
                run.state = new_state  # type: ignore[assignment]
                run.ended_at = utcnow()
                from ..security import redact
                run.error = redact(reason)
                # ADR-0018: classify HERE, not at the call sites. Seven callers
                # across watchdog / reconcile / clear / stop-run / stop-all
                # funnel through this method, and two earlier attempts to
                # enumerate them in a table both missed one. The state-keyed
                # default plus the DEV_KILLED catch-all means a future kill
                # site cannot silently produce an unclassified run.
                run.error_class = (error_class
                                   or KILL_CLASSES.get(
                                       new_state, failure_taxonomy.DEV_KILLED))
                if current is not None:
                    self.store.save(run)
            # ADR-0025 Hook B: every kill path (watchdog ×3, reconcile
            # orphan, operator stop, clear drain) funnels through this
            # teardown. Best-effort by doctrine — the dying container can
            # hold the bind through Dagu's stop grace and keep writing; the
            # sweep guarantees reclamation. cleanup() never raises, but this
            # block is the fail-safe teardown, so belt-and-suspenders.
            try:
                self.workspaces.cleanup(run.run_id)
            except Exception:  # noqa: BLE001 — best-effort teardown: workspace cleanup is belt-and-suspenders; the sweep reclaims leftovers
                log.exception("workspace cleanup failed for %s", run.run_id)
        # state_moved: the kill lost the race — the run's mover (finalize, or
        # another killer) now owns the mission transition, and restoring the
        # dispatch-time status here would revert it underneath the live
        # finalize (the exact hazard clear.py's drain guards against). The
        # record-deleted (clear-wipe) path keeps restoring as before: "start
        # fresh" legitimately hands missions back.
        if self.finalizer and run.mission_pmo_id and not state_moved:
            try:
                await self.finalizer.restore_after_failure(run)
            except Exception:  # noqa: BLE001 — best-effort teardown: INV-3 restore must not undo a completed kill if PMO write fails
                log.exception("restore_after_failure failed for %s", run.run_id)
        if self.oauth_mgr and run.run_id in self.oauth_mgr.sessions:
            self.oauth_mgr.sessions[run.run_id].update(
                state="failed", error=f"login container died ({reason})")
        if self.runlog is not None:
            self.runlog.close(run.run_id)  # end any live log followers
        log.warning("killed %s → %s (%s)", run.run_id, new_state, reason)
