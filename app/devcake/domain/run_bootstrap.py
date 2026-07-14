"""RunBootstrap — deep module for the dispatch spine (docs/04 §3.1).

Owns the invariant: create per-run ACL user → set auth_digest → durable
store.save → executor.start. Every dispatch flavor (hello, mission, mapper,
oauth) should call launch(); callers own mission-specific fields and spans.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .run import Run, auth_digest

if TYPE_CHECKING:
    from ..ports.executor import ExecutorPort
    from ..ports.messaging import MessagingPort
    from ..ports.state import StatePort

log = logging.getLogger("devcake.bootstrap")


class RunBootstrap:
    def __init__(
        self,
        store: StatePort,
        messaging: MessagingPort,
        executor: ExecutorPort,
    ) -> None:
        self.store = store
        self.messaging = messaging
        self.executor = executor

    async def launch(self, run: Run, *, image: str) -> Run:
        """Persist durable intent, then trigger the executor.

        Mutates ``run.auth_digest`` from the freshly created ACL password.
        Callers must fully populate mission/dev fields (and optional
        ``traceparent``) before calling.
        """
        password = await self.messaging.create_run_user(run.run_id)
        run.auth_digest = auth_digest(password)
        self.store.save(run)  # durable intent BEFORE the trigger
        await self.executor.start(
            params={
                "RUN_ID": run.run_id,
                "IMAGE": image,
                "TRACEPARENT": run.traceparent or "",
                "REDIS_USER": f"dev-{run.run_id}",
                "REDIS_PASSWORD": password,
            },
            dag_run_id=run.run_id,
        )
        return run
