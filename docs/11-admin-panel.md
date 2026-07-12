# 11 — Admin Panel: UI Spec and API Contract

> **Audience:** frontend implementer + app API implementer.
> **Depends on:** `02-domain-model.md` (AppConfig, DevType), `10-persistence.md` (write path), `13-deployment.md` (proxy topology).

Simple but beautiful: a static SPA (React + Vite + Tailwind — one page per tab, no client state library) served by nginx in the `admin` container, which reverse-proxies `/api/*` → `app:8000` (no CORS). The Dagu and OpenObserve UIs are **not** embedded: the Executor and Logs tabs open them in new browser tabs via buttons (confirmed decision — no iframes; their URLs reach the admin SPA as nginx-templated env vars `DAGU_UI_URL` / `OO_UI_URL`, `13-deployment.md` §2).

Three tabs: **Config**, **Executor**, **Logs**. Plus an ever-present header health strip (`GET /api/v1/health`: PMO reachable, forge reachable, Redis, Dagu, OpenObserve, config valid, per-Dev-Type circuit-breaker state).

**Milestone note:** the Vite/React/Tailwind SPA shipped at M6 (`admin/spa/`, multi-stage Docker build); the M0–M5 static shell remains in `admin/site/` as a fallback artifact only. All confirmation dialogs are React components — never native `window.confirm`/`alert` (they block automation and the browser).

## 1. REST API contract (`/api/v1`)

| Method + path | Purpose |
|---|---|
| `GET /api/v1/health` | Component health + circuit breakers |
| `GET /api/v1/config` · `PUT /api/v1/config` | General settings (AppConfig minus dev types). PUT validates server-side (pydantic); errors return field-keyed messages surfaced inline |
| `GET /api/v1/harnesses` | The harness registry: derived image, credential requirements, OAuth availability per `harness_template` — the Dev Type card renders (and previews unsaved harness switches) from this |
| `GET /api/v1/dev-types` · `POST /api/v1/dev-types` | List (enriched: `harness` info + `secrets_present`) / create Dev Types |
| `POST /api/v1/oauth/dev-types/{name}/start` · `GET /api/v1/oauth/status/{run_id}` | Per-dev-type device-code login (docs/16 M6); credential lands in `/data/secrets/{name}/` |
| `GET/PUT/DELETE /api/v1/dev-types/{name}` | CRUD one Dev Type. DELETE refuses while assigned to a Mission Type |
| `POST /api/v1/dev-types/{name}/credentials` | Either multipart file upload (credentials JSON → `/data/secrets/{name}/`, 0600) or `{"env_var": "NAME"}` reference |
| `GET /api/v1/assignments` · `PUT /api/v1/assignments` | Mission-Type → Dev-Type map. Validation: all four types assigned, each to exactly one existing Dev Type |
| `GET /api/v1/env-check?names=A,B` | Set/unset status (never values) of env vars in the app's environment — powers the Config tab's inline ✓/✗ on `*_env` fields |
| `POST /api/v1/connections/pmo/test` | Live probe: auth + team fetch; returns team name + label status |
| `POST /api/v1/connections/forge/test` | Live probe: auth + repo fetch + default branch (+ reviewer token check + branch-protection state) |
| `GET /api/v1/runs?mission_key=…&limit=…` | Read-only run history (from `/data/state/runs/`) for context |
| `GET /api/v1/runs/{run_id}/log?tail=N` | Plain-text condensed run output (from `/data/state/runlogs/`, relayed live by the Dev via `run.log` — `09-messaging.md` §3) |
| `GET /api/v1/runs/{run_id}/log/stream` | SSE follow of the same log: replays the stored lines, then streams new ones until the run reaches a terminal state (`event: end`). Sends `X-Accel-Buffering: no` so nginx doesn't buffer; 15 s `: ping` heartbeats stay under nginx's 60 s read timeout |
| `POST /api/v1/system/clear-runs` | Operator wipe: stop in-flight Devs, delete local run records + audit log, purge Dagu run history, delete OpenObserve log/trace streams. Config + secrets + PMO/forge untouched (`10-persistence.md` §5) |
| `POST /api/v1/relations-mapper/run` | Manually dispatch a Relations Mapper run (`03-mission-lifecycle.md` §4b). Works regardless of the `enabled` toggle (which governs only the interval service); 422 without a valid `dev_type`, 409 while a mapper run is active |
| `GET /api/v1/missions` | Debug: current derived Missions + types (M2, `16-roadmap.md`); includes `blocked_by` keys, and the reason string names open blockers |

All writes go through the app (single validation point, `10-persistence.md` §4).

## 2. Config tab — sections and fields

### Traffic control (added with adr/0007)
- **Mission intake toggle** (`intake_paused`) — OFF pauses DevCake's intake: no new runs start (missions or mapper) while the operator rearranges missions in Linear. In-flight runs finish normally (and may still update labels/statuses as they complete — pause freezes dispatch, not consequence), and the merge/tracking sweeps keep running; flipping back resumes on the next poll cycle. While paused, the header banner is **stateful**: it counts the in-flight runs still finishing ("N runs still finishing…") and flips to "all runs drained; Linear is all yours" at zero — no trip to the Executor tab needed. Save errors surface inline (the toggle never fails silently).
- **Relations Mapper card** — four controls: (a) **Run now** button → `POST /api/v1/relations-mapper/run`, showing the dispatched run id (or the 409/422 error) inline; (b) **interval in minutes**; (c) **Dev Type combobox** (defaults to the seeded `junior-dev`; required before enabling or running); (d) **Periodic service ON/OFF toggle** (default OFF — manual-only out of the box). Controls disable while a save is in flight (no stale-state races). The card shows the **degraded** state from `/health` (`mapper_degraded`: last 3 runs dead → periodic backs off; Run now still works and resets it). Deleting the mapper's Dev Type is refused with 409.

### Header banners (from `GET /health`)
Amber/blue strips under the header, all driven by the 10 s health poll: intake paused (stateful, above) · **dependency cycle detected** (names the loop: "DEV-10 → DEV-12 → DEV-10 — these missions will never start until a relation is deleted") · **default branch unprotected** (a Dev's forge token could merge without review — `13-deployment.md` §8a) · **out-of-pipeline activity** (a mission's PR merged mid-pipeline — `15-errors-and-retries.md`) · circuit breaker tripped (pre-existing).

**Field-level help (added 2026-07-11, after the token_env incident):** every
field carries a hover `?` tooltip explaining what it means and what shape of
value it wants. Fields that take an **env var name** (`api_key_env`,
`token_env`, `reviewer_token_env`) additionally validate live: a value shaped
like a secret (token prefixes, > 40 chars) shows a red warning that the field
wants the variable's NAME and the secret goes in `.env`; a well-formed name is
checked against `GET /api/v1/env-check` and shows `✓ set` or `✗ not set`. The
connection-test endpoints short-circuit with a plain-language error when the
configured env var resolves empty (instead of `Illegal header value b'Bearer '`).

### PMO connection
- API key: env-var name (default `LINEAR_API_KEY`) or direct value (stored to app env file — with a hint that env vars are preferred).
- **Team picker**: populated by a live Linear query once the key validates (from `connections/pmo/test`).
- **Adoption mode toggle** — `opt_in` (default) vs `opt_out`. Flipping to `opt_out` opens a confirmation dialog: *"DevCake will adopt EVERY non-completed Issue and Project in this team — including the entire existing backlog — and start working through them by priority, consuming tokens. In opt-in mode it only touches items you label `DEVCAKE`."* The change is applied only on explicit confirm; flipping back to `opt_in` confirms symmetrically (in-flight runs finish; unlabeled missions are simply no longer scheduled).
- Poll interval (seconds).

### Repository
- Forge selector (GitHub / GitLab) — one active repo.
- Repo URL, token env var, optional **reviewer token** env var (tooltip: enables formal PR approval, `06-forge-adapter.md` §4).
- **`auto_merge` toggle** — default OFF; enabling shows a confirm dialog: *"DevCake will merge its own pull requests to the default branch without human review. On GitHub without a reviewer token, merges proceed without formal approval."*

### Dev Types
Card list + editor (2026-07-12 rework — the harness combobox is **authoritative**, `08-harness-templates.md` §2):
- Name; **harness template** dropdown (options from `GET /harnesses`); **identifying prompt** textarea; **model** pin; per-type **max concurrency** integer.
- **Runtime & credentials block** (derived, read-only structure): the registry image for the *currently selected* harness, a readiness badge, and a per-requirement checklist — each `credential_env` var with live ✓/✗ from `GET /env-check`, each required secret file with ✓/✗ from the enriched `secrets_present` plus an **upload button** (filename forced to the registry `secret_file`). Flipping the combobox previews the new harness's requirements immediately, with an amber "unsaved harness change" note until Save. Any one ✓ (env var or file) suffices.
- **Connect via OAuth…** — per **Dev Type** (`POST /oauth/dev-types/{name}/start`), shown when the saved harness has a device-code flow; the credential lands in that Dev Type's `/data/secrets/{name}/` dir (two Dev Types on one harness = two accounts).
- **MCP servers**: free-text area, one CLI command per line (syntax hint per selected template, `08-harness-templates.md` §7), with the warning: *"These commands run inside the Dev container before the agent starts and are arbitrary code execution by design."* Execution semantics per `07-dev-runtime.md` §5 (failure ⇒ run fails).

### Assignments
Matrix: four Mission Types × (Dev-Type dropdown + **extra CLI args** textbox). The args are appended verbatim to the harness invocation for runs of that Mission Type — the mechanism for per-Mission-Type tuning like bounded-effort ONBOARD (`--max-turns 15` is the seeded default there for the claude-code harness). The textbox shows a hint naming the assigned Dev Type's harness; **reassigning a Mission Type to a Dev Type with a different harness triggers a warning offering to keep or clear the args** (they are harness-specific by nature). Same trust class as the MCP command area: admin-only, executed in the Dev container. Inline validation (every type assigned; a Dev Type may hold several).

### Limits
- **Global max Devs** integer (help text: effective ceiling = min(global, sum of per-type caps)).
- **Dev run timeout** minutes (default 120).
- Review-loop warning cadence; max attempts.

Save = `PUT` per section; optimistic UI with server errors inline.

## 3. Executor tab

A prominent **"Open Dagu ↗"** button (new tab, URL from `DAGU_UI_URL`) and a **"Clear runs"** danger button, above a live run table from `GET /api/v1/runs` — **paginated** (25/page, `limit`+`offset`, total count) and **filterable by mission key** (substring match on key or run id). Every row carries a **trace ↗ deep link** into OpenObserve pre-filtered to that run: `{OO}/web/traces?org_identifier=default&stream=default&period=1w&search_mode=spans&query=BASE64(devcake_run_id='<run_id>')` (URL shape verified live at M6). No iframe.

**Run terminal (popup):** clicking any run row opens a terminal-styled modal (dark chrome, monospace, blinking cursor while live) showing the run's condensed output. Live runs follow `GET /runs/{id}/log/stream` over `EventSource` (the server replays the stored log first, so no separate initial fetch); terminal runs fetch `GET /runs/{id}/log?tail=1000` once. The stream's `end` event prints `[process exited]` and stops the cursor. This is a simulacrum, not a TTY: the harness runs headless (no PTY) and the app deliberately holds no docker.sock (`13-deployment.md` §5), so the feed is the Dev's own `run.log` relay — the same condensed lines Dagu captures in its step log. Client caps at ~5000 lines; ESC / backdrop / ✕ close it.

**Clear runs** opens a React confirmation dialog (never `window.confirm`) and on confirm calls `POST /api/v1/system/clear-runs`. That endpoint:

1. Stops any in-flight Dagu runs (`POST /dags/dev-run/stop-all`).
2. Deletes every local Run file under `/data/state/runs/`, every run log under `/data/state/runlogs/` (open SSE followers get the end sentinel), and truncates `events.jsonl` (attempt counters and give-up watermarks reset — INV-1 / `10-persistence.md` §5).
3. Deletes every Dagu `dev-run` history record (`DELETE /dag-runs/dev-run/{id}`, paginated list).
4. Deletes OpenObserve log/trace streams (they recreate on next ingest; dashboards stay).
5. Trims the Redis ingress stream, drops leftover reply streams and per-run ACL users.

**Preserved:** `/data/config`, `/data/secrets`, Linear/GitHub/GitLab state, circuit breakers (credential health).

## 4. Logs tab

A prominent **"Open OpenObserve ↗"** button (new tab, URL from `OO_UI_URL`), plus three canned deep links (also opening in new tabs): **Errors (last hour)** · **Trace by mission key** (input box → trace search) · **Cost dashboard** (`12-observability.md` §5). No iframe.

## 5. Auth

v0 ships **HTTP basic auth at the nginx layer**, covering the SPA *and* the `/api` proxy in one gate (the API is the actual sensitive surface: it writes credentials and MCP commands). Credentials come from `ADMIN_USER`/`ADMIN_PASSWORD` env (`13-deployment.md` §3); nginx builds the htpasswd at container start; the browser's native prompt is the login UI — no session code in v0. The `app` service exposes no host port, so nginx is the only way in. OIDC/SSO is the documented upgrade path (`14-security.md` §7).
