"""Startup reconciliation steps 3–4 (docs/04 §6), factored out of the API
lifespan so the ordering contract is unit-testable (ISSUES #26)."""

import logging
import re

log = logging.getLogger("devcake.reconcile")


def _restamp_store_gen(store, run) -> None:
    """Bind a kept-alive run into THIS process's wipe generation (docs/10).

    ``wipe_generation`` is process-local and resets to 0 on restart, but run
    files may still carry ``store_gen`` from a prior process (e.g. 2 after
    two clears). Without restamping, the first clear in the new process
    bumps wipe 0→1 and ``_pre_wipe(store_gen=2, wipe=1)`` is false — so
    in-flight finalize after that clear can still post to the PMO and
    resurrect the record. Orphaned runs are not restamped (they are
    terminalled via kill and leave the active set).
    """
    wipe_gen = int(getattr(store, "wipe_generation", 0) or 0)
    if int(getattr(run, "store_gen", 0) or 0) == wipe_gen:
        return
    prev = getattr(run, "store_gen", 0)
    run.store_gen = wipe_gen
    store.save(run)
    log.info("reconciliation: restamped store_gen %s → %s for %s",
             prev, wipe_gen, run.run_id)


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
            # keep for reclaim, but born into this process's wipe generation
            # so a later clear-runs correctly treats it as pre-wipe
            _restamp_store_gen(store, r)
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
                # enrich the classified exits (13 clone/forge, 14 MCP setup,
                # 15 harness fault, 16 turn budget) when the app was down at
                # container death. Dagu's node-error string is the only
                # post-mortem source, so it can recover the numeric code but
                # never the structured `error_class` — dev_failure_error's 15
                # arm therefore labels the orphan and lets it contribute
                # evidence, but never excuses its attempt (ADR-0018).
                exit_m = re.search(r"exit status (13|14|15|16)", detail.lower())
                if finalizer and exit_m:
                    r.error = finalizer.dev_failure_error(
                        r, {"exit_code": int(exit_m.group(1)),
                            "error_detail": detail})
                    store.save(r)
            else:
                _restamp_store_gen(store, r)
                log.info("reconciliation: adopted in-flight run %s (dagu: %s)",
                         r.run_id, label or st or "running")
        except Exception:
            log.exception("reconciliation failed for %s", r.run_id)
    try:
        await messaging.reclaim_pending(manager.handle, manager.verify_auth)  # step 4
    except Exception:
        log.exception("pending-entry reclaim failed")
