# Agent notes — DevCake

Instructions for coding agents (Claude, Cursor, Grok, Codex, etc.) working in this repo.

## Docker images: Bake only

**DevCake images are built only with Docker Bake.** Compose never builds them.

| Command | What it builds |
|---|---|
| `docker buildx bake all` | Everything — **use this on first setup and full upgrades** |
| `docker buildx bake` | Control plane only: `app` + `admin` |
| `docker buildx bake images` | Dev harnesses + hello stub (shared `base` stage) |
| `docker compose up -d` | **Run** the stack (requires images already baked) |

### Do

- Build/rebuild with `docker buildx bake` / `bake all` / `bake images` (see `docker-bake.hcl`).
- After changing `app/`, `admin/`, or `images/`, rebake the affected targets (or `bake all`).
- Keep app/admin tags aligned with compose: `DEVCAKE_TAG` (default `latest`) in bake and compose.

### Do not

- Do **not** add `build:` back to `docker-compose.yml` for DevCake images.
- Do **not** use `docker compose build` or `docker compose --profile images build` — those paths are gone.
- Do **not** expect `docker compose up --build` to build `devcake/*` images (`pull_policy: never`).
- Do **not** invent per-harness Dockerfiles under `images/<harness>/` — use multi-target `images/Dockerfile` + a target in `docker-bake.hcl`.

### Layout

| Path | Role |
|---|---|
| `docker-bake.hcl` | **Single source of truth** for image builds and tags |
| `docker-compose.yml` | Runtime only (networks, volumes, third-party images) |
| `app/Dockerfile` | Multi-stage FastAPI app → tag `devcake/app:${TAG}` |
| `admin/Dockerfile` | Multi-stage SPA + nginx → tag `devcake/admin:${TAG}` |
| `images/Dockerfile` | Multi-target Dev harnesses (`base`, `hello`, `claude-code`, `codex`, `grok-build`) |
| `app/devcake/harness.py` | Runtime image names for Dagu dispatch (`devcake/dev-*:latest`) |

### Typical agent workflows

```bash
# After editing orchestrator / API
docker buildx bake app && docker compose up -d app

# After editing admin SPA
docker buildx bake admin && docker compose up -d admin

# After editing images/common/dev_entrypoint.py or a harness
docker buildx bake images

# Full lockstep (app + admin + all Dev images) — required when protocol/entrypoint changes
docker buildx bake all && docker compose up -d
```

Optional tag pin (bake and compose must match):

```bash
export DEVCAKE_TAG=$(git rev-parse --short HEAD)
docker buildx bake all
docker compose up -d
```

### Third-party images

Redis, Dagu, OpenObserve, Fluent Bit still come from registries via compose (`docker compose pull`). Only **DevCake-built** images go through bake.

### More detail

- Deployment runbook and matrix: `docs/13-deployment.md` §6
- Harness templates: `docs/08-harness-templates.md`
- Human quickstart: `README.md`
