"""RunBootstrap — deep module for the dispatch spine (docs/04 §3.1).

Owns the invariant: create per-run ACL user → set auth_digest → durable
store.save → executor.start. Every dispatch flavor (hello, mission, mapper,
oauth) should call launch(); callers own mission-specific fields and spans.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from .run import Run, auth_digest
from .workspaces import NullWorkspaceStore

if TYPE_CHECKING:
    from ..ports.executor import ExecutorPort
    from ..ports.messaging import MessagingPort
    from ..ports.state import StatePort
    from .workspaces import WorkspaceStore


class RunBootstrap:
    def __init__(
        self,
        store: StatePort,
        messaging: MessagingPort,
        executor: ExecutorPort,
        workspaces: "WorkspaceStore | None" = None,
    ) -> None:
        self.store = store
        self.messaging = messaging
        self.executor = executor
        self.workspaces = workspaces or NullWorkspaceStore()
        # THE serialization point for every dispatch flavor (audit re-audit
        # #0/#6): clear-runs holds this lock across its whole wipe, so no run
        # can create a `dev-<run_id>` ACL user or start a container while the
        # ACL sweep is deleting `dev-*` — the poll-loop lock alone missed the
        # oauth / mapper-run-now / hello paths, which bypass it. A run
        # launched just BEFORE the wipe grabs the lock is already in
        # store.save (below) → the drain's active() snapshot catches it.
        self.dispatch_lock = asyncio.Lock()

    async def launch(self, run: Run, *, image: str) -> Run:
        """Persist durable intent, then trigger the executor.

        Mutates ``run.auth_digest`` from the freshly created ACL password.
        Callers must fully populate mission/dev fields (and optional
        ``traceparent``) before calling.
        """
        async with self.dispatch_lock:
            password = await self.messaging.create_run_user(run.run_id)
            run.auth_digest = auth_digest(password)
            # Stamp the process-local wipe generation so a later clear-runs
            # cannot be undone by this run's in-flight saves (docs/10).
            wipe_gen = int(getattr(self.store, "wipe_generation", 0) or 0)
            run.store_gen = wipe_gen
            self.store.save(run)  # durable intent BEFORE the trigger
            # ADR-0025 Hook C — pre-create AFTER the save (record-before-dir:
            # a dir whose name has no record is always garbage, which makes
            # the sweep predicate sound without locks) and BEFORE the start
            # (an absent bind source would be dockerd-created root-owned).
            # A create failure raises out of launch: no container, the
            # mission gates and retries next cycle. An executor.start
            # failure deliberately does NOT rm here — a start timeout/409 is
            # ambiguous, and the watchdog's STARTUP_GRACE kill reaches Hook
            # B within ~2 minutes either way.
            self.workspaces.create(run.run_id)
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
