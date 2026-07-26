"""RunFinalizer — mission-side finalization and restore (docs/04 §4).

Breaks the RunManager ↔ MissionManager concrete cycle: ingress and kill depend
only on this interface. MissionManager is the production adapter; tests inject
fakes (two adapters → real seam).
"""

from __future__ import annotations

from typing import Protocol

from ..domain.run import Run


class RunFinalizer(Protocol):
    async def finalize(self, run: Run, payload: dict) -> None: ...
    async def finalize_mapper(self, run: Run, payload: dict) -> None: ...
    async def restore_after_failure(self, run: Run) -> None: ...
    def runspec_secret_payload(self, run: Run) -> dict | None: ...
    # takes the RUN (not a bare pmo_id): the finalizer may be a router that
    # needs run.pmo_ref to pick the owning PMO instance (schema v3)
    async def activity_payload(self, run: Run) -> dict: ...

    def dev_failure_error(self, run: Run, payload: dict) -> str:
        """Classify a Dev-container failure artifact (exit code + detail) into
        the run.error string. Used by startup reconciliation to enrich
        pre-harness orphans (domain/reconcile.py).

        MUTATES the run as a side effect (ADR-0018): every implementation must
        stamp `run.error_class`, and the exit-15 path also sets
        `run.attempt_counted`. It may additionally trip auth/forge breakers.
        A fake that returns a bare string leaves `error_class == ""`, which
        silently exercises the legacy pre-upgrade branch of
        `dispatch.counts_toward_attempts` instead of the real one.
        """
        ...
