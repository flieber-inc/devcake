# 02 — Domain Model

> **Audience:** implementers. The code's pydantic models — `app/devcake/domain/model.py` (Mission, labels, derivation), `app/devcake/domain/run.py` (Run), `app/devcake/config.py` (AppConfig, DevType) — must match this document 1:1 — field names included.
> **Depends on:** `00-overview.md` (glossary, invariants).

## 1. Mission

A **Mission** is a normalized DTO produced by the PMO adapter from a live Linear Project or Issue. Per INV-1 it is **never persisted authoritatively** — it is re-derived from the PMO System on every poll cycle and re-read live at dispatch and finalization time.

| Field | Type | Notes |
|---|---|---|
| `pmo_id` | `str` | The PMO System's stable ID — an opaque vendor id (Linear: a UUID). Primary key. |
| `pmo_kind` | `"issue" \| "project"` | What the Mission is in the PMO System. |
| `key` | `str` | Human-readable key, e.g. `ENG-142`. For Linear Projects (which have no issue key): a slug of the project name prefixed `PRJ-`, e.g. `PRJ-payment-revamp`. Used in branch names and transcript labels. |
| `title` | `str` | |
| `description` | `str` | Markdown body. |
| `status` | `"backlog" \| "in_progress" \| "done" \| "canceled"` | Normalized (mapping tables in `05-pmo-adapter.md` §3). |
| `priority` | `"urgent" \| "high" \| "medium" \| "low"` | Normalized; a Mission with no priority in the PMO System is `medium`. Always read live (INV-1). |
| `labels` | `set[str]` | Label names as they appear in the PMO System. |
| `updated_at` | `datetime` | PMO-side last update. Scheduling tiebreaker. |
| `url` | `str` | Deep link into the PMO System. |
| `parent_ref` | `str \| None` | For Issues that belong to a Project: the project's `pmo_id`. |
| `blocked_by` | `list[str]` | `pmo_id`s of Missions that block this one, read from the PMO System's native issue relations (`05-pmo-adapter.md` §3, `adr/0007`). Always `[]` for Projects (Linear relations are issue-scoped). Gates scheduling (`04-orchestrator.md` §2), not derivation. |
| `instance` | `str` | Which configured PMO instance produced this Mission (schema v3) — stamped by the adapter at normalization so no fetch path can return an unstamped mission. |
| `repo` | `str \| None` | Resolved work-repo instance name for this mission (poll-cycle stamp; never persisted). |
| `repo_reason` | `str \| None` | Human-readable reason the mission is gated without a resolved repo (poll-cycle stamp). |

## 1a. MissionRef and the activity feed DTOs

**`MissionRef`** is a `NamedTuple(pmo_id: str, kind: "issue" | "project")` — the adapter-facing mission handle. The port's unified read/write methods (`get`, `get_activity`, `post_feed`, `set_status`, `swap_labels`, `children_of` — `05-pmo-adapter.md` §1) take a ref; how each kind is stored (Linear's issue/project duality, or nothing of the sort) is the adapter's business, never the domain's. `Mission.ref` is a property returning `MissionRef(pmo_id, pmo_kind)`.

**`Activity`** is `{mission: Mission, entries: list[ActivityEntry], mission_attachments: list[AttachmentRef], truncated: bool}` — the normalized feed returned by `get_activity`. `mission_attachments` (full mode only, ADR-0014): assets the mission itself references — description-embedded uploads + the vendor's native attachment list. `truncated`: the full-history hard stop tripped; the activity-folder builder renders a loud banner.

**`ActivityEntry`:**

| Field | Type | Notes |
|---|---|---|
| `ts` | `datetime` | |
| `author` | `str` | |
| `kind` | `"comment" \| "status_change" \| "attachment"` | |
| `body` | `str` | Markdown. |
| `attachments` | `list[AttachmentRef]` | Assets referenced from the entry. |
| `entry_id` / `parent_id` | `str \| None` | Reply structure (ADR-0014) — populated only by full-mode `get_activity`; `None` on the shallow marker-scan path. |

**`AttachmentRef`** is `{url: str, name: str | None, kind: "file" | "link"}` — `name` is the markdown link text when the feed carried one; the **adapter** resolves it, so the domain never parses vendor asset URLs (`05-pmo-adapter.md` §4). `kind` (ADR-0014): `file` = downloadable bytes; `link` = external reference the builder renders as a markdown link.

## 2. Mission Type derivation (normative table)

Mission Type is a **pure function of live PMO state** — it is computed, never stored.

**Adoption gate (checked first):** in `opt_in` adoption mode (`AppConfig.adoption_mode`, the default) a Mission is only considered at all if it carries the `DEVCAKE` label; without it, DevCake ignores the item entirely. In `opt_out` mode every item is considered. Missions DevCake creates by decomposition receive the `DEVCAKE` label automatically (in opt-in mode), so pipelines never stall on their own children.

| # | Normalized status | Stage labels present | Derived Mission Type |
|---|---|---|---|
| 1 | `backlog` | none | **ONBOARD** |
| 2 | `backlog` or `in_progress` | `DEVCAKE-PLAN` only | **PLAN** |
| 3 | `backlog` or `in_progress` | `DEVCAKE-EXECUTE` only | **EXECUTE** |
| 4 | `backlog` or `in_progress` | `DEVCAKE-REVIEW` only | **REVIEW** |
| 5 | `done` or `canceled` | any | *terminal — not a Mission; ignore* |
| 6 | any active | ≥ 2 stage labels | *conflict — do not schedule; see `15-errors-and-retries.md` `LABEL_CONFLICT`* |
| 7 | any active | `DEVCAKE-SKIP` present | *human opt-out — do not schedule (overrides rows 1–4)* |
| 8 | any active | `DEVCAKE-FAILED` present | *needs human attention — do not schedule (overrides rows 1–4)* |
| 9 | `in_progress` | none | *no derivable type — a human moved it or a transition half-applied; do not schedule, log at INFO. It becomes schedulable again when a human sets a stage label or moves it back to backlog.* |
| 10 | any active | `DEVCAKE-MERGE` present | *awaiting merge — not schedulable; handled by the merge sweep (`04-orchestrator.md` §1), which completes the Mission when its PR merges (or cancels it if the PR is closed unmerged).* |
| 11 | any active | `DEVCAKE-NEEDS-HUMAN` present | *awaiting human action — do not schedule (overrides rows 1–4). A Dev deliberately handed off to a human (`03-mission-lifecycle.md` §4a); removing the label resumes the Mission at the same stage.* |

Rows 7, 8, 10, and 11 take precedence over rows 1–4; row 6 over everything except 5.

> **Blocked-by is not a derivation row.** A Mission whose `blocked_by` contains an open Mission still derives normally, but the scheduler skips it until every blocker is `done`/`canceled` (`04-orchestrator.md` §2) — this keeps `derive()` a pure single-Mission function.

> **Note on row 9:** ONBOARD is only derived from `backlog` + no stage label. This guarantees DevCake never "adopts" work a human has independently started (`in_progress` with no DevCake labels).

## 3. State machine

```
                        ┌───────────────────────────────────────────────────────┐
                        │                        ONBOARD                        │
                        │  (backlog, no stage label; opt-in gate passed)        │
                        └───────┬────────────────┬─────────────────┬────────────┘
                        trivial │         normal │            high │ complexity
                       (= plan  │                │                 │
                        attach) ▼                ▼                 ▼
                     + DEVCAKE-EXECUTE    + DEVCAKE-PLAN    decompose: create child
                     (PLAN.md attached          │           Missions (DEVCAKE-CREATED);
                      from triage — PLAN        │           Issue → Canceled,
                      step skipped; ONBOARD     │           Project → + DEVCAKE-TRACKING
                      never implements)         ▼
                                │  ┌───────────────────────────────┐
                                │  │             PLAN              │ → upload PLAN.md
                                │  └───────────────┬───────────────┘
                                │                  │ swap DEVCAKE-PLAN → DEVCAKE-EXECUTE
                                │                  ▼
                                └─►┌───────────────────────────────┐
                                   │            EXECUTE            │◄────────────┐
                                   └───────────────┬───────────────┘             │
                                                   │ swap → DEVCAKE-REVIEW       │
                                                   ▼                             │
                        ┌───────────────────────────────┐  reject: swap → EXECUTE│
                        │            REVIEW             │────(+ warning every ───┘
                        └───────────────┬───────────────┘      3rd loop)
                                        │ approve
                                        ▼
                        remove DEVCAKE-REVIEW, approve PR, then:
                        · mission's repo auto_merge ON: merge PR → Done
                            (conflict + that repo's auto-resolve ON, < 2 tries →
                             swap → EXECUTE with a resolve directive;
                             not-mergeable-yet → + DEVCAKE-MERGE, sweep
                             retries for that repo's merge_retry_window;
                             else → + DEVCAKE-MERGE + warning)
                        · mission's repo auto_merge OFF: + DEVCAKE-MERGE
                                        │
                                        ▼
                        ┌───────────────────────────────┐
                        │  merge sweep (every poll):    │
                        │  PR merged → Done, drop label │
                        │  PR closed unmerged → Canceled│
                        │  retry window open → mergeable│
                        │  → merge / conflict → EXECUTE │
                        └───────────────────────────────┘
```

**A Mission only reaches Done through a merged PR** (or through decomposition/human action) — merge always precedes the Done status, in every path.

**All transitions are label/status writes to the PMO System performed by the app** (never by the Dev — INV-4) during run finalization, using the compare-and-transition procedure of `04-orchestrator.md` §4. The playbook for each state is `03-mission-lifecycle.md`.

## 4. Priority ordering

`urgent` > `high` > `medium` > `low`. A Mission with no priority set in the PMO System is treated as `medium`. Priority is read live at dispatch time, never from the poll cache (INV-1, `04-orchestrator.md` §3).

## 5. Managed labels (the complete set)

Defined here and only here; code keeps them in a single constants module. The app ensures all ten exist in the configured Linear team at startup (`05-pmo-adapter.md` §5).

| Label | Class | Meaning |
|---|---|---|
| `DEVCAKE` | opt-in | In `opt_in` adoption mode (the default), only Missions carrying this label are adopted by DevCake. Ignored in `opt_out` mode. |
| `DEVCAKE-PLAN` | stage | Mission awaits a PLAN step. |
| `DEVCAKE-EXECUTE` | stage | Mission awaits an EXECUTE step. |
| `DEVCAKE-REVIEW` | stage | Mission awaits a REVIEW step. |
| `DEVCAKE-MERGE` | awaiting-merge | REVIEW approved; the PR awaits merging (by a human, after an `auto_merge` failure, or — while the deferred-merge retry window is open — by the sweep itself once CI/mergeability clears; conflict auto-resolve must be off or exhausted for the hand-off to be final, `03-mission-lifecycle.md` §4.1). The poll sweep watches the PR and completes the Mission when it merges (`04-orchestrator.md` §1). |
| `DEVCAKE-CREATED` | provenance | This Mission was created by DevCake (decomposition output). Coexists with stage labels. |
| `DEVCAKE-FAILED` | attention | A step failed `max_attempts` (default 3) times. DevCake will not touch the Mission until a human removes the label. |
| `DEVCAKE-SKIP` | opt-out | A human told DevCake to ignore this Mission entirely (works in both adoption modes). |
| `DEVCAKE-TRACKING` | tracking | A decomposed Project awaiting auto-completion once all its child Issues reach Done/Canceled. |
| `DEVCAKE-NEEDS-HUMAN` | hand-off | A Dev reported an obstacle only a human can clear (`human_needed` outcome — `03-mission-lifecycle.md` §4a). Applied by the app with a baton-pass comment; the human resolves the obstacle and removes the label to resume. Unlike `DEVCAKE-FAILED`, the run finished cleanly and never counts toward `max_attempts`. |

Naming is flat and uppercase; there are no version suffixes. Renaming a label is a documented migration (create new → copy → retire old), per `adr/0004-label-namespace-and-versioning.md`.

## 6. DevType

Persisted as one YAML file per Dev Type at `/data/config/dev_types/{name}.yaml` (`10-persistence.md`), CRUD-ed via the admin panel.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | e.g. `judgment`, `implementer`, `mapper` (kebab-case slug; display name derived). |
| `harness_template` | `"claude-code" \| "grok-build" \| "codex"` | **Authoritative** (2026-07-12 rework): the Docker image, credential requirements, and OAuth flow all derive from it via the harness registry (`app/devcake/harness.py`, `08-harness-templates.md` §2/§4). Changing it in the admin panel changes what actually runs. |
| `identifying_prompt` | `str` | Always delivered to the harness at the start of every run, before the playbook prompt. Short persona/workflow framing only — not step machinery. |
| `mcp_setup_commands` | `list[str]` | Shell commands run by the Dev entrypoint before harness launch — the MCP-plugin install/register lines (`08-harness-templates.md` §7). Delivered as a top-level runspec key, live-read at `runspec.get` (like the secret half — an edit applies to the next run without redispatch). Failure or 300 s per-command timeout ⇒ exit code 14, `DEV_MCP_SETUP` (`15-errors-and-retries.md` §1). |
| `skills` | `list[str]` | Skill-store skills **installed** (Available) in the Dev container before harness launch, into the harness's registry-declared skills directory (`harness.py` `skills_dir`: claude-code → `~/.claude/skills`; grok-build/codex → `~/.agents/skills` — `08-harness-templates.md` §7a). Consult is **optional** by default (model description-match). A harness with no `skills_dir` skips them with a warning. Names validated (`^[a-z0-9][a-z0-9_-]{0,63}$`), deduped preserving order. A selected-but-missing skill is skipped with a warning, never a refused run. Skills are **domain modules** (additive, not mission-step scripts) — **ADR-0016**, `app/devcake/skills/README.md`. |
| `skills_required` | `list[str]` | Subset of `skills`. Soft-force: after the playbook, the composed prompt appends a “must consult these skills” block listing names that actually shipped in the runspec payload. **Instructional only** — harnesses do not hard-enforce skill load (**ADR-0016**). Default `[]`. Validator: every name must appear in `skills`. |
| `secret_env` | `list[str]` | **Names** of GUI-stored secrets (`/data/secrets/harness/{VAR}.json`) delivered into the run's env — mission-tooling credentials referenced as `$VAR` from `mcp_setup_commands` (e.g. `DD_API_KEY` for a log-platform plugin), and any var the harness CLI itself reads (e.g. `ANTHROPIC_BASE_URL` / `GROK_MODELS_BASE_URL` for a local backend — `08-harness-templates.md` §8a). UPPER_SNAKE_CASE, ≤64 chars; reserved names refused (`PATH`/`HOME`, `REDIS_*`, `TRACEPARENT`, the forge CLI tokens `GH_TOKEN`/`GITLAB_TOKEN`/`GITEA_SERVER_TOKEN`, and the `DEVCAKE_*`/`OTEL_*`/`GIT_*` prefixes — they would shadow the protocol env). Missing value: **referenced** by a setup command ⇒ dispatch gate (`14-security.md` §8); unreferenced ⇒ warn-and-proceed. Global store: one stored value serves any number of Dev Types. |
| `max_concurrency` | `int` | Per-type cap (see `04-orchestrator.md` §3). |
| `model` | `str` | Pins the harness model (added 2026-07-12 after Claude Code silently defaulted to Sonnet). Delivered via runspec as `DEVCAKE_MODEL`; the entrypoint maps it to the harness flag (`claude --model` / `codex -m` / `grok --model`). Empty = harness default. Seed: `judgment` = `claude-fable-5`. Per-assignment `extra_cli_args` can still override (appended after the pin). |

There is deliberately **no stored `docker_image` or credential config**: requirements are per-harness (registry), while secret *material* stays per Dev Type under `/data/secrets/{name}/` — so two Dev Types on the same harness can hold different accounts. Legacy YAML keys (`docker_image`, `credential_env`, `credential_files`) are ignored on load and dropped on the next save.

**v0 defaults:**

| Dev Type | Template | Mission Types |
|---|---|---|
| `judgment` | `claude-code` (Claude Fable) | ONBOARD, PLAN, REVIEW |
| `implementer` | `grok-build` (Grok 4.5) | EXECUTE |
| `mapper` | `claude-code` (`claude-haiku-4-5`) | Relations Mapper (default vehicle; assignable anywhere) |

Dev Types are **vehicles** (harness, model, concurrency, skill chips), not seniority ranks. Mission-step contracts live in playbooks; domain knowledge lives in skills (**ADR-0016**).

Default Dev Types are **re-seeded by name** whenever their YAML is missing (boot-time top-up) — customize a default by editing it, not deleting it; a deleted default returns on the next boot.

The Mission-Type→Dev-Type assignment lives in `AppConfig.assignments` (§9); each Mission Type maps to exactly one Dev Type; a Dev Type may serve any number of Mission Types. A PMO instance may **override** rows for itself (`PMOInstance.assignments`, ADR-0019): a present key replaces the global row wholesale (args included), an absent key inherits it live — so the one-type-one-Dev rule holds *per instance*, and different boards can staff different crews on one deployment.

## 7. Run

The locally persisted record of one Mission Step attempt, one JSON file per run at `/data/state/runs/{run_id}.json`. **Telemetry and dispatch bookkeeping only** — wiping it never corrupts Mission state (INV-1); the documented consequence is reset attempt counters (`10-persistence.md` §5).

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` (= 2) | Bumped by the credential-state hardening (secrets left the record). Records `< 2` are quarantined at boot, never migrated (`10-persistence.md` §5). The `pmo_ref`/`repo_ref` additions were additive with defaults — no bump of their own. |
| `run_id` | `str` | Human-readable and unique: `{INSTANCE}-{key}-{seq}-{TYPE}-{6-char ULID suffix}`, e.g. `LINEAR-ENG-142-3-EXECUTE-9GX2TQ` (charset `[-A-Za-z0-9_]`, ≤ 64 chars — fits Dagu's `dagRunId` rules). The uppercased PMO-instance prefix (schema v3) keeps run ids — and therefore ACL users, container names, and reply streams — collision-free across instances. Also the Dagu run ID and the Dev container **name suffix** — one run is now **two** containers, `prov-<run_id>` then `dev-<run_id>` (ADR-0025), so a `docker ps` shows both — while Linear, the Dagu UI, traces, and Redis streams speak the bare run id. The charset is fenced twice more under ADR-0025: the DAG's `preconditions` guard and `WorkspaceStore`'s per-run dir validation both require `re:^[A-Za-z0-9_-]{6,64}$` before a container or a workspace dir is ever created. HELLO/OAUTH runs use the fixed pseudo-instance `sys`. |
| `mission_key` | `str` | Denormalized for log/trace readability. |
| `mission_pmo_id` | `str` | |
| `pmo_kind` | `str` (default `"issue"`) | The mission's kind at dispatch. |
| `pmo_ref` / `repo_ref` | `str` (default `"main"`) | Which configured instance served this run — the `AppConfig.pmos`/`repos` entry **`name`** (§9). Default `"main"` marks pre-v3 legacy records (not a live instance name). |
| `mission_type` | `str` | The type this run was dispatched as. |
| `dev_type` | `str` | |
| `seq` | `int` | Step number for transcript naming (§8). |
| `attempt_of_step` | `int` | 1-based attempt counter for this (mission, type) — seq-independent, since failed runs advance `seq` by posting transcripts. Resets at the newest of: last give-up watermark, any finished run for the mission, or the latest human feed comment (`15-errors-and-retries.md` §3). |
| `stage_label_at_dispatch` | `str \| None` | Input to compare-and-transition (`04-orchestrator.md` §4). |
| `branch` | `str` | The PR branch minted at dispatch (schema v3) — stored so review/merge lookups never drift from what the Dev pushed. Empty on legacy/mapper/hello records (`ports.forge.run_branch` derives those). |
| `spec_prompt` | `str` | The composed prompt delivered in the run spec. |
| `spec_skills` | `list[dict]` | Skill-store files for the Dev, snapshotted at dispatch: `[{name, files: [{path, content_b64}]}]` so a mid-run Gitea outage cannot change what a runspec re-request serves. |
| `spec_skills_dir` | `str` | HOME-relative dir the entrypoint writes `spec_skills` under — snapshotted from the harness registry at dispatch; empty on legacy records → entrypoint default. |
| `state` | `"dispatched" \| "running" \| "finalizing" \| "finished" \| "failed" \| "timed_out" \| "orphaned"` | |
| `created_at` | `datetime` | |
| `started_at` / `ended_at` | `datetime \| None` | |
| `last_heartbeat` | `datetime \| None` | Watchdog input (`04-orchestrator.md` §5). |
| `timeout_seconds` | `int` (default 7200) | From `dev_timeout_minutes` at dispatch. |
| `traceparent` | `str \| None` | W3C trace context linking the run's spans (`12-observability.md`). |
| `auth_digest` | `str \| None` | SHA-256 verifier for the per-run Redis envelope credential (`09-messaging.md` §1a); the raw password is never persisted. |
| `spec_env` | `dict[str, str]` | Non-secret run environment only. Secret env and injected files live in the transient Redis run-spec record, not Run JSON. |
| `finalized_steps` | `list[str]` | Idempotency checklist: which finalization side effects have durably completed (e.g. `["transcript", "token_report"]`). |
| `result` | `dict \| None` | The Dev's parsed `result.json` payload. |
| `token_report` | `dict \| None` | Shape in §10. |
| `artifact_bytes` | `int \| None` | Size of the collected result payload (finalization telemetry). |
| `error` | `str \| None` | Mapped error class + message (`15-errors-and-retries.md`). |
| `verdict` | `str \| None` | App-level judgment when it diverges from the executor's: a run can end `state="finished"` (Dagu succeeded, artifacts were legal) yet carry `"rejected: …"` because the transition refused to act on the outcome. `None` = ordinary success. |
| `store_gen` | `int` (default 0) | Process-local wipe generation stamped at launch (`10-persistence.md`): `RunStore.clear` bumps `wipe_generation` then unlinks files; `save()` drops any run whose `store_gen` is older so in-flight finalize/heartbeat cannot resurrect a record after clear-runs. |

## 8. `seq` derivation rule (normative)

`seq` = (highest step number among prior DevCake step artifacts — comments or attachments named `N_TYPE.md` — present in the Mission's activity feed) + 1, computed at workspace-preparation time from the live feed. Max, not count: numbering stays collision-proof even if a human deletes an earlier transcript comment. This makes transcript numbering robust to local-state loss and is the same counter used to name `{seq}_{TYPE}.md` (e.g. `5_EXECUTE.md`). A retried attempt of the same step reuses the same `seq` only if the prior attempt posted no transcript; otherwise it naturally increments.

**Quoting quarantine (ADR-0014):** every feed scan — this one, the deliverable-redelivery guard, the merge/conflict markers, the provenance sentinel — runs on the body with `>`-quoted lines stripped. A quoted marker mention (a human citing a transcript name, or the blockquoted last-message text DevCake posts at step end) never counts. This is the invariant that lets model-authored prose live inline in the feed without feeding the state machine.

## 9. AppConfig

Persisted at `/data/config/config.yaml` (full annotated example in `10-persistence.md` §3). **Schema v4** (GUI secret store — ADR-0011; see `10-persistence.md` for hand-migration). Config holds **identities and non-secret fields**; secret VALUES live under `/data/secrets/` and are never written into `config.yaml`. Shape (see also multi-instance notes in `10`):

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` (= 4) | Stale shapes refused at boot with hand-migration instructions (`10-persistence.md` §3). |
| `pmos` | `list[PMOInstance]` — `{name, system, team_key, api_base, repos, reference_repos, assignments, …}` | Instance `name` is the identity. `system` validated against `PMO_SYSTEMS`. **`api_key` is a read-through** over the GUI secret store (`/data/secrets/connections/pmo-{name}.json`) — not an env-var name. `repos` is the ordered work-repo set for that instance (first = default for unmarked missions); `reference_repos` are read-only consultation clones, disjoint from the work set. `assignments` is the instance's Mission-Type override map (ADR-0019): present key = wholesale override of the global row below, absent = inherit live; empty `dev_type` and unknown mission-type keys refused. |
| `repos` | `list[RepoInstance]` — `{name, forge, url, api_base, default_branch, auto_merge, auto_resolve_merge_conflicts, merge_retry_window_minutes, …}` | `forge` validated against `forges()`. **`token` / `token_ro` / `reviewer_token` are read-throughs** over `/data/secrets/connections/repo-{name}.json` — not `*_env` fields. **Merge doctrine is per repo** (ADR-0020): `auto_merge` (default `false`) — when true, the **app** merges this repo's PRs after REVIEW approval; when false, parks at `DEVCAKE-MERGE` (does **not** strip Dev forge-token merge capability — that is branch protection, `14-security.md` §2 zone C). `auto_resolve_merge_conflicts` (default `true`, inert while `auto_merge` off) — on conflict/stale branch, route back to EXECUTE (max 2 attempts). `merge_retry_window_minutes` (default 30, `≥ 0`, inert while `auto_merge` off) — deferred-merge sweep window. **Internal (zero-repo) synthesized instances always set `auto_merge=True`** at provision time — the zip deliverable depends on merge; operators who want doctrine control create the repo as a config card instead. |
| `adoption_mode` | `"opt_in" \| "opt_out"` (default `opt_in`) | `opt_in`: only Missions labeled `DEVCAKE` are adopted. `opt_out`: every non-terminal item in the team is adopted (the original mission-doc behavior — enable deliberately; the admin panel warns about the backlog-wide consequence, `11-admin-panel.md` §3). |
| `assignments` | `dict[MissionType, {dev_type: str, extra_cli_args: str}]` | The **global** Mission Type → Dev Type map: Dev Type name plus optional **extra CLI args** appended verbatim to the harness invocation for that Mission Type (`08-harness-templates.md` §1). Args are admin-set data, never hardcoded — they are harness-specific, so the admin UI warns and offers to clear them when the Mission Type is reassigned to a Dev Type with a different harness (`11-admin-panel.md` §3). Validation: all four types assigned. Per-instance override rows on `pmos[*].assignments` take precedence wholesale (ADR-0019); resolution is `assignment_for(config, instance, mission_type)` — the only read path schedule and dispatch use. |
| `concurrency` | `{global_max: int}` | Per-type caps live on each DevType. Effective ceiling = min(global_max, Σ per-type) — this is a property of the dispatch check, not a separate rule. |
| `dev_timeout_minutes` | `int` (default 120) | Enforced by the app watchdog (`04-orchestrator.md` §5), not by Dagu. |
| `poll_interval_seconds` | `int` (default 30) | |
| `attach_merged_changeset_to_pmo` | `bool` (default `false`) | When true, after a REVIEW-approved **merge** the app also zips the PR change set onto the PMO feed for **configured** work repos. Internal/zero-repo missions always attach a zip (ADR-0010) regardless. Leave off for eng repos — the forge PR is canonical; the zip is a merge-time snapshot (size-capped, may omit files). Best-effort: packaging failure never un-Dones the mission. |
| `review_loop_warning_every` | `int` (default 3) | Post a cost warning every Nth REVIEW→EXECUTE rejection. |
| `max_attempts` | `int` (default 3) | Failed attempts of the same step before `DEVCAKE-FAILED`. |
| `recover_misplaced_result` | `bool` (default `true`) | ADR-0018: accept a result file the Dev wrote elsewhere in its workspace — only when created during that run and passing the same validation; the misplacement is always recorded. |
| `continuation_policy` | `"auto" \| "resume-only" \| "fresh-only" \| "off"` (default `auto`) | ADR-0022: in-container continuation of clean-but-incomplete runs (`07-dev-runtime.md` §5a). `auto` resumes the session where capture-verified, escalating permanently to a fresh session after a zero-progress continuation; `resume-only` stops (fails as before) when resume is unavailable. Plan mode never continues. |
| `repo_mirror` | `{sync_max_age_seconds: int ≥ 0 (default 0), lfs: bool (default false)}` | ADR-0024: the source mirror is MANDATORY (no enable field); 0 = sync before every dispatch (fail-closed precondition, `07-dev-runtime.md` §7b); `lfs` upgrades pointer files to real content. |
| `max_continuations` | `int` ≥ 0 (default 2) | ADR-0022: the continuation budget — the ONLY terminator (stalls escalate, never stop). Deliberately unbounded above (large experiments are legitimate; the watchdog bounds the run). `0` = off. Effective turn budget becomes (budget + 1) × `--max-turns`. |
| `intake_paused` | `bool` (default `false`) | Operator master switch (`11-admin-panel.md` §0): while true, no NEW runs dispatch on **any** PMO (missions or mapper). In-flight runs finish, results finalize, and the merge/tracking sweeps keep running. Hot-applied next poll cycle. |
| `pmos[].intake_paused` | `bool` (default `false`) | Per-PMO intake under the master switch: while true, that instance dispatches no NEW runs (others unaffected, unless the master is also paused). Same in-flight/sweeps semantics. |
| `max_decomposition_depth` | `int` ≥ 0 (default 2) | How many generations of ONBOARD decomposition are allowed below a root (`adr/0012`). `0` = unlimited — the ONBOARD Dev decides (`03-mission-lifecycle.md` §1.3). |
| `relations_mapper` | `{enabled: bool, interval_minutes: int, dev_type: str \| None}` (default off/60/`mapper`) | The Relations Mapper (`03-mission-lifecycle.md` §4b): manual-only by default ("Run now"); the periodic service is opt-in. `dev_type` must name an existing Dev Type whenever `enabled`; deleting the referenced Dev Type is refused (409). |
| `cost_inputs` | `{rates: [{model_prefix, input_per_mtok, cache_read_per_mtok, cache_write_per_mtok, output_per_mtok}], override_native: bool}` (default: grok-4.5 list rates / `false`) | Operator rate card behind app-side cost **estimates** (`adr/0021`). Rates are USD per 1M tokens, matched by longest `model_prefix`; unknown models are never priced. `override_native` flips DISPLAY surfaces to prefer the rate-card computation over harness-reported cost. The derived `rate_card_id` (`builtin-v1` or `operator:<hash8>`) labels every stamped estimate. |
| `active_prompt_templates` | `dict[str, str]` (default `{}`) | Per-Mission-Type active prompt template name; missing key ⇒ built-in `"default"`. |
| `active_devtype_prompts` | `dict[str, str]` (default `{}`) | Per-Dev-Type active identifying-prompt template name; missing key ⇒ `"Development"` (the seeded original). Keys for deleted Dev Types are dropped on DELETE, stripped from export/profile snapshots, and pruned (with a warning) on PUT `/config` and bundle apply — never a hard 422, because `deep_merge` cannot remove dict keys from a partial SPA patch. |
| `dismissed_alerts` | `list[str]` (default `[]`) | Admin-UI state: dismissed advisory alerts as `"id:signature"` strings. A list (not a dict) on purpose — `deep_merge` can't delete dict keys, so the UI un-dismisses by PUTting the whole replacement list. |

## 10. TokenReport

Produced once per Dev run by the harness template's extraction strategy (`08-harness-templates.md` §5) and (a) posted to the activity feed as a message (INV-5), (b) attached to the `dev.run` span and metrics (`12-observability.md`).

Since `adr/0029` this is **TokenReport v1**: one CLOSED shape from every
extractor — every key always present (`None` = unknown, never an absent key),
provenance as the `source` field instead of key-presence folklore.

| Field | Type | Notes |
|---|---|---|
| `schema` | `1` | The shape version. |
| `model` | `str \| None` | Dominant model per `modelUsage` where reported, else the harness name. |
| `input_tokens` | `int \| None` | |
| `output_tokens` | `int \| None` | |
| `cache_read_tokens` | `int \| None` | |
| `cache_write_tokens` | `int \| None` | claude `cache_creation_input_tokens`; codex `cache_write_input_tokens` (**new at 0.146.0** — earlier codex streams read None, never a fabricated 0); grok has no write counter. |
| `total_tokens` | `int \| None` | REPORTED-only (never derived by the harness layer). Grok fills **both** total and split: at **0.2.112** its `end` event carries `usage {input_tokens, cache_read_input_tokens, output_tokens, reasoning_tokens, total_tokens}` plus `num_turns` and `modelUsage` inline, and that is what the entrypoint reads (`08-harness-templates.md` §1/§5). Total-only is what the retained `signals.json` fallback yields (`contextTokensUsed`, verified **v0.2.93**). Field names and their presence are CLI evidence; captured numbers came from a stub backend and are not. |
| `reasoning_tokens` | `int \| None` | A SUBSET of `output_tokens`, informational, never priced (pre-v1 it hid in a regex-parsed `notes` string). |
| `num_turns` | `int \| None` | Harness-reported turn count (claude/grok terminal events; codex none). |
| `duration_ms` | `int \| None` | Harness-reported wall time (claude result event; others none). |
| `cost_usd_native` | `float \| None` | Only when the harness reports it natively (Claude Code `total_cost_usd`). Never guessed **by the harness layer** — neither `codex` 0.146.0 nor `grok` 0.2.112 emits any cost field, so it stays null for both, forever untouched by estimation (`adr/0021`). |
| `cost_usd_estimated` | `float \| None` | Ships `None` from the image; the **app-side** rate-card estimate (`domain/costing.py`, `adr/0021`) fills it at finalize when the full input/cache-read/output split exists AND `config.cost_inputs.rates` maps the model. Kept strictly separate from `cost_usd_native`. |
| `rate_card_id` | `str \| None` | App-stamped alongside the estimate: `builtin-v1` (shipped defaults) or `operator:<hash8>` (edited card). Present iff the estimate is. |
| `source` | `"session_json" \| "end_event" \| "signals" \| "cumulative" \| "mixed" \| "unavailable"` | Which path actually filled the report — `end_event` grok's terminal stdout event, `session_json` the harness's own JSON (claude/codex), `signals` grok's session-file fallback, `cumulative` a codex resume chain (the harness's counters are cumulative; last-wins), `mixed` a multi-chain merge with disagreeing inputs; silence is never acceptable (INV-5 — `unavailable` is explicit). |
| `raw` | `dict` | The vendor usage payload, untouched (fidelity); merges carry `{"invocations": [...]}`. The only nested field. |

## 11. Decomposition drafts

The entry schema of the decomposition manifest an ONBOARD Dev emits per decomposed child Mission (`result.json` → `decomposition: [...]`, `03-mission-lifecycle.md` §6). Deliberately **not a pydantic model** — the entries are plain dicts validated by the orchestrator, which feeds each one to `PMOPort.create_mission(team_ref, title, description, priority, label_names, parent_ref)`:

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | Also the idempotency key on resume: a child whose title already exists is not re-created. |
| `description` | `str` | Must read as a **standalone** mission: no references to sibling missions or to "this mission" (`03-mission-lifecycle.md` §2.3). The app appends a provenance footer (`Created by DevCake from {key} — part i/n`). |
| `priority` | `"urgent" \| "high" \| "medium" \| "low"` | The playbook requires an explicit priority per draft; the app defaults a missing value to `medium`. |
| `blocked_by` | `list[int]` | Optional. 1-based indexes of **earlier** drafts in the same decomposition that must finish before this one starts (`03-mission-lifecycle.md` §1.3). Earlier-only is validated by the app and structurally prevents cycles; the app wires the corresponding PMO relation immediately after creating each child. |

`parent_ref` is **not** part of the draft — the app itself passes `create_mission`'s `parent_ref`: for a decomposed **Project**, its own `pmo_id` (children land inside it); for a decomposed **Issue**, the issue's containing project when it has one (`adr/0012` — the family stays in the project, and the tracking sweep waits for the grandchildren). The app adds the `DEVCAKE-CREATED` label (plus `DEVCAKE` in opt-in mode) on creation; the Dev does not manage labels (INV-4). The machine marker in each child's footer — `devcake:decomposition:v1 parent={pmo_id} manifest={sha256} part={i}/{n} depth={d}` — is the PMO-held record of lineage **and** generation: `depth` is optional in the parse (a marker without it is a level-1 child from the depth-1 era), and the canceled original carries the reverse pointer as a description note (`_Decomposed by DevCake into {children keys}_`, appended via `PMOPort.append_description`).
