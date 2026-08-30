# Tutorial 2 — Operating DevCake Day to Day

Your board is the interface — you operate work **through the PMO** (Linear,
Gitea Issues, GitHub Issues, or GitLab Issues — all launch-supported), not a
chat UI. Labels and statuses are the control surface; DevCake reads them as
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
| `DEVCAKE-MERGE` | **Approved — waiting for your merge.** The PR comment has the copy-paste command | Merge (→ Done) or close the PR (→ Canceled). If sibling discoveries landed after approve, **Re-check freshness** from the mission ⋯ menu may re-open REVIEW |
| `DEVCAKE-CREATED` | This issue was authored by DevCake (a decomposition child) | Treat as any adopted issue |
| `DEVCAKE-TRACKING` | A decomposed project; auto-completes when its children finish | — |
| `DEVCAKE-FAILED` | Gave up after 3 failed attempts (comment explains; trace linked) | Fix the cause, remove the label → fresh retries |
| `DEVCAKE-NEEDS-HUMAN` | A Dev hit something only you can do (credentials, an external account, a judgment call) — the hand-off comment carries the evidence | Do the thing, remove the label → DevCake resumes |
| `DEVCAKE-DISCOVERY` | Harvested discoveries still waiting to be routed (sweep gate) — visible on the board until routing clears; does not block progression | Wait for the discovery steward, or leave it — not a stage label |
| `DEVCAKE-SKIP` | You told DevCake: hands off | Remove to resume |

Statuses mean exactly what they say: **Backlog** = untouched, **In Progress** =
DevCake pulled it, **Done** = the PR is *merged* (never before), **Canceled** =
abandoned (decomposed away, or PR closed unmerged).

## Interventions that work (all verified)

- **Pause anything:** add `DEVCAKE-SKIP`. It outranks every other label.
- **Force a rework:** swap `DEVCAKE-MERGE` (or `-REVIEW`) → `DEVCAKE-EXECUTE`,
  optionally with a comment saying what you want changed — the next run reads
  the feed, reuses the branch, and updates the same PR. This is also the answer
  when a waiting PR grows **merge conflicts** — though with that repo's
  `auto_merge` + `auto_resolve_merge_conflicts` ON, DevCake does this swap itself (up to 2
  attempts) before handing the conflict to you; the manual swap remains the
  recovery path after that, or with the toggle OFF.
- **Edit mid-run without fear:** if you change a mission's stage label while a
  Dev is running, DevCake finishes, posts its output, and *applies nothing* —
  a comment tells you your edit won. Your labels always beat its labels.
- **Grant fresh attempts before give-up:** under the default strict
  attempt-reset policy (ADR-0026), an ordinary comment does NOT restart a
  failing step's attempt count — comment with the literal `DEVCAKE-RETRY` in
  the body (e.g. after fixing the cause) to grant a fresh budget. After
  `DEVCAKE-FAILED` lands, removing the label does the same. With the policy
  set to *Any comment* (Policies), every non-DevCake comment resets —
  fine on a board with no bot traffic, defeated by one chatty integration.
- **Re-triage:** move an untouched-looking mission back to Backlog with no stage
  labels and it becomes ONBOARD material again.

## Reading the bill

Every step posts a token report to the feed — model, token counts (full split
for Claude and Codex; totals for Grok), and cost where the harness reports it.
For aggregates, the Consoles page (OpenObserve) carries every run as a trace with
`devcake.tokens.*` / `devcake.cost.usd` attributes. Watch for the **loop
warning** comment: every third review rejection it posts the mission's
cumulative recorded cost — that's your cue to intervene or SKIP.

## The three big switches

- **`adoption_mode`** (the PMO page, under Connections) — `opt_in` (default: only `DEVCAKE`-labeled items) vs
  `opt_out` (**the entire team**, existing backlog included; DevCake will start
  working it by priority, spending tokens — flip deliberately. Remember: the
  whole team is in the agent trust boundary).
- **`auto_merge`** (per repo card on the Repositories page, under Connections) — off (default): the
  **app** will not merge; `DEVCAKE-MERGE` is the handoff (normally you merge).
  On: after REVIEW approves, the **app** merges and missions go straight to
  Done. Off does **not** strip merge rights from Dev tokens — **branch
  protection** does that (`14` §2 zone C). Enable only with protection, a
  **reviewer token** (app-only formal approval), and eyes open. REVIEW always
  runs as a pipeline stage; which Dev Type staffs it is a performance choice
  (skills / identifying prompt), not a security control.
- **`memory_auto_merge`** (Settings → Policies, Memory card) — off
  (default): every note a Memory Curator proposes for a team-memory
  notebook becomes official only through **your** merge. On means two
  models in a row — a Curator wrote it and a Reviewer approved it; not a
  person, not the reviewer token. Same shape as `auto_merge`, same rule:
  flip it deliberately, and remember a wrong note guides every later run
  until you revert it (git keeps each merge attributable — ADR-0035).

## Config profiles — save and switch whole setups

Settings → **Profiles & Export** snapshots your entire saved setup — connections, repos,
Dev Types, prompt templates, limits, **and every stored secret value** — under
a name, and applies one back in a single click (ADR-0013):

1. Get a setup working, then **Save current as profile…** (e.g. `baseline`).
2. Reconfigure freely for a different task; save that as `docs-sprint`.
3. Switch back anytime with **Apply a profile** → the confirm shows exactly
   what changes (counts and names, never secret values) before anything
   applies. Applying is blocked while runs are active — pause intake and let
   them drain first.

Honesty rules worth knowing: profiles are snapshots, not live links — later
edits never update a profile (the row shows "settings changed since" when
you've drifted from the last-applied one); applying an old profile restores
its **old** secret values, and the preview warns when a live secret is newer
than the snapshot. Two different "skill" surfaces — do not conflate them:

- **Skill-source connection secrets** (`skill-*` tokens for configured skill
  source cards) **are** in the snapshot and **restore** with Apply (same
  path as `pmo-*` / `repo-*`; ADR-0011 / `docs/11`).
- The **skill store** (installed skill contents on disk), run history,
  internal repos (memory notebooks included), and `.env` are **never**
  touched by a profile.

Save-time `snapshot_warnings` still name gaps only for PMO and repo cards —
a skill source with a URL but no stored token may get no warning even though
tokens that *are* stored do ride the snapshot. Profile snapshots live on
`/data` and hold secret values — they are part of why backups are a password
export.

**Moving a setup to another install:** the same section's **Export…** writes
one bundle file — configs stay readable YAML; secrets and `.env` setup
values ride encrypted under a passphrase you choose (plaintext exists behind
a red warning; treat such a file like a password-manager export and delete
it after use). Tick "Embed skill contents" to carry custom skills along. On
the target: **Import…** → passphrase → review the preview → it lands as a
profile → apply it when ready. If the bundle carried setup values, download
the generated `.env`, review its HOST-SPECIFIC lines, place it at the repo
root, and `docker compose up -d`. Internal work repos (with their git
history and PRs) move separately via `scripts/backup_gitea.sh` /
`restore_gitea.sh`.

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
CLI args slot (optional, harness-specific — e.g. an ONBOARD `--max-turns 15`
triage budget on claude-code / grok-build; first-setup leaves the slot empty). When you're ready
to point DevCake at a real team: start `opt_in`, keep `auto_merge` off, label
one small mission, and expand from there.
