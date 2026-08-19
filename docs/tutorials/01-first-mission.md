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
- **Both default-staffing credentials** — default Mission Types send
  ONBOARD / PLAN / REVIEW to Dev Type **`judgment`** (`claude-code`) and
  EXECUTE to **`implementer`** (`grok-build`). A literal reader needs **both**
  before creating the mission; missing Claude fails judgment stages, missing
  Grok fails EXECUTE. Registry table: [`08-harness-templates.md`](../08-harness-templates.md) §4.
  - Claude Code: `claude setup-token` → paste `CLAUDE_CODE_OAUTH_TOKEN` in
    Configuration → Dev Types (or `ANTHROPIC_API_KEY`).
  - Grok Build: device-code OAuth via DevCake in step 3 (or `XAI_API_KEY`).

## Step 1 — Bootstrap and start

```bash
# Clone the remote or fork you intend to run (no fixed public product URL yet):
git clone <this-repo-url> && cd devcake
cp .env.example .env
```

Fill in **bootstrap only** (schema v4 — see `.env.example`):

- Strong passwords: `ADMIN_*`, `REDIS_PASSWORD`, `DAGU_PASSWORD`, `OO_*`,
  `GITEA_ADMIN_PASSWORD` (empty/`change-me*` refuse boot unless
  `DEVCAKE_ALLOW_INSECURE=1`).
- Leave `DOCKER_GID` blank — `./up.sh` discovers it from
  `/var/run/docker.sock` (or set manually with `stat -c %g /var/run/docker.sock`).

**Do not** put Linear/forge/model tokens in `.env` for normal ops — Config page
stores them under `/data/secrets/` (ADR-0011).

> ⚠️ Don't put inline comments after values in `.env`.

```bash
./up.sh --bake            # DOCKER_GID + control plane + hello + host baker
# Day-to-day (images already baked):  ./up.sh
```

Open **http://localhost:8080** (loopback; admin user/password from `.env`).
**Sidebar** health dots should go green (service health lives in the sidebar, not a page header).

### Step 1b — Secrets and connections

1. **PMO** (`#/pmo`, under Adapters) — Add PMO instance → team key → **Set** Linear API key → Test.
2. **Repositories** (`#/repos`) — Add repository → choose a short card **name**
   (lowercase alnum, ≤12 chars — this is the `devcake-repo:<name>` marker) →
   URL → **Set** write token; prefer **RO** for non-EXECUTE and a **reviewer**
   token (app-only, different account) for formal forge approval → Test.
   Repositories and PMO both live under the sidebar's **Adapters** group.
3. **Bind that card on the PMO instance** — still on **PMO** (`#/pmo`), under
   the instance's **Repositories** chips, select the sandbox card you just
   added (first selected = default for unmarked tickets). **Save** the shared
   draft. Without this bind, the instance's work-repo set stays empty and
   missions take the **zero-repo** path: bundled internal Gitea with forced
   auto-merge — not a GitHub PR ([operator-drill](operator-drill.md) §3;
   ADR-0010 / `docs/06`).
4. **Configuration → Dev Types** — set **both** harness credentials from
   *What you need* (Claude for `judgment`, Grok OAuth or key for
   `implementer`).
5. On **Repositories**, leave the **external** card's **`auto_merge` OFF** for
   this tutorial (after REVIEW approve you expect `DEVCAKE-MERGE` and merge
   with `gh` yourself).

Labels `DEVCAKE-*` appear on the team after a successful PMO connection.

## Step 2 — Meet the admin pages

Six top-level sidebar items (Adapters expands to two pages — seven surfaces total):

- **Overview** — masthead answer sentence, Let's get baking checklist, alerts, Needs Human Action, stats, In the oven, recent runs, quick links. Service health = **sidebar** dots.
- **Missions** — pipeline strip (stage counts) + grouped mission list of the poll snapshot; **Poll now**; row MoreMenu (Park/Retry/…); drawer Send guidance + Stop run.
- **Runs** — live table; click a row for the terminal; open Dagu for the executor; rare actions (stop/clear) live in the ⋯ MoreMenu.
- **Adapters** — sidebar group with two pages: **Repositories** (external forge repos + bundled internal Gitea operator repos + merge posture toggles) and **PMO** (instances + adoption mode). Both edit the same shared config draft; connection tests hit `/connections/pmo/{name}/test` and `/connections/forge/{name}/test`.
- **Configuration** — sections: Dev Types, Mission Types, Skills, Prompts, Limits, Scheduled Tasks, Profiles & Export.
  Secrets are VALUES here (never echoed back).
- **Consoles** — the external UIs: OpenObserve (traces/costs), Dagu (execution history), Gitea (internal forge, when enabled). One Dev run = one trace.

## Step 3 — Log Grok in (one time)

On Configuration → Dev Types, **implementer** → **Connect via OAuth…** — dialog shows URL + code.
(Or `./scripts/grok_login.sh` — defaults to the seeded `implementer`; override with
`DEVCAKE_DEV_TYPE` or a positional Dev Type name.) Session is DevCake's own. Device-code OAuth
rides the operator's **xAI account billing / subscription quota** (same trust
as pasting an `XAI_API_KEY`). Grok's feed cost is an **app-side rate-card
estimate** (`08` §5 / ADR-0021) — the harness does not report billed USD.

## Step 3b — Supply-chain checklist (before you create the mission)

| Check | Why |
|---|---|
| Sandbox Linear team (or tight membership) | Ticket writers = agent trust |
| **Branch protection** on default branch (forge UI: require PR + ≥1 approval; Dev account cannot bypass) | Primary containment — forge enforces merges, not `auto_merge` (`14` zone C, `13` §8a) |
| `auto_merge` still **off** | App will not merge; you merge; Done only after a real merge |
| Write token set; **RO** forge token set (recommended) | EXECUTE pushes/opens PR; non-EXECUTE should not hold write |
| **Reviewer** token from a different account (recommended for formal forge approval) | App-only — never given to a Dev; second identity under branch protection / later auto-merge |
| Health `security_warnings` read, not dismissed unread | Advisory posture (`14` §8) |

Mental model: EXECUTE opens the PR → REVIEW Dev judges → app may formally
approve with the reviewer token → with auto-merge **off**, **you** merge on the
forge. Full walkthrough: README “How forge merges are controlled” · `14` §2.

Full list: `14-security.md` §9.

## Step 4 — Create a mission

In your sandbox Linear team, create an issue:

- **Title:** small and real — e.g. *"Add input validation to the parser, with tests"*.
- **Description:** brief a competent contractor, and include a backticked
  work-repo marker matching the card name from step 1b, e.g.
  `` `devcake-repo:sandbox` `` (lowercase alnum, ≤12). Resolution order is
  marker → instance default (first chip) → zero-repo; this tutorial needs the
  external card, so put the marker (or rely on the chip default from step 1b).
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
