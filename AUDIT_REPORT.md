# Audit report — changes since `v0.2.5`

| Field | Value |
|---|---|
| **Range** | `v0.2.5` (`d3361f2`) → `HEAD` (at audit time; re-check with `git log v0.2.5..HEAD`) |
| **Scope** | ~25 commits, ~179 files, ~+11k / −1.9k lines |
| **Date** | 2026-08-03 |
| **Method** | Skeptical static review: full tag-range pass, then five parallel deep-dives on residual risk (deploy/images/DAG, SPA, merge/forge, continuation/security, boot/cost/tests). Highest-signal claims spot-checked against source. |
| **Dynamic** | No live stack smoke, no image rebake, no forge live battery in this audit. |
| **Prior notes** | Initial structured review: `/tmp/grok-1000/grok-review-757d814d.md` (session scratch; this file is the durable deliverable). |

**Verdict in one line:** Architecturally strong release (mirrors, provisioned workspaces, continuation, per-repo merge, costing) with honest security docs overall — but several **fail-closed claims are incomplete in code**, and deploy/ops lockstep has sharp edges that can host-root-pollute or desync image tags.

---

## 1. What this release ships (themes)

| Theme | ADR / PR trail | Risk class |
|---|---|---|
| Per-repo merge doctrine (`auto_merge` / auto-resolve / retry window) | ADR-0020 | Domain correctness |
| App-side estimated cost + operator rate card | ADR-0021 | Honesty of metrics |
| In-container continuation (turn discipline, exit 11) | ADR-0022 | Agent side-effects, cost |
| Dev toolchain floor (tini, browsers, class A/B/C tools) | ADR-0023 | Ops / noisy neighbor |
| Mandatory repo source mirror + Dagu 2.11.3 | ADR-0024 | Isolation + dispatch gate |
| Provisioned workspaces (agent never mounts `/mirrors`) | ADR-0025 | Isolation + deploy lockstep |
| Admin SPA overhaul (nav, missions strip, Dev Types table, Runs cost) | UI PRs | Operator UX / DESIGN |
| Boot: forge probe non-blocking; unused-repo hygiene | #64, #65 | Ops latency |
| Docs: REVIEW stage vs reviewer token | docs | Security-claim hygiene |

---

## 2. What was deep-reviewed this pass (vs first pass)

| Area | First pass | This audit |
|---|---|---|
| Workspaces create-after-save + SPA “dispatch frozen” lie | Deep | Confirmed + expanded |
| Mirror fail-closed + phase runspec | Deep | Confirmed; phase is **client-claimed** |
| Merge rearm clear + missing-PR wedge | Deep | Confirmed; Gitea finalize/sweep split found |
| Costing null-vs-0 | Deep | Confirmed; type-unsafe tokens edge |
| Continuation side-effects | Skimmed | Deep — prompt-only idempotency |
| ADR-0023 images / tini / pw-browsers | Skimmed | Deep — coherent |
| Dagu two-step + `DEVCAKE_WS_HOST` deploy | Skimmed | Deep — **high** ritual gaps |
| `up.sh --bake` vs `DEVCAKE_TAG` | Not covered | **High** tag desync |
| SPA DESIGN compliance | Skimmed | Deep — polish gaps, no critical draft bugs |
| Poll lock + forge re-sweep after #64 | Partial | **High** unbounded in-cycle sweep |
| docs/09 ACL orphan boot sweep | Not covered | **Doc overclaim** — not implemented |
| RunStore parse cache | Partial | Cache not cleared on `clear()` |
| Structure guards ADR-0015 | Partial | Still ratcheting; no new god methods |

---

## 3. Consolidated findings

Severity guide:

| Sev | Meaning |
|---|---|
| **P0** | Wrong fail-closed behavior, silent permanent wedge, or deploy path that can damage host / desync production images under supported ops rituals |
| **P1** | Real bug or honesty gap with operator impact; should fix before calling the release “done” |
| **P2** | Edge / forge-specific / defense-in-depth / polish |
| **P3** | Nit, docs drift, residual accepted by product contract |

IDs are stable for discussion (`AUD-###`).

---

### 3.1 P0 — fix before you trust fail-closed / deploy

#### AUD-001 — Workspace create failure after durable save strands the mission and degrades the whole PMO instance

- **Evidence:** `app/devcake/domain/run_bootstrap.py:51-78` (order: ACL → `store.save` → `workspaces.create` → `executor.start`); `app/devcake/domain/orchestrator/schedule.py:129` (no try/except around dispatch); `app/devcake/api/poll.py:270-276` (`poll_degraded` on any permanent instance exception); in-flight guard `schedule.py:82-84`; `STARTUP_GRACE` ~90s in `watchdog.py:18,68-69`.
- **What happens:** Disk full / unwritable `$DEVCAKE_WS_HOST` / permission error → run already `dispatched` + ACL user exists → exception aborts schedule for the rest of that segment → whole instance marked `poll_degraded` → mission blocked by in-flight until grace kill burns an attempt.
- **Contradicts:** Comment at `run_bootstrap.py:63-67` (“mission gates and retries next cycle”); ADR-0025 §7 wording about pre-create gating without attempt burn.
- **Tests:** `test_launch_create_failure_gates_before_the_executor` only asserts `executor.starts == []` — not store emptiness, ACL cleanup, or per-mission gating.
- **Fix direction:** On create failure after save: terminal-fail/orphan the run, delete ACL, cleanup workspace (partial already), **return without re-raising** into poll. Prefer gating on `workspaces.volume_error` before any launch (mirror pattern).

#### AUD-002 — SPA/docs claim “dispatch is frozen” for workspace volume_error; dispatch does not gate

- **Evidence:** `admin/spa/src/lib/alerts.js:222-227` (“Workspace base is unusable — **dispatch is frozen**”); mirror counterpart `alerts.js:190-195` is backed by `RepoCache.ensure_fresh` / `volume_error` in `repo_mirror.py:151-152`. Workspace `volume_error` is only a `/health` probe (`workspaces.py:41,191-198`; `health.py` surface) — **never consulted** in bootstrap/dispatch.
- **Why it matters:** Operators believe the fleet is frozen while launches keep failing as AUD-001.
- **Fix direction:** Gate every dispatch flavor on `workspaces.volume_error`; align alert copy and ADR-0025 §7 with real behavior.

#### AUD-003 — Live DAG bind + missing `DEVCAKE_WS_HOST` can expand workspace mounts at host root

- **Evidence:** `dagu/dags/dev-run.yaml` volumes `$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace`; `./dagu/dags` is a **live** `:ro` bind (`docker-compose.yml`); compose only hard-fails empty `DEVCAKE_WS_HOST` when **recreating** dagu (`DEVCAKE_WS_HOST=${…:?…}`). Docs (`docs/13-deployment.md`, AGENTS.md) prescribe `stop dagu → pull → ./up.sh --bake` but nothing enforces it.
- **Why it matters:** `git pull` can land the two-step DAG into a still-running dagu without `DEVCAKE_WS_HOST` → expansion like `/${params.RUN_ID}` → dockerd may create root-owned dirs at filesystem root. Sentinel can fail provision (good), host pollution remains.
- **Fix direction:** Pre-up gate in `up.sh` (refuse if dagu up without env); DAG precondition / wrapper abort if `DEVCAKE_WS_HOST` empty or non-absolute; operator one-liner check documented as required, not optional.

#### AUD-004 — `./up.sh --bake` does not export `.env` `DEVCAKE_TAG` for bake

- **Evidence:** `up.sh:154-168` exports `DOCKER_GID` + `DEVCAKE_WS_HOST` only, then `docker buildx bake all`. Bake tags from process env / HCL defaults (`docker-bake.hcl` `DEVCAKE_TAG` / `TAG`), **not** from `.env`. Compose + app use `.env` / container `DEVCAKE_TAG`; harness images follow app env (`app/devcake/harness.py` `_TAG`). CI does export correctly (`scripts/ci_compose_for_dispatch.sh`).
- **Why it matters:** Supported path with pinned `DEVCAKE_TAG=<sha>` in `.env` can bake `*:latest` while compose runs `app:<sha>` and dispatches `dev-*:sha` never baked this round — silent stale harnesses (`pull_policy: missing`) or hard failures.
- **Fix direction:** Source/export `DEVCAKE_TAG` from `.env` before bake **and** compose; fail if bake tag ≠ compose tag.

#### AUD-005 — `rearm_merge_repos` cleared even when no mission was re-armed

- **Evidence:** Unconditional `mgr.rearm_merge_repos.clear()` at `sweeps.py:54-57`. Re-arm only posts `MERGE_RETRY_MARKER` inside `_deferred_merge_retry` after PR lookup (`sweeps.py:75-80`, `124-161`).
- **Lost forever (until another OFF→ON toggle) if:** no PR that cycle; forge missing; feed post fails (swallowed per-mission then clear); exception mid-loop.
- **Tests:** Happy path only (`test_rearm_reopens_parked_mission_when_auto_merge_flips_on`).
- **Fix direction:** Clear a repo from the set only after successful rearm feed (or active window already present) for each applicable parked mission.

#### AUD-006 — `auto_merge` ON + missing PR at REVIEW finalize → permanent dead app-driven merge

- **Evidence:** `review.py:152` `if inst.auto_merge and pr:` … `elif` parks `DEVCAKE-MERGE` with human-await copy (`266-277`) and **no** `MERGE_RETRY_MARKER`. Sweep silent-returns when PR missing (`sweeps.py:75-80`) — no `blocked_reasons`, no `merge_handoffs` banner (banner only after PR found).
- **Why it matters:** Forge list lag / branch naming miss parks the mission forever for **app** auto-merge; human/out-of-band merge still works. Same PR-miss class poisons rearm (AUD-005).
- **Fix direction:** Treat missing PR like a visible gate (`blocked_reasons` / anomaly / explicit feed). If auto_merge ON, open deferred window from `result.pr_url` / branch or re-probe with bound; never use pure human-await copy for auto_merge ON.

---

### 3.2 P1 — real bugs / honesty gaps

#### AUD-007 — In-cycle forge `refresh_all` under poll lock is unbounded after #64

- **Evidence:** Initial sweep in `poll.loop` uses `asyncio.timeout(60)` (`poll.py:326-335`). Every later `run_cycle` with `breakers` **or** `last_full_probe_at is None` calls `refresh_forge_health()` **without** a timeout (`poll.py:220-222`), while holding `poll_rt.lock` (cycle runs under `async with self.lock`). Full catalog re-probe, not “latched names only.”
- **Why it matters:** #64 fixed lifespan blocking the listen socket; force-poll / clear-runs still serialize behind multi-minute sick-catalog sweeps. Partial timeout on first attempt leaves `last_full_probe_at` unset → every subsequent cycle retries unbounded.
- **Fix direction:** Time-bound (or name-bound) in-cycle refresh; prefer re-probing latched repos only; keep budget ≤ admin proxy window for force-poll.

#### AUD-008 — `docs/09-messaging.md` claims boot ACL orphan sweep that does not exist

- **Evidence:** docs/09 §1a: *“startup reconciliation sweeps `ACL LIST` for `dev-*` users with no live Run.”* Code: `delete_run_user` on finalize/kill/clear/quarantine paths; `ACL USERS` sweep only in clear-runs (`clear.py`). **No** ACL pass in `reconcile.py` or lifespan beyond quarantine of **known** run ids.
- **Why it matters:** Crash after `create_run_user` and before durable save (narrow) or lost run file → orphan ACL until operator clear-runs. Doc overclaims fail-closed hygiene.
- **Fix direction:** Implement the sweep or reword docs to match code.

#### AUD-009 — Runspec phase is client-claimed (same Redis ACL for provision + harness)

- **Evidence:** App branches on `(payload or {}).get("phase") == "provision"` (`runs.py:254-262`). Both DAG steps share run ACL credentials. A compromised/malicious provision entrypoint can request full harness secrets while still mounting `/mirrors` RO.
- **Why it matters:** Phase reduction is **TCB-on-entrypoint**, not cryptographically bound. Acceptable under single-operator dedicated-host model if stated; easy to over-read as “provision never can see harness secrets.”
- **Fix direction:** Document as honor-system; optional: bind reduced replies to known provision image/entrypoint hash, or separate ACL roles (heavy).

#### AUD-010 — Gitea / non-tristate: finalize vs sweep conflict doctrine split

- **Evidence:** Finalize trusts conflict only if `mergeable_tristate` (or caps missing) (`review.py:206-214`); on Gitea, `mergeable=False` → **handoff**, not EXECUTE. Sweep routes `verdict is False` to EXECUTE without tristate check (`sweeps.py` deferred path). Docs (`docs/06` §5) imply auto-resolve works broadly.
- **Fix direction:** Align finalize with sweep (merge-FIRST) **or** document auto-resolve as tristate-only; add FakeForge with `mergeable_tristate=False` tests.

#### AUD-011 — Workspace `RUN_ID` fence weaker than DAG/docs

- **Evidence:** Store `RUN_ID_RE` `{1,64}` (`workspaces.py:28`); DAG precondition `{6,64}` (`dev-run.yaml`); `docs/02` claims both use `{6,64}`.
- **Why it matters:** Short id can create a husk dir the DAG will never start; docs overclaim dual fence. Path traversal still blocked (charset).
- **Fix direction:** Align store to `{6,64}` (or actual `make_run_id` minimum) + fix docs.

#### AUD-012 — Relative `DEVCAKE_WS_HOST` accepted by compose (not by `up.sh`)

- **Evidence:** `up.sh` requires absolute path; compose only `:?` non-empty. App bind vs Dagu daemon-host resolution can diverge.
- **Fix direction:** Reject non-absolute at boot / dagu start; keep `up.sh` as primary path.

---

### 3.3 P2 — edge cases, polish, defense-in-depth

| ID | Area | Summary | Evidence |
|---|---|---|---|
| AUD-013 | Mirror | URL-mismatch rebuild: `delete_mirror` then recurse; failed delete → stack/log spam / wedged mirror | `repo_mirror.py` sync_one rebuild branch |
| AUD-014 | Continuation | Double-PR / re-push residual is **prompt-only**; `max_continuations` multiplies Zone-B surface | `continuation.py:134-137`; docs/14 Zone B |
| AUD-015 | Reconcile | Exit enrichment only matches exit status 13–16; 10/11/12/20 orphans may keep generic kill class | `reconcile.py:71-76` |
| AUD-016 | Workspaces | `NullWorkspaceStore` is silent default if composition omits injection; prod `main.py` wires real store — no structure guard | `runs.py` / `run_bootstrap.py` defaults; `main.py:85-86` |
| AUD-017 | Costing | Non-numeric token fields can `TypeError` → 500 on `GET /runs` | `costing.py:44-53` |
| AUD-018 | RunStore | `clear()` does not empty `_parse_cache` immediately (next `all()` prunes) | `run_store.py` |
| AUD-019 | Clear-runs | Force-remove undrained → wipe proceeds; ACL DELUSER can race residual containers | `clear.py` drain residual |
| AUD-020 | Deploy | Hard-coded volume name `devcake_mirrors` tied to compose project `name: devcake` | `dev-run.yaml` / compose |
| AUD-021 | Ops | Harness-step boot failure detection up to HEARTBEAT_GRACE (~300s) after provision marks running | `watchdog.py`; ADR-0025 §7 |
| AUD-022 | Resources | No Dev container cgroup limits; ADR-0023 browser floor raises OOM blast radius on dedicated host | `dev-run.yaml` comments; docs/14 §11 |
| AUD-023 | Merge | Fail path not `_checkpoint`’d — crash mid-ladder can re-merge/re-comment (mitigated by already-merged probes) | `review.py` |
| AUD-024 | Merge | Bundle/profile rearm wired but not E2E tested | `settings_bundle.py:770-774` |
| AUD-025 | SPA | Disallowed `blue-*` on missions flash / Repos links; missing focus-visible on new strip controls | DESIGN.md §1/§6; MissionsPage / ReposPage |
| AUD-026 | SPA | CostInputs InstantZone half-marked (Save outside zone); MissionRow one-item Park MoreMenu vs DESIGN §3.3 | CostInputsModal; MissionRow |
| AUD-027 | SPA | PMO page not bulk-capped like Repos (fleet scale) | PmoSection vs ReposPage |
| AUD-028 | SPA | NavGuard “Save & leave” untested | DraftChrome; settings.mjs |
| AUD-029 | Images | `export_sids` imported unused; hello skips sentinel / tini; grok `curl \| bash` at bake | entrypoint / Dockerfile |
| AUD-030 | Entrypoint | PATH shadowing of harness CLIs under continuation (integrity residual, not privilege) | Dockerfile PATH; argv bare names |

---

### 3.4 P3 — nits / accepted residuals

| ID | Summary |
|---|---|
| AUD-031 | Misleading create-failure comment (`run_bootstrap.py:63-67`) once AUD-001 fixed |
| AUD-032 | ADR-0025 §7 / docs/02 fence wording drift until code/docs aligned |
| AUD-033 | markers.py still says “newest 100 comments”; docs/05 now paginates higher — residual at 1k ceiling |
| AUD-034 | Rearm is process-memory only (documented); restart loses flip |
| AUD-035 | Runtime totals always numeric `0` when no completed runs (token columns use null) — minor honesty asymmetry |
| AUD-036 | Equal-length model_prefix ties → first list entry (dups rejected; exotic case) |
| AUD-037 | Stale SPA test comments (“Buzz-style grid”) |
| AUD-038 | ADR-0015 allowlist residual fat routes (`clear_runs`, oauth, …) — pre-existing debt |

**Accepted product residuals (do not “fix” as bugs without product change):** single-operator dedicated host; Zone B agent + write token; auto_merge does not remove Dev merge capability; wipe races with undrained containers; no multi-tenant sandbox; redaction ≠ egress control (see `docs/14-security.md`).

---

## 4. What looked solid (do not regress)

### Isolation (ADR-0024 / 0025)

- Provision mounts mirrors RO + workspace; harness mounts **only** workspace (`dev-run.yaml`).
- Dispatch fail-closed on mirror `ensure_fresh` / `volume_error`.
- Mirrored extras tokenless in runspec; app git child env never inherits host `os.environ` for tokens.
- Askpass for app mirror sync not on `/mirrors`.
- Sentinel + marker forensics; DAG `RUN_ID` precondition; record-before-dir; never delete between start and terminal.
- Warm-at-boot prohibited (structure guard); warm on background poll task.

### Continuation (ADR-0022)

- Only clean incomplete (row-9): exit 0, no fault, no valid/recoverable result.
- Faults outrank continuation; resume argv capture-verified for claude/codex/grok; no default resume fallback.
- Token merge handles cumulative vs sum correctly; `continuations_used` stamped into app surfaces.

### Costing (ADR-0021)

- Pure estimator; unknown model / incomplete split → `None`, not `$0`.
- Totals null-until-contribution; `cache_write` null display honesty.
- Read-time reprice from current card; draft IGNORES `cost_inputs`; CostInputs Instant PUT is narrow.

### Merge doctrine spine (ADR-0020)

- Per-repo fields; single `apply_auto_merge_rearm` on PUT **and** bundle.
- Merge-before-Done; already-merged honesty; feed-marker windows restart-safe.
- Vanished repo VISIBLE in sweep/finalize.
- Capability branching pattern (right idea; Gitea incomplete — AUD-010).

### Boot / multi-instance

- Forge probe out of lifespan (structure-guarded); listen socket free at boot.
- `backend_degraded` placement correct (unconditional, before schedule).
- `poll_degraded` per-instance; cache retention on skip.
- Clear-runs lock order: poll lock → dispatch lock; workspaces wiped; **mirrors preserved**.

### SPA / structure

- No raw hex outside `@theme`; no `window.confirm/prompt/alert` in components.
- Draft shared across Config/Repos/PMO; InstantZone for real instant paths.
- MissionManager remains façade of module functions; ADR-0015 guards still enforce.

### Security contract honesty

- `docs/14` / AGENTS / README generally **do not** claim multi-tenant sandbox or “secrets never leave the host under injection.” Reviewer token never in Dev runspec. Workspace host bind called out as secret-bearing.

---

## 5. Test gaps (highest leverage)

| Gap | Related |
|---|---|
| Create failure: store empty / ACL deleted / no poll_degraded / volume_error gates dispatch | AUD-001, AUD-002 |
| Finalize + sweep with `pr is None` (auto_merge ON/OFF) | AUD-006 |
| Rearm with missing PR / feed failure / partial loop then clear | AUD-005 |
| In-cycle forge refresh time-bounded / latched-only | AUD-007 |
| Bundle `apply_bundle` rearm populates set | AUD-024 |
| FakeForge `mergeable_tristate=False` finalize path | AUD-010 |
| Non-numeric token fields do not 500 list_runs | AUD-017 |
| `clear()` empties parse cache immediately | AUD-018 |
| SPA: InstantZone on CostInputs Save; Save&leave nav guard | AUD-026, AUD-028 |
| Align RUN_ID min length unit case | AUD-011 |

**Process risk:** stale `devcake/app-test:latest` false greens if pytest runs without rebake (`AGENTS.md`). Prefer `./scripts/pytest_app.sh` or `PYTHONPATH=app` on Python 3.12.

---

## 6. Recommended fix order

1. **AUD-001 + AUD-002** — workspace fail-closed + real dispatch gate + honest alert/docs  
2. **AUD-005 + AUD-006** — merge rearm + missing-PR visibility (silent permanent wedges)  
3. **AUD-003 + AUD-004** — deploy ritual: empty WS_HOST gate + `up.sh` tag lockstep  
4. **AUD-007** — bound in-cycle forge refresh under poll lock  
5. **AUD-008 + AUD-009** — docs honesty (ACL sweep, phase trust boundary)  
6. **AUD-010–AUD-012** — Gitea doctrine, RUN_ID fence, absolute WS_HOST  
7. **P2 backlog** — SPA DESIGN polish, costing guard, parse-cache clear, mirror rebuild loop, continuation anomaly tripwire (duplicate open PRs)

---

## 7. Residual unknowns (not closed by this audit)

1. Live wall-clock of full forge catalog under `PROBE_CONCURRENCY=8` with breakers latched.  
2. Real-world rate of `get_pr_by_branch` misses (Gitea filter races, forge lag).  
3. Dagu env expansion for **unset** vs empty `DEVCAKE_WS_HOST` on this Dagu version.  
4. `COMPOSE_PROJECT_NAME` override interaction with hard-coded `devcake_mirrors`.  
5. Double-PR rate under weak models × high `max_continuations` (prompt-only).  
6. Whether any non-`main.py` composition path ever wires `NullWorkspaceStore` in prod-like deploys.  
7. Live DESIGN §8 screenshot / `check:ui` against `:8080` (static SPA audit only).  
8. Live two-step smoke: sentinel, no-mirrors-in-dev probe, LFS pin (relied on ADR probes + static code).

---

## 8. Release themes scorecard

| Theme | Correctness | Tests | Docs honesty | Ops risk |
|---|---|---|---|---|
| ADR-0020 merge | Strong spine; rearm + missing PR holes | Happy path strong; gaps above | Good ADR; Gitea overclaim | Medium |
| ADR-0021 cost | Strong null semantics | Strong unit | Good | Low |
| ADR-0022 continuation | Trigger/classify strong | Fixtures present | Good residual callouts | Medium (Zone B × N) |
| ADR-0023 toolchain | Floor matches ADR | Bake/smoke in image | Good | Medium (RAM/cgroups) |
| ADR-0024 mirrors | Strong fail-closed | Strong | Good | Low if volume name stable |
| ADR-0025 workspaces | Isolation strong; **create gate incomplete** | Partial | Overclaims gate | **High** deploy skew |
| SPA overhaul | Draft/Instant solid | Missions/runs good | DESIGN mostly | Low–medium polish |
| Boot #64 / hygiene #65 | Lifespan fixed; lock residual | Partial | Good incident notes | Medium under large catalog |

---

## 9. Bottom line

This is not a “rubber stamp” release. The architecture (mirror → provision → harness-only workspace, phase-reduced secrets when entrypoint is honest, pure costing, per-repo merge spine) is the right shape and much of it is carefully tested.

What fails a **skeptical** bar:

1. **Workspace fail-closed is theater** until create failures and `volume_error` actually gate without burning attempts or `poll_degraded`ing the fleet.  
2. **auto_merge re-arm / missing PR** can silently lose app-driven merge forever.  
3. **Deploy lockstep is ritual-only** — live DAG + empty `DEVCAKE_WS_HOST`, and `up.sh --bake` ignoring `DEVCAKE_TAG`, are supported-path footguns.  
4. **#64 is incomplete** for large/sick catalogs still holding the poll lock unbounded.  
5. **Docs slightly outrun code** on boot ACL orphan sweep and workspace gate wording.

No critical multi-tenant breakout chain beyond the product’s accepted Zone-B model was found. The sharpest issues are **operator-visible wedges, attempt burns, and deploy host pollution** — not secret crypto failures.

---

## 10. How this report was produced

| Pass | Role |
|---|---|
| Tag-range review subagent | Initial bugs on workspaces, rearm, missing PR, cost/continuation/mirror depth |
| Deep-dive: images / DAG / deploy | AUD-003, AUD-004, AUD-011, AUD-012, AUD-020–022, ADR-0023 OK list |
| Deep-dive: SPA DESIGN | AUD-025–028, draft/Instant OK |
| Deep-dive: merge / forge | AUD-005, AUD-006, AUD-010, AUD-023–024, edge matrix |
| Deep-dive: continuation / security | AUD-008, AUD-009, AUD-014–016, docs/14 honesty |
| Deep-dive: boot / cost / tests | AUD-007, AUD-017–019, structure guards, wipe order |
| Orchestrator spot-checks | Verified AUD-001–008 paths against current source |

This file is advisory audit output only; it does not change product behavior.
