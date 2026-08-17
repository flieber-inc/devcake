# 04 — Orchestrator: Scheduling, Atomicity, and Recovery

> **Audience:** implementers. This is the most correctness-sensitive document in the set.
> **Depends on:** `00-overview.md` (INV-1…6), `02-domain-model.md` (derivation table, Run, AppConfig).

The orchestrator is the always-on core of the main app. **Implementation** lives under `app/devcake/domain/orchestrator/` as a package (structure per ADR-0015 / ADR-0034). `MissionManager` (`manager.py`) is the DI container + advisory state + public verb surface, holding explicit delegating methods. Focused modules take the manager (`mgr`) as an explicit first parameter:

| Module | Role |
|---|---|
| `schedule.py` | candidate gate map + schedule order |
| `dispatch.py` | mission dispatch (prompt, attempts, repo resolution) |
| `finalize.py` | run finalize spine, checkpoints, `restore_after_failure` |
| `transitions.py` | outcome → label/status transitions |
| `review.py` | REVIEW finalize (approve/reject) |
| `freshness.py` | ADR-0031 gate on REVIEW's context-closing finalize (`docs/03` §4.1) + deferred-merge disclose |
| `completion.py` | merge-complete, conflict-resolve routing |
| `decomposition.py` | ONBOARD `decomposed` finalize |
| `discovery.py` | harvest / pending / discovery sweep (ADR-0033) |
| `deliver.py` | internal zip deliverable after merge |
| `sweeps.py` | `merge_sweep` / `tracking_sweep` |
| `feed.py` | comment posting choke-point (`_feed`), provenance helpers |
| `markers.py` | `LEGAL_OUTCOMES`, feed markers, conflict/freshness constants |
| `steps.py` | **checkpoint step-key registry** — sole authority for `finalized_steps` keys (ADR-0034) |
| `steward.py` | STEWARD dispatch/finalize + edge/discovery apply |
| `family_graph.py` | discovery family map |
| `activity_payload.py` | activity-folder mount payload |
| `router.py` | multi-instance `FinalizerRouter` (`RunFinalizer` over N managers) |

**Module-public seams** (sanctioned test surface — module-level functions, not private helpers): `transitions.transition`, `review.finalize_review`, `freshness.review_freshness_gate` / `disclose_unread_at_close`, `decomposition.finalize_decomposition`, `sweeps.merge_sweep` / `tracking_sweep`, `dispatch.attempt_number` / `resolve_repo` / `resolve_repo_live` / `decomposition_rule` / `steward_repo`, `steward.apply_steward_edges`, `discovery.harvest` / `discovery_sweep`, `completion.complete_merged` / `route_conflict_to_execute`. Cadence outside the package: `StewardService` in `domain/steward_service.py`; `CronService` (Scheduled Tasks — ADR-0035; fire ledger in `adapters/files/cron_store.py`) in `domain/cron_service.py`. Shared dispatch spine: `domain/run_bootstrap.py` `RunBootstrap.launch`. Composition is `api/services.build_services()`. The three loops are started from `api/main.py`'s lifespan: poll (`api/poll.py` `PollRuntime`), ingress (`adapters/redis/messaging.py`), watchdog (`domain/watchdog.py`). Each poll segment ends with `rotate_grace()` so a write this cycle is skipped next cycle.

It runs three cooperating loops on one asyncio event loop:

1. **Poll loop** — refresh the world from the PMO System, schedule and dispatch work; steward cadence and scheduled-task fires (ADR-0035) ride its tail segments.
2. **Ingress consumer** — consume Dev messages from Redis and finalize runs (`09-messaging.md`). Also feeds the live run-log store: `run.log {lines}` batches are redacted and appended to `/data/state/runlogs/{run_id}.log`, which the admin panel's run terminal follows over SSE (`11-admin-panel.md` §4). Finalization and every kill path close the log's live followers (end-of-stream sentinel).
3. **Watchdog** — enforce the Dev timeout and detect orphaned runs.

## 1. Poll cycle

Every `poll_interval_seconds` (default 30). **Multi-PMO:** the poll runtime walks **one `MissionManager` per configured PMO instance** (composition root builds the set from `config.pmos`); each segment uses that instance's adapter/team. A permanent failure on one instance is recorded in `poll_degraded` without stopping the others.

1. Fetch all non-terminal Projects and Issues in the instance's team via `PMOPort.list_all(team_ref)` and normalize to `Mission` DTOs.
2. Derive each Mission's type per the table in `02-domain-model.md` §2.
3. **Project auto-completion sweep:** for each Project carrying `DEVCAKE-TRACKING`, check its child Issues; if all are `done`/`canceled` (and it has ≥1 child), set the Project's status to `done` **then** remove `DEVCAKE-TRACKING` (status is the derive commit — same ordering as the merge sweep). **No completion comment** is posted — the status + label change is the signal. (State is derived entirely from the PMO — no local tracking.) The child read is a flat in-project filter, so second-level decomposition composes correctly: a child canceled in favor of its own sub-missions counts terminal, while the sub-missions — created inside the same Project (`adr/0012`) — hold it open until they finish. An empty child list never completes. If `children_of` raises (e.g. a project-kind ref on a `projects_supported=False` forge-issue adapter — port F1), the project is **not** completed and the reason is recorded in `blocked_reasons` for `/health` (not log-only).
4. **Merge sweep:** for each Mission carrying `DEVCAKE-MERGE`, check its PR via the forge adapter (`get_pr_by_branch`/`pr_state`, which return normalized `PullRequest` DTOs — attribute access, never raw forge JSON) — merged → status `done` **then** remove the label (status is the derive commit; a failed status leaves the label so the next cycle still selects); closed unmerged → cancel **then** remove the label; either way with a comment (`03-mission-lifecycle.md` §4.1). While the PR is still open and a deferred-merge retry window is active (that mission's repo has `auto_merge` ON, latest merge-state feed marker is `` `devcake:merge-retry` ``), the sweep also drives the retry: `mergeable()` each cycle — ready → merge and complete; conflict → conflict-rework routing; computing/CI → wait; window elapsed → terminal hand-off, once. Done is only ever declared after the merge is real.
5. Run the scheduling algorithm (§3) over the derived candidates — **unless intake is paused** (`02-domain-model.md` §9): the global `intake_paused` master freezes every instance; each `pmos[].intake_paused` freezes only that instance. While blocked, steps 1–4 and 6 still run (sweeps, health, snapshot), the ingress consumer still finalizes in-flight runs, and the watchdog still enforces timeouts; only NEW dispatches (missions and STEWARD runs) are withheld for the blocked instance(s).
6. **Relations Steward cadence (`StewardService`):** when `steward.enabled` with a valid `dev_type`, no STEWARD run active, `interval_minutes` elapsed, and the service not **degraded** (3 most recent STEWARD runs all dead — store-derived, restart-safe) → dispatch a STEWARD run (`03-mission-lifecycle.md` §4b). One lock serializes this with the manual "Run now" endpoint; the watermark advances only after a successful dispatch. STEWARD runs count toward `global_max`. The **discovery lane** (ADR-0033) rides the same segment **and the same lock** as the relations cadence and "Run now": event-kicked by harvest (pause-gated — the kick is not a back door) and re-driven by the label-gated sweep, it drains one family's pending discovery batches per pass when `pmos[].discovery_routing` is on. Per-instance single-flight (a sound coarsening of per-family; families never span instances), sharing `active()`/`global_max`/degradation. The watermark advances only after a successful dispatch (`None` from a skipped workspace is not success).
7. Refresh the in-memory missions snapshot served by `GET /api/v1/missions` (advisory only, rebuilt every cycle) and emit the `poll.cycle` span with counts (`12-observability.md`).
8. **Scheduled-task fires (`CronService.maybe_fire`, ADR-0035):** for every enabled `crons` row whose elapsed-interval window (persisted `last_fire_at` in `state/cron_outcomes.json`) is due and that is not degraded (last 3 automatic fires failed — ledger-derived, restart-safe; Run-now always works and a success re-arms), create ONE labeled ticket (`DEVCAKE` + stage label, `devcake:cron:v1 job=<id>` marker, single-flight per board, intake-pause honored). The reserved `memory-curator` row instead fans out one EXECUTE ticket per Curator board, skipping notebooks whose `.claims/` listing is empty. One outcome (`created`/`skipped`/`failed`) is recorded per fire window. Exceptions never kill the cycle.

A `PMOTransient` on one instance skips **only that instance's segment** for this cycle (`15-errors-and-retries.md`, `PMO_TRANSIENT`); other configured PMO instances still poll. The next tick re-attempts the sick instance — nothing is lost because nothing local is authoritative.

## 2. Candidate filtering

`candidates` = derived Missions **excluding** any that:

- fail the opt-in adoption gate, or have no derivable type (rows 5–11 of the derivation table: terminal, conflict, `DEVCAKE-SKIP`, `DEVCAKE-FAILED`, in-progress-without-label, awaiting-merge, `DEVCAKE-NEEDS-HUMAN`);
- have an active local Run in state `dispatched | running | finalizing` (in-flight guard — this is bookkeeping, not a lock: if `/data/state` is wiped, the reconciliation in §6 rebuilds it from the Dagu API before the first cycle);
- were transitioned by *us* within the last poll cycle (**grace cycle**): the app keeps an **in-memory** set (`MissionManager._grace` / `_grace_next`, rotated each poll cycle) of pmo_ids it wrote to and treats those as busy for one cycle, absorbing the PMO's read-after-write staleness — not a re-read of `events.jsonl`;
- have an **open blocker** (`adr/0007`): any Mission in `blocked_by` whose normalized status is not `done`/`canceled`. The check resolves blockers against the poll snapshot (terminal Missions included, so done blockers resolve); a blocker outside the snapshot resolves once per cycle (memoized) through the deployment-wide `BlockerLocator` — owner map, then same-system PEER instances when the local adapter declares `PMOCapabilities.global_ids` (ADR-0009 amendment), then the local adapter — so a native edge to a peer instance's mission gates identically to a local one; a blocker no path can read counts as open — fail-safe, self-healing next cycle. Cycle detection stays on the local instance graph (a cross-instance edge cannot close a local cycle). A blocker carrying `DEVCAKE-FAILED`/`DEVCAKE-SKIP` is still open: the prerequisite will not complete autonomously, so dependents stay parked and the reason string names the guard label (surfaced in `/api/v1/missions` — this makes blocked-on-a-dead-blocker deadlocks visible). **Blocked-by is re-verified live at dispatch** (§3.1); a live reopen surfaces the same `blocked by …` reason on the missions row. Because it honors *any* blocked-by relation, humans steer ordering by adding/removing relations in the PMO UI — no DevCake-specific knowledge needed. Treating a `canceled` blocker as satisfied stays sound under decomposition because inherited edges are **fail-closed**: the finalizer replicates a decomposed original's edges onto its children strictly before canceling it, and any edge failure keeps the original open and gating (`03-mission-lifecycle.md` §1.3, `adr/0012`) — dependents hand over from the original to the still-open children with no released window.
- are a **decomposition child whose issue parent is still open** (the family gate, `adr/0012`): the parent's cancel is finalization's *last* step, so an open issue-parent means the child's inherited and sibling edges may not all exist yet (or the parent is parked for a human) — the child waits, with the reason naming the parent. Parent trust is the same `markers.decomposition_parent_ref` chokepoint as `family_of` (`DEVCAKE-CREATED` gates the marker; parent= resolves by pmo_id or key). Project parents (which stay open by design under `DEVCAKE-TRACKING`) and parents missing from the snapshot are exempt. Enforced at `schedule` from the poll snapshot (not re-fetched at dispatch — see §3 Properties).

**The gate is a poll artifact (`MissionManager.gate_map`), not a scheduling side effect:** the poll loop computes it EVERY cycle — paused or not — and both `schedule()` and the `/api/v1/missions` snapshot consume the same map. Pause freezes dispatch, never information: relations edited in Linear during a pause are reflected within one poll interval.

**Dependency-cycle detection (§2a):** `gate_map` runs a pure cycle finder (`find_cycles`, `domain/model.py`) over the snapshot's blocked-by graph. A cycle is an *unsatisfiable* wait — every member is parked until a human deletes a relation — so members get the explicit reason `dependency cycle: A → B → A — will never unblock; delete one relation in Linear` instead of ordinary blocking, `/api/v1/health` reports `dependency_cycles`, and the SPA surfaces an amber alert (Overview + slim strip elsewhere). Nothing prevents a human from creating a cycle (the PMO accepts both relations); DevCake's job is to make the deadlock unmistakable.

> **Philosophy — blocking is deliberately pipeline-coarse.** A blocked Mission does not ONBOARD, PLAN, or anything else until its blockers are done. Better bottlenecked by a single well-ordered lane than accumulating parallel garbage: routing quality is the product thesis.

## 3. Scheduling algorithm (normative pseudocode)

```
def schedule(candidates, config, dev_types, active_runs):
    order = sort(candidates,
                 key = (priority_rank,            # urgent=0, high=1, medium=2, low=3
                        updated_at ascending,     # oldest first
                        pmo_id))                  # deterministic final tiebreak
    for mission in order:
        # instance override wholesale, else the global row (ADR-0019)
        dev_type = assignment_for(config, instance, mission.type)
        if active_runs.count(dev_type=dev_type) >= dev_types[dev_type].max_concurrency:
            continue
        if active_runs.count() >= config.concurrency.global_max:
            break                                  # global cap saturated; stop
        dispatch(mission, dev_type)
```

Properties:

- The effective ceiling is min(`global_max`, Σ per-type caps) — it falls out of the two checks; there is no separate rule.
- Priority and labels used in the sort come from the poll snapshot, but type/repo/blockers are **re-verified live at dispatch** (§3.1) so a stale snapshot can never dispatch wrong work (INV-1). The **family gate** (ADR-0012, open issue-parent) is enforced at `schedule` from the poll snapshot — an issue-parent cannot flip from open to finalized mid-cycle without the finalize that already completed wiring, so a second live parent fetch is not required for correctness.

### 3.1 Dispatch (ordered, crash-safe)

Mission-specific fields (prompt, attempts, stage label, PMO refs) are built by the caller (`MissionManager.dispatch`, `dispatch_steward`, hello, OAuth). The **shared spine** is `RunBootstrap.launch` (`domain/run_bootstrap.py`) — one deep module every dispatch flavor must use so ACL lifecycle, auth digest, durable intent, and executor start cannot drift apart. **`dispatch_lock`** serializes every flavor with clear-runs (poll alone is not enough — oauth / steward / hello bypass the poll lock). Clear-runs itself holds **`poll_rt.lock` and `bootstrap.dispatch_lock`** for the full wipe (order: poll then dispatch — matching the poll loop's own acquire order so it never deadlocks with an in-flight cycle). Launch also stamps `run.store_gen` from `RunStore.wipe_generation` so a later clear cannot be undone by in-flight saves (`10-persistence.md`).

```
def launch(run, *, image):                         # RunBootstrap — all four flavors
  if workspaces.volume_error:                         # (0) ADR-0025 fail-closed gate:
      raise WorkspaceUnavailable(...)                 #     BEFORE the lock and any side
      # effect — an unusable workspace base refuses cleanly; callers (dispatch,
      # steward) catch this and surface a blocked_reason; NO attempt burns, NO
      # poll_degraded (the AUD-001/002 fix — fail-closed for real)
  async with dispatch_lock:                        # clear-runs holds this for the full wipe
    password = messaging.create_run_user(run.run_id)  # MessagingPort (+ ACL SAVE, 09 §1a)
    run.auth_digest = sha256(password)                # never persist the raw ACL secret
    run.store_gen = store.wipe_generation             # clear-runs generation guard
    state.save(run)                                   # (1) durable intent BEFORE side effects
    try:
        workspaces.create(run.run_id)                 # (1b) per-run host-bind dir, 0700
    except OSError:                                   #      record-BEFORE-dir makes the
        state.delete(run); messaging.delete_run_user  #      sweep predicate sound; a create
        raise WorkspaceUnavailable(...)               #      failure UNWINDS record + ACL
    executor.start(params={                           # (2) ExecutorPort (Dagu in prod)
        "RUN_ID": run.run_id, "IMAGE": image,         #     non-secret params only (13 §4)
        "TRACEPARENT": run.traceparent or "",
        "REDIS_USER": f"dev-{run.run_id}",
        "REDIS_PASSWORD": password},
        dag_run_id=run.run_id)

def dispatch(mission, dev_type):                   # MissionManager — mission flavor only
    live = pmo.get(mission.ref)                    # live re-read: INV-1, INV-3
    if derive_type(live) != mission.type: return   # world moved on; skip silently
    if open_blockers(live): return                 # blocked-by re-check, live (§2)
    # PMO activity / live repo resolve failures gate THIS mission only (A1)

    run = Run(run_id=make_run_id(instance, mission.key, seq, mission.type),  # 02 §7 → {INSTANCE}-{key}-{seq}-{TYPE}-{suffix}
              state="dispatched",
              seq=derive_seq(live),                # 02-domain-model.md §8
              stage_label_at_dispatch=stage_label(live),
              spec_env=protocol_spec_env(...), ...)
    run.spec_prompt = resolve_prompt(mission, dev_type)  # includes {blocker_repos}
    run.blocker_work = resolve_blocker_work(...)         # ADR-0017: done blockers' repo_refs
    run.mirror_repos = needed                            # the mirror gate's proven set,
    #                                                      snapshotted at dispatch: the runspec
    #                                                      serves mirrors from THIS, never
    #                                                      from live config — 09 §3
    bootstrap.launch(run, image=harness_image(dev_type))
    # launch = workspace gate → ACL user → durable Run file → workspace dir
    # BEFORE the Dagu trigger (WorkspaceUnavailable at any of those steps is
    # an unschedulable reason, not a failed attempt); the image param is a
    # plain tag (e.g. devcake/dev-claude-code:latest — no digest pinning;
    # 13-deployment.md §6), and Dagu receives only non-secret params
    # runspec later attaches RO tokens for blocker_work as extra_repos
    if live.status == "backlog":
        pmo.set_status(mission.ref, "in_progress")      # (3) reflect pull in PMO
        audit_log.append(...)                      # feeds the grace cycle (§2)
```

Writing the Run file *before* the executor trigger means a crash between (1) and (2) leaves a `dispatched` Run with no Dagu counterpart — repaired by startup reconciliation (§6), never dispatched twice (a blind re-trigger with the same `dag_run_id` would 409 regardless — `13-deployment.md` §4).

**Failure symmetry rule:** every side effect of a dispatch whose run then fails must be reverted so the mission re-derives exactly as before the attempt. Concretely, step (3)'s backlog→in_progress write is undone on `failed`/`timed_out`/`orphaned` runs — after a live re-read confirming a human hasn't moved the mission meanwhile (`finalize.restore_after_failure`). Kill and failed finalization call `RunFinalizer.restore_after_failure` (`ports/finalizer.py`; production path = `MissionManager` via `FinalizerRouter` when multi-PMO) so `RunManager` never types against the concrete orchestrator. The watchdog's liveness reference is `last_heartbeat or started_at` and Devs send their first heartbeat immediately, so a Dev killed in its first seconds is detected within the heartbeat grace, not at the wall-clock timeout.

The run spec (stage-2 env, credentials, repo info) is fully resolved by the app at dispatch and served to the Dev over `runspec.get` (`09-messaging.md` §3); Dagu receives only non-secret params and executes with zero business logic (app is the brain, Dagu is muscle).

## 4. Mission atomicity without leases: compare-and-transition

There are no persistent per-Mission leases or checkouts (INV-3). The PMO
System's own state is the authoritative Mission-coordination primitive, applied
at **finalization** — after the ingress consumer receives the Dev's
`run.artifacts` message (`09-messaging.md`). Process-local locks serve a
different purpose: `poll_rt.lock` serializes poll cycles and
`RunBootstrap.dispatch_lock` serializes dispatch against Clear run history;
neither survives a crash or owns a Mission (`18-operator-contract.md` §3).
Ingress lives in `RunManager` (`domain/runs.py`); mission and STEWARD artifacts
are routed through the injected **`RunFinalizer`** (`finalize` /
`finalize_steward`); hello and other PMO-less runs use the local finalize
checklist. Composition binds `manager.set_finalizer(mission_mgr)` at boot
(`01-architecture.md` §3).

Finalization side-effect order is fixed:

1. **Post transcript** — for issues, **always** upload `{seq}_{TYPE}.md` as a file attachment with a short referencing comment (`05-pmo-adapter.md` §4). Upload failure falls back to an inline (blockquoted) post so INV-5 still holds. *Idempotent:* skip when `"transcript"` is already in `run.finalized_steps` (not by scanning the feed for the attachment name).
2. **Post token report** — the accompanying cost message (INV-5). *Idempotent:* `"token_report"` ∈ `run.finalized_steps` (and the run_id embedded in the message footer).
3. **Compare-and-transition:**

```
def compare_and_transition(run, intended: Transition):
    live = pmo.get(run.mission_ref)                     # re-read, live
    # EXTERNAL_TRANSITION redelivery: a live stage matching a checkpointed
    # swap marker of ours (_SWAP_MARKER_STAGE) is accepted as our own prior
    # work — not an external change. A human change between deliveries still
    # halts further mutation.
    if stage_label(live) not in expected_stages(run):
        # A human (or another actor) changed state mid-run.
        pmo.post_feed(run.mission_ref,
            "DevCake completed a {type} run, but the mission's state was changed "
            "externally while it ran. Its output is posted above; no status/label "
            "changes were applied.")
        return EXTERNAL_TRANSITION                       # not an error; 15-errors-and-retries.md §1
    pmo.swap_labels(run.mission_ref,
                    remove=intended.remove, add=intended.add)   # single adapter call
    if intended.status: pmo.set_status(run.mission_ref, intended.status)
    audit_log.append(...)
```

4. **Forge side effects** (EXECUTE/REVIEW only): PR comments/approval per `03-mission-lifecycle.md` and `06-forge-adapter.md`. **Exception to the ordering:** on REVIEW-approve, the merge (when the mission's repo has `auto_merge` on) runs *before* the status transition — Done is only declared after a real merge; a failed merge lands on `DEVCAKE-MERGE` instead (`03-mission-lifecycle.md` §4.1).

Each completed side effect is appended to `run.finalized_steps` (keys from `orchestrator/steps.py` only — ADR-0034) and the Run file rewritten (atomic tmp+rename), so a crash mid-finalization resumes exactly where it stopped — already-done steps are skipped by their step keys.

Because the *label swap is the stage-advancing PMO mutation* and scheduling only derives work from live labels, a Mission can never be worked twice concurrently: until the swap lands, the Mission still derives as its old type, and the in-flight guard + grace cycle (§2) keep it out of candidates; after the swap, it derives as the next type. (Feed posts and other non-stage side effects may still land after the swap on some paths — the scheduler keys on the stage label, not on "last write wins.")

## 5. Watchdog

Runs every 10 s over all Runs in `dispatched | running`:

- **Timeout:** if `now - created_at > timeout_seconds` (default from `dev_timeout_minutes` at dispatch, 120 min), kill the run via Dagu's stop endpoint — `POST /api/v1/dag-runs/dev-run/{run_id}/stop` (SIGTERM → SIGKILL after `max_clean_up_time_sec` → container force-removed, run `aborted`) — and mark the Run `timed_out`. Counts as a failed attempt (`15-errors-and-retries.md`, `DEV_TIMEOUT`). Ages are measured from **`created_at`**, not `started_at`. Dagu-side timeouts are intentionally not relied on — the app owns the kill (single owner; `13-deployment.md` §4 sets a belt-and-suspenders `timeout_sec` well above the app's). The app needs no `docker.sock` for this.
- **Liveness:** a Run in `running` whose last `run.heartbeat` (`09-messaging.md`) is older than 5 minutes triggers a Dagu run-status query; a non-running Dagu status ⇒ Run `failed` (`DEV_KILLED` — the kill-chokepoint catch-all; exit 10/20 `DEV_CRASH` is only stamped when a Dev failure artifact or orphan post-mortem carries that code). A `dispatched` run that never starts within the startup grace (90 s) is treated the same way.
- **Every kill branch re-reads before it kills.** The loop's snapshot of `store.active()` goes stale at every `await` (earlier kills, the Dagu status probe), so the timeout and liveness branches re-read the record and re-derive their verdict on the FRESH state immediately before killing — a run that finalized, entered finalize, or heartbeat mid-window is left alone. The same guard protects the stalled-finalize branch; `_kill_inner` (`domain/runs.py`) additionally aborts its save (and the mission-status restore) if the state moved during its own teardown awaits — the mover wins.
- **No mid-run kill on mission terminal:** the watchdog does **not** poll the PMO for `done`/`canceled` mid-run. `EXTERNAL_TRANSITION` is decided only at **finalize**, when compare-and-transition re-reads the live stage label and finds it differs from `stage_label_at_dispatch` (artifacts still post; no further stage mutation).
- **Finalize-stall backstop:** Runs in `finalizing` are never wall-clock-killed while their `run.artifacts` entry can still be redelivered (crash+reclaim resumes them). But if a run is past `timeout_seconds + DEVCAKE_FINALIZE_STALL_SECONDS` (default 3600, age from `created_at`) **and** its entry is no longer on the ingress stream (poison dead-lettered per `15-errors-and-retries.md` §5, or lost), nothing can ever finish it — the watchdog fails it without re-entering finalize, so it stops blocking its mission and holding a concurrency slot.

## 6. Startup reconciliation (ordered checklist)

On every app boot, before the first poll cycle:

1. Load config; validate (`CONFIG_INVALID` blocks startup with a clear admin-panel health error).
2. Ensure the managed labels in `domain/model.py` `ALL_LABELS` exist in the configured team (`05-pmo-adapter.md` §5) — eleven names today, including `DEVCAKE-DISCOVERY` (ADR-0033 sweep gate; not a derivation stage).
3. **Reconcile Runs** (`domain/reconcile.py`): for each local Run in a non-terminal state, query the Dagu run-status API for `run_id`:
   - Dagu says finished + artifacts message present in Redis (pending-entries reclaim, `09-messaging.md` §5) → resume finalization from `finalized_steps`.
   - Dagu says running → adopt it: mark `running`, resume watchdog coverage.
   - Dagu status/label is in the exact dead set `failed` / `aborted` / `error` / `cancelled` (British spelling on the label arm), or the API returns no such run → mark `orphaned` (`DEV_ORPHANED`); counts as a failed attempt; the Mission (whose label never advanced — INV-3) reschedules naturally. American `canceled` is **not** in this exact set (watchdog §5 uses a `cancel` substring and would treat it as dead mid-flight).
   - A `dispatched` Run with no Dagu run → the §3.1 crash window; mark `orphaned` (never re-trigger blindly — a duplicate `dagRunId` trigger returns 409 `already_exists`).
   - Runs in `finalizing` are left for reclaim (step 4), not orphan-killed.
4. Reclaim pending Redis messages older than the consumer's dead-time (XAUTOCLAIM) and process them.
5. Start the poll, ingress, and watchdog loops. The poll task runs the **initial full forge sweep** (bounded-parallel, `ForgeRuntime.refresh_all`) before its first cycle — off the boot critical path (so a large catalog probe does not hold the listen socket past the compose healthcheck) but still ahead of any dispatch, so a definitively bad repo credential latches its breaker before cycle 1 can burn an attempt. `/health.forge_probe` reports the sweep's progress; a sweep that misses its 60 s budget is retried each cycle until one completes.

## 7. Crash matrix (summary)

| Failure | Effect | Recovery |
|---|---|---|
| App crashes mid-poll | Nothing dispatched twice (Run file precedes trigger) | §6 reconciliation |
| App crashes mid-finalization | Some side effects applied | `finalized_steps` + idempotency keys resume the rest |
| Dev container crashes | Mission label untouched (INV-3); **the dispatch-time backlog→in_progress status write is reverted** (live compare first — human edits win), else a failed first ONBOARD would strand the mission at derivation row 9 | Run `failed`; attempt++; reschedules next cycle; after `max_attempts` → `DEVCAKE-FAILED` |
| Dev exceeds timeout | Watchdog kills container | Same as crash (`DEV_TIMEOUT`) |
| Redis down | Devs buffer/retry publishes with backoff; app consumer reconnects | Streams are durable (AOF); nothing lost; finalization is stream-driven |
| Linear down | That instance's poll segment skips; other instances continue; running Devs unaffected (they don't talk to PMO — INV-4) | Next poll tick re-tries the sick instance (`PMO_TRANSIENT`); finalizations queue on the ingress until PMO is writable |
| `/data/state` wiped | Attempt counters, run history, and the scheduled-task fire ledger lost (INV-1) | §6 rebuilds the in-flight picture from the Dagu API and Redis; every enabled cron row fires on the next poll and `cron_degraded` clears (`10` §5) |
