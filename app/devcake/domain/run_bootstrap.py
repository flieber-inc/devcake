"""RunBootstrap — deep module for the dispatch spine (docs/04 §3.1).

Owns the invariant: create per-run ACL user → set auth_digest → durable
store.save → executor.start. Every dispatch flavor (hello, mission, steward,
oauth) should call launch(); callers own mission-specific fields and spans.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import contextlib

from .run import Run, auth_digest
from .workspaces import NullWorkspaceStore, WorkspaceUnavailable

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
        # oauth / steward-run-now / hello paths, which bypass it. A run
        # launched just BEFORE the wipe grabs the lock is already in
        # store.save (below) → the drain's active() snapshot catches it.
        self.dispatch_lock = asyncio.Lock()

    async def launch(self, run: Run, *, image: str) -> Run:
        """Persist durable intent, then trigger the executor.

        Mutates ``run.auth_digest`` from the freshly created ACL password.
        Callers must fully populate mission/dev fields (and optional
        ``traceparent``) before calling. Raises ``WorkspaceUnavailable`` if
        the workspace base cannot host the run (AUD-001) — BEFORE any
        container starts and AFTER any partial side effect is unwound, so
        the caller gates cleanly (no attempt burned, no phantom record).
        """
        # AUD-001/002: a persistent unusable base (root-owned mount → the
        # boot writability probe latched `volume_error`) fails fast BEFORE
        # any ACL user or durable record — nothing to unwind. This is the
        # fail-closed precondition the "dispatch is frozen" alert promises.
        if self.workspaces.volume_error:
            raise WorkspaceUnavailable(self.workspaces.volume_error)
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
            try:
                self.workspaces.create(run.run_id)
            except OSError as e:
                # AUD-001: a transient create failure (disk full mid-op, a
                # race) AFTER the save must not strand a `dispatched` record +
                # ACL user for the watchdog to kill 90 s later (burning an
                # attempt and, via the raise, degrading the whole instance).
                # Unwind both, then gate like the pre-check above.
                self.store.delete(run.run_id)
                with contextlib.suppress(Exception):
                    await self.messaging.delete_run_user(run.run_id)
                raise WorkspaceUnavailable(
                    f"workspace create failed: {e}") from e
            # An executor.start failure deliberately does NOT rm here — a
            # start timeout/409 is ambiguous, and the watchdog's STARTUP_GRACE
            # kill reaches Hook B within ~2 minutes either way.
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
