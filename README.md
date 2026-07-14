# DevCake 🍰

**You never operate it. You write a ticket; finished, reviewed work comes back — with a receipt.**

DevCake staffs your task board with AI developers. You keep working the way you
already do — writing tickets in Linear, in plain language — and DevCake picks
them up: it sizes each task, plans it, writes the code in a disposable
container, and then a *second* AI reviews the work like a skeptical senior
engineer before you ever see it. What lands back on your board is a finished
pull request, a full transcript of every step, and an itemized bill of what
each step cost. Nothing merges without your approval — unless you've decided to
trust it that far and switched auto-merge on.

There is no new app to live in, no chat window to babysit. The board is the
interface: one label adopts a ticket, one label pauses everything, and a human
edit always beats an in-flight agent. Autonomy, with receipts.

## A day with DevCake

1. You write a ticket: *"Add a `--quiet` flag to the CLI, with tests."* You add
   the `DEVCAKE` label and get on with your day.
2. Within a minute the ticket is **In Progress**. A triage transcript appears in
   its feed, then a token report — model, tokens, cost for that step.
3. Labels progress on their own: a plan is attached, an implementation lands on
   a branch, a pull request opens on your repo.
4. A review — written to reject unless convinced, tests actually run — is posted
   to the PR. On approval, the ticket waits on **`DEVCAKE-MERGE`** with the
   exact merge command ready to paste.
5. You read the diff, merge, and the ticket flips to **Done**. Done means
   *merged* — never before.

Not happy instead? Swap one label and the crew reworks the same branch with
your notes. Want it to stop? `DEVCAKE-SKIP` wins over everything, always.

## Why it's different

- **No new interface.** Your PMO system (Linear in v0) is the single source of
  truth; four labels are the whole state machine. Adopt with a label, stop with
  a label.
- **Reviewed by design.** By default, EXECUTE and REVIEW use different Dev Types
  (and models). That independence is the recommended configuration — the
  assignments API warns if you point both at the same type. Rejections loop
  back with findings; every Nth rejection posts a cost warning.
- **Receipts for everything.** Every step posts its transcript and token bill to
  the ticket; every action — dispatches, kills, sweeps, logins — is an
  OpenTelemetry trace you can pull up by run id.
- **Your models, your box.** One `docker-compose up`. Mix Claude Code, Grok
  Build, and Codex per role, on the subscriptions you already pay — connected
  through guided OAuth, stored only on your machine.

> **Status: v0 technical preview** (crystallization 2026-07-13). Contracts and
> golden-path evidence were verified on 2026-07-11; operational posture continues
> to harden toward v0.1. The release gate is an acceptance script that takes
> fresh tickets to merged PRs with zero human input (GitHub by default; GitLab
> via `--forge gitlab`). A 200+-test suite (unit + live-Redis + stub dispatch)
> pins the core invariants. See [`docs/16-roadmap.md`](docs/16-roadmap.md).

## Quickstart

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env         # REQUIRED: strong ADMIN/REDIS/DAGU/OO passwords
                             # (empty or change-me* values refuse boot unless
                             # DEVCAKE_ALLOW_INSECURE=1 — upgrade note: rotate
                             # old placeholder passwords before first start),
                             # LINEAR_API_KEY, DEVCAKE_TEAM_KEY, GITHUB_TOKEN
                             # (or GITLAB_*), DEVCAKE_REPO_URL, model creds
                             # (subscription OAuth preferred), DOCKER_GID
                             # (stat -c %g /var/run/docker.sock)
                             # Optional: OO_INGEST_* (non-root OTLP),
                             # GITHUB_TOKEN_RO / token_ro_env for non-EXECUTE stages
docker buildx bake all       # single source of truth for images (app, admin, 3 harnesses + hello)
docker compose up -d         # run only — control ports bind to 127.0.0.1
# After upgrades: re-run `docker buildx bake all` — stale local :latest tags
# keep running otherwise (Dagu launches Dev images with pull_policy: missing).
open http://localhost:8080   # admin panel (basic auth from .env) → Config page →
                             # test connections; connect Grok/Codex via the OAuth wizard
```

Rebuild after any change to `app/`, `admin/`, or `images/`:

```bash
docker buildx bake all       # or: bake (app+admin) · bake images (harnesses only)
docker compose up -d
```

Then follow **[Tutorial 1 — your first mission, end to end](docs/tutorials/01-first-mission.md)**
(~30 minutes from clean machine to merged PR) and
**[Tutorial 2 — operating DevCake day to day](docs/tutorials/02-operating-devcake.md)**
(the label language, interventions, and reading the bill).

## How it works, in one breath

A FastAPI orchestrator polls your Linear team and derives each ticket's next
step purely from its live labels — no local state is ever authoritative. Steps
run as disposable Docker containers spawned through [Dagu](https://docs.dagu.sh),
each wrapping a real coding harness with a fresh clone and the mission's full
history; results travel back over per-run-authenticated Redis Streams, and the
orchestrator applies all board updates itself, with compare-and-transition
semantics so human edits always win. Everything exports to OpenObserve, cost
included. There are no locks anywhere: a crashed agent holds nothing and simply
gets rescheduled.

## Documentation

**Agents / automation:** see **[`AGENTS.md`](AGENTS.md)** for Bake-only image builds
(`docker buildx bake all` — compose never builds DevCake images). CI on GitHub Actions:
`.github/workflows/ci.yml` (bake + pytest), `docker-images.yml` (harnesses), optional
`docker-publish.yml` (GHCR, manual).
Start with **[`docs/00-overview.md`](docs/00-overview.md)** — glossary, the six
core invariants, and a full walkthrough. Highlights:

| | |
|---|---|
| The idea & the words | [`00-overview`](docs/00-overview.md) · [`17-positioning`](docs/17-positioning.md) · [`tutorials/`](docs/tutorials/) |
| The state machine | [`02-domain-model`](docs/02-domain-model.md) · [`03-mission-lifecycle`](docs/03-mission-lifecycle.md) · [`04-orchestrator`](docs/04-orchestrator.md) |
| The integrations | [`05-pmo-adapter`](docs/05-pmo-adapter.md) · [`06-forge-adapter`](docs/06-forge-adapter.md) · [`08-harness-templates`](docs/08-harness-templates.md) |
| The runtime | [`07-dev-runtime`](docs/07-dev-runtime.md) · [`09-messaging`](docs/09-messaging.md) · [`13-deployment`](docs/13-deployment.md) |
| Trust & operations | [`12-observability`](docs/12-observability.md) · [`14-security`](docs/14-security.md) · [`15-errors-and-retries`](docs/15-errors-and-retries.md) · [`11-admin-panel`](docs/11-admin-panel.md) |
| The record | [`16-roadmap`](docs/16-roadmap.md) · [`docs/adr/`](docs/adr/) · [`10-persistence`](docs/10-persistence.md) · [`01-architecture`](docs/01-architecture.md) |

*The name "DevCake" is provisional — the naming discussion lives in
[`docs/17-positioning.md`](docs/17-positioning.md) §6.*
