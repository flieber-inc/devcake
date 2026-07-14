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
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, MutableMapping, Optional

from ..harness import HARNESSES
from .ids import make_run_id
from .run import Run

log = logging.getLogger("devcake.oauth")

SECRETS_DIR = Path(os.environ.get("DEVCAKE_DATA_DIR", "/data")) / "secrets"


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
        run_id = make_run_id("OAUTH", 1,
                             dev_type.harness_template.split("-")[0].upper())
        run = Run(run_id=run_id, mission_key="OAUTH", mission_type="OAUTH",
                  dev_type=dev_type.name, seq=1, timeout_seconds=600,
                  spec_env={"DEVCAKE_OAUTH_MODE": dev_type.harness_template,
                            "DEVCAKE_OAUTH_LOGIN_CMD": flow.login_cmd,
                            "DEVCAKE_OAUTH_AUTH_PATH": flow.auth_path})
        await self.runs.bootstrap.launch(run, image=harness.image)
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
        target = SECRETS_DIR / s["dev_type"]      # snapshot from start(), never
        target.mkdir(parents=True, exist_ok=True)  # re-looked-up (see _start_inner)
        p = target / s["secret_file"]
        p.write_text(payload["content"])
        p.chmod(0o600)
        s["state"] = "completed"
        run.state = "finished"
        self.runs.store.save(run)
        await self.messaging.delete_run_user(run_id)
        await self.messaging.delete_reply_stream(run_id)
        # a fresh credential clears any auth breaker for this dev type (docs/15 §4)
        if self.breakers is not None:
            self.breakers.pop(s["dev_type"], None)
        log.info("oauth: %s credential stored for %s", s["harness"], s["dev_type"])

    def status(self, run_id: str) -> Optional[dict]:
        return self.sessions.get(run_id)
