"""ForgePort — Gitea adapter (docs/06; docs/16 M11). Serves both the bundled
internal fallback forge and external/user-supplied Gitea instances.

Every divergence below was LIVE-VERIFIED against 1.24.7-rootless (M11 probe):
- the PR list's `head` filter is IGNORED server-side → filter client-side;
- review approval event is "APPROVED" (not GitHub's "APPROVE"); the poster
  cannot approve their own PR (422); approvals count as *official* only when
  the reviewer has write access OR is on the branch protection's approvals
  whitelist — the internal-forge provisioner whitelists devcake-reviewer;
- merge is POST /pulls/{n}/merge {"Do": "squash"}; 405 is OVERLOADED:
  "Please try again later" (async mergeability check — transient, retry) vs
  "Does not have enough approvals" (definitive) vs already-merged — never
  classify by status code alone (the GitHub 405 lesson, docs/06 §5);
- `mergeable` is a plain boolean (no tri-state field): True/False/absent →
  True/False/None; the merge-first ordering in the sweep compensates;
- tokens are 40 hex chars — a redaction regex would mask every git SHA, so
  token_patterns is DELIBERATELY empty; value registration is the only
  redaction line (docs/14 §5);
- clone auth: the token in the URL userinfo authenticates regardless of the
  username half → a static clone_user works.
"""

import asyncio
import logging
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx

from ...ports.forge import (BranchProtection, ForgeDescriptor, ForgeError,
                            ForgeHealth, PullRequest)

log = logging.getLogger("devcake.forge")

_TRY_AGAIN = "try again later"


class GiteaForge:
    descriptor = ForgeDescriptor(
        id="gitea",
        display_name="Gitea",
        pr_instructions=(
            "Pull request via the Gitea API (no CLI needed; idempotent). "
            "Derive the API root from $DEVCAKE_REPO_URL: insert `/api/v1/repos` "
            "after the host and drop the trailing `.git` "
            "(http://host/owner/repo.git → http://host/api/v1/repos/owner/repo); "
            "call it $API below, and authenticate every call with "
            "`-H \"Authorization: token $GITEA_SERVER_TOKEN\"`. First check for "
            "an existing PR: `curl -s $API/pulls?state=all` and look for one "
            "whose head ref is `{branch}` — if found, push your commits and "
            "stop (the PR updates itself). Else create it: "
            "`curl -s -X POST $API/pulls -H 'Content-Type: application/json' "
            "-d '{{\"head\": \"{branch}\", \"base\": \"{default}\", "
            "\"title\": \"[{key}] {title}\"}}'`."),
        clone_user="devcake",   # userinfo username is ignored (live-verified)
        git_user_name="DevCake",
        git_email="devcake@devcake.invalid",
        pr_noun="pull request",
        cli_token_envs=["GITEA_SERVER_TOKEN"],
        token_env_default="GITEA_TOKEN",
        secret_env_vars=["GITEA_TOKEN", "GITEA_TOKEN_RO",
                         "GITEA_REVIEWER_TOKEN", "GITEA_SERVER_TOKEN"],
        # DELIBERATELY empty: 40-hex tokens collide with git SHAs (docstring)
        token_patterns=[],
        secret_shape_prefixes=[],
    )

    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 api_base: str | None = None):
        origin, url_path = "", repo_url
        if repo_url:
            parts = urlsplit(repo_url)
            if parts.scheme and parts.netloc:
                origin = f"{parts.scheme}://{parts.netloc}"
                url_path = parts.path
        path = url_path.strip("/").removesuffix(".git")
        segs = [s for s in path.split("/") if s]
        if len(segs) < 2:
            raise ValueError(
                f"invalid Gitea repository URL {repo_url!r}: need "
                f"https://<host>/owner/repo")
        self.owner, self.repo = segs[-2], segs[-1]
        self.api = (api_base or origin or "").rstrip("/")
        if not self.api:
            raise ValueError(
                f"invalid Gitea repository URL {repo_url!r}: need a host")
        self.token = token
        self.reviewer_token = reviewer_token or None

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        token = self.reviewer_token if reviewer else self.token
        return {"Authorization": f"token {token}"}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method,
                f"{self.api}/api/v1/repos/{self.owner}/{self.repo}{path}",
                headers=self._headers(reviewer), **kwargs)
            if resp.status_code >= 400:
                raise ForgeError(f"{method} {path} → {resp.status_code}: "
                                 f"{resp.text[:200]}", status=resp.status_code)
            return resp.json() if resp.text else None

    async def health_probe(self) -> ForgeHealth:
        repository = f"{self.owner}/{self.repo}"
        try:
            repo = await self._req("GET", "")
        except ForgeError as e:
            # 401/403/404 indict the credential (bad/read-only token, no
            # access, invisible private repo) — no rate-limit carve-out on
            # self-hosted Gitea
            definitive = e.status in (401, 403, 404)
            return ForgeHealth(
                ok=False, repository=repository, transient=not definitive,
                detail=f"repository access failed (HTTP {e.status}); the "
                       f"token needs write:repository scope and repo access")
        can_push = bool((repo.get("permissions") or {}).get("push"))
        return ForgeHealth(
            ok=can_push, repository=repository, can_push=can_push,
            detail="" if can_push else (
                "token can read the repository but lacks push permission"),
        )

    async def get_pr_by_branch(self, branch: str) -> Optional[PullRequest]:
        """Newest PR (any state) whose head is the given branch. The server
        IGNORES a `head` query param (live-verified) — filter client-side."""
        prs = await self._req("GET", "/pulls?state=all&limit=50")
        matching = [p for p in (prs or [])
                    if ((p.get("head") or {}).get("ref")) == branch]
        if not matching:
            return None
        pr = max(matching, key=lambda p: p.get("number", 0))
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state="closed" if pr["state"] == "closed" else "open",
                           merged=bool(pr.get("merged")))

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from ...security import redact
        await self._req("POST", f"/issues/{pr_number}/comments",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        """Formal approval with the reviewer token; False when none configured.
        NOTE: only counts toward required_approvals when the reviewer is on
        the protection's approvals whitelist or has write access (verified)."""
        if not self.reviewer_token:
            return False
        await self._req("POST", f"/pulls/{pr_number}/reviews", reviewer=True,
                        json={"event": "APPROVED",
                              "body": "Approved by DevCake REVIEW."})
        return True

    async def merge(self, pr_number: int) -> None:
        """Squash-merge. Gitea's 405 is overloaded (docstring): retry only the
        transient "Please try again later" (async mergeability check); on any
        other failure probe already-merged first (ISSUES #6 — redelivery
        after a successful merge must not report auto-merge failure)."""
        for attempt in range(3):
            try:
                await self._req("POST", f"/pulls/{pr_number}/merge",
                                json={"Do": "squash"})
                return
            except ForgeError as e:
                if e.status == 405 and _TRY_AGAIN in str(e).lower() and attempt < 2:
                    await asyncio.sleep(3)
                    continue
                if await self._already_merged(pr_number):
                    return
                raise

    async def _already_merged(self, pr_number: int) -> bool:
        try:
            state = await self.pr_state(pr_number)
            return bool(state.merged)
        except Exception:
            return False

    async def mergeable(self, pr_number: int) -> Optional[bool]:
        """Port tri-state from Gitea's plain boolean (no mergeable_state
        equivalent — capability `mergeable_tristate=False`): the sweep's
        merge-FIRST ordering provides the missing wait dimension."""
        pr = await self._req("GET", f"/pulls/{pr_number}")
        m = pr.get("mergeable")
        if m is True:
            return True
        if m is False:
            return False
        return None

    async def pr_state(self, pr_number: int) -> PullRequest:
        pr = await self._req("GET", f"/pulls/{pr_number}")
        return PullRequest(number=pr["number"], url=pr["html_url"],
                           state="closed" if pr["state"] == "closed" else "open",
                           merged=bool(pr.get("merged")))

    async def default_branch_protection(
            self, branch: str = "main") -> Optional[BranchProtection]:
        """Reads the protection rule list (repo-admin scope; None when
        unreadable per the port contract). requires_reviews maps from
        required_approvals ≥ 1."""
        try:
            rules = await self._req("GET", "/branch_protections")
        except ForgeError:
            return None
        rule = next((r for r in (rules or [])
                     if r.get("branch_name") == branch), None)
        if rule is None:
            return BranchProtection(protected=False, requires_reviews=None)
        return BranchProtection(
            protected=True,
            requires_reviews=bool(rule.get("required_approvals", 0) >= 1))

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        """D14: every REVIEW PR comment ends with the exact copy-pasteable
        way to approve + merge (Gitea: the PR page — no ubiquitous CLI)."""
        return ("\n\n---\nTo approve and merge this PR yourself, open "
                f"{pr_url} and use Review → Approve, then Merge (squash).")
