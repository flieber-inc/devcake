# 06 — Forge Adapter: `ForgePort`, GitHub, and GitLab

> **Audience:** implementers.
> **Depends on:** `02-domain-model.md` (AppConfig.repo), `03-mission-lifecycle.md` (branch/PR conventions).

Both GitHub and GitLab adapters ship in v0 behind `ForgePort`. Exactly **one repository on one forge is active at a time** (`repo` in AppConfig); both credential slots may nevertheless be configured so Devs can read cross-forge dependencies.

Terminology: this doc says "PR" throughout; on GitLab the same operations target Merge Requests.

## 1. Port interface (normative signatures)

```python
class ForgePort(Protocol):
    async def default_branch(self, repo: RepoRef) -> str: ...
    def authenticated_clone_url(self, repo: RepoRef) -> str: ...
        # used only to build the credential helper; the token never lands in a URL on disk
    async def ensure_pr(self, repo: RepoRef, branch: str, title: str, body: str) -> PR: ...
        # idempotent create-or-get by head branch; updates title/body when it exists
    async def get_pr(self, repo: RepoRef, pr_ref: str) -> PR: ...
    async def post_pr_comment(self, pr: PR, markdown: str) -> None: ...
    async def approve(self, pr: PR, *, use_reviewer_token: bool) -> None: ...
    async def merge(self, pr: PR) -> None: ...
        # auto_merge toggle only; squash by default. Adapters retry their
        # forge's transient merge race (409 head-modified/SHA race) up to 2
        # times internally; only real failures propagate.
    async def mergeable(self, pr: PR) -> bool | None: ...
        # single-shot, non-blocking tri-state (§5): False = auto-resolvable by
        # a branch sync; True = ready to merge now; None = wait (computing, CI
        # pending, or any unrecognized state — the safe default)
    def capabilities(self) -> ForgeCapabilities: ...
        # e.g. self_approval_allowed, merge_strategies
```

All adapters raise the same `ForgeError` (with a `status` attribute carrying the HTTP code) — callers never see forge-native exception types.

`PR` DTO: `{url, number_or_iid, head_branch, state, approved}`.

## 2. Division of labor: Dev vs. app

- The **Dev** (inside its container, during EXECUTE/trivial-ONBOARD/REVIEW) clones, branches, commits at end, pushes, and opens/updates the PR using injected forge credentials and the forge CLI (`gh`/`glab`, shipped in the Dev images). This is unavoidable — the code lives in the Dev's workspace.
- The **app** performs the *decision-bearing* forge effects at finalization: PR comments with the approval footer, formal approval, and merge (`auto_merge`). This keeps the auditable actions in one instrumented place, driven by `result.json` (INV-4 analog for the forge).

The idempotency rule binds both sides: `ensure_pr` semantics for creation (Dev side uses `gh pr create` guarded by `gh pr view`, or the API equivalent), keyed comments on the app side.

## 3. Conventions (restated from `03-mission-lifecycle.md`)

- Branch: `devcake/{mission_key}` — reused across EXECUTE loops; never force-pushed; checked out if it already exists on the remote.
- PR title: `[{mission_key}] {title}`; body links the Mission URL and the plan attachment.
- DevCake never pushes to the default branch. Ever. The only path to the default branch is a PR merge (human, or app under `auto_merge`).

## 4. The self-approval problem

GitHub and GitLab forbid approving a PR with the account that opened it. Resolution (confirmed with the founder):

1. **Optional reviewer token** — `repo.reviewer_token_env` names a second credential (different account, e.g. a `devcake-reviewer` machine user). When present, `approve(use_reviewer_token=True)` files a formal approval review.
2. **Without it** — the REVIEW PR comment carries the `APPROVED-BY-DEVCAKE` marker and the Mission's Done status is the signal; no formal approval is filed.
3. **Always, in both cases** — every REVIEW PR comment ends with the copy-pasteable approval command footer with concrete refs (`03-mission-lifecycle.md` §5), so one paste in a human terminal approves/merges.

## 5. `auto_merge` and the merge-before-Done rule

**Merge always precedes Done, in every path** (confirmed decision — `03-mission-lifecycle.md` §4.1). All DevCake-written code (including the trivial-ONBOARD path, which now always passes REVIEW) reaches Done only through REVIEW approval followed by a real merge:

- `auto_merge` **ON**: after approval, the app merges (squash); only a successful merge triggers the Done transition. On GitHub without a reviewer token the merge proceeds without formal approval — merge permission is a repo setting the operator accepts by enabling the toggle; the admin panel warns about exactly this (`11-admin-panel.md` §2).
- `auto_merge` **OFF**: after approval the Mission carries `DEVCAKE-MERGE` and stays In Progress; the poll-cycle merge sweep (`04-orchestrator.md` §1) marks it Done when a human merges the PR, or Canceled if the PR is closed unmerged.

**Merge-failure classes.** Neither forge's merge status code alone identifies the cause (GitHub 405 covers conflicts, branch protection, AND pending required checks; GitLab 405 covers conflicts, drafts, running/failed pipelines) — so after a failed merge the app reads the port's `mergeable()` and classifies:

- **Auto-resolvable** (`False` — conflict or stale branch behind an up-to-date rule): with `auto_resolve_merge_conflicts` ON, the Mission goes back to EXECUTE for a sync-and-resolve rework, max 2 attempts (`03-mission-lifecycle.md` §4.1).
- **Deferred** (`True`/`None` — ready-but-raced, mergeability computing, CI pending): the Mission lands on `DEVCAKE-MERGE` and the merge sweep retries for `merge_retry_window_minutes` before handing off. On large repos mergeability computation takes a while and CI-gated repos legitimately block merges for many minutes — `None` means "keep watching," never "give up."
- **Transient** (409 head-modified/SHA race): retried inside the adapter's `merge()`, invisible to the orchestrator.
- **Everything else** is `FORGE_PERMANENT`: the Mission lands on `DEVCAKE-MERGE` (never a hollow Done), the PR stays open, a comment explains, and an admin-panel health warning is raised — never silent (`15-errors-and-retries.md`).

Per-adapter `mergeable()` signal mapping:

| Contract | GitHub | GitLab |
|---|---|---|
| auto-resolvable (`False`) | `mergeable_state ∈ {dirty, behind}` or `mergeable: false` | `detailed_merge_status ∈ {conflict, need_rebase}` |
| ready (`True`) | `mergeable: true` and state ∈ {clean, unstable, has_hooks} | `detailed_merge_status = mergeable` |
| wait (`None`) | `mergeable: null` (computing), `blocked` (checks/CI), any unknown state | `checking`, `unchecked`, `ci_must_pass`, `ci_still_running`, any unknown state |

GitLab < 15.6 has no `detailed_merge_status`; the adapter falls back to the legacy `merge_status` field (`cannot_be_merged` → `False`, `can_be_merged` → `True`, anything else → `None`).

## 6. GitHub specifics

- Auth: fine-grained PAT (or classic token). Minimum scopes: `contents: read/write`, `pull_requests: read/write` (fine-grained), or classic `repo`. Reviewer token additionally needs nothing beyond PR review permission.
- CLI in Dev images: `gh` (authenticated via `GH_TOKEN` env).
- API: REST for `ensure_pr`/comments/reviews/merge; `merge_method: squash`.

## 7. GitLab specifics

- Auth: project access token or PAT; scopes `api`, `write_repository`. Approvals use the MR Approvals API (availability varies by tier — `capabilities()` reports honestly; when approvals are unavailable, path 2 of §4 applies).
- CLI in Dev images: `glab` (authenticated via `GITLAB_TOKEN` env).
- Merge: `PUT /merge_requests/:iid/merge` with `squash: true`.

## 8. Adapter contract tests

| # | Scenario |
|---|---|
| 1 | `ensure_pr` twice for the same branch yields one PR, second call updates title/body |
| 2 | `default_branch` correct; clone URL never contains a raw token on disk |
| 3 | `approve` with reviewer token files a formal review; without, raises `CapabilityUnavailable` handled as §4.2 |
| 4 | `merge` respects squash; surfaces branch-protection failure as `FORGE_PERMANENT` |
| 5 | PR comment posting is idempotent under retry (keyed by run_id footer) |
| 6 | Rate-limit and 5xx surface as `FORGE_TRANSIENT` |
| 7 | `mergeable()` maps every row of the §5 signal table (incl. the GitLab legacy fallback); unknown states → `None` |
| 8 | `merge()` retries a 409 twice then succeeds/raises; a 405 raises immediately (no retry); errors are `ForgeError` with `.status` |
