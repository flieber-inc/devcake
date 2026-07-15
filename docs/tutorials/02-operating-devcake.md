# Tutorial 2 — Operating DevCake Day to Day

You never **babysit** DevCake through a chat UI — you operate work **through
Linear**. Labels and statuses are the control surface; DevCake reads them as
instructions and reports back the same way. The admin panel is for config,
health, secrets, and runs — keep it on **localhost** (or SSH tunnel); it is
host-equivalent power (`14-security.md` §4).

This tutorial is the vocabulary plus the interventions you'll actually use.

## The label language

| You see… | It means… | You can… |
|---|---|---|
| `DEVCAKE` | Adopted: DevCake owns this mission (opt-in mode) | Remove it to un-adopt at any point |
| `DEVCAKE-PLAN` | Queued for a planning run | — |
| `DEVCAKE-EXECUTE` | Queued for implementation (a plan exists in the feed) | — |
| `DEVCAKE-REVIEW` | Implementation done; queued for skeptical review | — |
| `DEVCAKE-MERGE` | **Approved — waiting for your merge.** The PR comment has the copy-paste command | Merge (→ Done) or close the PR (→ Canceled) |
| `DEVCAKE-CREATED` | This issue was authored by DevCake (a decomposition child) | Treat as any adopted issue |
| `DEVCAKE-TRACKING` | A decomposed project; auto-completes when its children finish | — |
| `DEVCAKE-FAILED` | Gave up after 3 failed attempts (comment explains; trace linked) | Fix the cause, remove the label → fresh retries |
| `DEVCAKE-NEEDS-HUMAN` | A Dev hit something only you can do (credentials, an external account, a judgment call) — the hand-off comment carries the evidence | Do the thing, remove the label → DevCake resumes |
| `DEVCAKE-SKIP` | You told DevCake: hands off | Remove to resume |

Statuses mean exactly what they say: **Backlog** = untouched, **In Progress** =
DevCake pulled it, **Done** = the PR is *merged* (never before), **Canceled** =
abandoned (decomposed away, or PR closed unmerged).

## Interventions that work (all verified)

- **Pause anything:** add `DEVCAKE-SKIP`. It outranks every other label.
- **Force a rework:** swap `DEVCAKE-MERGE` (or `-REVIEW`) → `DEVCAKE-EXECUTE`,
  optionally with a comment saying what you want changed — the next run reads
  the feed, reuses the branch, and updates the same PR. This is also the answer
  when a waiting PR grows **merge conflicts** — though with `auto_merge` +
  `auto_resolve_merge_conflicts` ON, DevCake does this swap itself (up to 2
  attempts) before handing the conflict to you; the manual swap remains the
  recovery path after that, or with the toggle OFF.
- **Edit mid-run without fear:** if you change a mission's stage label while a
  Dev is running, DevCake finishes, posts its output, and *applies nothing* —
  a comment tells you your edit won. Your labels always beat its labels.
- **Re-triage:** move an untouched-looking mission back to Backlog with no stage
  labels and it becomes ONBOARD material again.

## Reading the bill

Every step posts a token report to the feed — model, token counts (full split
for Claude and Codex; totals for Grok), and cost where the harness reports it.
For aggregates, the Logs page (OpenObserve) carries every run as a trace with
`devcake.tokens.*` / `devcake.cost.usd` attributes. Watch for the **loop
warning** comment: every third review rejection it posts the mission's
cumulative recorded cost — that's your cue to intervene or SKIP.

## The two big switches (Config)

- **`adoption_mode`** — `opt_in` (default: only `DEVCAKE`-labeled items) vs
  `opt_out` (**the entire team**, existing backlog included; DevCake will start
  working it by priority, spending tokens — flip deliberately. Remember: the
  whole team is in the agent trust boundary).
- **`auto_merge`** — off (default): every merge is yours; `DEVCAKE-MERGE` is the
  handoff point. On: approved PRs merge themselves and missions go straight to
  Done. Enable only with **branch protection**, a clear review setup, and eyes
  open (`14` §2 zone C). Independent REVIEW Dev Type is **recommended**, not
  enforced.

## Security warnings and daily hygiene

- Read **security_warnings** on Overview/health (write token on all stages,
  unprotected branch, basic-auth secrets reminder). Dismissing = accepting residual risk.
- Prefer a **RO forge token** for non-EXECUTE when you can.
- Do not expose `:8080` / Dagu / OO past loopback without reading `14`.
- `/data` backups contain every GUI secret — handle like a password export.

## Scaling up

Per-Dev-Type concurrency caps plus a global cap bound how many containers run at
once (`/data/config/`); priorities decide the queue order; and the assignment
matrix decides which Dev Type performs each mission type — including the extra
CLI args slot (e.g. ONBOARD's `--max-turns 15` triage budget). When you're ready
to point DevCake at a real team: start `opt_in`, keep `auto_merge` off, label
one small mission, and expand from there.
