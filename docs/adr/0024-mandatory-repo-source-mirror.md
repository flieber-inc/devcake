# ADR-0024 — Mandatory repo source mirror

- **Status:** accepted (2026-08-03); §5's "ambient mirror read" accepted risk
  SUPERSEDED by ADR-0025 — the mirrors are now mounted RO into the **provision**
  container only, never the agent's harness container, so the deployment-wide
  ambient read surface named below no longer exists. **MAPPER→STEWARD** path /
  service rename (Decision 2 + Related) — banner below.
- **Context:** A production stress test ran a multi-repo instance with 27
  GitLab repositories; every run re-cloned all of them (~300 MB) from the
  external forge — 25 mission steps ≈ 7.5 GB of redundant egress, plus
  latency, connection risk and rate-limit exposure. Client-side git had
  nothing left to give (context repos were already `--depth 1`). Founder
  decisions (2026-08-03, several skeptical rounds): the mirror is
  MANDATORY with **no on/off toggle** ("an only-way-out … a necessary part
  of the run"); sync is a **fail-closed dispatch precondition** ("I am
  absolutely comfortable that a Dev is BLOCKED if a sync fails");
  proceed-on-stale was considered and REJECTED; all repos ride uniformly
  (a per-run work/reference split was rejected — any card can be some
  run's work repo); LFS is a **capability toggle, not a refusal** (an
  earlier refuse-LFS position was retracted: dev images shipped no
  git-lfs, so pointers-without-content was already the status quo, and
  the toggle *upgrades* it); the escape hatch is `git revert`.

> **Supersession note (MAPPER→STEWARD rename):** Decision 2's "MAPPER = its
> one repo" and "the mapper's periodic path" refer to today's **STEWARD**
> vehicle (run kind `STEWARD`, `StewardService`, seed `steward` / Opus per
> ADR-0033 D10). Related still listed `domain/mapper_service.py` — live path
> is `domain/steward_service.py`. Historical MAPPER spelling below is
> provenance; grepping engineers should follow the STEWARD names.

## Decision

### 1 — The app owns bare mirrors on a shared named volume

`RepoCache` (`domain/repo_mirror.py`, one instance injected into every
manager like `ForgeRuntime`) maintains `/mirrors/<name>.git` for every
`config.repos` card: `git init --bare` + `remote add` (clone_user@ URL —
never a token on disk) + `gc.auto=0`, then per sync `git fetch --prune
origin "+refs/heads/*:refs/heads/*" "+refs/tags/*:refs/tags/*"` — heads
and tags ONLY (a `+refs/*` mirror would drag GitHub's `refs/pull/*`,
doubling disk for content no clone takes) — plus `symbolic-ref HEAD
refs/heads/<default_branch>` EVERY sync (a bare init defaults HEAD to
main/master; a wrong HEAD makes `file://` clones check out nothing).
Remote-URL mismatch ⇒ delete-aside and re-init. Fetch credentials are the
card's `token_ro or token`, delivered per child process via an
app-container-local askpass (`/tmp/…` — NEVER on the Dev-readable
volume); the app image gained `git`+`git-lfs`, its first and only
subprocess seam (`adapters/git.py`: minimal env, 900 s timeout, never
raises). Storage: compose named volume `mirrors: {}` → real name
`devcake_mirrors`; app mounts it rw at `/mirrors`; `dev-run.yaml` mounts
the SAME volume `devcake_mirrors:/mirrors:ro` (measured on Dagu 2.10.5,
2.11.3 AND 2.13.0 — the 2026-08-13 drill re-inspected a live provision
container: `/mirrors=RW:false`, `:ro` kernel-enforced, named volumes
resolve — this amends `13-deployment.md`'s old "no volumes needed" note).
2026-08-13 rider (ADR-0016 addendum): the mirror gained a second read-side
consumer — external `<card>/<skill>` skills are served from the bare
mirror via app-side git plumbing (`tree_head`/`read_skill_tree`/
`read_skill_file`), and skill sources (dedicated connections — ADR-0016 addendum 2) join the dispatch gate's
needed-set; the mirror contract itself is unchanged.

### 2 — Sync is a fail-closed dispatch precondition

`dispatch()` computes the run's mirror-eligible set (work repo + ONBOARD
routing set + reference repos + configured blocker-work repos; MAPPER =
its one repo *(runtime: STEWARD = its one repo)*; HELLO/OAUTH none) and awaits `ensure_fresh` BEFORE the
activity-repo push (hoisted `resolve_blocker_work`; gating after the push
would write one snapshot commit per gated cycle). Freshness:
`repo_mirror.sync_max_age_seconds` (default 0 = sync before every
dispatch; a sync completing after the caller asked satisfies it — that is
what lets concurrent dispatches and the warm-up COALESCE onto one fetch
via per-name locks, Semaphore(4) overall). Failure ⇒ the mission does not
dispatch this cycle: no container, no attempt burned, reason on the
missions row (the gate dict written inside dispatch IS the dict the poll
row-builder reads — no extra plumbing) and on `/health.blocked_reasons`,
natural retry next poll. Auth-classed sync failures latch the EXISTING
per-repo forge breaker (`mirror sync: …`); `repository not found` is
deliberately NOT auth app-side (a 404 is a config problem; the breaker's
"update the token" remediation would mislead). The mapper's periodic path
*(runtime: steward periodic path)*
skips with outcome `mirror_stale` (never raising into the poll segment);
`Run now` returns the reason as a 422. Warm-up runs as a background task
started by the poll loop — never awaited at boot (structure-guard
enforced), and `verify_writable()` (boot, no network) surfaces a
root-owned-volume misfire as a critical alert with the one-line fix.

### 3 — Devs clone from the mirror and cannot tell

The runspec carries `DEVCAKE_MIRROR_PATH` ("" = direct: internal repos)
and extras entries gain `mirror_path` — with their read tokens DROPPED
(the clone needs none; strictly less secret material in transit) and the
old omit-when-tokenless rule bypassed for mirrored entries (omitting a
repo whose mirror gates dispatch would be incoherent). The entrypoint
clones `file://<mirror_path>` — `file://`, never the bare path: plain
paths silently IGNORE `--depth` and hardlink, `file://` forces the smart
transport (depth honored; test-pinned) — then `git remote set-url origin
<real URL with clone_user@>`, so push, PR/MR, and the playbook's own
pre-PR `git fetch origin` hit the real forge exactly as a direct clone.
A mirror-clone failure is exit 13 class `DEV_FORGE` ALWAYS (never
`DEV_FORGE_AUTH` — a file:// clone cannot be a credential failure, and
latching the repo breaker over infrastructure trouble would be wrong).
Extras keep `--depth 1` and stay non-fatal.

### 4 — LFS: pointer-compatible by default, real content on demand

`repo_mirror.lfs=false` (default): pointer files ride as ordinary git
objects — byte-identical to the pre-git-lfs images, enforced by the
entrypoint's `git lfs install --skip-smudge`. `lfs=true`: sync also runs
`git lfs fetch origin <default_branch>` (default-branch scope v1) into
the bare mirror's own LFS store, and Dev clones — full smudge, via
git-lfs's **standalone file:// transfer** — materialize real content
straight from the RO mount. Probe-verified 2026-08-03 before wiring: a
2 MB object round-tripped bit-exact through bare-mirror `lfs fetch` →
`file://` clone, at full depth AND `--depth 1`; `DEVCAKE_LFS` rides the
"1"/"" flag convention.

### 5 — Exceptions and security posture (audit 2026-08-03)

Internal-forge synthesized repos and activity repos stay direct-clone —
a SECURITY decision, not a LAN optimization: zero-repo mission isolation
lives in per-mission token scope (ADR-0010, `14` §2 Zone B), and putting
those repos on a deployment-shared volume would dissolve exactly that
boundary. Cross-Dev WRITE contamination is impossible by construction
(`:ro` kernel-enforced; app-only writes; per-repo bare dirs, no shared
objects). The deliberate widening, recorded in `14`: every Dev can READ
the source of every configured repo via the shared mount — consistent
with repos being deployment-global and DevCake being explicitly
non-multi-tenant, but now ambient; if mutually-distrustful instances
ever matter, the passive-Gitea transport (per-credential read scoping)
is the documented alternative. `14`'s "no host/volume secret mounts"
stays true: mirrors carry repo content, not secret material; sync
stderr is redacted before ledger/health/reasons; mirror tokens are the
already-registered repo tokens. Pre-existing and unchanged: askpass
echoes the work-repo token for whatever host a Dev fetches.

### 6 — Consequences accepted and named

- A reference/blocker repo whose sync fails now hard-gates whole
  missions that previously ran degraded (extras were silently omitted).
  Fail-closed by decision; the non-dismissable `mirror-sync` alert and
  the gate reason name the repo.
- Cold warm-up clones the full catalog once, in the background; the
  first cycle's dispatches wait only for their own repos (coalescing),
  bounded by the 900 s per-fetch cap. Manual polls 409 while a cycle
  holds the lock, as today.
- Disk: full-history heads+tags mirrors with `gc.auto=0`; pack
  accumulation is surfaced via `/health.repo_mirror.disk`, offline
  `git gc` is the documented maintenance, and the volume is DISPOSABLE
  (`docker volume rm devcake_mirrors` + restart re-warms) — excluded
  from the backup set on purpose.
- Rare accepted races: `--prune` vs an in-flight clone (exit-13 retry;
  objects stay present under gc.auto=0), URL edits mid-flight (sticky
  routing gates most), breaker flap between the API probe and git-auth
  reality (cosmetic flicker; dispatch stays correctly gated).
- Success metric vs the incident workload: per-run forge transfer drops
  from ~300 MB × N Devs to delta fetches from one app IP.

### 7 — Dagu 2.10.5 → 2.11.3 → 2.13.0 (severable rider)

Audited: two breaking changes in range, neither applies (v2.11.0 CORS
hardening — DevCake talks server-side, the SPA only links to Dagu's own
UI; v2.10.6 token-TTL cap — basic auth, no tokens); no REST or container
schema changes across our six endpoints; `volumes:` is now officially
documented; container cpu/memory/pids limits did not exist at 2.11.3
(delivered 2026-08-13 at 2.13.0 via the docker-executor form's nested
`resources:` — `14` §6). Explicitly NOT adopted: controller DAGs, LLM
steps, human-task API — all business logic stays in the app; the DAG
remains a dumb launcher. All three volume probes re-measured green on
2.11.3; digest pinned. If live smoke ever implicates the bump, revert
its commit alone — the mirror does not depend on it.

2026-08-13 — 2.13.0: same posture, re-audited (2.12/2.13 notes: UI,
webhook, documents, build-workflow — nothing on our six endpoints) and
live-drilled per `13-deployment.md` §4; §4 above records the volume
probes re-measured on 2.13.0. One real break (2.12 wiki store vs our RO
dags bind) fixed in compose via `DAGU_WIKI_DIR`.

## Addendum — the repository's HEAD is the truth; a wrong pin is loud (2026-09-05)

Field finding: a fleet of reference cards bulk-created with `default_branch: main` mirrored repositories whose default is `master`. The sync set the mirror's HEAD to the pin without checking the branch existed, recorded a green sync, and every `file://` clone checked out nothing ("No commits yet on main"); Devs reported the repositories as "not mounted". Rulings:

1. **Blank is the contract for repository cards too**: the repository's own default, inquired from its HEAD (`ls-remote --symref`) before every sync — the skill-source path, unchanged and probed every sync on purpose. A value is a deliberate pin. The model default is blank, so a bulk import never invents `main` again; existing explicit pins keep working. No migration code, ever — operators (or their agents) blank or correct cards, and the admin's **Discover default branch** actions fill the draft with what the repository names.
2. **Verify before the HEAD moves, on both paths.** A pin the repository does not have, or a probed default the fetch never brought, fails that card's sync with both names in the ledger detail; the previous valid HEAD stays for any clone in flight. Zero branches (an empty repository awaiting its first commit) keep their pin. Fail-closed stays for every sourced repository, work and reference alike — a wrong card stops that board's dispatch, loudly, until fixed.
3. **Last-good means "HEAD names a branch that is there"** (`has_last_good`): a mirror whose HEAD dangles is never served as stale content to a memory or skill mount.
4. **One resolver** (`RepoCache.resolved_branch`: pin, else the mirror's HEAD when its target exists, never a bare-init HEAD) feeds the Dev's env and playbook and the branch-protection probe and apply; an unresolved blank card defers dispatch (mission and steward) and refuses protection apply rather than render an empty name. The claims writer needs no name: a blank card clones the notebook at the repository's own HEAD and pushes the branch it checked out. The health protection probe covers work repos only (cards a board lists under `repos`).
6. **An empty repository bootstraps.** With no HEAD symref and no branches there is no default to inherit; a blank card's mirror takes `main` as its HEAD so the first push (a Dev's first commit, the claims writer's first notebook) creates that branch, and a pin keeps its own name. A repository that has branches but advertises no HEAD symref still asks for a pin. A remote probe is bounded (30 s) so a black-holed host cannot hold a sync or a bulk Discover; the bulk action answers inside the admin proxy's window with finished cards and "timed out" for the rest.
5. **The provision step keeps a belt**: a mirror clone that checked nothing out of a mirror that has branches is removed and noted (strict memory mounts fail the run); the work-repo clone fails as `DEV_FORGE` instead of starting a Dev on an empty tree.

## Related

- Implement: `app/devcake/adapters/git.py`, `domain/repo_mirror.py`,
  `domain/orchestrator/dispatch.py` (gate, spec_env, extras),
  `domain/steward_service.py` *(was `mapper_service.py` — MAPPER→STEWARD)*, `api/{main,poll,health,config_service}.py`,
  `images/common/dev_entrypoint.py` + `devcake_dev/workspace/clone.py`,
  `docker-compose.yml`, `dagu/dags/dev-run.yaml`, `app/Dockerfile`,
  `images/Dockerfile` (git-lfs — ADR-0023 floor amendment).
- Evidence: `app/tests/test_repo_mirror.py` (decisions),
  `test_repo_mirror_git.py` (real-git incl. the file:// depth contract),
  gate tests in `test_repo_mirror.py` via the dispatch rig; the LFS and
  Dagu probes of 2026-08-03 (recorded above).
- Operator: `07-dev-runtime.md` §§3, 5, 7b; `13-deployment.md` §§1, 4-5,
  8; `14-security.md` §§5-6, 11; `15-errors-and-retries.md` §§1-2;
  `11-admin-panel.md` (Policies).
