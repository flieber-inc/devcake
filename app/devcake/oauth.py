"""GUI OAuth helpers (docs/16 M6, founder request): device-code login flows run
INSIDE the harness images via the normal Dev pipeline — Dagu spawns the
container, the entrypoint streams the verification URL + user code back over
Redis (`run.log`), and the resulting credential file arrives on a dedicated
`oauth.result` message, stored straight to /data/secrets/ (never persisted in
run records)."""

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .ids import make_run_id
from .state import Run

log = logging.getLogger("devcake.oauth")

SECRETS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"

FLOWS = {
    "grok-build": {"login_cmd": "grok login --device-auth",
                   "auth_path": "~/.grok/auth.json",
                   "secret_file": "grok-auth.json"},
    "codex": {"login_cmd": "codex login --device-auth",
              "auth_path": "~/.codex/auth.json",
              "secret_file": "codex-auth.json"},
}


class OAuthManager:
    def __init__(self, runs, messaging, dev_types):
        self.runs = runs
        self.messaging = messaging
        self.dev_types = dev_types
        self.sessions: dict[str, dict[str, Any]] = {}  # run_id → status

    def _dev_type_for(self, harness: str):
        return next((d for d in self.dev_types.values()
                     if d.harness_template == harness), None)

    async def start(self, harness: str) -> dict:
        from opentelemetry import trace as _t
        with _t.get_tracer("devcake").start_as_current_span("oauth.start") as span:
            span.set_attribute("devcake.harness", harness)
            result = await self._start_inner(harness)
            span.set_attribute("devcake.run.id", result["run_id"])
            return result

    async def _start_inner(self, harness: str) -> dict:
        flow = FLOWS.get(harness)
        dev_type = self._dev_type_for(harness)
        if not flow or not dev_type:
            raise ValueError(f"no OAuth flow/dev type for harness {harness!r}")
        run_id = make_run_id("OAUTH", 1, harness.split("-")[0].upper())
        password = await self.messaging.create_run_user(run_id)
        run = Run(run_id=run_id, mission_key="OAUTH", mission_type="OAUTH",
                  dev_type=dev_type.name, seq=1, timeout_seconds=600,
                  redis_password=password,
                  spec_env={"DEVCAKE_OAUTH_MODE": harness,
                            "DEVCAKE_OAUTH_LOGIN_CMD": flow["login_cmd"],
                            "DEVCAKE_OAUTH_AUTH_PATH": flow["auth_path"]})
        self.runs.store.save(run)
        await self.runs.executor.start(
            params={"RUN_ID": run_id, "IMAGE": dev_type.docker_image,
                    "TRACEPARENT": "", "REDIS_USER": f"dev-{run_id}",
                    "REDIS_PASSWORD": password},
            dag_run_id=run_id)
        self.sessions[run_id] = {"harness": harness, "state": "starting",
                                 "url": None, "code": None,
                                 "started": datetime.now(timezone.utc).isoformat()}
        log.info("oauth: started %s flow (%s)", harness, run_id)
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
        flow = FLOWS[s["harness"]]
        dev_type = self._dev_type_for(s["harness"])
        target = SECRETS_DIR / dev_type.name
        target.mkdir(parents=True, exist_ok=True)
        p = target / flow["secret_file"]
        p.write_text(payload["content"])
        p.chmod(0o600)
        s["state"] = "completed"
        run.state, run.redis_password = "finished", None
        self.runs.store.save(run)
        await self.messaging.delete_run_user(run_id)
        await self.messaging.delete_reply_stream(run_id)
        # a fresh credential clears any auth breaker for this dev type (docs/15 §4)
        if self.runs.mission_mgr:
            self.runs.mission_mgr.breakers.pop(dev_type.name, None)
        log.info("oauth: %s credential stored for %s", s["harness"], dev_type.name)

    def status(self, run_id: str) -> Optional[dict]:
        return self.sessions.get(run_id)
