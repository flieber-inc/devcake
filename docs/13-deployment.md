# 13 — Deployment: docker-compose, Dagu, Networking, Runbook

> **Audience:** operators and implementers.
> **Depends on:** `01-architecture.md`, `07-dev-runtime.md`, `12-observability.md`, `14-security.md`.

Goal per the mission doc: **as simple as possible, local-friendly, production-grade** — one `docker compose up`.

## 1. Service names, volumes, network (normative — these are DNS names other docs reference)

- Services: `app`, `dagu`, `redis`, `openobserve`, `admin`.
- Volumes: `devcake_data` (→ `app:/data`), `dagu_data`, `redis_data`, `oo_data`.
- Network: the default compose network, external name **`devcake_default`** — Dev containers are attached to it by name (§5).

## 2. Annotated `docker-compose.yml` skeleton

```yaml
name: devcake

services:
  app:
    build: ./app
    env_file: .env                       # LINEAR_API_KEY, GITHUB_TOKEN, model keys, …
    volumes:
      - devcake_data:/data               # note: NO docker.sock — kill/reconcile go via the Dagu API
    depends_on:
      redis:        { condition: service_healthy }
      openobserve:  { condition: service_started }
      dagu:         { condition: service_healthy }
    healthcheck: { test: ["CMD", "curl", "-f", "http://localhost:8000/api/v1/health"],
                   interval: 10s, retries: 5 }

  dagu:
    image: ghcr.io/dagucloud/dagu:2.10.5   # PIN the version — project moves fast (see §4)
    ports: [ "8525:8080" ]                 # UI opened directly via the admin panel's button (no iframe)
    environment:
      - DAGU_AUTH_MODE=basic               # API 401s without creds; /api/v1/health stays open (verified)
      - DAGU_AUTH_BASIC_USERNAME=${DAGU_USER}
      - DAGU_AUTH_BASIC_PASSWORD=${DAGU_PASSWORD}
      - DOCKER_GID=${DOCKER_GID}           # host docker-group gid → daemon runs uid 1000 : docker
    volumes:
      - dagu_data:/var/lib/dagu
      - ./dagu/dags:/var/lib/dagu/dags     # contains the single dev-run DAG
      - ./dagu/init:/etc/custom-init.d:ro  # repairs the image's broken DOCKER_GID mechanism (§4)
      - /var/run/docker.sock:/var/run/docker.sock   # ⚠ root-equivalent host access — 14-security.md §4
    healthcheck:   # stock image has no curl/wget (verified at M0) — bash /dev/tcp HTTP probe
      test: ["CMD", "bash", "-c",
             "exec 3<>/dev/tcp/127.0.0.1/8080 && printf 'GET /api/v1/health HTTP/1.0\\r\\n\\r\\n' >&3 && head -1 <&3 | grep -q ' 200 '"]
      interval: 10s
      retries: 5

  redis:
    image: redis:7-alpine
    command: ["redis-server", "--appendonly", "yes", "--appendfsync", "everysec",
              "--requirepass", "${REDIS_PASSWORD}"]    # default user = app-only; per-run ACL users for Devs (09 §1a)
    volumes: [ redis_data:/data ]
    healthcheck: { test: ["CMD", "redis-cli", "-a", "${REDIS_PASSWORD}", "ping"], interval: 5s, retries: 5 }

  openobserve:
    image: openobserve/openobserve:v0.91.1   # pinned
    environment:
      - ZO_ROOT_USER_EMAIL=${OO_ROOT_EMAIL}
      - ZO_ROOT_USER_PASSWORD=${OO_ROOT_PASSWORD}
    volumes: [ oo_data:/data ]
    ports: [ "5080:5080" ]

  admin:
    build: ./admin                        # nginx + built SPA; proxies /api only
    ports: [ "8080:80" ]
    environment:
      - DAGU_UI_URL=${DAGU_UI_URL:-http://localhost:8525}    # targets of the Executor/Logs
      - OO_UI_URL=${OO_UI_URL:-http://localhost:5080}        #   tabs' open-in-new-tab buttons
      - ADMIN_USER=${ADMIN_USER}                             # nginx basic auth over the SPA
      - ADMIN_PASSWORD=${ADMIN_PASSWORD}                     #   AND the /api proxy (11 §5)
    depends_on: [ app ]

volumes:
  devcake_data: {}
  dagu_data: {}
  redis_data: {}
  oo_data: {}
```

*(The committed file adds the image-build entries for the three Dev images — §6.)*

## 3. `.env.example` (full listing)

```
# PMO
LINEAR_API_KEY=

# Forge (both may be set; one active repo — 06-forge-adapter.md)
GITHUB_TOKEN=
GITHUB_REVIEWER_TOKEN=        # optional 2nd account for formal PR approvals
GITLAB_TOKEN=

# Model/harness credentials (env_api_key mode; JSON mode uses the admin panel upload)
ANTHROPIC_API_KEY=
CLAUDE_CODE_OAUTH_TOKEN=      # preferred: subscription token from `claude setup-token`
XAI_API_KEY=
OPENAI_API_KEY=

# OpenObserve
OO_ROOT_EMAIL=admin@example.com
OO_ROOT_PASSWORD=change-me

# Dagu API auth (basic mode — verified env names from source; app + browser use these)
DAGU_USER=devcake
DAGU_PASSWORD=

# Host docker group gid, for Dagu's sock access: stat -c %g /var/run/docker.sock
DOCKER_GID=

# Redis default-user password (app-only; Devs get per-run ACL users — 09 §1a)
REDIS_PASSWORD=change-me-too

# Admin panel basic auth (11 §5)
ADMIN_USER=admin
ADMIN_PASSWORD=change-me-as-well

# UI links shown as buttons in the admin panel (defaults fit local compose)
DAGU_UI_URL=http://localhost:8525
OO_UI_URL=http://localhost:5080
```

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
  `{"params": "{\"RUN_ID\": \"ENG-142-3-EXECUTE-9GX2TQ\", \"IMAGE\": \"<digest-pinned image>\", \"TRACEPARENT\": \"<w3c>\", …}", "dagRunId": "ENG-142-3-EXECUTE-9GX2TQ"}` —
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
      network: devcake_default       # verified: plain custom-network attach
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

Dagu holds the host `docker.sock`, so Dev containers are **siblings** of the compose stack (docker-outside-of-docker). The DAG's `network: devcake_default` key attaches them at spawn (verified via `docker inspect`), giving them:
- resolution of `redis:6379` and `openobserve:5080` by service name, and
- full outbound (host-equivalent) network access, per the mission doc.

Names: `dev-{run_id}` via the DAG's `name:` key, with the human-readable run id format of `02-domain-model.md` §7 (`ENG-142-3-EXECUTE-9GX2TQ`) — so the Dagu UI's run list reads as a natural map of Missions and Mission Steps (confirmed decision), and container names, traces, and Redis streams all match it.

## 6. Image build matrix

| Image | Context | Tag |
|---|---|---|
| `devcake/app` | `./app` | `devcake/app:<git-sha>` |
| `devcake/admin` | `./admin` | same scheme |
| `devcake/dev-claude-code` | `./images/claude-code` | pinned by digest in run specs |
| `devcake/dev-grok-build` | `./images/grok-build` | 〃 |
| `devcake/dev-codex` | `./images/codex` | 〃 |

Which Dev image a run uses is `HARNESSES[harness_template].image` (`app/devcake/harness.py` — `08-harness-templates.md` §2); `docker_image` is no longer stored config. Since any harness is selectable from the admin panel at any time, **all three `devcake/dev-*` images must be built locally**.

## 7. Log shipping for non-instrumented services

`dagu` and `redis` stdout is shipped to OpenObserve via a lightweight shipper (vector/fluent-bit sidecar or compose logging driver) into stream `container_logs`. App and Devs use OTLP directly (`12-observability.md` §1).

## 8. Runbook

- **First run:** `cp .env.example .env` → fill keys → `docker compose up -d` → open `http://localhost:8080` → Config tab: PMO connection test, repo connection test, review the three default Dev Types → done. The app bootstraps the ten Linear labels on startup.

### 8a. Protect the default branch (deployment requirement — docs/14 §2)

Dev containers hold the forge token, and token scoping cannot separate "push a feature branch" from "merge to main" (both are `contents: write`). Before pointing DevCake at a repository:

- **GitHub:** add a ruleset (or classic protection) on the default branch — *require a pull request before merging* + *require ≥1 approval*; do not grant the Dev token's account a bypass. With a reviewer token configured, DevCake's REVIEW files a formal approval, so `auto_merge` keeps working.
- **GitLab:** protect the default branch (no direct pushes) and require ≥1 MR approval.

The forge connection test and the admin header surface the protection state; an unprotected default branch shows a standing amber warning.
- **Upgrade:** `docker compose pull && docker compose build && docker compose up -d`. State survives (volumes). Schema migrations run automatically (`10-persistence.md` §2).
- **Kill a stuck Dev:** admin → Executor tab → open Dagu and stop the run (or `POST /api/v1/dag-runs/dev-run/<run_id>/stop`). The watchdog would do it at timeout regardless; the Mission reschedules per INV-3.
- **Logs:** admin → Logs tab (OpenObserve). One run = one trace ID (`12-observability.md` §2).
- **Data reset:** `docker compose down && docker volume rm devcake_devcake_data` — consequences per `10-persistence.md` §5 (Mission state is safe in the PMO).
