# 00 — Overview: Vision, Glossary, and Core Invariants

> **Audience:** everyone. Every other document in `docs/` assumes you have read this one.
> **Status:** technical preview.
> **Security:** the product security contract lives in [`14-security.md`](14-security.md) — this file must not claim a stronger posture.

## 1. What DevCake is

DevCake is a **CLI agent orchestrator** for automating ticket resolutions, designed for a
**single operator on a dedicated host**. It runs automated coding agents
("**Devs**") inside Docker containers that systematically resolve work items
("**Missions**") pulled from a project-management system ("**PMO System**"). Devs are scheduled through [Dagu](https://docs.dagu.sh), talk back to the main app through Redis Streams and a Gitea internal server, and telemetry lands in OpenObserve.

**Deployment premise:** one machine you control; host `docker.sock` on Dagu;
admin + secrets ≅ host trust. Not multi-tenant SaaS. Ticket writers and repo
writers are inside the agent trust boundary (`14-security.md` §0).

The whole system ships as Bake images + `docker compose up`, stores local state
as plain files on one volume, and treats the PMO System as the single source of
truth — so it recovers from any crash by re-reading the world.

## 2. Non-goals for v0

The following are explicitly **out of scope** (see also `14-security.md` and
`16-roadmap.md`):

**Product / runtime**

- Webhook-based ingestion (v0 polls; the internal interface is webhook-ready).
- N work repos *per mission* (each mission resolves to **0 or 1** work repo;
  reference repos may still ride along read-only — `10-persistence.md`).
  **0..N** PMO instances and repos *per stack* are in scope (multi-connection).
- Human-in-the-loop approval steps *inside* DevCake (approval happens in the PMO
  System and the forge).
- PMO systems beyond the in-tree set (Linear + Gitea Issues; adapters are
  pluggable). Markdown-fidelity markers are a port requirement
  (`ports/pmo.py`) — multi-PMO is not adapter-only for ADF/rich-text systems.
- Hosted multi-tenant SaaS.

**Security / tenancy (design choices — not temporary shame)**

- Multi-operator RBAC or OIDC-as-default (HTTP basic auth is the control-plane
  auth story for dedicated host / loopback — `11-admin-panel.md`, `14` §4).
- Hard enforcement of branch protection, RO forge PATs, or independent REVIEW
  Dev Types — **recommended + warned**, operator-enforced (`14` §8).
- Treating prompt injection as a product defect — ticket + repo content are the
  interface; agents are powerful by design (`14` §3).
- Sandboxed multi-tenant Dev isolation or least-privilege multi-customer
  hosting (`14` §6).

## 3. Glossary (normative definitions)

| Term | Definition |
|---|---|
| **Mission** | A unit of work of any shape or size: a Linear **Project or Issue** in the configured team whose status is not Done/Completed/Canceled. DevCake makes no structural distinction between "projects" and "issues" — both are Missions. |
| **Mission Type** | One of exactly four values — `ONBOARD`, `PLAN`, `EXECUTE`, `REVIEW` — **derived** from the Mission's live PMO status + labels (derivation table in `02-domain-model.md`). Never stored authoritatively anywhere local. |
| **Mission Step** | One Dev run against one Mission (e.g. "the 2nd EXECUTE pass of ENG-142"). Identified by a sequence number `seq` used in transcript names (`1_ONBOARD.md`, `2_PLAN.md`, …). |
| **Dev** | An ephemeral Docker container running a Dev Type's selected model through its harness CLI to perform one Mission Step, then exit. |
| **Dev Type** | A named configuration: harness template + model selection + identifying prompt + MCP servers + credentials + concurrency cap + optional domain skills. v0 seeds three role vehicles: **judgment** (Claude Fable / Claude Code — ONBOARD/PLAN/REVIEW), **implementer** (Grok 4.5 / Grok Build — EXECUTE), and **mapper** (Claude Haiku / Claude Code — Relations Mapper default). The model belongs to the Dev Type; multiple Dev Types may share one harness. |
| **Harness Template** | One of three registry-backed runtime adapters: `claude-code`, `grok-build`, or `codex`. A template selects the CLI image, credential/OAuth contract, invocation and artifact parsing, and skills directory — not a fixed model. `DevType.model` selects the model; empty uses the registry or CLI default. Specified in `08-harness-templates.md`. |
| **PMO System** | The external project-management system holding the Missions. Adapters are pluggable (registered in `adapters/registry.py`); in-tree: **Linear** and **Gitea Issues**. A stack may configure **0..N** PMO instances; **each instance is scoped to exactly one team** (Linear team key, or Gitea `owner/repo` board). Accessed only through the `PMOPort` adapter (`05-pmo-adapter.md`). |
| **Forge** | The code-hosting platform holding the configured repository: GitHub, GitLab, or Gitea (including the bundled internal Gitea for zero-repo missions). Accessed only through the `ForgePort` adapter (`06-forge-adapter.md`). |
| **Run** | The locally persisted record of one Mission Step attempt: telemetry, timing, outcome, token report. Advisory data only — never authoritative (see INV-1). |
| **Activity feed** | The Mission's chronological record inside the PMO System: description, comments, attachments, status changes. Rendered into `ACTIVITY.md` for each Dev run. |
| **Stage label** | One of `DEVCAKE-PLAN`, `DEVCAKE-EXECUTE`, `DEVCAKE-REVIEW` — the label that drives the Mission state machine. A Mission carries at most one at a time (INV-2). |

The complete set of ten managed labels is defined in `02-domain-model.md` §5 and nowhere else.

## 4. Core invariants

These are **behavioral** contracts (not the full security model — that is
`14-security.md`). They are referenced by ID (`INV-n`) throughout the docs and
covered by automated tests (`16-roadmap.md`, M7).

- **INV-1 — The PMO System is the single source of truth.** All Mission status, labels, and priority are read live from the PMO System. No local data is ever deemed current; local state (`/data/state`) is advisory telemetry that can be wiped without corrupting the system (consequences of a wipe are documented in `10-persistence.md`).
- **INV-2 — At most one stage label per Mission.** A Mission carrying two or more stage labels is in conflict: DevCake refuses to schedule it (`LABEL_CONFLICT` — unschedulable gate reason only; no auto-comment; `15-errors-and-retries.md`).
- **INV-3 — No new work before the PMO reflects the previous step.** There are no persistent per-Mission leases or checkouts. A Mission Step may only be dispatched when the PMO System's live state shows the previous step's transition fully applied. A crashed Dev holds nothing; its Mission simply re-derives and reschedules. Process-local locks may serialize dispatch and maintenance but carry no Mission authority across a crash.
- **INV-4 — Devs never talk to the PMO System directly.** The main app is the sole PMO client. Devs communicate only via Redis Streams (`09-messaging.md`); all PMO writes are performed by the app during run finalization.
- **INV-5 — Every Dev run posts a transcript AND a token/cost report to the activity feed.** No exceptions. When token usage cannot be extracted, an explicit "unavailable" report is posted instead of silence (`08-harness-templates.md` §Token extraction).
- **INV-6 — Devs work only inside their fresh clone and commit only at the very end.** Every Dev container receives a fresh `git clone`; all work is atomistic; git commits and pushes happen only during run finalization inside the container, never incrementally.

## 5. System at a glance

```
                     ┌──────────────────── docker-compose (dedicated host) ────────────────────┐
                     │  devcake_control                              devcake_runtime            │
  ┌────────┐ GraphQL │  ┌──────────┐ REST  ┌──────┐ docker.sock  ┌ ─ ─ ─ ─ ─ ─ ─ ┐           │
  │ Linear │◄────────┤  │ app      │──────►│ dagu │─────────────►│ dev-<run_id>  │           │
  │ (PMO)  │  poll   │  │ FastAPI  │       └──────┘  siblings    │ harness       │           │
  └────────┘         │  │          │◄── Redis Streams ───────────┤               │           │
  ┌────────┐ HTTPS   │  └────┬─────┘   (per-run ACL)             └───────┬───────┘           │
  │ Forge  │◄────────┤       │ /api/v1 (via admin)                       │ clone/push        │
  │ GH/GL  │◄────────┤  ┌────┴─────┐  loopback :8080                     ▼                   │
  └────────┘  Devs   │  │ admin    │  ┌─────────────┐              forge / packages          │
                     │  │ SPA+nginx│  │ openobserve │◄── app OTLP                            │
                     │  └──────────┘  └──────▲──────┘                                        │
                     │                       │ otel-collector bridges runtime→control        │
                     │                  Devs ──OTLP unauth──► collector (no OO creds in Dev)   │
                     └───────────────────────────────────────────────────────────────────────┘
```

Two container levels: the compose stack, and ephemeral **Dev containers** that
Dagu spawns as *siblings* via the host `docker.sock`. Devs join **`devcake_runtime`**
only (Redis, otel-collector, optional internal Gitea) — not the app/admin/Dagu
control plane. OpenObserve is **control-only**; Devs never hold OO credentials.
Control ports bind **loopback** by default. See `13-deployment.md`, `14-security.md`.

## 6. Walkthrough: one Mission's life

A concrete end-to-end pass, naming the governing document at each hop:

1. **A human creates an Issue** in the configured Linear team and labels it `DEVCAKE` (adoption is opt-in by default; an `opt_out` mode adopts everything in the team — `02-domain-model.md` §2). It sits in `Backlog`. Anyone who can write that ticket is inside the agent trust boundary (`14` §0).
2. **The PMO Handler polls Linear** (default every 30 s) and normalizes the Issue into a Mission (`05-pmo-adapter.md`). Status `backlog` + no stage label ⇒ derived Mission Type = **ONBOARD** (`02-domain-model.md`).
3. **The scheduler picks it** by priority (Urgent > High > Medium > Low; unset = Medium), checks the concurrency caps for the mapped Dev Type (`judgment`), and dispatches: writes a Run record, triggers Dagu's `dev-run` DAG, and marks the Mission `In Progress` in Linear (`04-orchestrator.md`).
4. **Dagu spawns a Dev container.** The entrypoint prepares `/workspace/repo` (fresh clone), `/workspace/activity/ACTIVITY.md` (+ attachments), registers MCP servers, and launches Claude Code with the judgment identifying prompt + the ONBOARD playbook prompt (`07-dev-runtime.md`, `08-harness-templates.md`, `03-mission-lifecycle.md`).
5. **The Dev assesses complexity** and, say, deems it *normal*. It writes `/workspace/out/result.json` with `outcome: "plan_needed"` and exits 0. The entrypoint publishes the transcript, token report, and result over Redis (`09-messaging.md`).
6. **The app finalizes**: posts `1_ONBOARD.md` and the token report to the Linear activity feed, then — after re-reading the Mission live (compare-and-transition, `04-orchestrator.md` §4) — adds the `DEVCAKE-PLAN` label.
7. **Next poll cycle**: status `started` + `DEVCAKE-PLAN` ⇒ Mission Type **PLAN** ⇒ judgment produces `PLAN.md`; the app uploads it, swaps the label to `DEVCAKE-EXECUTE`.
8. **EXECUTE**: implementer (Grok Build) implements the plan on branch `devcake/LINEAR-ENG-142` (instance-prefixed — `mission_branch(instance, key)`), opens a PR (`06-forge-adapter.md`), and the app swaps the label to `DEVCAKE-REVIEW`.
9. **REVIEW:** by **recommended** config, a *different* Dev Type than EXECUTE reviews the PR (warned if shared — not a hard gate; `14` §8). The REVIEW **Dev** only returns judgment in `result.json`. On approval, the **app** posts the PR comment and, if a **reviewer token** is configured (app-only, never injected into a Dev), files a formal forge approval; then **merge precedes Done**: with `auto_merge` **off** (default) the Mission carries `DEVCAKE-MERGE` until a merge is observed (normally a human — the app does not merge; Dev write tokens are stopped by **forge branch protection**, not by this toggle — `14` §2 zone C); with it on the app squash-merges using the **write** token (not the reviewer token) and only then marks **Done**. On rejection, the report is posted and the label swaps back to `DEVCAKE-EXECUTE`.
10. **Throughout**, every hop is one connected OpenTelemetry trace (dispatch → container → finalization), visible in OpenObserve (`12-observability.md`).

## 6a. Onboarding path (30 minutes for a new engineer)

Read in this order — each doc assumes the ones before it:

1. This overview, §1–§6 — what it is, the invariants, one Mission's life.
2. [`02-domain-model.md`](02-domain-model.md) — the entities and the label
   state machine everything else speaks in.
3. [`03-mission-lifecycle.md`](03-mission-lifecycle.md) — what each Mission
   Type actually does.
4. **[`14-security.md`](14-security.md)** — the trust contract; nothing you
   build or operate may outclaim it.
5. [`04-orchestrator.md`](04-orchestrator.md) — scheduling, dispatch,
   finalization, crash recovery.
6. [`13-deployment.md`](13-deployment.md) — the stack you'll actually touch,
   plus the runbook.
7. [`tutorials/01-first-mission.md`](tutorials/01-first-mission.md) — drive one
   mission end to end.

Operating duties — once at setup and recurring — live in
[`18-operator-contract.md`](18-operator-contract.md).

## 7. Document map

| Doc | Governs |
|---|---|
| **`14-security.md`** | **Product security contract** — threat model, trust zones, risk-bucket reading aid, operator checklist |
| `01-architecture.md` | Component topology, interaction matrix, ports & adapters layering |
| `02-domain-model.md` | Entities, fields, Mission Type derivation, label set, state machine |
| `03-mission-lifecycle.md` | The four Mission Type playbooks, `result.json`, canonical prompts |
| `04-orchestrator.md` | Poll loop, scheduling, no-lock atomicity, crash recovery |
| `05-pmo-adapter.md` | `PMOPort` + Linear + Gitea Issues adapters |
| `06-forge-adapter.md` | `ForgePort` + GitHub/GitLab/Gitea adapters, PR/branch conventions |
| `07-dev-runtime.md` | Dev container contract: filesystem, env, exit codes, lifecycle |
| `08-harness-templates.md` | Harness invocation, plan mode, token extraction, MCP setup, local backends |
| `09-messaging.md` | Redis Streams protocol |
| `10-persistence.md` | `/data` layout, file formats, atomic writes |
| `11-admin-panel.md` | Admin UI + `/api/v1` contract |
| `12-observability.md` | OTel conventions, cost telemetry, dashboards |
| `13-deployment.md` | docker-compose, Dagu config, networking, runbook |
| `15-errors-and-retries.md` | Error taxonomy, retry matrix, `DEVCAKE-FAILED` |
| `16-roadmap.md` | Milestone era + closed releases + living log after v0.2 |
| `17-positioning.md` | Outward voice — must not outclaim `14` |
| `18-operator-contract.md` | What the operator owns — setup pointer + recurring duties + rotation |
| `adr/` | Records of significant architectural decisions |
| `tutorials/` | Operator path (includes supply-chain checklist) |
