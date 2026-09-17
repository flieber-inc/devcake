# ADR-0017 — Blocker RO work mounts + optional PMO changeset zip

- **Status:** accepted (2026-07-21); **amended 2026-07-28** (cross-instance
  blocker resolution via `BlockerLocator`); **amended 2026-09-17** (§1 retired
  in favour of the delivery destination — addendum below). Mechanism is an RO **token** +
  prompt contract in ordinary writable clone dirs — not a filesystem mount
  (falls back to the write token when no `token_ro` is configured)
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

> Amended by ADR-0043 §4: beside the work-repo mount, a direct done blocker's whole record is now copied under `upstream/{KEY}/` in the dependent's activity folder. The mount contract above is unchanged.

## Addendum — the delivery destination (2026-09-17)

**Context.** A research mission whose deliverable was a report needed no repository change. Its ONBOARD planned a step that posts the report to the ticket — a step no Dev can perform (INV-4: the app is the only PMO writer, and the Dev→app payload is a closed set) — its EXECUTE opened no pull request, the `executed` transition advanced anyway, REVIEW approved, and the mission parked at `DEVCAKE-MERGE` with nothing to merge and no way out. The only mechanism that carries files to the ticket was §1's toggle: post-merge, deployment-wide, all missions or none. The report reached the ticket only because it was embedded in the attached plan.

**Decision.** Every mission's change set rides a pull request on the mission branch; what a mission carries is where that change set **lands** — its **delivery destination**:

- `repository` (the default, silent): the pull request merges, as before.
- `ticket`: at REVIEW approve the app attaches the pull request's changed files to the ticket (each file its own attachment when the set is small and every file fits the PMO cap; otherwise the archive with its MANIFEST, exactly as the post-merge path builds it), completes the mission through the one completion chokepoint with copy that says **no repository changed**, and closes the pull request without merging (`ForgePort.close_pr`, best-effort after Done). No formal forge approval and no approval footer are posted. EXECUTE and REVIEW mechanics do not change: the pull request is still where the work is reviewed.

The destination is **mission-record data**: a backticked description marker, `` `devcake:delivery:v1 to=ticket` `` with a `Reason:` line, that the **app** writes once — from ONBOARD's structured result on a mission that carries no marker, or from a parent's decomposition child fields — and that only a **person** changes afterwards (last marker wins). Any later differing declaration by a Dev (ONBOARD on a recorded mission, EXECUTE) is a **proposal**: one notice with the exact line to paste and `DEVCAKE-NEEDS-HUMAN`, never a write. REVIEW never proposes; it rejects and says why. A person may also write the line onto a mission parked at `DEVCAKE-MERGE`, and the merge sweep honours it. The park at ONBOARD follows the board's `plan_approval`; on a board whose PMO cannot hold attachments the ticket destination is unavailable. Runs snapshot the destination at dispatch (`Run.delivery_to`). Dispatch enforces the conveyor: an `executed` whose branch carries no pull request is `DEV_BAD_OUTPUT`; a parked mission with no pull request is handed back to a person, never wedged.

**§1 is retired.** `attach_merged_changeset_to_pmo` is removed. The ticket receives a change set exactly when the mission's destination is the ticket, or — as always — after merge for the invisible internal forge (ADR-0010). Pre-v1: an old key is an unknown key (the generic warning, no migration), and the Policies → Delivery card is gone.

**Rejected.** A Dev posting to the ticket (INV-4). A Dev-authored file payload on `run.artifacts` attached at the step card — a second attachment chokepoint, a second completion doctrine, size caps, and it bypasses the forge review surface (diff, reviewer token, PR comments). Embedding the deliverable in `PLAN.md` — the incident. A new label (ADR-0004's fixed set). A per-board or deployment-wide switch — the choice is a fact about one mission, so it lives on that mission's record where a person can read and edit it. Deleting the mission branch at close — a separate port surface; the branch stays as provenance.

**Consequences.** Both destinations demand the same pull request of EXECUTE, so a destination change never invalidates work and costs no rework before approve — which is what lets a Dev's power be bounded to asking. A ticket-destination mission on a code repository still opens a pull request there (pipelines run, owners see it); operators who want reports elsewhere route such missions to a documents repository through the existing `devcake-repo:` card. The app never inspects the change set's content: REVIEW's deliverable-files-only rule and the plan gate are the controls. After Done the pull request is closed, not deleted; a person can reopen and merge it, and nothing detects that afterwards (docs/14 zone C residual). Description appends now go through one path, `feed.append_note` with `feed.marked_note` (ADR-0034: the third tenant made the chokepoint). Docs 02, 03, 04, 05, 06, 08, 10, 11, 14, 16; ADR-0016, ADR-0020, ADR-0034.
