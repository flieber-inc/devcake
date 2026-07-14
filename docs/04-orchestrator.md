# 04 — Orchestrator: Scheduling, Atomicity, and Recovery

> **Audience:** implementers. This is the most correctness-sensitive document in the set.
> **Depends on:** `00-overview.md` (INV-1…6), `02-domain-model.md` (derivation table, Run, AppConfig).

The orchestrator is the always-on core of the main app. **Implementation** lives under `app/devcake/domain/orchestrator/` as a package (ISSUES #36): `MissionManager` remains the public façade; schedule, dispatch, finalize, transitions, review, decomposition, sweeps, feed policy, and MAPPER mission ops are focused modules. `MapperService` (cadence) stays in `domain/mapper_service.py`. Wiring and the three loops live in `api/main.py`.

It runs three cooperating loops on one asyncio event loop:

1. **Poll loop** — refresh the world from the PMO System, schedule and dispatch work.
2. **Ingress consumer** — consume Dev messages from Redis and finalize runs (`09-messaging.md`). Also feeds the live run-log store: `run.log {lines}` batches are redacted and appended to `/data/state/runlogs/{run_id}.log`, which the admin panel's run terminal follows over SSE (`11-admin-panel.md` §4). Finalization and every kill path close the log's live followers (end-of-stream sentinel).
3. **Watchdog** — enforce the Dev timeout and detect orphaned runs.

## 1. Poll cycle

Every `poll_interval_seconds` (default 30):

1. Fetch all non-terminal Projects and Issues in the configured team via `PMOPort.list_all(team_ref)` and normalize to `Mission` DTOs.
2. Derive each Mission's type per the table in `02-domain-model.md` §2.
3. **Project auto-completion sweep:** for each Project carrying `DEVCAKE-TRACKING`, check its child Issues; if all are `done`/`canceled` (and it has ≥1 child), set the Project's status to `done`, remove `DEVCAKE-TRACKING`, and post a completion comment. (State is derived entirely from the PMO — no local tracking.)
4. **Merge sweep:** for each Mission carrying `DEVCAKE-MERGE`, check its PR via the forge adapter (`get_pr_by_branch`/`pr_state`, which return normalized `PullRequest` DTOs — attribute access, never raw forge JSON) — merged → remove the label, status `done`; closed unmerged → remove the label, status `canceled`; either way with a comment (`03-mission-lifecycle.md` §4.1). While the PR is still open and a deferred-merge retry window is active (`auto_merge` ON, latest merge-state feed marker is `` `devcake:merge-retry` ``), the sweep also drives the retry: `mergeable()` each cycle — ready → merge and complete; conflict → conflict-rework routing; computing/CI → wait; window elapsed → terminal hand-off, once. Done is only ever declared after the merge is real.
5. Run the scheduling algorithm (§3) over the derived candidates — **unless `intake_paused`** (`02-domain-model.md` §9): while paused, steps 1–4 and 6 still run (sweeps, health, snapshot), the ingress consumer still finalizes in-flight runs, and the watchdog still enforces timeouts; only NEW dispatches (missions and MAPPER runs) are withheld.
6. **Relations Mapper cadence (`MapperService`):** when `relations_mapper.enabled` with a valid `dev_type`, no MAPPER run active, `interval_minutes` elapsed, and the service not **degraded** (3 most recent MAPPER runs all dead — store-derived, restart-safe) → dispatch a MAPPER run (`03-mission-lifecycle.md` §4b). One lock serializes this with the manual "Run now" endpoint; the watermark advances only after a successful dispatch. MAPPER runs count toward `global_max`.
7. Refresh the in-memory missions snapshot served by `GET /api/v1/missions` (advisory only, rebuilt every cycle) and emit the `poll.cycle` span with counts (`12-observability.md`).

A poll cycle that fails on a PMO transient error is skipped after retries (`15-errors-and-retries.md`, `PMO_TRANSIENT`) — the next cycle starts fresh; nothing is lost because nothing local is authoritative.

## 2. Candidate filtering

`candidates` = derived Missions **excluding** any that:

- fail the opt-in adoption gate, or have no derivable type (rows 5–11 of the derivation table: terminal, conflict, `DEVCAKE-SKIP`, `DEVCAKE-FAILED`, in-progress-without-label, awaiting-merge, `DEVCAKE-NEEDS-HUMAN`);
- have an active local Run in state `dispatched | running | finalizing` (in-flight guard — this is bookkeeping, not a lock: if `/data/state` is wiped, the reconciliation in §6 rebuilds it from the Dagu API before the first cycle);
- were transitioned by *us* within the last poll cycle (**grace cycle**): the app consults its own `events.jsonl` audit log and treats any Mission it wrote to in the previous cycle as busy for one cycle, absorbing the PMO's read-after-write staleness (resolves G5);
- have an **open blocker** (`adr/0007`): any Mission in `blocked_by` whose normalized status is not `done`/`canceled`. The check resolves blockers against the poll snapshot (terminal Missions included, so done blockers resolve); a blocker outside the snapshot is live-fetched once per cycle (memoized), and a blocker that cannot be read counts as open — fail-safe, self-healing next cycle. A blocker carrying `DEVCAKE-FAILED`/`DEVCAKE-SKIP` is still open: the prerequisite will not complete autonomously, so dependents stay parked and the reason string names the guard label (surfaced in `/api/v1/missions` — this makes blocked-on-a-dead-blocker deadlocks visible). The gate is re-verified live at dispatch (§3.1). Because it honors *any* blocked-by relation, humans steer ordering by adding/removing relations in the PMO UI — no DevCake-specific knowledge needed.

**The gate is a poll artifact (`MissionManager.gate_map`), not a scheduling side effect:** the poll loop computes it EVERY cycle — paused or not — and both `schedule()` and the `/api/v1/missions` snapshot consume the same map. Pause freezes dispatch, never information: relations edited in Linear during a pause are reflected within one poll interval.

**Dependency-cycle detection (§2a):** `gate_map` runs a pure cycle finder (`find_cycles`, `domain/model.py`) over the snapshot's blocked-by graph. A cycle is an *unsatisfiable* wait — every member is parked until a human deletes a relation — so members get the explicit reason `dependency cycle: A → B → A — will never unblock; delete one relation in Linear` instead of ordinary blocking, `/api/v1/health` reports `dependency_cycles`, and the admin header shows an amber banner. Nothing prevents a human from creating a cycle (the PMO accepts both relations); DevCake's job is to make the deadlock unmistakable.

> **Philosophy — blocking is deliberately pipeline-coarse.** A blocked Mission does not ONBOARD, PLAN, or anything else until its blockers are done. Better bottlenecked by a single well-ordered lane than accumulating parallel garbage: routing quality is the product thesis (founder decision 2026-07-12).

## 3. Scheduling algorithm (normative pseudocode)

```
def schedule(candidates, config, dev_types, active_runs):
    order = sort(candidates,
                 key = (priority_rank,            # urgent=0, high=1, medium=2, low=3
                        updated_at ascending,     # oldest first
                        pmo_id))                  # deterministic final tiebreak
    for mission in order:
        dev_type = config.assignments[mission.type]
        if active_runs.count(dev_type=dev_type) >= dev_types[dev_type].max_concurrency:
            continue
        if active_runs.count() >= config.concurrency.global_max:
            break                                  # global cap saturated; stop
        dispatch(mission, dev_type)
```

Properties:

- The effective ceiling is min(`global_max`, Σ per-type caps) — it falls out of the two checks; there is no separate rule.
- Priority and labels used in the sort come from the poll snapshot, but are **re-verified live at dispatch** (§3.1) so a stale snapshot can never dispatch wrong work (INV-1).

### 3.1 Dispatch (ordered, crash-safe)

```
def dispatch(mission, dev_type):
    live = pmo.get(mission.ref)                    # live re-read: INV-1, INV-3
    if derive_type(live) != mission.type: return   # world moved on; skip silently
    if open_blockers(live): return                 # blocked-by re-check, live (§2)
    run = Run(run_id=f"{mission.key}-{seq}-{mission.type}-{ulid()[-6:]}",   # 02 §7
              state="dispatched",
              seq=derive_seq(live),                # 02-domain-model.md §8
              stage_label_at_dispatch=stage_label(live), ...)
    run.spec = resolve_run_spec(mission, dev_type) # stage-2 env + credential refs (07 §3)
    write_run_file(run)                            # (1) durable intent BEFORE side effects
    dagu.start_dag("dev-run",                      # (2) trigger executor — non-secret params only
                   params={"RUN_ID": run.run_id,   #     (Dagu params are UI-visible, 13 §4);
                           "IMAGE": image_tag,  # e.g. devcake/dev-claude-code:latest
                           "TRACEPARENT": traceparent},
                   dag_run_id=run.run_id)
    if live.status == "backlog":
        pmo.set_status(mission.ref, "in_progress")      # (3) reflect pull in PMO
        audit_log.append(...)                      # feeds the grace cycle (§2)
```

Writing the Run file *before* the Dagu trigger means a crash between (1) and (2) leaves a `dispatched` Run with no Dagu counterpart — repaired by startup reconciliation (§6), never dispatched twice (a blind re-trigger with the same `dag_run_id` would 409 regardless — verified, `13-deployment.md` §4).

**Failure symmetry rule (added at M3):** every side effect of a dispatch whose run then fails must be reverted so the mission re-derives exactly as before the attempt. Concretely, step (3)'s backlog→in_progress write is undone on `failed`/`timed_out`/`orphaned` runs — after a live re-read confirming a human hasn't moved the mission meanwhile. The watchdog's liveness reference is `last_heartbeat or started_at` and Devs send their first heartbeat immediately, so a Dev killed in its first seconds is detected within the heartbeat grace, not at the wall-clock timeout (both gaps found and fixed live at M3).

The run spec (stage-2 env, credentials, repo info) is fully resolved by the app at dispatch and served to the Dev over `runspec.get` (`09-messaging.md` §3); Dagu receives only non-secret params and executes with zero business logic (app is the brain, Dagu is muscle).

## 4. No-lock atomicity: compare-and-transition

There are no locks, leases, or checkouts (INV-3). The only synchronization primitive is the PMO System's own state, applied at **finalization** — after the ingress consumer receives the Dev's `run.artifacts` message (`09-messaging.md`).

Finalization side-effect order is fixed:

1. **Post transcript** — upload `{seq}_{TYPE}.md` (comment, or file attachment when > ~50 KB — `05-pmo-adapter.md` §4). *Idempotent:* skip if an artifact with that exact name already exists in the feed.
2. **Post token report** — the accompanying cost message (INV-5). *Idempotent:* keyed by run_id embedded in the message footer.
3. **Compare-and-transition:**

```
def compare_and_transition(run, intended: Transition):
    live = pmo.get(run.mission_ref)                     # re-read, live
    if stage_label(live) != run.stage_label_at_dispatch:
        # A human (or another actor) changed state mid-run.
        pmo.post_feed(run.mission_ref,
            "DevCake completed a {type} run, but the mission's state was changed "
            "externally while it ran. Its output is posted above; no status/label "
            "changes were applied.")
        return EXTERNAL_TRANSITION                       # not an error; 15-errors §
    pmo.swap_labels(run.mission_ref,
                    remove=intended.remove, add=intended.add)   # single adapter call
    if intended.status: pmo.set_status(run.mission_ref, intended.status)
    audit_log.append(...)
```

4. **Forge side effects** (EXECUTE/REVIEW only): PR comments/approval per `03-mission-lifecycle.md` and `06-forge-adapter.md`. **Exception to the ordering:** on REVIEW-approve, the merge (when `auto_merge` is on) runs *before* the status transition — Done is only declared after a real merge; a failed merge lands on `DEVCAKE-MERGE` instead (`03-mission-lifecycle.md` §4.1).

Each completed side effect is appended to `run.finalized_steps` and the Run file rewritten (atomic tmp+rename), so a crash mid-finalization resumes exactly where it stopped — already-done steps are skipped by their idempotency keys.

Because the *label swap is the last PMO mutation* and scheduling only derives work from live labels, a Mission can never be worked twice concurrently: until the swap lands, the Mission still derives as its old type, and the in-flight guard + grace cycle (§2) keep it out of candidates; after the swap, it derives as the next type.

## 5. Watchdog

Runs every 10 s over all Runs in `dispatched | running`:

- **Timeout:** if `now - started_at > dev_timeout_minutes` (default 120), kill the run via Dagu's stop endpoint — `POST /api/v1/dag-runs/dev-run/{run_id}/stop` (verified: SIGTERM → SIGKILL after `max_clean_up_time_sec` → container force-removed, run `aborted`) — and mark the Run `timed_out`. Counts as a failed attempt (`15-errors-and-retries.md`, `DEV_TIMEOUT`). Dagu-side timeouts are intentionally not relied on — the app owns the kill (single owner; `13-deployment.md` §4 sets a belt-and-suspenders `timeout_sec` well above the app's). The app needs no `docker.sock` for this.
- **Liveness:** a Run in `running` whose last `run.heartbeat` (`09-messaging.md`) is older than 5 minutes triggers a Dagu run-status query; a non-running Dagu status ⇒ Run `failed` (`DEV_CRASH`).
- **Mission terminal-state check:** if the Run's Mission has gone `done`/`canceled` in the PMO mid-run (human decision), **kill the run immediately** (saves tokens), mark it `failed` with `EXTERNAL_TRANSITION`, skip all finalization transitions (artifacts are still persisted locally and to logs).
- **Finalize-stall backstop:** Runs in `finalizing` are never wall-clock-killed while their `run.artifacts` entry can still be redelivered (crash+reclaim resumes them). But if a run is past `timeout_seconds + DEVCAKE_FINALIZE_STALL_SECONDS` (default 3600) **and** its entry is no longer on the ingress stream (poison dead-lettered per `15-errors-and-retries.md` §5, or lost), nothing can ever finish it — the watchdog fails it without re-entering finalize, so it stops blocking its mission and holding a concurrency slot.

## 6. Startup reconciliation (ordered checklist)

On every app boot, before the first poll cycle:

1. Load config; validate (`CONFIG_INVALID` blocks startup with a clear admin-panel health error).
2. Ensure the ten managed labels exist in the configured team (`05-pmo-adapter.md` §5).
3. **Reconcile Runs:** for each local Run in a non-terminal state, query the Dagu run-status API for `run_id`:
   - Dagu says finished + artifacts message present in Redis (pending-entries reclaim, `09-messaging.md` §5) → resume finalization from `finalized_steps`.
   - Dagu says running → adopt it: mark `running`, resume watchdog coverage.
   - Dagu says failed/aborted, or has no such run → mark `orphaned`; counts as a failed attempt; the Mission (whose label never advanced — INV-3) reschedules naturally.
   - A `dispatched` Run with no Dagu run → the §3.1 crash window; mark `orphaned` (never re-trigger blindly — a duplicate `dagRunId` trigger returns 409 `already_exists`, verified).
4. Reclaim pending Redis messages older than the consumer's dead-time (XAUTOCLAIM) and process them.
5. Start the poll, ingress, and watchdog loops.

## 7. Crash matrix (summary)

| Failure | Effect | Recovery |
|---|---|---|
| App crashes mid-poll | Nothing dispatched twice (Run file precedes trigger) | §6 reconciliation |
| App crashes mid-finalization | Some side effects applied | `finalized_steps` + idempotency keys resume the rest |
| Dev container crashes | Mission label untouched (INV-3); **the dispatch-time backlog→in_progress status write is reverted** (live compare first — human edits win), else a failed first ONBOARD would strand the mission at derivation row 9 (verified at M3) | Run `failed`; attempt++; reschedules next cycle; after `max_attempts` → `DEVCAKE-FAILED` |
| Dev exceeds timeout | Watchdog kills container | Same as crash (`DEV_TIMEOUT`) |
| Redis down | Devs buffer/retry publishes with backoff; app consumer reconnects | Streams are durable (AOF); nothing lost; finalization is stream-driven |
| Linear down | Poll cycles skip; running Devs unaffected (they don't talk to PMO — INV-4) | Backoff per `PMO_TRANSIENT`; finalizations queue on the ingress until PMO is writable |
| `/data/state` wiped | Attempt counters and run history lost — nothing else (INV-1) | §6 rebuilds in-flight picture from the Dagu API and Redis |
