# ADR-0017 — Blocker RO work mounts + optional PMO changeset zip

- **Status:** accepted (2026-07-21)
- **Context:** Two usability gaps. (1) Zero-repo / internal missions always zip the merged change set to the PMO (ADR-0010); configured work repos never did — operators who live in Linear saw no files. (2) Pipeline missions ordered by `blocked_by` had no way to see upstream **internal** work trees: each mission gets its own internal repo, so dependents started empty and could only recover artifacts by downloading Linear attachments (losing git shape). Zip-on-A does not appear in B’s activity folder (per-mission materialization).

## Decision

### 1 — Optional PMO changeset zip for configured repos

`AppConfig.attach_merged_changeset_to_pmo` (default **false**). When true, the existing deliver path zips the merged PR file list onto the PMO feed for **non-internal** work repos after observed merge (same best-effort, non-blocking Done as internal). Internal/zero-repo missions **always** zip, regardless of the flag.

Default off: eng monorepos, dual source of truth vs `main`, attachment size caps, secrets landing in the PMO. UI copy discourages casual enablement. The forge PR remains the canonical eng artifact.

### 2 — Always-on RO mounts of done blockers’ work repos

At dispatch, resolve **direct** `blocked_by` missions with status **`done`**, take each blocker’s latest run `repo_ref` (if any), skip the dependent’s primary repo, cap at 8, snapshot as non-secret `Run.blocker_work`. **Amendment (2026-07-28, ADR-0009 cross-instance):** blockers resolve through the shared `BlockerLocator`, and the run index is keyed by `mission_pmo_id` filtered per blocker by the locator's `accepted_pmo_refs` attribution — NOT by the dispatching instance (`_run_is_ours`). A done blocker resolved to a peer Linear instance therefore mounts that instance's tree (same Zone B class — docs/14); colliding-id vendors (`gitea_issues`) only ever match the local set, so a local `#3` can never mount a peer's unrelated `#3`. At runspec time, append those repos to `extra_repos` with RO tokens (internal: `mission_credentials.token_read`; configured: `token_ro or token`). Prompt section `{blocker_repos}` lists paths under `/workspace/repo/{slug}/` and forbids writes.

Canceled blockers do not mount. A blocker repo with **no read credential at dispatch time** (cleared internal repo, removed instance) is listed as *skipped* in `{blocker_repos}` — the prompt never names a mount the runspec would omit; extant pipelines can be weeks old, so this is the common staleness case. A clear that lands *between* dispatch and runspec still omits silently (clone non-fatal). Does **not** rebind the dependent’s RW work repo (one branch / one PR / sticky routing intact).

### 3 — Related but separate: activity zip extract + setup checklist

- Every activity `.zip` attachment is kept **and** extracted under `{stem}/` (zip-slip hardened) so same-mission Devs can read deliverables without tools. The payload always stays **one valid file tree** — a file and a directory must never share a name (unrepresentable in the snapshot's git tree; crashes the entrypoint's mkdir/write): zip members that conflict with an already-extracted member are dropped, an extraction dir colliding with an existing flat attachment remaps wholesale to `{stem}-2/…`, and a flat attachment named like an existing extraction dir takes the `-2` suffix. The entrypoint additionally survives a colliding payload from an old app by flattening the conflicting file to `conflict-{path}`.
- Overview setup checklist treats a healthy internal forge (or an explicit “I’ll work with the internal forge” dismiss) as satisfying the repository step.

## Alternatives considered

- **Zip as mission-to-mission bus** — A’s zip never enters B’s workspace; rejected.
- **Shared RW internal family repo** — branch/PR/sticky/isolation collapse; rejected.
- **Always-on external zip** — monorepo/size/secrets; rejected for default.
- **Full ancestor graph / unbounded clones** — fan-out; v1 is direct blockers + cap 8.
- **Shared RO service account for all internal work** — unnecessary when per-mission `token_read` already isolates to one repo.

## Consequences

- Dependents on internal pipelines can read upstream trees without Linear archaeology.
- Operators can opt into PMO file visibility for configured repos without changing zero-repo guarantees.
- Docs/14: blocker RO tokens (other mission’s read token) enter the Dev under Zone B trust — documented, same class as reference repos.
