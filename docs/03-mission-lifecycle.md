# 03 — Mission Lifecycle: The Four Playbooks

> **Audience:** implementers and prompt authors. This document owns the canonical prompt texts and the `result.json` contract.
> **Depends on:** `02-domain-model.md` (state machine, labels), `04-orchestrator.md` (finalization), `07-dev-runtime.md` (workspace).

Each Mission Type has a **playbook**: what the Dev receives, what it must do inside the workspace, the structured output it must produce, and the app-side finalization that follows. Common to all four:

- **Inputs:** `/workspace/repo` (fresh clone), `/workspace/activity/` (MISSION.md brief + ACTIVITY.md feed mirror + attachments, ADR-0014), the Dev Type's identifying prompt, and the playbook prompt (§7).
- **Output:** `/workspace/out/result.json` (§6) — the app finalizes **from this file plus the exit code, never by parsing prose**.
- **Finalization (app-side, always):** post the step comment (the Dev's last message, `>`-blockquoted) with the `{seq}_{TYPE}.md` full-session transcript attached (ADR-0014) → post token report (§8) → compare-and-transition (`04-orchestrator.md` §4) → forge side effects if any. **Exception:** on REVIEW-approve, the merge precedes the Done transition (§4.1) — Done must never overstate the repository.
- **Failure:** nonzero exit or invalid `result.json` ⇒ no PMO transition; the Mission's label is untouched and it reschedules (attempt counting per `15-errors-and-retries.md`).

## 1. ONBOARD

**Goal:** assess a previously-untouched Mission's complexity and route it.

The Dev studies the Mission against the actual codebase and classifies it using this rubric:

| Verdict | Criteria | Expectation |
|---|---|---|
| `trivial` | The Dev is confident it can complete the work in a single short session: localized change (typically ≤ ~2 files), no design ambiguity, no migration/infra impact, obvious verification. | Rare. |
| `normal` | Definable piece of work needing a plan first. | **Most Missions.** |
| `high` | Too large/compound for one plan–execute–review cycle; naturally splits into independent work items. | Rare. |

### 1.1 Trivial path
**Founder decision 2026-07-18 (rode ADR-0014's PR):** ONBOARD never implements — it holds no write token whenever the recommended RO PAT is configured (per-stage least privilege, `14-security.md` §3/§8), and the old self-executing trivial path only ever worked on repo cards without one. Trivial is now the degenerate case of the opportunistic plan (§1.2): the Dev writes the short exact plan to `/workspace/out/PLAN.md` and returns `outcome: "plan_needed"`; finalization takes the plan-attach path and the mission jumps straight to EXECUTE — the only stage that can push. `executed_trivially` is removed outright (preproduction, no deprecation window); a stray one parks with `DEVCAKE-SKIP` like any illegal outcome. REVIEW still always stands between any DevCake-written code and Done/merge — unchanged.

### 1.2 Normal path
No code changes. `result.json`: `outcome: "plan_needed"` with a one-paragraph `summary` of the assessment.

**Bounded effort:** ONBOARD is a triage pass, not an exploration — the playbook prompt says "assess, don't deep-dive," and operators can cap it mechanically via the per-Mission-Type extra CLI args (`02-domain-model.md` §9; the seeded default gives ONBOARD `--max-turns 15` on the claude-code harness — admin-editable data, never hardcoded).

**Opportunistic plan:** if, in the course of assessing, the Dev has already effectively formed the complete plan, it may write it to `/workspace/out/PLAN.md`. This is optional and confidence-gated — never forced; assessment and planning remain separate jobs by default.

**Finalization:** transcript + token report → if a `PLAN.md` was attached: upload it as `PLAN_{seq}.md` to the activity feed and add `DEVCAKE-EXECUTE` (the PLAN step is skipped — its work already exists); otherwise add `DEVCAKE-PLAN`.

### 1.3 High-complexity path (decomposition)
No code changes. The Dev emits a **decomposition manifest** in `result.json` (`outcome: "decomposed"`, `decomposition: [{title, description, priority, …}, …]` — entry schema in `02-domain-model.md` §11), observing:

- **Standalone rule:** every child description must read as an independent Mission. Never "Review the work done in this Mission"; instead "Review all work recently done in connection with the creation of feature XYZ". No cross-references between siblings.
- **Explicit priority** on every child (required field).
- **Ordering (`blocked_by`):** each draft may declare `blocked_by`: 1-based indexes of **earlier** drafts it must not start before (`02-domain-model.md` §11). The playbook instructs the Dev to order parts prerequisites-first and to declare an edge whenever one part consumes another's output (implementation after documentation/design, etc.); independent parts omit it so they run in parallel. The app validates earlier-only (violation ⇒ `DEV_BAD_OUTPUT`, a counted attempt) — which structurally prevents cycles — and creates the corresponding native PMO relation immediately after creating each child (crash-safe: duplicate relations are tolerated on resume). The scheduler then withholds each child until its blockers are done (`04-orchestrator.md` §2, `adr/0007`).
- **Depth limit (configurable, default 2 — adr/0012):** decomposition depth is **PMO state**: every child's machine marker records its generation (`depth={d}`; a marker without the field is a level-1 child from the depth-1 era). A mission's depth is read from its own record only — no `DEVCAKE-CREATED` label ⇒ depth 0 regardless of description text (a forged marker is inert); label + marker ⇒ the recorded depth; label without a readable marker ⇒ **treated as at-limit, fail-safe**. Marker reads anchor to the **last** match in the description (the app appends the genuine footer after the untrusted Dev-authored body), and marker syntax inside draft bodies is defanged at child creation — quoted markers can never shadow the real one. The check is **replay-stable**: once child checkpoints exist, a resume finishes the wiring even if the operator lowered the limit mid-flight. A parked at-limit mission is restored to `backlog` and its run carries the `handed off: decomposition depth limit` verdict. `AppConfig.max_decomposition_depth` (Traffic control; `0` = unlimited — the ONBOARD Dev decides) bounds it: at or above the limit the app parks a `decomposed` outcome with `DEVCAKE-SKIP` + comment (retrying cannot change depth, so this is a hand-off, not a counted attempt). The dispatch-time playbook interpolates the matching per-mission rule (`{decomposition_rule}`, §7) so an at-limit Dev is told `decomposed` is forbidden *before* wasting a run. The default of 2 lets a Project's missions each split once more; the limit is what prevents fission chain reactions — Unlimited removes that backstop by explicit operator choice.

**Finalization:** transcript + token report → canonicalize the normalized draft list as sorted compact JSON and hash it with SHA-256 → create each child via `PMOPort.create_mission` (app adds `DEVCAKE-CREATED`, plus `DEVCAKE` in opt-in mode). Every child carries `devcake:decomposition:v1 parent={pmo_id} manifest={sha256} part={i}/{n} depth={d}` (the depth field is the marker's only post-v0.1.1 addition; markers without it parse as depth 1 — adr/0012). Before any write, the app scans all `DEVCAKE-CREATED` missions: exact marker parts are reused and a partial manifest is topped up (the machine marker is the only recognized format). A different manifest, duplicate part, or title/marker disagreement creates **nothing** and hands the parent off with `DEVCAKE-NEEDS-HUMAN`. Then:
- original is an **Issue** → **containment:** children are created inside the original's containing Project when it has one (`parent_ref` passthrough), so the tracking sweep waits for them. **Edge inheritance (adr/0012):** before anything terminal happens to the original, its dependency topology is replicated onto the children — every still-open blocker of the original (re-read from a **post-creation** snapshot, so relations a human adds mid-finalize are honored) gains a relation onto **each** child, and **each** child gains a relation onto every still-open dependent of the original (all-children fan-out; a mission "blocked by the original" becomes "blocked by all of its replacements"). Inherited edges are checkpointed, duplicate-tolerant, and **strict — fail-closed exactly like sibling edges**: a failed edge aborts finalization *before* the cancel, so the original stays open and keeps gating its dependents; a transient error heals on the retry, a deleted endpoint drops out of the recomputed lists, and a persistent error parks the mission with ordering intact for a human. The lineage description note `_Decomposed by DevCake into {children keys}_` is appended next (**best-effort by design** — it is provenance, not ordering safety; a failing append audits `lineage_note_failed` and never blocks the cancel; replay-guarded via stable child keys). Only then is the linking comment posted and the original `canceled`. Because every inherited edge must exist **before** the cancel, the gate never sees a canceled original without replacement edges — unconditionally (`04-orchestrator.md` §2);
- original is a **Project** → children are created inside the Project, the Project gets `DEVCAKE-TRACKING` and stays open; the poll loop auto-completes it when all children are done (`04-orchestrator.md` §1.3, ADR-0006). Projects always take this path — never trivial or normal (`05-pmo-adapter.md` §6). Relations are issue-scoped, so the project path needs no edge inheritance.

## 2. PLAN

**Goal:** produce a plan and nothing else.

The Dev invokes the harness's plan capability (mapping per harness in `08-harness-templates.md` §3) over the Mission and the codebase, producing `/workspace/out/PLAN.md`. No code changes. `result.json`: `outcome: "planned"`.

**Finalization:** transcript + token report → upload `PLAN_{seq}.md` to the activity feed (as attachment, referenced from a comment) → swap `DEVCAKE-PLAN` → `DEVCAKE-EXECUTE`.

## 3. EXECUTE

**Goal:** implement the most recent plan (and address the most recent review report, if any).

The Dev reads the latest `PLAN.md` and the latest REVIEW report from `ACTIVITY.md`/attachments, then:

1. **Branch:** `devcake/{INSTANCE}-{mission_key}` (e.g. instance `linear`, key `ENG-142` → `devcake/LINEAR-ENG-142`) — the branch convention is defined once as `mission_branch(instance, key)` in `ports/forge.py` and imported by the orchestrator and prompt templates alike. The uppercased instance prefix keeps branches collision-free across PMO instances (schema v3); pre-v3 unprefixed branches remain findable via `legacy_branch(key)`. If the branch already exists on the remote (a prior EXECUTE loop), check it out and continue on it. **Never force-push.**
2. Implement; run the repo's tests/build where present.
3. **Commit only at the very end** (INV-6). Commit message: `[{mission_key}] {concise summary}`.
4. Push; the PR/MR is opened by the Dev via the forge CLI/API using injected credentials — **idempotently**: if a PR for the branch exists, update it (title/body) instead of creating another. Title: `[{mission_key}] {title}`; body links the Mission URL and the plan. The concrete CLI instructions in the playbook prompt come from the active forge adapter's `ForgeDescriptor.pr_instructions` — never hardcoded per forge.

`result.json`: `outcome: "executed"` with `pr_url` and `summary`.

**Finalization:** transcript + token report → swap `DEVCAKE-EXECUTE` → `DEVCAKE-REVIEW` → post the PR link as a feed comment. The PR-link comment is **not** idempotent by scanning prior `pr_url` posts — redelivery safety rides `run.finalized_steps` checkpoints, not a feed scan for the same URL.

## 4. REVIEW

**Goal:** act as a skeptical software engineer over the previous step's work.

The Dev must: check out the PR branch in its clone; diff against the plan; hunt for bugs, flaws, and omissions; run the tests if present. The playbook prompt forbids rubber-stamping — the default posture is distrust.

`result.json`: `outcome: "reviewed"`, `verdict: "approve" | "reject"`, `report_md` (the full review report), `pr_url`.

### 4.1 Approve

**Merge always precedes Done** — a Mission's Done status must never claim more than the repository shows (confirmed decision). Finalization: transcript + token report → forge effects, then the PMO transition:

1. Post the review report as a PR comment, **always ending with the copy-pasteable approval command** (§5).
2. If a **reviewer token** is configured (GUI secret `reviewer_token` for the repo), formally approve the PR with it; otherwise the comment carries the marker `APPROVED-BY-DEVCAKE`.
3. Then, by `auto_merge`:
   - **ON:** merge the PR (`06-forge-adapter.md` §5). Success → remove `DEVCAKE-REVIEW`, mission status `done`. Failure branches three ways on the port's `mergeable()` read:
     - **Auto-resolvable** (merge conflict or stale branch) with `auto_resolve_merge_conflicts` ON and fewer than 2 prior attempts → swap `DEVCAKE-REVIEW` → `DEVCAKE-EXECUTE` with a 🧩 resolve directive (🧩 is reserved for this directive; 🔀 already means "PR opened"): the next EXECUTE Dev only syncs the branch with the default branch, resolves the conflicts, and pushes; the PR then returns to REVIEW. Attempts are counted from `` `devcake:conflict-resolve:N` `` markers in the feed (PMO-derivable, quoted lines ignored); the directive posts **before** the label swap so the count never undercounts.
     - **Not possible yet** (mergeability still computing, CI pipeline running) and `merge_retry_window_minutes` > 0 → remove `DEVCAKE-REVIEW`, add `DEVCAKE-MERGE`, post a deferred-retry comment carrying `` `devcake:merge-retry` `` — the merge sweep keeps retrying (below).
     - **Otherwise** (toggle off, attempts exhausted, window 0, unknown cause) → remove `DEVCAKE-REVIEW`, add `DEVCAKE-MERGE`, post an explanatory comment carrying `` `devcake:merge-handoff` `` — the Mission stays In Progress until a human resolves the merge, and it appears in the admin panel's **awaiting-human-merge banner** (`11-admin-panel.md` §2) within one poll cycle.
   - **OFF:** remove `DEVCAKE-REVIEW`, add `DEVCAKE-MERGE`. The Mission stays In Progress, awaiting the human merge; it also appears in the awaiting-human-merge banner (the operator's live merge queue).

**Merge sweep** (every poll cycle, alongside the `DEVCAKE-TRACKING` sweep — `04-orchestrator.md` §1): for each Mission carrying `DEVCAKE-MERGE`, check its PR via the forge adapter — merged → remove the label, mission status `done`; closed without merging → remove the label, mission status `canceled`, with a comment either way. Additionally, while the feed's latest merge-state marker is `` `devcake:merge-retry` `` (auto_merge ON, PR open), the sweep drives the **deferred-merge window**: `mergeable()` each cycle — ready → merge and complete; auto-resolvable → the conflict-rework path above; still computing → wait. Once `merge_retry_window_minutes` elapse (measured from the marker comment's PMO timestamp — live-tunable, restart-safe), post the terminal hand-off with `` `devcake:merge-handoff` `` exactly once. A human can merge manually at any point mid-window. State remains fully derivable from PMO + forge; nothing local.

### 4.2 Reject
**Finalization:** transcript + token report → upload `report_md` as a `{seq}_REVIEW_REPORT.md` attachment referenced by a short feed comment (docs/05 §4), AND post it in full as a PR comment → swap `DEVCAKE-REVIEW` → `DEVCAKE-EXECUTE`. The next EXECUTE Dev finds the report in its `activity/` folder and reworks the same branch/PR.

**Loop guardrail:** loops are unlimited by design, but every `review_loop_warning_every`-th (default 3rd) rejection of the same Mission, the app posts a warning **to the Mission's activity feed** (the source of truth, where a human intervenes by adding `DEVCAKE-SKIP`) **and mirrors it as a PR comment**, containing the loop count and cumulative token cost across all the Mission's runs; also emitted as a metric (`12-observability.md`). Loop count is derived from **local finished REVIEW runs with `verdict == "reject"`** for that mission (store-side), not from feed artifacts.

## 4a. Human hand-off (`human_needed`)

Any ONBOARD, EXECUTE, or REVIEW run may end with `outcome: "human_needed"` instead of its normal outcome when the Dev hits an obstacle **only a human can clear** — a missing permission or credential scope, an external account/service decision, anything outside the repository. The playbooks instruct the Dev to stop rather than improvise a workaround, and — **evidence requirement** — to first actually attempt the blocked operation and quote its exact error/output in `summary`: a hand-off is expensive, and one without evidence wastes a human's time. (PLAN cannot emit this: plan mode is read-only and the entrypoint synthesizes its `result.json`.)

**Finalization:** transcript + token report → add `DEVCAKE-NEEDS-HUMAN` (the stage label stays, so work resumes at the same step) → post a baton-pass comment quoting the summary and the resume instruction. If the run was an ONBOARD (no stage label at dispatch) the status is restored to `backlog` — otherwise removing the label later would land on derivation row 9 and strand the Mission. For **project-kind** missions (no issue-style comments API) the baton goes out as a **project update** — Linear's project-native feed, sentinel-signed (`05-pmo-adapter.md` §6; verified live 2026-07-12).

**Loop guardrail (warnings only — never auto-park; founder decision 2026-07-12):** the app counts prior `human_needed` runs for the same (mission, stage) from the run store; from the 2nd hand-off on, the baton-pass comment carries an escalating header — "Hand-off #N on this step … add `DEVCAKE-SKIP` to stop DevCake on it." DevCake never parks on its own; the human always decides.

**Semantics vs neighbors:** `DEVCAKE-FAILED` = DevCake errored out after `max_attempts` (involuntary); `DEVCAKE-SKIP` = human opt-out; `DEVCAKE-NEEDS-HUMAN` = a clean, deliberate hand-off — the run `finished`, so it **never counts toward `max_attempts`**. Recovery: the human resolves the obstacle and removes the label; the Mission re-derives its stage on the next poll. See `15-errors-and-retries.md`.

## 4b. Relations Mapper (`MAPPER` runs)

A **team-scoped run kind** (not a Mission Type — it has no host Mission and no labels) whose only job is proposing missing blocked-by relations across the team's open Missions. Configured under `AppConfig.relations_mapper` (`02-domain-model.md` §9): **manual-only by default** (the admin "Run now" button) with an opt-in periodic service. Its default vehicle is the seeded **junior-dev** Dev Type (claude-code pinned to a cheap model) — ordering judgment from titles and description heads doesn't need a heavyweight; the repo clone is kept so a future prompt can let it inspect code for dependency evidence.

**Cadence & degradation (`MapperService`):** one lock serializes the manual and periodic paths (no double dispatch); the interval watermark advances only after a successful dispatch (a transient executor error costs one poll cycle, not a full interval); and when the 3 most recent MAPPER runs all died, the periodic service **backs off** — surfaced as `mapper_degraded` in `/health` and on the admin card — while "Run now" stays available and a successful run clears the condition (store-derived, restart-safe).

- **Dispatch:** the app inlines every open, adopted, issue-kind Mission into the prompt — `key · status · existing blocker keys · title · first ~300 chars of description` (capped at 200 missions; truncation logged). No PMO writes at dispatch. Skipped while `intake_paused`; max one MAPPER run in flight; counts toward `global_max`.
- **Output:** `result.json` `{"outcome": "relations_mapped", "edges": [{"blocker": "<key>", "blocked": "<key>"}, …], "summary": "…"}` — an empty `edges` list is valid and common. The playbook demands conservatism: propose only edges where one Mission clearly consumes another's output; never invent keys.
- **Finalization (the app is the gatekeeper):** each proposed edge is validated against a live snapshot and dropped (audited `mapper_edge_rejected`) if it references an unknown or terminal key, is a self-edge, duplicates an existing relation, or would create a cycle in the blocked-by graph. Surviving edges become native PMO relations, and the blocked Mission gets a sentinel-signed comment naming its blocker ("delete the relation in Linear if wrong"). No transcript/token-report comments — there is no host Mission; failures are logged only and the next interval retries.

## 5. The approval-command footer (normative)

Every REVIEW PR comment (approve *and* reject — on reject it helps a human short-circuit the loop) ends with the footer supplied by the active forge adapter's `approval_footer()`. For example:

```
---
To approve and merge this PR yourself:
  gh pr review --approve <PR_URL> && gh pr merge --squash <PR_URL>     # GitHub
  glab mr approve <MR_IID> && glab mr merge <MR_IID>                   # GitLab
```

rendered with the *concrete* URL/IID substituted — one paste must suffice. Each adapter emits only its own dialect (the `gh` line on GitHub, the `glab` line on GitLab).

## 6. `result.json` schema (normative)

```jsonc
{
  "schema_version": 1,
  "outcome": "plan_needed | decomposed | planned | executed | reviewed | human_needed | relations_mapped",
  "summary": "one-paragraph human summary of what was done/found",   // required, all outcomes
  "verdict": "approve | reject",          // REVIEW only
  "report_md": "…full review report…",    // REVIEW only
  "decomposition": [                       // ONBOARD 'decomposed' only
    {"title": "…", "description": "…", "priority": "high", "parent_ref": null,
     "blocked_by": [1]}                    // optional: earlier-sibling indexes (§1.3)
  ],
  "edges": [                               // MAPPER 'relations_mapped' only (§4b)
    {"blocker": "ENG-10", "blocked": "ENG-12"}
  ],
  "pr_url": "https://…"                    // executed / reviewed
}
```

A `plan_needed` outcome may additionally be accompanied by `/workspace/out/PLAN.md` (the opportunistic plan, §1.2) — carried in the `run.artifacts` payload as `plan_md`, like a PLAN run's output (`09-messaging.md` §3).

**Outcome legality (normative — the trust boundary).** Devs ingest untrusted text (mission descriptions, human comments), so a forged outcome must never let a run transition outside its step. The app enforces this table at finalization (`domain/orchestrator/markers.py`, `LEGAL_OUTCOMES`) — an illegal outcome on a mission step is parked with `DEVCAKE-SKIP` + comment + audit `illegal_outcome`, never acted on. The Dev entrypoint applies a related first-line check (exit 11) that also knows MAPPER's `relations_mapped`; the app table below is the mission-step invariant (old images may run):

| Run type | Legal outcomes |
|---|---|
| ONBOARD | `plan_needed` · `decomposed` · `human_needed` |
| PLAN | `planned` |
| EXECUTE | `executed` · `human_needed` |
| REVIEW | `reviewed` · `human_needed` |

**MAPPER is not in `LEGAL_OUTCOMES`.** Mapper runs finalize through a separate path (`finalize_mapper`): the only accepted outcome is `relations_mapped`; anything else marks the run **`failed`** (no `DEVCAKE-SKIP` parking — there is no host mission to park).

A **structurally invalid payload** behind a legal outcome (empty decomposition, forward/self `blocked_by` index) is different: it fails the run as `DEV_BAD_OUTPUT` — a counted attempt that retries naturally (`15-errors-and-retries.md` §2) — because a formatting slip deserves a retry where a forged outcome does not.

## 7. Canonical prompts (v0)

The full prompt a Dev receives = **identifying prompt** (Dev Type, below) + **playbook prompt** (per Mission Type, maintained as templates in `app/devcake/prompts/`, interpolated with mission metadata; the EXECUTE playbook additionally interpolates the forge descriptor's `pr_instructions`). Playbook prompts inline only the mission title and description; the `activity/` folder is presented as **reference material to consult as needed** ("the mission's history and artifacts are in activity/ — grep or read what you need"), never dumped into the prompt (`07-dev-runtime.md` §2). The playbook prompts restate, verbatim, the binding rules from this document: workspace boundaries (INV-6), commit-at-end, branch conventions, the standalone rule, the depth limit, and the `result.json` contract.

### Senior Dev — identifying prompt
> You are **Senior Dev**, DevCake's judgment-heavy engineer. You assess, plan, and review software work with the skepticism of a staff engineer who has been burned before. You are precise about scope: you do exactly what your current mission type asks — no more. You never invent requirements, you flag what you cannot verify, and you write conclusions that a teammate can act on without asking follow-up questions.

### Main Dev — identifying prompt
> You are **Main Dev**, DevCake's implementation engineer. You turn plans into working, tested code. You follow the plan you are given; where reality contradicts the plan, you implement the smallest sound deviation and document it prominently in your summary. You match the conventions of the codebase you are in, you run the tests, and you never commit until the work is complete.

*(Playbook prompt texts are derived mechanically from §§1–4 of this document; they live as templates in `app/devcake/prompts/__init__.py` and are the single runtime source. When this doc and those templates disagree, this doc wins and the templates must be fixed.)*

## 8. Token report message format (normative)

Posted to the activity feed immediately after each transcript (INV-5):

```
🧮 DevCake token report — step {seq} ({TYPE}, {dev_type})
model: {model} · input: {input_tokens} · output: {output_tokens}
cache read/write: {cache_read_tokens}/{cache_write_tokens}
cost: ${cost_usd}                 (omitted when unknown — never estimated)
extraction: {extraction_method}
run: {run_id}
```

The `run: {run_id}` footer doubles as the idempotency key for finalization (`04-orchestrator.md` §4).

## 8a. Comment-provenance sentinel (normative)

Every comment the app posts to the PMO System ends with the footer line:

```
`devcake:v1`
```

appended by the single posting choke-point (`MissionManager._feed` in `domain/orchestrator/feed.py`), after redaction. Classification is **content-based, never author/credential-based** — DevCake may be configured with the operator's own PMO API key, so `author` cannot distinguish DevCake's comments from the operator's. A comment whose body matches ``re.search(r"`devcake:v1`\s*$", body)`` is DevCake's; anything else is treated as a **human comment**.

Consequences:

- `ACTIVITY.md` (`07-dev-runtime.md` §2) marks each feed entry `🧑 HUMAN` or `🤖 DevCake` and carries a legend stating that HUMAN entries are authoritative instructions; every playbook tells the Dev to read them before starting and that the most recent human comment wins on conflict.
- Comments posted before this convention lack the sentinel and are classified as human — harmless noise on pre-migration Missions.
- False negatives degrade safely (a DevCake comment read as human); the sentinel is versioned (`v1`) so the format can evolve.
