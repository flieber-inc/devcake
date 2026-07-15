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
  `fluentbit`, `gitea` (internal fallback forge).
- Volumes: `devcake_data` (→ `app:/data` — **secrets + config + state**),
  `dagu_data`, `redis_data`, `oo_data`, `gitea_data`.
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
    networks: [control]
    # no host ports — reach API only via admin proxy

  dagu:
    image: ghcr.io/dagucloud/dagu:2.10.5   # PIN — see §4
    ports: [ "127.0.0.1:8525:8080" ]      # loopback only — docs/14
    environment:
      - DAGU_AUTH_MODE=basic
      - DAGU_AUTH_BASIC_USERNAME=${DAGU_USER}
      - DAGU_AUTH_BASIC_PASSWORD=${DAGU_PASSWORD}
      - DOCKER_GID=${DOCKER_GID}
    volumes:
      - dagu_data:/var/lib/dagu
      # Prefer :ro — DAG YAML is trusted launch code (14 §5). Committed compose
      # may still be RW; treat host tree as operator-controlled either way.
      - ./dagu/dags:/var/lib/dagu/dags
      - ./dagu/init:/etc/custom-init.d:ro
      - /var/run/docker.sock:/var/run/docker.sock  # ⚠ host root-equivalent — 14 §5
    networks: [control]

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec",
              "--requirepass", "${REDIS_PASSWORD}"]
    networks: [control, runtime]

  openobserve:
    image: public.ecr.aws/zinclabs/openobserve:v0.91.1
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

# Host docker group: stat -c %g /var/run/docker.sock
DOCKER_GID=

# Optional local sandbox only — never in production
# DEVCAKE_ALLOW_INSECURE=1

DAGU_UI_URL=http://localhost:8525
OO_UI_URL=http://localhost:5080
```

Empty or `change-me*` bootstrap passwords refuse app boot unless
`DEVCAKE_ALLOW_INSECURE=1`.

## 4. Dagu configuration

- **Version pinned** (`2.10.5` at spec time — everything in this section was **verified live against v2.10.5**, source + running server, and exercised end-to-end at M1). The project rebranded to `dagucloud/dagu` and releases fast; on upgrade, re-check this section against the new version.
- **Auth (verified at M1):** v2.10.5 locks the API by default (401). We run `DAGU_AUTH_MODE=basic` with `DAGU_AUTH_BASIC_USERNAME/PASSWORD` (env names confirmed from the source's config loader); the app sends HTTP Basic on every call; `/api/v1/health` stays open for the compose healthcheck.
- **docker.sock access (verified at M1):** the image's entrypoint always drops to uid 1000 via sudo, and its `DOCKER_GID` group setup is broken on the ubuntu base (alpine-only `addgroup`). Our `dagu/init/10-docker-group.sh` (mounted at `/etc/custom-init.d/`, the image's own hook) creates the docker group with `groupadd`, so the daemon runs as `dagu:docker` — least privilege, no root daemon.
- **Step ids are `^[a-zA-Z][a-zA-Z0-9_]*$`** (verified) — underscores, not dashes: the DAG's step is `run_dev`.
- **Auto-retry disabled (verified):** the DAG sets `retry_policy: {limit: 0}` — Dagu would otherwise auto-retry failed runs 3×, fighting DevCake's own attempt counting (`15-errors-and-retries.md` §2).
- **UI:** served at root on host port 8525; the admin panel links to it with a button (no iframe, no base-path/proxy configuration — confirmed decision).
- **v2 YAML is snake_case only** (`timeout_sec`, not `timeoutSec` — camelCase keys are rejected with a hint). The step-level `container:` field is the current preferred syntax; `action: docker.run` and the legacy `type: docker` shapes also parse but are not used here.
- **Timeout ownership:** the app watchdog owns the real kill (`04-orchestrator.md` §5) via Dagu's **stop endpoint** (verified: SIGTERM → SIGKILL after `max_clean_up_time_sec` → container force-removed → run status `aborted`). Dagu gets a belt-and-suspenders DAG-level `timeout_sec` set to *app timeout + 30 min* so it can never fire first — satisfying the mission-doc requirement that Dagu never times out a run prematurely.
- **Trigger (verified):** `POST /api/v1/dags/dev-run/start` with body
  `{"params": "{\"RUN_ID\": \"ENG-142-3-EXECUTE-9GX2TQ\", \"IMAGE\": \"devcake/dev-claude-code:latest\", \"TRACEPARENT\": \"<w3c>\", …}", "dagRunId": "ENG-142-3-EXECUTE-9GX2TQ"}` —
  `params` is a JSON-**encoded string** of named params; the client-chosen `dagRunId` (`^[-a-zA-Z0-9_]+$`, ≤ 64 chars — our human-readable run id fits) makes duplicate triggers return **HTTP 409** `already_exists` (`04-orchestrator.md` §6.3). Auth: `Authorization: Bearer dagu_<api-key>`.
- **Stop (watchdog kill, verified):** `POST /api/v1/dag-runs/dev-run/{dagRunId}/stop`, empty body, returns 200 immediately (async).
- **Params are visible unmasked in the Dagu UI and run API** (verified) — therefore params carry only non-secret values (`RUN_ID`, `IMAGE`, `TRACEPARENT`) **plus one deliberate exception**: the per-run scoped Redis ACL credential (`REDIS_USER`/`REDIS_PASSWORD`), acceptable because the Dagu UI/API is itself authenticated and the credential is revoked at finalization (`09-messaging.md` §1a). All real secrets reach the Dev via the Redis `runspec.get` channel (`14-security.md` §3). Keep params small (they travel as one CLI arg; practical ceiling ~128 KiB).
- **The single `dev-run` DAG** (`dagu/dags/dev-run.yaml`) — all business logic stays in the app; the DAG is a dumb container launcher. This exact shape (container field, param interpolation into `name`/`env`, network attach, auto-removal, blocking on the entrypoint) was validated and executed on v2.10.5:

```yaml
# dev-run: launch one Dev container. Only non-secret params; everything else via runspec.get.
timeout_sec: 9000               # 150 min belt-and-suspenders; the app watchdog kills at 120
max_clean_up_time_sec: 30       # grace between SIGTERM and SIGKILL on stop

params:
  - name: RUN_ID
    default: ""
  - name: IMAGE
    default: ""
  - name: TRACEPARENT
    default: ""
  - name: REDIS_USER          # per-run ACL user dev-{run_id} (09 §1a); scoped + revoked at
    default: ""
  - name: REDIS_PASSWORD      #   finalization — visible only to Dagu-authenticated operators
    default: ""

steps:
  - id: run-dev
    container:
      image: ${params.IMAGE}
      name: dev-${params.RUN_ID}
      network: devcake_runtime       # Redis + otel-collector + Gitea + outbound; NOT OO
      pull_policy: missing
      keep_container: false          # verified: force-removed on every exit path (incl. stop);
                                     #   post-mortem lives in Dagu step logs + OpenObserve
      startup: entrypoint            # run the image ENTRYPOINT; step blocks until exit (verified)
      env:
        DEVCAKE_RUN_ID: ${params.RUN_ID}
        TRACEPARENT: ${params.TRACEPARENT}
        REDIS_URL: redis://redis:6379/0
        REDIS_USER: ${params.REDIS_USER}
        REDIS_PASSWORD: ${params.REDIS_PASSWORD}
```

> Notes from the live verification: no `volumes:` are needed (credentials arrive via `runspec.get`, not bind mounts — and bind-mount sources would resolve on the *daemon host*, not inside the Dagu container). The stock `ghcr.io/dagucloud/dagu` image ships **no docker CLI**, so a shell `docker run` fallback is not viable — irrelevant, since the native executor (Moby SDK over the mounted socket) is fully verified. The image's `DOCKER_GID` entrypoint mechanism is broken on the 2.10.5 ubuntu image — hence `user: "0:0"` in §2. In the `container:` env map, host-process env does **not** resolve implicitly; anything beyond params would need DAG-level `secrets:`/`env:` — we deliberately need neither.

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

Names: `dev-{run_id}` via the DAG's `name:` key, with the human-readable run id
format of `02-domain-model.md` §7 — container names, traces, and Redis streams
match.

### 5a. Operator deploy rules (security)

1. Keep published ports on **127.0.0.1** (default compose). Use SSH tunnels for remote access.
2. Do not run on a shared multi-tenant Docker host.
3. Treat `/data` volume backups as **secret dumps**.
4. Before first real EXECUTE: branch protection + team membership + checklist in `14` §9.
5. Prefer mounting `./dagu/dags` **read-only** into Dagu when compose allows.

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

Harness images stay at the tag Dagu dispatches (`HARNESSES[…].image` in `app/devcake/harness.py` — still `…:latest` in v0 unless you retag). Production digests are pinned in run specs at dispatch time.

Which Dev image a run uses is `HARNESSES[harness_template].image` (`08-harness-templates.md` §2); `docker_image` is no longer stored config. Since any harness is selectable from the admin panel at any time, **all three `devcake/dev-*` images must be baked locally**.

## 7. Log shipping for non-instrumented services

`dagu` and `redis` stdout is shipped to OpenObserve via a lightweight shipper (vector/fluent-bit sidecar or compose logging driver) into stream `container_logs`. App and Devs use OTLP directly (`12-observability.md` §1).

## 8. Runbook

- **First run:** `cp .env.example .env` → strong bootstrap passwords + `DOCKER_GID` → `docker buildx bake all` → `docker compose up -d` → open `http://localhost:8080` → Config page: enter PMO/forge/model **secret VALUES**, connection tests, Dev Types → done. Labels bootstrap on startup. Then `14` §9 checklist before first EXECUTE.
- **Upgrading from a pre-Bake install (app ran as root):** the baked app image runs as non-root uid 1000, so `/data` files written by the old root-running app (config.yaml, run records, secrets) crash-loop boot with `PermissionError`. One-time fix before `up`:
  `docker run --rm -v devcake_devcake_data:/data alpine chown -R 1000:1000 /data`

### 8a. Protect the default branch (operator supply-chain control — docs/14 §2 zone C)

Dev containers hold the forge token, and token scoping cannot separate "push a
feature branch" from "merge to the default branch" (both are often
`contents: write`). **You** must protect **the branch named
`default_branch` on the `repos:` entry** before production-ish use. The app
**warns** when unprotected; it does not hard-block dispatch (`14` §8).

- **GitHub:** ruleset or classic protection — *require a pull request before merging* + *require ≥1 approval*; do not grant the Dev token's account a bypass. With a reviewer token configured, DevCake's REVIEW can file a formal approval so `auto_merge` still works if you enable it.
- **GitLab:** protect that branch (no direct pushes) and require ≥1 MR approval.

Forge connection test and `/health` surface protection state; amber warning when unprotected.
- **Upgrade:** `docker compose pull` (third-party images only) → `docker buildx bake all` → `docker compose up -d`. State survives (volumes). There is **no auto-migration**: pre-v2 state (a v1 `config.yaml`, v1 run records) is refused or quarantined with instructions (`10-persistence.md` §§2, 3, 5) — the v1→v2 migrators were removed at v0 crystallization.
- **Upgrade — app and Dev images deploy in LOCKSTEP ("just rebuild it all"):** every deploy that touches `images/*` (and, to be safe, every upgrade) must run `docker buildx bake all`. There are **no cross-version compat shims** (founder decision): a new app with old images — or the reverse — fails loudly (missing descriptor vars crash the clone bootstrap; protocol shape changes reject old senders' output). The dev-run DAG uses `pull_policy: missing`, so stale locally-tagged `devcake/dev-*:latest` images keep running silently unless rebaked.
- **Kill a stuck Dev:** admin → Runs page → open Dagu and stop the run (or `POST /api/v1/dag-runs/dev-run/<run_id>/stop`). The watchdog would do it at timeout regardless; the Mission reschedules per INV-3.
- **Logs:** admin → Logs page (OpenObserve). One run = one trace ID (`12-observability.md` §2).
- **Data reset:** `docker compose down && docker volume rm devcake_devcake_data` — consequences per `10-persistence.md` §5 (Mission state is safe in the PMO).
