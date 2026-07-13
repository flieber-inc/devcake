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

Two container levels, per the mission doc: the compose stack, and the Dev containers Dagu spawns via `docker.sock`. Dev siblings attach only to `devcake_runtime`; the app/admin/Dagu control plane lives on `devcake_control` (`13-deployment.md` §5).

## 2. Interaction matrix

| From → To | Protocol | Purpose |
|---|---|---|
| app → PMO (Linear adapter) | HTTPS GraphQL | poll missions; write feed posts/labels/status/attachments (sole PMO client — INV-4) |
| app → dagu | REST (`/api/v1/dags/dev-run/start`) | trigger a Dev run with a fully-resolved run spec |
| dagu → docker.sock | Docker API (Moby SDK) | spawn/stop/remove `dev-{run_id}` sibling containers |
| app → dagu | REST | watchdog kill (`stop` endpoint), run-status queries, startup reconciliation — the app holds no `docker.sock` |
| dev → redis | Redis Streams | `runspec.get` (env + credentials), `run.started/heartbeat/artifacts`, `activity.get` req/reply |
| app → redis | Redis Streams | consume ingress (group `app`); serve replies |
| dev → forge (GitHub/GitLab adapters) | HTTPS/git | clone, push, open/update PR (Dev side of `06-forge-adapter.md` §2) |
| app → forge (GitHub/GitLab adapters) | HTTPS | PR comments, approval, merge (decision-bearing side) |
| admin(browser) → admin(nginx) → app | REST `/api/v1` | config CRUD, health, run history |
| everything → openobserve | OTLP HTTP (+ container stdout shipping) | traces, logs, metrics (`12-observability.md`) |

## 3. Hexagonal layering of the app

```
app/devcake/
  domain/          # pure logic — imports NO adapter code at runtime
    model.py       #   entities, Mission Type derivation, label set (02)
    run.py         #   Run record + state machine
    orchestrator.py#   poll loop, scheduler, finalization protocol (04)
    runs.py        #   run bookkeeping
    oauth.py       #   harness OAuth flows
    watchdog.py    #   timeout/zombie detection
    ids.py         #   id generation
  ports/           # Protocols + the DTOs that cross them
    pmo.py         #   PMOPort, PMOHealth, PMOCapabilities, PMOTransient (05)
    forge.py       #   ForgePort, PullRequest, BranchProtection,
                   #   ForgeDescriptor, ForgeError, mission_branch() (06)
  adapters/
    registry.py    #   PMO_SYSTEMS, make_pmo(), make_forge(), forges() —
                   #   the ONE place that knows which adapters exist
    linear/        #   PMOPort impl (05)
    github/ gitlab/#   ForgePort impls (06)
    dagu/          #   Dagu executor: start_dag, run_status
    files/         #   /data reads/writes: run_store.py, runlog.py (10)
    redis/         #   messaging.py — ingress consumer + reply publisher (09)
  api/             # FastAPI: main.py (/api/v1, health — 11), clear.py
  telemetry/       # OTel setup, devcake.* attribute helpers (12)
  prompts/         # playbook prompt templates (03 §7)
  config.py        # single pydantic schema authority for config.yaml (root-level:
                   #   cross-cutting — consumed by domain, adapters, and api alike)
  security.py      # redaction choke point, fed token shapes by the registry (14)
  harness.py       # harness registry: the 3 model/harness pairs (08)
```

The app boots via `uvicorn devcake.api.main:app`. `config.py`, `security.py`, and `harness.py` sit at the package root because they are cross-cutting concerns, not layer members. `ExecutorPort` and `StatePort` are declared-future ports: the `dagu/`, `files/`, and `redis/` adapters are already packaged under `adapters/`, but their port Protocols are not yet formalized (`16-roadmap.md`).

**Rule:** the domain core is testable with fakes of the ports; adapters never leak vendor types upward (normalized DTOs only, `02-domain-model.md`). `domain/*` has zero runtime adapter imports — adapter types appear only under `TYPE_CHECKING`.

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
| Dev → control plane | No shared Docker network. FastAPI still requires Basic auth on every non-liveness route and an explicit intent header on mutations. |

**What survives what:** an app restart loses nothing (state = PMO + files + Redis streams); a Dev crash loses only that attempt (labels never advanced — INV-3); a full host loss recovers from the PMO System + `/data` backup.
