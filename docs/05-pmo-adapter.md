# 05 — PMO Adapter: `PMOPort`, Linear, and Gitea Issues

> **Audience:** implementers of PMO adapters (Linear, Gitea Issues; later GitHub/GitLab Issues).
> **Depends on:** `02-domain-model.md` (Mission, MissionRef, labels), `00-overview.md` (INV-1, INV-4).

The domain core never sees vendor types. It programs against `PMOPort` (`app/devcake/ports/pmo.py`), a Python `Protocol` over the normalized DTOs of `02-domain-model.md`. In-tree adapters: **Linear** (`adapters/linear/`) and **Gitea Issues** (`adapters/gitea_issues/` — a pure `PMOPort`, **not** `ForgePort`). The port + registry (§1a) + contract-test batteries (§7) are **the template for every future PMO System**: adding one = an adapter package under `app/devcake/adapters/{system}/` implementing the full port + one `PMO_SYSTEMS` entry (plus its constructor branch in `make_pmo`).

## 0. The PMO capability contract (normative — any candidate system)

A PMO system qualifies for a DevCake adapter iff it satisfies all four capabilities (F2, docs/16 M9). The conformance battery (§7) is the acceptance gate — an adapter that passes it against a live instance is admissible; one that cannot express these capabilities is not "just another adapter" and needs its own design round:

- **(a) Missions as the unit of work.** The system's work items map straightforwardly onto Missions (`02-domain-model.md`): a stable vendor id, title/description, a status that normalizes onto backlog/in-progress/done/canceled, and a priority.
- **(b) Labels (or an equivalent) assign Mission Steps.** The DEVCAKE-* stage machinery needs an idempotently-creatable, atomically-swappable tag concept readable back on every item (`§5`).
- **(c) Traffic control via blocked-by relations.** Native "X blocks Y" dependencies, listable per item — the scheduler gate and Relations Mapper ride on them (`adr/0007`).
- **(d) A reliable activity feed.** Ordered comments + file attachments with **markdown fidelity** for backticked markers (`devcake:v1`, decomposition manifests, merge-retry markers): the feed is DevCake's persistent memory, so a PMO that rewrites comment bytes (ADF/rich-text — Jira) needs an explicit fidelity strategy before an adapter is attempted (ISSUES #35). Abandonment must be expressible (`cancel_mission` — a canceled/archived terminal state).

## 1. Port interface (normative signatures)

Reads and writes are keyed by `MissionRef(pmo_id, kind)` — the adapter dispatches on `ref.kind` **internally**, so vendor dualities (Linear's issue/project split) never leak into the domain. `PMOTransient` (retryable 429/5xx/network failure, `15-errors-and-retries.md`) also lives in `ports/pmo.py`.

```python
class PMOPort(Protocol):
    # ── reads ──
    async def list_missions(self, team_ref: str) -> list[Mission]: ...
        # non-terminal Projects + Issues of the ONE configured team.
        # No v0 caller (the poll loop uses list_all) — kept on the contract
        # for adapters where the filtered read is materially cheaper
        # (founder decision, v0 crystallization)
    async def list_all(self, team_ref: str) -> list[Mission]: ...
        # terminal included — the poll loop + /api/v1/missions
    async def get(self, ref: MissionRef) -> Mission: ...
    async def get_activity(self, ref: MissionRef, full: bool = False) -> Activity: ...
        # ordered feed; full=True walks entire history + reply structure +
        # mission attachments (ADR-0014). A ref without a comment feed
        # (Linear projects) returns the mission with entries=[] — never raises
    async def children_of(self, ref: MissionRef) -> list[Mission]: ...

    # ── writes ──
    async def post_feed(self, ref: MissionRef, markdown: str) -> None: ...
        # kind-appropriate channel: issue → comment, project → project update.
        # Feed POLICY (redaction, sentinel, suppression) is the orchestrator's
        # job; transport is the adapter's.
    async def set_status(self, ref: MissionRef, status: NormalizedStatus) -> None: ...
    async def swap_labels(self, ref: MissionRef, remove: set[str],
                          add: set[str]) -> None: ...
        # single call so each adapter implements the closest-to-atomic native op
    async def create_mission(self, team_ref: str, title: str, description: str,
                             priority: str, label_names: set[str],
                             parent_ref: Optional[str] = None) -> tuple[str, str]: ...
        # returns (key, pmo_id) — the id wires relation edges
    async def create_relation(self, blocker_id: str, blocked_id: str) -> None: ...
        # native "blocker blocks blocked" relation (adr/0007); duplicate-tolerant
    async def append_description(self, ref: MissionRef, text: str) -> None: ...
        # append-only INTENT with markdown fidelity (adr/0012); Linear
        # implements it as an unguarded read-modify-write, so a human edit
        # saved inside the window is lost (last writer wins — accepted for
        # the sole v0 caller: a short lineage footer on an issue canceled
        # moments later). Issues only; callers treat failures as non-fatal.
    async def ensure_labels(self, team_ref: str, names: set[str]) -> None: ...
        # creates the managed set in EVERY namespace the vendor requires
        # (Linear: team issue labels + workspace project labels)
    async def cancel_mission(self, ref: MissionRef) -> None: ...
        # terminal cancel/archive — used by decomposition (issue children) and
        # the merge sweep (PR closed unmerged)

    # ── assets ──
    async def upload_attachment(self, pmo_id: str, filename: str,
                                data: bytes) -> str: ...
        # returns the asset URL, referenced from a follow-up feed post
    async def download_asset(self, url: str) -> bytes: ...

    # ── meta ──
    async def health_probe(self, team_ref: str) -> PMOHealth: ...
    def capabilities(self) -> PMOCapabilities: ...
        # adapter self-description. No v0 reader — kept on the contract for
        # future multi-PMO scheduling / admin-UI behavior selection
        # (founder decision, v0 crystallization)
```

```python
class PMOHealth(BaseModel):        # neutral connection-probe result — replaces
    ok: bool                       # the old private reach-ins into vendor JSON
    workspace: str = ""            # resolved team/workspace reference
    managed_labels_present: int = 0   # of DevCake's managed set, found remotely
    managed_labels_expected: int = 0
    detail: str = ""

class PMOCapabilities(BaseModel):
    projects_supported: bool          # Linear: True
    project_labels_supported: bool    # Linear: True (project labels since 2025-06)
    attachment_max_bytes: int
    native_label_swap_atomic: bool    # Linear: True via issueUpdate(labelIds)
    relations_supported: bool = False # Linear: True (issue relations; issue-only)
```

`/health` and `POST /api/v1/connections/pmo/{name}/test` consume `health_probe` (the public port method) instead of reaching into adapter internals. Two deliberate behavior changes from the pre-port era: the managed-label count is the **intersection with `ALL_LABELS`** (a `DEVCAKE-CUSTOM-EXTRA` label no longer inflates it, as the old `startswith("DEVCAKE")` check did), and the test endpoint's response now carries `labels_expected` alongside `labels`.

## 1a. Adapter registry

`app/devcake/adapters/registry.py` is the single place that knows which PMO systems exist and how to construct them. The domain never imports it — `api/main.py` builds adapters here and injects them (`01-architecture.md` §3).

- **`PMO_SYSTEMS: dict[str, PMOSystemInfo]`** — registry metadata per system: `id`, `display_name`, `secret_env_vars`, `token_patterns` (regex sources), `secret_shape_prefixes`. (There is no `api_key_env_default` field — schema v4 stores secret VALUES in the GUI store.) The secret fields feed `security.redact` (`14-security.md` §7) and the admin SPA's paste guard — every registered system contributes its token shapes **whether configured or not**, so switching adapters never opens a redaction gap. Linear's entry: env names for redaction `LINEAR_API_KEY`, patterns `lin_api_…`/`lin_oauth_…`, prefixes `lin_api_`/`lin_oauth_`.
- **`make_pmo(inst) -> PMOPort`** constructs one adapter for a single `PMOInstance` (`inst`); the composition root builds **one manager/adapter per** configured entry in `config.pmos` (0..N). An unregistered `inst.system` raises.
- **`PMOInstance.system` is validated against `PMO_SYSTEMS`** at config-load/PUT time (pydantic field validator), so a typo'd system name is a 422, not a boot crash.
- **`GET /api/v1/connections/registry`** exposes the registered PMO systems and forges (display names, default env-var names, merged `secret_shape_prefixes`, `managed_labels_expected`) — the admin Config page's selectors and paste guard are driven from it, so adding an adapter never means editing the SPA (`11-admin-panel.md`).
- **Hot reload:** a successful config `PUT` calls `reload_connections()` — the PMO (and forge) adapters are rebuilt from the saved config, the orchestrator is repointed, and `ensure_labels` is re-run for the (possibly new) team. Label bootstrap is otherwise startup-only; without the re-ensure, a hot-swapped `team_key` would run unlabeled until restart.

The registry also carries the forge side (`forges()` / `make_forge`, `06-forge-adapter.md`); the shapes mirror each other.

## 2. Linear adapter — connection

- Endpoint: `POST https://api.linear.app/graphql`.
- Auth: personal API key in the `Authorization` header **without a `Bearer` prefix** (OAuth apps would use `Bearer`; v0 uses a personal API key from the GUI secret store for the instance — ADR-0011).
- Scope: **exactly one team per adapter instance** — `inst.team_key` (e.g. `ENG`). A stack may run 0..N PMO instances, each with its own team; **no work is ever done outside that instance's configured team** (mission-doc requirement) — every query filters by team, and `create_mission` targets it explicitly.
- Rate limits: ~5,000 requests/hour for API-key auth, plus GraphQL complexity limits. At the default 30 s poll of a single team this is comfortable; the adapter still backs off on `RATELIMITED`/429 per `15-errors-and-retries.md` (`_gql` raises `PMOTransient`).

## 3. Normalization tables (normative)

Everything below is **adapter internals behind the port** — the unified `get(ref)` dispatches to `_get_issue`/`_get_project`, `set_status` to `_set_issue_status`/`_set_project_status`, `swap_labels` to `_swap_issue_labels`/`_swap_project_labels`, and `post_feed` to `commentCreate`/`projectUpdateCreate`. The domain only ever sees the port surface of §1.

Linear workflow states carry a fixed `type` enum. DevCake maps by **type**, never by display name (teams rename states freely):

| Linear state `type` | Normalized status |
|---|---|
| `triage`, `backlog`, `unstarted` | `backlog` |
| `started` | `in_progress` |
| `completed` | `done` |
| `canceled` | `canceled` |

When *writing* a status, the adapter picks the team's first workflow state of the corresponding type (e.g. `set_status(:done)` → the team's first `completed`-type state).

Issue priority (Linear numeric):

| Linear | Normalized |
|---|---|
| 1 | `urgent` |
| 2 | `high` |
| 3 (and 0 = none) | `medium` |
| 4 | `low` |

Projects: Linear Project statuses come in five fixed categories — Backlog, Planned, In Progress, Completed, Canceled — mapped `Backlog/Planned→backlog`, `In Progress→in_progress`, `Completed→done`, `Canceled→canceled` (plus `Paused→backlog`). Project priority uses the same five-level scale and maps identically. Project labels are first-class in Linear (shipped 2025-06) — the same ten managed labels are ensured for projects.

**Blocked-by relations (adr/0007):** issue queries (`list_all`, `_get_issue`) additionally fetch `inverseRelations(first: 50)` with `pageInfo`; nodes of type `blocks` map to `Mission.blocked_by` (on issue B, `inverseRelations` holds relations where B is `relatedIssue`, so each node's `issue` is a blocker). A full first page is **cursor-walked** (`_paginate_issue_relations`, ceiling 10 × 50 with a fail-loud warning — adr/0012: a truncated read would under-block the gate and silently skip decomposition edge inheritance). `create_relation` → `issueRelationCreate(input: {issueId: blocker, relatedIssueId: blocked, type: blocks})`, tolerating the duplicate-relation error so decomposition resume stays idempotent. Relations are **issue-only** in Linear — projects always normalize with `blocked_by = []`.

**Cross-instance resolution (ADR-0009 amendment):** a blocker id that is not in this instance's snapshot may resolve through a PEER Linear instance's adapter (same `get` query, that instance's API key) via the orchestrator's `BlockerLocator` — Linear ids are workspace-global UUIDs, so first-success is unambiguous. This is an orchestrator concern; the adapter itself stays instance-bound.

**Verified live 2026-07-12 (sandbox):** (a) the direction above is correct end-to-end (`B.blocked_by == [A]`, A unaffected); (b) a duplicate `issueRelationCreate` returns an **idempotent success**, not an error — the adapter's error-tolerance is belt-and-suspenders; (c) the enlarged `list_all` costs complexity **1,310** against Linear's 3,000,000/hour budget (headers `x-complexity` / `x-ratelimit-complexity-*`) — ~5% of budget at 30 s polling; (d) the `` `devcake:v1` `` comment footer survives the create→read roundtrip byte-for-byte; (e) deleting a blocker issue clears the relation from the blocked issue immediately; (f) `projectUpdateCreate` posts a project update that reads back with the sentinel intact — the baton-pass channel for project-kind hand-offs (§6, `03-mission-lifecycle.md` §4a).

**Read robustness (normative):** every list read (`list_all` issues and projects, `children_of`) is **cursor-paginated** — the scheduling gate and the mapper's validator must see the whole team; a first-page-only read turns silent truncation into wrong scheduling (and, for the mapper, wrong *writes*). `inverseRelations` uses `first: 50` — Linear returns ALL relation types and the `blocks` filter is client-side, so an undersized page can evict a blocker; a full relations page is logged as a WARNING (never silent).

**`get_activity` pagination:** on issue refs, `get_activity` cursor-walks the full comment thread (`comments(first: 100, orderBy: createdAt, after: $cursor)`), per the read-robustness rule above — a single-page read was **verified lossy live on 2026-07-12** (DEV-50: 108 comments, 8 silently dropped). The ordering is pinned explicitly (verified: newest-first), so pages arrive newest-to-oldest and the safety ceiling of **10 pages / 1,000 comments** — a fail-loud valve at ~50× DevCake's post-hygiene comment rate, not a design limit — always keeps the newest comments, where the merge-state and conflict-resolve markers live (`03-mission-lifecycle.md` §4.1). Hitting the ceiling logs a truncation WARNING, never silent. Below it, `ACTIVITY.md` (`07-dev-runtime.md` §2), `_derive_seq`, and all marker counting see the complete thread. On project refs, `get_activity` returns the mission with `entries=[]` — Linear projects have no issue-style comments API (verified M2/M5).

## 4. Feed posts, transcripts, and attachments

- `post_feed(ref, markdown)` → issue: `commentCreate(input: {issueId, body})`; project: `projectUpdateCreate(input: {projectId, body})` — Linear's project-native feed. Body is Markdown either way.
- **Attachment-first feed policy (feed hygiene):** the activity feed is for *messages* — directives, short specifications, token reports, status notes. Bulk markdown always goes up as `.md` attachments referenced from a short sentinel-signed comment. This policy lives in the **orchestrator**, not the adapter (the port note in §1: policy above, transport below):
  - **Transcripts** (ADR-0014: the FULL session dump — every assistant-visible text block of the run) are ALWAYS uploaded as `{seq}_{TYPE}.md` attachments; the step comment keeps the backticked filename (the seq-derivation marker) + asset link **and carries the Dev's last message inline as a `>`-blockquote** (redacted, truncated at 2048 chars with a pointer to the attachment, posted with the externalization opt-out — the full text already rides the attachment). Quoting is the ADR-0014 D2 quarantine: `>`-quoted lines never count in any feed scan. Old-image payloads without a last message post the pointer-only comment. If the attachment upload fails (INV-5), the dump posts inline — blockquoted, with only the marker header unquoted.
  - **REVIEW reject reports** are uploaded as `{seq}_REVIEW_REPORT.md`; the feed gets a short "rejected (round N)" comment, while the PR comment keeps the full report.
  - **Safety net:** ANY issue comment over **2048 chars** is externalized the same way (as `comment-{ts}.md`) with a 300-char preview + link — the preview is built from UNQUOTED lines only (flattening newlines would otherwise land quarantined text back in scan scope). The finalize step comment opts out (`externalize=False`). The provenance sentinel goes on the short comment, never inside the attachment. Upload failures fall back to posting inline — an upload outage must never lose feed content. Devs always receive full content: `activity.get` downloads attachments into the Dev's `activity/` folder (`07-dev-runtime.md` §2).
  - Marker-bearing comments (`devcake:conflict-resolve:N`, `devcake:merge-retry`, `devcake:merge-handoff`, step markers) are short by construction so the markers always stay inline and countable.
- **Attachment references arrive named:** `ActivityEntry.attachments` is a `list[AttachmentRef{url, name, kind}]` — the adapter extracts asset URLs from comment bodies and resolves `name` from the markdown link text (`[r.md](https://uploads.linear.app/…)` → `name="r.md"`; a bare URL → `name=None`; `kind` per `02-domain-model.md` §1). The domain never parses vendor asset URLs, and basenames path-y link texts before writing the folder (a `[v1/r.md](…)` name lands as `r.md` everywhere).
- `upload_attachment` implements Linear's three-step flow:
  1. `fileUpload(contentType, filename, size)` mutation → `{uploadUrl, assetUrl, headers[]}`;
  2. server-side HTTP `PUT` of the bytes to `uploadUrl`, including every returned header (client-side PUT is CSP-blocked; the headers array must be converted to a header map);
  3. reference `assetUrl` in a comment. Note: `assetUrl` downloads require Linear auth — `download_asset` sends the key, and the Dev entrypoint downloads attachments through the app relay, which holds it (INV-4).

## 5. Label bootstrap

At startup (`04-orchestrator.md` §6) — and again after every config `PUT`, via `reload_connections()` (§1a) — the app calls `ensure_labels(team, {the ten managed labels})` — `02-domain-model.md` §5. Missing labels are created via `issueLabelCreate` scoped to the team (issue labels) and `projectLabelCreate` (workspace-level project labels). Existing labels are matched case-insensitively but always written in canonical uppercase form.

`swap_labels(ref, remove, add)` on issues is implemented as a single `issueUpdate(labelIds: [...])` computed from the live label set (read-modify-write with the removal and addition applied together), which is the closest-to-atomic operation Linear offers; `capabilities().native_label_swap_atomic = True`. The project branch (`_swap_project_labels`) does the same read-modify-write via `projectUpdate(labelIds)` against the workspace-level project-label ids.

**Full-history mode (ADR-0014):** `get_activity(ref, full=True)` — used ONLY by the activity-folder builder — walks the entire comment history (hard stop `MAX_COMMENT_PAGES_FULL` = 100 pages / 10,000 comments; tripping it sets `Activity.truncated` and logs ERROR instead of raising, because a raise would starve the Dev's `activity.get` reply), fetches reply structure (`id parent { id }` → `entry_id`/`parent_id`), and surfaces mission-level attachments (`Activity.mission_attachments`): description-embedded `uploads.linear.app` assets (named via markdown links) plus the issue's native `attachments` connection (`pageInfo { hasNextPage } nodes { url title }` — a >50 overflow warns, never silent), url-deduped and classified `file` (asset host) vs `link` (external reference). The DEFAULT (shallow) query is field-identical to the pre-ADR one (whitespace aside) — the four marker-scan call paths never pay full-history cost.

**Verified at M5:** Linear caps project `description` at **255 chars** — the long-form body lives in `content` (the adapter reads `content or description`); projects have **no issue-style comments API**, so project-run transcripts/token reports are recorded in the audit log + OpenObserve only (the substance lands on the child issues anyway, per ADR-0006).

**Verified at M2:** (a) Linear **project labels are a separate, workspace-level entity** (`projectLabels` / `projectLabelCreate`) — `ensure_labels` creates the ten managed labels in *both* namespaces, and `ProjectUpdateInput.labelIds` takes project-label ids, not issue-label ids; (b) Linear enforces a **per-query complexity budget** (~10k) — queries stay small and split rather than nesting team+issues+projects in one request.

## 6. Projects as Missions

Projects are normalized into Missions like Issues (`pmo_kind="project"`, `key="PRJ-{slug}"`). Policy (ADR `0006-projects-always-decompose.md`):

- A Project always takes the **high-complexity ONBOARD path**: it is decomposed into child Issues created inside the Project (`create_mission(..., parent_ref=project pmo_id)`), each labeled `DEVCAKE-CREATED`.
- The Project itself then receives `DEVCAKE-TRACKING` and stays open; the poll loop auto-completes it once all child Issues are `done`/`canceled` (`04-orchestrator.md` §1.3).
- Projects never take the trivial or normal ONBOARD paths.

## 7. Adapter contract tests

Two batteries. Every future `PMOPort` implementation reuses both shapes: the offline suite pins the port, the live battery pins the vendor behavior.

**Offline (`app/tests/test_pmo_contract.py`, runs in CI, no network):**

- **Port-surface pinning** — the exact method list of `PMOPort` is asserted, so a port edit must be deliberate.
- **Adapter conformance** — `LinearAdapter` implements every port method with matching parameter names.
- **Fake drift tripwire** — every port method a test fake (`FakePMO`/`MapPMO`/`DepPMO`) implements must match the port signature, keeping fakes honest as the contract evolves.
- **Unified dispatch on canned GraphQL** — via an injected `httpx.MockTransport`: `get(ref)` routes issue vs project queries by `ref.kind`; `post_feed` routes `commentCreate` vs `projectUpdateCreate`; `get_activity` on a project returns `entries=[]` without ever querying comments; attachment `name` resolution (named markdown link vs bare URL).
- **`health_probe` counting** — managed labels counted by `ALL_LABELS` intersection: a `DEVCAKE-CUSTOM-EXTRA` label must NOT count; `managed_labels_expected == len(ALL_LABELS)`.
- **Transient typing** — a 429 surfaces as `PMOTransient` from the port.

**Live (`scripts/contract_tests_pmo.py` — acceptance gate for every `PMOPort`):** runs inside the app container:

```bash
docker compose exec -T app python - < scripts/contract_tests_pmo.py
# optional: DEVCAKE_CONTRACT_INSTANCE=<name>
```

**System-agnostic:** the adapter is built with `make_pmo(inst)` from the registry (or a **direct harness** for Gitea Issues without GUI secrets — `DEVCAKE_CONTRACT_SYSTEM=gitea_issues`, `DEVCAKE_CONTRACT_API_BASE`, `DEVCAKE_CONTRACT_TEAM=owner/repo`, `DEVCAKE_CONTRACT_TOKEN`). Temp issues are created with **`create_mission`** and cleaned with **`cancel_mission`** — port methods only (no private GraphQL). Profile-aware rows (2, 3, 10) branch on `capabilities().projects_supported` so Linear (full status + projects) and forge-issue systems (open→backlog, issue-only, priority always medium) share one script.

| # | Scenario |
|---|---|
| 1 | `list_missions` returns only the configured team's items, excluding terminal ones |
| 2 | Status normalization round-trips (Linear: all four; forge-issue: open→backlog, done/canceled closed variants) |
| 3 | Priority: Linear urgent + medium; forge-issue always medium |
| 4 | `swap_labels` removes+adds in one observable step; no intermediate two-stage-label state visible to a subsequent `get` |
| 5 | `ensure_labels` is idempotent and case-insensitive |
| 5b | `health_probe` reports ok + the full managed set present — the public replacement for `_team` reach-ins |
| 8 | `get_activity` ordering is chronological and attachments are extracted with fetchable URLs |
| 9 | Rate-limit (429/RATELIMITED) surfaces as `PMOTransient` |
| 10 | Linear: project normalized + capabilities; forge-issue: issue-only capabilities truthful |
| 11 | `cancel_mission` terminal + idempotent |
| 12 | `post_feed` marker/markdown fidelity (`` `devcake:v1` ``) |
| 13 | Attachment upload/download round-trip |
| 14 | `create_relation` + `blocked_by` (duplicate-tolerant) when `relations_supported` |

The numbering is historical and stable (test files reference rows by number). The gaps are covered elsewhere: row 6 (attachment-first feed policy, > 2048-char externalization, inline fallback) is orchestrator policy, tested in `app/tests/test_transitions.py`; row 7 (`create_mission` labeling/priority/team scoping) is exercised through the decomposition tests; Linear-specific relation GraphQL parsing also has hermetic coverage in `app/tests/test_linear_relations.py`.

## 8. Webhook readiness

A push-based `watch()` seam was designed but never implemented; it is recorded as future work in `16-roadmap.md`. v0 polls every 30–60 s, well within rate limits.

## 9. Gitea Issues adapter (forge-issue family)

First **forge-issue** PMO: issue trackers that are not Linear-style product PMOs. System id **`gitea_issues`** (package `adapters/gitea_issues/`) is deliberately distinct from forge id **`gitea`** so the F1 import tripwire and hexagonal boundaries stay clean — **never** subclass or wrap `GiteaForge` / `InternalForgePort`.

### 9.1 Connection

| Config field | Meaning |
|---|---|
| `system` | `gitea_issues` |
| `api_base` | Gitea origin reachable **from the app container**. Bundled stack: `http://gitea:3000`. Browser UI remains `http://localhost:3300` (`ROOT_URL`). External: `https://gitea.example.com`. |
| `team_key` | Exactly `owner/repo` of a **dedicated issues board** (e.g. `devcake-pmo/missions`). Not a per-mission internal-forge work repo. |
| `api_key` | PAT with issue (and label) write on that repo. GUI-stored; **empty `token_patterns`** (40-hex tokens collide with git SHAs — value registration only, same posture as the Gitea forge). |

Internal vs external is **only** `api_base` + token + board path — one system, not two registry entries. Credential separation is mandatory: PMO PAT ≠ forge write tokens ≠ `GITEA_ADMIN_*`.

### 9.2 Normalization (forge-issue profile)

| Gitea | Normalized |
|---|---|
| `state=open` | `backlog` always (no vendor `in_progress`; stages ride `DEVCAKE-*` labels) |
| `state=closed` without cancel footer | `done` |
| `state=closed` with `` `devcake:canceled:v1` `` in body | `canceled` |
| priority | always `medium` (no issue priority field) |
| `pmo_id` | issue **index** (`number`) as string — repo-scoped; API paths use index |
| `key` | `{owner}/{repo}#{number}` |
| `pmo_kind` | always `issue` (`projects_supported=False`) |

`cancel_mission` closes the issue and appends the cancel footer (idempotent). Closing an issue that still has **open blockers** 412s on Gitea 1.24 — the adapter **clears dependencies first** then closes (scheduler already consumed them).

**Cross-instance blockers are unsupported for Gitea Issues** (ADR-0009 amendment): `pmo_id` is a repo-scoped issue *number*, so ids collide across instances — the `BlockerLocator` hard-refuses peer resolution and peer run history for `gitea_issues` (never best-effort). Same-instance blocker semantics are unaffected.

### 9.3 Relations, labels, feed, attachments

- **Relations:** `POST/GET/DELETE …/issues/{index}/dependencies` with `IssueMeta{owner,repo,index}`. `create_relation(blocker, blocked)` makes `blocked` depend on `blocker`. Duplicate create returns 500 “does already exist” → treated as success. `ensure_labels` enables `internal_tracker.enable_issue_dependencies` on the board (off by default on new repos).
- **Labels:** repo labels; `PUT …/issues/{index}/labels` replaces the full set (`native_label_swap_atomic=True`). Managed set ensured uppercase.
- **Feed:** issue comments; markdown markers round-trip byte-for-byte (live-verified).
- **Attachments:** multipart `POST …/issues/{index}/assets`. Gitea returns `browser_download_url` with **ROOT_URL** / `GITEA_UI_URL` (bundled: `localhost:3300`); the adapter rewrites **presentation hosts** (`api_base` host, `GITEA_UI_URL` host, loopback) onto `api_base` so the app container can download, pins path to `/attachments/` and origin netloc, and refuses off-allowlist redirects with the PMO token (docs/14 §11). Operator use of the Gitea UI and direct git remains unrestricted.

### 9.4 Operator setup (bundled Gitea)

1. UI `http://localhost:3300` → create org/repo e.g. `devcake-pmo/missions` (empty git repo is fine).
2. Mint a PAT with issue write on that repo.
3. Admin → PMO page (`#/pmo`, Adapters group) → system **Gitea Issues**, api base `http://gitea:3000`, issues repo `devcake-pmo/missions`, paste PAT → Save → Test connection (expect 10/10 managed labels).
4. Label an issue `DEVCAKE` (opt-in) and poll.

Work forge remains independent (GitHub/GitLab/Gitea repo cards, or empty → per-mission internal forge).

### 9.5 Live contract battery

**Wired into `scripts/ci_suite.sh`** (after the forge battery). Default lane with no env extras: if `GITEA_ADMIN_*` is present (app container), auto-provision a scratch `owner/pmo-contract-*` board, run all scenarios, delete the board + token. Zero external tokens — same posture as `contract_tests_forge.py`.

```bash
# CI / local full suite (auto gitea_issues lane)
docker compose exec -T app python - < scripts/contract_tests_pmo.py

# Or pin a configured instance / direct PAT board:
#   DEVCAKE_CONTRACT_INSTANCE=…  or  DEVCAKE_CONTRACT_SYSTEM=gitea_issues + TEAM/TOKEN/API_BASE
```

Expect all rows **PASS** (1–5, 5b, 8–14 when `relations_supported`). Same script against a Linear config instance uses the Linear profile rows (projects + urgent priority).

**GHA `ci.yml` note:** the minimal dispatch compose has no Gitea, so the PMO live battery stays in **local `ci_suite.sh`** (full stack), not the PR-minimal job — same as the forge contract battery today.

### 9.6 Path to GitHub / GitLab Issues

Same forge-issue profile (issue-only, label stages, open→backlog, markdown comments, dependency/links). New package per vendor; do **not** grow a shared Issues Port until a second forge-issue adapter exists. Live gate: same `scripts/contract_tests_pmo.py` once the system is registered.
