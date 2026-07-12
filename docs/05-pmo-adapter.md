# 05 — PMO Adapter: `PMOPort` and the Linear Implementation

> **Audience:** implementers of the Linear adapter now; implementers of GitHub Issues / GitLab / Monday adapters later.
> **Depends on:** `02-domain-model.md` (Mission, MissionDraft, labels), `00-overview.md` (INV-1, INV-4).

The domain core never sees Linear types. It programs against `PMOPort`, a Python `Protocol` over the normalized DTOs of `02-domain-model.md`. The Linear adapter is the only v0 implementation; the port + contract-test battery (§7) is the template for every future PMO System.

## 1. Port interface (normative signatures)

```python
class PMOPort(Protocol):
    async def list_missions(self, team_ref: str) -> list[Mission]: ...
    async def get_mission(self, pmo_id: str) -> Mission: ...
    async def get_activity(self, pmo_id: str) -> Activity: ...
        # ordered feed: comments, status changes, attachments (with download URLs)
    async def post_comment(self, pmo_id: str, markdown: str) -> None: ...
    async def upload_attachment(self, pmo_id: str, filename: str, data: bytes) -> str: ...
        # returns the asset URL, referenced from a follow-up comment
    async def set_status(self, pmo_id: str, status: NormalizedStatus) -> None: ...
    async def swap_labels(self, pmo_id: str, remove: set[str], add: set[str]) -> None: ...
        # single call so each adapter implements the closest-to-atomic native operation
    async def create_relation(self, blocker_id: str, blocked_id: str) -> None: ...
        # native "blocker blocks blocked" relation (adr/0007); duplicate-tolerant
    async def create_mission(self, team_ref: str, draft: MissionDraft) -> Mission: ...
    async def cancel_mission(self, pmo_id: str) -> None: ...
    async def ensure_labels(self, team_ref: str, names: set[str]) -> None: ...
    async def watch(self, team_ref: str) -> AsyncIterator[ChangeEvent]: ...
        # v0: wraps the poller; a future webhook receiver implements the same signature
    def capabilities(self) -> PMOCapabilities: ...
```

```python
@dataclass(frozen=True)
class PMOCapabilities:
    projects_supported: bool          # Linear: True
    project_labels_supported: bool    # Linear: True (project labels since 2025-06)
    attachment_max_bytes: int
    native_label_swap_atomic: bool    # Linear: True via issueUpdate(labelIds)
    relations_supported: bool         # Linear: True (issue relations; issue-only)
```

## 2. Linear adapter — connection

- Endpoint: `POST https://api.linear.app/graphql`.
- Auth: personal API key in the `Authorization` header **without a `Bearer` prefix** (OAuth apps would use `Bearer`; v0 uses a personal API key configured as `pmo.api_key_env`).
- Scope: exactly one team, `pmo.team_key` (e.g. `ENG`). **No work is ever done outside the configured team** (mission-doc requirement) — every query filters by team, and `create_mission` targets it explicitly.
- Rate limits: ~5,000 requests/hour for API-key auth, plus GraphQL complexity limits. At the default 30 s poll of a single team this is comfortable; the adapter still backs off on `RATELIMITED`/429 per `15-errors-and-retries.md`.

## 3. Normalization tables (normative)

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

Projects: Linear Project statuses come in five fixed categories — Backlog, Planned, In Progress, Completed, Canceled — mapped `Backlog/Planned→backlog`, `In Progress→in_progress`, `Completed→done`, `Canceled→canceled`. Project priority uses the same five-level scale and maps identically. Project labels are first-class in Linear (shipped 2025-06) — the same ten managed labels are ensured for projects.

**Blocked-by relations (adr/0007):** issue queries (`list_all`, `get_mission`) additionally fetch `inverseRelations(first: 10) { nodes { type issue { id } } }`; nodes of type `blocks` map to `Mission.blocked_by` (on issue B, `inverseRelations` holds relations where B is `relatedIssue`, so each node's `issue` is a blocker). `create_relation` → `issueRelationCreate(input: {issueId: blocker, relatedIssueId: blocked, type: blocks})`, tolerating the duplicate-relation error so decomposition resume stays idempotent. Relations are **issue-only** in Linear — projects always normalize with `blocked_by = []`.

**Verified live 2026-07-12 (sandbox):** (a) the direction above is correct end-to-end (`B.blocked_by == [A]`, A unaffected); (b) a duplicate `issueRelationCreate` returns an **idempotent success**, not an error — the adapter's error-tolerance is belt-and-suspenders; (c) the enlarged `list_all` costs complexity **1,310** against Linear's 3,000,000/hour budget (headers `x-complexity` / `x-ratelimit-complexity-*`) — ~5% of budget at 30 s polling; (d) the `` `devcake:v1` `` comment footer survives the create→read roundtrip byte-for-byte; (e) deleting a blocker issue clears the relation from the blocked issue immediately; (f) `projectUpdateCreate` posts a project update that reads back with the sentinel intact — the baton-pass channel for project-kind hand-offs (§6, `03-mission-lifecycle.md` §4a).

**Read robustness (normative):** every list read (`list_all` issues and projects, `children_of_project`) is **cursor-paginated** — the scheduling gate and the mapper's validator must see the whole team; a first-page-only read turns silent truncation into wrong scheduling (and, for the mapper, wrong *writes*). `inverseRelations` uses `first: 50` — Linear returns ALL relation types and the `blocks` filter is client-side, so an undersized page can evict a blocker; a full relations page is logged as a WARNING (never silent). Port method `create_project_update(project_id, body)` posts to the project-native feed.

## 4. Comments, transcripts, and attachments

- `post_comment` → `commentCreate(input: {issueId, body})`; body is Markdown.
- **Transcript size policy:** Linear documents no hard comment length limit, so DevCake sets its own: payloads over **50 KB** are uploaded as `.md` file attachments named `{seq}_{TYPE}.md` instead of inline comments (mission-doc requirement), then referenced from a short comment.
- `upload_attachment` implements Linear's three-step flow:
  1. `fileUpload(contentType, filename, size)` mutation → `{uploadUrl, assetUrl, headers[]}`;
  2. server-side HTTP `PUT` of the bytes to `uploadUrl`, including every returned header (client-side PUT is CSP-blocked; the headers array must be converted to a header map);
  3. reference `assetUrl` in a comment. Note: `assetUrl` downloads require Linear auth — the Dev entrypoint downloads attachments through the app relay, which holds the key (INV-4).

## 5. Label bootstrap

At startup (`04-orchestrator.md` §6) the app calls `ensure_labels(team, {the ten managed labels})` — `02-domain-model.md` §5. Missing labels are created via `issueLabelCreate` scoped to the team (issue labels) and the project-label equivalent. Existing labels are matched case-insensitively but always written in canonical uppercase form.

`swap_labels` is implemented as a single `issueUpdate(labelIds: [...])` computed from the live label set (read-modify-write with the removal and addition applied together), which is the closest-to-atomic operation Linear offers; `capabilities().native_label_swap_atomic = True`.

**Verified at M5:** Linear caps project `description` at **255 chars** — the long-form body lives in `content` (the adapter reads `content or description`); projects have **no issue-style comments API**, so project-run transcripts/token reports are recorded in the audit log + OpenObserve only (the substance lands on the child issues anyway, per ADR-0006).

**Verified at M2:** (a) Linear **project labels are a separate, workspace-level entity** (`projectLabels` / `projectLabelCreate`) — `ensure_labels` creates the ten managed labels in *both* namespaces, and `ProjectUpdateInput.labelIds` takes project-label ids, not issue-label ids; (b) Linear enforces a **per-query complexity budget** (~10k) — queries stay small and split rather than nesting team+issues+projects in one request.

## 6. Projects as Missions

Projects are normalized into Missions like Issues (`pmo_kind="project"`, `key="PRJ-{slug}"`). Policy (ADR `0006-projects-always-decompose.md`):

- A Project always takes the **high-complexity ONBOARD path**: it is decomposed into child Issues created inside the Project (`MissionDraft.parent_ref` = project id), each labeled `DEVCAKE-CREATED`.
- The Project itself then receives `DEVCAKE-TRACKING` and stays open; the poll loop auto-completes it once all child Issues are `done`/`canceled` (`04-orchestrator.md` §1.3).
- Projects never take the trivial or normal ONBOARD paths.

## 7. Adapter contract tests

A reusable battery every `PMOPort` implementation must pass (run against a sandbox team in CI for Linear; against fakes for the port itself):

| # | Scenario |
|---|---|
| 1 | `list_missions` returns only the configured team's items, excluding terminal ones |
| 2 | Status normalization round-trips for every state type |
| 3 | Priority normalization incl. the unset→`medium` default |
| 4 | `swap_labels` removes+adds in one observable step; no intermediate two-stage-label state visible to a subsequent `get_mission` |
| 5 | `ensure_labels` is idempotent and case-insensitive |
| 6 | Transcript > 50 KB goes up as an attachment, ≤ 50 KB as a comment |
| 7 | `create_mission` applies `DEVCAKE-CREATED`, explicit priority, and team scoping |
| 8 | `get_activity` ordering is chronological and includes attachments with fetchable URLs |
| 9 | Rate-limit (429/RATELIMITED) surfaces as `PMO_TRANSIENT` |
| 10 | Project normalization: statuses, priority, labels; `capabilities()` truthful |
| 11 | `inverseRelations` of type `blocks` parse into `blocked_by`; other relation types ignored |
| 12 | `create_relation` issues the correct mutation and tolerates the duplicate-relation error |

## 8. Webhook readiness (fast-follow, not v0)

`watch()` is the seam: v0's implementation polls and diffs; the fast-follow adds a FastAPI webhook receiver (Linear signs payloads with `Linear-Signature` HMAC-SHA256; events exist for Issues, Comments, Projects, Labels) that yields the same `ChangeEvent`s. Nothing above the port changes. Until then, polling every 30–60 s is well within rate limits.
