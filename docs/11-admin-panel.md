# 11 — Admin Panel: UI Spec and API Contract

> **Audience:** frontend implementer + app API implementer.
> **Depends on:** `02-domain-model.md` (AppConfig, DevType), `10-persistence.md` (write path), `13-deployment.md` (proxy topology).

Simple but beautiful: a static SPA (React + Vite + Tailwind, `admin/spa/`) served by nginx in the `admin` container, which reverse-proxies `/api/*` → `app:8000` (no CORS). A persistent **sidebar** hosts navigation and the mission-intake master switch; a tiny hash router (`#/overview · #/runs · #/config · #/config/<section> · #/logs`) drives **four pages**: **Overview**, **Runs**, **Config**, **Logs**. The Dagu and OpenObserve UIs are **not** embedded: the Runs and Logs pages open them in new browser tabs via buttons (confirmed decision — no iframes; their URLs reach the SPA as nginx-templated env vars `DAGU_UI_URL` / `OO_UI_URL` in `/config.js`, `13-deployment.md` §2). All confirmation dialogs are React components — never native `window.confirm`/`alert` (they block automation and the browser).

**Health polling is honest by design:** the SPA polls `GET /api/v1/health` every 10 s, keeps the last-known data on failure, and renders an unreachable backend **RED** (never gray/unknown) — the failure itself is the signal (founder decision, 2026-07-13).

## 0. Sidebar (persistent shell)

- Navigation to the four pages, with scrollspy sub-entries for the Config sections; collapsible to an icon rail.
- **Mission-intake master switch** — THE operational control, so it lives in the sidebar (visible even collapsed, founder decision) and applies immediately (its own `PUT /config`, outside the Config draft): OFF pauses intake — no new runs start (missions or mapper) while the operator rearranges missions in the PMO. In-flight runs finish normally (pause freezes dispatch, not consequence) and the merge/tracking sweeps keep running; flipping back resumes on the next poll cycle. Disabled (with an explanatory tooltip) while the backend is unreachable or health is unknown; save errors surface inline — the toggle never fails silently.
- Component health dots (app/PMO/forge/Redis/Dagu/OpenObserve) from the 10 s health poll, plus a theme toggle.

## 1. REST API contract (`/api/v1`)

| Method + path | Purpose |
|---|---|
| `GET /api/v1/health` | Full component health (below) |
| `GET /api/v1/health/live` | Unauthenticated liveness (`{"app": true}`) — the compose healthcheck |
| `GET /api/v1/config` · `PUT /api/v1/config` | General settings (AppConfig minus dev types). PUT validates server-side (pydantic); errors return field-keyed messages surfaced inline. Nested dicts deep-merge, but the plural `pmos:`/`repos:` lists are **replaced whole**; singular v1-shaped `{"pmo": {…}}`/`{"repo": {…}}` bodies are **rejected with 422** (never silently dropped — the v1→v2 migration was removed at v0). A successful PUT hot-reloads both adapters (`reload_connections`) and re-ensures the managed labels |
| `GET /api/v1/harnesses` | The harness registry: derived image, credential requirements, OAuth availability per `harness_template` — the Dev Type card renders (and previews unsaved harness switches) from this |
| `GET /api/v1/dev-types` · `POST /api/v1/dev-types` | List (enriched: `harness` info + `secrets_present`) / create Dev Types |
| `POST /api/v1/oauth/dev-types/{name}/start` · `GET /api/v1/oauth/status/{run_id}` | Per-dev-type device-code login; credential lands in `/data/secrets/{name}/` |
| `PUT/DELETE /api/v1/dev-types/{name}` | Update / delete one Dev Type. DELETE refuses while assigned to a Mission Type (or to the Relations Mapper) |
| `POST /api/v1/dev-types/{name}/credentials` | JSON `{"filename": "...", "content": "..."}` → stored to `/data/secrets/{name}/{filename}` (0600); a fresh credential clears that Dev Type's auth breaker |
| `GET /api/v1/assignments` · `PUT /api/v1/assignments` | Mission-Type → Dev-Type map. Validation: all four types assigned, each to exactly one existing Dev Type |
| `PUT/DELETE /api/v1/secrets/{scope}/{instance}/{field}` | Write/delete connection secret **VALUES** (pmo `api_key`; repo `token`/`token_ro`/`reviewer_token`) — never echoed (`14` §4, ADR-0011) |
| `PUT/DELETE /api/v1/harness-secrets/{VAR}` | Write/delete harness/model key VALUES |
| `GET /api/v1/secrets-check` | Presence + `updated_at` only (no values, no fingerprints) — powers Config ✓/✗ |
| `GET /api/v1/connections/registry` | Adapter registry metadata: PMO systems + forges, `secret_shape_prefixes` (paste guard), `managed_labels_expected` |
| `POST /api/v1/connections/pmo/test` | Live probe: auth + team fetch; returns `{ok, team, labels, labels_expected, missions_visible}` — `labels` counts the intersection with DevCake's managed label set |
| `POST /api/v1/connections/forge/test` | Live probe: authenticated repo fetch + explicit push permission + default branch (+ reviewer token check + branch-protection state). A read-only or fine-grained token that omits the configured repository returns `ok: false` and trips the global forge breaker before dispatch; transient probe failures (5xx/network/rate-limit) are reported but never latch the breaker, and a latched breaker re-probes every poll cycle (`15-errors-and-retries.md` §4) |
| `GET /api/v1/runs?mission_key=…&limit=…` | Read-only run history (from `/data/state/runs/`) for context |
| `GET /api/v1/runs/{run_id}` | Fixed allowlist of operational Run fields (incl. `verdict`); run specs, prompts, results/token reports, envelope verifiers, and credential material are never serialized |
| `GET /api/v1/runs/{run_id}/log?tail=N` | Plain-text condensed run output (from `/data/state/runlogs/`, relayed live by the Dev via `run.log` — `09-messaging.md` §3) |
| `GET /api/v1/runs/{run_id}/log/stream` | SSE follow of the same log: replays the stored lines, then streams new ones until the run reaches a terminal state (`event: end`). Sends `X-Accel-Buffering: no` so nginx doesn't buffer; 15 s `: ping` heartbeats stay under nginx's 60 s read timeout |
| `POST /api/v1/system/clear-runs` | Operator wipe: stop in-flight Devs, delete local run records + audit log, purge Dagu run history, delete OpenObserve log/trace streams. Config + secrets + PMO/forge untouched (`10-persistence.md` §5) |
| `POST /api/v1/relations-mapper/run` | Manually dispatch a Relations Mapper run (`03-mission-lifecycle.md` §4b). Works regardless of the `enabled` toggle (which governs only the interval service); 422 without a valid `dev_type`, 409 while a mapper run is active |
| `GET /api/v1/missions` | Current derived Missions + types (poll-cycle snapshot, advisory — INV-1); includes `blocked_by` keys, and the reason string names open blockers |
| `POST /api/v1/debug/dispatch-hello` | Dispatches the hello stub Dev through the full pipeline (Dagu → container → Redis → finalize). Permanent debug/CI fixture — `scripts/ci_suite.sh` |

All writes go through the app (single validation point, `10-persistence.md` §4).

### `GET /api/v1/health` payload

| field | content |
|---|---|
| `app`, `redis`, `dagu`, `openobserve`, `pmo` | booleans (live probes; PMO via `health_probe`) |
| `forge` | the latest `ForgeHealth` dict (`ok`, `can_push`, `transient`, `detail`, …) |
| `circuit_breakers` | per-Dev-Type auth breakers + the global `forge` breaker (`15-errors-and-retries.md` §4) |
| `intake_paused` | the master switch state |
| `active_runs` | count of dispatched/running/finalizing runs |
| `forge_protection` | default-branch protection probe (cached ~5 min; `null` when unknown) |
| `anomalies` | per-mission advisory strings (out-of-pipeline merges etc.; pruned when terminal) |
| `merge_handoffs` | pmo_id → "awaiting human merge" strings — the live merge queue banner |
| `needs_human` | pmo_id → advisory string, rebuilt each cycle from the `DEVCAKE-NEEDS-HUMAN` label (clears the moment the human removes the label) |
| `dependency_cycles` | detected blocked-by loops (each names the mission keys in the loop) |
| `blocked_reasons` | pmo_id → why the scheduler is currently holding a mission back (advisory mirror of the last gate map) |
| `mapper_degraded` | `null`, or the error string when the last 3 mapper runs all died (periodic service backs off; Run now still works) |

## 2. Overview page

The landing dashboard, fed by the health poll:

- **Component health cards** — every `/health` boolean plus the forge detail; backend unreachable renders RED.
- **Advisory alerts** — derived client-side (`lib/alerts.js`) from the health payload: dependency cycles (names the loop: "DEV-10 → DEV-12 → DEV-10 — these missions will never start until a relation is deleted") · default branch unprotected (a Dev's forge token could merge without review — `13-deployment.md` §8a) · out-of-pipeline activity (`15-errors-and-retries.md`) · mapper degraded · circuit breakers. Alerts are **dismissible**; dismissals persist server-side as `AppConfig.dismissed_alerts` ("id:signature" strings — a changed signature resurfaces the alert; a "N dismissed" affordance restores them), with localStorage as a fallback while the PUT can't reach the backend.
- **Merge queue** — every `DEVCAKE-MERGE` mission awaiting a human (from `merge_handoffs`), and **Needs attention** — every `DEVCAKE-NEEDS-HUMAN` mission (from `needs_human`), each with its remove-the-label call to action.
- **Recent runs** — the last 5 from `GET /runs`, linking into the Runs page.

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

Sections (scrollspy anchors `#/config/<id>`): **Traffic control · PMO · Repository · Dev Types · Assignments · Limits**.

### Traffic control
- **Relations Mapper card** — four controls: (a) **Run now** button → `POST /api/v1/relations-mapper/run`, showing the dispatched run id (or the 409/422 error) inline; (b) **interval in minutes**; (c) **Dev Type combobox** (defaults to the seeded `junior-dev`; required before enabling or running); (d) **Periodic service ON/OFF toggle** (default OFF — manual-only out of the box). Controls disable while a save is in flight (no stale-state races). The card shows the **degraded** state from `/health` (`mapper_degraded`). Deleting the mapper's Dev Type is refused with 409.

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
- **API key VALUE** via secret field (`secrets/pmo/{name}/api_key`) — Set / clear; ✓ from `secrets-check`.
- Validated by **Test connection** (`connections/pmo/{name}/test`).
- **Adoption mode toggle** — `opt_in` (default) vs `opt_out`. Flipping to `opt_out` opens a confirmation dialog: *"DevCake will adopt EVERY non-completed Issue and Project in this team — including the entire existing backlog — and start working through them by priority, consuming tokens. In opt-in mode it only touches items you label `DEVCAKE`."* Remember: the whole team is in the agent trust boundary (`14` §0). The confirm writes the draft; Save applies it.
- Poll interval (seconds).

### Repository
Fields edit configured repo instances.
- Forge selector — options from registry `forges` (GitHub / GitLab / …).
- Repo URL; **access token / optional RO / optional reviewer** as secret VALUES
  (`token`, `token_ro`, `reviewer_token`) — not env-var names (`06` token posture, `14` §8).
- **`auto_merge` toggle** — default OFF; enabling shows a confirm dialog: *"DevCake will merge its own pull requests to the default branch without human review. On GitHub without a reviewer token, merges proceed without formal approval."* Only enable with branch protection + eyes open (`14` zone C).
- **`auto_resolve_merge_conflicts` toggle** — default ON, no confirm dialog (every resulting merge still passes the full EXECUTE→REVIEW gate). Dimmed and non-interactive while `auto_merge` is OFF (the setting is inert without it). Tooltip explains the EXECUTE rework loop and the 2-attempt cap (`03-mission-lifecycle.md` §4.1).
- **`merge_retry_window_minutes` number field** — default 30, min 0; also dimmed while `auto_merge` is OFF. Tooltip: lower it on CI-light repos to surface unmergeable PRs faster; raise it on CI-heavy repos to stop premature `DEVCAKE-MERGE` hand-offs; 0 = hand off immediately. Live-tunable: raising it mid-wait extends an active window.

### Dev Types
Card list + editor (the harness combobox is **authoritative**, `08-harness-templates.md` §2):
- Name; **harness template** dropdown (options from `GET /harnesses`); **identifying prompt** textarea; **model** pin (e.g. `claude-fable-5` on the seeded senior-dev); per-type **max concurrency** integer.
- **Runtime & credentials block** (derived): registry image for the selected harness, readiness badge, per-requirement checklist — harness secret VALUES via `harness-secrets/{VAR}` (✓ from `secrets-check`), credential files via upload (`POST …/credentials` → `/data/secrets/{dev_type}/`). Flipping the combobox previews requirements; amber "unsaved harness change" until Save.
- **Connect via OAuth…** — per **Dev Type** (`POST /oauth/dev-types/{name}/start`), shown when the saved harness has a device-code flow; the credential lands in that Dev Type's `/data/secrets/{name}/` dir (two Dev Types on one harness = two accounts).
- **MCP servers**: free-text area, one CLI command per line (syntax hint per selected template, `08-harness-templates.md` §7), with the warning: *"These commands run inside the Dev container before the agent starts and are arbitrary code execution by design."* Execution semantics per `07-dev-runtime.md` §5 (failure ⇒ run fails).
- **Skills**: toggle-chip multi-select from the skill-store catalog (skill store v1; the shared `SelectionChips` control — see the multi-select convention above). Enabled on the claude-code harness only (other harnesses render the chips disabled with a hint and skip at dispatch); a selected-but-missing skill renders as the standard red ✕ stale chip and is skipped at dispatch with a warning.

### Skills (anchor `#/config/skills`)
The skill-store catalog: name / description / source badge (`store` = served from the `devcake-repos/skill-store` repo on the bundled Gitea; `bundled` = fallback copies shipped in the app image, used when the internal forge is disabled or unreachable). Actions: **Add skill**, **Edit in Gitea →** (store repo, operators push skills straight to `main`) and **Re-seed built-ins** (`POST /api/v1/skills/sync` — restores missing built-in files, never overwrites edits). Skill content is operator-controlled instructions injected into the agent session — same trust class as the MCP command area.

**Add skill** (no Gitea, no YAML — the non-technical path) opens a dialog with two modes:
- **Write** — name + "when should the agent use it?" (the trigger description) + a markdown instructions box. The app *generates* the frontmatter and commits `<name>/SKILL.md` to the store (`POST /api/v1/skills`).
- **Import files** — pick the skill's *folder* (containing `SKILL.md` plus any supporting files); the browser's directory picker preserves the nested layout (a plain file picker would flatten `refs/x.md` to `x.md`), OS/VCS cruft like `.DS_Store` is dropped, the name is read from the frontmatter, and the tree is committed under `<name>/` (`POST /api/v1/skills/import`).

Both validate server-side (name shape, required description, per-file 200 KB / total 1 MB caps, path-safety) and **refuse a name collision** with an existing store skill or a built-in unless the operator confirms **Overwrite** (409 → explicit confirm). Operator-created skills show a **delete** (trash) affordance; **built-ins have none** — they re-seed at boot, so the retirement path is to deselect them on Dev Types (`DELETE /api/v1/skills/{name}` refuses a built-in with 422). Skills created here carry `metadata.source: operator (admin panel)`.

### Assignments
Matrix: four Mission Types × (Dev-Type dropdown + **extra CLI args** textbox). The args are appended verbatim to the harness invocation for runs of that Mission Type — the mechanism for per-Mission-Type tuning like bounded-effort ONBOARD (`--max-turns 15` is the seeded default there for the claude-code harness). The textbox shows a hint naming the assigned Dev Type's harness; **reassigning a Mission Type to a Dev Type with a different harness triggers a warning offering to keep or clear the args** (they are harness-specific by nature). Same trust class as the MCP command area: admin-only, executed in the Dev container. Inline validation (every type assigned; a Dev Type may hold several).

### Limits
- **Global max Devs** integer (help text: effective ceiling = min(global, sum of per-type caps)).
- **Dev run timeout** minutes (default 120).
- Review-loop warning cadence; max attempts.

## 4. Runs page

A prominent **"Open Dagu ↗"** button (new tab, URL from `DAGU_UI_URL`) and a **"Clear runs"** danger button, above a live run table from `GET /api/v1/runs` (10 s poll) — **paginated** (25/page, `limit`+`offset`, total count) and **filterable by mission key** (substring match on key or run id). Rows show state and, where the app's judgment diverged from the executor's, the `verdict` (rejected/skipped/parked/handed-off — `02-domain-model.md` §7). Every row carries a **trace ↗ deep link** into OpenObserve pre-filtered to that run: `{OO}/web/traces?org_identifier=default&stream=default&period=1w&search_mode=spans&query=BASE64(devcake_run_id='<run_id>')` (URL shape verified live). No iframe.

**Run terminal (popup):** clicking any run row opens a terminal-styled modal (dark chrome, monospace, blinking cursor while live) showing the run's condensed output. Live runs follow `GET /runs/{id}/log/stream` over `EventSource` (the server replays the stored log first, so no separate initial fetch); terminal runs fetch `GET /runs/{id}/log?tail=1000` once. The stream's `end` event prints `[process exited]` and stops the cursor. This is a simulacrum, not a TTY: the harness runs headless (no PTY) and the app deliberately holds no docker.sock (`13-deployment.md` §5), so the feed is the Dev's own `run.log` relay — the same condensed lines Dagu captures in its step log. Client caps at ~5000 lines; ESC / backdrop / ✕ close it.

**Clear runs** opens a React confirmation dialog (never `window.confirm`) and on confirm calls `POST /api/v1/system/clear-runs`. That endpoint:

1. Stops any in-flight Dagu runs (`POST /dags/dev-run/stop-all`).
2. Deletes every local Run file under `/data/state/runs/` (including quarantined records), every run log under `/data/state/runlogs/` (open SSE followers get the end sentinel), and truncates `events.jsonl` (attempt counters and give-up watermarks reset — INV-1 / `10-persistence.md` §5).
3. Deletes every Dagu `dev-run` history record (`DELETE /dag-runs/dev-run/{id}`, paginated list).
4. Deletes OpenObserve log/trace streams (they recreate on next ingest; dashboards stay).
5. Trims the Redis ingress stream, drops leftover reply streams and per-run ACL users.

**Preserved:** `/data/config`, `/data/secrets`, PMO/forge state, circuit breakers (credential health).

## 5. Logs page

A prominent **"Open OpenObserve ↗"** button (new tab, URL from `OO_UI_URL`), plus canned deep links (also opening in new tabs): **Errors (last hour)** · **Trace by mission key** (input box → trace search) · **Cost dashboard** (`12-observability.md` §5). No iframe.

## 6. Auth and control-plane posture

v0 ships **HTTP basic auth at both nginx and FastAPI**, using the same
`ADMIN_USER`/`ADMIN_PASSWORD`. That is a **design choice** for a
**single-operator dedicated host** with **loopback** binds (`14-security.md`
§0, §4) — not multi-tenant RBAC. Admin credentials protect the GUI secret store
(`/data/secrets`), config (including MCP free-text / `extra_cli_args` = ACE in
Dev containers), and destructive **Clear runs**.

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
