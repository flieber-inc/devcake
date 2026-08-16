"""GUI OAuth helpers (founder request): device-code login flows run
INSIDE the harness images via the normal Dev pipeline — Dagu spawns the
container, the entrypoint streams the verification URL + user code back over
Redis (`run.log`), and the resulting credential file arrives on a dedicated
`oauth.result` message, stored straight to /data/secrets/ (never persisted in
run records).

Dispatch spine (ACL → digest → durable save → executor.start) is RunBootstrap;
this module owns session snapshot + credential landing.
"""

import logging
from datetime import datetime, timezone
from typing import Any, MutableMapping, Optional

from ..harness import HARNESSES, resolve_image
from .ids import make_run_id
from .run import Run, utcnow

log = logging.getLogger("devcake.oauth")


class OAuthManager:
    def __init__(self, runs, messaging, dev_types,
                 breakers: MutableMapping[str, str] | None = None):
        self.runs = runs
        self.messaging = messaging
        self.dev_types = dev_types
        # optional shared breaker map (MissionManager.breakers); cleared on success
        self.breakers = breakers
        self.sessions: dict[str, dict[str, Any]] = {}  # run_id → status

    async def start(self, dev_type_name: str) -> dict:
        from opentelemetry import trace as _t
        with _t.get_tracer("devcake").start_as_current_span("oauth.start") as span:
            span.set_attribute("devcake.dev_type", dev_type_name)
            result = await self._start_inner(dev_type_name)
            span.set_attribute("devcake.run.id", result["run_id"])
            return result

    async def _start_inner(self, dev_type_name: str) -> dict:
        dev_type = self.dev_types.get(dev_type_name)
        if not dev_type:
            raise ValueError(f"unknown dev type {dev_type_name!r}")
        harness = HARNESSES[dev_type.harness_template]
        if not harness.oauth:
            raise ValueError(f"harness {dev_type.harness_template!r} has no OAuth "
                             "flow (it authenticates via env token)")
        flow = harness.oauth
        run_id = make_run_id("sys", "OAUTH", 1,
                             dev_type.harness_template.split("-")[0].upper())
        run = Run(run_id=run_id, mission_key="OAUTH", mission_type="OAUTH",
                  pmo_ref="sys",   # not the legacy-marker default "main" (A29)
                  # 660 not 600 (ADR-0025 R6): the two-step DAG spends one
                  # extra container cycle (provision exits 0 early for OAuth)
                  # before the device-code window opens for the human
                  dev_type=dev_type.name, seq=1, timeout_seconds=660,
                  spec_env={"DEVCAKE_OAUTH_MODE": dev_type.harness_template,
                            "DEVCAKE_OAUTH_LOGIN_CMD": flow.login_cmd,
                            "DEVCAKE_OAUTH_AUTH_PATH": flow.auth_path})
        await self.runs.bootstrap.launch(run, image=resolve_image(dev_type))
        # snapshot everything on_result needs: a dev type deleted or re-harnessed
        # mid-login must not misroute (or KeyError) the credential
        self.sessions[run_id] = {"dev_type": dev_type.name,
                                 "harness": dev_type.harness_template,
                                 "secret_file": flow.secret_file,
                                 "state": "starting", "url": None, "code": None,
                                 "started": datetime.now(timezone.utc).isoformat()}
        log.info("oauth: started %s flow for %s (%s)",
                 dev_type.harness_template, dev_type.name, run_id)
        return {"run_id": run_id}

    def on_log(self, run_id: str, payload: dict) -> None:
        s = self.sessions.get(run_id)
        if not s:
            return
        if payload.get("oauth_url"):
            s.update(state="awaiting_user", url=payload["oauth_url"],
                     code=payload.get("code"))
        if payload.get("oauth_error"):
            s.update(state="failed", error=payload["oauth_error"])

    async def on_result(self, run_id: str, payload: dict) -> None:
        from opentelemetry import trace as _t
        with _t.get_tracer("devcake").start_as_current_span("oauth.result") as span:
            span.set_attribute("devcake.run.id", run_id)
            await self._on_result_inner(run_id, payload)

    async def _on_result_inner(self, run_id: str, payload: dict) -> None:
        s = self.sessions.get(run_id)
        run = self.runs.store.get(run_id)
        if not s or not run:
            return
        # snapshot from start(), never re-looked-up (see _start_inner)
        from .. import secrets as secrets_store
        secrets_store.write_credential_file(
            s["dev_type"], s["secret_file"], payload["content"])
        s["state"] = "completed"
        # Terminal-ize ONLY a still-active run. A credential can land after
        # the watchdog's 660 s kill (a slow human on the device-code page) or
        # an operator stop — the credential is still stored above (the login
        # DID succeed), but the run record's history is not rewritten: before
        # this guard, a late oauth.result flipped a timed_out run back to
        # "finished" while racing the kill's own save. And stamp ended_at:
        # this was the ONE terminal transition in the codebase that omitted
        # it, so every successful OAuth login's runtime grew forever in the
        # Runs tab ((ended_at or now) - started_at, repriced every poll) and
        # ranked as the longest run in history under the duration sort.
        if run.state in ("dispatched", "running"):
            run.state = "finished"
            run.ended_at = utcnow()
            self.runs.store.save(run)
        else:
            log.info("oauth: credential for %s landed after run %s already "
                     "terminalled (%s) — record left as-is",
                     s["dev_type"], run_id, run.state)
        await self.messaging.delete_run_user(run_id)
        await self.messaging.delete_reply_stream(run_id)
        # ADR-0025 Hook D: OAuth completion bypasses finalize AND kill, so
        # its (empty) workspace dir is reclaimed right here
        self.runs.workspaces.cleanup(run_id)
        # a fresh credential clears any auth breaker for this dev type (docs/15 §4)
        if self.breakers is not None:
            self.breakers.pop(s["dev_type"], None)
        log.info("oauth: %s credential stored for %s", s["harness"], s["dev_type"])

    def status(self, run_id: str) -> Optional[dict]:
        return self.sessions.get(run_id)
