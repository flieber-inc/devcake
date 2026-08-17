# ADR-0025 — Provisioned workspaces (the agent never sees the mirror)

- **Status:** accepted (2026-08-03)
- **Context:** ADR-0024 shipped the mandatory repo source mirror by mounting
  the whole `devcake_mirrors` volume read-only into **every** Dev container.
  That closed the egress problem but reopened one the project had spent
  enormous effort closing — control of exactly what the agent sees. Founder
  concerns, post-ship: (1) the agent's context is polluted — under `/mirrors`
  it can see bare-pack duplicates of its own repo plus every other configured
  repo; (2) ambient visibility of all mirrors undermines the curated
  reference-repo concept (`14` §6 named the ambient-mirror residual that this
  ADR later deleted); (3)
  "the optimal state is for the mirror folder to disappear once its cloning
  happens." In-container unmount is impossible (uid 1000, no CAP_SYS_ADMIN),
  so the fix is structural: split each run into a **provision** step and a
  **harness** step. Step 1 is trusted entrypoint code with no agent — it
  mounts the mirrors RO plus a per-run workspace, clones everything, and
  exits. Step 2 is the agent — it mounts **only** the per-run workspace. The
  mirror is never present during the agent's lifetime — strictly stronger
  than "disappears after cloning."

## Decision

### 1 — One run is two dependent container steps

`dev-run.yaml` grows a second step. `provision` (container `prov-<run_id>`)
runs the run's dev IMAGE with `DEVCAKE_PHASE=provision`, mounting
`devcake_mirrors:/mirrors:ro` **and** `$DEVCAKE_WS_HOST/<run_id>:/workspace`
rw. `run_dev` (`dev-<run_id>`) `depends: [provision]`, sets
`DEVCAKE_PHASE=harness`, and mounts **only** the per-run workspace — no
mirrors line. Probe-verified live on Dagu 2.11.3 (one-shot rigs, re-checked
at build): `${params.RUN_ID}` and the dagu-process `$DEVCAKE_WS_HOST`
interpolate inside a `volumes:` source; two dependent container steps share a
per-run bind while the second container genuinely lacks the first's mounts
("NO-MIRRORS-IN-DEV"); a step-level `:ro` is kernel-enforced. A DAG-level
`preconditions` guard pins `${params.RUN_ID}` to `re:^[A-Za-z0-9_-]{6,64}$`
so a bare Dagu-UI "Run" click (default params) can never bind the whole
workspace base into a container — dry-run-verified: a valid id runs both
steps, `../escape` aborts before any container.

### 2 — Plane separation (why not Redis between the steps)

The three planes stay distinct and each does the one job it is good at.
**Dagu is the control plane**: `depends` is the ONLY sequencer — no Redis
handshake gates step 2. **Redis is the data/secret plane**: the per-run ACL
credential rides params into both containers; the run spec is served over the
bus (phase-scoped, §4); `run.started`, heartbeats and artifacts span the
step gap unchanged. **The filesystem is the artifact plane**: what provision
produced for the harness is verified *in-band with the artifact itself*. A
Redis-mediated "provision done" flag was considered and rejected — a bus
flag can read green while the harness is bound to the wrong or empty
directory; the sentinel + marker (§3) are checked against the very bytes
they certify, which is the whole point.

### 3 — The workspace is a host-bind tree the app owns end to end

Named volumes cannot express a per-run subpath through Dagu's
`source:target` syntax, and the app holds no docker socket to create/remove
per-run volumes — so the workspace base is a host directory,
`DEVCAKE_WS_HOST` (host-absolute; bind sources resolve on the daemon host).
`up.sh` derives `$(pwd)/workspaces`, `mkdir`s it `0700` (the tree holds repo
source, activity-transcript history and agent output — treat it like
`gitea_data`, `14` §1), and upserts the var into `.env` exactly like
`DOCKER_GID`; compose mounts it rw into the app at `/workspaces` and passes
it to the dagu service env, both with `:?` so an empty value fails loud
instead of binding junk at the host root. `WorkspaceStore`
(`domain/workspaces.py`, modeled on `RepoCache`) owns the lifecycle:

- **Pre-create (Hook C).** Inside `RunBootstrap.launch`'s dispatch lock, in
  this exact order: `create_run_user` → `store.save` → **mkdir** →
  `executor.start`. Record-before-dir makes "a dir whose name has no record"
  always garbage — which is what makes the sweep predicate sound without a
  lock. Dir-before-start kills the daemon-root-autocreate edge (dockerd
  creates an absent bind source `root`-owned). `create` writes a **sentinel**
  `.devcake/created-by-app` containing the run id, and uses
  `mkdir(exist_ok=False)` so a collision or a pre-existing husk is loud, not
  adopted. A `create` failure raises out of `launch` (the mission gates and
  retries next cycle); an `executor.start` failure does **not** rm inline (a
  start timeout / 409 is ambiguous — the watchdog's STARTUP_GRACE kill
  reaches Hook B within ~2 min either way).
- **Sentinel + marker.** Provision's first act verifies the sentinel names
  ITS run — a missing/mismatched sentinel means the container is bound to the
  wrong directory (daemon autocreate, or a stale/hand-edited
  `DEVCAKE_WS_HOST` pointing every step at the same wrong base), and it exits
  20 with forensics instead of provisioning into it. On success provision
  writes `.devcake/provisioned` (the marker, also the run id) last. The
  harness step verifies **both**: a missing/mismatched marker ⇒ exit 20 WITH
  artifacts, the detail carrying owner/mode/listing so an operator-rm →
  daemon-recreate (root-owned, empty) is distinguishable from a
  half-finished provision. One sentinel check closes the daemon-autocreate
  edge AND silent base drift, which the marker's divergence check alone could
  not (a *consistently* wrong base passes a divergence test).
- **Cleanup is best-effort; the sweep is the guarantee.** Dagu's
  `max_clean_up_time_sec: 30` means a kill can race a container that holds
  the bind ~35 s, so deletion is never a synchronous invariant. Kernel
  reality (probed): removing bind *contents* on the host is instantly
  visible in-container; removing the bind *source dir* while mounted is
  allowed (lazy VFS detach ≥ kernel 3.18; the container's `/workspace`
  becomes a detached mount freed at exit). The real hazards are a dying
  container still writing (→ `ignore_errors` + sweep) and rm-before-step-2-
  create (→ never delete between `executor.start` and a terminal state).
  Hooks — **A** post-finalize (runs.py, only once the run is actually
  terminal; a finalize that stalls in `finalizing` keeps its workspace for
  redelivery); **B** the `_kill_inner` teardown (every watchdog/reconcile/
  operator/clear kill; never raises); **D** OAuth completion (bypasses both
  finalize and kill); **E** clear-runs base wipe (inside the dispatch-lock
  wrap — race-free vs pre-create — unconditional, since the records are
  going too); **F** boot quarantine of corrupt records; **G** the sweep —
  run-id-charset children, `lstat`-filtered (never follows symlinks),
  record-absent-or-terminal, older than a 10-min guard — at boot (in
  `reconcile_runs`, before anything serves) and periodically from the
  watchdog. The sweep is what turns every best-effort hook into an
  invariant; `/health.workspaces.leaked` surfaces when it is losing.

### 4 — The runspec is phase-scoped

Gating only the credential-file *write* in provision was not enough: the run
spec reply merges harness credential-env (model API keys, the claude OAuth
token), Dev-Type `secret_env`, credential-FILE content and
`DEVCAKE_FORGE_TOKEN` into one payload, so provision would receive every
secret over the wire regardless. `runspec.get` therefore carries a `phase`.
For `provision`, `provision_runspec_reply` serves a REDUCED spec: the run's
non-secret `spec_env`, the extras entries (mirrored ones are already
tokenless), the activity-repo credential (provision clones it), and
`DEVCAKE_FORGE_TOKEN` ONLY when the work repo direct-clones (`mirror_path`
empty — internal repos). Credential files and harness/Dev-Type secret env
never ride the provision reply. The harness phase gets the full spec, as
before (a defensive no-phase request also gets the full spec). Honest
scope of the claim: **the provision container runs no agent and receives no
harness/model secret**; the forge token it may hold (direct-clone internal
repos, or the activity token) is exactly what its clones need and no more.
The `phase` is **client-asserted**, not cryptographically bound (both steps
share the run's one Redis ACL) — a compromised provision entrypoint could ask
for the harness phase's full spec (AUD-009). This is honor-system by design:
the provision container runs DevCake's own trusted entrypoint under the
single-operator, dedicated-host model (`14` §2 zone B). The reduction hardens
against accidental leakage and shrinks secret material in transit; it is not
a defense against a subverted image. Binding replies to a provision-image
hash, or separate ACL roles per step, is the heavier option if that model
ever changes.

### 5 — LFS runs in both phases, credential-stripped, endpoint-pinned

Smudge fires at clone/checkout time, so `git lfs install [--skip-smudge]`
runs in the provision container (before its clones) **and** again in the
harness container (fresh HOME; mid-run pulls smudge too) — omitting it from
provision would silently deliver pointer files under `lfs=true`, a bug that
hides until the toggle flips. Mirror clones run credential-stripped
(`GIT_ASKPASS`/`DEVCAKE_FORGE_TOKEN` removed — a `file://` clone needs none)
with `-c lfs.url=file://<own mirror>` pinned. Both defenses were probe-
verified against the pinned git-lfs (3.3.0): a repo-committed `.lfsconfig`
naming a custom transfer agent (`touch /PWNED`) had its command-bearing keys
IGNORED by git-lfs's own unsafe-key allowlist ("These unsafe '.lfsconfig'
keys were ignored"), and the effective `lfs.url` was the pinned mirror, not
the `.lfsconfig` attacker URL — so repo content cannot steer git-lfs at
another repo's mirror or an external host inside the trusted provision
container.

### 6 — Timing, phases, and the hello/OAuth shape

`DEVCAKE_PHASE` ∈ `provision` | `harness` — the DAG always sets one, and the
entrypoint exits 20 loudly on anything else (a `phase_of` helper + test pin
this). There is **no** single-container fallback: rollback compat is not a
pre-v1 concern (the initial ship carried a monolithic branch for one commit;
it was deleted the same day). Only the provision phase sends `run.started`; the harness phase
relies on its heartbeat (started **before** its `runspec.get`, so a phase-2
boot fault keeps the liveness clock ticking). The `run.started` handler is
hardened to accept ONLY the `dispatched→running` transition — every replay
(ingress redelivery, the bus's XADD retry, a Dagu-UI re-run) is dropped,
which also fixes a pre-existing latent bug where a replayed `run.started`
reverted a `finalizing` run to `running` and clobbered `started_at` (the
Runs-page runtime metric). `hello_dev.py` gains a 3-line guard: phase
`provision` prints a note and exits 0 (no Redis, no marker) — so hello
reports `run.started` from phase 2 BY DESIGN, and STARTUP_GRACE (90 s)
absorbs both container creates; the CI hello smoke now exercises the exact
two-step production topology (its poll headroom was bumped). OAuth runs the
real harness image: provision exits 0 early (its spec has no repo vars), the
login runs in the harness step, and `timeout_seconds` moved 600→660 to pay
the extra container cycle out of the app's budget, not the human's.

### 7 — Failure taxonomy

Provision runspec-timeout → exit 20, no artifacts → watchdog probe kill (as
today's boot failures). Provision clone failures → today's exit-13 classes +
artifacts; `depends` blocks the harness step; finalize's failure branch is
provably harness-free-safe (today's exit-13 is already pre-harness — stub
token_report + transcript). Sentinel/marker-write failure → exit 20 with
artifacts. Harness-step marker missing / root-owned recreated dir → exit 20
loud, forensic detail. Harness-step boot failure (container create, phase-2
runspec timeout, Redis auth) → the run is already `running`, so it is caught
by stale-heartbeat at ≤ HEARTBEAT_GRACE (300 s) rather than STARTUP_GRACE
(90 s) — the one detection-latency regression of the split, bounded and
documented; heartbeat-before-runspec confines the 300 s case to a genuine
container-create failure. WS disk exhaustion / an unusable base → gated in
`RunBootstrap.launch`, which raises `WorkspaceUnavailable` and the caller
gates the mission cleanly — no attempt burned, reason on the missions row +
`/health` — exactly like the mirror precondition (hardened per AUD-001/002; a
latched boot `volume_error` fails fast before any ACL/record, and a transient
create failure after the save unwinds both). This is what makes the SPA's
"workspace base unusable — dispatch is frozen" alert TRUE rather than
aspirational: dispatch really does gate. Also exit 13 at clone / exit 20 at
marker for other WS troubles, plus a `/health.workspaces` disk alert, because
otherwise disk exhaustion presents as DEV_FORGE retry churn.

### 8 — Security posture and consequences

The agent container sees exactly the curated clones under `/workspace` and
nothing else: no `/mirrors`, no bare-pack duplicates, reference repos =
exactly the configured set. This SUPERSEDES ADR-0024 §5's accepted
"ambient mirror read" risk — deleted from `14`, not merely narrowed. Cross-
run isolation: each container binds only its own run's dir; only the trusted
app mounts the base; no app code path parses Dev-written workspace bytes
(sweep/stat only), and cross-run symlinks dangle in the Dev's own mount
namespace. New exposure named honestly (`14` §1): the workspace tree is a
user-owned host directory holding repo content + activity transcripts +
agent/tool output, persisting from dispatch until cleanup — `0700`,
gitignored, excluded from the backup set (`13` §8), and bounded by workspace
lifetime, not container lifetime (`14` §4 item 7 / §6 now carry).
run-id path safety: every producer routes through `make_run_id`
(`[A-Za-z0-9_-]`, ≤64), and `WorkspaceStore` re-validates the charset and
lexical containment in create/cleanup as defense-in-depth. Deploy-window
skew is real and ritualized (`13` §8): `./dagu/dags` is a live bind-mount, so
between `git pull` and `./up.sh` the old dagu container has no
`DEVCAKE_WS_HOST` — the deploy must `docker compose stop dagu` → pull →
`./up.sh` (which force-recreates dagu with the new env); in-flight DAG-runs
are reconcile-adopted.

## Related

- Implement: `dagu/dags/dev-run.yaml`, `images/common/dev_entrypoint.py` +
  `devcake_dev/workspace/{provision,clone}.py` + `devcake_dev/adapters/bus.py`,
  `images/hello/hello_dev.py`, `app/devcake/domain/{workspaces,run_bootstrap,
  runs,oauth,watchdog,reconcile}.py`, `app/devcake/api/{main,health,clear}.py`,
  `app/devcake/settings_bundle.py`, `docker-compose.yml`, `up.sh`,
  `scripts/ci_compose_for_dispatch.sh`, `scripts/ci_dispatch_hello.sh`,
  `admin/spa/src/lib/alerts.js`, `.env.example`, `.gitignore`.
- Evidence: `app/tests/test_workspaces.py` (store, hooks, run.started,
  phase-scoped runspec, launch ordering); `test_entrypoint_render.py`
  (`phase_of`, sentinel/marker, `mirror_clone_argv`/`_env`);
  `test_health_payload.py`, `test_oauth.py`, `test_clear.py`,
  `test_settings_bundle.py`; the 2026-08-03 LFS-steering and DAG-precondition
  probes recorded above.
- Supersedes: ADR-0024 §5 accepted "ambient mirror read" risk.
- Operator: `07-dev-runtime.md` §§1, 3, 5, 7b; `09-messaging.md` §4;
  `13-deployment.md` §§1, 3-5, 8; `14-security.md` §§1, 4 (item 7), 5–6;
  `15-errors-and-retries.md` §§1-2; `02-domain-model.md` §7; `00`, `01`, `12`.
