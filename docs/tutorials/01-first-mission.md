# Tutorial 1 — Your First Mission, End to End

Thirty minutes from a clean machine to watching DevCake take a Linear issue all
the way to a merged pull request. Every step below mirrors the exact flow the
project's own integration testing uses.

## What you need

- **Docker** with Compose v2.
- **A dedicated Linear team** — create a fresh team (e.g. key `DEV`) just for
  DevCake at first. DevCake only ever touches the one team you configure, but a
  sandbox keeps your learning mess out of real projects. You'll need a personal
  **API key** (Linear → Settings → API).
- **A sandbox GitHub repository** with a bit of code in it (a couple of files is
  plenty — DevCake's triage reads the repo), plus a **PAT** with `contents` and
  `pull_requests` read/write on it.
- **Model credentials** — DevCake prefers subscriptions:
  - Claude Code: run `claude setup-token` on any machine with your subscription
    and keep the token it prints.
  - Grok Build: nothing yet — you'll log in through DevCake in step 3.

## Step 1 — Configure and start

```bash
git clone https://github.com/fidecastro/devcake && cd devcake
cp .env.example .env
```

Fill in `.env`: `LINEAR_API_KEY`, `DEVCAKE_TEAM_KEY` (your sandbox team's key),
`GITHUB_TOKEN`, `DEVCAKE_REPO_URL` (your sandbox repo), `CLAUDE_CODE_OAUTH_TOKEN`,
and passwords of your choosing for OpenObserve, Redis, Dagu, and the admin panel.
One machine-specific value: `DOCKER_GID` — find it with
`stat -c %g /var/run/docker.sock`.

> ⚠️ Don't put inline comments after values in `.env` — some tools treat them
> as part of the value.

```bash
docker buildx bake all    # FIRST: builds app, admin, and all Dev images (docker-bake.hcl)
docker compose up -d      # then start the stack — compose does not build DevCake images
```

(Bake first — otherwise the scheduler can try to dispatch a Dev before its
image exists. On upgrades, re-run `docker buildx bake all` so app and Dev
images move together.)

Open **http://localhost:8080** (your admin user/password from `.env`). The
header health strip should show every dot green. On this first boot DevCake also
created its ten `DEVCAKE-*` labels in your Linear team — go look.

## Step 2 — Meet the four pages

- **Overview** — the landing dashboard: component health, advisory alerts,
  the merge queue, and anything waiting on a human. The sidebar next to it
  carries the **mission-intake switch** — the one master control.
- **Config** — every setting: PMO team, repository, Dev Types (with OAuth
  wizards), assignments, limits, and the two big toggles (adoption mode,
  auto-merge). Secrets stay in `.env`; everything else is editable here —
  nothing saves until you review the change list and hit Save.
- **Runs** — the live run table (every run appears under a name like
  `DEV-17-3-EXECUTE-560E6T`: *mission* `DEV-17`, *step* 3, *type* EXECUTE);
  click a row for its live terminal, or open Dagu for the executor's view.
- **Logs** — opens OpenObserve. One Dev run = one trace, from dispatch through
  the container to finalization.

## Step 3 — Log Grok in (one time)

On the admin panel's Config page, find the **main-dev** card and click
**"Connect via OAuth…"** — a dialog shows a URL and a code; open, enter, approve
with your X/xAI account, and the dialog completes itself. (The same wizard works
for Codex Dev Types. Terminal alternative: `./scripts/grok_login.sh`.) The
session is DevCake's own — your local Grok CLI (if any) is untouched.

## Step 4 — Create a mission

In your sandbox Linear team, create an issue:

- **Title:** something real but small — e.g. *"Add input validation to the parser,
  with tests"*.
- **Description:** write it like you'd brief a competent contractor. The better
  the brief, the better the triage.
- **Label:** add **`DEVCAKE`** — this is the adoption signal; without it DevCake
  ignores the issue entirely (default opt-in mode).
- **Status:** Backlog. **Priority:** your call — DevCake works Urgent → Low.

## Step 5 — Watch it flow

Within ~30 seconds (the poll interval) you'll see, in the issue's activity feed:

1. Status flips to **In Progress**; a Dev container appears on the Runs page.
2. A transcript comment `1_ONBOARD.md` — the triage verdict and reasoning.
3. A **token report** — model, tokens, and cost for that step. Every step posts
   one; this is your running bill.
4. A label appears: `DEVCAKE-PLAN` (needs a plan), or `DEVCAKE-EXECUTE` (the
   triage already produced a plan — it's attached as `PLAN_1.md`), or for truly
   tiny missions the trivial path opens a PR immediately.
5. The cycle continues on its own: plan → implementation on branch
   `devcake/<KEY>` → a pull request on your repo → `DEVCAKE-REVIEW` → a
   skeptical review posted to the PR.
6. On approval you'll find the mission wearing **`DEVCAKE-MERGE`**: DevCake is
   waiting for *you* to merge. The PR comment ends with the exact command:
   ```
   gh pr review --approve <url> && gh pr merge --squash <url>
   ```
   Merge it — within a poll cycle the mission flips to **Done**.

That's the whole loop. If the review had found problems instead, the mission
would have gone back to `DEVCAKE-EXECUTE` with the findings, and the next
implementation run would rework the same branch and PR.

## If something goes wrong

- **`DEVCAKE-FAILED` appears** — three attempts at a step failed; the comment
  says why and links the trace. Fix the cause, **remove the label**, and DevCake
  retries with a fresh attempt counter.
- **A run seems stuck** — Runs page → open the run in Dagu → Stop. The
  mission reschedules by itself; no cleanup needed.
- **You want DevCake to leave an issue alone** — add **`DEVCAKE-SKIP`** at any
  time. It always wins.

Next: [Tutorial 2 — Operating DevCake day to day](02-operating-devcake.md).
