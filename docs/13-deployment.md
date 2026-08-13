# 13 — Deployment: docker-compose, Dagu, Networking, Runbook

> **Audience:** operators and implementers.
> **Depends on:** `01-architecture.md`, `07-dev-runtime.md`, `12-observability.md`,
> **`14-security.md` (product security contract — read first).**

**Normative posture:** DevCake runs on a **dedicated host** you control. Dagu
holds host `docker.sock` (root-equivalent). Control-plane ports bind
**loopback** by default. Single operator; not multi-tenant SaaS. Operator
checklist before first real EXECUTE: `14-security.md` §9.

Bake builds images; Compose runs the stack only (never builds `devcake/*`).

## 1. Service names, volumes, network (normative — these are DNS names other docs reference)

- Services: `app`, `dagu`, `redis`, `openobserve`, `admin`, `otel-collector`,
  `fluentbit`, `gitea` (internal fallback forge). Long-lived services carry
  `restart: unless-stopped` in compose — a compose fact, not an app knob
  (there is deliberately no UI control for it).
- Volumes: `devcake_data` (→ `app:/data` — **secrets + config + state**),
  `devcake_mirrors` (→ `app:/mirrors` rw + the Dev **provision** container
  `:ro` — ADR-0024 source mirrors; DISPOSABLE cache, excluded from backups,
  §8), `dagu_data`, `redis_data`, `oo_data`, `gitea_data`.
- Host bind (NOT a named volume): `$DEVCAKE_WS_HOST` (→ `app:/workspaces` rw;
  each run's `<run_id>` subdir binds into its two Dev containers, ADR-0025).
  Host-absolute, derived + `mkdir`'d `0700` by `./up.sh` (default
  `./workspaces`). Holds repo source + activity transcripts + agent output —
  treat like `gitea_data` (`14` §1); DevCake-exclusive (the sweep/wipe touch
  every run-id-shaped child) and excluded from backups (§8).
- **Do NOT override `COMPOSE_PROJECT_NAME`** (AUD-020): the compose project is
  fixed to `devcake` (`name: devcake`), which makes the mirror volume's real
  name `devcake_mirrors` — the literal string `dev-run.yaml` mounts. A custom
  project name would rename the volume (`<proj>_mirrors`) while the DAG still
  mounts `devcake_mirrors`, so every provision step would fail to find the
  mirrors. If a rename is ever required, `dev-run.yaml`'s volume name must
  change in lockstep.
- Networks:
  - **`devcake_control`:** `app`, `admin`, `dagu`, Redis, OpenObserve,
    fluent-bit, otel-collector, gitea.
  - **`devcake_runtime`:** ephemeral Devs, Redis, **otel-collector**, gitea.
    **OpenObserve is not on runtime** (A23). Devs retain outbound forge/package
    access but cannot resolve `app`, `admin`, or Dagu.
- The collector is the Dev-side telemetry boundary: Devs export OTLP
  **credential-free**; only the collector holds `OO_INGEST_*`
  (`12-observability.md` §1, `14` §10).

## 2. Annotated `docker-compose.yml` skeleton

Truth is the committed `docker-compose.yml` (digest pins, full healthchecks,
logging). Skeleton for orientation — **ports are loopback-bound**:

```yaml
name: devcake

services:
  app:
    image: devcake/app:${DEVCAKE_TAG:-latest}   # built by: docker buildx bake
    pull_policy: never
    env_file: .env                       # bootstrap secrets ONLY (schema v4)
    volumes:
      - devcake_data:/data               # NO docker.sock — kill/reconcile via Dagu API
      - mirrors:/mirrors                 # ADR-0024 source mirrors (app = only writer)
      - ${DEVCAKE_WS_HOST}:/workspaces   # ADR-0025 per-run workspace base (host bind)
      - ./images/common:/srv/images/common:ro   # TEST FIXTURE ONLY — entrypoint-render
                                         #   tests read the real Dev entrypoint tree;
                                         #   the running app never imports from it
    networks: [control]
    # no host ports — reach API only via admin proxy

  dagu:
    image: ghcr.io/dagucloud/dagu:2.13.0   # PIN — see §4
    ports: [ "127.0.0.1:8525:8080" ]      # loopback only — docs/14
    environment:
      - DAGU_AUTH_MODE=basic
      - DAGU_AUTH_BASIC_USERNAME=${DAGU_USER}
      - DAGU_AUTH_BASIC_PASSWORD=${DAGU_PASSWORD}
      - DOCKER_GID=${DOCKER_GID}
    volumes:
      - dagu_data:/var/lib/dagu
      # :ro — DAG YAML is trusted launch code (14 §5). Runtime state stays on
      # dagu_data (data/logs/suspend); Dagu may WARN that it cannot write
      # .dag.index under the RO bind — non-fatal (in-memory rebuild).
      - ./dagu/dags:/var/lib/dagu/dags:ro
      - ./dagu/init:/etc/custom-init.d:ro
      - /var/run/docker.sock:/var/run/docker.sock  # ⚠ host root-equivalent — 14 §5
    # Compose overrides entrypoint: runs custom-init.d via `sh` (bit-independent)
    # then execs stock /entrypoint.sh — see live docker-compose.yml.
    networks: [control]

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec",
              "--requirepass", "${REDIS_PASSWORD}"]
    networks: [control, runtime]

  openobserve:
    image: public.ecr.aws/zinclabs/openobserve:v0.91.5
    ports: [ "127.0.0.1:5080:5080" ]
    networks: [control]                  # NOT on runtime

  otel-collector:
    # Devs → :4318 unauthenticated; collector → OO with OO_INGEST_*
    networks: [control, runtime]

  admin:
    image: devcake/admin:${DEVCAKE_TAG:-latest}
    pull_policy: never
    ports: [ "127.0.0.1:8080:80" ]        # loopback only
    environment:
      - ADMIN_USER=${ADMIN_USER}
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}
    networks: [control]
    # nginx /api/ proxies with client_max_body_size 96m (ADR-0030: base64
    # composer attachments + >1MB settings-bundle imports; nginx's 1MB
    # default silently 413'd both — server-side caps stay authoritative)

  gitea:
    ports: [ "127.0.0.1:3300:3000" ]
    networks: [control, runtime]         # Devs clone/push; app provisions

# Dev harness images are Bake-built only — never compose services (§6).
```

Do **not** re-add OpenObserve to `runtime` via override — that reopens the OO
API to Devs (`14` §10).

## 3. `.env` — bootstrap only (schema v4 / ADR-0011)

`.env` holds **stack bootstrap secrets** — passwords and `DOCKER_GID` needed
before the admin GUI is up. **PMO/forge/model secrets are entered as VALUES on
the Config page** and stored under `/data/secrets/` (`10-persistence.md`,
`14` §4). See committed `.env.example` for the full list.

```
# OpenObserve root + ingest service account (required; weak values refuse boot)
OO_ROOT_EMAIL=admin@example.com
OO_ROOT_PASSWORD=
OO_INGEST_EMAIL=
OO_INGEST_PASSWORD=

# Internal Gitea admin (required)
GITEA_ADMIN_USER=devcakeadmin
GITEA_ADMIN_PASSWORD=

# Dagu / Redis / admin basic auth
DAGU_USER=devcake
DAGU_PASSWORD=
REDIS_PASSWORD=
ADMIN_USER=admin
ADMIN_PASSWORD=

# Host docker group (auto by ./up.sh; or: stat -c %g /var/run/docker.sock)
DOCKER_GID=

# Per-run workspace base — HOST-ABSOLUTE (ADR-0025; auto by ./up.sh)
DEVCAKE_WS_HOST=

# Optional local sandbox only — never in production
# DEVCAKE_ALLOW_INSECURE=1

DAGU_UI_URL=http://localhost:8525
OO_UI_URL=http://localhost:5080
```

Empty or `change-me*` bootstrap passwords refuse app boot unless
`DEVCAKE_ALLOW_INSECURE=1`.

## 4. Dagu configuration

- **Version pinned** (`2.13.0` since the 2026-08-13 ops bump; `2.11.3` since ADR-0024; originally verified live against v2.10.5 end-to-end at M1. The 2.11.3 bump re-measured the volume probes and audited the release notes: the v2.11.0 CORS hardening does not apply — DevCake calls the API server-side and the SPA only LINKS to Dagu's own same-origin UI — and the v2.10.6 token-TTL cap is moot under basic auth. The 2.13.0 bump audited the 2.12/2.13 release notes — no REST-API or container-schema changes (UI, webhook, documents, build-workflow features) — and was **live-drilled 2026-08-13**: healthcheck + init hook green, `dev-run.yaml` loads unchanged, executor REST start/status/stop verified (stop kills the container), hello battery green twice, and the ADR-0024 guarantees re-measured (provision `/mirrors=RW:false` — `:ro` kernel-enforced; dev step workspace-only; `${params.*}` interpolation in volume sources works). **One real break found and fixed:** the 2.12 wiki/documents store defaults to `<dags_dir>/wiki` and crash-loops the server against our deliberately-RO dags bind — no disable knob exists, so compose sets `DAGU_WIKI_DIR=/var/lib/dagu/wiki` (the writable state volume; the feature stays unused). Back up `dagu_data` before upgrading (a state-format migration could make rollback to 2.11.3 lossy — acceptable, dagu history is advisory, the board is truth. The step `container:` schema still has no HostConfig fields at 2.13.0, and the docker-executor form's `host:` block passes Docker SDK HostConfig fields **with a measured trap**: only strings and string-arrays decode at the top level — the cgroup numerics (`Memory`/`NanoCpus`/`PidsLimit`) and struct-arrays (`Devices`) live in HostConfig's EMBEDDED `Resources` struct, which the 2.13.0 decoder SILENTLY DROPS unless they ride a nested `resources:` key (missing mapstructure `Squash` — dagucloud/dagu#2557, our upstream fix). That nested form is the door for the planned per-container limits; see the ops-quartet plan — and never quote the first half of this sentence without the trap). Controller/LLM/human-task DAG features stay deliberately NOT adopted: all business logic stays in the app, the DAG remains a dumb launcher. The project rebranded to `dagucloud/dagu` and releases fast; on upgrade, re-check this section against the new version.
- **Auth (verified at M1):** v2.10.5 locks the API by default (401). We run `DAGU_AUTH_MODE=basic` with `DAGU_AUTH_BASIC_USERNAME/PASSWORD` (env names confirmed from the source's config loader); the app sends HTTP Basic on every call; `/api/v1/health` stays open for the compose healthcheck.
- **docker.sock access (verified at M1):** the image's entrypoint always drops to uid 1000 via sudo, and its `DOCKER_GID` group setup is broken on the ubuntu base (alpine-only `addgroup`). Our `dagu/init/10-docker-group.sh` (mounted at `/etc/custom-init.d/`) creates the docker group with `groupadd`, so the daemon runs as `dagu:docker` — least privilege, no root daemon. Stock `/entrypoint.sh` only runs custom-init scripts that are `+x`, and the bind is `:ro`, so a non-executable host file is a silent skip → `sudo: unknown group #$DOCKER_GID` crash-loop. Compose therefore wraps the entrypoint and always invokes hooks via `sh` before handing off (does not depend on the host execute bit; git still tracks the script as `100755`).
- **Step ids are `^[a-zA-Z][a-zA-Z0-9_]*$`** (verified) — underscores, not dashes: the DAG's steps are `provision` and `run_dev` (ADR-0025).
- **Two dependent steps per run (ADR-0025):** `provision` mounts the mirrors RO + this run's workspace, clones, exits; `run_dev` `depends: [provision]` and mounts ONLY the workspace. `$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace` binds each run's host dir — `$DEVCAKE_WS_HOST` comes from the **dagu service env** (compose) and resolves on the daemon host, so it must be host-absolute; `${params.RUN_ID}` interpolates in the source (probe-verified 2.11.3). A DAG-level `preconditions` guard pins `${params.RUN_ID}` to `re:^[A-Za-z0-9_-]{6,64}$` so a manual Dagu-UI run with default params cannot bind the whole base into a container (dry-run-verified).
- **Auto-retry disabled (verified):** the DAG sets `retry_policy: {limit: 0}` — Dagu would otherwise auto-retry failed runs 3×, fighting DevCake's own attempt counting (`15-errors-and-retries.md` §2).
- **UI:** served at root on host port 8525; the admin panel links to it with a button (no iframe, no base-path/proxy configuration — confirmed decision).
- **v2 YAML is snake_case only** (`timeout_sec`, not `timeoutSec` — camelCase keys are rejected with a hint). The step-level `container:` field is the current preferred syntax; `action: docker.run` and the legacy `type: docker` shapes also parse but are not used here.
- **Timeout ownership:** the app watchdog owns the real kill (`04-orchestrator.md` §5) via Dagu's **stop endpoint** (verified: SIGTERM → SIGKILL after `max_clean_up_time_sec` → container force-removed → run status `aborted`). Dagu gets a belt-and-suspenders DAG-level `timeout_sec` set to *app timeout + 30 min* so it can never fire first — satisfying the mission-doc requirement that Dagu never times out a run prematurely.
- **Trigger (verified):** `POST /api/v1/dags/dev-run/start` with body
  `{"params": "{\"RUN_ID\": \"LINEAR-ENG-142-3-EXECUTE-9GX2TQ\", \"IMAGE\": \"devcake/dev-claude-code:latest\", \"TRACEPARENT\": \"<w3c>\", …}", "dagRunId": "LINEAR-ENG-142-3-EXECUTE-9GX2TQ"}` —
  `params` is a JSON-**encoded string** of named params; the client-chosen `dagRunId` (`^[-a-zA-Z0-9_]+$`, ≤ 64 chars — our human-readable run id fits) makes duplicate triggers return **HTTP 409** `already_exists` (`04-orchestrator.md` §6.3). Auth: **HTTP Basic** with `DAGU_USER` / `DAGU_PASSWORD` (same as `DAGU_AUTH_MODE=basic` above) — not a Bearer API key.
- **Stop (watchdog kill, verified):** `POST /api/v1/dag-runs/dev-run/{dagRunId}/stop`, empty body, returns 200 immediately (async).
- **Params are visible unmasked in the Dagu UI and run API** (verified) — therefore params carry only non-secret values (`RUN_ID`, `IMAGE`, `TRACEPARENT`) **plus one deliberate exception**: the per-run scoped Redis ACL credential (`REDIS_USER`/`REDIS_PASSWORD`), acceptable because the Dagu UI/API is itself authenticated and the credential is revoked at finalization (`09-messaging.md` §1a). All real secrets reach the Dev via the Redis `runspec.get` channel (`14-security.md` §4). Keep params small (they travel as one CLI arg; practical ceiling ~128 KiB).
- **The single `dev-run` DAG** (`dagu/dags/dev-run.yaml`) — all business logic stays in the app; the DAG is a dumb container launcher. This exact shape (container field, param interpolation into `name`/`env`, network attach, auto-removal, blocking on the entrypoint) was validated and executed on v2.10.5:

```yaml
# dev-run: one run = two dependent Dev containers (ADR-0025). Only non-secret
# params; everything else via runspec.get. $DEVCAKE_WS_HOST is dagu-service env.
timeout_sec: 9000               # 150 min belt-and-suspenders; the app watchdog kills at 120
max_clean_up_time_sec: 30       # grace between SIGTERM and SIGKILL on stop
retry_policy:
  limit: 0                      # Dagu must NOT auto-retry: DevCake owns attempt counting
preconditions:                  # fence manual runs: default/empty RUN_ID must not bind the base
  - condition: "${params.RUN_ID}"
    expected: "re:^[A-Za-z0-9_-]{6,64}$"

params:                         # RUN_ID, IMAGE, TRACEPARENT, REDIS_USER, REDIS_PASSWORD
  - {name: RUN_ID, default: ""}     #   (per-run ACL user dev-{run_id}, 09 §1a — the one
  # …                               #    param-borne secret; revoked at finalization)

steps:
  - id: provision                    # ADR-0025 step 1: trusted code, no agent
    container:
      image: ${params.IMAGE}
      name: prov-${params.RUN_ID}
      network: devcake_runtime
      startup: entrypoint            # entrypoint sees DEVCAKE_PHASE=provision
      volumes:
        - devcake_mirrors:/mirrors:ro                    # RO source mirrors (ADR-0024)
        - $DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace   # this run's host-bind workspace
      env:
        DEVCAKE_PHASE: provision
        DEVCAKE_RUN_ID: ${params.RUN_ID}
        # … TRACEPARENT, REDIS_URL, REDIS_USER, REDIS_PASSWORD as before
  - id: run_dev                      # ADR-0025 step 2: the agent
    depends: [provision]
    container:
      image: ${params.IMAGE}
      name: dev-${params.RUN_ID}
      network: devcake_runtime
      keep_container: false          # force-removed on every exit path; post-mortem in step logs + OO
      startup: entrypoint            # entrypoint sees DEVCAKE_PHASE=harness
      volumes:
        - $DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace   # ONLY the workspace — no /mirrors
      env:
        DEVCAKE_PHASE: harness
        DEVCAKE_RUN_ID: ${params.RUN_ID}
        # … TRACEPARENT, REDIS_URL, REDIS_USER, REDIS_PASSWORD as before
```

> Notes from the live verification: no SECRET `volumes:` are needed (credentials arrive via `runspec.get`, never mounts). The two workspace-carrying mounts are ADR-0025: `devcake_mirrors:/mirrors:ro` (a NAMED volume by real docker name, because bind sources resolve on the *daemon host*) on the provision step ONLY, and `$DEVCAKE_WS_HOST/${params.RUN_ID}:/workspace` (a host-bind, `$DEVCAKE_WS_HOST` from the dagu service env → host-absolute) on both steps; `volumes:` support, `:ro` enforcement, `${params.X}`+`$ENV` interpolation in a volume source, and two dependent container steps sharing a per-run bind while the second lacks the first's mounts were all measured on 2.11.3. The stock `ghcr.io/dagucloud/dagu` image ships **no docker CLI**, so a shell `docker run` fallback is not viable — irrelevant, since the native executor (Moby SDK over the mounted socket) is fully verified. The image's stock `DOCKER_GID` entrypoint is broken on the ubuntu image — our `dagu/init/10-docker-group.sh` custom-init fix (always invoked via `sh` by the compose entrypoint wrapper, so host `+x` is not required) creates the docker group so the daemon runs as `dagu:docker` (not root). In the `container:` env map, host-process env does **not** resolve implicitly in the ENV values, but it DOES expand in a `volumes:` SOURCE (that is how `$DEVCAKE_WS_HOST` reaches the bind); per-step literal `DEVCAKE_PHASE` needs no expansion.

## 5. Two-level containers and networking

Dagu holds the host `docker.sock`, so Dev containers are **siblings** of the
compose stack (docker-outside-of-docker). **Dedicated host only** — socket
access is root-equivalent (`14-security.md` §5).

The DAG's `network: devcake_runtime` key attaches Devs at spawn (verified via
`docker inspect`), giving them:

- Redis Streams by service DNS (per-run ACL users — `09-messaging.md`);
- **otel-collector** for OTLP (unauthenticated; no OO credentials in Dev);
- optional **internal Gitea** for zero-repo missions;
- full outbound access for forge/package/model traffic (by design — `14` §6);
- **no** Docker-network route or DNS entry for `app`, `admin`, Dagu, or
  **OpenObserve**.

Names: each run is two containers — `prov-{run_id}` then `dev-{run_id}` via the
DAG's `name:` keys (ADR-0025), with the human-readable run id format of
`02-domain-model.md` §7 — the run id, traces, and Redis streams match (a
`docker ps` shows both container names).

### 5a. Operator deploy rules (security)

1. Keep published ports on **127.0.0.1** (default compose). Use SSH tunnels for remote access.
2. Do not run on a shared multi-tenant Docker host.
3. Treat `/data` volume backups as **secret dumps** — likewise `gitea_data` backups (repo content + Gitea's credential DB) and any settings-export bundle containing secrets or setup values (ADR-0013).
4. Before first real EXECUTE: branch protection + team membership + checklist in `14` §9.
5. Mount `./dagu/dags` **read-only** into Dagu (compose does: `:ro`). The dev-run DAG's own `devcake_mirrors:/mirrors:ro` mount is likewise non-negotiable — the `:ro` is the mirror-poisoning defense (ADR-0024).

## 6. Image build matrix (Bake only — `docker-bake.hcl`)

**Compose never builds DevCake images.** Bake is the single source of truth. Run specs still reference Dev images by **tag** (`devcake/dev-*:latest`) — digest pinning is not implemented; re-bake lockstep with app upgrades:

| Command | Builds |
|---|---|
| `docker buildx bake` | `app` + `admin` (control plane; **prod** app — no pytest) |
| `docker buildx bake app-test` | `devcake/app-test` (pytest + `tests/` for CI) |
| `docker buildx bake images` | `hello` + all three harnesses (shared `base` stage) |
| `docker buildx bake ci` | `app` + `app-test` + `admin` + `hello` (no full harness matrix) |
| `docker buildx bake all` | everything — **use this on first install and full upgrades** |

**Cache:** opt-in local `.buildx-cache/` — `BAKE_LOCAL_CACHE=1 docker buildx bake …` (needs a docker-container builder or the containerd image store; the default `docker` driver cannot export cache, so plain `bake all` works everywhere without it). CI: `docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl …` for GitHub Actions `type=gha` cache.

**GitHub Actions:** `.github/workflows/ci.yml` bakes group `ci` + pytest on every PR; `docker-images.yml` bakes harnesses when `images/**` changes; `docker-publish.yml` (manual) pushes all images to GHCR.
| Image | Bake target | Context / Dockerfile target | Default tag |
|---|---|---|---|
| `devcake/app` | `app` → `runtime` | `./app` | `devcake/app:${TAG}` |
| `devcake/app-test` | `app-test` → `test` | `./app` + pytest | `devcake/app-test:${TAG}` |
| `devcake/admin` | `admin` | `./admin` | `devcake/admin:${TAG}` |
| `devcake/dev-claude-code` | `claude-code` | `./images` → `claude-code` (CLI **pinned**) | `devcake/dev-claude-code:${TAG}` |
| `devcake/dev-grok-build` | `grok-build` | `./images` → `grok-build` | 〃 |
| `devcake/dev-codex` | `codex` | `./images` → `codex` (CLI **pinned**) | 〃 |
| `devcake/dev-hello` | `hello` | `./images` → `hello` | CI stub |

`TAG` / `DEVCAKE_TAG` default to `latest`. Pin a release with:

```bash
export DEVCAKE_TAG=$(git rev-parse --short HEAD)
docker buildx bake all
docker compose up -d          # compose reads DEVCAKE_TAG for app/admin
```

Harness image tags follow **`DEVCAKE_TAG`** (same as app/admin — default `latest`): `HARNESSES[…].image` in `app/devcake/harness.py` is `devcake/dev-*:${DEVCAKE_TAG}`. Digest pinning at dispatch is **not** implemented; re-bake lockstep with app upgrades and keep `DEVCAKE_TAG` exported for `compose up` so dispatch matches what you baked.

Which Dev image a run uses is `HARNESSES[harness_template].image` (`08-harness-templates.md` §2); `docker_image` is no longer stored config. Since any harness is selectable from the admin panel at any time, **all three `devcake/dev-*` images must be baked locally**.

## 7. Log shipping for non-instrumented services

Compose services that opt into the **fluentd logging driver** (`dagu`, `redis`, **`gitea`**, and others wired in `docker-compose.yml`) ship stdout to **fluent-bit** (`fluentbit` service, host `127.0.0.1:24224`) → OpenObserve stream `container_logs`. The stack does **not** use Vector. **OTLP:** the **app** exports traces directly to OpenObserve; **Devs export unauthenticated OTLP to `otel-collector`**, which alone holds ingest credentials and forwards to OO (`12-observability.md` §1). Fluent-bit's OO path currently hardcodes org `default` in `fluentbit/fluent-bit.conf` (and SPA deep-links often assume `org_identifier=default`); changing `OO_ORG` without updating those is a footgun.

## 8. Runbook

- **First run:** `cp .env.example .env` → strong bootstrap passwords → `./up.sh --bake` (discovers `DOCKER_GID` from the docker socket, bakes all images, `compose up -d`) → open `http://localhost:8080` → **Configuration** (PMO + model/harness secrets, Dev Types) and **Repositories** (`#/repos`: forge tokens + merge posture) → connection tests → done. Day-to-day restarts: `./up.sh`. Labels bootstrap on startup; **OpenObserve ingest user** is auto-created at app boot from `OO_INGEST_*` (dashboard/alerts still optional via `scripts/provision_oo.py`). Then `14` §9 checklist before first EXECUTE.
- **Upgrading from a pre-Bake install (app ran as root):** the baked app image runs as non-root uid 1000, so `/data` files written by the old root-running app (config.yaml, run records, secrets) crash-loop boot with `PermissionError`. One-time fix before `up`:
  `docker run --rm -v devcake_devcake_data:/data alpine chown -R 1000:1000 /data`

### 8a. Protect the default branch (operator supply-chain control — docs/14 §2 zone C)

**Branch protection** is a policy you set **on the forge** (GitHub / GitLab /
Gitea) for a branch name — in DevCake, the repo’s `default_branch` (usually
`main`). The forge refuses direct pushes and merges that do not meet your rules
(PR required, ≥1 approval, checks, no force-push, no bypass for the Dev
account). DevCake does not implement those rules; it only **warns** when the
branch looks unprotected and does not hard-block dispatch (`14` §8).

Why it is mandatory for production-ish use: Dev containers hold a write-capable
forge token, and token scoping cannot separate “push a feature branch” from
“merge to the default branch” (both are often `contents: write`). **Per-repo
`auto_merge` off only stops the app from merging that repo** — it does not
change what the Dev token can do (`14` §2 zone C). Full actor/token
walkthrough: `14` §2 and the README “How forge merges are controlled.”

Recommended operator setup:

1. **Protect `default_branch`** on every work repo.
2. **Write token** for EXECUTE (push + open PR); app reuses it for merge if
   that repo's `auto_merge` is later enabled (Repos page, per card — ADR-0020).
3. **RO token** for non-EXECUTE (recommended).
4. **Reviewer token** from a **different** account (**recommended**, app-only):
   formal approval so “require ≥1 approval” can pass without self-approval.
   Not the same as which Dev Type runs the REVIEW stage.
5. Leave each repo's **`auto_merge` off** until you want the app to
   squash-merge that repo after REVIEW.

- **GitHub:** ruleset or classic protection — *require a pull request before merging* + *require ≥1 approval*; do not grant the Dev write account a bypass. With a reviewer token configured, the **app** (not the REVIEW Dev) files a formal approval so per-repo `auto_merge` can still work if you enable it.
- **GitLab:** protect that branch (no direct pushes) and require ≥1 MR approval.

Forge connection test and `/health` surface protection state; amber warning when unprotected.
- **Upgrade:** `docker compose pull` (third-party images only) → `docker buildx bake all` → `docker compose up -d`. State survives (volumes). When the pinned **dagu** image is bumped, snapshot the `dagu_data` volume FIRST — `docker run --rm -v devcake_dagu_data:/v -v "$PWD":/out alpine tar czf /out/dagu_data-pre-bump.tar.gz -C /v .` — its run history is advisory (the board is truth), but a state-format migration can make rollback to the previous pin lossy (§4). There is **no auto-migration**: pre-v2 state (a v1 `config.yaml`, v1 run records) is refused or quarantined with instructions (`10-persistence.md` §§2, 3, 5) — the v1→v2 migrators were removed at v0 crystallization.
- **Upgrade — app and Dev images deploy in LOCKSTEP ("just rebuild it all"):** every deploy that touches `images/*` (and, to be safe, every upgrade) must run `docker buildx bake all`. There are **no cross-version compat shims** (founder decision): a new app with old images — or the reverse — fails loudly (missing descriptor vars crash the clone bootstrap; protocol shape changes reject old senders' output). The dev-run DAG uses `pull_policy: missing`, so stale locally-tagged `devcake/dev-*:latest` images keep running silently unless rebaked.
- **Deploy ordering under ADR-0025 — the DAG, compose, and env deploy lockstep.** `./dagu/dags` is a LIVE `:ro` bind-mount and Dagu re-reads the YAML per dispatch, so a new `dev-run.yaml` goes live at `git pull`, before `./up.sh`. In that window the old dagu container has no `DEVCAKE_WS_HOST` (the new bind source would expand empty → a root-owned junk dir at the host root), and images that predate the deploy mishandle `DEVCAKE_PHASE` (a post-ADR-0025 image without a phase exits 20 loudly — there is no single-container fallback). The deploy ritual closes the window: **`docker compose stop dagu` → `git pull` → `./up.sh --bake`**. `./up.sh --bake` now enforces the sharp edges itself (AUD-003/004): it stops dagu before the multi-minute bake (so a running dagu can't see the new DAG without the new env), resolves `DEVCAKE_TAG` once (shell env > `.env` > `latest`) and exports it for BOTH the bake and `compose up` so images and the running stack never desync, and upserts + `mkdir 0700`s `DEVCAKE_WS_HOST`. `up -d` force-recreates dagu with the new env. The explicit `stop dagu` before `git pull` is still recommended to close the brief pull→up window. In-flight DAG-runs are orphaned by the dagu recreate and reconcile-adopted at the next app boot.
- **Kill a stuck Dev:** admin → Runs page → open Dagu and stop the run (or `POST /api/v1/dag-runs/dev-run/<run_id>/stop`). The watchdog would do it at timeout regardless; the Mission reschedules per INV-3.
- **Logs:** admin → Consoles page (OpenObserve). One run = one trace ID (`12-observability.md` §2).
- **Data reset:** `docker compose down && docker volume rm devcake_devcake_data` — consequences per `10-persistence.md` §5 (Mission state is safe in the PMO).
- **Backups (the full story):** back up the **`/data` volume** (settings + secrets + run state) AND the **`gitea_data` volume** (internal repos with history/PRs + skill store) — `scripts/backup_data.sh` / `scripts/restore_data.sh` handle the former (2026-08: the primary target finally has its own pair; restore refuses while the app runs) and `scripts/backup_gitea.sh` / `scripts/restore_gitea.sh` the latter (restore refuses while gitea runs). Backups write via `*.partial` then `mv` so a mid-`tar` crash cannot clobber the last good file. Restores refuse any tarball without a matching `DEVCAKE_BACKUP_KIND` marker; a failed extract puts the previous tree back on its original paths. Both backups are secret dumps. With no output argument, `backup_data.sh` writes outside the checkout under `${XDG_DATA_HOME:-$HOME/.local/share}/devcake/backups`; set `DEVCAKE_BACKUP_DIR` or pass an explicit path to choose another protected destination. The `devcake_mirrors` volume (ADR-0024) and the `$DEVCAKE_WS_HOST` workspace tree (ADR-0025) are DISPOSABLE and **excluded** — the mirror re-warms, and workspaces are per-run scratch reclaimed at run end; both may hold repo source, so if you do snapshot the host treat it like a repo backup, but neither belongs in the settings/secrets backup set. **Settings bundles** (Config → Profiles & Export) are the portable, selective layer on top: configs diffable-plaintext, secrets/setup values encrypted by default; import lands as a profile on the target. They do not replace volume backups (no run state, no repo content).
- **Redis posture (2026-08):** the service runs `--maxmemory 1gb --maxmemory-policy noeviction` (a flooding Dev gets write errors instead of OOMing the control plane; the app XDELs ingress entries after every handled batch, so legit load never nears the cap) and `--aclfile /data/users.acl` (per-run Dev users survive a redis-only restart; the app `ACL SAVE`s on every user create/delete). With an aclfile configured redis ignores `--requirepass` (measured — an empty aclfile left the instance password-less), so the compose entrypoint writes the `default` user INTO the aclfile from `.env` on every boot, preserving `dev-*` lines. **Rotating `REDIS_PASSWORD`** is therefore just: change `.env`, `docker compose up -d redis` (the app needs a restart too, to reconnect with the new value).
- **Moving `.env` (setup values) between hosts:** export with "Setup values" checked → on the target, Import → "Download generated .env" → review the HOST-SPECIFIC lines (`DOCKER_GID`, `DEVCAKE_TAG`, `DEVCAKE_WS_HOST` — the last an absolute host path re-derived by `./up.sh`, so leave it for the script or set it to the target's checkout) → place at the repo root as `.env` → `./up.sh` (mkdirs the workspace base `0700`). If the invoking user is not uid 1000, `chown -R 1000:1000 $DEVCAKE_WS_HOST` so the app (uid 1000) can create run dirs — same precedent as the pre-Bake `/data` chown above. The app never writes the host's `.env` — exported values reflect the source stack at container start.
