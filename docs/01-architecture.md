# 01 — Architecture

> **Audience:** implementers and reviewers.
> **Depends on:** `00-overview.md`. Details delegated to: `04` (orchestrator), `07` (Dev runtime), `09` (messaging), `13` (deployment).

## 1. Component inventory

| Service | Image | Role |
|---|---|---|
| `app` | built from `app/` (Python 3.12, FastAPI + asyncio) | PMO Handler (poll loop), scheduler, ingress consumer/finalizer, watchdog, `/api/v1`, all PMO+forge writes |
| `dagu` | `ghcr.io/dagucloud/dagu:<pinned>` | Executor: runs the single parameterized `dev-run` DAG; spawns Dev containers as siblings via host `docker.sock` |
| `redis` | `redis:7-alpine` | Streams transport between Devs and app (`09-messaging.md`); AOF persistence |
| `openobserve` | `openobserve/openobserve:<pinned>` | All logs, traces, spans, metrics; cost dashboards |
| `admin` | nginx + static SPA (React/Vite/Tailwind) | Admin panel UI; reverse-proxies `/api`→app; links out to the Dagu and OpenObserve UIs (buttons, no iframes) |
| `dev-{run_id}` *(ephemeral)* | one of 3 harness images | One Mission Step, then exit (`07-dev-runtime.md`) |

Two container levels, per the mission doc: the compose stack, and the Dev containers Dagu spawns via `docker.sock` — siblings on the same Docker network with host-equivalent network access (`13-deployment.md` §5).

## 2. Interaction matrix

| From → To | Protocol | Purpose |
|---|---|---|
| app → Linear | HTTPS GraphQL | poll missions; write comments/labels/status/attachments (sole PMO client — INV-4) |
| app → dagu | REST (`/api/v1/dags/dev-run/start`) | trigger a Dev run with a fully-resolved run spec |
| dagu → docker.sock | Docker API (Moby SDK) | spawn/stop/remove `dev-{run_id}` sibling containers |
| app → dagu | REST | watchdog kill (`stop` endpoint), run-status queries, startup reconciliation — the app holds no `docker.sock` |
| dev → redis | Redis Streams | `runspec.get` (env + credentials), `run.started/heartbeat/artifacts`, `activity.get` req/reply |
| app → redis | Redis Streams | consume ingress (group `app`); serve replies |
| dev → GitHub/GitLab | HTTPS/git | clone, push, open/update PR (Dev side of `06-forge-adapter.md` §2) |
| app → GitHub/GitLab | HTTPS | PR comments, approval, merge (decision-bearing side) |
| admin(browser) → admin(nginx) → app | REST `/api/v1` | config CRUD, health, run history |
| everything → openobserve | OTLP HTTP (+ container stdout shipping) | traces, logs, metrics (`12-observability.md`) |

## 3. Hexagonal layering of the app

```
app/
  domain/          # pure logic: Mission derivation, state machine, scheduler,
                   #   finalization protocol. Imports NO adapter code. No I/O.
  ports/           # Protocols: PMOPort, ForgePort, ExecutorPort, StatePort
  adapters/
    linear/        # PMOPort impl (05)
    github/ gitlab/# ForgePort impls (06)
    dagu/          # ExecutorPort impl: start_dag, run_status
    files/         # StatePort impl: /data reads/writes (10)
    redis/         # ingress consumer + reply publisher (09)
  api/             # FastAPI: /api/v1 (11), health
  telemetry/       # OTel setup, devcake.* attribute helpers (12)
  prompts/         # playbook prompt templates (03 §7)
  harness_templates/  # the 3 template files (08)
```

**Rule:** the domain core is testable with fakes of the four ports; adapters never leak vendor types upward (normalized DTOs only, `02-domain-model.md`).

## 4. Data-flow summaries

- **Poll → schedule → dispatch:** `04-orchestrator.md` §§1–3.
- **Dev run lifecycle:** `07-dev-runtime.md` §5.
- **Callback → finalize:** `09-messaging.md` → `04-orchestrator.md` §4.

## 5. Trust boundaries and failure domains

| Boundary | Stance |
|---|---|
| Mission content from Linear → Dev prompts | Untrusted input executed by an agent with repo write access — prompt-injection risk accepted in v0, mitigated by single-team scoping, PR-only writes, and `auto_merge` defaulting off (`14-security.md` §2) |
| `docker.sock` | Only `dagu` holds it (the app kills/reconciles via the Dagu REST API); never Dev containers |
| Credentials | Injected at `docker run`, never in images, Dagu params, or logs (`14-security.md` §3) |

**What survives what:** an app restart loses nothing (state = PMO + files + Redis streams); a Dev crash loses only that attempt (labels never advanced — INV-3); a full host loss recovers from the PMO System + `/data` backup.
