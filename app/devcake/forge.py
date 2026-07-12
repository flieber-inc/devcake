"""ForgePort — GitHub adapter (docs/06). App-side, decision-bearing operations:
PR lookup, review comments, formal approval (optional 2nd token), squash merge.
GitLab lands at M6."""

import logging
import os
from typing import Any, Optional

import httpx

log = logging.getLogger("devcake.forge")

API = "https://api.github.com"


class ForgeError(Exception):
    pass


class GitHubForge:
    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None):
        # https://github.com/{owner}/{repo}
        parts = repo_url.rstrip("/").removesuffix(".git").split("/")
        self.owner, self.repo = parts[-2], parts[-1]
        self.token = token
        self.reviewer_token = reviewer_token or None

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        token = self.reviewer_token if reviewer else self.token
        return {"Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json"}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method, f"{API}/repos/{self.owner}/{self.repo}{path}",
                headers=self._headers(reviewer), **kwargs)
            if resp.status_code >= 400:
                raise ForgeError(f"{method} {path} → {resp.status_code}: "
                                 f"{resp.text[:200]}")
            return resp.json() if resp.text else None

    async def get_pr_by_branch(self, branch: str) -> Optional[dict]:
        """Newest PR (any state) whose head is the given branch."""
        prs = await self._req(
            "GET", f"/pulls?head={self.owner}:{branch}&state=all&sort=created&direction=desc")
        return prs[0] if prs else None

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        from .security import redact
        await self._req("POST", f"/issues/{pr_number}/comments",
                        json={"body": redact(markdown)})

    async def approve(self, pr_number: int) -> bool:
        """Formal approval with the reviewer token; False when none configured."""
        if not self.reviewer_token:
            return False
        await self._req("POST", f"/pulls/{pr_number}/reviews", reviewer=True,
                        json={"event": "APPROVE",
                              "body": "Approved by DevCake REVIEW."})
        return True

    async def merge(self, pr_number: int) -> None:
        await self._req("PUT", f"/pulls/{pr_number}/merge",
                        json={"merge_method": "squash"})

    async def pr_state(self, pr_number: int) -> dict:
        pr = await self._req("GET", f"/pulls/{pr_number}")
        return {"state": pr["state"], "merged": bool(pr.get("merged")),
                "url": pr["html_url"], "number": pr["number"]}

    async def default_branch_protection(self, branch: str = "main") -> Optional[dict]:
        """{"protected": bool, "requires_reviews": bool|None} for the default
        branch, or None when unreadable. Branch protection is the ONLY effective
        control against a Dev merging with its own token (docs/14, ADR-0007
        addendum) — push-branch and merge both need contents:write."""
        try:
            b = await self._req("GET", f"/branches/{branch}")
        except ForgeError:
            return None
        protected = bool(b.get("protected"))
        requires_reviews = None
        try:  # classic protection detail (may 403/404 without admin scope)
            prot = await self._req("GET", f"/branches/{branch}/protection")
            requires_reviews = bool(prot.get("required_pull_request_reviews"))
        except ForgeError:
            pass
        try:  # repository rulesets (the modern mechanism)
            rules = await self._req("GET", f"/rules/branches/{branch}")
            if isinstance(rules, list) and rules:
                protected = True
                if requires_reviews is None:
                    requires_reviews = any(r.get("type") == "pull_request"
                                           for r in rules)
        except ForgeError:
            pass
        return {"protected": protected, "requires_reviews": requires_reviews}

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        """D14 (confirmed decision): every REVIEW PR comment ends with the exact
        copy-pasteable command to approve + merge."""
        return ("\n\n---\nTo approve and merge this PR yourself:\n"
                f"```\ngh pr review --approve {pr_url} && gh pr merge --squash {pr_url}\n```")
