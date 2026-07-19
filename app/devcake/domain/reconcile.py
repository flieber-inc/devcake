"""Startup reconciliation steps 3–4 (docs/04 §6), factored out of the API
lifespan so the ordering contract is unit-testable (ISSUES #26)."""

import logging
import re

log = logging.getLogger("devcake.reconcile")


async def reconcile_runs(manager) -> None:
    """Step 3: orphan dead Dagu runs. Skip state=="finalizing" — those may have
    pending run.artifacts on the ingress stream; killing them to orphaned
    before reclaim would either re-finalize after orphan (ISSUES #1+#2) or
    drop mid-finalize work once terminal redelivery is a no-op. Reclaim
    (step 4) resumes them.

    `manager` is the RunManager — it already carries the store, executor,
    messaging, and finalizer (RunFinalizer seam) this needs; separate params
    would let callers wire mismatched objects.
    """
    store, executor = manager.store, manager.executor
    messaging, finalizer = manager.messaging, manager.finalizer
    for r in store.active():
        if r.state == "finalizing":
            log.info("reconciliation: leaving finalizing run %s for reclaim",
                     r.run_id)
            continue
        try:
            status = await executor.status(r.run_id)
            st = str(((status or {}).get("dagRunDetails") or {}).get("status", "")).lower()
            label = str(((status or {}).get("dagRunDetails") or {}).get("statusLabel", "")).lower()
            if status is None or st in ("failed", "aborted", "error") \
                    or label in ("failed", "aborted", "error", "cancelled"):
                try:
                    node_errors = await executor.node_errors(r.run_id) if status else []
                except Exception:  # noqa: BLE001 — error-detail probe is optional enrichment; empty detail is the safe default, the orphan kill proceeds
                    node_errors = []
                await manager.kill(r, "orphaned", "reconciliation: dagu run not alive")
                detail = " ".join(str(item.get("error") or "") for item in node_errors)
                # enrich the classified pre-harness exits (13 clone/forge,
                # 14 MCP setup) when the app was down at container death
                exit_m = re.search(r"exit status (13|14)", detail.lower())
                if finalizer and exit_m:
                    r.error = finalizer.dev_failure_error(
                        r, {"exit_code": int(exit_m.group(1)),
                            "error_detail": detail})
                    store.save(r)
            else:
                log.info("reconciliation: adopted in-flight run %s (dagu: %s)",
                         r.run_id, label or st or "running")
        except Exception:
            log.exception("reconciliation failed for %s", r.run_id)
    try:
        await messaging.reclaim_pending(manager.handle, manager.verify_auth)  # step 4
    except Exception:
        log.exception("pending-entry reclaim failed")
