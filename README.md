# DevCake 🍰

**Your board is the interface. Tickets in, pull requests out.**

<img width="350" height="350" alt="devcake" src="https://github.com/user-attachments/assets/52a7ffec-26b3-47bc-9973-9c9e7ce25ed5" />

DevCake staffs the task board you already use with AI developers. You write a
ticket in plain language; disposable containers triage, plan, implement, and
pass every mission through REVIEW; work comes back as a pull request with a
**transcript and a token bill** on the ticket. Labels are the control plane. A
human edit always beats an in-flight agent. **Done means merged** — with
auto-merge off, DevCake waits for you; with it on, the app merges an approved
pull request before marking the mission Done.

It runs on **your** machine, with **your** model subscriptions or API
credentials and **your** forge. There is no hosted SaaS and no second
day-to-day work queue: the board drives missions, while the bundled admin UI
handles configuration and operations.

> Anyone who can write tickets on the configured team (or land content in a
> configured repo) can influence agents that hold forge and model credentials.
> You own branch protection (what stops Devs from merging), team membership,
> and whether the **app** may auto-merge after REVIEW. Full contract:
> [`docs/14-security.md`](docs/14-security.md).

Product voice and pitch variants: [`docs/17-positioning.md`](docs/17-positioning.md).

---

## Why DevCake?

The deepest AI-assisted work today happens in CLI harnesses — Claude Code,
Grok Build, Codex (launch-supported; Pi, OpenCode, and Qwen Code are
**experimental**) — with an expert invisibly orchestrating each session:
curating context, sizing the task, sequencing the work, verifying the output.
DevCake mechanizes that orchestration for board-shaped work. It is a
**meta-harness** — a CLI agent orchestrator that staffs those harnesses rather
than competing with them: not a new coding agent, but a session made
repeatable, without the expert chained to the keyboard.

The method is context hygiene, engineered: one clear goal per session, tasks
decomposed until they fit, fresh containers, curated read-only mounts of
exactly the relevant prior work, feedback at step boundaries — never
interruption inside one. We call it putting AI to work in a state of flow,
and we treat the conditions for it as a design target, not a hope. In one
phrase: **the clean room for delegated deep work** — as capability gets
cheap, the scarce thing is accountability, and the envelope supplies it,
domain-free ([`docs/17-positioning.md`](docs/17-positioning.md) §1c).

We believe this works; we have not yet proven it publicly. The mechanisms are
built and tested; the evidence so far is our own production use, self-reported
([`docs/16-roadmap.md`](docs/16-roadmap.md), living log). The full argument —
its evidence status stated claim by claim, and what would change our mind — is
[`docs/19-thesis.md`](docs/19-thesis.md).

---

## Who this is for

- Teams already running work on a **PMO board** (Linear and Gitea Issues in-tree)
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
| Zero or more **external repos** | Clone, branch, PR; zero uses the bundled Gitea; **RO** + **reviewer** tokens recommended (write always for work repos) |
| **Work** vs **reference** vs **memory** repos per PMO | Routing targets vs read-only consultation clones vs team-memory notebooks (curated notes, mounted read-only into every run — ADR-0035) |
| **Dev Types**, assignments, prompts | ONBOARD → PLAN → EXECUTE → REVIEW (plus optional steward) |
| **Skills** per Dev Type (skill store + skill sources) | Curated skills seeded into an editable Gitea repo, plus your own external **skill sources** — dedicated read-only connections serving `<source>/<skill>` — installed into agent sessions |
| **Scheduled Tasks** | Built-in maintenance on a timer — the Relations Steward (proposes ticket orderings) and the Memory Curator (reviews raw leads into notebook notes via PRs) — plus your own recurring ticket-creating tasks |
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

REVIEW is always a pipeline stage (judgment in `result.json`). The
**reviewer token** — app-only, different forge account — is what is
**recommended** when branch protection requires formal approval; it is never
injected into a Dev. Staffing a different Dev Type for REVIEW than EXECUTE is
optional and about role focus (skills, identifying prompt), not security. How
merges are actually controlled is next.

### How forge merges are controlled (first deploy)

Three different things are easy to conflate. Only the forge enforces the last:

| Knob | Who it constrains | What it does |
|---|---|---|
| **`auto_merge`** (per repo, default **off**) | The **app** only | Off on a repo → app never merges that repo's PRs; parks at `DEVCAKE-MERGE` until a real merge is observed. On → app squash-merges after REVIEW approve. (ADR-0020) |
| **Forge tokens** (Repositories page) | Devs + app | **Write** token: EXECUTE push + open PR; app also uses it to **merge** when auto-merge is on. **RO** token (recommended): non-EXECUTE stages clone without write. **Reviewer** token (**recommended** for formal PR/MR approval under branch protection; **app-only** — never injected into a Dev). |
| **Branch protection** (on the forge UI) | Everyone with a token | Server rules on the **default branch** (require a PR, require ≥1 approval, no bypass for the Dev account). Token scopes usually **cannot** separate “push a feature branch” from “merge to main” — protection is what does. |

**Happy path with protection + tokens configured:**

1. **EXECUTE** Dev (write token) pushes a feature branch and opens a PR.
2. **REVIEW** Dev judges the PR (`result.json`). It does **not** formally approve on the forge. With an RO token set, it does not even hold write credentials.
3. **App** (on approve): posts the PR comment; if a **reviewer** token is set, files a **formal forge approval** with that token (different identity from the PR author — needed when the forge blocks self-approval).
4. Then either:
   - **mission's repo `auto_merge` off:** park at `DEVCAKE-MERGE`; **you** merge on the forge; the app marks Done when it sees the merge.
   - **mission's repo `auto_merge` on:** the app **merges with the write token** (not the reviewer token), then Done.

Without branch protection, a Dev that holds the write token can often merge (or push) despite `auto_merge` being off — playbooks say not to; that is guidance, not enforcement. Full contract: [`docs/14-security.md`](docs/14-security.md) §2 zone C · setup steps: [`docs/13-deployment.md`](docs/13-deployment.md) §8a · token details: [`docs/06-forge-adapter.md`](docs/06-forge-adapter.md) §4–5.

---

## A day on the board

1. Write a ticket; add `DEVCAKE` (opt-in mode) — or adopt a whole team deliberately.
2. Labels advance: ONBOARD → PLAN → EXECUTE → REVIEW as work completes.
3. A PR opens on `devcake/…` (or the internal forge equivalent).
4. With auto-merge **off** (default): the **app** parks at `DEVCAKE-MERGE` until
   the PR is merged (normally by you); then Done. Branch protection is what
   stops Devs from merging on their own — see above.
5. Steer with comments and label swaps; stop everything with `DEVCAKE-SKIP`.
   (Comments steer the *next run*; granting a failing step **fresh attempts**
   takes the literal `DEVCAKE-RETRY` in a comment, or a label op — the strict
   default keeps bot comments from resetting the budget. ADR-0026.)

Details and interventions: [Tutorial 2](docs/tutorials/02-operating-devcake.md).

---

## Architecture (one screen)

```
PMO (Linear / Gitea Issues) ──poll / labels──► app (orchestrator)
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
- **No per-mission lease or checkout.** A crashed agent holds nothing; the next
  poll reschedules. Process-local locks serialize dispatch and maintenance.
- **PMO is source of truth.** Local run files are advisory
  ([`docs/10`](docs/10-persistence.md)).
- **Control plane** (app, admin, Dagu, OpenObserve) stays off the Dev network;
  optional **Gitea** straddles both for clone/push.

---

## Trust in one breath

Self-hosted, single operator, loopback by default. Stack passwords live in
`.env`; **operator secrets** (PMO keys, forge tokens, model credentials) are
entered through the admin UI's Configuration (incl. Skills → Skill sources), Repositories and PMO pages and stored
on the app volume — never echoed back. Be clear-eyed about what "GUI secret
store" means: values rest as **plaintext files (mode 0600) on the `/data`
volume** — there is no vault and no at-rest encryption (a key would have to
live on the same host); anyone with host root or a volume backup reads
everything. Treat `/data` backups like a password-manager export
([`docs/14-security.md`](docs/14-security.md) §4).

Agents are powerful by design. The app enforces outcome legality and never lets
Devs write the PMO directly; it **warns** on weak posture (write token on every
stage, unprotected default branch). It does not replace forge branch protection
or careful team membership.

→ [`docs/14-security.md`](docs/14-security.md) (contract) · checklist before first
real EXECUTE: §9.

---

## Quickstart

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env
# Edit .env: strong ADMIN / REDIS / DAGU / OO / GITEA passwords only.
# Leave DOCKER_GID blank. Empty/change-me* passwords refuse boot unless
# DEVCAKE_ALLOW_INSECURE=1 (local sandbox only). Operator secrets
# (PMO / forge / model) go in the admin Config UI after up — not in .env.

./up.sh --bake            # discovers DOCKER_GID, bakes all images, compose up -d
# Later restarts (images already baked):  ./up.sh

open http://localhost:8080   # basic auth → Config / the Adapters pages → secrets + connection tests
# Optional OO dashboard/alerts: python3 scripts/provision_oo.py
```

`./up.sh` is the supported start path: it reads the docker socket’s group id
into `.env`, optionally bakes, then runs compose. Control ports bind
`127.0.0.1`. Images are **Bake-only** — compose never builds them.

**Before the first real mission:**

1. Sandbox (or tightly controlled) Linear team — ticket writers = agent trust.
2. On the forge: **protect the default branch** (require PR + ≥1 approval; Dev
   write account must not bypass) — see [merge control](#how-forge-merges-are-controlled-first-deploy).
3. Repositories: write token; prefer **RO** for non-EXECUTE and a **reviewer**
   token (app-only formal approval — the security-relevant second identity).
4. Leave each repo's **`auto_merge` off** until you want the **app** to merge that repo after REVIEW.

1. [Tutorial 1 — first mission](docs/tutorials/01-first-mission.md)
2. [Tutorial 2 — daily operations](docs/tutorials/02-operating-devcake.md)
3. [Tutorial 3 — MCP plugins](docs/tutorials/03-mcp-plugins.md)
4. Fresh empty volume drill: [operator-drill](docs/tutorials/operator-drill.md)

After upgrades or changes under `app/`, `admin/`, or `images/`:

```bash
./up.sh --bake
```

More detail: [`AGENTS.md`](AGENTS.md) · [`docs/13-deployment.md`](docs/13-deployment.md).

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
| Why this exists — the thesis | [`docs/19-thesis.md`](docs/19-thesis.md) |

Full map and architecture: [`docs/00`](docs/00-overview.md) §7 · [`docs/01`](docs/01-architecture.md).

---

## Contributing

Self-hosted operator software. Prefer PRs with tests at public seams
(`app/tests/`) and zero-drift docs when public contracts change. Build with
**Bake**, target **Python 3.12**, and prove run/up paths — not only “build
succeeded.” See [`AGENTS.md`](AGENTS.md).
