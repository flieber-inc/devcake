"""ForgePort — the contract every forge adapter implements (docs/06), plus the
normalized DTOs that cross it and the single definition of DevCake's branch
convention. Adapters normalize vendor payloads (GitHub PRs, GitLab MRs) into
these DTOs; callers never see raw forge JSON."""

from typing import ClassVar, Literal, Optional, Protocol

from pydantic import BaseModel

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


class BranchProtection(BaseModel):
    protected: bool
    requires_reviews: Optional[bool] = None


class ForgeHealth(BaseModel):
    ok: bool
    repository: str = ""
    can_push: bool = False
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
    token_env_default: str              # default token env name (config + SPA)
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

    async def health_probe(self) -> ForgeHealth: ...
    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]: ...
    async def pr_state(self, pr_number: int) -> PullRequest: ...
    async def post_pr_comment(self, pr_number: int, markdown: str) -> None: ...
    async def approve(self, pr_number: int) -> bool: ...
    async def merge(self, pr_number: int) -> None: ...
    async def mergeable(self, pr_number: int) -> Optional[bool]: ...
    async def default_branch_protection(
        self, branch: str = "main") -> Optional[BranchProtection]: ...
    def approval_footer(self, pr_url: str) -> str: ...
