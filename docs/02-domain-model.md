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
                        · auto_merge ON:  merge PR → on success mark Done
                            (conflict + auto-resolve ON, < 2 tries →
                             swap → EXECUTE with a resolve directive;
                             not-mergeable-yet → + DEVCAKE-MERGE, sweep
                             retries for merge_retry_window_minutes;
                             else → + DEVCAKE-MERGE + warning)
                        · auto_merge OFF: + DEVCAKE-MERGE (await human merge)
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
| `name` | `str` | e.g. `senior-dev`, `main-dev` (kebab-case slug; display name derived). |
| `harness_template` | `"claude-code" \| "grok-build" \| "codex"` | **Authoritative** (2026-07-12 rework): the Docker image, credential requirements, and OAuth flow all derive from it via the harness registry (`app/devcake/harness.py`, `08-harness-templates.md` §2/§4). Changing it in the admin panel changes what actually runs. |
| `identifying_prompt` | `str` | Always delivered to the harness at the start of every run, before the playbook prompt. |
| `mcp_setup_commands` | `list[str]` | Shell commands run by the Dev entrypoint before harness launch — the MCP-plugin install/register lines (`08-harness-templates.md` §7). Delivered as a top-level runspec key, live-read at `runspec.get` (like the secret half — an edit applies to the next run without redispatch). Failure or 300 s per-command timeout ⇒ exit code 14, `DEV_MCP_SETUP` (`15-errors-and-retries.md` §1). |
| `skills` | `list[str]` | Skill-store skills installed in the Dev container before harness launch, into the harness's registry-declared skills directory (`harness.py` `skills_dir`: claude-code → `~/.claude/skills`; grok-build/codex → `~/.agents/skills` — `08-harness-templates.md` §7a). A harness with no `skills_dir` skips them with a warning. Names validated (`^[a-z0-9][a-z0-9_-]{0,63}$`), deduped preserving order. A selected-but-missing skill is skipped with a warning, never a refused run. |
| `secret_env` | `list[str]` | **Names** of GUI-stored secrets (`/data/secrets/harness/{VAR}.json`) delivered into the run's env — mission-tooling credentials referenced as `$VAR` from `mcp_setup_commands` (e.g. `DD_API_KEY` for a log-platform plugin). UPPER_SNAKE_CASE, ≤64 chars; reserved names refused (`PATH`/`HOME`, `REDIS_*`, `TRACEPARENT`, the forge CLI tokens `GH_TOKEN`/`GITLAB_TOKEN`/`GITEA_SERVER_TOKEN`, and the `DEVCAKE_*`/`OTEL_*`/`GIT_*` prefixes — they would shadow the protocol env). Missing value: **referenced** by a setup command ⇒ dispatch gate (`14-security.md` §8); unreferenced ⇒ warn-and-proceed. Global store: one stored value serves any number of Dev Types. |
| `max_concurrency` | `int` | Per-type cap (see `04-orchestrator.md` §3). |
| `model` | `str` | Pins the harness model (added 2026-07-12 after Claude Code silently defaulted to Sonnet). Delivered via runspec as `DEVCAKE_MODEL`; the entrypoint maps it to the harness flag (`claude --model` / `codex -m` / `grok --model`). Empty = harness default. Seed: `senior-dev` = `claude-fable-5`. Per-assignment `extra_cli_args` can still override (appended after the pin). |

There is deliberately **no stored `docker_image` or credential config**: requirements are per-harness (registry), while secret *material* stays per Dev Type under `/data/secrets/{name}/` — so two Dev Types on the same harness can hold different accounts. Legacy YAML keys (`docker_image`, `credential_env`, `credential_files`) are ignored on load and dropped on the next save.

**v0 defaults:**

| Dev Type | Template | Mission Types |
|---|---|---|
| `senior-dev` ("Senior Dev") | `claude-code` (Claude Fable) | ONBOARD, PLAN, REVIEW |
| `main-dev` ("Main Dev") | `grok-build` (Grok 4.5) | EXECUTE |
| `junior-dev` ("Junior Dev") | `claude-code` (`claude-haiku-4-5`) | Relations Mapper (default vehicle; assignable anywhere) |

Default Dev Types are **re-seeded by name** whenever their YAML is missing (boot-time top-up) — customize a default by editing it, not deleting it; a deleted default returns on the next boot.

The Mission-Type→Dev-Type assignment lives in `AppConfig.assignments` (§9); each Mission Type maps to exactly one Dev Type; a Dev Type may serve any number of Mission Types.

## 7. Run

The locally persisted record of one Mission Step attempt, one JSON file per run at `/data/state/runs/{run_id}.json`. **Telemetry and dispatch bookkeeping only** — wiping it never corrupts Mission state (INV-1); the documented consequence is reset attempt counters (`10-persistence.md` §5).

| Field | Type | Notes |
|---|---|---|
| `schema_version` | `int` (= 2) | Bumped by the credential-state hardening (secrets left the record). Records `< 2` are quarantined at boot, never migrated (`10-persistence.md` §5). The `pmo_ref`/`repo_ref` additions were additive with defaults — no bump of their own. |
| `run_id` | `str` | Human-readable and unique: `{INSTANCE}-{key}-{seq}-{TYPE}-{6-char ULID suffix}`, e.g. `LINEAR-ENG-142-3-EXECUTE-9GX2TQ` (charset `[-A-Za-z0-9_]`, ≤ 64 chars — fits Dagu's `dagRunId` rules). The uppercased PMO-instance prefix (schema v3) keeps run ids — and therefore ACL users, container names, and reply streams — collision-free across instances. Also the Dagu run ID and the Dev container name suffix, so Linear, the Dagu UI, `docker ps`, traces, and Redis streams all speak the same name (confirmed decision). HELLO/OAUTH runs use the fixed pseudo-instance `sys`. |
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
| `pmos` | `list[PMOInstance]` — `{name, system, team_key, api_base, repos, reference_repos, …}` | Instance `name` is the identity. `system` validated against `PMO_SYSTEMS`. **`api_key` is a read-through** over the GUI secret store (`/data/secrets/connections/pmo-{name}.json`) — not an env-var name. `repos` is the ordered work-repo set for that instance (first = default for unmarked missions); `reference_repos` are read-only consultation clones, disjoint from the work set. |
| `repos` | `list[RepoInstance]` — `{name, forge, url, api_base, default_branch, …}` | `forge` validated against `forges()`. **`token` / `token_ro` / `reviewer_token` are read-throughs** over `/data/secrets/connections/repo-{name}.json` — not `*_env` fields. |
| `adoption_mode` | `"opt_in" \| "opt_out"` (default `opt_in`) | `opt_in`: only Missions labeled `DEVCAKE` are adopted. `opt_out`: every non-terminal item in the team is adopted (the original mission-doc behavior — enable deliberately; the admin panel warns about the backlog-wide consequence, `11-admin-panel.md` §3). |
| `assignments` | `dict[MissionType, {dev_type: str, extra_cli_args: str}]` | Mission Type → Dev Type name, plus optional **extra CLI args** appended verbatim to the harness invocation for that Mission Type (`08-harness-templates.md` §1). Args are admin-set data, never hardcoded — they are harness-specific, so the admin UI warns and offers to clear them when the Mission Type is reassigned to a Dev Type with a different harness (`11-admin-panel.md` §3). Validation: all four types assigned. |
| `concurrency` | `{global_max: int}` | Per-type caps live on each DevType. Effective ceiling = min(global_max, Σ per-type) — this is a property of the dispatch check, not a separate rule. |
| `dev_timeout_minutes` | `int` (default 120) | Enforced by the app watchdog (`04-orchestrator.md` §5), not by Dagu. |
| `poll_interval_seconds` | `int` (default 30) | |
| `auto_merge` | `bool` (default `false`) | When true, DevCake merges its own PRs with no human intervention at the Done-producing transition (REVIEW approval). See `03-mission-lifecycle.md`, `06-forge-adapter.md`, `14-security.md`. |
| `auto_resolve_merge_conflicts` | `bool` (default `true`) | Inert while `auto_merge` is off. On a merge conflict (or stale branch), route the Mission back to EXECUTE with a sync-and-resolve directive instead of parking on `DEVCAKE-MERGE`; max 2 attempts per Mission, counted from feed markers (`03-mission-lifecycle.md` §4.1). |
| `merge_retry_window_minutes` | `int ≥ 0` (default 30) | Inert while `auto_merge` is off. When a merge is not possible *yet* (CI running, mergeability computing), the merge sweep keeps retrying for this long before the human hand-off; 0 = hand off immediately. Lower on CI-light repos, raise on CI-heavy ones. |
| `review_loop_warning_every` | `int` (default 3) | Post a cost warning every Nth REVIEW→EXECUTE rejection. |
| `max_attempts` | `int` (default 3) | Failed attempts of the same step before `DEVCAKE-FAILED`. |
| `intake_paused` | `bool` (default `false`) | Operator switch (`11-admin-panel.md` §0): while true, no NEW runs dispatch (missions or mapper). In-flight runs finish, results finalize, and the merge/tracking sweeps keep running. Hot-applied next poll cycle. |
| `max_decomposition_depth` | `int` ≥ 0 (default 2) | How many generations of ONBOARD decomposition are allowed below a root (`adr/0012`). `0` = unlimited — the ONBOARD Dev decides (`03-mission-lifecycle.md` §1.3). |
| `relations_mapper` | `{enabled: bool, interval_minutes: int, dev_type: str \| None}` (default off/60/`junior-dev`) | The Relations Mapper (`03-mission-lifecycle.md` §4b): manual-only by default ("Run now"); the periodic service is opt-in. `dev_type` must name an existing Dev Type whenever `enabled`; deleting the referenced Dev Type is refused (409). |
| `active_prompt_templates` | `dict[str, str]` (default `{}`) | Per-Mission-Type active prompt template name; missing key ⇒ built-in `"default"`. |
| `active_devtype_prompts` | `dict[str, str]` (default `{}`) | Per-Dev-Type active identifying-prompt template name; missing key ⇒ `"Development"` (the seeded original). |
| `dismissed_alerts` | `list[str]` (default `[]`) | Admin-UI state: dismissed advisory alerts as `"id:signature"` strings. A list (not a dict) on purpose — `deep_merge` can't delete dict keys, so the UI un-dismisses by PUTting the whole replacement list. |

## 10. TokenReport

Produced once per Dev run by the harness template's extraction strategy (`08-harness-templates.md` §5) and (a) posted to the activity feed as a message (INV-5), (b) attached to the `dev.run` span and metrics (`12-observability.md`).

| Field | Type | Notes |
|---|---|---|
| `input_tokens` | `int \| None` | |
| `output_tokens` | `int \| None` | |
| `cache_read_tokens` | `int \| None` | |
| `cache_write_tokens` | `int \| None` | |
| `total_tokens` | `int \| None` | For harnesses that only expose a total (Grok v0.2.93 `contextTokensUsed`); filled alongside or instead of the split. Tokens are the primary cost signal; billed cost is best-effort on top. |
| `cost_usd` | `float \| None` | Only when the harness reports it natively (Claude Code `total_cost_usd`). Never guessed. |
| `model` | `str` | |
| `extraction_method` | `"session_json" \| "unavailable"` | The entrypoint records which path filled the report; silence is never acceptable (INV-5). |
| `notes` | `str \| None` | e.g. which fallback triggered. |

## 11. Decomposition drafts

The entry schema of the decomposition manifest an ONBOARD Dev emits per decomposed child Mission (`result.json` → `decomposition: [...]`, `03-mission-lifecycle.md` §6). Deliberately **not a pydantic model** — the entries are plain dicts validated by the orchestrator, which feeds each one to `PMOPort.create_mission(team_ref, title, description, priority, label_names, parent_ref)`:

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | Also the idempotency key on resume: a child whose title already exists is not re-created. |
| `description` | `str` | Must read as a **standalone** mission: no references to sibling missions or to "this mission" (`03-mission-lifecycle.md` §2.3). The app appends a provenance footer (`Created by DevCake from {key} — part i/n`). |
| `priority` | `"urgent" \| "high" \| "medium" \| "low"` | The playbook requires an explicit priority per draft; the app defaults a missing value to `medium`. |
| `blocked_by` | `list[int]` | Optional. 1-based indexes of **earlier** drafts in the same decomposition that must finish before this one starts (`03-mission-lifecycle.md` §1.3). Earlier-only is validated by the app and structurally prevents cycles; the app wires the corresponding PMO relation immediately after creating each child. |

`parent_ref` is **not** part of the draft — the app itself passes `create_mission`'s `parent_ref`: for a decomposed **Project**, its own `pmo_id` (children land inside it); for a decomposed **Issue**, the issue's containing project when it has one (`adr/0012` — the family stays in the project, and the tracking sweep waits for the grandchildren). The app adds the `DEVCAKE-CREATED` label (plus `DEVCAKE` in opt-in mode) on creation; the Dev does not manage labels (INV-4). The machine marker in each child's footer — `devcake:decomposition:v1 parent={pmo_id} manifest={sha256} part={i}/{n} depth={d}` — is the PMO-held record of lineage **and** generation: `depth` is optional in the parse (a marker without it is a level-1 child from the depth-1 era), and the canceled original carries the reverse pointer as a description note (`_Decomposed by DevCake into {children keys}_`, appended via `PMOPort.append_description`).
