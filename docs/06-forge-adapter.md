# 06 — Forge Adapter: `ForgePort`, GitHub, GitLab, and Gitea

> **Audience:** implementers.
> **Depends on:** `02-domain-model.md` (AppConfig.repos), `03-mission-lifecycle.md` (branch/PR conventions).

GitHub, GitLab, and Gitea adapters ship behind `ForgePort` (`app/devcake/ports/forge.py`). Config holds **0..N** repos (`repos:` list, mirroring `pmos:` — `10-persistence.md`); each mission resolves to **0 or 1** work repo (plus optional read-only passengers: reference clones and memory notebooks — ADR-0035). Two further forge-backed connection classes exist beside repo cards: **memory notebooks** are ordinary repo cards bound via `memory_repos`, while **skill sources** (`AppConfig.skill_sources` — ADR-0016 addendum 2) are their OWN connection type (forge/url/branch/subdir + read tokens), never a `RepoInstance` and never selectable by a PMO. Credential slots (write / read-only / reviewer) may all be configured so non-EXECUTE stages and cross-repo consultation stay least-privilege where the operator supplies RO tokens.

Terminology: this doc says "PR" throughout; on GitLab the same operations target Merge Requests.

## 1. Port interface (normative signatures)

```python
class ForgePort(Protocol):
    descriptor: ClassVar[ForgeDescriptor]        # the dev-side dialect (§3a)
    capabilities: ClassVar[ForgeCapabilities]    # behavioral divergence (§1a)

    async def health_probe(self) -> ForgeHealth: ...
    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]: ...
        # NEWEST PR (any state) whose head is the branch; None when there is none
    async def pr_state(self, pr_number: int) -> PullRequest: ...
    async def post_pr_comment(self, pr_number: int, markdown: str) -> None: ...
        # body passes through security.redact before it leaves the app
    async def approve(self, pr_number: int) -> bool: ...
        # formal approval with the reviewer token; returns False when none configured (§4)
    async def merge(self, pr_number: int) -> None: ...
        # squash merge; retries the forge's transient merge race (GitHub/GitLab
        # 409 head-modified; Gitea 405 "try again later") up to 2 times —
        # only real failures propagate
    async def mergeable(self, pr_number: int) -> Optional[bool]: ...
        # single-shot, non-blocking tri-state (§5): False = auto-resolvable by
        # a branch sync; True = ready to merge now; None = wait (computing, CI
        # pending, or any unrecognized state — the safe default)
    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]: ...
        # protection state of the given branch (callers pass the resolved
        # repo's default_branch); None when unreadable
    async def pr_files(self, pr_number: int) -> PRFilesResult: ...
        # changed-file list for deliverable packaging (M11); truncated=True
        # when the vendor withheld paths (names unknown)
    async def file_content(self, path: str, ref: str) -> bytes: ...
        # blob at path@ref for deliverable zip contents
    def approval_footer(self, pr_url: str) -> str: ...
        # the copy-pasteable approve+merge command footer (D14, §4)
```

All adapters raise the same `ForgeError` — callers never see forge-native or `httpx` exception types. `status` carries the HTTP code when one exists; **network / transport failures use `status=None`** (health probes treat that as transient). Success on the wire is **HTTP 2xx only** — **3xx** raise `ForgeError` with that status (no silent redirect success); non-JSON **2xx** bodies also raise `ForgeError` (never raw `json.JSONDecodeError`). The wire call is ONE chokepoint: `adapters/http.forge_request` (ADR-0034; pinned by `test_forge_error_contract.py`). Adapter `_req` methods must call it rather than re-implementing the map.

Normalized DTOs (pydantic models in `ports/forge.py`):

- `PullRequest` — `{number, url, state: "open"|"closed", merged: bool, merge_commit_sha: str|None}`. GitLab's `iid` maps to `number`; MR state `"merged"` normalizes to `state="closed"` + `merged=True`; GitHub *list* payloads carry `merged_at` (not `merged`), so the adapter derives `merged` from it. `merge_commit_sha` is populated on `pr_state` reads (GitLab squash merges map `squash_commit_sha` onto it) — it pins the deliverable zip to the actual merge commit instead of the moving default branch.
- `BranchProtection` — `{protected: bool, requires_reviews: bool|None}` (`None` = couldn't determine).
- `PRFile` — `{path, status, additions, deletions}` for deliverable packaging.
- `PRFilesResult` — `{files: list[PRFile], truncated: bool}`. `truncated=True` means the vendor withheld some changed paths (names unknown). GitLab reads `/merge_requests/{iid}/changes` `overflow` and retries once with `access_raw_diffs=true`; residual overflow stays `truncated=True`. GitHub paginates to completion (`truncated=False`). Gitea passes through `paginate_rest`'s ceiling flag. Delivery discloses residual truncation in `MANIFEST.txt` and the feed note without inventing dropped filenames.
- `ForgeHealth` — `{ok, repository, can_push, can_read, transient, detail}` from `health_probe`.
- `ForgeCapabilities` — behavioral divergence between forges (§1a).

Earlier drafts of this doc specified `RepoRef`, a `PR` dataclass, `ensure_pr()`, `authenticated_clone_url()`, `get_pr()`, and a free function `capabilities()` — none of that was the shipped seam. **`ForgeCapabilities` as a ClassVar on each adapter is live** (M11). Clone authentication is a dev-side concern: the entrypoint builds a git credential helper (`GIT_ASKPASS`) from the run-spec token, so the token never lands in a URL on disk (`07-dev-runtime.md` §5).

### 1a. `ForgeCapabilities`

Each adapter declares a `capabilities` ClassVar so call sites branch on behavior, not forge identity:

| Field | Meaning | GitHub | GitLab | Gitea |
|---|---|---|---|---|
| `mergeable_tristate` | real tri-state vs True/False/None-on-absent | True | True | False |
| `self_approval_blocked` | server rejects PR author approving own PR | True | **False** (allows by default) | True |
| `branch_protection_read` | scope needed to READ protection | `"admin"` | `"maintainer"` | `"admin"` |
| `pr_list_head_filter` | server-side `head` filter on PR list | True | True | False (adapter filters client-side) |

## 2. Branch convention (single definition)

`ports/forge.py` is **the** single definition of DevCake's branch convention, imported by the orchestrator and the prompt templates alike:

```python
BRANCH_PREFIX = "devcake/"

def mission_branch(instance: str, key: str) -> str:
    # e.g. instance "linear", DEV-35 → devcake/LINEAR-DEV-35
    return f"{BRANCH_PREFIX}{instance.upper()}-{key}"

def legacy_branch(key: str) -> str:
    # pre-v3 unprefixed form — kept only so old branches stay findable
    return f"{BRANCH_PREFIX}{key}"
```

Restated from `03-mission-lifecycle.md`:

- Branch: `devcake/{INSTANCE}-{mission_key}` — reused across EXECUTE loops; never force-pushed; checked out if it already exists on the remote. Playbooks receive it via the `{branch}` placeholder, fed by `mission_branch(instance, key)`. Pre-v3 records without a stored `branch` fall back via `run_branch()` / `legacy_branch()`.
- PR title: `[{mission_key}] {title}`; body links the Mission URL and the plan attachment.
- DevCake **playbooks** never push to the default branch (the resolved repo's `default_branch`). The only *intended* path to the default branch is a PR merge (human, or app under `auto_merge`). Enforcement is **forge-side branch protection** (operator-owned, `14-security.md` §2 zone C / `13-deployment.md` §8a) — token capability cannot separate push-branch from merge on many forges. The app **warns** when the default branch is unprotected (via `/health` `forge_protection`); it does not hard-block dispatch.

## 3. Division of labor: Dev vs. app

- The **Dev** (inside its container, during EXECUTE/REVIEW) clones, branches, commits at end, pushes, and opens/updates the PR using injected forge credentials and the forge dialect from `ForgeDescriptor` — `gh`/`glab` where those dialects use a CLI (shipped in the Dev images); Gitea uses descriptor `pr_instructions` (curl against `/api/v1` — no `tea` CLI in the images). This is unavoidable — the code lives in the Dev's workspace. The dev side is **descriptor-driven, not string-templated**: everything forge-specific the Dev needs (clone auth user, git identity, CLI token envs, PR/MR instructions) comes from the adapter's `ForgeDescriptor` (§3a) — the orchestrator injects it via `spec_env` (`07-dev-runtime.md` §3) and via the EXECUTE playbook's `pr_instructions` slot; the app carries no per-forge tables outside the adapters.
- The **app** performs the *decision-bearing* forge effects at finalization through `ForgePort` (§1): PR lookup and state, PR comments with the approval footer, formal approval, and merge (`auto_merge`). This keeps the auditable actions in one instrumented place, driven by `result.json` (INV-4 analog for the forge).

The idempotency rule binds both sides: the descriptor's `pr_instructions` template instructs create-or-update by head branch (`gh pr view` before `gh pr create`, `glab mr list` before `glab mr create`, Gitea list-then-POST curl in §7a); the app side uses `get_pr_by_branch` and keyed comments.

### 3a. `ForgeDescriptor` — the dev-side dialect

Each adapter ships a `descriptor` classvar (a `ForgeDescriptor`); prompts, `spec_env`, redaction, and the admin SPA consume it from the registry instead of hardcoding per-forge tables:

| Field | Meaning | Consumed by |
|---|---|---|
| `id`, `display_name` | registry key + UI label | registry, admin SPA |
| `pr_instructions` | PR/MR CLI instructions for the EXECUTE playbook — a template with placeholders `{key}` `{title}` `{default}` `{branch}` (`{branch}` fed by `mission_branch()`, `{default}` by the resolved repo's `default_branch`) | `prompts.execute_prompt(…, pr_instructions=…)` |
| `clone_user` | credential-in-URL user for https clones (`x-access-token` / `oauth2`) | `DEVCAKE_CLONE_USER` in `spec_env`; also `RepoCache`'s injected `clone_user_of` resolver — mirror fetches of **skill sources** read the descriptor directly because a source has no live adapter (ADR-0016 addendum 2) |
| `git_user_name`, `git_email` | the Dev's git identity (`git_email` is required on the port — every adapter supplies its own) | `DEVCAKE_GIT_NAME` / `DEVCAKE_GIT_EMAIL` |
| `pr_noun` | user-facing noun (`"pull request"` / `"merge request"` on GitLab) | SPA + playbook copy |
| `cli_token_envs` | env vars the entrypoint mirrors the forge token into for the CLI (`GH_TOKEN` / `GITLAB_TOKEN` / `GITEA_SERVER_TOKEN`) | `DEVCAKE_FORGE_CLI_ENVS` (comma-joined) |
| `secret_env_vars`, `token_patterns` | the secret shapes this forge's tokens take | `security.redact` (`14-security.md` §7) |
| `secret_shape_prefixes` | token prefixes (`ghp_`, `glpat-`, …) | admin SPA paste guard |

## 3b. The registry and config

`app/devcake/adapters/registry.py` is the **sole construction site** for forge adapters and the internal provisioner (F1 tripwire in `test_agnosticism.py`):

| Factory / export | Builds | Also |
|---|---|---|
| `DEFAULT_FORGE` | Seed default forge id for a first-boot `RepoInstance` (`"github"`) | Config derives its default lazily from this — the only forge-name default allowed outside a descriptor |
| `forges()` | `{id: ForgeDescriptor}` for every registered forge | SPA registry endpoint + `security.redact` pattern/env contributions |
| `make_forge(inst)` | Ordinary `ForgePort` for one configured `RepoInstance` | Registers `token` / `token_ro` / `reviewer_token` values for redaction; reference-only repos fall back to the read token. `ForgeRuntime.rebuild(config.repos, make_forge)` builds the full set |
| `make_internal_forge()` | Bundled-Gitea `InternalForgePort` provisioner | Admin/provision only — day-to-day PR ops stay on the `ForgePort` adapter from `mission_repo_binding` |
| `make_gitea_adapter(url, token, reviewer_token=…)` | Explicit-token `GiteaForge` for an internal mission repo | Registers the explicit token values for redaction (Gitea `token_patterns` is deliberately empty — 40-hex tokens collide with git SHAs; value registration is the redaction line, ADR-0010 / docs/14) |

Adapter class imports are lazy inside these factories, so importing the registry never drags in the httpx-heavy adapter modules. Composition callers (`api/services.build_services`, `domain/forge_runtime.rebuild`) receive factories — they never construct vendor classes directly.

SPA cold-start forge ids ride the same pinned mirror as PMO metadata (`admin/spa/src/lib/registry_fallback.json` ↔ `GET /connections/registry`; pin: `test_spa_registry_fallback.py`, ADR-0034).

`RepoInstance` (`config.py`, schema v4): identity + URL/forge/branch fields in config; **tokens are GUI-stored** (`token` / `token_ro` / `reviewer_token` read-throughs — ADR-0011). The `forge` field is **registry-validated** — an unknown forge id is rejected at config load.

- `api_base` (default `None`): explicit API endpoint override — this is what unlocks **GitHub Enterprise** (`https://ghe.corp/api/v3`).
- **Self-hosted GitLab needs no `api_base`:** the adapter derives its API origin from the repo URL itself (`https://gitlab.corp.example/grp/repo` → API at `https://gitlab.corp.example/api/v4/…`), identical to the old `https://gitlab.com` default for gitlab.com repos. `api_base` remains the explicit override when the API lives elsewhere.
- `default_branch` (default `"main"`): replaces the previously hardcoded `"main"` everywhere — the EXECUTE sync instructions, `DEVCAKE_DEFAULT_BRANCH`, and the branch-protection check all use `config.repos[i].default_branch` for the resolved repo.

## 4. The self-approval problem

GitHub and Gitea forbid approving a PR with the account that opened it (`self_approval_blocked=True`). GitLab allows self-approval by default (`self_approval_blocked=False`). Resolution (confirmed with the founder):

1. **Reviewer token (recommended for formal forge approval under branch protection)** — GUI secret `reviewer_token` (different account, e.g. a `devcake-reviewer` machine user). When present, `approve(pr_number)` files a formal approval review and returns `True`. App-only — never injected into a Dev. Not the same as staffing a different Dev Type for the REVIEW stage.
2. **Without it** — `approve()` returns `False` (no error): the REVIEW PR comment carries the `APPROVED-BY-DEVCAKE` marker and the Mission's Done status is the signal; no formal approval is filed.
3. **Same write token pasted as reviewer on a `self_approval_blocked` forge** — `approve()` returns `False` without a wire call (the paste is not a distinct reviewer). On GitLab (`self_approval_blocked=False`) the same token still posts the approve call.
4. **Always, in every case** — every REVIEW PR comment ends with the copy-pasteable approval command footer with concrete refs (`approval_footer`, `03-mission-lifecycle.md` §5), so one paste in a human terminal approves/merges.

### Token posture (operator)

| Secret | Who receives it | Role |
|---|---|---|
| **Write / access** (`token`) | EXECUTE Dev (always); non-EXECUTE only if `token_ro` is unset; **app** always has it for forge side effects | Push feature branch, open/update PR; app **squash-merge** when `auto_merge` is on |
| **Read-only** (`token_ro`) | Non-EXECUTE stages when set (recommended) | Clone/read only — health warns `forge-write-token:{repo}` if missing (`14` §8) |
| **Reviewer** (`reviewer_token`, recommended for formal approval) | **App only** — never injected into a Dev container | Formal PR/MR approval after REVIEW Dev returns approve; enables protected-branch auto-merge without self-approval |

Do not conflate REVIEW Dev judgment with forge approval: the Dev returns
`result.json`; the app calls `approve()` with the reviewer token (or skips
formal approval and posts `APPROVED-BY-DEVCAKE` when no reviewer token is set).
Merge — when enabled — always uses the **write** token, never the reviewer token
(`14` §2 zone C).

## 5. Per-repo `auto_merge` and the merge-before-Done rule

**Merge always precedes Done, in every path** (confirmed decision — `03-mission-lifecycle.md` §4.1). All DevCake-written code reaches Done only through REVIEW approval followed by a real merge. Merge doctrine is **per repository** (ADR-0020): each `RepoInstance` carries `auto_merge` / `auto_resolve_merge_conflicts` / `merge_retry_window_minutes`. Internal (zero-repo) synthesized instances always auto-merge.

- Mission's repo `auto_merge` **ON**: after approval, the **app** merges (squash); only a successful merge triggers the Done transition. On GitHub without a reviewer token the merge proceeds without formal approval — merge permission is a repo setting the operator accepts by enabling the toggle; the admin panel warns about exactly this (`11-admin-panel.md` §2b).
- Mission's repo `auto_merge` **OFF**: after approval the Mission carries `DEVCAKE-MERGE` and stays In Progress; the poll-cycle merge sweep (`04-orchestrator.md` §1) marks it Done when the PR is **observed merged** (normally a human), or Canceled if the PR is closed unmerged. **Off gates the app only** — it does not strip merge rights from the Dev's write token. Devs still receive forge credentials and CLI (`gh`/`glab`/API) for push + open PR; preventing a Dev from merging is **forge branch protection** (`14-security.md` §2 zone C, `13-deployment.md` §8a). A mid-pipeline merge is an out-of-pipeline **detection** tripwire, not a block.

**Merge-failure classes.** Neither forge's merge status code alone identifies the cause (GitHub 405 covers conflicts, branch protection, AND pending required checks; GitLab 405 covers conflicts, drafts, running/failed pipelines; Gitea 405 is similarly overloaded — see §7a) — so after a failed merge the app reads the port's `mergeable()` and classifies:

- **Auto-resolvable** (`False` — conflict or stale branch behind an up-to-date rule): with that repo's `auto_resolve_merge_conflicts` ON, the Mission goes back to EXECUTE for a sync-and-resolve rework, max 2 attempts (`03-mission-lifecycle.md` §4.1). A `False` is trusted as a real conflict **only on a tri-state forge** (`mergeable_tristate=True` — GitHub/GitLab). On a boolean-only forge (Gitea) a `False` can be "not computed yet", so a failed merge **hands off to a human, never routes to EXECUTE** — the same capability check in BOTH the REVIEW-finalize path and the merge sweep (AUD-010 aligned the two; the sweep still tries the merge first and only reaches this after it fails, retrying until the window expires then handing off).
- **Deferred** (`True`/`None` — ready-but-raced, mergeability computing, CI pending): the Mission lands on `DEVCAKE-MERGE` and the merge sweep retries for that repo's `merge_retry_window_minutes` before handing off. On large repos mergeability computation takes a while and CI-gated repos legitimately block merges for many minutes — `None` means "keep watching," never "give up."
- **Transient** (GitHub 409 head-modified/SHA race; Gitea 405 with *"Please try again later"* during async mergeability): short in-adapter retries, invisible to the orchestrator. Other 405s are **not** retried as transient — classify via `mergeable()` / already-merged probes.
- **Everything else** is `FORGE_PERMANENT`: the Mission lands on `DEVCAKE-MERGE` (never a hollow Done), the PR stays open, a comment explains, and an admin-panel health warning is raised — never silent (`15-errors-and-retries.md`).

Per-adapter `mergeable()` signal mapping:

| Contract | GitHub | GitLab |
|---|---|---|
| auto-resolvable (`False`) | `mergeable_state ∈ {dirty, behind}` or `mergeable: false` | `detailed_merge_status ∈ {conflict, need_rebase}` |
| ready (`True`) | `mergeable: true` and state ∈ {clean, unstable, has_hooks} | `detailed_merge_status = mergeable` |
| wait (`None`) | `mergeable: null` (computing), `blocked` (checks/CI), any unknown state | `checking`, `unchecked`, `ci_must_pass`, `ci_still_running`, any unknown state |

GitLab < 15.6 has no `detailed_merge_status`; the adapter falls back to the legacy `merge_status` field (`cannot_be_merged` → `False`, `can_be_merged` → `True`, anything else → `None`). Gitea has no mergeable-state equivalent (`mergeable_tristate=False`); the merge sweep's merge-FIRST ordering supplies the missing wait dimension.

## 6. GitHub specifics

- Auth: fine-grained PAT (or classic token). A fine-grained PAT must explicitly select the configured repository under **Repository access** and grant `Contents: read/write` plus `Pull requests: read/write`; a token that authenticates the user but omits the private repository receives 404/403 from GitHub. A classic token needs `repo`. The connection probe requires the repository API's `permissions.push=true` before scheduling is allowed. Reviewer token additionally needs nothing beyond PR review permission.
- CLI in Dev images: `gh` (authenticated via `GH_TOKEN` — the descriptor's `cli_token_envs`, mirrored from the forge token by the entrypoint).
- API: REST for the app-side operations (PR lookup, comments, reviews, merge with `merge_method: squash`, branch protection); PR creation happens Dev-side via the descriptor's `pr_instructions`. `api_base` overrides `https://api.github.com` for GitHub Enterprise.
- `default_branch_protection` reads the branch's `protected` flag, classic protection detail (may 403/404 without admin scope), and repository rulesets (the modern mechanism).

## 7. GitLab specifics

- Auth: project access token or PAT; scopes `api`, `write_repository`. Approvals use the MR Approvals API (availability varies by tier) and require the reviewer token — without one, `approve()` returns `False` and path 2 of §4 applies. Note: GitLab's `self_approval_blocked=False` — the server allows a PR author to approve by default (unlike GitHub/Gitea).
- CLI in Dev images: `glab` (authenticated via `GITLAB_TOKEN`).
- Merge: `PUT /merge_requests/:iid/merge` with `squash: true`.
- Self-hosted: the API origin derives from the repo URL (§3b); the project path is URL-encoded (`grp/repo` → `grp%2Frepo`). `default_branch_protection`: a 404 on `/protected_branches/{branch}` means unprotected.
- **`pr_files` truncation:** `GET /merge_requests/{iid}/changes` may set `overflow: true` when size limits withhold paths (names are not returned). The adapter retries once with `access_raw_diffs=true`; if overflow remains, `PRFilesResult.truncated=True` and delivery discloses that additional paths are unknown (MANIFEST + feed note), pointing at the MR as canonical.

## 7a. Gitea specifics

- Ships as both an external forge (`RepoInstance(forge="gitea", …)`) and the **bundled internal fallback** for zero-repo missions (`make_internal_forge()`, ADR-0010). Provisioning surface is **`InternalForgePort`** (`ports/internal_forge.py` — mission machine users, skill-store, activity repos) plus the provisioner method **`create_operator_repo`** for operator/"gitea (internal)" repo cards incl. memory notebooks (admin `POST …/internal-repos/create` → `internal_repos_service.create_internal_repo`; refuses `activity-*` names, never swept by Clear — not a Protocol method today). Isolation honesty is **docs/14 §2 Zone B** + ADR-0010 (tokens are user-scoped, not repo-scoped). Day-to-day PR ops use the ordinary `ForgePort` from `mission_repo_binding` → `make_gitea_adapter` on **org service tokens** (PR comment/merge); the per-mission write/read pair is Dev/runspec only.
- Auth: Gitea personal/access tokens; machine users + scoped token pairs for per-mission isolation on the internal forge (ADR-0010; container isolation posture `14` §6 — Gitea admin password never enters the Dev env).
- **Machine-user naming (measured, Gitea 1.27.1):** usernames are capped at 40 chars and reject **consecutive hyphens** — `_svc_user()` therefore rstrips hyphens off the truncated stem before appending the full-name hash. Provisioning also discriminates Gitea's overloaded **422**: `"already exists"` is tolerated (idempotent re-provision), anything else — notably `"invalid username"` — fails loud. Both were one bug: the ADR-0030 board's repo names truncated exactly onto a hyphen, the invalid user was silently never created, and every zero-repo board mission then gated on the collaborator PUT with `user does not exist` (founder report 2026-08-05).
- Capabilities: `mergeable_tristate=False`, `self_approval_blocked=True` (enforced client-side like GitHub — a pasted write token returns `False` from `approve()` without a wire call, §4 item 3), `pr_list_head_filter=False` (client-side head filter).
- **Merge 405:** Gitea's 405 is overloaded — `"Please try again later"` (async mergeability) is retried briefly inside `merge()`; `"Does not have enough approvals"` and already-merged paths are definitive (probe `merged` before reporting failure so redelivery is safe).
- Contract battery: `scripts/contract_tests_forge.py` default / `DEVCAKE_CONTRACT_FORGE=gitea` lane (wired into `ci_suite.sh` / GHA when the stack+Gitea are up).

## 8. Adapter contract tests

Two layers:

**Domain / port tables** (`app/tests/test_forge.py`) — no network; `_req` is stubbed so mergeable maps, merge retries, and DTO parity stay fast and independent of HTTP:

| # | Scenario |
|---|---|
| 1 | Port conformance: registered adapters implement every `ForgePort` method with signatures that match the protocol |
| 2 | `mergeable()` maps every row of the §5 signal table (incl. the GitLab legacy fallback) **and** Gitea's boolean-only True/False/absent → True/False/None; unknown states → `None` |
| 3 | `merge()` retries a transient GitHub 409 twice then succeeds/raises; non-retryable 405s raise; Gitea's "try again later" 405 retries inside the adapter while approvals/already-merged 405s probe then raise/absorb (§7a) |
| 4 | Error normalization: every forge `_req` routes through `adapters/http.forge_request` → `ForgeError` only (`status=None` for network; success is **2xx**; **3xx**/non-2xx preserve status; non-JSON 2xx map through `ForgeError`). Direct chokepoint + all three adapter classes pinned in `test_forge_error_contract.py` |
| 5 | DTO shape parity: `get_pr_by_branch`/`pr_state` normalize GitHub/GitLab/Gitea payloads to identical `PullRequest` values; Gitea filters head client-side (no `head=` query); no PR → `None` |
| 6 | `BranchProtection` DTO: GitHub `protected` flag; GitLab 404 → `protected=False` |
| 7 | `api_base`: GitHub default vs GHE override; GitLab origin derived from the repo URL, explicit override wins, project path stays URL-encoded |
| 8 | Registry: `forges()` covers `{github, gitlab, gitea}` with real descriptors; `make_forge` constructs each (passing `api_base`); `make_internal_forge` / `make_gitea_adapter` are the only other construction paths; production AST scan forbids direct `*Forge(` outside the registry; an unknown forge is rejected by `RepoInstance` validation |
| 9 | Descriptor completeness: every field non-empty; `pr_instructions` renders against `{key}/{title}/{default}/{branch}` without `KeyError`; `token_patterns` compile; Gitea `token_patterns == []` is pinned (intentional — SHA collision) |
| 10 | `mission_branch(instance, key)` single definition: `devcake/{INSTANCE}-{key}` prefix |
| 11 | `ForgeCapabilities` ClassVar present and matches the §1a matrix exactly (GitHub / GitLab / Gitea) |
| 12 | Redaction at construction: `make_forge` registers token / token_ro / reviewer; `make_gitea_adapter` registers explicit tokens (`test_security.py`) |
| 13 | `approve()`: False without reviewer; same write/reviewer token no-ops on `self_approval_blocked` forges and still posts on GitLab; `post_pr_comment` redacts known secret shapes on the wire (`test_forge_http.py`) |

**HTTP contract** (`app/tests/test_forge_http.py`) — hermetic `httpx.MockTransport` injected via optional constructor `transport=` (same seam as Linear / Gitea provisioner). Asserts auth header shape, full URL assembly, PR-comment redaction, self-approval same-token honesty for GitHub/GitLab, and Gitea's `APPROVED` review event, so empty `_headers()` or a broken `_req` URL fails the suite. The live forge battery (`scripts/contract_tests_forge.py`) is **gitea-only** (default / `DEVCAKE_CONTRACT_FORGE=gitea`; non-gitea values hard-exit). GitHub/GitLab live forge proof is the M12 acceptance ritual / `scripts/acceptance.py` (tester-side tokens), not this script.

## 9. Adding a forge (checklist)

1. One adapter package under `app/devcake/adapters/{forge}/` implementing every `ForgePort` method plus a `descriptor` and `capabilities` ClassVar (§1a/§3a) — constructor signature `(repo_url, token, reviewer_token=None, api_base=None)`.
2. One entry in `registry._forge_classes()`.
3. If the dialect's `pr_instructions` needs a CLI, bake it into the Dev images (`07-dev-runtime.md` §8).

Config validation, redaction, the SPA paste guard, the playbook prompts, and `spec_env` all pick the new forge up from the registry — no other code changes.
