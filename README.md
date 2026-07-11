# DevCake 🍰

**DevCake is a lightweight, production-grade agentic developer.** It runs automated coding agents ("**Devs**") in Docker containers that systematically resolve work items ("**Missions**") from your project-management system (v0: **Linear**) — assessing, planning, executing, and reviewing them, ending in pull requests on your repository (GitHub or GitLab). A **Mission** is any work request of any shape or size (a Linear Project or Issue alike); a **Dev Type** pairs a model with a harness (v0: **Senior Dev** = Claude Fable/Claude Code for onboarding, planning, and review; **Main Dev** = Grok 4.5/Grok Build for execution); each Mission flows through up to four **Mission Types** — `ONBOARD → PLAN → EXECUTE → REVIEW` — driven entirely by labels and statuses in the PMO System, which is always the single source of truth.

> **Status:** implementation in progress per [`docs/16-roadmap.md`](docs/16-roadmap.md) — **M0 complete** (compose skeleton + observability spine); next: M1 (hello-world Dev).

## Architecture at a glance

```
 Linear ◄──poll/write──► app (FastAPI) ──trigger──► Dagu ──docker.sock──► dev-<run_id>
                          ▲    │                                          (Claude Code /
 GitHub/GitLab ◄──PRs─────┤    └──/api/v1──► admin panel (nginx+SPA)       Grok Build /
                          │                                                Codex)
                          └────────◄── Redis Streams ◄────────────────────────┘
                                     all logs/traces/costs ──► OpenObserve
```

Five compose services + ephemeral Dev containers spawned as siblings by Dagu. Everything is OpenTelemetry-traced into OpenObserve — including per-step **token and cost reports**, which are also posted to each Mission's activity feed.

## Quickstart

```bash
cp .env.example .env         # fill: LINEAR_API_KEY, GITHUB_TOKEN (or GITLAB_TOKEN),
                             #       and your model credentials (subscription OAuth preferred)
docker compose up -d
open http://localhost:8080   # admin panel → Config tab → test connections, pick your team
```

From there, any non-done Issue or Project in your configured Linear team becomes a Mission. Progress, transcripts (`1_ONBOARD.md`, `2_PLAN.md`, …), and token reports appear in the Mission's activity feed; code lands as PRs on `devcake/<mission-key>` branches. DevCake never pushes to your default branch (merges happen by a human — or by DevCake itself if you enable the `auto_merge` toggle).

## Documentation

Start with **[`docs/00-overview.md`](docs/00-overview.md)** — glossary, core invariants, and a full walkthrough. Then:

| Doc | What it covers |
|---|---|
| [`01-architecture.md`](docs/01-architecture.md) | Topology, interaction matrix, ports & adapters |
| [`02-domain-model.md`](docs/02-domain-model.md) | Entities, the Mission Type derivation table, labels, state machine |
| [`03-mission-lifecycle.md`](docs/03-mission-lifecycle.md) | The four playbooks, `result.json`, canonical prompts |
| [`04-orchestrator.md`](docs/04-orchestrator.md) | Scheduling, no-lock atomicity, crash recovery |
| [`05-pmo-adapter.md`](docs/05-pmo-adapter.md) | `PMOPort` + the Linear adapter |
| [`06-forge-adapter.md`](docs/06-forge-adapter.md) | `ForgePort` + GitHub/GitLab, PR conventions, approvals |
| [`07-dev-runtime.md`](docs/07-dev-runtime.md) | The Dev container contract |
| [`08-harness-templates.md`](docs/08-harness-templates.md) | Claude Code / Grok Build / Codex specifics, token extraction |
| [`09-messaging.md`](docs/09-messaging.md) | Redis Streams protocol |
| [`10-persistence.md`](docs/10-persistence.md) | The `/data` volume |
| [`11-admin-panel.md`](docs/11-admin-panel.md) | Admin UI + REST API |
| [`12-observability.md`](docs/12-observability.md) | OTel conventions, cost telemetry |
| [`13-deployment.md`](docs/13-deployment.md) | docker-compose, Dagu, networking, runbook |
| [`14-security.md`](docs/14-security.md) | Threat model, credential handling |
| [`15-errors-and-retries.md`](docs/15-errors-and-retries.md) | Error taxonomy, retries, `DEVCAKE-FAILED` |
| [`16-roadmap.md`](docs/16-roadmap.md) | Milestones M0–M7 with exit criteria |
| [`docs/adr/`](docs/adr/) | Why the big decisions were made |
