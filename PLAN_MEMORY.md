# Memory + Cron — implementation plan (draft 09)

> **Decision record: `docs/adr/0035`** — this file is the byte-frozen
> build contract behind it (agreed 2026-08-14; kept verbatim at the repo
> root for its ~40 in-code `PLAN_MEMORY §n` citations). Post-build
> amendments are marked inline. It retires into the pilot write-up once
> the §14–§15 receipts land in docs/16.

**Status:** AGREED by Felix, Grok, and Fable (countersign + F1′–F4
folded 2026-08-14). This is the build contract. If this file and
`consensus/MEMO.md` disagree on intent, fix this file. If they
disagree on a path, schema, label, or chokepoint, **this file
wins** and the memo must be updated.

**Implementer:** Grok. **Reviewer of the PR:** Fable, against this
plan and `consensus/draft-05.md`.

**Normative companions:** `consensus/MEMO.md` (why), 
`consensus/draft-05.md` (numbered), `admin/spa/DESIGN.md` (UI),
`docs/07-dev-runtime.md` (workspace), `docs/14-security.md` (trust),
ADR-0016 addendum, ADR-0024, ADR-0033, ADR-0034.

**Supersedes:** `plan/PILOT-PLAN-draft-01.md` (drafts 01–07). Leave
that file in git as history. Do not implement from it.

**Do not invent seams.** If a case is not named here, stop and ask.
Do not "just make it work" by reading note bodies, by special-casing
curation in `derive()`, or by aiming the Memory Curator at a product
board.

---

## 0. What this build is

A release-candidate slice that makes DevCake **memory-compatible**
and adds a **Cron module** whose first reserved job is Memory
Curator. It is not a memory product and not a job-runner framework.

End-to-end, once built:

1. Operator creates an empty notebook (or cards an external repo),
   writes a README, binds it board-bound and/or domain-bound.
2. Consumer runs mount `/workspace/memory/<card>/` read-only.
3. When those runs harvest discoveries, the app writes one file
   per claim under that notebook's `.claims/` (fast lane, no
   judgment).
4. On a timer, Cron opens a new EXECUTE ticket on each Curator
   board (a board whose only work repo is that notebook). N
   notebooks ⇒ up to N tickets per fire.
5. That ticket's Dev drains `.claims/` into notes via an
   ordinary PR (delete the drained files, add notes). A person
   merges (default), or two models merge if the operator turned
   `memory_auto_merge` on.

---

## 1. Locked decisions — do not relitigate in the PR

1. Bindings, not card types. No `kind: memory` field.
2. Derived usage chips. Nothing stored as a kind.
3. Skills stay `<card>/<skill>`. No dedicated skills ports.
4. Notes: the app never opens, parses, filters, or displays bodies.
5. `.claims/` is the only tree the app may read or write inside
   a notebook, and only as specified in §5. Notes never live
   under `.claims/`. The Curator must not edit `.claims/README.md`.
6. `CONTESTED.json` is **not** implemented. Do not create it. Do
   not mention it in shipped playbooks or UI.
7. `context_sourcing_strict` default true, covers skill **cards**
   and memory cards. Amends ADR-0016 addendum. Document it.
8. Reference / blocker / ONBOARD-sibling extras stay non-fatal.
   Memory mounts, when the knob is true, do not.
9. `memory_auto_merge` default false **enforces** the human merge
   at the existing merge chokepoint. ON is consent, two models,
   not a person, not the reviewer token.
10. Memory-bound card is never a work repo except on a Curator
    board (`repos == [M]`).
11. Each curation batch is a **new** ticket. No standing mission.
    No reply-reopen. Create with `DEVCAKE` + `DEVCAKE-EXECUTE`.
12. Cron is one verb: create a labeled ticket. Reserved row
    `memory-curator` cannot be deleted, cannot be aimed at a
    product PMO, entry locked to EXECUTE.
13. Curator inherit = consumer `repos ∪ reference_repos`. Not
    other memory notebooks. Not Dev Types. Extras, non-fatal.
    Never added to the Curator board's `repos`.
14. STEWARD **does** mount memory (same union). It still does
    not author discoveries (ADR-0033 D7). It is not the Curator.
15. No shipped Curator Dev Type, playbook, or skill.
16. No layout seed. Empty notebook. README is the operator's.
17. Missions never wait on curation. Scheduler is blind to
    claims depth except the Memory Curator's skip-if-empty.
18. Memory Curator **automatic** fires skip a notebook whose
    `.claims/` depth is 0. **Run now** does not skip (F4).
19. TDD: failing test first for each public seam below.
    Python 3.12. Always Works™: `./scripts/pytest_app.sh` or
    `PYTHONPATH=app` on 3.12, then bake `app` (and `images` /
    `admin` when those trees change).

---

## 2. Config schema

All new fields ride the settings bundle (ADR-0013). Round-trip
tests are mandatory.

### 2.1 `DevType`

```
memory_repos: list[str] = []
```

Card names. Same shape as existing repo-card names / external
skill prefixes. Deduped, order preserved. Empty default.

### 2.2 `PMOInstance` (a board)

```
memory_repos: list[str] = []
```

Same shape. Empty default. `managed` boards may carry this list;
it is operator-owned, like `repos` / `reference_repos`.

### 2.3 `AppConfig`

```
context_sourcing_strict: bool = True
memory_auto_merge: bool = False
crons: list[CronJob] = [<reserved memory-curator row>]
```

Reconcile the reserved row the way `reconcile_managed_pmos` keeps
the default board: a PUT that omits it must not delete it. A stray
`reserved: true` on any other incoming row is stripped.

### 2.4 `CronJob`

```
id: str                    # stable; reserved id is "memory-curator"
name: str
enabled: bool = False
interval_minutes: int = 60 # ge=1
pmo: str | None = None     # instance name; REQUIRED for non-reserved
entry_stage: Literal["ONBOARD","PLAN","EXECUTE","REVIEW"]
description_template: str  # becomes the ticket description
reserved: bool = False     # only the seed row may be true
```

Validation:

- `id` matches `^[a-z0-name...]` — use the same conservative slug
  regex as other config names (Dev Type / instance). Refuse `/`.
- IDs unique in the list.
- Non-reserved: `pmo` must name an existing instance. `entry_stage`
  is free.
- Reserved `memory-curator`: `reserved` is true and sticky;
  `entry_stage` is always `EXECUTE` (canonicalize on ingest);
  `pmo` must be `None` (canonicalize / refuse a value — this row
  does not pick a product board); `enabled` and
  `interval_minutes` and `description_template` are operator-owned;
  DELETE of this id is 409.

Seed description template for `memory-curator` (operator may
edit):

```
Drain .claims/ in the work repository. Each *.json file is one
unvalidated lead (finding / evidence / scope) with origin ids.
Promote a lead to a note only when the notebook's own README
filing rules and the evidence support it; otherwise discard.
Every change is a pull request. Delete drained files from
.claims/ in the same PR. Do not invent a layout. Do not write
notes under .claims/. Do not edit .claims/README.md. Do not
open a PR against any inherited extra clone.
```

### 2.5 Cross-field invariants (one validator, one test module)

Call this `validate_memory_bindings(cfg)` from the same place
`repos` ∩ `reference_repos` is checked today
(`config.py`).

**I1 — within one instance, three lists are pairwise disjoint:**
`repos`, `reference_repos`, `memory_repos`.

**I2 — the Curator-board invariant (Felix, critical):**
Let `M` be the set of every card name that appears in any
instance's `memory_repos` or any Dev Type's `memory_repos`.
For every instance `I` and every `m ∈ M`:
either `m ∉ I.repos`, or `I.repos == [m]`.

Refuse a save that would make `m` one work repo among others on
a product board. Refuse a save that would put `m` in both
`I.repos` and `I.memory_repos`.

**I3 — pickers only offer existing cards.** Dangling names can
still appear from hand-edited YAML; §5.3 handles them at run
time. UI never offers a deleted card.

**I4 — deleting a card** that still appears in any work /
reference / memory / skill-source binding warns with the derived
usage counts. Follow the existing unused-repos / delete-guard
pattern; do not silently break bindings.

**I5 — subfolder bindings** (`card/path/`) are **out of scope**.
v1 is card-granular. Do not parse slashes in `memory_repos`
entries.

---

## 3. Workspace and sourcing

### 3.1 Path

Consumer memory clones:

```
/workspace/memory/<card>/
```

Sibling of `repo/` and `activity/`. **Not**
`/workspace/repo/memory/<card>/`. The entrypoint and docs/07 §1
must gain this directory. That is an **images** lockstep bake
(`docker buildx bake images` or `all`) with the app.

`<card>` is the repo-card name. If a card name would be an
unsafe path segment (`..`, `/`, empty), refuse at validation
(I3/I5) so the entrypoint never sees it.

On a **Curator** run the notebook is the **primary work clone**
at `/workspace/repo/<slug>/` (today's work-repo layout). Do not
also mount it under `/workspace/memory/`. Inherited consumer
trees land as extras (see §3.4 and §7).

### 3.2 Which runs mount memory

Union, deduped, order = instance list then Dev Type list:

```
mounts = (instance.memory_repos ∪ dev_type.memory_repos)
         minus {run.repo_ref}
```

**F2:** the union always excludes the run's primary work repo.
A Curator Dev Type that lists the notebook domain-bound must
not produce a second clone under `/workspace/memory/`. Add a
test. I2 making the instance list empty is not enough.

Apply for `ONBOARD`, `PLAN`, `EXECUTE`, `REVIEW`, **and
`STEWARD`**. Steward works discoveries (routing, not
authorship). It gets the same consult-optional notebook as
everyone else: instance list ∪ the staffed Steward Dev Type's
list, minus `repo_ref`. Do not add a per-stage knob. Do not
treat this as a third write lane.

A Curator EXECUTE on a Curator board has `instance.memory_repos`
empty (I2) and `repo_ref == m`. It does not receive a consumer
memory mount of its own notebook.

### 3.3 ADR-0034 chokepoint

Extend **`repo_sourcing.sourced_repo_names`** (or a sibling it
calls, with a parametrized test that gate-set == mount-set) so
the ADR-0024 `needed_for` union includes every memory card the
run will mount, plus every inherited extra name on a Curator
run (§7).

Do **not** write a second needed-set. Skill-source cards stay on
`skill_source_cards` as today; they are not cloned into
`/workspace`.

### 3.4 Extra-clone dest

Today `clone_extra_repos` writes to `repo_dir / slug`
(`/workspace/repo/<slug>/`). That remains for reference,
blockers, ONBOARD siblings, and Curator-inherited trees.

Memory **consumer** mounts do **not** go through that dest. They
go to `workspace/memory/<card>/`. Give `clone_extra_repos` a
destination override **or** add `clone_memory_repos` next to it
with the same mirror/askpass/LFS behavior, fail-closed when
`context_sourcing_strict` is true.

Inherited Curator extras use the **existing** extra path and the
**existing** non-fatal rule.

### 3.5 Failure class (`context_sourcing_strict`)

Implement in the **dispatch/provision** path, not as an exit-11
inside the harness.

**True (default):**

- Memory card dangling or `ensure_fresh` fails ⇒ do not start
  the run. Classify with the ADR-0025 workspace / provisioning
  family (same honesty as a missing workspace). Never as
  `DEV_BAD_OUTPUT`. Never increment a Dev-type attempt that
  looks like a dumb agent.
- **Amended 2026-08-14 (review R1):** the mirror gate only sees
  mirror-eligible cards, so this rule has two halves. Dispatch
  resolves every mount — a dangling binding or an
  uncredentialed internal-forge card defers via the existing
  blocked-reasons path. Each runspec memory entry snapshots a
  `strict` flag at dispatch; the **provision step** makes a
  strict mount's clone failure fatal through the existing
  exit-13 forge family (`clone_failed`), the same class and
  counting as the primary clone. Instance `memory_repos` names
  are existence-checked at validation like `repos`.
- Skill-source card in the needed-set fails the same way
  (today's fail-closed, now toggle-governed).

**False:**

- Last-good mirror present ⇒ clone that sha, stamp
  `stale_cache: true` on that mount's provenance, warn.
- Mirror has never held the repo ⇒ omit that mount, warn, run
  continues.
- Same rule for skill-source cards (this is the ADR-0016
  amendment: skills may now fail open).

**Unchanged:** a selected skill whose **files** cannot be read
after a passed gate still warns and drops that skill (additive
payload). A reference/blocker/ONBOARD extra clone still
non-fatal.

### 3.6 Provenance on the Run / runspec

Per memory mount:

```
card: str
binding: "board" | "domain" | "both"
commit: str
stale_cache: bool
path: "/workspace/memory/<card>"
```

Snapshot at dispatch. A notebook added to config mid-flight must
not appear. Same spirit as `run.mirror_repos`.

Curator inherit extras appear in `extra_repos` as today (name,
url / mirror_path). They are not memory mounts.

### 3.7 Dispatch sentence (D1, amended)

When `len(memory_mounts) > 0`, append exactly:

```
Memory notebooks are mounted read-only under /workspace/memory/.
Everything outside the .claims/ folder is a curated note. Files
under .claims/ are unvalidated leads copied from runs. Leads may
contradict notes. Check both. Trust neither blindly.
```

Append in the same mechanical place as
`append_required_skills`. No sentence when there are no consumer
memory mounts. Curator runs (no consumer mounts) do not get this
sentence; their ticket description is the cron template.

---

## 4. Write path for notes (slow lane)

Consumer Devs: read-only tokens on memory mounts. INV-6 still
says work happens only in the primary work clone. Playbooks
already forbid writing extras; restated for `/workspace/memory/`.

The only Dev that writes a notebook is one whose **primary**
`repo_ref` **is** that card — i.e. a Curator-board run.

PR + REVIEW + merge chokepoint: existing pipeline. Do not add a
mission type. Do not add a Steward duty. Staff EXECUTE on the
Curator board with the operator's Curator Dev Type via the
existing assignment table (ADR-0019 override on that instance).

### 4.1 `memory_auto_merge`

At the **existing** merge-completion chokepoint (the one that
honors `RepoInstance.auto_merge` today — `review.py` /
`sweeps.py`):

```
if target_repo is memory-bound anywhere at merge time
   and not cfg.memory_auto_merge:
    do not auto-merge
    leave the mission in the human-await / DEVCAKE-MERGE state
```

"Memory-bound anywhere" = the card is in any instance
`memory_repos` or any Dev Type `memory_repos` **or** is the sole
work repo of a Curator board (I2 second branch). Use a single
helper `is_memory_bound(cfg, name) -> bool`.

When `memory_auto_merge` is true, existing card `auto_merge` +
REVIEW-approved path runs unchanged. The proposer Dev Type must
not be the REVIEW Dev Type on that board — that is operator
staffing, not a kernel check, but the modal and the Curator-board
setup copy must say it.

OFF→ON requires `Modal.jsx` (DESIGN.md). Copy (do not soften):

> Recommended: keep this off so a person merges every note.
> With this on, a note the Curator wrote becomes official once
> another Dev (a Reviewer) approves it. That is two models in a
> row. It is not a person. It is not the reviewer token. A wrong
> note can guide every later run until you revert it. Everything
> stays in git: every merge names its run and can be reverted.

Turning OFF never prompts.

App commits under `.claims/` do **not** go through this gate.

---

## 5. `.claims/` conveyor (fast lane) — this build

F1 (Fable) + Felix 2026-08-14: a single `CLAIMS.json` would
merge-conflict every time a harvest landed during a drain PR.
ADR-0020's conflict route-back is **inert while `auto_merge` is
OFF**, and `memory_auto_merge` OFF is the default — so the
single-file design would park JSON-array conflicts on a human
exactly on the busiest notebooks. Per-entry files eliminate
the class. Do not implement a single shared JSON array.

### 5.1 Layout

Claims live under **`.claims/`** at the notebook root. They
never sit in the root or in a high-visibility folder.

- `.claims/<id>.json` — one file per claim. Schema
  `devcake-claims/v1`. `<id>` is the deterministic hash of
  `(source_instance, source_pmo_id, step, index)` — safe path
  segment, dedup = file exists, re-harvest is idempotent
  (amended 2026-08-14, review N6: the instance joins the hash so
  two boards on different forges with colliding numeric pmo ids
  cannot dedup each other's claims on a shared notebook).
- `.claims/README.md` — app-written **create-if-missing**
  only. Carries the leads-not-truths framing verbatim. The
  app does not rewrite it after it exists. The Curator does
  not edit it. Notes never live under `.claims/`.

One entry per file (same fields as the withdrawn array
element): `id`, `source_instance`, `source_key`,
`source_pmo_id`, `step`, `index`, `run_id`, `finding`,
`evidence`, `scope`, `about` (default `[]`; fill only from
structured discovery fields, never by parsing prose),
`harvested_at`.

No `rationale`. No `replacement`. No `status: contested`.

Append = create a file. Drain = delete files (+ write notes
elsewhere). Clear prune = delete matching files. None of
these merge-conflict with each other.

Depth = count of `.claims/*.json` (README excluded).

Dot-folder visibility is intended (wikis skip it). Devs find
claims because the mount sentence **names** `.claims/`, not
by stumbling on it.

### 5.2 When

At the existing discovery **harvest** chokepoint (finalize,
after entries are validated and memorialized on the source
feed, alongside the `DEVCAKE-DISCOVERY` label). Authorship
unchanged: ONBOARD, EXECUTE, REVIEW. PLAN does not author.
STEWARD does not author.

For each harvested entry, create `.claims/<id>.json` on
**every memory card listed on that run's dispatch snapshot**
(§3.6), excluding a card that is the run's own `repo_ref`.
Not live config. If the snapshot is empty, do nothing.

This **dissolves** promote-to-memory routing as a judgment
hop. The Curator sorts. Do not ask STEWARD to pick a
notebook.

### 5.3 How the app writes

Use the app's forge credentials, **never** a mission Dev
token.

- **Amended 2026-08-14 (review R5, replaces the Contents-API
  instruction):** ONE forge-neutral writer for every registered
  forge — shallow clone + commit + push with the card's
  **write** token via askpass (never a mission Dev token, never
  `GITEA_ADMIN_*`). Touch **only** paths under `.claims/`.
  Accepted costs, named: two checkouts per notebook per harvest
  (one listing+README snapshot, one commit); a push race
  (another commit landing inside the window) is retried ONCE by
  replaying the whole cycle onto the fresh head —
  creates/deletes are idempotent — and a second failure is
  audited, never silent.
- If the card is `reference_only`, skip that notebook, audit
  loudly, do not fail the discovering run.

Algorithm (`claims.append_from_harvest`):

1. If `.claims/README.md` is missing, include it in this
   commit (create-if-missing). Do not overwrite an existing
   README.
2. Compute `id`. If `.claims/<id>.json` already exists,
   skip (dedup).
3. Caps **before** write:
   - per harvest: do not exceed
     `budgets.discoveries_per_run` (already applied at
     harvest).
   - per notebook: `budgets.claims_queue_max` (new,
     default 50, `0` = unlimited). Count `*.json` files.
     At cap: refuse the new id, audit
     `claims_queue_capped`. Do **not** evict old claims
     (that would be the app editing memory on its own
     judgment).
4. Commit message: `devcake:claims:v1 run=<id> n=<k>`.
5. Refresh advisory `claims_depth[card] =` json-file
   count. The directory listing is the truth; the count
   is a cache. After restart, the next append or a
   list-only read of `.claims/` (F3) refreshes it.

Failure of any step: log + audit, **do not** fail the
discovering run, **do not** withhold feed memorialization.

### 5.4 Clear

Clear is the all-boards operator wipe, so (amended 2026-08-14,
review N7):

- For every notebook the app can write, delete **every**
  `.claims/*.json` — all origin boards are being wiped, and
  orphan claims from boards deleted before the Clear have no
  owner left to match on.
- Leave `.claims/README.md`.
- **Do not** touch any path outside `.claims/`.
- Confirm copy adds: claims copied from this board's
  runs are removed from notebooks; notes stay.

A full stack wipe is a different operation (Gitea backup).

### 5.5 What the SPA may show (F3)

Queue **depth** per card: from the conveyor cache **or**
from listing `.claims/*.json` filenames. Never from
reading note bodies. Never from rendering finding text.
Not a claims browser. Depth is a number. C10's exception
list includes this listing.

---

## 6. Cron module

### 6.1 Placement

New Config section — **named "Scheduled Tasks"**
(`#/config/scheduled-tasks`; founder ruling 2026-08-14: never
"Cron" in the UI; the config field stays `crons` and the API
stays `/api/v1/cron`). It consolidates the Relations Steward
controls (from Traffic control, which dissolves into Limits)
and segregates **DevCake tasks** (Steward + Memory Curator,
built-in, never deletable) from **Custom tasks** (operator
rows) as two cards. One section per view
(DESIGN.md). `useSharedDraft()` for the list. `SettingRow` for
scalars. Table for the rows, styled like the Runs table.

Reserved row always visible. Delete control disabled on it
(not a one-item MoreMenu of nothing — disable/hide Delete;
edit template/interval/enabled remain).

"Run now" per row is an **instant** POST
(`POST /api/v1/cron/{id}/run`), like Steward "Run now".
Enabled/interval ride the draft until Save, **or** InstantZone
if you follow the Steward card exactly. Prefer: list edits are
draft; Run now is instant. State that in the PR.

Native `window.confirm` is banned. Disable/delete of a
non-reserved row uses `Modal.jsx`.

### 6.2 What a fire does (generic row)

Single-flight lock per `id` (and, for `memory-curator`, per
target board as well). If a previous cron-created ticket for
this row is still non-terminal on the target board (see marker),
skip.

Honor **global** `intake_paused` and the **target instance's**
`intake_paused`. Count created work toward `global_max` only
when the resulting run dispatches (existing scheduler).

Create a mission on `row.pmo` via the existing `create_mission`
PMO port:

- title: `[cron:{id}] {date}` or the first line of the
  template if you prefer — pick one, test it, do not invent
  per-row.
- description: the template, with `{timestamp}` interpolated
  if present. No other interpolations in v1.
- labels: `DEVCAKE` (opt-in adoption) **plus** the stage
  label for `entry_stage` unless `entry_stage == ONBOARD`.
  There is **no** `DEVCAKE-ONBOARD`.
- footer / last line of description must include
  `` `devcake:cron:v1 job=<id>` `` so a later fire can find
  in-flight work. Use the existing feed/description marker
  discipline (defang if the template contains backticks).

Do not set status to `in_progress`. Leave it `backlog` so
`derive()` can see the stage label (rows 2–4). ONBOARD-entry
tickets have no stage label (row 1).

Degradation: if the last 3 automatic fires for this row
produced no ticket and no successful skip-reason (paused /
in-flight / empty-queue), mark `cron_degraded[id]` on `/health`
and stop automatic fires. Run now still works; a successful
fire clears it. **Amended 2026-08-14 (review R4):** cron fires
create tickets, not runs, so "like Steward" needs its own
ledger — `state/cron_outcomes.json` (atomic writes) records one
outcome per automatic fire window plus `last_fire_at`; the
schedule is elapsed-time off that stamp (restart-safe, one
attempt per window), replacing the minute-multiple watermark.

### 6.3 Reserved row `memory-curator`

On each automatic or Run-now fire:

1. Compute `M` = all memory-bound card names (same set as I2).
2. For each `m ∈ M`, find instances `I` with `I.repos == [m]`.
   Those are Curator boards for `m`. If none, health-warn
   `memory_curator_no_board:<m>` and continue.
3. For each such `I`:
   - If this is an **automatic** fire and
     `claims_depth[m] == 0` (confirmed by listing
     `.claims/*.json`), **skip** this board.
   - **F4:** Run-now **bypasses** skip-if-empty. An explicit
     click is its own justification (tidy / compaction pass).
     UI copy on Run now may say so; do not silently no-op.
   - If an in-flight cron ticket for this job already exists
     on `I`, skip.
   - Else create a ticket on `I` as §6.2, ignoring `row.pmo`,
     `entry_stage` forced `EXECUTE`.

Never create a Memory Curator ticket on a board that fails I2.

**Cardinality (Felix):** curation is per-notebook. Each
memory-bound card `m` is curated by its own Curator board —
I2 makes that board single-notebook (`repos == [m]`). One
reserved row serves all notebooks: each fire fans out to
**up to one ticket per memory-bound card**. A deployment
with N bound notebooks and N Curator boards runs up to N
curation pipelines in parallel, each independently subject
to skip-if-empty (automatic only) and per-board
single-flight. A bound notebook with no Curator board is a
standing health warning (`memory_curator_no_board:<m>`),
never a silent skip.

Those N pipelines share `global_max`. That is operator
cost, not a reason to serialize in v1. Document it on the
Cron card.

### 6.4 Service wiring

A `CronService` next to `StewardService`, same poll-loop
cadence opportunity (do not add a new process). Composition
root: `api/services.build_services()`. `main.py` stays wiring
+ thin forwards. New routes:

- `GET /api/v1/cron` — list (from live config)
- `POST /api/v1/cron/{id}/run` — 422 unknown / reserved-ok;
  409 in-flight (generic rows); 200 `{created: [{pmo, key}]}`.
  The reserved row's fan-out returns 200 with an empty
  `created` when every board is busy/paused/absent — the SPA
  message explains it (amended 2026-08-14, review N4).

No new attributes bound onto `MissionManager` after class
body (ADR-0015 / structure guards).

---

## 7. Curator inherit

When dispatching a run whose `repo_ref == m` and `m` is
memory-bound (I2 second branch):

```
inherited = []
for each instance J != this instance:
    if m in J.memory_repos:
        inherited += J.repos
        inherited += J.reference_repos
dedup, drop m, drop names not in cfg.repos (cards)
```

Also include **this** instance's `reference_repos` if any
(usually empty on a Curator board).

These names join `sourced_repo_names` / `extra_repos` as
ordinary extras. Non-fatal clone. Dest:
`/workspace/repo/<slug>/` (existing extra layout).

Playbook / identifying prompt (operator's) must say inherited
trees are read-only. Do not put them in `I.repos`.

Same-instance product boards are the usual case: CS board
`reference_repos` holds the codebase; Curator board is a
different instance; inherit pulls those names. If someone
runs Curator and consumer on one instance, I2 forbids it
(cannot have `m` as sole work repo and also list product
repos). Two instances is the design.

Cap: **none below what those boards already mount.** If the
union is huge, that is the operator's existing extra-clone
cost. Log the count.

---

## 8. Auto-provision a notebook

Extend the existing `POST` that backs
`internal_repos_service.create_internal_repo` /
`create_operator_repo`. New SPA dialog, one flow:

1. Name (same regex as card names; reject `activity-*` and
   names that fail I5).
2. Create empty repo in `devcake-repos` (existing operator
   org). Create the card. Mint tokens as today.
3. Optional bindings in the same dialog: add name to chosen
   instance `memory_repos` and/or chosen Dev Type
   `memory_repos`. Run I1–I2 before save.
4. Do **not** write a notebook README. Do **not** create
   `.claims/` (the conveyor creates it on first harvest).
5. Optional "copy from template" only if the operator names
   an existing card or URL. DevCake ships no template.
6. Dialog copy, required:

> This repository is yours. Clear will not delete it. A full
> stack wipe will, unless you use the Gitea backup. After
> Clear, claims copied from the wiped board are removed from
> this notebook; notes stay.

Tests: name never `activity-*`; Clear does not sweep it;
result is indistinguishable from a hand-carded external
card (no `memory: true` flag).

---

## 9. UI surfaces (DESIGN.md is mandatory)

Read `admin/spa/DESIGN.md` before touching the SPA. Tokens
only. `SettingRow` for scalars. Tables like Runs. `Modal.jsx`.
Draft via `useConfigDraft`. Instant only inside `InstantZone`
or an existing instant POST.

| Surface | What |
|---|---|
| Dev Type editor | "Memory (domain-bound)" card picker **under Skills**. First repo surface on a Dev Type. Match the Skills card-picker pattern. |
| Board / PMO page | "Memory (board-bound)" list **beside** Reference repos, not inside it. |
| Repos page | Derived chips: work / reference / skills-source / memory board-bound ×N / memory domain-bound ×N. Memory-used cards sort first in memory pickers. Delete warns with usages. Queue depth number if known. |
| Run view | Memory mounts: card, binding badge, commit, stale marker. Not file contents. |
| Create notebook | §8 dialog. |
| Config → Scheduled Tasks | §6.1 (DevCake tasks / Custom tasks split). |
| Config (limits or a SettingRow near traffic) | `context_sourcing_strict` — copy must say what **off** means (runs proceed on cached content). |
| Config | `memory_auto_merge` + OFF→ON modal (§4.1). |
| Health | `cron_degraded`, `memory_curator_no_board`, `claims_queue_capped`. Not `steward_degraded` (that stays on the Steward card). |

Mobile: Config chip row must include Cron. Check the section
at both desktop and a 390-wide viewport.

---

## 10. Docs that must move in the same change

Zero-drift with public seams:

- `docs/02-domain-model.md` — DevType.memory_repos,
  pmos[].memory_repos, the two toggles, CronJob, I2.
- `docs/07-dev-runtime.md` §1 — `/workspace/memory/`.
- `docs/08-harness-templates.md` — mount sentence; no
  harness-native memory dir.
- `docs/09-messaging.md` — runspec memory mounts /
  provenance.
- `docs/11-admin-panel.md` — new endpoints and surfaces.
- `docs/14-security.md` — memory RO extras; claims write
  uses app credentials; Clear vs notes; reviewer-token
  honesty for `memory_auto_merge`.
- `docs/03-mission-lifecycle.md` — harvest also appends
  claims; Cron-created tickets; no CONTESTED.
- ADR-0016 addendum note: skill-source fail-closed is now
  toggle-governed.
- ADR-0034: sourcing chokepoint includes memory + inherit.
- `docs/16-roadmap.md` — shipped when the pilot has
  receipts, not at bake.

Do not claim a stronger security posture than docs/14.

---

## 11. Tests (minimum; add as you hit seams)

House style: `app/tests/test_*.py`, public seams, independent
expected values, no private-helper assertion.

- Config: shapes, I1, I2 (product board cannot list M as one
  of several work repos; Curator board `repos == [M]` ok;
  M not in that board's memory_repos), reserved cron
  reconcile, bundle round-trip.
- Sourcing: union, dedupe, STEWARD included, ONBOARD included,
  gate-set == mount-set (ADR-0034 parametrized).
- `context_sourcing_strict` both sides; provisioning-class
  vs Dev failure; stale_cache marker; never-synced skip;
  extras still non-fatal.
- Inherit: CS-shaped fixture (codebase only in
  `reference_repos`) appears on Curator extras; other
  memory notebooks do not; `m` itself does not.
- Claims: create `.claims/<id>.json` from harvest
  snapshot; dedup by file-exists; cap by file count;
  README create-if-missing only; write failure does not
  fail finalize; Clear deletes only matching claim
  files; app does not write outside `.claims/`; drain
  deletes do not conflict with concurrent creates
  (two-file fixture).
- Mount union excludes `run.repo_ref` (F2).
- Run-now on memory-curator creates a ticket even when
  depth is 0 (F4); automatic fire does not.
- Merge guard: memory-bound PR not auto-merged when
  toggle off; is auto-merged when on and card says so.
- Cron: ONBOARD labeling has no `DEVCAKE-ONBOARD`;
  EXECUTE labeling; single-flight; pause; memory-curator
  skip on empty queue; memory-curator refuses product
  PMO; cannot delete reserved row.
- Auto-provision: empty, not `activity-*`, not swept.
- Structure guards still pass (no domain→adapters, no
  new main.py bodies, no new MissionManager attributes
  after class body, no bare checkpoint literals).
- SPA contract / UI-suite for every surface in §9.

---

## 12. Explicit non-goals

- Promote-to-memory STEWARD judgment / family routing of
  claims.
- `CONTESTED.json` or any contest stamp.
- Harness memory-dir install or harvest.
- `memory_inject`.
- App reads of note bodies (SPA included).
- Shipped Curator Dev Type / playbook / skill / layout.
- Job kinds other than create-ticket.
- Reply-reopen of done tickets.
- Blocking product missions on curation.
- Per-binding failure-class overrides.
- Subfolder bindings.
- Evicting old claims to make room (v1 cap = refuse new).
- Fixing MCP-setup fail-closed (exit 14) for optional
  tools.

---

## 13. Implementation order (one seam at a time)

TDD each slice. Do not bulk-write the suite then implement.

1. Schema + I1 + I2 + reserved cron reconcile + bundle.
2. Sourcing + consumer mount path + fail-class + provenance
   + dispatch sentence. Bake images with the entrypoint
   dest. Tests green.
3. `memory_auto_merge` chokepoint + modal.
4. Create-notebook dialog.
5. CLAIMS conveyor + Clear prune + depth cache.
6. Inherit extras on Curator runs.
7. CronService + generic fire + reserved Memory Curator.
8. SPA surfaces + health.
9. Docs in the same change.
10. `./scripts/pytest_app.sh` (or 3.12 `PYTHONPATH=app`)
    **and** `docker buildx bake app` (+ `admin`, `images`
    as touched). Do not claim done from an old
    `devcake/app-test` image.

---

## 14. Setup on the throwaway box (Felix, after bake)

1. Create notebook via §8. Write a README (layout policy).
2. Bind it board-bound on the **product** pilot board
   (CS or eng). Do **not** put it in that board's work
   list.
3. Create a **second** PMO instance: the Curator board.
   Work list = `[that card]` only. Assignments: EXECUTE →
   a Dev Type you create (paste the sample in
   `grok/06-round3-opening.md`, edited to say drain
   `.claims/`). REVIEW → judgment (or whatever you
   staff). Turn Memory Curator cron on. Set interval.
4. Optionally bind a second small notebook domain-bound
   on one Dev Type to exercise the union.
5. Write one tacit note by hand (C12).
6. Enable Memory Curator. Leave `memory_auto_merge` off
   unless you are testing the modal path on purpose.
7. Run the A/B.

---

## 15. A/B (pre-registered — do not improvise metrics)

Same board, same Dev Type, same product repos.

- **Arm A:** memory bindings on; claims conveyor live;
  Curator cron on.
- **Arm B:** no memory bindings (so no mounts and no
  claims).

`context_sourcing_strict` default on so arm A cannot
silently run memoryless.

Metrics, fixed now:

- Re-discovery of facts already in **notes**.
- Consultation of note paths, from receipts (transcript /
  tool args), never self-report.
- Consultation of `.claims/`, from receipts.
- Whether claims are drained or the queue only grows.
- Queue depth over time.
- REVIEW pass rate; tokens; wall time.
- Merge lag, additions vs corrections (notes).
- Staleness-window incidents: a consumer run whose
  mounted commit does not include an in-flight Curator
  PR that would have changed a path it opened.
- Inherit check: on the CS-shaped setup, the Curator
  workspace actually contains the reference code trees.

Primary success: consultation of notes happens **and**
re-discovery drops. Claims-seen and claims-drained are
reported honestly even if they fail. Write-up in
docs/16 style, whatever they show.

---

## 16. Sample Curator identifying prompt

Not shipped. Operator may paste. Logistics only:

> You tend a notebook. The notebook is the work
> repository. The .claims/ folder is a queue of
> unvalidated leads the app copied from other runs.
> Product trees cloned beside the notebook are
> read-only evidence. Drain the queue: promote a lead
> to a note only with evidence, or discard it. Every
> change is one pull request that also deletes the
> drained .claims/*.json files. Do not put notes under
> .claims/. Do not edit .claims/README.md. If the
> notebook has a README filing rule, follow it. If it
> does not, add the fewest files needed. Do not invent
> a house style. Do not open a PR against an extra
> clone. Do exactly what this mission asks.

---

## 17. Rulings log

- **D1** — factual mount sentence. Amended 2026-08-14 to
  name `.claims/`, not `CONTESTED.json` / `CLAIMS.json`.
- **D2** — mount wherever bindings apply, **including
  STEWARD**; auto-provision built. Steward reads; it does
  not author discoveries and does not write the notebook.
- **D3** — within-instance disjointness. **I2 added**
  (memory never a product work repo).
- **D4** — dissolved (cards are global).
- **D5** — `context_sourcing_strict` default true;
  skills + memory; Felix wants the unification.
- **D6** — product missions never block on curation.
- **D7** — `memory_auto_merge` default off (enforces
  C4); ON = consent; copy = two models.
- **D8** — claims queue **adopted** into this build
  (Felix 2026-08-14). CONTESTED withdrawn. Layout is
  `.claims/<id>.json` (Fable F1′ + Felix). Clear
  prunes claim files only.
- **F2** — mount union excludes `run.repo_ref`.
- **F3** — SPA depth may list `.claims/` names (C10).
- **F4** — Run-now bypasses skip-if-empty.
- **D9** — Cron module, one verb; reserved Memory
  Curator; no product PMO on that row.
- **D10** — inherit `repos ∪ reference_repos` (CS
  case). No extra cap of 8.
- **D11** — each batch is a new EXECUTE-labeled
  ticket. No standing mission. No reply-reopen.
- **D12** — ADR-0016 addendum: skill-source
  fail-closed is now the true side of the same knob.

---

## 18. Definition of done for the implementer

You may say this slice is implemented only when:

- Every §11 test exists and was seen green on **this**
  tree (rebaked `app-test` or `PYTHONPATH=app` on 3.12).
- `docker buildx bake` of every touched target succeeded
  (`app`, and `admin` / `images` if those trees changed).
- Docs in §10 match the code.
- A human can click through: create notebook, bind it,
  see a consumer run mount it, see a harvest grow
  `.claims/`, fire Memory Curator (or wait), see an
  EXECUTE ticket on the Curator board, merge a drain PR,
  see depth drop — or you have named exactly which of
  those steps you could not run on this box.

"Build succeeded" is not done.
