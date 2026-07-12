# 02 — Domain Model

> **Audience:** implementers. The code's `models.py` (pydantic) must match this document 1:1 — field names included.
> **Depends on:** `00-overview.md` (glossary, invariants).

## 1. Mission

A **Mission** is a normalized DTO produced by the PMO adapter from a live Linear Project or Issue. Per INV-1 it is **never persisted authoritatively** — it is re-derived from the PMO System on every poll cycle and re-read live at dispatch and finalization time.

| Field | Type | Notes |
|---|---|---|
| `pmo_id` | `str` | The PMO System's stable ID (Linear UUID). Primary key. |
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

Rows 7, 8, and 10 take precedence over rows 1–4; row 6 over everything except 5.

> **Note on row 9:** ONBOARD is only derived from `backlog` + no stage label. This guarantees DevCake never "adopts" work a human has independently started (`in_progress` with no DevCake labels).

## 3. State machine

```
                        ┌───────────────────────────────────────────────────────┐
                        │                        ONBOARD                        │
                        │  (backlog, no stage label; opt-in gate passed)        │
                        └───────┬────────────────┬─────────────────┬────────────┘
                        trivial │         normal │            high │ complexity
                                ▼                ▼                 ▼
                     PR opened +          + DEVCAKE-PLAN    decompose: create child
                     + DEVCAKE-REVIEW           │           Missions (DEVCAKE-CREATED);
                     (skips PLAN+EXECUTE,       │           Issue → Canceled,
                      never skips REVIEW)       │           Project → + DEVCAKE-TRACKING
                                │               ▼
                                │  ┌───────────────────────────────┐
                                │  │             PLAN              │ → upload PLAN.md
                                │  └───────────────┬───────────────┘
                                │                  │ swap DEVCAKE-PLAN → DEVCAKE-EXECUTE
                                │                  ▼
                                │  ┌───────────────────────────────┐
                                │  │            EXECUTE            │◄────────────┐
                                │  └───────────────┬───────────────┘             │
                                │                  │ swap → DEVCAKE-REVIEW       │
                                ▼                  ▼                             │
                        ┌───────────────────────────────┐  reject: swap → EXECUTE│
                        │            REVIEW             │────(+ warning every ───┘
                        └───────────────┬───────────────┘      3rd loop)
                                        │ approve
                                        ▼
                        remove DEVCAKE-REVIEW, approve PR, then:
                        · auto_merge ON:  merge PR → on success mark Done
                                          (merge fails → + DEVCAKE-MERGE + warning)
                        · auto_merge OFF: + DEVCAKE-MERGE (await human merge)
                                        │
                                        ▼
                        ┌───────────────────────────────┐
                        │  merge sweep (every poll):    │
                        │  PR merged → Done, drop label │
                        │  PR closed unmerged → Canceled│
                        └───────────────────────────────┘
```

**A Mission only reaches Done through a merged PR** (or through decomposition/human action) — merge always precedes the Done status, in every path.

**All transitions are label/status writes to the PMO System performed by the app** (never by the Dev — INV-4) during run finalization, using the compare-and-transition procedure of `04-orchestrator.md` §4. The playbook for each state is `03-mission-lifecycle.md`.

## 4. Priority ordering

`urgent` > `high` > `medium` > `low`. A Mission with no priority set in the PMO System is treated as `medium`. Priority is read live at dispatch time, never from the poll cache (INV-1, `04-orchestrator.md` §3).

## 5. Managed labels (the complete set)

Defined here and only here; code keeps them in a single constants module. The app ensures all nine exist in the configured Linear team at startup (`05-pmo-adapter.md` §5).

| Label | Class | Meaning |
|---|---|---|
| `DEVCAKE` | opt-in | In `opt_in` adoption mode (the default), only Missions carrying this label are adopted by DevCake. Ignored in `opt_out` mode. |
| `DEVCAKE-PLAN` | stage | Mission awaits a PLAN step. |
| `DEVCAKE-EXECUTE` | stage | Mission awaits an EXECUTE step. |
| `DEVCAKE-REVIEW` | stage | Mission awaits a REVIEW step. |
| `DEVCAKE-MERGE` | awaiting-merge | REVIEW approved; the PR awaits merging (by a human, or after an `auto_merge` failure). The poll sweep watches the PR and completes the Mission when it merges (`04-orchestrator.md` §1). |
| `DEVCAKE-CREATED` | provenance | This Mission was created by DevCake (decomposition output). Coexists with stage labels. |
| `DEVCAKE-FAILED` | attention | A step failed `max_attempts` (default 3) times. DevCake will not touch the Mission until a human removes the label. |
| `DEVCAKE-SKIP` | opt-out | A human told DevCake to ignore this Mission entirely (works in both adoption modes). |
| `DEVCAKE-TRACKING` | tracking | A decomposed Project awaiting auto-completion once all its child Issues reach Done/Canceled. |

Naming is flat and uppercase; there are no version suffixes. Renaming a label is a documented migration (create new → copy → retire old), per `adr/0004-label-namespace-and-versioning.md`.

## 6. DevType

Persisted as one YAML file per Dev Type at `/data/config/dev_types/{name}.yaml` (`10-persistence.md`), CRUD-ed via the admin panel.

| Field | Type | Notes |
|---|---|---|
| `name` | `str` | e.g. `senior-dev`, `main-dev` (kebab-case slug; display name derived). |
| `harness_template` | `"claude-code" \| "grok-build" \| "codex"` | See `08-harness-templates.md`. |
| `identifying_prompt` | `str` | Always delivered to the harness at the start of every run, before the playbook prompt. |
| `mcp_setup_commands` | `list[str]` | Shell commands run by the Dev entrypoint before harness launch (e.g. `claude mcp add …`). Failure ⇒ exit code 14. |
| `credential` | `{kind: "env_api_key" \| "credentials_json", ref: str}` | `env_api_key`: `ref` names an env var resolved from the app's environment at dispatch. `credentials_json`: `ref` is the filename under `/data/secrets/{name}/`; its content is delivered to the Dev in the run spec and written to the harness-specific path by the entrypoint (`08-harness-templates.md`, `09-messaging.md` §3). OAuth/subscription credentials preferred. |
| `max_concurrency` | `int` | Per-type cap (see `04-orchestrator.md` §3). |
| `docker_image` | `str` | Defaults from the harness template; overridable. |
| `model` | `str` | Pins the harness model (added 2026-07-12 after Claude Code silently defaulted to Sonnet). Delivered via runspec as `DEVCAKE_MODEL`; the entrypoint maps it to the harness flag (`claude --model` / `codex -m` / `grok --model`). Empty = harness default. Seed: `senior-dev` = `claude-fable-5`. Per-assignment `extra_cli_args` can still override (appended after the pin). |

**v0 defaults:**

| Dev Type | Template | Mission Types |
|---|---|---|
| `senior-dev` ("Senior Dev") | `claude-code` (Claude Fable) | ONBOARD, PLAN, REVIEW |
| `main-dev` ("Main Dev") | `grok-build` (Grok 4.5) | EXECUTE |

The Mission-Type→Dev-Type assignment lives in `AppConfig.assignments` (§9); each Mission Type maps to exactly one Dev Type; a Dev Type may serve any number of Mission Types.

## 7. Run

The locally persisted record of one Mission Step attempt, one JSON file per run at `/data/state/runs/{run_id}.json`. **Telemetry and dispatch bookkeeping only** — wiping it never corrupts Mission state (INV-1); the documented consequence is reset attempt counters (`10-persistence.md` §5).

| Field | Type | Notes |
|---|---|---|
| `run_id` | `str` | Human-readable and unique: `{mission_key}-{seq}-{TYPE}-{6-char ULID suffix}`, e.g. `ENG-142-3-EXECUTE-9GX2TQ` (charset `[-A-Za-z0-9_]`, ≤ 64 chars — fits Dagu's `dagRunId` rules). Also the Dagu run ID and the Dev container name suffix, so Linear, the Dagu UI, `docker ps`, traces, and Redis streams all speak the same name (confirmed decision). |
| `mission_pmo_id` | `str` | |
| `mission_key` | `str` | Denormalized for log/trace readability. |
| `mission_type` | enum | The type this run was dispatched as. |
| `dev_type` | `str` | |
| `seq` | `int` | Step number for transcript naming (§8). |
| `attempt_of_step` | `int` | 1-based attempt counter for this (mission, type, seq). |
| `dagu_run_id` | `str \| None` | Returned by the Dagu API on trigger. |
| `state` | `"dispatched" \| "running" \| "finalizing" \| "finished" \| "failed" \| "timed_out" \| "orphaned"` | |
| `started_at` / `ended_at` | `datetime \| None` | |
| `exit_code` | `int \| None` | Per the table in `07-dev-runtime.md` §4. |
| `stage_label_at_dispatch` | `str \| None` | Input to compare-and-transition (`04-orchestrator.md` §4). |
| `finalized_steps` | `list[str]` | Idempotency checklist: which finalization side effects have durably completed (e.g. `["transcript", "token_report"]`). |
| `token_report` | `TokenReport \| None` | §10. |
| `error` | `str \| None` | Mapped error class + message (`15-errors-and-retries.md`). |

## 8. `seq` derivation rule (normative)

`seq` = (number of prior DevCake step artifacts — comments or attachments named `N_TYPE.md` — present in the Mission's activity feed) + 1, computed at workspace-preparation time from the live feed. This makes transcript numbering robust to local-state loss and is the same counter used to name `{seq}_{TYPE}.md` (e.g. `5_EXECUTE.md`). A retried attempt of the same step reuses the same `seq` only if the prior attempt posted no transcript; otherwise it naturally increments.

## 9. AppConfig

Persisted at `/data/config/config.yaml` (full annotated example in `10-persistence.md` §3). Shape:

| Field | Type | Notes |
|---|---|---|
| `pmo` | `{system: "linear", api_key_env: str, team_key: str}` | |
| `adoption_mode` | `"opt_in" \| "opt_out"` (default `opt_in`) | `opt_in`: only Missions labeled `DEVCAKE` are adopted. `opt_out`: every non-terminal item in the team is adopted (the original mission-doc behavior — enable deliberately; the admin panel warns about the backlog-wide consequence, `11-admin-panel.md` §2). |
| `repo` | `{forge: "github" \| "gitlab", url: str, token_env: str, reviewer_token_env: str \| None}` | The single configured repository. `reviewer_token_env` is the optional second credential used for formal PR approvals. |
| `assignments` | `dict[MissionType, {dev_type: str, extra_cli_args: str}]` | Mission Type → Dev Type name, plus optional **extra CLI args** appended verbatim to the harness invocation for that Mission Type (`08-harness-templates.md` §1). Args are admin-set data, never hardcoded — they are harness-specific, so the admin UI warns and offers to clear them when the Mission Type is reassigned to a Dev Type with a different harness (`11-admin-panel.md` §2). Validation: all four types assigned. |
| `concurrency` | `{global_max: int}` | Per-type caps live on each DevType. Effective ceiling = min(global_max, Σ per-type) — this is a property of the dispatch check, not a separate rule. |
| `dev_timeout_minutes` | `int` (default 120) | Enforced by the app watchdog (`04-orchestrator.md` §5), not by Dagu. |
| `poll_interval_seconds` | `int` (default 30) | |
| `auto_merge` | `bool` (default `false`) | When true, DevCake merges its own PRs with no human intervention at the two Done-producing transitions (trivial ONBOARD, REVIEW approval). See `03-mission-lifecycle.md`, `06-forge-adapter.md`, `14-security.md`. |
| `review_loop_warning_every` | `int` (default 3) | Post a cost warning every Nth REVIEW→EXECUTE rejection. |
| `max_attempts` | `int` (default 3) | Failed attempts of the same step before `DEVCAKE-FAILED`. |

## 10. TokenReport

Produced once per Dev run by the harness template's extraction strategy (`08-harness-templates.md` §5) and (a) posted to the activity feed as a message (INV-5), (b) attached to the `dev.run` span and metrics (`12-observability.md`).

| Field | Type | Notes |
|---|---|---|
| `input_tokens` | `int \| None` | |
| `output_tokens` | `int \| None` | |
| `cache_read_tokens` | `int \| None` | |
| `cache_write_tokens` | `int \| None` | |
| `total_tokens` | `int \| None` | For harnesses that only expose a total (Grok v0.2.93 `contextTokensUsed`); filled alongside or instead of the split. Tokens are the primary cost signal; billed cost is best-effort on top. |
| `cost_usd` | `float \| None` | Only when the harness reports it natively (Claude Code `total_cost_usd`) or the model's prices are known from the price table in `08-harness-templates.md`. Never guessed. |
| `model` | `str` | |
| `extraction_method` | `"session_json" \| "stdout_parse" \| "unavailable"` | |
| `notes` | `str \| None` | e.g. which fallback triggered. |

## 11. MissionDraft

The payload an ONBOARD Dev emits per decomposed child Mission (inside `result.json`, `03-mission-lifecycle.md` §6) and the app feeds to `PMOPort.create_mission`:

| Field | Type | Notes |
|---|---|---|
| `title` | `str` | |
| `description` | `str` | Must read as a **standalone** mission: no references to sibling missions or to "this mission" (`03-mission-lifecycle.md` §2.3). |
| `priority` | `"urgent" \| "high" \| "medium" \| "low"` | Required — every decomposed Mission gets an explicit priority. |
| `parent_ref` | `str \| None` | Project `pmo_id` when the children belong inside a decomposed Project. |

The app adds the `DEVCAKE-CREATED` label on creation; the Dev does not manage labels (INV-4).
