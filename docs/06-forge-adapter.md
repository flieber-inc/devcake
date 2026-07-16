# 06 — Forge Adapter: `ForgePort`, GitHub, and GitLab

> **Audience:** implementers.
> **Depends on:** `02-domain-model.md` (AppConfig.repos), `03-mission-lifecycle.md` (branch/PR conventions).

Both GitHub and GitLab adapters ship behind `ForgePort` (`app/devcake/ports/forge.py`). Exactly **one repository on one forge is active at a time** — but the persisted config is already plural (`repos:` list, exactly-one enforced by an AppConfig validator, mirroring `pmos:`), so multi-repo is a declared future seam that needs no schema break. Both credential slots may nevertheless be configured so Devs can read cross-forge dependencies.

Terminology: this doc says "PR" throughout; on GitLab the same operations target Merge Requests.

## 1. Port interface (normative signatures)

```python
class ForgePort(Protocol):
    descriptor: ClassVar[ForgeDescriptor]        # the dev-side dialect (§3a)

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]: ...
        # NEWEST PR (any state) whose head is the branch; None when there is none
    async def pr_state(self, pr_number: int) -> PullRequest: ...
    async def post_pr_comment(self, pr_number: int, markdown: str) -> None: ...
        # body passes through security.redact before it leaves the app
    async def approve(self, pr_number: int) -> bool: ...
        # formal approval with the reviewer token; returns False when none configured (§4)
    async def merge(self, pr_number: int) -> None: ...
        # squash merge; retries the forge's transient merge race (409 head-modified/
        # SHA race) up to 2 times internally — only real failures propagate
    async def mergeable(self, pr_number: int) -> Optional[bool]: ...
        # single-shot, non-blocking tri-state (§5): False = auto-resolvable by
        # a branch sync; True = ready to merge now; None = wait (computing, CI
        # pending, or any unrecognized state — the safe default)
    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]: ...
        # protection state of the given branch (callers pass config.repo.
        # default_branch); None when unreadable
    def approval_footer(self, pr_url: str) -> str: ...
        # the copy-pasteable approve+merge command footer (D14, §4)
```

All adapters raise the same `ForgeError` (with a `status` attribute carrying the HTTP code) — callers never see forge-native exception types (the GitLab adapter in particular must never leak `httpx` exceptions).

Normalized DTOs (pydantic models in `ports/forge.py`):

- `PullRequest` — `{number, url, state: "open"|"closed", merged: bool}`. GitLab's `iid` maps to `number`; MR state `"merged"` normalizes to `state="closed"` + `merged=True`; GitHub *list* payloads carry `merged_at` (not `merged`), so the adapter derives `merged` from it.
- `BranchProtection` — `{protected: bool, requires_reviews: bool|None}` (`None` = couldn't determine).

Earlier drafts of this doc specified `RepoRef`, a `PR` dataclass, `ForgeCapabilities`, `ensure_pr()`, `authenticated_clone_url()`, `get_pr()`, and `capabilities()` — none of that was ever implemented and it is deleted; future port seams live in `16-roadmap.md`. Clone authentication is a dev-side concern: the entrypoint builds a git credential helper (`GIT_ASKPASS`) from the run-spec token, so the token never lands in a URL on disk (`07-dev-runtime.md` §5).

## 2. Branch convention (single definition)

`ports/forge.py` is **the** single definition of DevCake's branch convention, imported by the orchestrator and the prompt templates alike:

```python
BRANCH_PREFIX = "devcake/"

def mission_branch(key: str) -> str:      # DEV-35 → devcake/DEV-35
    return f"{BRANCH_PREFIX}{key}"
```

Restated from `03-mission-lifecycle.md`:

- Branch: `devcake/{mission_key}` — reused across EXECUTE loops; never force-pushed; checked out if it already exists on the remote. Playbooks receive it via the `{branch}` placeholder, fed by `mission_branch()`.
- PR title: `[{mission_key}] {title}`; body links the Mission URL and the plan attachment.
- DevCake **playbooks** never push to the default branch (`config.repo.default_branch`). The only *intended* path to the default branch is a PR merge (human, or app under `auto_merge`). Enforcement is **forge-side branch protection** (operator-owned, `14-security.md` §2 zone C / `13-deployment.md` §8a) — token capability cannot separate push-branch from merge on many forges. The app **warns** when the default branch is unprotected; it does not hard-block dispatch.

## 3. Division of labor: Dev vs. app

- The **Dev** (inside its container, during EXECUTE/trivial-ONBOARD/REVIEW) clones, branches, commits at end, pushes, and opens/updates the PR using injected forge credentials and the forge CLI (`gh`/`glab`, shipped in the Dev images). This is unavoidable — the code lives in the Dev's workspace. The dev side is **descriptor-driven, not string-templated**: everything forge-specific the Dev needs (clone auth user, git identity, CLI token envs, PR/MR CLI instructions) comes from the adapter's `ForgeDescriptor` (§3a) — the orchestrator injects it via `spec_env` (`07-dev-runtime.md` §3) and via the EXECUTE playbook's `pr_instructions` slot; the app carries no per-forge tables outside the adapters.
- The **app** performs the *decision-bearing* forge effects at finalization through `ForgePort` (§1): PR lookup and state, PR comments with the approval footer, formal approval, and merge (`auto_merge`). This keeps the auditable actions in one instrumented place, driven by `result.json` (INV-4 analog for the forge).

The idempotency rule binds both sides: the descriptor's `pr_instructions` template instructs create-or-update by head branch (`gh pr view` before `gh pr create`, `glab mr list` before `glab mr create`); the app side uses `get_pr_by_branch` and keyed comments.

### 3a. `ForgeDescriptor` — the dev-side dialect

Each adapter ships a `DESCRIPTOR` classvar (a `ForgeDescriptor`); prompts, `spec_env`, redaction, and the admin SPA consume it from the registry instead of hardcoding per-forge tables:

| Field | Meaning | Consumed by |
|---|---|---|
| `id`, `display_name` | registry key + UI label | registry, admin SPA |
| `pr_instructions` | PR/MR CLI instructions for the EXECUTE playbook — a template with placeholders `{key}` `{title}` `{default}` `{branch}` (`{branch}` fed by `mission_branch()`, `{default}` by `config.repo.default_branch`) | `prompts.execute_prompt(…, pr_instructions=…)` |
| `clone_user` | credential-in-URL user for https clones (`x-access-token` / `oauth2`) | `DEVCAKE_CLONE_USER` in `spec_env` |
| `git_user_name`, `git_email` | the Dev's git identity | `DEVCAKE_GIT_NAME` / `DEVCAKE_GIT_EMAIL` |
| `cli_token_envs` | env vars the entrypoint mirrors the forge token into for the CLI (`GH_TOKEN` / `GITLAB_TOKEN`) | `DEVCAKE_FORGE_CLI_ENVS` (comma-joined) |
| `token_env_default` | default token env name (`GITHUB_TOKEN` / `GITLAB_TOKEN`) | config seeding + admin SPA |
| `secret_env_vars`, `token_patterns` | the secret shapes this forge's tokens take | `security.redact` (`14-security.md` §5) |
| `secret_shape_prefixes` | token prefixes (`ghp_`, `glpat-`, …) | admin SPA paste guard |

## 3b. The registry and config

`app/devcake/adapters/registry.py` is the single place that knows which forges exist:

- `forges()` → `{id: ForgeDescriptor}` for every registered forge (feeds the SPA registry endpoint and the redaction contributions). Adapter imports are lazy, so importing the registry never drags in the httpx-heavy adapter modules.
- `make_forge(inst)` constructs the adapter for the configured `RepoInstance` (`config.repos[0]`), passing `(url, token, reviewer_token, api_base=inst.api_base)`.

`RepoInstance` (`config.py`, schema v4): identity + URL/forge/branch fields in config; **tokens are GUI-stored** (`token` / `token_ro` / `reviewer_token` read-throughs — ADR-0011). The `forge` field is **registry-validated** — an unknown forge id is rejected at config load.

- `api_base` (default `None`): explicit API endpoint override — this is what unlocks **GitHub Enterprise** (`https://ghe.corp/api/v3`).
- **Self-hosted GitLab needs no `api_base`:** the adapter derives its API origin from the repo URL itself (`https://gitlab.corp.example/grp/repo` → API at `https://gitlab.corp.example/api/v4/…`), identical to the old `https://gitlab.com` default for gitlab.com repos. `api_base` remains the explicit override when the API lives elsewhere.
- `default_branch` (default `"main"`): replaces the previously hardcoded `"main"` everywhere — the EXECUTE sync instructions, `DEVCAKE_DEFAULT_BRANCH`, and the branch-protection check all use it.

## 4. The self-approval problem

GitHub and GitLab forbid approving a PR with the account that opened it. Resolution (confirmed with the founder):

1. **Optional reviewer token** — GUI secret `reviewer_token` (different account, e.g. a `devcake-reviewer` machine user). When present, `approve(pr_number)` files a formal approval review and returns `True`.
2. **Without it** — `approve()` returns `False` (no error): the REVIEW PR comment carries the `APPROVED-BY-DEVCAKE` marker and the Mission's Done status is the signal; no formal approval is filed.
3. **Always, in both cases** — every REVIEW PR comment ends with the copy-pasteable approval command footer with concrete refs (`approval_footer`, `03-mission-lifecycle.md` §5), so one paste in a human terminal approves/merges.

### Token posture (operator)

- **Write token** is required for EXECUTE (push + open PR).
- **Read-only PAT** for non-EXECUTE stages is **recommended**; if unset, every stage receives the write token and health shows dismissable `forge-write-token` (`14` §8).
- **Reviewer token** enables formal PR approval for auto-merge paths.

## 5. `auto_merge` and the merge-before-Done rule

**Merge always precedes Done, in every path** (confirmed decision — `03-mission-lifecycle.md` §4.1). All DevCake-written code (including the trivial-ONBOARD path, which now always passes REVIEW) reaches Done only through REVIEW approval followed by a real merge:

- `auto_merge` **ON**: after approval, the app merges (squash); only a successful merge triggers the Done transition. On GitHub without a reviewer token the merge proceeds without formal approval — merge permission is a repo setting the operator accepts by enabling the toggle; the admin panel warns about exactly this (`11-admin-panel.md` §3).
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

- Auth: fine-grained PAT (or classic token). A fine-grained PAT must explicitly select the configured repository under **Repository access** and grant `Contents: read/write` plus `Pull requests: read/write`; a token that authenticates the user but omits the private repository receives 404/403 from GitHub. A classic token needs `repo`. The connection probe requires the repository API's `permissions.push=true` before scheduling is allowed. Reviewer token additionally needs nothing beyond PR review permission.
- CLI in Dev images: `gh` (authenticated via `GH_TOKEN` — the descriptor's `cli_token_envs`, mirrored from the forge token by the entrypoint).
- API: REST for the app-side operations (PR lookup, comments, reviews, merge with `merge_method: squash`, branch protection); PR creation happens Dev-side via the descriptor's `pr_instructions`. `api_base` overrides `https://api.github.com` for GitHub Enterprise.
- `default_branch_protection` reads the branch's `protected` flag, classic protection detail (may 403/404 without admin scope), and repository rulesets (the modern mechanism).

## 7. GitLab specifics

- Auth: project access token or PAT; scopes `api`, `write_repository`. Approvals use the MR Approvals API (availability varies by tier) and require the reviewer token — without one, `approve()` returns `False` and path 2 of §4 applies.
- CLI in Dev images: `glab` (authenticated via `GITLAB_TOKEN`).
- Merge: `PUT /merge_requests/:iid/merge` with `squash: true`.
- Self-hosted: the API origin derives from the repo URL (§3b); the project path is URL-encoded (`grp/repo` → `grp%2Frepo`). `default_branch_protection`: a 404 on `/protected_branches/{branch}` means unprotected.

## 8. Adapter contract tests

Two layers:

**Domain / port tables** (`app/tests/test_forge.py`) — no network; `_req` is stubbed so mergeable maps, merge retries, and DTO parity stay fast and independent of HTTP:

| # | Scenario |
|---|---|
| 1 | Port conformance: both adapters implement every `ForgePort` method with signatures that match the protocol |
| 2 | `mergeable()` maps every row of the §5 signal table (incl. the GitLab legacy fallback); unknown states → `None` |
| 3 | `merge()` retries a transient 409 twice then succeeds/raises; a 405 raises immediately (no retry) |
| 4 | Error normalization: both adapters raise `ForgeError` with `.status` from `_req` (GitLab never leaks httpx exceptions) |
| 5 | DTO shape parity: `get_pr_by_branch`/`pr_state` normalize GitHub and GitLab payloads to identical `PullRequest` values (GitHub list `merged_at` → `merged`; GitLab `iid` → `number`, MR `"merged"` → `closed` + `merged=True`); no PR → `None` |
| 6 | `BranchProtection` DTO: GitHub `protected` flag; GitLab 404 → `protected=False` |
| 7 | `api_base`: GitHub default vs GHE override; GitLab origin derived from the repo URL, explicit override wins, project path stays URL-encoded |
| 8 | Registry: `forges()` covers exactly `{github, gitlab}` with real descriptors; `make_forge` constructs each (passing `api_base`); an unknown forge is rejected by `RepoInstance` validation |
| 9 | Descriptor completeness: every field non-empty; `pr_instructions` renders against `{key}/{title}/{default}/{branch}` without `KeyError`; `token_patterns` compile |
| 10 | `mission_branch()` single definition: `devcake/` prefix |

**HTTP contract** (`app/tests/test_forge_http.py`) — hermetic `httpx.MockTransport` injected via optional constructor `transport=` (same seam as Linear / Gitea provisioner). Asserts auth header shape and full URL assembly for GitHub, GitLab, and Gitea so empty `_headers()` or a broken `_req` URL fails the suite. Live Gitea battery remains `scripts/contract_tests_forge.py` (vendor drift).

## 9. Adding a forge (checklist)

1. One adapter package under `app/devcake/adapters/{forge}/` implementing every `ForgePort` method plus a `DESCRIPTOR` classvar (§3a) — constructor signature `(repo_url, token, reviewer_token=None, api_base=None)`.
2. One entry in `registry._forge_classes()`.
3. If the dialect's `pr_instructions` needs a CLI, bake it into the Dev images (`07-dev-runtime.md` §8).

Config validation, redaction, the SPA paste guard, the playbook prompts, and `spec_env` all pick the new forge up from the registry — no other code changes.
