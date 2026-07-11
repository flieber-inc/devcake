"""ForgePort — GitLab adapter (docs/06 §7). Same shape as GitHubForge; MRs
instead of PRs. Live verification requires a GitLab sandbox (M6 checklist)."""

import logging
from typing import Any, Optional
from urllib.parse import quote

import httpx

log = logging.getLogger("devcake.forge")


class GitLabForge:
    def __init__(self, repo_url: str, token: str, reviewer_token: str | None = None,
                 base: str = "https://gitlab.com"):
        path = repo_url.removeprefix(base).strip("/").removesuffix(".git")
        self.project = quote(path, safe="")
        self.base = base
        self.token = token
        self.reviewer_token = reviewer_token or None

    def _headers(self, reviewer: bool = False) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.reviewer_token if reviewer else self.token}

    async def _req(self, method: str, path: str, *, reviewer: bool = False,
                   **kwargs) -> Any:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.request(
                method, f"{self.base}/api/v4/projects/{self.project}{path}",
                headers=self._headers(reviewer), **kwargs)
            resp.raise_for_status()
            return resp.json() if resp.text else None

    async def get_pr_by_branch(self, branch: str) -> Optional[dict]:
        mrs = await self._req("GET", f"/merge_requests?source_branch={branch}"
                                     f"&order_by=created_at&sort=desc")
        if not mrs:
            return None
        mr = mrs[0]
        # normalize to the GitHubForge dict shape the callers rely on
        return {"number": mr["iid"], "html_url": mr["web_url"], "state": mr["state"]}

    async def post_pr_comment(self, pr_number: int, markdown: str) -> None:
        await self._req("POST", f"/merge_requests/{pr_number}/notes",
                        json={"body": markdown})

    async def approve(self, pr_number: int) -> bool:
        if not self.reviewer_token:
            return False
        await self._req("POST", f"/merge_requests/{pr_number}/approve", reviewer=True)
        return True

    async def merge(self, pr_number: int) -> None:
        await self._req("PUT", f"/merge_requests/{pr_number}/merge",
                        json={"squash": True})

    async def pr_state(self, pr_number: int) -> dict:
        mr = await self._req("GET", f"/merge_requests/{pr_number}")
        return {"state": "closed" if mr["state"] in ("closed", "merged") else "open",
                "merged": mr["state"] == "merged", "url": mr["web_url"],
                "number": mr["iid"]}

    @staticmethod
    def approval_footer(pr_url: str) -> str:
        return ("\n\n---\nTo approve and merge this MR yourself:\n"
                f"```\nglab mr approve {pr_url} && glab mr merge {pr_url} --squash\n```")
