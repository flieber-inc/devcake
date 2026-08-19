"""ForgePort — the contract every forge adapter implements (docs/06), plus the
normalized DTOs that cross it and the single definition of DevCake's branch
convention. Adapters normalize vendor payloads (GitHub PRs, GitLab MRs) into
these DTOs; callers never see raw forge JSON."""

from typing import ClassVar, Literal, Optional, Protocol

from pydantic import BaseModel

from ..domain.run import LEGACY_PMO_REFS

# THE branch-naming convention: one definition, imported by the orchestrator
# and the prompt templates alike (docs/06 §2).
BRANCH_PREFIX = "devcake/"


def mission_branch(instance: str, key: str) -> str:
    """The working branch for a mission: devcake/{INSTANCE}-{key}
    (e.g. instance "linear", DEV-35 → devcake/LINEAR-DEV-35). The uppercased
    instance prefix keeps identifiers collision-free across PMO instances
    (schema v3, docs/16 M9); instance names contain no hyphens, so the
    compound is unambiguous. An empty instance is a provenance bug upstream —
    fail loudly rather than mint an ambiguous devcake/-KEY branch."""
    if not instance:
        raise ValueError(f"mission_branch: empty instance for key {key!r} — "
                         "provenance was not stamped (schema v3)")
    return f"{BRANCH_PREFIX}{instance.upper()}-{key}"


def legacy_branch(key: str) -> str:
    """The pre-v3 (unprefixed) convention — devcake/DEV-35. Kept ONLY so
    branches created before the schema-v3 upgrade stay findable: review
    paths fall back to it for runs without a stored branch, and the merge
    sweep re-probes it when the prefixed lookup finds no PR (docs/10 §3)."""
    return f"{BRANCH_PREFIX}{key}"


def run_branch(run) -> str:
    """The PR branch for a run: the branch STORED at dispatch (authoritative
    since M9 — resolution must never drift mid-mission), else derived; legacy
    (pre-v3) records derive the unprefixed convention their Devs pushed."""
    if getattr(run, "branch", ""):
        return run.branch
    if run.pmo_ref in LEGACY_PMO_REFS:
        return legacy_branch(run.mission_key)
    return mission_branch(run.pmo_ref, run.mission_key)


class ForgeError(Exception):
    """Raised by all forge adapters for HTTP-level failures (docs/06).
    `status` carries the HTTP status code when one exists. Adapters must never
    leak httpx exceptions upward."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class PullRequest(BaseModel):
    """Normalized PR/MR. GitLab's iid maps to number; its 'merged' state
    normalizes to state='closed' + merged=True (matching today's pr_state)."""
    number: int
    url: str
    state: Literal["open", "closed"]
    merged: bool = False
    # the merge commit once merged (deliverable zip ref); None before the
    # merge or when the vendor omits it — callers fall back to the default
    # branch name "main" (DTO fields only; no adapter-private probe)
    merge_commit_sha: Optional[str] = None


class BranchProtection(BaseModel):
    protected: bool
    requires_reviews: Optional[bool] = None


class PRFile(BaseModel):
    """One changed file in a PR (M11 deliverable zip)."""
    path: str
    status: str                    # added | modified | removed | renamed
    additions: int = 0
    deletions: int = 0


class PRFilesResult(BaseModel):
    """Changed-file list for deliverable packaging. ``truncated=True`` means
    the vendor withheld some paths (names unknown) — callers must disclose
    the incomplete list rather than claim completeness."""
    files: list[PRFile]
    truncated: bool = False


class ForgeCapabilities(BaseModel):
    """Behavioral divergence between forges, extracted from the observed
    GitHub/GitLab/Gitea differences (M11, F4). Call sites branch on these
    instead of on forge identity — a fourth forge declares its own row and
    needs no `if forge.id == …` edits anywhere (F1 spirit)."""
    # does mergeable() carry a real tri-state, or only True/False/None-on-
    # absent? Gitea has no mergeable_state equivalent → False (the sweep's
    # merge-FIRST ordering supplies the missing wait dimension)
    mergeable_tristate: bool = True
    # does the server reject a PR author approving their own PR? (GitHub/
    # Gitea yes; GitLab allows by default)
    self_approval_blocked: bool = True
    # scope needed to READ branch protection: "writer" | "maintainer" | "admin"
    branch_protection_read: str = "admin"
    # does the PR-list `head` query param filter server-side? (Gitea ignores
    # it → the adapter filters client-side; capability documents the fact)
    pr_list_head_filter: bool = True


class ForgeHealth(BaseModel):
    ok: bool
    repository: str = ""
    can_push: bool = False
    # the repository GET itself succeeded — distinguishes "readable but not
    # writable" (the EXPECTED healthy state of a reference-only repo, founder
    # decision 2026-07-15) from "no access at all"; ForgeRuntime rewrites
    # ok=True for RO-only repos when this is set
    can_read: bool = False
    # failure not attributable to the credential/permissions (5xx, network,
    # rate limit) — a retry may succeed, so it must never latch the breaker
    transient: bool = False
    detail: str = ""


class ForgeDescriptor(BaseModel):
    """Everything forge-specific that is NOT an API call: the dev-side dialect
    (clone auth, git identity, PR/MR CLI instructions) plus the secret shapes
    this forge's tokens take. Each adapter ships one as a DESCRIPTOR classvar;
    prompts, spec_env, redaction and the admin SPA consume it from the
    registry instead of hardcoding per-forge tables (docs/06)."""
    id: str
    display_name: str
    # PR/MR CLI instructions for the EXECUTE playbook. Template placeholders:
    # {key} {title} {default} {branch}
    pr_instructions: str
    clone_user: str                     # credential-in-URL user for https clones
    git_user_name: str = "DevCake"
    # required, no default: a git identity is forge-specific knowledge, so the
    # port must not bake one in (F1) — every adapter supplies its own
    git_email: str
    pr_noun: str = "pull request"       # user-facing noun ("merge request" on GitLab)
    cli_token_envs: list[str]           # env vars the entrypoint mirrors the token into
    secret_env_vars: list[str]          # → security.redact env-value scrubbing
    token_patterns: list[str]           # → security.redact regex scrubbing
    secret_shape_prefixes: list[str]    # → SPA paste guard


class ForgePort(Protocol):
    """App-side, decision-bearing forge operations. Contract notes (docs/06 §5):

    - `mergeable` is a single-shot, non-blocking tri-state: False = auto-
      resolvable by a branch sync (conflict/behind); True = ready now; None =
      wait (computing, CI running, or unknown — the safe default).
    - `merge` squash-merges and retries transient 409 races in place.
    - `approve` uses the reviewer token; returns False when none configured.
    - `get_pr_by_branch` returns the NEWEST PR (any state) for the branch.
    """

    descriptor: ClassVar[ForgeDescriptor]
    capabilities: ClassVar[ForgeCapabilities]

    async def health_probe(self) -> ForgeHealth: ...
    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]: ...
    async def pr_state(self, pr_number: int) -> PullRequest: ...
    async def post_pr_comment(self, pr_number: int, markdown: str) -> None: ...
    async def approve(self, pr_number: int) -> bool: ...
    async def merge(self, pr_number: int) -> None: ...
    async def mergeable(self, pr_number: int) -> Optional[bool]: ...
    async def default_branch_protection(
        self, branch: str = "main") -> Optional[BranchProtection]: ...
    # deliverable packaging (M11): the merged change set → zip → PMO feed
    async def pr_files(self, pr_number: int) -> PRFilesResult: ...
    async def file_content(self, path: str, ref: str) -> bytes: ...
    def approval_footer(self, pr_url: str) -> str: ...
