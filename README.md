# DevCake 🍰

**Your board is the interface. Tickets in, pull requests out.**

<img width="350" height="350" alt="devcake" src="https://github.com/user-attachments/assets/52a7ffec-26b3-47bc-9973-9c9e7ce25ed5" />

DevCake staffs the task board you already use with AI developers. You write a
ticket in plain language; disposable containers triage, plan, implement, and
(optionally) review; work comes back as a pull request with a **transcript and a
token bill** on the ticket. Labels are the control plane. A human edit always
beats an in-flight agent. **Done means merged** — never before — unless you turn
auto-merge on.

It runs on **your** machine, with **your** model subscriptions and **your**
forge. There is no hosted SaaS and no second product UI to live in.

> Anyone who can write tickets on the configured team (or land content in a
> configured repo) can influence agents that hold forge and model credentials.
> You own branch protection, team membership, and whether auto-merge is on.
> Full contract: [`docs/14-security.md`](docs/14-security.md).

Product voice and pitch variants: [`docs/17-positioning.md`](docs/17-positioning.md)
(*name is provisional* — §6).

---

## Who this is for

- Teams already running work on a **PMO board** (Linear in-tree today)
- A **single operator** who will **self-host on a dedicated machine** (Docker,
  Bake, your forge, your models) — not multi-tenant SaaS
- People who want **receipts** (transcripts, costs, traces) more than a chat copilot

**Not for:** anyone wanting a hosted, zero-ops service (self-hosting *is* the
trust model); boards without discipline (DevCake amplifies your board — it
cannot invent one); shared Docker hosts; exposing the admin UI to the open
internet on basic auth alone; or expecting injection-proof agents. Prompt
injection is in scope of “ticket writers are trusted,” not a product defect to
be papered over.

What you own as the operator — once at setup, and recurring — fits on one page:
[`docs/18-operator-contract.md`](docs/18-operator-contract.md).

---

## What you get

| You set up | The system does |
|---|---|
| One or more **PMO instances** (teams) | Polls, managed labels, feed posts, adoption modes |
| One or more **repos** | Clone, branch, PR; optional read-only and reviewer tokens |
| **Work** vs **reference** repos per PMO | Routing targets vs read-only consultation clones |
| **Dev Types**, assignments, prompts | ONBOARD → PLAN → EXECUTE → REVIEW (plus optional mapper) |
| **Skills** per Dev Type (skill store) | Curated Claude Code skills seeded into an editable Gitea repo, installed into agent sessions |
| Auto-merge, intake pause, limits | Operator knobs — defaults favor a human merge |

### Three ways to use it

1. **External forge** — Classic path: a Linear ticket becomes a PR on your
   GitHub or GitLab repo, with the full label pipeline and receipts on the issue.
2. **Internal forge** — No external repo: work runs on the **bundled Gitea**;
   missions still complete on the board, with deliverables you can take from the
   PMO feed. Useful for non-code or sandbox workloads without burning forge PATs.
3. **Multi-connection** — Several teams and/or repos on one stack. Missions
   route by instance config and optional markers; reference repos can ride along
   read-only for every stage.

Independent AI review (a different Dev Type for REVIEW than EXECUTE) is
**recommended configuration**, warned when shared — not a hard product
invariant. The supply-chain gate that actually holds is **yours**: protect the
default branch; keep auto-merge off until you mean it.

---

## A day on the board

1. Write a ticket; add `DEVCAKE` (opt-in mode) — or adopt a whole team deliberately.
2. Labels advance: ONBOARD → PLAN → EXECUTE → REVIEW as work completes.
3. A PR opens on `devcake/…` (or the internal forge equivalent).
4. With auto-merge **off** (default): `DEVCAKE-MERGE` until **you** merge; then Done.
5. Steer with comments and label swaps; stop everything with `DEVCAKE-SKIP`.

Details and interventions: [Tutorial 2](docs/tutorials/02-operating-devcake.md).

---

## Architecture (one screen)

```
PMO (Linear) ──poll / labels──► app (orchestrator)
                                    │ RunBootstrap → Dagu
                                    ▼
                         docker.sock ──► Dev containers
                         (runtime net, open egress)
                                    │
                         Redis Streams (per-run ACL)
                                    ▼
                              finalize → PMO + forge
                                    │
                         OpenObserve ← app OTLP
                         (Devs → otel-collector only)
```

- **Dedicated host.** Dagu holds `docker.sock` (root-equivalent). See
  [`docs/14`](docs/14-security.md) §5 and [`docs/13`](docs/13-deployment.md).
- **No locks.** A crashed agent holds nothing; the next poll reschedules.
- **PMO is source of truth.** Local run files are advisory
  ([`docs/10`](docs/10-persistence.md)).
- **Control plane** (app, admin, Dagu, OpenObserve) stays off the Dev network;
  optional **Gitea** straddles both for clone/push.

---

## Trust in one breath

Self-hosted, single operator, loopback by default. Stack passwords live in
`.env`; **operator secrets** (PMO keys, forge tokens, model credentials) are
entered in the admin Config UI and stored on the app volume — never echoed back.

Agents are powerful by design. The app enforces outcome legality and never lets
Devs write the PMO directly; it **warns** on weak posture (write token on every
stage, unprotected default branch, shared EXECUTE/REVIEW). It does not replace
forge branch protection or careful team membership.

→ [`docs/14-security.md`](docs/14-security.md) (contract) · checklist before first
real EXECUTE: §9.

---

## Quickstart

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env
# Bootstrap only: strong ADMIN / REDIS / DAGU / OO / GITEA passwords,
# DOCKER_GID (stat -c %g /var/run/docker.sock). Empty or change-me* values
# refuse boot unless DEVCAKE_ALLOW_INSECURE=1 (local sandbox only).
# PMO / forge / model secrets → admin Config after up (not long-lived in .env).

docker buildx bake all    # builds all DevCake images (compose never does)
docker compose up -d      # control ports bind 127.0.0.1
# If OpenObserve ingest is not yet provisioned:
#   python3 scripts/provision_oo.py

open http://localhost:8080   # basic auth → Config → secrets + connection tests
```

**Before the first real mission:** sandbox (or tightly controlled) Linear team;
branch protection on the default branch; leave auto-merge off; prefer a
read-only forge token for non-EXECUTE and a different Dev Type for REVIEW.

1. [Tutorial 1 — first mission](docs/tutorials/01-first-mission.md)
2. [Tutorial 2 — daily operations](docs/tutorials/02-operating-devcake.md)
3. [Tutorial 3 — MCP plugins](docs/tutorials/03-mcp-plugins.md)
4. Fresh empty volume drill: [operator-drill](docs/tutorials/operator-drill.md)

Rebuild after upgrades or changes under `app/`, `admin/`, or `images/`:

```bash
docker buildx bake all && docker compose up -d
```

Images are **Bake-only**. Compose only runs them. See [`AGENTS.md`](AGENTS.md)
and [`docs/13-deployment.md`](docs/13-deployment.md).

---

## Verify

```bash
# Unit suite only (always rebakes app-test so the image matches the tree):
./scripts/pytest_app.sh

# CI-shaped bake (Docker): see .github/workflows/ci.yml
docker buildx bake -f docker-bake.hcl -f docker-bake.ci.hcl ci

# Full local suite (stack up; forge battery + dispatch-hello smoke):
./scripts/ci_suite.sh
```

Token-spending golden path (optional, real models/forges):
`scripts/acceptance.py` — including internal-forge / Gitea lanes when configured.

Coding agents: follow [`AGENTS.md`](AGENTS.md). Security and autonomy claims
must not exceed [`docs/14-security.md`](docs/14-security.md).

---

## Documentation

| I want to… | Start here |
|---|---|
| Understand the product and invariants | [`docs/00-overview.md`](docs/00-overview.md) |
| Onboard as a new engineer (reading path) | [`docs/00-overview.md`](docs/00-overview.md) §6a |
| Understand the security deal | [`docs/14-security.md`](docs/14-security.md) |
| Know what you own as operator | [`docs/18-operator-contract.md`](docs/18-operator-contract.md) |
| Run a first mission / operate daily | [`docs/tutorials/`](docs/tutorials/) |
| Deploy, networks, runbook | [`docs/13-deployment.md`](docs/13-deployment.md) |
| Labels, lifecycle, orchestrator | [`docs/02`](docs/02-domain-model.md) · [`03`](docs/03-mission-lifecycle.md) · [`04`](docs/04-orchestrator.md) |
| PMO / forge / harnesses | [`05`](docs/05-pmo-adapter.md) · [`06`](docs/06-forge-adapter.md) · [`08`](docs/08-harness-templates.md) |
| Admin API & UI contract | [`docs/11-admin-panel.md`](docs/11-admin-panel.md) |
| History and backlog | [`docs/16-roadmap.md`](docs/16-roadmap.md) |
| How we talk about it | [`docs/17-positioning.md`](docs/17-positioning.md) |

Full map and architecture: [`docs/00`](docs/00-overview.md) §7 · [`docs/01`](docs/01-architecture.md).

---

## Contributing

Self-hosted operator software. Prefer PRs with tests at public seams
(`app/tests/`) and zero-drift docs when public contracts change. Build with
**Bake**, target **Python 3.12**, and prove run/up paths — not only “build
succeeded.” See [`AGENTS.md`](AGENTS.md).
