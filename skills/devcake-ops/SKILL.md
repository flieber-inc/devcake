---
name: devcake-ops
description: Set up, inspect, configure, troubleshoot, and upgrade a self-hosted DevCake deployment for its operator. Use for questions about missions, repository context, memory, scheduled tasks, skills, or deployment health; developing DevCake's own code follows AGENTS.md instead.
---

# Operating DevCake with a coding agent

Help the operator turn goals into board-driven work and keep the deployment
healthy. Explain relevant choices, inspect actual state, make authorized
corrections, and verify the result. You are the operator's interactive
helper; the Devs running inside DevCake are separately staffed agents.
DevCake continues operating when your session ends.

This skill lives at `skills/devcake-ops/SKILL.md` in the DevCake checkout.
Use it with any capable coding agent; automatic skill discovery is optional.
The `.claude/skills/devcake-ops` compatibility link points here too.
Resolve the links below from this canonical location. If the skill was
installed separately, locate the operator's checkout and read its matching
docs before issuing commands. Run host commands from that checkout, after
checking its branch, local changes, and deployment target.

## Understand the product before configuring it

DevCake orchestrates CLI coding agents from a project-management board
(**PMO**): Linear, GitHub Issues, GitLab Issues, or Gitea Issues. A ticket is
a **Mission**; one agent invocation lifecycle at one pipeline stage is a
**Run**. A **Dev Type** supplies the harness, model, credentials, prompt,
skills, and concurrency limit; assignments select one for each stage.
Harnesses are Claude Code, Codex, Grok Build, Pi, OpenCode, and Qwen Code.

The normal pipeline is **ONBOARD → PLAN → EXECUTE → REVIEW**. ONBOARD
triages and routes; it can attach a plan or decompose large work into child
tickets. EXECUTE creates or updates a PR on the work repository. REVIEW
judges it; formal forge approval and app-driven merges happen in the app.
REVIEW is always a stage, even when ONBOARD supplied the plan. With the
repo's `auto_merge` off (default), approved PRs wait at `DEVCAKE-MERGE`;
PR-producing missions become Done only after merge. Tracking parents
complete when their children do. Documents and other Git deliverables can
use the same workflow, including the bundled Gitea.

**Fresh context is the execution model.** Every new run gets a fresh
workspace and session with the mission brief, feed, attachments, prior
plans/transcripts, relevant ancestor/blocker context, and selected repos
and skills. A continuation inside that run may resume the session. Do not
assume a later run remembers an earlier conversation; durable instructions
belong on the ticket or in the appropriate repository. The PMO owns mission
state; local run history is operational evidence. The app applies Dev
outcomes and writes to the PMO through the normal pipeline.

Read the [overview](../../docs/00-overview.md) and
[lifecycle](../../docs/03-mission-lifecycle.md) for unfamiliar work states.
Do not infer deployment readiness or feature maturity from the roadmap's
aspirational sections; check code and recorded evidence.

## Inspect first; preserve the operator's choices

- Start with read-only checks. Reuse choices and authorization already given;
  ask only for missing information that materially affects the work. Ordinary
  diagnosis does not authorize dispatching tickets or spending model tokens.
- Before a state change, establish its target and consequence. Work within
  the authorized deployment, boards, repositories, and budget. For an
  unapproved destructive operation, broader intake, merge policy change,
  privileged host change, or external exposure, prepare the concrete change
  before requesting approval. Do not ask again for an already authorized step.
- Keep evidence: mission key, run ID, relevant error, configuration diff,
  and observed result. Never print credentials or put them in argv, chat,
  commits, or diagnostic artifacts. Read only the needed `.env` fields
  internally; do not dump the file.
- Never use volume deletion, Clear run history, or a host wipe as a generic
  repair. Those require a separately scoped destructive task and the
  [deployment runbook](../../docs/13-deployment.md). Routine shutdown is
  `devcake down`, which preserves volumes.

The [security contract](../../docs/14-security.md) governs every recommendation.
This is a single-operator system for a dedicated host: Docker access is
root-equivalent and Devs have open egress. Trusted ticket/repository/skill
authors can influence agents with credentials. Branch protection enforces
forge permissions; `auto_merge` off only constrains the app. Prefer RO tokens
for non-EXECUTE stages. The **reviewer token** is an app-only, separate forge
identity for formal approval; assigning a different model to REVIEW is a
staffing choice. Do not weaken protection to make a stalled merge succeed.

## Set up and prove the first mission

Read [deployment](../../docs/13-deployment.md) for the host type, especially
§8b on macOS Docker Desktop. Required: dedicated Linux/macOS host, Docker
with Compose and Buildx, Python 3.12+, and this repository checkout.

```bash
uv tool install '.[mcp]'   # the MCP extra lets an agent hold DevCake's tools
devcake --help
devcake doctor --json
```

Prefer installing the CLI from the checkout so its command surface matches
the stack. Refresh it with `uv tool install --reinstall .` after updating the tree; a PyPI
install (`devcake-cli`) must match the chosen release instead. Doctor names
failed checks and remedies; it never performs sudo/usermod/linger changes.
Inspect warnings in context: occupied ports may be this stack or another
process, and missing supervision may need attention on a headless host.

For an authorized bring-up:

```bash
devcake up --bake
devcake status --json
devcake setup --help
```

`up --bake` seeds missing bootstrap secrets into mode-600 `.env`, discovers
Docker socket settings, computes the image digest, bakes app/admin/hello,
starts the stack, checks health/socket access, runs a hello dispatch, and
installs host-baker supervision. The baker builds the configured harness
pins; a real mission waits for its image receipt. `--bake all` requests
the full matrix. `devcake up` omits baking; `devcake up --dry-run` previews
the plan. Use Bake through this flow; Compose does not build DevCake images.
Standalone `devcake bake` is not implemented.

Configure the reachable stack with `devcake setup` or the admin UI at
`http://localhost:8080` (basic-auth login from `.env`). `setup` does not
start the stack. Use `--same-harness <template>` or `--role-harness` for the
first roster, then supported connection options; consult `--help` for exact
flags. Secrets enter via env/file/stdin sources, never literal argv values.
Model authentication and RO/reviewer tokens use the documented credential
interfaces; do not assume the basic `setup` connection flags cover them.

The UI groups **Connections** (PMO, Repositories, Skill sources), **Fleet**
(Dev Types, Mission Types, Prompts, Skills), and **Settings** (Policies,
Scheduled Tasks, Profiles & Export). Configuration edits require Save;
intake and Run now are immediate actions. Operator credentials live in the
app's `/data` volume, not bootstrap `.env`; plaintext stored secrets make
that volume and its backups sensitive.

Keep opt-in adoption and auto-merge off unless the operator chose otherwise.
Complete the security contract's first-EXECUTE checklist. Follow the
[first-mission tutorial](../../docs/tutorials/01-first-mission.md): prove
connections and a staffed harness, dispatch one authorized small ticket,
inspect its transcript and PR, and observe REVIEW and the merge handoff.
A hello success proves plumbing, not model credentials or real mission quality.

CLI exits: `0` success, `2` usage, `3` preflight, `4` bake/compose,
`5` setup conflict, `6` baker/supervisor, `1` other. Read the receipt and
stderr as well as the code. An existing roster is not something to erase
to force first-setup; inspect it and use the normal configuration interface.

## Shape context, memory, and recurring work

| Surface | Operating rule |
|---|---|
| Work repos (`pmos[].repos`) | Routing targets for mission branches and PRs. Check repository/default-branch selection before changing a ticket's routing. |
| Reference repos (`pmos[].reference_repos`) | Consultation clones across stages. Read-only by token and prompt contract, not a filesystem sandbox. |
| Memory repos (`pmos[].memory_repos`, `DevType.memory_repos`) | Persistent notebooks; the union is supplied to consumers at `/workspace/memory/<card>/`. Within a board, work/reference/memory lists are disjoint. |
| Skills | The editable built-in skill store and external **Skill sources** supply Dev Type selections. External IDs are `<source>/<skill>`; use dedicated source connections, not work-repo cards. “Required” adds prompt instructions, not guaranteed model compliance. This operator skill is separate from skills installed into Dev runs. |
| Context availability | `context_sourcing_strict` defaults true: unavailable memory or skill sources defer dispatch. False permits stale last-good content or omission; it does not relax required work/reference/blocker mirrors. Repair the named source before proposing a policy change. |

**Memory setup:** a notebook is an ordinary repository whose README defines
its filing/curation policy. Bind it to consumer boards or Dev Types. Create
a separate **Curator board** whose sole work repo is that notebook, staff
its EXECUTE/REVIEW roles, and configure the Memory Curator task. Discoveries
are copied by the app into `.claims/` as unverified leads; curator runs
propose note changes through the normal PR pipeline. Consumers do not author
notebook notes. App merges into a memory-bound repo additionally require
`memory_auto_merge` (default off); the repo's own `auto_merge` still applies.
Monitor missing Curator boards, capped claims queues, and waiting note PRs.
Consultation and note correctness are not guaranteed; the comparative pilot
remains open in [ADR-0035](../../docs/adr/0035-memory-notebooks-claims-conveyor-and-scheduled-tasks.md).

**Scheduled Tasks:** custom tasks create a labeled ticket from a template
on an elapsed interval, targeting a board and entry stage. That task's previous
ticket must finish before another fire creates work on that board. Three
failed automatic fires degrade the schedule; Run now can recover it after
the cause is fixed, but still honors intake pause and single-flight.
Memory Curator fires target Curator boards (automatic fires skip empty
claims queues). The Relations Steward shares this UI but runs directly to
propose dependency edges. Specify a concrete deliverable and verification
in recurring ticket text; these are agent workloads with costs.

Read [configuration and API](../../docs/11-admin-panel.md),
[domain fields](../../docs/02-domain-model.md), and
[harness/skill behavior](../../docs/08-harness-templates.md) as needed.

## Investigate and correct failures

1. **Establish state.** Run `devcake status --json`; inspect `health_reachable`,
   `health_error`, and PMO budgets, not just process liveness. Fetch
   `GET /api/v1/health` for full health detail. For "is it frozen or
   waiting", read `GET /api/v1/activity` (the admin's activity bar): the
   phases in flight with their durations, boards skipped for a tracker's
   quota with the retry hint, and a dead poll loop reading as stalled.
   Match the mission's live board state with its latest run, feed, and PR.
   Check pauses, staffing/bake waits, dependencies, merge waits, and human
   handoffs before calling it a failure.
2. **Locate the cause.** Use Overview/health, Runs/transcript, and bounded
   `docker compose logs --tail=100 app` (or the affected service). Check
   `poll_degraded`, `circuit_breakers`, `dev_backend_degraded`, repository
   probe detail, PMO budgets/rate limits, and scheduled-task outcomes.
3. **Make the smallest authorized correction.** Fix the named credential,
   branch, connection, assignment, or ticket instruction. Re-read current
   configuration before saving; preserve unrelated settings and secrets.
   Use the app's validated interfaces, not direct edits to `/data` files.
4. **Verify at the original symptom.** Re-read health, connection-test
   results, and the mission after reconciliation. If a retry may have
   already created a ticket/PR or committed a transition, inspect that
   remote state before replaying the action. Respect provider backoff. Stop
   repeating an unchanged failure and report the unresolved dependency.

| Observation | Appropriate next step |
|---|---|
| `DEVCAKE-NEEDS-HUMAN` | Read the handoff or plan-approval request. Resolve it before removing the label/resuming. |
| `DEVCAKE-FAILED` | Diagnose first; removing the label grants fresh attempts. Under the strict reset policy, a comment containing literal `DEVCAKE-RETRY` grants attempts before give-up; ordinary comments do not. |
| PR at `DEVCAKE-MERGE` | Usually an intentional human merge wait. For authorized rework, swap to `DEVCAKE-EXECUTE` with a clear comment; it reuses the branch/PR. |
| `DEV_AUTH` breaker | Correct/re-upload the Dev Type's credentials; that write clears it. Repo breakers clear after a successful probe. |
| `dev_backend_degraded` / tracker throttling | Inspect provider health and request budgets. Backend degradation allows a probe run and clears on success; avoid credential resets or retry storms. |
| Mirror/context failure | Check the named card's URL, branch, token, and source reachability. Do not silently disable strict context sourcing. |

Board comments guide the next run. `DEVCAKE-SKIP` prevents further mission
progression; intake pause stops new dispatch while in-flight work and
finalization continue. Neither is an emergency kill. Read the
[operations tutorial](../../docs/tutorials/02-operating-devcake.md) and
[error/retry contract](../../docs/15-errors-and-retries.md) for the specific case.

**Prefer the MCP tools when they are available.** If the operator's agent
configuration includes `devcake mcp` (`--read-only` for inspection, plain
for changes), every admin-API operation is a tool with the same contract as
[docs/11](../../docs/11-admin-panel.md): read state with the read tools,
change it with the write tools, and expect a 409 with the reason when the
app refuses. Secret values never cross those tools by design; use presence
checks and connection tests. Every change made through them is audited as
actor `mcp`. Without MCP, build the requests by hand as follows.

For API work, read the relevant route in
[docs/11](../../docs/11-admin-panel.md) before constructing requests. The
host entry is the loopback admin proxy's `/api/v1`, with basic auth from
`.env`; mutating requests require `X-DevCake-Request: 1`. Keep loopback
credentials out of configured HTTP proxies and command output. Use existing
secret presence indicators and connection tests rather than secret exports
for diagnosis. Preserve UI drafts; disclose direct API changes to the operator.

## Maintain and hand back evidence

For upgrades, follow the [runbook](../../docs/13-deployment.md) and
[operator duties](../../docs/18-operator-contract.md). Pause intake, let
active runs/finalizers drain, and prepare backups of `/data` and any Gitea
data holding real work using the shipped scripts. Settings exports do not
replace repository backups. **Stop Dagu before updating the checkout**:
`dagu/dags` is a live bind, so a changed DAG can take effect before a rebuild.
Preserve local edits, update to the chosen revision, refresh the CLI, and
use `devcake up --bake` (full `--bake all` for a full image/protocol upgrade).
Verify health, baker/image readiness, and a hello dispatch before restoring
the operator's prior intake state. A successful build alone is insufficient.

Report what you observed, what changed, the resulting health/mission state,
and anything unproven. Distinguish a successful repair from a retry merely
queued. If repair needs a DevCake code change, follow
[AGENTS.md](../../AGENTS.md) and make a tested, reviewable patch; operating
authorization does not automatically authorize deploying that patch.
