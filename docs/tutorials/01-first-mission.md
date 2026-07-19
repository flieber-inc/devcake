# Tutorial 1 — Your First Mission, End to End

Thirty minutes from a clean machine toward watching DevCake take a Linear issue
through to a pull request (and Done after **you** merge, with auto-merge off).
Every step mirrors the project's integration-test shape.

> **Security contract:** [docs/14-security.md](../14-security.md) — dedicated host,
> ticket writers can steer agents, supply chain is your job.

## What you need

- **Docker** with Compose v2 + **Buildx** (Bake builds images).
- A **machine you control** (dedicated host posture — Dagu uses `docker.sock`).
- **A dedicated Linear team** — create a fresh team (e.g. key `DEV`) just for
  DevCake at first. **Anyone who can write issues on that team can influence
  agents that hold your forge token** (`14` §0). You'll need a personal
  **API key** (Linear → Settings → API) — entered in Config later, not long-lived in `.env`.
- **A sandbox GitHub repository** with a bit of code, plus a **PAT** with
  `contents` and `pull_requests` read/write. Prefer also a **read-only PAT** for
  non-EXECUTE stages. Enable **branch protection** on the default branch before
  production-ish use (`13` §8a).
- **Model credentials** — subscriptions preferred:
  - Claude Code: `claude setup-token` → paste in Config.
  - Grok Build: OAuth via DevCake in step 3.

## Step 1 — Bootstrap and start

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env
```

Fill in **bootstrap only** (schema v4 — see `.env.example`):

- Strong passwords: `ADMIN_*`, `REDIS_PASSWORD`, `DAGU_PASSWORD`, `OO_*`,
  `GITEA_ADMIN_PASSWORD` (empty/`change-me*` refuse boot unless
  `DEVCAKE_ALLOW_INSECURE=1`).
- `DOCKER_GID` — `stat -c %g /var/run/docker.sock`.

**Do not** put Linear/forge/model tokens in `.env` for normal ops — Config page
stores them under `/data/secrets/` (ADR-0011).

> ⚠️ Don't put inline comments after values in `.env`.

```bash
docker buildx bake all    # FIRST: app, admin, all Dev images
docker compose up -d      # compose does not build DevCake images
```

Open **http://localhost:8080** (loopback; admin user/password from `.env`).
**Sidebar** health dots should go green (service health lives in the sidebar, not a page header).

### Step 1b — Secrets and connections

1. **Configuration → PMO** — Add PMO instance → team key → **Set** Linear API key → Test.
2. **Repositories** (`#/repos`) — Add repository → URL → **Set** write token (and optional RO / reviewer) → Test. Repos are **not** under Configuration.
3. **Configuration → Dev Types** — Assign harness secrets / OAuth.
4. On **Repositories**, leave **`auto_merge` OFF** for this tutorial.
5. Prefer different Dev Types for EXECUTE vs REVIEW (warned if shared).

Labels `DEVCAKE-*` appear on the team after a successful PMO connection.

## Step 2 — Meet the six pages

- **Overview** — masthead answer sentence, Let's get baking checklist, alerts, Needs Human Action, stats, In the oven, recent runs, quick links. Service health = **sidebar** dots.
- **Missions** — kanban board of the poll snapshot; **Poll now**; card MoreMenu (Park/Retry/…); drawer Send guidance + Stop run.
- **Runs** — live table; click a row for the terminal; open Dagu for the executor; rare actions (stop/clear) live in the ⋯ MoreMenu.
- **Repositories** — external forge repos + bundled internal Gitea operator repos + merge posture toggles (not under Configuration).
- **Configuration** — sections: PMO, Dev Types, Skills, Assignments, Prompts, Profiles & Export, Limits, Traffic.
  Secrets are VALUES here (never echoed back). Connection tests hit `/connections/pmo/{name}/test` and `/connections/forge/{name}/test`.
- **Logs** — **Open OpenObserve ↗**. One Dev run = one trace.

## Step 3 — Log Grok in (one time)

On Configuration → Dev Types, **main-dev** → **Connect via OAuth…** — dialog shows URL + code.
(Or `./scripts/grok_login.sh`.) Session is DevCake's own.

## Step 3b — Supply-chain checklist (before you create the mission)

| Check | Why |
|---|---|
| Sandbox Linear team (or tight membership) | Ticket writers = agent trust |
| Branch protection on default branch | Primary containment (`14` zone C) |
| `auto_merge` still **off** | You merge; Done only after merge |
| RO forge token set (optional but recommended) | Non-EXECUTE stages shouldn't get write |
| EXECUTE ≠ REVIEW Dev Type (recommended) | Independent second look |
| Health `security_warnings` read, not dismissed unread | Advisory posture (`14` §8) |

Full list: `14-security.md` §9.

## Step 4 — Create a mission

In your sandbox Linear team, create an issue:

- **Title:** small and real — e.g. *"Add input validation to the parser, with tests"*.
- **Description:** brief a competent contractor.
- **Label:** **`DEVCAKE`** (opt-in adoption).
- **Status:** Backlog. **Priority:** your call.

## Step 5 — Watch it flow

Within ~30 seconds (poll interval), on the issue feed:

1. Status → **In Progress**; a Dev appears on Runs.
2. Transcript `1_ONBOARD.md` + **token report** (every step posts one).
3. Label: `DEVCAKE-PLAN`, or `DEVCAKE-EXECUTE` (plan attached — trivial or
   opportunistic; ONBOARD never implements).
4. Cycle continues: plan → branch `devcake/<INSTANCE>-<KEY>` (e.g. `devcake/LINEAR-DEV-1`) → PR → `DEVCAKE-REVIEW`.
5. On approval (with auto-merge **off**): **`DEVCAKE-MERGE`** — you merge:
   ```
   gh pr review --approve <url> && gh pr merge --squash <url>
   ```
   After merge, a poll cycle marks the mission **Done**.

If review rejects, label returns to `DEVCAKE-EXECUTE` on the same branch/PR.

## If something goes wrong

- **`DEVCAKE-FAILED`** — three failed attempts; fix cause, **remove the label**.
- **Stuck run** — Runs → Dagu → Stop; mission reschedules (INV-3).
- **Leave an issue alone** — **`DEVCAKE-SKIP`** always wins.

Next: [Tutorial 2 — Operating DevCake day to day](02-operating-devcake.md).
