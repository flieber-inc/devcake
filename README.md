# DevCake 🍰

**You never operate it. You write a ticket; finished, reviewed work comes back — with a receipt.**

DevCake staffs your existing task board with AI developers. You write tickets in
Linear (plain language); DevCake triages, plans, implements in disposable
containers, and runs an independent AI review before you see a PR. Every step
posts a transcript and a token bill. **Done means merged** — never before —
unless you turn auto-merge on.

There is no chat UI to babysit. Labels are the control plane; a human edit to a
ticket always beats an in-flight agent.

> Full product voice and pitch variants: [`docs/17-positioning.md`](docs/17-positioning.md).  
> *Name is provisional* — naming discussion lives in that doc §6.

## Who this is for

- Teams that already run work on a **PMO board** and want agents that speak labels + PRs
- Operators willing to **self-host** (Docker, your model subscriptions, your forge)
- Engineers who want **receipts** (transcripts, costs, OpenTelemetry traces) more than a chat copilot

## Status (v0)

| | |
|---|---|
| **Scope** | **v0 technical preview** (crystallization 2026-07-13; golden-path evidence 2026-07-11; operational posture continues to harden toward v0.1); Linear + one GitHub **or** GitLab repo per instance |
| **Release gate** | Acceptance path: fresh tickets → merged PRs, zero human input (GitHub by default; GitLab via `--forge gitlab`) — record in [`docs/16-roadmap.md`](docs/16-roadmap.md) |
| **Automated checks** | GitHub Actions CI: Bake `ci` group → pytest (incl. live Redis) → admin/hello smoke — [`.github/workflows/ci.yml`](.github/workflows/ci.yml) |
| **Invariants** | Six core invariants in [`docs/00-overview.md`](docs/00-overview.md); 230+-test suite (unit + live-Redis + stub dispatch) in `app/tests/` |

Claims without a linked doc or command are out of scope for this README.

## What v0 is not

Explicit non-goals (see [`docs/00-overview.md`](docs/00-overview.md) §2):

- **Not** multi-team / multi-repo *runtime* (config schema is plural; exactly one entry enforced)
- **Not** webhook ingestion (poll loop; webhook-ready internals later)
- **Not** PMO systems other than Linear in-tree (port + registry are pluggable)
- **Not** SSO/OIDC on the admin panel (HTTP basic auth from `.env`)
- **Not** a hosted SaaS — you run the box

## Prerequisites

- Docker Engine + Compose + **Buildx** (Bake is mandatory for images)
- A **Linear** team + API key; forge token for **GitHub or GitLab**
- Model credentials for the harnesses you assign (Claude / Grok / Codex — OAuth preferred for some)
- Host `DOCKER_GID` for Dagu’s access to `docker.sock` (see `.env.example`)

First full setup is **not** a five-minute toy: bake builds control plane + harness images, then compose starts the stack.
>>>>>>> 4cda5d2 (docs(readme): professional structure with limits-first narrative)

## Quickstart

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env
# REQUIRED: strong ADMIN/REDIS/DAGU/OO passwords (empty or change-me* values
# refuse boot unless DEVCAKE_ALLOW_INSECURE=1; upgrading? rotate old placeholder
# passwords first), LINEAR_API_KEY, team key, forge token + DEVCAKE_REPO_URL,
# DOCKER_GID (stat -c %g /var/run/docker.sock), model credentials
# (subscription OAuth preferred).
# Optional: OO_INGEST_* (non-root OTLP), GITHUB_TOKEN_RO for non-EXECUTE stages.

docker buildx bake all       # single source of truth for DevCake images
docker compose up -d         # run only — compose never builds devcake/*;
                             # control ports bind to 127.0.0.1 (docs/14)
open http://localhost:8080   # admin (basic auth) → Config → test connections
```

Rebuild after changes under `app/`, `admin/`, or `images/` — and after every
upgrade (stale local `:latest` tags keep running otherwise; Dagu launches Dev
images with `pull_policy: missing`):

```bash
docker buildx bake all       # or: bake (app+admin) · bake images (harnesses)
docker compose up -d
```

Then:

1. **[Tutorial 1 — first mission end to end](docs/tutorials/01-first-mission.md)** (~30 min to a merged PR)
2. **[Tutorial 2 — daily operations](docs/tutorials/02-operating-devcake.md)** (labels, interventions, reading the bill)

### A day with DevCake (operator view)

1. Write a ticket; add the `DEVCAKE` label (opt-in mode).
2. Poll adopts it → ONBOARD / PLAN / EXECUTE / REVIEW labels advance on the board.
3. A PR opens on `devcake/{mission_key}`; independent REVIEW posts findings.
4. Approve path ends in merge (or `DEVCAKE-MERGE` hand-off); ticket **Done** only after merge.
5. Not happy? Label swap reworks the same branch. Stop everything with `DEVCAKE-SKIP`.

## Architecture (one screen)

```
Linear (source of truth)
    │ poll + label writes
    ▼
FastAPI app (orchestrator) ── ports: PMO · Forge · Executor · State · Messaging
    │ dispatch (RunBootstrap: ACL → durable Run → Dagu)
    ▼
Dagu ──docker.sock──► ephemeral Dev containers (Claude / Grok / Codex / hello)
    │ results
    ▼
Redis Streams (per-run ACL) ──► finalize (compare-and-transition; human wins)
    │
    ▼
OpenObserve (traces, logs, cost attributes)
```

- **No locks.** Crashed agents hold nothing; next poll reschedules.
- **PMO is authoritative.** Local run files are advisory (`docs/10-persistence.md`).
- **Hexagonal app tree:** `domain/` · `ports/` · `adapters/` — see [`docs/01-architecture.md`](docs/01-architecture.md).

## Verify

```bash
# PR-style checks (needs Docker; Bake builds app-test + runs pytest against Redis)
# Prefer the GitHub Action, or locally after bake ci / app-test:
docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl ci
# …then pytest via app-test as in .github/workflows/ci.yml

# Full local suite (compose stack up; includes hello dispatch smoke):
./scripts/ci_suite.sh
```

Coding agents: **[`AGENTS.md`](AGENTS.md)** — Bake-only images, Always Works™ proof bar, **TDD / SOLID / Python 3.12** for new work.

## Why it’s different (engineering claims)

| Claim | Where to verify |
|---|---|
| Board is the UI (labels = state machine) | [`docs/02`](docs/02-domain-model.md) · [`docs/03`](docs/03-mission-lifecycle.md) |
| Independent review before default-branch trust (default config — recommended, not enforced; the assignments API warns when EXECUTE and REVIEW share a Dev Type) | [`docs/03`](docs/03-mission-lifecycle.md) REVIEW · `LEGAL_OUTCOMES` trust boundary · [`docs/14`](docs/14-security.md) §2 |
| Receipts (transcript + tokens + traces) | [`docs/12`](docs/12-observability.md) · INV-5 in [`docs/00`](docs/00-overview.md) |
| Your models, your machine | [`docs/08`](docs/08-harness-templates.md) · [`docs/13`](docs/13-deployment.md) |
| Human edit beats in-flight agent | compare-and-transition — [`docs/04`](docs/04-orchestrator.md) §4 |

## Documentation map

| | |
|---|---|
| Start here | [`docs/00-overview`](docs/00-overview.md) · tutorials under [`docs/tutorials/`](docs/tutorials/) |
| Domain & control plane | [`02`](docs/02-domain-model.md) · [`03`](docs/03-mission-lifecycle.md) · [`04`](docs/04-orchestrator.md) |
| Integrations | [`05` PMO](docs/05-pmo-adapter.md) · [`06` forge](docs/06-forge-adapter.md) · [`08` harnesses](docs/08-harness-templates.md) |
| Runtime | [`07`](docs/07-dev-runtime.md) · [`09` messaging](docs/09-messaging.md) · [`13` deploy](docs/13-deployment.md) |
| Trust & ops | [`11` admin](docs/11-admin-panel.md) · [`12` OTel](docs/12-observability.md) · [`14` security](docs/14-security.md) · [`15` errors](docs/15-errors-and-retries.md) |
| Record | [`16` roadmap](docs/16-roadmap.md) · [`docs/adr/`](docs/adr/) · [`01` architecture](docs/01-architecture.md) · [`10` persistence](docs/10-persistence.md) |
| Positioning | [`17`](docs/17-positioning.md) |

## License / contributing

Self-hosted operator software. For agents and contributors: follow **`AGENTS.md`** (Bake, TDD, SOLID, proof-before-done). Prefer PRs with tests at public seams (`app/tests/`) and zero-drift docs when changing architecture.
