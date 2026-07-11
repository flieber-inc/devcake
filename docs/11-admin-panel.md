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
| `GET /api/v1/dev-types` · `POST /api/v1/dev-types` | List / create Dev Types |
| `GET/PUT/DELETE /api/v1/dev-types/{name}` | CRUD one Dev Type. DELETE refuses while assigned to a Mission Type |
| `POST /api/v1/dev-types/{name}/credentials` | Either multipart file upload (credentials JSON → `/data/secrets/{name}/`, 0600) or `{"env_var": "NAME"}` reference |
| `GET /api/v1/assignments` · `PUT /api/v1/assignments` | Mission-Type → Dev-Type map. Validation: all four types assigned, each to exactly one existing Dev Type |
| `POST /api/v1/connections/pmo/test` | Live probe: auth + team fetch; returns team name + label status |
| `POST /api/v1/connections/forge/test` | Live probe: auth + repo fetch + default branch (+ reviewer token check) |
| `GET /api/v1/runs?mission_key=…&limit=…` | Read-only run history (from `/data/state/runs/`) for context |
| `POST /api/v1/system/clear-runs` | Operator wipe: stop in-flight Devs, delete local run records + audit log, purge Dagu run history, delete OpenObserve log/trace streams. Config + secrets + PMO/forge untouched (`10-persistence.md` §5) |
| `GET /api/v1/missions` | Debug: current derived Missions + types (M2, `16-roadmap.md`) |

All writes go through the app (single validation point, `10-persistence.md` §4).

## 2. Config tab — sections and fields

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
Card list + editor:
- Name; **harness template** dropdown (the three from `08-harness-templates.md`); **identifying prompt** textarea.
- **Credentials**: radio — (a) API-key env var name, or (b) credentials JSON file upload (OAuth/subscription preferred; per-harness how-to hints from `08-harness-templates.md` §4).
- **MCP servers**: free-text area, one CLI command per line (syntax hint per selected template, `08-harness-templates.md` §7), with the warning: *"These commands run inside the Dev container before the agent starts and are arbitrary code execution by design."* Execution semantics per `07-dev-runtime.md` §5 (failure ⇒ run fails).
- Per-type **max concurrency** integer.

### Assignments
Matrix: four Mission Types × (Dev-Type dropdown + **extra CLI args** textbox). The args are appended verbatim to the harness invocation for runs of that Mission Type — the mechanism for per-Mission-Type tuning like bounded-effort ONBOARD (`--max-turns 15` is the seeded default there for the claude-code harness). The textbox shows a hint naming the assigned Dev Type's harness; **reassigning a Mission Type to a Dev Type with a different harness triggers a warning offering to keep or clear the args** (they are harness-specific by nature). Same trust class as the MCP command area: admin-only, executed in the Dev container. Inline validation (every type assigned; a Dev Type may hold several).

### Limits
- **Global max Devs** integer (help text: effective ceiling = min(global, sum of per-type caps)).
- **Dev run timeout** minutes (default 120).
- Review-loop warning cadence; max attempts.

Save = `PUT` per section; optimistic UI with server errors inline.

## 3. Executor tab

A prominent **"Open Dagu ↗"** button (new tab, URL from `DAGU_UI_URL`) and a **"Clear runs"** danger button, above a live run table from `GET /api/v1/runs` — **paginated** (25/page, `limit`+`offset`, total count) and **filterable by mission key** (substring match on key or run id). Every row carries a **trace ↗ deep link** into OpenObserve pre-filtered to that run: `{OO}/web/traces?org_identifier=default&stream=default&period=1w&search_mode=spans&query=BASE64(devcake_run_id='<run_id>')` (URL shape verified live at M6). No iframe.

**Clear runs** opens a React confirmation dialog (never `window.confirm`) and on confirm calls `POST /api/v1/system/clear-runs`. That endpoint:

1. Stops any in-flight Dagu runs (`POST /dags/dev-run/stop-all`).
2. Deletes every local Run file under `/data/state/runs/` and truncates `events.jsonl` (attempt counters and give-up watermarks reset — INV-1 / `10-persistence.md` §5).
3. Deletes every Dagu `dev-run` history record (`DELETE /dag-runs/dev-run/{id}`, paginated list).
4. Deletes OpenObserve log/trace streams (they recreate on next ingest; dashboards stay).
5. Trims the Redis ingress stream, drops leftover reply streams and per-run ACL users.

**Preserved:** `/data/config`, `/data/secrets`, Linear/GitHub/GitLab state, circuit breakers (credential health).

## 4. Logs tab

A prominent **"Open OpenObserve ↗"** button (new tab, URL from `OO_UI_URL`), plus three canned deep links (also opening in new tabs): **Errors (last hour)** · **Trace by mission key** (input box → trace search) · **Cost dashboard** (`12-observability.md` §5). No iframe.

## 5. Auth

v0 ships **HTTP basic auth at the nginx layer**, covering the SPA *and* the `/api` proxy in one gate (the API is the actual sensitive surface: it writes credentials and MCP commands). Credentials come from `ADMIN_USER`/`ADMIN_PASSWORD` env (`13-deployment.md` §3); nginx builds the htpasswd at container start; the browser's native prompt is the login UI — no session code in v0. The `app` service exposes no host port, so nginx is the only way in. OIDC/SSO is the documented upgrade path (`14-security.md` §7).
