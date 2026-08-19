"""Startup reconciliation steps 3–4 (docs/04 §6), factored out of the API
lifespan so the ordering contract is unit-testable (ISSUES #26)."""

import logging
import re

from . import failure_taxonomy
from .run import utcnow

log = logging.getLogger("devcake.reconcile")

# ADR-0027: the recoverable-code list is a table derivation, not a hand-list.
# Exit 12's absence is a table FIELD (orphan_recoverable=False on DEV_AUTH) —
# see the enrichment comment below for why — and test_crash_recovery pins the
# membership (10/11/20 recovered, 12 refused) independently of this regex.
_EXIT_RE = re.compile(r"exit status (%s)" % "|".join(
    str(c) for c in failure_taxonomy.ORPHAN_RECOVERABLE_EXIT_CODES))

# Terminal Dagu run labels/statuses. One set for reconcile + watchdog so a
# cancel* spelling cannot be a ghost on boot and a kill mid-flight (docs/04
# §5–6). "cancel" substring covers cancelled/canceled without listing both.
_DEAD_DAGU = frozenset({"failed", "aborted", "error", "cancelled", "canceled"})


def dagu_run_not_alive(status: dict | None) -> bool:
    """True when executor.status is missing (404) or the Dagu run is terminal.

    Shared by reconcile (boot orphan) and the watchdog liveness branch so
    dead-status membership cannot drift between the two kill paths.
    """
    if status is None:
        return True
    detail = (status.get("dagRunDetails") or {})
    st = str(detail.get("status", "")).lower()
    label = str(detail.get("statusLabel", "")).lower()
    if st in _DEAD_DAGU or label in _DEAD_DAGU:
        return True
    # belt: unknown "…cancel…" labels (Dagu stop variants) still count as dead
    return "cancel" in st or "cancel" in label


def _restamp_store_gen(store, run) -> None:
    """Bind a kept-alive run into THIS process's wipe generation (docs/10).

    ``wipe_generation`` is process-local and resets to 0 on restart, but run
    files may still carry ``store_gen`` from a prior process (e.g. 2 after
    two clears). Restamp keeps the on-disk stamp aligned with this process
    so heartbeats and adoption stay current before any clear. Anti-
    resurrection after clear is fail-closed at the store fence itself
    (``is_current_generation``: after wipe_generation ≥ 1 only an exact
    stamp match may save) — restamp is no longer the sole defense against
    prior-process stamps, but it still avoids carrying dead high stamps
    into the active set. Orphaned runs are not restamped (they are
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

    Also skip (promote to finalizing) when Dagu is dead but unresolved
    messaging still holds the run's entry — docs/04 §6 "artifacts present →
    resume finalization"; orphaning first then reclaim would hit the
    terminal-state guard and drop a first delivery (CAKE-73).

    `manager` is the RunManager — it already carries the store, executor,
    messaging, and finalizer (RunFinalizer seam) this needs; separate params
    would let callers wire mismatched objects.
    """
    store, executor = manager.store, manager.executor
    messaging, finalizer = manager.messaging, manager.finalizer
    # Fail-closed: if the probe fails we cannot prove the ingress is empty,
    # so we must not orphan (CAKE-73). None ⇒ unknown; skip orphan this boot.
    try:
        unresolved: set[str] | None = await messaging.unresolved_run_ids()
    except Exception:  # noqa: BLE001 — messaging probe fail-closed: prefer leave-for-reclaim over orphaning a first delivery we cannot rule out
        log.exception(
            "reconciliation: unresolved_run_ids failed — skipping orphan "
            "kills this boot (fail-closed)")
        unresolved = None
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
            if dagu_run_not_alive(status):
                if unresolved is None or r.run_id in unresolved:
                    if unresolved is not None:
                        # docs/04 §6 + CAKE-73: artifacts pending → resume finalize
                        r.state = "finalizing"
                        store.save(r)
                        log.info(
                            "reconciliation: leaving dead-Dagu run %s for "
                            "reclaim (artifacts still on ingress)",
                            r.run_id)
                    else:
                        log.info(
                            "reconciliation: skipping orphan of %s "
                            "(unresolved probe failed)",
                            r.run_id)
                    _restamp_store_gen(store, r)
                    continue
                try:
                    node_errors = await executor.node_errors(r.run_id) if status else []
                except Exception:  # noqa: BLE001 — error-detail probe is optional enrichment; empty detail is the safe default, the orphan kill proceeds
                    node_errors = []
                await manager.kill(r, "orphaned", "reconciliation: dagu run not alive")
                # Enrich only a run that is still orphaned on disk — if kill
                # aborted (state moved / wipe), do not scribble error_class
                # onto a live or missing record.
                fresh = store.get(r.run_id)
                if fresh is None or fresh.state != "orphaned":
                    continue
                detail = " ".join(str(item.get("error") or "") for item in node_errors)
                # enrich the classified exits (13 clone/forge, 14 MCP setup,
                # 15 harness fault, 16 turn budget) when the app was down at
                # container death. Dagu's node-error string is the only
                # post-mortem source, so it can recover the numeric code but
                # never the structured `error_class` — dev_failure_error's 15
                # arm therefore labels the orphan and lets it contribute
                # evidence, but never excuses its attempt (ADR-0018).
                # AUD-015: enrich the informational exit classes too (10/11/20
                # → DEV_CRASH / DEV_BAD_OUTPUT, incl. the ADR-0025 exit-20
                # sentinel/marker family), not just 13-16. Exit 12 is
                # DELIBERATELY excluded — dev_failure_error latches the dev-type
                # auth breaker for it, and a stale orphan post-mortem must never
                # trip a breaker from reconcile.
                exit_m = _EXIT_RE.search(detail.lower())
                if finalizer and exit_m:
                    fresh.error = finalizer.dev_failure_error(
                        fresh, {"exit_code": int(exit_m.group(1)),
                                "error_detail": detail})
                    store.save(fresh)
            else:
                _restamp_store_gen(store, r)
                # docs/04 §6: Dagu running → mark running. A crash that lost
                # run.started left the record at dispatched; without promotion
                # heartbeat liveness never arms (state must be running) and a
                # healthy container only dies at wall-clock timeout.
                if r.state == "dispatched":
                    r.state = "running"
                    if r.started_at is None:
                        r.started_at = utcnow()
                    store.save(r)
                    log.info("reconciliation: promoted dispatched→running %s",
                             r.run_id)
                else:
                    detail = (status or {}).get("dagRunDetails") or {}
                    label = str(detail.get("statusLabel",
                                           detail.get("status", ""))).lower()
                    log.info("reconciliation: adopted in-flight run %s (dagu: %s)",
                             r.run_id, label or "running")
        except Exception:
            log.exception("reconciliation failed for %s", r.run_id)
    try:
        await messaging.reclaim_pending(manager.handle, manager.verify_auth)  # step 4
    except Exception:
        log.exception("pending-entry reclaim failed")
    # ADR-0025 Hook G (boot half): reclaim leaked workspace dirs from runs
    # that terminalled while the app was down. Safe by construction — the
    # lifespan runs reconcile before the poll/ingress/watchdog tasks start
    # and before uvicorn serves, so no dispatch can race it.
    try:
        manager.workspaces.sweep(manager.store)
    except Exception:
        log.exception("boot workspace sweep failed")
