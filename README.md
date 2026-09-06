<div align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/img/brand/A-devcake-full-color-transparent.svg">
    <img src="docs/img/brand/C-devcake-full-black-transparent.svg" alt="DevCake" width="380">
  </picture>

### Your personal software factory. Tickets in, pull requests out.

</div>

DevCake runs a team of coding agents from your task board. Give it a goal;
it breaks down large work, plans, implements, and reviews. You get pull
requests, session transcripts, and recorded token usage and costs. You steer
on the board and decide what gets merged. Every run is a clean room that
starts from the ticket and the recorded artifacts alone, so what an agent
knew is exactly what the record shows.

**Bring a coding agent.** Use your preferred agent as your setup and operations
companion: have it configure the host, connect your tools, explain the queue,
and diagnose failed runs. DevCake keeps the work moving between your sessions.

Clone this repository, open it in your agent, and start with:

> Read `skills/devcake-ops/SKILL.md`. Explain how DevCake fits my workflow,
> check this host, and help me set it up for one small mission. Keep
> auto-merge off. Verify the result and tell me what still needs my attention.

The [operator skill](skills/devcake-ops/SKILL.md) is agent-neutral. Ask your
agent to read it explicitly; automatic discovery is optional.

## Start with one mission

You need a **dedicated Linux or macOS host**, Docker with Compose and Buildx,
Python 3.12+ with [uv](https://docs.astral.sh/uv/) (or pip), and model
credentials. Ask your agent to check the
[deployment requirements](docs/13-deployment.md), then, from this checkout:

```bash
uv tool install .          # install the CLI from the version you will run
devcake doctor --json      # inspect prerequisites and remedies
devcake up --bake          # prepare secrets, build, start, and smoke-test
```

Have your agent connect a board and repositories with `devcake setup`
(`--help` lists options), or use the admin UI at
**http://localhost:8080**. Its login lives in the generated `.env`.
Configure model credentials there; the host baker builds the selected agent images.

**Ask your agent:** “Check my connections and branch protection, then walk me
through the [first mission](docs/tutorials/01-first-mission.md).” Start in
opt-in mode with one `DEVCAKE`-labeled ticket and inspect its PR together.

## A fresh start for every run

The usual path is **ONBOARD → PLAN → EXECUTE → REVIEW → merge**. Large
missions split into linked tickets; triage can supply the plan. REVIEW always
runs. For work that produces a PR, **Done means merged**; auto-merge is off
by default.

Each run starts a fresh session in a disposable container, with the ticket,
prior transcripts and plans, relevant upstream work, and selected repositories
and skills. Progress passes between runs through these artifacts; continuations
within a run can resume its session.

**Ask your agent:** “Help me write a bounded mission with acceptance criteria,
choose its repositories, and decide whether plans should need my approval.”

## Give the team the right context

| Ingredient | What it does | Ask your agent |
|---|---|---|
| **Work repositories** | Targets for branches and PRs. Use GitHub, GitLab, Gitea, or the bundled Gitea for code, documents, and other deliverables. | “Where should this team's output go?” |
| **Reference repositories** | Consultation copies supplied across stages: shared libraries, specifications, examples. | “Which sources should this team consult?” |
| **Memory repositories** | Persistent notebooks shared across missions, bound to a board or Dev Type. Consumers get read-only context. | “Help me organize a notebook and its curation.” |
| **Skills** | Reusable instructions selected per Dev Type, from the editable built-in store or trusted external Git skill sources. | “Find useful skills and review their instructions with me.” |
| **Dev Types** | Harness, model, credentials, prompts, skills, and capacity assigned to pipeline roles. | “Staff my planning, execution, and review roles.” |

Memory is ordinary Git: a notebook's README defines its organization. Runs
contribute unverified leads to `.claims/`; a Memory Curator works through
them and proposes notes as PRs. **You merge notes by default.** DevCake
handles transport and review; learning depends on the notes you keep.
The implementation's comparative pilot is still [outstanding](docs/adr/0035-memory-notebooks-claims-conveyor-and-scheduled-tasks.md#ship-gate-throwaway-box-ab-not-yet-satisfied).

Supported boards: **Linear, GitHub Issues, GitLab Issues, Gitea Issues**.
Supported harnesses: **Claude Code, Codex, Grok Build, Pi, OpenCode, Qwen Code**.
Mix them by role or team.

## Put recurring work on a schedule

Scheduled Tasks create tickets from a template at an interval: dependency
reviews, documentation checks, recurring reports, or your own maintenance
routine. They use the normal mission pipeline. The same settings page holds
the **Memory Curator** and the **Relations Steward**, which adds missing
ticket dependencies on the board. Intake pause applies to scheduled work too.

**Ask your agent:** “Set up a recurring documentation check with a clear
deliverable, a suitable interval, and human review.”

## Operate it with your agent

- “Read `devcake status --json`. Explain what's running, waiting, or blocked.”
- “Investigate this failed mission using its ticket, run record, and logs.
  Fix the cause before granting another attempt.”
- “Review token usage and concurrency with me before we expand intake.”
- “Prepare backups and a quiet upgrade; verify health and a dispatch afterward.”

Ask your agent to use the [operator skill](skills/devcake-ops/SKILL.md) and
[daily operations tutorial](docs/tutorials/02-operating-devcake.md) for these tasks.

**You own the host and the trust.** Ticket authors and repository contributors
can influence agents holding your credentials. Docker access is
root-equivalent; containers are not an injection-proof sandbox. Keep control
ports private, protect default branches, and safeguard backups containing
plaintext secrets. Auto-merge off constrains the app; forge branch protection
constrains agent tokens. Ask your agent to walk through the
[security contract](docs/14-security.md) and
[operator duties](docs/18-operator-contract.md) before real work.

## Evidence and next steps

DevCake is early-production software. In our own
[documented run](docs/evidence/2026-08-devcake-audits-devcake.md), one board
prompt became 54 tickets, 257 runs, and 42 human-merged PRs: self-reported
evidence to assess with your agent against your own workload.

Ask your agent to explore the [product overview](docs/00-overview.md),
[architecture](docs/01-architecture.md), or [design thesis](docs/19-thesis.md)
with you. For changes to DevCake itself, start with
[Contributing](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

[Changelog](CHANGELOG.md) · [Roadmap and known gaps](docs/16-roadmap.md) ·
[Report a vulnerability](SECURITY.md) · [GPL-3.0 license](LICENSE)
