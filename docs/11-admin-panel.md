# 11 — Admin Panel: UI Spec and API Contract

> **Audience:** frontend implementer + app API implementer.
> **Depends on:** `02-domain-model.md` (AppConfig, DevType), `10-persistence.md` (write path), `13-deployment.md` (proxy topology).

Simple but beautiful: a static SPA (React + Vite + Tailwind, `admin/spa/`) served by nginx in the `admin` container, which reverse-proxies `/api/*` → `app:8000` (no CORS). A persistent **sidebar** hosts navigation and the mission-intake master switch; a tiny hash router drives **six pages**: **Overview** (`#/overview`), **Missions** (`#/missions`), **Runs** (`#/runs`), **Repositories** (`#/repos`), **Configuration** (`#/config/<section>`), **Logs** (`#/logs`). Config sections (from `admin/spa/src/lib/nav.js`): `pmo`, `dev-types`, `skills`, `assignments`, `prompts`, `profiles` (**Profiles & Export**), `limits`, `traffic` — **repository is not a Configuration section** (repos live on `#/repos`; the old `#/config/repository` hash redirects there). The Dagu and OpenObserve UIs are **not** embedded: the Runs and Logs pages open them in new browser tabs via buttons (confirmed decision — no iframes; their URLs reach the SPA as nginx-templated env vars `DAGU_UI_URL` / `OO_UI_URL` in `/config.js`, `13-deployment.md` §2). All confirmation dialogs are React components — never native `window.confirm`/`alert` (they block automation and the browser).

**Health polling is honest by design:** the SPA polls `GET /api/v1/health` every 10 s, keeps the last-known data on failure, and renders an unreachable backend **RED** (never gray/unknown) — the failure itself is the signal (founder decision, 2026-07-13).

## 0. Sidebar (persistent shell)

- Navigation to the six pages, with Config sub-entries for the sections above; collapsible to an icon rail.
- **Mission-intake master switch** — THE operational control, so it lives in the sidebar (visible even collapsed, founder decision) and applies immediately (its own `PUT /config`, outside the Config draft): OFF pauses intake — no new runs start on **any** PMO (missions or mapper) while the operator rearranges missions. In-flight runs finish normally (pause freezes dispatch, not consequence) and the merge/tracking sweeps keep running; flipping back resumes on the next poll cycle. Disabled (with an explanatory tooltip) while the backend is unreachable or health is unknown; save errors surface inline — the toggle never fails silently.
- **Per-PMO intake** — each *saved* PMO card on Configuration → PMO has an InstantZone toggle driven by `/health` (not the config draft). It calls `PUT /api/v1/config/pmos/{name}/intake` with `{paused}` — a narrow server-side flip that never rewrites the `pmos` list. Freezes only that instance's NEW dispatches; the sidebar master still freezes everything. Unsaved (draft-only) cards unlock the toggle after Save.
- Component health dots from the 10 s health poll: app / pmo / redis / dagu / **gitea** (`health.internal_forge`, grey when Gitea is unset) / logs (OpenObserve) — not a generic forge dot — plus a theme toggle.

## 1. REST API contract (`/api/v1`)

| Method + path | Purpose |
|---|---|
| `GET /api/v1/health` | Full component health (below) |
| `GET /api/v1/health/live` | Unauthenticated liveness (`{"app": true}`) — the compose healthcheck |
| `GET /api/v1/config` · `PUT /api/v1/config` | General settings (AppConfig minus dev types). PUT validates server-side (pydantic); errors return field-keyed messages surfaced inline. Nested dicts deep-merge, but the plural `pmos:`/`repos:` lists are **replaced whole**. **Exception:** `pmos[].intake_paused` is owned by the narrow intake endpoint below — when a list entry **omits** the key, the server inherits the live value by instance name so a draft Save cannot undo a pause. Stale-shaped bodies (singular `pmo`/`repo`, `id:` keys, `*_env` fields, non-v4 schema) are **rejected with 422** by `reject_stale_patch` (never silently dropped). A successful PUT hot-reloads both adapters (`reload_connections`) and re-ensures the managed labels |
| `PUT /api/v1/config/pmos/{name}/intake` | `{paused: bool}` — flip one saved PMO's intake switch in place (no list rewrite, no adapter reload). 404 if the name is unknown. SPA InstantZone control; state is read from `/health` `pmo_instances` |
| `GET /api/v1/harnesses` | The harness registry: derived image, credential requirements, OAuth availability per `harness_template` — the Dev Type editor renders (and previews unsaved harness switches) from this. The Dev Types section is a roster grid of tiles opening an editor modal (`admin/spa/DESIGN.md` §2); see `docs/img/devtypes-roster/` for UI evidence |
| `GET /api/v1/dev-types` · `POST /api/v1/dev-types` | List (enriched: `harness` info + `secrets_present`) / create Dev Types |
| `POST /api/v1/oauth/dev-types/{name}/start` · `GET /api/v1/oauth/status/{run_id}` | Per-dev-type device-code login; credential lands in `/data/secrets/{name}/` |
| `PUT/DELETE /api/v1/dev-types/{name}` | Update / delete one Dev Type. DELETE refuses while assigned to a Mission Type (or to the Relations Mapper); on success it also drops `active_devtype_prompts` keys and removes that Dev Type's prompt-template **and credential** directories (`/data/secrets/{name}/`). Shared harness keys and connection secrets are untouched (use Clear secrets) |
| `POST /api/v1/dev-types/{name}/credentials` | JSON `{"filename": "...", "content": "..."}` → stored to `/data/secrets/{name}/{filename}` (0600); a fresh credential clears that Dev Type's auth breaker |
| `GET /api/v1/assignments` · `PUT /api/v1/assignments` | The **global** Mission-Type → Dev-Type map. Validation: all four types assigned, each to exactly one existing Dev Type. Per-instance override rows (ADR-0019) ride `pmos[*].assignments` in PUT `/config` instead — mission-type keys validated there; dev-type existence checked at bundle/profile apply and inline in the SPA (same split as the global map) |
| `PUT/DELETE /api/v1/secrets/{scope}/{instance}/{field}` | Write/delete connection secret **VALUES** (pmo `api_key`; repo `token`/`token_ro`/`reviewer_token`) — never echoed (`14` §4, ADR-0011) |
| `PUT/DELETE /api/v1/harness-secrets/{VAR}` | Write/delete harness/model key VALUES |
| `GET /api/v1/secrets-check` | Presence + `updated_at` only (no values, no fingerprints) — powers Config ✓/✗ |
| `GET /api/v1/secrets/inventory` | Presence-only catalog of clearable secrets (harness keys, connection fields, per-Dev-Type credential files). Never values. Excludes profile snapshots and `internal_forge` mission tokens |
| `POST /api/v1/secrets/clear` | Delete an operator-selected subset: body `{harness, connections, credential_files, pause_intake?}`. At least one secret required; invalid refs 422 before side effects. **Order:** optional master `intake_paused` first (fail aborts with no deletes), then deletes, breakers, connection reload, audit. `pause_intake` omitted ⇒ **false** (API-safe for non-SPA clients). SPA ConfirmDialog checkbox defaults **on** and always sends an explicit bool. Response `{ok, deleted, intake_paused}` |
| `GET /api/v1/connections/registry` | Adapter registry metadata: PMO systems + forges, `secret_shape_prefixes` (paste guard), `managed_labels_expected` |
| `POST /api/v1/connections/pmo/{name}/test` | Live probe for one named PMO instance: auth + team fetch; returns `{ok, team, labels, labels_expected, missions_visible}` — `labels` counts the intersection with DevCake's managed label set |
| `POST /api/v1/connections/forge/{name}/test` | Live probe for one named repo: authenticated repo fetch + explicit push permission + default branch (+ reviewer token check + branch-protection state). A read-only or fine-grained token that omits the configured repository returns `ok: false` and can latch the **per-repo** forge breaker (`repo:{name}`); transient probe failures (5xx/network/rate-limit) are reported but never latch the breaker, and a latched breaker re-probes every poll cycle (`15-errors-and-retries.md` §4) |
| `GET /api/v1/runs?mission_key=…&pmo_ref=…&created_from=…&created_to=…&sort=…&dir=…&group_by=…&limit=…&offset=…` | Read-only run history (from `/data/state/runs/`). Filters compose: `mission_key` substring, `pmo_ref` exact, `created_*` ISO dates/datetimes **interpreted as UTC** (date-only `to` is end-inclusive; unparseable → 400). `sort` ∈ started · duration · input_tokens · output_tokens · cache_read_tokens · cache_write_tokens · cost (+ `dir` asc/desc, default desc) orders the **whole filtered set server-side**, nulls always last; `cost` sorts the effective value under the current rate card; `started` keeps dispatch order (`created_at` — not-yet-started runs must not sink). `group_by=mission` switches the response to mission groups `{pmo_ref, mission_key, run_count, subtotal, runs}`: pagination counts **missions** (`total` = groups, `total_runs` = runs), the active sort orders groups by their aggregate, runs inside a group stay in pipeline order (seq), and each `subtotal` follows the grand-totals null semantics. Bad `sort`/`dir`/`group_by` → 400. Response `{total, offset, limit, runs: […], totals, pmo_refs, rate_card}` — `totals` covers the **entire filtered set** (completed runtime seconds, five token sums, native / estimated / effective cost, plus `total_tokens_effective` — per run the harness-reported total when present else the arithmetic sum of the known splits, feeding the totals-row "· N tokens" label; a sum no run contributed to is `null` — rendered "—" — so an all-grok history shows cache-write "—", never a fabricated 0), `pmo_refs` lists every connector seen across all runs (dropdown source), `rate_card` = `{rate_card_id, override_native}`. Rows carry **token/cost scalars** (`model`, five token counts, `cost_usd`, `cost_usd_estimated` — the latter recomputed at read time from the current `cost_inputs`, `adr/0021` §4) |
| `GET /api/v1/runs/{run_id}` | Fixed allowlist of operational Run fields (incl. `verdict`) plus the same token/cost scalars; run specs, prompts, results, the raw token_report dict (with `notes`), envelope verifiers, and credential material are never serialized |
| `GET /api/v1/runs/{run_id}/log?tail=N` | Plain-text condensed run output (from `/data/state/runlogs/`, relayed live by the Dev via `run.log` — `09-messaging.md` §3) |
| `GET /api/v1/runs/{run_id}/log/stream` | SSE follow of the same log: replays the stored lines, then streams new ones until the run reaches a terminal state (`event: end`). Sends `X-Accel-Buffering: no` so nginx doesn't buffer; 15 s `: ping` heartbeats stay under nginx's 60 s read timeout |
| `POST /api/v1/runs/{run_id}/stop` | Stop one active run (dispatched/running); 409 once finalizing or terminal |
| `POST /api/v1/missions/{pmo_id}/actions` | Card MoreMenu ops: park / retry / resume / unpark (label mutations against the live PMO) |
| `POST /api/v1/missions/{pmo_id}/comment` | Drawer **Send guidance** — posts a human feed comment (attempt-counter reset input, `15` §3) |
| `GET /api/v1/prompt-templates` · `PUT/DELETE /api/v1/prompt-templates/{mission_type}/{name}` | Mission-type playbook templates; active selection rides `config.active_prompt_templates` |
| `PUT/DELETE /api/v1/devtype-prompts/{dev_type}/{name}` | Per-Dev-Type identifying-prompt templates; active selection rides `config.active_devtype_prompts` |
| `GET /api/v1/skills` · `GET /api/v1/skills/{name}` · `POST /api/v1/skills` · `POST /api/v1/skills/import` · `DELETE /api/v1/skills/{name}` · `POST /api/v1/skills/sync` | Skill-store catalog CRUD + View content + re-seed built-ins |
| `GET /api/v1/internal-repos` · `POST /api/v1/internal-repos/create` · `DELETE /api/v1/internal-repos/{name}` | Bundled-Gitea operator repos (list / create / clear one) |
| `POST /api/v1/dev-types/{name}/rename` | Rename a Dev Type (moves files, migrates breakers / active-prompt keys) |
| `POST /api/v1/system/stop-runs` | Stop every dispatched/running Dev via the run manager (full per-run teardown; each counts as a failed attempt). Finalizing runs are skipped — never killed — and named in the response. Nothing is deleted |
| `POST /api/v1/system/clear-runs` | Operator wipe: stop **and drain** in-flight Devs (wait for container exit, capped just past Dagu's 30 s SIGTERM grace — the later ACL sweep must never race a live Dev), delete local run records + audit log, purge Dagu run history, delete OpenObserve log/trace streams, **delete every `activity-*` repo on the internal Gitea** (ADR-0014 D4 — the PMO stays the source of truth; repo git history, incl. pre-edit feed states, is lost; the `activity-` prefix is reserved — never hand-create repos with it in `devcake-repos`, the sweep would delete them). Config + secrets + PMO + operator repos + skill-store + work repos untouched (`10-persistence.md` §5) |
| `POST /api/v1/relations-mapper/run` | Manually dispatch a Relations Mapper run (`03-mission-lifecycle.md` §4b). Works regardless of the `enabled` toggle (which governs only the interval service); 422 without a valid `dev_type`, 409 while a mapper run is active |
| `GET /api/v1/profiles` | Config profile rows (ADR-0013): counts + presence only, plus the last-applied breadcrumb and the divergence boolean. Never a secret value |
| `GET /api/v1/profiles/{name}` | One profile: metadata, full section A, a secrets **presence map**, and the apply-preview `diff` vs current settings |
| `POST /api/v1/profiles` | Save-current-as: snapshots the live settings + secret values under `{"name": …}`. 409 on collision unless `overwrite: true`; warnings name configured instances whose secret is missing from the snapshot |
| `POST /api/v1/profiles/{name}/apply` | THE world-swap: replaces the sections the profile contains through the config choke points (a configs-only profile keeps live secrets). **409 while runs are active**; rollback-by-reapply on reload failure; appends an audit event |
| `POST /api/v1/profiles/{name}/rename` · `DELETE /api/v1/profiles/{name}` | Rename (moves both snapshot files, keeps the breadcrumb honest) / delete a snapshot — live settings untouched |
| `POST /api/v1/settings/export` | The ONE sanctioned secret-value egress (ADR-0013): source = current settings or a saved profile; sections config/secrets/setup_env; optional skill-content embedding. Secret-bearing exports require an encryption choice — scrypt+AESGCM passphrase (default) or `plaintext` + `acknowledge_plaintext`. Returns a YAML attachment; audited with the encrypted flag |
| `GET /api/v1/settings/export/summary` | Counts for the export dialog (secrets by scope, env keys present, skills) — never values |
| `POST /api/v1/settings/import/preview` | Stateless: parse (`yaml.safe_load`, 20 MB cap) + decrypt + diff vs current. `{needs_passphrase}` for encrypted bundles without one; wrong passphrase = one indistinguishable 422 |
| `POST /api/v1/settings/import` | **Lands as a profile, never applies** — apply stays `POST /profiles/{name}/apply`, the single world-swap path. Optionally writes embedded skills to the store (additive). No runs guard needed |
| `POST /api/v1/settings/import/env` | Section C → a generated ready-to-place `.env` download (`devcake.env`); no server state change — the operator places it and restarts the stack |
| `GET /api/v1/missions` | Current derived Missions + types (poll-cycle snapshot, advisory — INV-1); includes `blocked_by` keys (peer-instance blockers resolve to keys via the merged post-pass — ADR-0009 amendment; a blocker aged out of every instance's snapshot stays a raw vendor id), and the reason string names open blockers |
| `POST /api/v1/poll/run` | Force a poll cycle now (the Missions board's "Poll now" primary action). Mirrors the relations-mapper "Run now" shape (§1.6): 409 while a cycle is in flight (periodic or another manual trigger); 200 with `{ok, cycle, started_at, duration_ms}` on completion. Missions are born in the PMO — this closes the ~30s feedback loop after a Linear edit without waiting for the next automatic tick |
| `POST /api/v1/debug/dispatch-hello` | Dispatches the hello stub Dev through the full pipeline (Dagu → container → Redis → finalize). Permanent debug/CI fixture — `scripts/ci_suite.sh` |

All writes go through the app (single validation point, `10-persistence.md` §4).

### `GET /api/v1/health` payload

| field | content |
|---|---|
| `app`, `redis`, `dagu`, `openobserve`, `pmo` | booleans (live probes; `pmo` is the aggregate over configured instances) |
| `oo_ingest` | ingest-path probe (`{ok, detail}`) — distinct from the OO admin UI boolean; used by the operator drill / readiness |
| `pmo_instances` | per-instance PMO health (`ok` / `configured` / `team` / `intake_paused`); unconfigured instances show grey (`ok: null`) |
| `forge` | per-repo `ForgeHealth` map (`ok`, `can_push`, `can_read`, `transient`, `detail`, …) — fills incrementally while the initial sweep runs |
| `forge_probe` | initial-sweep progress: `{complete, completed_at, probed, configured}` — `complete: false` means the poll task's full forge sweep hasn't finished yet (`04` §6 step 5), so an empty `forge` map is "pending", not "no repos" |
| `circuit_breakers` | per-Dev-Type auth breakers + **per-repo** `repo:{name}` forge breakers (`15-errors-and-retries.md` §4) |
| `dev_backend_degraded` | dev_type → reason: model-backend degradation (ADR-0018). NOT a breaker — the Dev Type is throttled to one probe run and clears itself on success, so it is deliberately kept out of `circuit_breakers` (`15-errors-and-retries.md` §4a) |
| `intake_paused` | the master switch state |
| `last_poll_at` | ISO-8601 UTC of the last poll cycle that finished (periodic OR manual); `null` before the first cycle. Powers the Missions board's "Last polled Ns ago · next in ~Ns" honesty line |
| `poll_interval_seconds` | current `config.poll_interval_seconds`, echoed here so the SPA doesn't need a separate `/config` read to compute the cadence line |
| `poll_degraded` | instance → reason when that instance's poll segment hit a permanent error (other instances keep polling) |
| `internal_forge` | bundled Gitea health (`{ok, detail, ui_url}`) or `null` when `GITEA_ADMIN_PASSWORD` is unset |
| `active_runs` | count of dispatched/running/finalizing runs |
| `forge_protection` | default-branch protection probe per repo (cached ~5 min; `null` when unknown) — **not** part of `security_warnings` |
| `anomalies` | per-mission advisory strings (out-of-pipeline merges etc.; pruned when terminal) |
| `merge_handoffs` | pmo_id → "awaiting human merge" strings — the live merge queue banner |
| `needs_human` | pmo_id → advisory string, rebuilt each cycle from the `DEVCAKE-NEEDS-HUMAN` label (clears the moment the human removes the label) |
| `dependency_cycles` | detected blocked-by loops (each names the mission keys in the loop) |
| `blocked_reasons` | pmo_id → why the scheduler is currently holding a mission back (advisory mirror of the last gate map) |
| `mapper_degraded` | `null`, or the error string when the last 3 mapper runs all died (periodic service backs off; Run now still works). Surfaced on the **Traffic** config card, **not** as an Overview SPA alert |
| `security_warnings` | dismissable credential-posture list from `security.security_warnings` (`14` §8) — e.g. `gui-secrets-basic-auth`, `forge-write-token:{repo}`, `repo-read-only:{repo}` |
| `prompt_template_warnings` | active templates that no longer resolve (fallback-to-default in effect) |

## 2. Overview page

The landing dashboard, fed by the health poll + a short runs/dev-types poll. **Service health lives as sidebar dots** (app / pmo / redis / dagu / gitea / logs) — not as Overview cards.

- **Masthead answer sentence** — display-face title that answers "do I need to do anything?": *"Nothing needs you."* / *"{N} things need you."* / critical-warning count when health is known; eyebrow line for service health ("all services healthy" / "a service is down — see the sidebar"). Subline: active Devs baking · intake ON/PAUSED · runs recorded.
- **Let's get baking** checklist — first-run three-step card (Connect a PMO, Add a repository **or** use the internal forge, Give a Dev Type credentials). The repository step is satisfied by an external work-repo token, a healthy internal forge (`/health.internal_forge.ok`), or an explicit **I'll work with the internal forge** dismiss (persisted in `dismissed_alerts` as `setup-checklist:internal-forge`). Retires itself once all three pass.
- **Advisory alerts** — derived client-side (`lib/alerts.js`) from the health payload: **intake paused** · **dependency cycles** · **unprotected default branch** (`forge_protection`) · **`security_warnings`** · **`prompt_template_warnings`** · **`poll_degraded`** · **anomalies** (out-of-pipeline) · **circuit breakers** · **unused repositories** (`unused_repos` — adapters no PMO selects; points at Repositories → ⋯ → Remove unused repositories). **Not** `mapper_degraded` (that is the Traffic card only). Some alerts are **dismissible**; dismissals persist server-side as `AppConfig.dismissed_alerts` ("id:signature" strings — a changed signature resurfaces the alert; a "N dismissed" affordance restores them), with localStorage as a fallback while the PUT can't reach the backend.
- **Needs Human Action** — unified panel of `merge_handoffs` (`DEVCAKE-MERGE`) and `needs_human` (`DEVCAKE-NEEDS-HUMAN`) rows (not separate "merge queue" / "needs attention" cards).
- **Stats strip** — Active runs · Mission intake · Devs (available/running/broken color code) · Needs human count.
- **In the oven** — active runs by stage (ONBOARD/PLAN/EXECUTE/REVIEW), linking into Runs.
- **Recent runs** — first 5 from `GET /runs?limit=25&offset=0` (click opens the run terminal).
- **Quick links** — Dagu, OpenObserve, Gitea (when `internal_forge` is live), Spec & source.

## 2a. Missions board (`#/missions`)

Hermes-style kanban of the current poll snapshot (`GET /api/v1/missions`): cards per derived mission with stage glyphs, priority, blockers, and a drawer for runs + PR link. Primary action **Poll now** (`POST /api/v1/poll/run` — 409 while a cycle is in flight). Card **MoreMenu**: **Park** / **Retry** (when `DEVCAKE-FAILED`) / **Resume** (when `DEVCAKE-NEEDS-HUMAN`) / **Unpark** (when `DEVCAKE-SKIP`) via `POST /missions/{id}/actions`. Drawer: **Send guidance** (`POST /missions/{id}/comment`) and per-run **Stop** (`POST /runs/{id}/stop`). See `docs/img/missions-board/` for UI evidence.

## 2b. Repos page (`#/repos`)

Operator-facing repository inventory: external `RepoInstance` cards (forge, URL, secret presence, connection test via `POST /api/v1/connections/forge/{name}/test`) plus bundled internal Gitea operator repos when `internal_forge` is live. This is where repository identity lives — **not** under Configuration.

The header ⋯ menu offers **Remove unused repositories…** (2026-08-01 incident hygiene): computes — from the *draft*, so unsaved PMO (de)selections count — every repo card no PMO selects as work or reference, and removes them all in one confirmed draft edit. Like per-card removal, nothing changes until Save; saving deletes the dropped repos' stored tokens (`connections/repo-{name}.json`) via the ordinary config PUT. `/health.unused_repos` (`{count, names, configured}`) feeds the matching dismissable Overview alert.

Each repository card hosts that repo's merge doctrine (drafted with the rest of config; ADR-0020 — not a deployment master switch):
- **`auto_merge`** — default OFF per card; enabling shows a confirm dialog whose body matches `AUTO_MERGE_COPY` (`lib/configLabels.js`) and names the repo: the **app** will merge after REVIEW approve on **this** repo; without a reviewer token, merges proceed without a formal forge approval; parked `DEVCAKE-MERGE` missions on **this** repo are re-armed; and the copy states that the toggle **gates the app only** — branch protection stops agents from merging. Only enable with branch protection + eyes open (`14` §2 zone C).
- **`auto_resolve_merge_conflicts`** — default ON; dimmed while this card's `auto_merge` is OFF. Tooltip explains the EXECUTE rework loop and the 2-attempt cap (`03-mission-lifecycle.md` §4.1).
- **`merge_retry_window_minutes`** — default 30, min 0; dimmed while this card's `auto_merge` is OFF.

Internal (zero-repo) missions always auto-merge; operators who want doctrine control create the repo as a config card via "gitea (internal) → + Create repository". The page-level **`attach_merged_changeset_to_pmo`** toggle remains deployment-global.

## 3. Config page — draft/Save model and sections

**Unified draft (founder decision, 2026-07-13):** every edit on this page lands in a client-side draft — *nothing* persists until the operator reviews and saves. A **DirtyBar** appears while the draft differs from the server state; **Save** opens a **SaveReviewDialog** listing every pending change (per section) for confirmation, then issues the PUTs (`/config`, `/dev-types/{name}`, `/assignments`) and reports per-section results inline. A **nav guard** intercepts hash navigation away from a dirty draft (revert-and-ask, then replay). Danger confirms (adoption mode, auto-merge) still appear at flip time but only write the draft — the real write happens at Save. The one exception is the sidebar's mission-intake switch, which is deliberately immediate (§0); `dismissed_alerts` writes also bypass the draft (they're UI state, not operator config).

**Multi-select convention (mandatory for new fields):** every field that
selects multiple entries from a catalog/list — the PMO **repo set**,
**reference repos**, Dev Type **skills**, and any future such field — uses
the shared toggle-chip control
(`admin/spa/src/components/SelectionChips.jsx`): ordered rounded chips
(click to toggle; selection order = click order where order carries meaning,
e.g. the repo set's `· default` first-badge — normalize order in `onChange`
where it doesn't, the draft diff is order-sensitive), a selected entry whose
option no longer exists renders **red/strikethrough with ✕** (visible and
removable — a stale name must never wedge the Save PUT), and an explicit
empty-state note. Do not introduce checkbox lists or multi-select dropdowns
for these.

Sections (one section per `#/config/<id>` view, from `nav.js`): **PMO · Dev Types · Skills · Assignments · Prompts · Profiles & Export · Limits · Traffic control**. Repositories are **not** a Configuration section — use `#/repos`.

### Traffic control
- **Decomposition depth** (`SettingRow` select, drafted like every scalar) — `max_decomposition_depth`: **1 level** (a decomposition child is never split again), **2 levels** (default — a Project's missions can each split once more), or **Unlimited** (stored as `0`; the ONBOARD Dev decides every time — removes the fission backstop, and the help copy says so). The schema accepts any depth ≥ 0; a value outside the offered three (set via API/YAML) renders as an extra "*N levels (set via API)*" option so it round-trips instead of being clobbered on the next save. `03-mission-lifecycle.md` §1.3, `adr/0012`.
- **Relations Mapper card** — four controls: (a) **Run now** button → `POST /api/v1/relations-mapper/run`, showing the dispatched run id (or the 409/422 error) inline; (b) **interval in minutes**; (c) **Dev Type combobox** (defaults to the seeded `mapper`; required before enabling or running); (d) **Periodic service ON/OFF toggle** (default OFF — manual-only out of the box). Controls disable while a save is in flight (no stale-state races). The card shows the **degraded** state from `/health` (`mapper_degraded`). Deleting the mapper's Dev Type is refused with 409.

**Secrets (schema v4 / ADR-0011):** operator secrets are entered as **VALUES**
through write-only `SecretField` controls (`PUT /secrets/…`,
`PUT /harness-secrets/…`). They are stored `0600` under `/data/secrets/`,
never echoed by `GET /config` or `secrets-check` (presence + timestamp only).
`.env` is bootstrap-only. Paste guards use registry `secret_shape_prefixes`.
Connection tests fail clearly when a required secret is absent.

### PMO connection (anchor `#/config/pmo`)
Fields edit configured PMO instances (`02-domain-model.md` §9); section copy
renders from `GET /connections/registry` and stays **PMO-neutral**.
- System selector, instance name, team/workspace key.
- **API key VALUE** via secret field (`/data/secrets/connections/pmo-{name}.json`) — Set / clear; ✓ from `secrets-check`.
- Validated by **Test connection** (`POST /api/v1/connections/pmo/{name}/test`).
- **Adoption mode toggle** — `opt_in` (default) vs `opt_out`. Flipping to `opt_out` opens a confirmation dialog: *"DevCake will adopt EVERY non-completed Issue and Project in this team — including the entire existing backlog — and start working through them by priority, consuming tokens. In opt-in mode it only touches items you label `DEVCAKE`."* Remember: the whole team is in the agent trust boundary (`14` §0). The confirm writes the draft; Save applies it.
- Poll interval (seconds).

### Dev Types
Card list + editor (the harness combobox is **authoritative**, `08-harness-templates.md` §2):
- Name; **harness template** dropdown (options from `GET /harnesses`); **identifying prompt** textarea; **model** pin (e.g. `claude-fable-5` on the seeded `judgment`); per-type **max concurrency** integer.
- **Runtime & credentials block** (derived): registry image for the selected harness, readiness badge, per-requirement checklist — harness secret VALUES via `harness-secrets/{VAR}` (✓ from `secrets-check`), credential files via upload (`POST …/credentials` → `/data/secrets/{dev_type}/`). Flipping the combobox previews requirements; amber "unsaved harness change" until Save.
- **Connect via OAuth…** — per **Dev Type** (`POST /oauth/dev-types/{name}/start`), shown when the saved harness has a device-code flow; the credential lands in that Dev Type's `/data/secrets/{name}/` dir (two Dev Types on one harness = two accounts).
- **Clear secrets** (section ⋯ menu on **Dev Types**, **PMO**, and **Repositories**) — same multi-select picker over `GET /secrets/inventory`, then a **ConfirmDialog** (yes/no) whose body summarizes counts and holds **Turn off mission intake after this** (default **on** — uncheck to leave intake running). Confirm calls `POST /secrets/clear`. Full inventory on every page; **context reorders** groups (and connection rows) so page-relevant secrets float first (Dev Types → harness + OAuth files; PMO → `pmo` connections; Repos → `repo` connections). Master **Select all** / **Deselect all** at the top of the list. Dev credentials: in-flight containers keep injected values; connection secrets: app drops them immediately via adapter reload. Profile snapshots / internal-forge tokens are not listed. After clear: draft reload + SecretField remount; if intake was paused, the sidebar master switch updates immediately via health state.
- **MCP servers**: free-text area, one CLI command per line (syntax hint per selected template, `08-harness-templates.md` §7), with the warning: *"These commands run inside the Dev container before the agent starts and are arbitrary code execution by design."* Execution semantics per `07-dev-runtime.md` §5 (failure or 300 s per-command timeout ⇒ run fails with `DEV_MCP_SETUP` + the command and stderr in the run error).
- **Secret env vars**: free-text area, one NAME per line (UPPER_SNAKE_CASE; the draft validates shape and duplicates inline), plus one paste field per listed name (values via `harness-secrets/{VAR}`; the paste widget's ✓/✗ from `secrets-check`). `GET /api/v1/dev-types` additionally exposes `secret_env_present` per declared name — the headless provisioning check. Delivered into the run's env so MCP setup commands can reference `$VAR` without a value ever entering config (ADR-0011). Presence never affects `credentials_ready`, but a missing value **referenced** by an MCP setup command gates dispatch (`14-security.md` §8).
- **Skills**: **tri-state chips** from the skill-store catalog (`SkillModeChips` — click-cycle: off → **Available** → **Required** → off). **Available** installs the skill (consult-optional; model description-match). **Required** installs it and soft-forces a “must consult” prompt append (`DevType.skills_required` ⊂ `skills` — instructional, not kernel-enforced). Enabled whenever the selected harness declares a `skills_dir` in the registry (all three current harnesses do — `08-harness-templates.md` §7a); a harness without one renders the chips disabled with a hint and dispatch skips with a warning. A selected-but-missing skill renders as the standard red ✕ stale chip and is skipped at dispatch with a warning. Skills are domain modules, not mission scripts (**ADR-0016**, `app/devcake/skills/README.md`).

### Skills (anchor `#/config/skills`)
The skill-store catalog: name / description / source badge (`store` = served from the `devcake-repos/skill-store` repo on the bundled Gitea; `bundled` = fallback copies shipped in the app image, used when the internal forge is disabled or unreachable). Per-row **View** opens a read-only dialog of the skill files (`GET /api/v1/skills/{name}` — store-first, bundled fallback; multi-file skills show file tabs) with **Rendered** Markdown (reading aid; leading YAML frontmatter stripped on `.md` files) and **Source** (stored file bytes). Placeholders are not substituted. Header actions: **Add skill**, **Edit in Gitea →** (store repo, operators push skills straight to `main`) and **Re-seed built-ins** (`POST /api/v1/skills/sync` — restores missing built-in files, never overwrites edits). Skill content is operator-controlled instructions injected into the agent session — same trust class as the MCP command area.

**Add skill** (no Gitea, no YAML — the non-technical path) opens a dialog with two modes:
- **Write** — name + "when should the agent use it?" (the trigger description) + a markdown instructions box. The app *generates* the frontmatter and commits `<name>/SKILL.md` to the store (`POST /api/v1/skills`).
- **Import files** — pick the skill's *folder* (containing `SKILL.md` plus any supporting files); the browser's directory picker preserves the nested layout (a plain file picker would flatten `refs/x.md` to `x.md`), OS/VCS cruft like `.DS_Store` is dropped, the name is read from the frontmatter, and the tree is committed under `<name>/` (`POST /api/v1/skills/import`).

Both validate server-side (name shape, required description, per-file 200 KB / total 1 MB caps, path-safety) and **refuse a name collision** with an existing store skill or a built-in unless the operator confirms **Overwrite** (409 → explicit confirm). Every row shows **View**; when the store is editable every row also shows a **Delete** control — live for operator/retired store skills, **disabled** for built-ins (they re-seed at boot; deselect on Dev Types instead; `DELETE` still 422s). Skills created here carry `metadata.source: operator (admin panel)`.

### Assignments
Matrix: four Mission Types × (Dev-Type dropdown + **extra CLI args** textbox). The args are appended verbatim to the harness invocation for runs of that Mission Type — the mechanism for per-Mission-Type tuning like bounded-effort ONBOARD (`--max-turns 15` is the seeded default there for the claude-code harness). Harness-specific means capability-specific: `--max-turns` exists on claude-code and grok-build, but **codex 0.144.4 has no turn-cap flag at all**, so no args value bounds a codex Dev's effort (`08-harness-templates.md` §1, `15-errors-and-retries.md` §2a). The textbox shows a hint naming the assigned Dev Type's harness; **reassigning a Mission Type to a Dev Type with a different harness triggers a warning offering to keep or clear the args** (they are harness-specific by nature). Same trust class as the MCP command area: admin-only, executed in the Dev container. Inline validation (every type assigned; a Dev Type may hold several).

Below the global matrix, **one override block per configured PMO instance** (ADR-0019): a tri-state select per Mission Type — the inherit option names the effective global Dev Type; choosing a type creates a wholesale override row with its **own** args textbox (fresh overrides start with empty args; the harness-mismatch warning applies per row). When EXECUTE and REVIEW share a Dev Type, a **performance tip** (not a security warning) notes that a distinct REVIEW type can carry review-focused skills/prompt — evaluated per instance on **effective** rows. Overrides save through the config draft (PUT `/config`, they are `pmos[*]` fields), not PUT `/assignments`.

### Pointing a Dev Type at a local / OpenAI-compatible backend

Four click targets, and **which ones you use depends on the harness** — which
mechanism each template reads is the template contract
([`08-harness-templates.md`](08-harness-templates.md) §8):

| where | what goes there |
|---|---|
| **Dev Types → model** | the backend's model id exactly as its `/v1/models` reports it (rides as `DEVCAKE_MODEL` → the harness's model flag) |
| **Dev Types → Secret env vars** | the NAMES the CLI reads: `ANTHROPIC_BASE_URL` + `ANTHROPIC_AUTH_TOKEN` (claude-code), `GROK_MODELS_BASE_URL` (grok-build) |
| **Dev Types → Runtime & credentials** / secret-env paste fields | the VALUES (`PUT /harness-secrets/{VAR}`) — the registry keys `XAI_API_KEY` / `CODEX_API_KEY` have their own checklist rows |
| **Assignments → extra CLI args** | **codex only**: the whole `-c model_provider=… -c model_providers.<id>.base_url=…` block, in **every** Mission Type row routed to that Dev Type |

- **claude-code and grok-build need no extra CLI args at all** — leave the textbox empty; the backend is selected entirely by env vars.
- **The base-URL shape differs**: `ANTHROPIC_BASE_URL` takes **no** `/v1` suffix; `GROK_MODELS_BASE_URL` **requires** it. Getting this backwards is the common failure.
- A claude-code Dev Type configured this way shows **"no credentials configured"** on the Devs card — `credentials_ready` only checks the registry keys (§3, Dev Types). Advisory only; it gates nothing.
**The codex block.** codex takes its whole backend definition as `-c` overrides,
pasted into the extra-CLI-args textbox of **every** Mission Type routed to that Dev
Type (the args are per Mission Type, not per Dev Type — §3):

```
-c model_provider=vllm -c model_providers.vllm.name=vLLM -c model_providers.vllm.base_url=http://<host>:8000/v1 -c model_providers.vllm.env_key=CODEX_API_KEY -c model_providers.vllm.wire_api=responses -c model_context_window=<max_model_len> -c model_auto_compact_token_limit=<~80% of it>
```

**Clear a stale OAuth credential file.** If the Dev Type has a `grok-auth.json` or
`codex-auth.json` from an earlier device-code login it is still delivered to the
container; clear it via **Dev Types → ⋯ → Clear secrets** (or by deleting that
file under `/data/secrets/{dev_type}/`) if the Dev Type is dedicated to a local
backend.

- **codex + models that invent tool syntax as prose** can exit 0 with no
  `result.json` (exit 11; no brake) — prefer grok-build or claude-code for those
  stages (`08-harness-templates.md` §8).

### Profiles (anchor `#/config/profiles`)
Named snapshots of the runtime settings AND secret values (ADR-0013). **Entirely Instant** — profiles carry secret values, which never enter the client draft, so the section header carries the ImmediateBadge and the apply panel sits in an InstantZone. The UI deals in presence and counts only; a secret value never reaches the SPA.

- **Save current as profile…** (the one primary): PromptDialog for the name; a 409 collision chains into an explicit red **Overwrite** confirm. Save warnings name configured instances whose secret is missing from the snapshot.
- **Apply a profile**: dropdown + "Apply profile…" (the Prompts workflow-switcher lineage). The ConfirmDialog **renders the backend's diff preview** — per-section change counts, secret deletions by name, "updated after this snapshot" rotation warnings, intake-pause changes — plus honest destroyed/survives copy ("Run history, the skill store, internal repos, and .env values are untouched"). Disabled while the page draft is dirty (Save or Discard first); a 409 while runs are active renders inside the dialog for retry.
- Rows: mono name (+ "configs only" badge for A-only profiles), capture time + counts, last-applied breadcrumb with the honest divergence note ("settings changed since" / "matches as of apply" — dict compare + secret timestamps, never value fingerprints). Rename/Delete live in the row ⋯ menu; delete states that live settings are unaffected.
- Applying reloads the whole shared draft (everything changed). Profiles are fire-and-forget: later edits never update a snapshot.
- **Transfer rows** (same section): **Export…** — source select (current / any profile), section checkboxes (runtime configs on by default; secrets row shows live counts from `export/summary`; setup values), "Embed skill contents" toggle, and the encryption block when secrets/setup are checked (passphrase+confirm preselected; **Plaintext** flips the primary to a red "Download with plaintext secrets" under a password-manager-export warning). Download is a Blob + `<a download>` with the server-authoritative filename. **Import…** — file picker → passphrase step for encrypted bundles → server preview (per-section diffs, secret counts, amber warnings verbatim) → name it → **Save as profile** (409 → red overwrite flip). The done step offers the generated-`.env` download + numbered host steps when the bundle carries setup values.

### Prompts (anchor `#/config/prompts`)
Mission-type **playbook templates** (`GET/PUT/DELETE /prompt-templates/{TYPE}/{name}`) and per-Dev-Type **identifying-prompt templates** (`/devtype-prompts/{dev}/{name}`). Template create/edit/delete is **Immediate** (own Save in the modal); only the **active** selection per mission type / Dev Type rides the unified config draft (`active_prompt_templates` / `active_devtype_prompts`). Missing actives fall back to the built-in default; unresolved actives surface as `prompt_template_warnings` on `/health`. **View** uses the same **Rendered** / **Source** dialog as Skills (Markdown reading aid; unsubstituted `{var}` placeholders; Source is the stored template).

### Limits
- **Global max Devs** integer (help text: effective ceiling = min(global, sum of per-type caps)).
- **Dev run timeout** minutes (default 120).
- **Review-loop warning** cadence (every N rejections).
- Service auto-restart is compose-managed (read-only note). **`max_attempts` is a config field** (`config.yaml` / `PUT /config`) but is **not** exposed on the Limits UI today — edit via API/YAML or leave the default 3.

## 4. Runs page

A prominent **"Open Dagu ↗"** button (new tab, URL from `DAGU_UI_URL`) and a **MoreMenu (⋯)** for rare/destructive run actions — **not** a bare danger button in the header. The menu carries **Cost inputs…** (below), **Stop all runs** and **Clear run history** (each with an honest one-line consequence description). Live run table from `GET /api/v1/runs` (10 s poll) — **paginated** (25/page, `limit`+`offset`, total count) and **filterable** by mission key (substring on key or run id), **PMO connector** (`pmo_ref` dropdown, self-sourced from run records), and a **UTC date range** (labeled so; `to` is end-inclusive). Rows show state, the `verdict` where the app's judgment diverged (`02-domain-model.md` §7), and **token/cost columns** — in / out / cache r / cache w rendered in millions (`2.43M`; `—` = unknown, `<0.01M` = tiny-but-real) and an effective **cost** cell: native harness cost plain (`$0.12`), estimates muted with a `~` prefix and a tooltip naming the rate-card vintage (`adr/0021`). A pinned **filtered-totals row** sums the ENTIRE filtered set server-side (not the visible page): completed-runs runtime (live runs deliberately excluded — the label says so), the four token classes, and effective cost with native/estimated in its tooltip. The started/duration/token/cost **headers sort server-side** (first click descending, click again to flip, nulls always last — an ascending cost sort never leads with token-less rows). An **"Aggregate by mission"** checkbox (default off) regroups the table into mission clusters — a subtotal row per `(pmo_ref, mission_key)` with its runs beneath in pipeline order — flipping pagination to missions; the active sort then orders whole missions by their aggregate ("most expensive missions first"), which also makes retries visible as clustered duplicate seqs. Every row carries a **trace ↗ deep link** into OpenObserve pre-filtered to that run: `{OO}/web/traces?org_identifier=default&stream=default&period=1w&search_mode=spans&query=BASE64(devcake_run_id='<run_id>')` (URL shape verified live). No iframe.

**Cost inputs (⋯ menu, instant regime):** the operator rate card behind every estimated cost (`AppConfig.cost_inputs`, `adr/0021`) — per-model rows (longest prefix wins; USD per 1M for input / cache read / cache write / output, seeded with grok-4.5 list rates) plus the **override checkbox** ("use these rates for displayed cost even when the harness reported its own" — it only affects models with a matching row, and the modal says so). Save PUTs `{cost_inputs}` straight through `/config` (validation + hot reload + rollback) inside an `InstantZone`; the Runs table reprices on its next poll. `cfg.cost_inputs` sits in the config draft's IGNORED list so an open Configuration draft can never clobber an instant rate edit; the fields still carry labels in `configLabels.js` so profile/bundle diffs read well.

**Run terminal (popup):** clicking any run row opens a terminal-styled modal (dark chrome, monospace, blinking cursor while live) showing the run's condensed output. Live runs follow `GET /runs/{id}/log/stream` over `EventSource` (the server replays the stored log first, so no separate initial fetch); terminal runs fetch `GET /runs/{id}/log?tail=1000` once. The stream's `end` event prints `[process exited]` and stops the cursor. This is a simulacrum, not a TTY: the harness runs headless (no PTY) and the app deliberately holds no docker.sock (`13-deployment.md` §5), so the feed is the Dev's own `run.log` relay — the same condensed lines Dagu captures in its step log. Client caps at ~5000 lines; ESC / backdrop / ✕ close it.

**Stop all runs** (More menu, confirm dialog) calls `POST /api/v1/system/stop-runs`: every dispatched/running Dev is killed through the run manager (failure record shipped, terminal state, ACL teardown); finalizing runs complete on their own and are named in the response. Nothing is deleted.

**Clear run history** opens a React confirmation dialog (never `window.confirm`) and on confirm calls `POST /api/v1/system/clear-runs`. That endpoint:

The whole wipe holds **two locks for the entire `clear_all`** (including OpenObserve stream deletes — intentional; dispatch is paused for the full wall-clock window):

- `poll_rt.lock` — no poll cycle / force-poll runs mid-wipe  
- `RunBootstrap.dispatch_lock` — **every** dispatch flavor (poll, hello, OAuth, mapper “run now”) creates its Redis ACL user + starts the container inside `launch()` under this lock (PR #31). Holding only the poll lock is **not** enough.

Operator impact: no new Devs start until clear finishes; OO stream delete can dominate latency.

1. **Soft drain:** stops every dispatched/running Dev via the run manager and **waits for container exit** (capped just past Dagu's ~30 s SIGTERM grace). Finalizing runs are skipped (their containers already exited).  
2. **Force-remove pass** (if any still live): re-`stop` each undrained id via Dagu, optional `stop_all` hammer, short re-poll. The app has **no `docker.sock`** — force is Dagu-API only. Residual undrained ids are reported; response `ok` is **false** if any remain. Wipe **still proceeds** so a wedged container cannot hold the operator hostage forever (residual: ACL may race a still-live straggler — check the response / logs).  
3. **Local wipe with generation guard:** bumps process-local `RunStore.wipe_generation`, then deletes every Run file under `/data/state/runs/` (including quarantine), every run log under `/data/state/runlogs/` (SSE followers get the end sentinel), and truncates `events.jsonl` (attempt counters reset — INV-1 / `10-persistence.md` §5). In-flight finalize/heartbeat/kill cannot resurrect pre-wipe records (`store_gen` < `wipe_generation` → `save` no-op; mission finalize aborts further PMO posts).  
4. Deletes every Dagu `dev-run` history record (`DELETE /dag-runs/dev-run/{id}`, paginated list).  
5. Deletes OpenObserve log/trace streams (they recreate on next ingest; dashboards stay).  
6. Trims the Redis ingress stream, drops leftover reply streams and per-run ACL users (`dev-*`).  
7. Deletes every per-mission `activity-*` repo on the internal Gitea (ADR-0014 D4).

**Preserved:** `/data/config`, `/data/secrets`, PMO/forge state, circuit breakers (credential health).

## 5. Logs page

A prominent **"Open OpenObserve ↗"** button (new tab, URL from `OO_UI_URL`). **Canned deep links (Errors last hour / Trace by mission key / Cost dashboard) are not implemented** in the SPA — only the Open OpenObserve action ships. No iframe.

## 6. Auth and control-plane posture

v0 ships **HTTP basic auth at both nginx and FastAPI**, using the same
`ADMIN_USER`/`ADMIN_PASSWORD`. That is a **design choice** for a
**single-operator dedicated host** with **loopback** binds (`14-security.md`
§0, §4) — not multi-tenant RBAC. Admin credentials protect the GUI secret store
(`/data/secrets`), config (including MCP free-text / `extra_cli_args` = ACE in
Dev containers), and destructive **Clear run history**.

Nginx covers the SPA and proxy; FastAPI independently protects every route
except minimal `/api/v1/health/live`. OpenAPI/docs endpoints are disabled.
Every `POST`/`PUT`/`PATCH`/`DELETE` additionally requires `X-DevCake-Request: 1`;
the SPA sends it from its centralized mutation helper. The browser's native
prompt is the login UI.

**Do not bind admin past localhost** without accepting that you are publishing
every stored PAT. OIDC/SSO is optional if you must expose beyond that posture
(`14` §11) — not required for the default dedicated-host deploy.

Health **security_warnings** (forge write token, unprotected branch,
gui-secrets-basic-auth, …) are **advisory** (`14` §8); dismissing them is
operator acceptance of residual risk.
