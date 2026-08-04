# 01 — Architecture

> **Audience:** implementers and reviewers.
> **Depends on:** `00-overview.md`. **Security contract:** `14-security.md`.
> Details delegated to: `04` (orchestrator), `07` (Dev runtime), `09` (messaging), `13` (deployment).

## 1. Component inventory

| Service | Image | Role |
|---|---|---|
| `app` | built from `app/` (Python 3.12, FastAPI + asyncio) | PMO Handler (poll loop), scheduler, ingress consumer/finalizer, watchdog, `/api/v1`, all PMO+forge writes |
| `dagu` | `ghcr.io/dagucloud/dagu:<pinned>` | Executor: runs the single parameterized `dev-run` DAG (two dependent steps per run — provision then harness, ADR-0025); spawns Dev containers as siblings via host `docker.sock` (**root-equivalent** — dedicated host only, `14` §5) |
| `redis` | `redis:7-alpine` | Streams transport between Devs and app (`09-messaging.md`); AOF persistence; on **control + runtime** |
| `openobserve` | `public.ecr.aws/zinclabs/openobserve:<pinned>` | Logs, traces, metrics, cost dashboards — **control network only** (not reachable from Devs) |
| `otel-collector` | contrib collector (pinned) | Dev-side OTLP receiver (unauthenticated on runtime); forwards to OO with ingest credentials (`12-observability.md`) |
| `fluentbit` | fluent-bit (pinned) | Ships container stdout to OO |
| `gitea` | gitea (pinned, rootless) | Internal fallback forge for zero-repo missions; **control + runtime** (ADR-0010) |
| `admin` | nginx + static SPA (React/Vite/Tailwind) | Admin panel UI; reverse-proxies `/api`→app; links out to Dagu/OO/Gitea UIs (buttons, no iframes); loopback `:8080` |
| `prov-{run_id}` *(ephemeral)* | harness images from Bake | Provision step (ADR-0025): mounts the mirrors RO + the run's workspace, clones everything, exits; **runtime network only** |
| `dev-{run_id}` *(ephemeral)* | harness images from Bake | Harness step: one Mission Step then exit (`07-dev-runtime.md`), mounting ONLY its own workspace — never the mirrors; **runtime network only** |

Two container levels: the compose stack, and Dev containers Dagu spawns via `docker.sock`. Each run is two sequential Dev containers (provision → harness, ADR-0025). Dev siblings attach only to `devcake_runtime`; app/admin/Dagu/OpenObserve live on `devcake_control` (`13-deployment.md` §5).

## 2. Interaction matrix

| From → To | Protocol | Purpose |
|---|---|---|
| app → PMO (Linear adapter) | HTTPS GraphQL | poll missions; write feed posts/labels/status/attachments (sole PMO client — INV-4) |
| app → dagu | REST (`/api/v1/dags/dev-run/start`) | trigger a Dev run (non-secret params + per-run Redis ACL) |
| dagu → docker.sock | Docker API (Moby SDK) | spawn/stop/remove `dev-{run_id}` sibling containers |
| app → dagu | REST | watchdog kill (`stop`), run-status, reconciliation — app holds **no** `docker.sock` |
| dev → redis | Redis Streams | `runspec.get` (env + credentials), heartbeats/artifacts, `activity.get` |
| app → redis | Redis Streams | consume ingress (group `app`); serve replies |
| dev → forge (GitHub/GitLab/Gitea) | HTTPS/git | clone, push, open/update PR (Dev side of `06-forge-adapter.md`) |
| app → forge | HTTPS | PR comments, approval, merge (decision-bearing side) |
| admin(browser) → admin(nginx) → app | REST `/api/v1` | config CRUD, health, run history (basic auth) |
| app → openobserve | OTLP HTTP | control-plane traces/logs |
| dev → otel-collector | OTLP HTTP **unauthenticated** | Dev traces; collector alone holds OO ingest creds (`14` §10) |
| fluentbit → openobserve | HTTP | container stdout shipping |

## 3. Hexagonal layering of the app

```
app/devcake/
  domain/          # pure logic — depends on ports, not adapters
    model.py       #   entities, Mission Type derivation, label set (02)
    run.py         #   Run record + state machine
    run_bootstrap.py#  dispatch spine: workspace gate → ACL → digest → durable
                   #   save → workspace dir → executor.start (04 §3.1, ADR-0025)
    orchestrator/  #   package: MissionManager (DI + verbs) + module functions (04, ADR-0015)
                   #     manager, schedule, dispatch, finalize, transitions,
                   #     review, decomposition, sweeps, feed, markers, mapper
    mapper_service.py  # Relations Mapper cadence (ADR-0007; ISSUES #36 first cut)
    runs.py        #   ingress, kill, hello dispatch; holds RunBootstrap + RunFinalizer
    oauth.py       #   harness OAuth flows (launches via RunBootstrap)
    watchdog.py    #   timeout/zombie detection + workspace sweep cadence
    reconcile.py   #   boot reconciliation: adopt/orphan runs vs the Dagu API (04 §6)
    workspaces.py  #   WorkspaceStore — per-run host-bind tree, fail-closed
                   #   volume gate, sweep (ADR-0025)
    repo_mirror.py #   RepoCache — mandatory source mirrors, freshness gate,
                   #   needed_for (ADR-0024)
    forge_runtime.py   # ForgeRuntime — live adapter set, per-repo breakers,
                   #   bounded health sweeps (M10)
    repo_routing.py    # mission → work-repo resolution (M10 markers)
    blocker_locator.py #  deployment-wide blocked_by resolution (ADR-0009)
    backend_health.py  #  model-backend brake predicates (ADR-0018/0026)
    skills.py      #   skill store reads + prompt assembly (ADR-0016)
    costing.py     #   app-side cost estimation vs the rate card (ADR-0021)
    asset_fetch.py #   PMO attachment/zip fetching for activity repos (ADR-0014/0017)
    ids.py         #   id generation
  ports/           # Protocols + the DTOs that cross them
    pmo.py         #   PMOPort, PMOHealth, PMOCapabilities, PMOTransient (05)
    forge.py       #   ForgePort, PullRequest, BranchProtection,
                   #   ForgeDescriptor, ForgeCapabilities, ForgeError,
                   #   mission_branch(instance, key) (06)
    internal_forge.py  # InternalForgePort — bundled Gitea provisioner (ADR-0010)
    executor.py    #   ExecutorPort — start/stop/status (dagu adapter)
    state.py       #   StatePort — run-record persistence (files adapter)
    messaging.py   #   MessagingPort — Redis Streams surface (redis adapter)
    finalizer.py   #   RunFinalizer — mission finalize/restore (MissionManager)
  adapters/
    registry.py    #   PMO_SYSTEMS, make_pmo(), make_forge(), make_internal_forge(),
                   #   forges() — the ONE place that knows which adapters exist
    linear/        #   PMOPort impl (05)
    gitea_issues/  #   PMOPort impl — forge-issue family (05 §9; not ForgePort)
    github/ gitlab/ gitea/  # ForgePort impls + Gitea provisioner (06, ADR-0010)
    dagu/          #   ExecutorPort impl
    files/         #   StatePort impl (+ runlog.py, owner_store.py) (10)
    redis/         #   MessagingPort impl — ingress + replies (09)
  api/             # FastAPI (11, ADR-0015): main.py = composition root +
                   #   ≤4-statement route forwards; behavior in service
                   #   modules — poll.py (PollRuntime), health.py,
                   #   mission_actions.py, clear.py, config_service.py,
                   #   profiles_service.py, settings_transfer.py,
                   #   devtypes_service.py, connections_service.py,
                   #   internal_repos_service.py, runs_service.py,
                   #   auth.py (basic-auth + intent-header middleware —
                   #   attached in main.py; load-bearing, tested via
                   #   tests/test_api_surface.py)
  telemetry/       # OTel setup, devcake.* attribute helpers (12)
  prompts/         # playbook prompt templates (03 §7)
  config.py        # single pydantic schema authority for config.yaml (root-level:
                   #   cross-cutting — consumed by domain, adapters, and api alike)
  security.py      # redaction choke point, fed token shapes by the registry (14)
  harness.py       # harness registry: CLI/image/credential runtime adapters (08)
```

The app boots via `uvicorn devcake.api.main:app`. `config.py`, `security.py`, and `harness.py` sit at the package root because they are cross-cutting concerns, not layer members.

**Ports today:** vendor seams (`PMOPort`, `ForgePort` — ADR-0008; `InternalForgePort` — ADR-0010) and run-infrastructure seams (`ExecutorPort`, `StatePort`, `MessagingPort`, `RunFinalizer`). Production adapters: Linear, GitHub/GitLab/Gitea, Dagu, files, Redis Streams; `MissionManager` satisfies `RunFinalizer`. Composition root (`api/main.py`) builds adapters (incl. optional `make_internal_forge()` when Gitea admin creds are set), injects them into `RunManager` / per-instance `MissionManager`s, then `manager.set_finalizer(…)` so ingress/kill never type against the concrete orchestrator. Skill-store + per-mission repo routing (`resolve_repo` / `resolve_repo_live`) ride the same composition. All four dispatch flavors (hello, mission, mapper, OAuth) call `RunBootstrap.launch` for the durable-intent-before-trigger spine (`04-orchestrator.md` §3.1).

**Rule:** the domain core is testable with fakes of the ports; adapters never leak vendor types upward (normalized DTOs only, `02-domain-model.md`). Domain modules depend on **port Protocols**, not adapter packages.

## 4. Data-flow summaries

- **Poll → schedule → dispatch:** `04-orchestrator.md` §§1–3.
- **Dev run lifecycle:** `07-dev-runtime.md` §5.
- **Callback → finalize:** `09-messaging.md` → `04-orchestrator.md` §4.

## 5. Trust boundaries and failure domains

Normative product contract: **`14-security.md` §0–2** (three trust zones). Summary:

| Zone | Boundary | Stance |
|---|---|---|
| **A — Host / control** | `docker.sock` on Dagu only | Design: dedicated host; app never holds sock |
| **A** | Admin basic auth + `/data` secrets | Design: single operator; loopback default; volume backup = secret dump |
| **B — Agent** | Mission + repo content → prompts | **Trusted by design** (prompt injection is not a product defect) |
| **B** | Dev credentials + open egress | Required for work; redaction does not cover Dev sockets |
| **B** | Dev → control plane | No Docker route to app/admin/Dagu; runtime isolation is intentional |
| **C — Supply chain** | Default branch, team membership, auto_merge, RO PAT, reviewer token | **Primary mitigation; mostly operator-owned** (warnings, not hard gates) |
| **Hard product gates** | `LEGAL_OUTCOMES`, INV-4, out-of-pipeline merge detection | Enforced regardless of “adult” ethos |

**Credentials delivery:** real secrets are **not** Dagu/`docker run` env (except the per-run Redis ACL). The app rebuilds secret material on authenticated `runspec.get` (`09-messaging.md`, `14` §4). Never in images, Run JSON, or DAG YAML.

**What survives what:** an app restart loses nothing durable (state = PMO + files + Redis streams); a Dev crash loses only that attempt if labels never advanced (INV-3); host compromise is total for secrets and sock; a full host loss recovers mission state from the PMO System + a carefully handled `/data` backup (treat as secret material).
